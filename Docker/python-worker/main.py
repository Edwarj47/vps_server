import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("discord-agent-gateway")

DISCORD_PUBLIC_KEY = os.getenv("DISCORD_PUBLIC_KEY", "").strip()
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "").strip()
N8N_WEBHOOK_SHARED_SECRET = os.getenv("N8N_WEBHOOK_SHARED_SECRET", "").strip()
N8N_PROXY_TIMEOUT_SEC = float(os.getenv("N8N_PROXY_TIMEOUT_SEC", "30"))
DISCORD_MAX_TIMESTAMP_AGE_SEC = int(os.getenv("DISCORD_MAX_TIMESTAMP_AGE_SEC", "300"))

POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.getenv("POSTGRES_DB", "n8n")
POSTGRES_USER = os.getenv("POSTGRES_USER", "").strip()
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "").strip()

N8N_INTERNAL_HEALTH_URL = os.getenv("N8N_INTERNAL_HEALTH_URL", "http://n8n:5678/healthz")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")

if not DISCORD_PUBLIC_KEY:
    raise RuntimeError("DISCORD_PUBLIC_KEY is required")
if not N8N_WEBHOOK_URL:
    raise RuntimeError("N8N_WEBHOOK_URL is required")

try:
    VERIFY_KEY = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
except ValueError as exc:
    raise RuntimeError("DISCORD_PUBLIC_KEY must be valid hex Ed25519") from exc

DB_POOL: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _init_db_pool()
    yield
    if DB_POOL is not None:
        await DB_POOL.close()


app = FastAPI(lifespan=lifespan)


async def _init_db_pool() -> None:
    global DB_POOL
    if not POSTGRES_USER or not POSTGRES_PASSWORD:
        logger.warning("postgres disabled: POSTGRES_USER/POSTGRES_PASSWORD not configured")
        return

    try:
        DB_POOL = await asyncpg.create_pool(
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            database=POSTGRES_DB,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            min_size=1,
            max_size=5,
            command_timeout=10,
        )
        async with DB_POOL.acquire() as conn:
            await _ensure_agent_tables(conn)
        logger.info("postgres pool initialized")
    except Exception:
        DB_POOL = None
        logger.exception("postgres init failed; continuing without persistence")


async def _ensure_agent_tables(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        create table if not exists agent_jobs (
            id bigserial primary key,
            discord_interaction_id text,
            guild_id text,
            channel_id text,
            user_id text,
            command text not null,
            prompt text not null default '',
            status text not null default 'queued',
            risk_level text not null default 'read_only',
            requires_approval boolean not null default false,
            result_summary text,
            metadata_json jsonb not null default '{}'::jsonb,
            created_at timestamptz not null default now(),
            updated_at timestamptz not null default now()
        );

        create index if not exists idx_agent_jobs_status_created
            on agent_jobs (status, created_at desc);
        create index if not exists idx_agent_jobs_user_created
            on agent_jobs (user_id, created_at desc);

        create table if not exists agent_tool_calls (
            id bigserial primary key,
            job_id bigint references agent_jobs(id) on delete set null,
            tool_name text not null,
            classification text not null default 'read_only',
            status text not null,
            input_json jsonb not null default '{}'::jsonb,
            output_json jsonb not null default '{}'::jsonb,
            error_detail text,
            created_at timestamptz not null default now(),
            completed_at timestamptz
        );

        create index if not exists idx_agent_tool_calls_job_created
            on agent_tool_calls (job_id, created_at desc);

        create table if not exists agent_memory_events (
            id bigserial primary key,
            user_id text,
            guild_id text,
            channel_id text,
            session_id text,
            event_type text not null,
            content text not null,
            metadata_json jsonb not null default '{}'::jsonb,
            created_at timestamptz not null default now()
        );

        create index if not exists idx_agent_memory_events_user_created
            on agent_memory_events (user_id, created_at desc);

        create table if not exists agent_approvals (
            id bigserial primary key,
            job_id bigint references agent_jobs(id) on delete cascade,
            status text not null default 'pending',
            requested_by text,
            approved_by text,
            reason text,
            created_at timestamptz not null default now(),
            decided_at timestamptz
        );

        create index if not exists idx_agent_approvals_status_created
            on agent_approvals (status, created_at desc);
        """
    )


def _verify_signature(signature_hex: str, timestamp: str, raw_body: bytes) -> bool:
    try:
        signature = bytes.fromhex(signature_hex)
        ts = int(timestamp)
    except ValueError:
        return False

    if abs(time.time() - ts) > DISCORD_MAX_TIMESTAMP_AGE_SEC:
        return False

    try:
        VERIFY_KEY.verify(timestamp.encode("utf-8") + raw_body, signature)
        return True
    except BadSignatureError:
        return False


def _extract_content(n8n_body: Any) -> str:
    data = n8n_body
    if isinstance(data, list) and data:
        data = data[0]

    if isinstance(data, dict):
        if isinstance(data.get("data"), dict) and isinstance(data["data"].get("content"), str):
            return data["data"]["content"]
        for key in ("output", "response", "text", "answer", "message", "content"):
            if isinstance(data.get(key), str) and data[key].strip():
                return data[key].strip()

    return "Sorry, I could not generate a response right now."


def _discord_context(payload: dict) -> dict[str, str]:
    user = payload.get("user") or payload.get("member", {}).get("user") or {}
    return {
        "interaction_id": str(payload.get("id") or ""),
        "application_id": str(payload.get("application_id") or ""),
        "token": str(payload.get("token") or ""),
        "guild_id": str(payload.get("guild_id") or ""),
        "channel_id": str(payload.get("channel_id") or ""),
        "user_id": str(user.get("id") or ""),
        "username": str(user.get("username") or user.get("global_name") or ""),
    }


def _command_name(payload: dict) -> str:
    if payload.get("type") != 2:
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return str(data.get("name") or "").lower()


def _flatten_options(options: list[dict] | None) -> list[dict]:
    flat: list[dict] = []
    for option in options or []:
        flat.append(option)
        nested = option.get("options")
        if isinstance(nested, list):
            flat.extend(_flatten_options(nested))
    return flat


def _option_text(payload: dict) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    options = _flatten_options(data.get("options") if isinstance(data.get("options"), list) else [])
    preferred = {"prompt", "question", "message", "text", "query", "task", "request"}
    for option in options:
        if str(option.get("name", "")).lower() in preferred and isinstance(option.get("value"), str):
            return option["value"].strip()
    for option in options:
        if isinstance(option.get("value"), str):
            return option["value"].strip()
    return ""


def _session_id(ctx: dict[str, str]) -> str:
    guild = ctx.get("guild_id") or "dm"
    channel = ctx.get("channel_id") or "unknown"
    user = ctx.get("user_id") or "unknown"
    return f"discord:{guild}:{channel}:{user}"


async def _insert_job(
    ctx: dict[str, str],
    command: str,
    prompt: str,
    *,
    status: str = "running",
    risk_level: str = "read_only",
    requires_approval: bool = False,
    metadata: dict | None = None,
) -> int | None:
    if DB_POOL is None:
        return None
    async with DB_POOL.acquire() as conn:
        return await conn.fetchval(
            """
            insert into agent_jobs (
                discord_interaction_id, guild_id, channel_id, user_id,
                command, prompt, status, risk_level, requires_approval, metadata_json
            )
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
            returning id
            """,
            ctx["interaction_id"],
            ctx["guild_id"],
            ctx["channel_id"],
            ctx["user_id"],
            command,
            prompt,
            status,
            risk_level,
            requires_approval,
            json.dumps(metadata or {}),
        )


async def _finish_job(job_id: int | None, status: str, result_summary: str = "") -> None:
    if DB_POOL is None or job_id is None:
        return
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            update agent_jobs
            set status = $2, result_summary = $3, updated_at = now()
            where id = $1
            """,
            job_id,
            status,
            result_summary[:4000],
        )


async def _audit_interaction(
    ctx: dict[str, str],
    status: str,
    prompt: str,
    *,
    model: str = "router",
    error_detail: str = "",
) -> None:
    if DB_POOL is None:
        return
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            insert into discord_interaction_audit (
                interaction_id, user_id, guild_id, channel_id, session_id,
                status, prompt_len, model, error_detail
            )
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            ctx["interaction_id"],
            ctx["user_id"],
            ctx["guild_id"],
            ctx["channel_id"],
            _session_id(ctx),
            status,
            len(prompt),
            model,
            error_detail[:1000],
        )


async def _save_chat_memory(ctx: dict[str, str], role: str, content: str, metadata: dict | None = None) -> None:
    if DB_POOL is None or not content.strip() or role not in {"user", "assistant"}:
        return
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            insert into discord_chat_memory (
                session_id, role, content, guild_id, channel_id, user_id, metadata_json
            )
            values ($1, $2, $3, $4, $5, $6, $7::jsonb)
            """,
            _session_id(ctx),
            role,
            content[:12000],
            ctx["guild_id"],
            ctx["channel_id"],
            ctx["user_id"] or "unknown",
            json.dumps(metadata or {}),
        )


async def _search_memory(ctx: dict[str, str], query: str) -> str:
    if DB_POOL is None:
        return "Memory search is unavailable because Postgres is not connected."
    if len(query) < 3:
        return "Give me at least 3 characters to search memory."

    async with DB_POOL.acquire() as conn:
        rows = await conn.fetch(
            """
            select role, content, created_at
            from discord_chat_memory
            where user_id = $1 and content ilike '%' || $2 || '%'
            order by created_at desc
            limit 5
            """,
            ctx["user_id"] or "unknown",
            query[:200],
        )

    if not rows:
        return "No matching memory found for your user history."

    lines = ["Memory matches:"]
    for row in rows:
        content = " ".join(str(row["content"]).split())
        lines.append(f"- {row['created_at']:%Y-%m-%d %H:%M} {row['role']}: {content[:220]}")
    return "\n".join(lines)


async def _record_memory_event(ctx: dict[str, str], event_type: str, content: str, metadata: dict | None = None) -> None:
    if DB_POOL is None:
        return
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            insert into agent_memory_events (
                user_id, guild_id, channel_id, session_id, event_type, content, metadata_json
            )
            values ($1, $2, $3, $4, $5, $6, $7::jsonb)
            """,
            ctx["user_id"],
            ctx["guild_id"],
            ctx["channel_id"],
            _session_id(ctx),
            event_type,
            content[:12000],
            json.dumps(metadata or {}),
        )


async def _check_http_json(name: str, url: str) -> tuple[str, bool, str]:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
        if resp.status_code >= 400:
            return name, False, f"http {resp.status_code}"
        return name, True, "ok"
    except Exception as exc:
        return name, False, type(exc).__name__


async def _status_report() -> str:
    checks: list[tuple[str, bool, str]] = []
    checks.append(await _check_http_json("n8n", N8N_INTERNAL_HEALTH_URL))
    checks.append(await _check_http_json("ollama", f"{OLLAMA_BASE_URL}/api/tags"))

    if DB_POOL is None:
        checks.append(("postgres", False, "pool unavailable"))
    else:
        try:
            async with DB_POOL.acquire() as conn:
                await conn.fetchval("select 1")
            checks.append(("postgres", True, "ok"))
        except Exception as exc:
            checks.append(("postgres", False, type(exc).__name__))

    lines = ["Agent status:"]
    for name, ok, detail in checks:
        marker = "OK" if ok else "FAIL"
        lines.append(f"- {name}: {marker} ({detail})")
    return "\n".join(lines)


async def _forward_to_n8n(raw_body: bytes) -> str:
    headers = {"content-type": "application/json"}
    if N8N_WEBHOOK_SHARED_SECRET:
        headers["x-n8n-shared-secret"] = N8N_WEBHOOK_SHARED_SECRET

    async with httpx.AsyncClient(timeout=N8N_PROXY_TIMEOUT_SEC) as client:
        n8n_resp = await client.post(
            N8N_WEBHOOK_URL,
            content=raw_body,
            headers=headers,
        )
        logger.info("n8n forward status=%s", n8n_resp.status_code)
        n8n_resp.raise_for_status()
        return _extract_content(n8n_resp.json())


async def _send_followup(ctx: dict[str, str], content: str) -> None:
    app_id = ctx.get("application_id")
    token = ctx.get("token")
    if not app_id or not token:
        logger.error("followup failure: missing application_id/token")
        return

    followup_url = f"https://discord.com/api/v10/webhooks/{app_id}/{token}"
    async with httpx.AsyncClient(timeout=10) as client:
        discord_resp = await client.post(
            followup_url,
            json={
                "content": content[:1900],
                "allowed_mentions": {"parse": []},
            },
        )
        logger.info("followup send status=%s", discord_resp.status_code)


async def _handle_agent_command(payload: dict, raw_body: bytes) -> str:
    ctx = _discord_context(payload)
    command = _command_name(payload)
    prompt = _option_text(payload)
    job_id = await _insert_job(ctx, command or "legacy", prompt, metadata={"phase": "discord-agent-mvp"})

    try:
        if command == "status":
            content = await _status_report()
        elif command == "memory":
            content = await _search_memory(ctx, prompt)
            await _record_memory_event(ctx, "search", prompt, {"command": command})
        elif command in {"task", "codex"}:
            risk = "approval_required" if command == "codex" else "safe_write"
            await _finish_job(job_id, "queued", "Queued for future worker implementation.")
            await _audit_interaction(ctx, "queued", prompt, model=f"router:{command}")
            return (
                f"Queued `{command}` job"
                f"{f' #{job_id}' if job_id else ''}.\n"
                f"Risk class: `{risk}`.\n"
                "The Phase 1 router has recorded it, but execution bridge workers are not enabled yet."
            )
        elif command == "ask" or not command:
            if prompt:
                await _save_chat_memory(ctx, "user", prompt, {"command": command or "legacy"})
            content = await _forward_to_n8n(raw_body)
            await _save_chat_memory(ctx, "assistant", content, {"command": command or "legacy"})
        else:
            content = await _forward_to_n8n(raw_body)

        await _finish_job(job_id, "completed", content)
        await _audit_interaction(ctx, "completed", prompt, model=f"router:{command or 'legacy'}")
        return content
    except Exception as exc:
        logger.exception("agent command failed command=%s", command)
        await _finish_job(job_id, "failed", type(exc).__name__)
        await _audit_interaction(ctx, "failed", prompt, model=f"router:{command or 'legacy'}", error_detail=str(exc))
        return "Sorry, I hit an error while handling that request."


async def _run_interaction(payload: dict, raw_body: bytes) -> None:
    ctx = _discord_context(payload)
    content = await _handle_agent_command(payload, raw_body)
    try:
        await _send_followup(ctx, content)
    except Exception:
        logger.exception("followup failure: could not send message to Discord")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/agent/status")
async def agent_status() -> dict:
    return {
        "ok": True,
        "postgres": DB_POOL is not None,
        "n8n_health_url": N8N_INTERNAL_HEALTH_URL,
        "ollama_base_url": OLLAMA_BASE_URL,
    }


@app.post("/discord/interactions")
async def discord_interactions(request: Request):
    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")
    raw_body = await request.body()

    if not signature or not timestamp:
        logger.warning("verification failure: missing Discord signature headers")
        return JSONResponse(status_code=401, content={"error": "invalid request signature"})

    if not _verify_signature(signature, timestamp, raw_body):
        logger.warning("verification failure: invalid Discord signature")
        return JSONResponse(status_code=401, content={"error": "invalid request signature"})

    logger.info("verification success")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "invalid json body"})

    if payload.get("type") == 1:
        logger.info("ping request")
        return JSONResponse(status_code=200, content={"type": 1})

    asyncio.create_task(_run_interaction(payload, raw_body))
    return JSONResponse(status_code=200, content={"type": 5})
