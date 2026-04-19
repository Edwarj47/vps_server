import asyncio
import json
import logging
import os
import re
import socket
import time
from contextlib import asynccontextmanager
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
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
RECENT_MEMORY_LIMIT = int(os.getenv("RECENT_MEMORY_LIMIT", "5"))
RELEVANT_MEMORY_LIMIT = int(os.getenv("RELEVANT_MEMORY_LIMIT", "3"))
RESEARCH_SEARCH_URL = os.getenv("RESEARCH_SEARCH_URL", "https://duckduckgo.com/html/")
RESEARCH_USER_AGENT = os.getenv(
    "RESEARCH_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
)
RESEARCH_MAX_RESULTS = int(os.getenv("RESEARCH_MAX_RESULTS", "4"))
RESEARCH_MAX_FETCHES = int(os.getenv("RESEARCH_MAX_FETCHES", "3"))
RESEARCH_TIMEOUT_SEC = float(os.getenv("RESEARCH_TIMEOUT_SEC", "8"))
RESEARCH_MAX_BYTES = int(os.getenv("RESEARCH_MAX_BYTES", "300000"))

if not DISCORD_PUBLIC_KEY:
    raise RuntimeError("DISCORD_PUBLIC_KEY is required")
if not N8N_WEBHOOK_URL:
    raise RuntimeError("N8N_WEBHOOK_URL is required")

try:
    VERIFY_KEY = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
except ValueError as exc:
    raise RuntimeError("DISCORD_PUBLIC_KEY must be valid hex Ed25519") from exc

DB_POOL: asyncpg.Pool | None = None


PRIVATE_HOSTS = {"localhost", "0.0.0.0"}
PRIVATE_NET_PREFIXES = (
    "10.",
    "127.",
    "169.254.",
    "172.16.",
    "172.17.",
    "172.18.",
    "172.19.",
    "172.20.",
    "172.21.",
    "172.22.",
    "172.23.",
    "172.24.",
    "172.25.",
    "172.26.",
    "172.27.",
    "172.28.",
    "172.29.",
    "172.30.",
    "172.31.",
    "192.168.",
)


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


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_result_link = False
        self._in_snippet = False
        self._current_href = ""
        self._current_text: list[str] = []
        self._snippet_href = ""
        self._snippet_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: v or "" for k, v in attrs}
        if tag == "a" and "result__a" in attr.get("class", ""):
            self._in_result_link = True
            self._current_href = attr.get("href", "")
            self._current_text = []
        elif tag == "a" and "result__snippet" in attr.get("class", ""):
            self._in_snippet = True
            self._snippet_href = attr.get("href", "")
            self._snippet_text = []

    def handle_data(self, data: str) -> None:
        if self._in_result_link:
            self._current_text.append(data)
        elif self._in_snippet:
            self._snippet_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_result_link:
            title = " ".join(" ".join(self._current_text).split())
            url = _normalize_search_url(self._current_href)
            if title and url:
                self.results.append({"title": title[:180], "url": url, "snippet": ""})
            self._in_result_link = False
            self._current_href = ""
            self._current_text = []
        elif tag == "a" and self._in_snippet:
            snippet = " ".join(" ".join(self._snippet_text).split())
            url = _normalize_search_url(self._snippet_href)
            for result in reversed(self.results):
                if result["url"] == url:
                    result["snippet"] = snippet[:500]
                    break
            self._in_snippet = False
            self._snippet_href = ""
            self._snippet_text = []


class _ReadableTextParser(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "form", "nav", "footer"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag in {"p", "br", "li", "h1", "h2", "h3", "h4", "tr"} and self._skip_depth == 0:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in {"p", "li", "h1", "h2", "h3", "h4", "tr"} and self._skip_depth == 0:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            text = " ".join(data.split())
            if text:
                self.parts.append(text)

    def text(self) -> str:
        raw = " ".join(self.parts)
        raw = re.sub(r"\s+", " ", raw)
        raw = re.sub(r"\s+([,.;:!?])", r"\1", raw)
        return raw.strip()


class _StartpageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_title = False
        self._in_description = False
        self._current_href = ""
        self._current_title: list[str] = []
        self._current_description: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: v or "" for k, v in attrs}
        classes = attr.get("class", "")
        if tag in {"script", "style", "svg"}:
            self._skip_depth += 1
        elif tag == "a" and "result-link" in classes:
            self._in_title = True
            self._current_href = attr.get("href", "")
            self._current_title = []
            self._current_description = []
        elif tag == "p" and "description" in classes and self.results:
            self._in_description = True
            self._current_description = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._current_title.append(data)
        elif self._in_description:
            self._current_description.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "a" and self._in_title:
            title = " ".join(" ".join(self._current_title).split())
            if title and self._current_href:
                self.results.append({"title": title[:180], "url": unescape(self._current_href), "snippet": ""})
            self._in_title = False
            self._current_href = ""
            self._current_title = []
        elif tag == "p" and self._in_description:
            snippet = " ".join(" ".join(self._current_description).split())
            if self.results and snippet:
                self.results[-1]["snippet"] = unescape(snippet[:500])
            self._in_description = False
            self._current_description = []


def _normalize_search_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//duckduckgo.com/l/"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.query:
        uddg = parse_qs(parsed.query).get("uddg")
        if uddg:
            return unquote(uddg[0])
    return href


def _host_is_blocked(hostname: str) -> bool:
    host = hostname.strip().lower().rstrip(".")
    if not host or host in PRIVATE_HOSTS or host.endswith(".local"):
        return True
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return True
    for info in infos:
        ip = info[4][0]
        if ip == "::1" or ip.startswith("fc") or ip.startswith("fd") or ip.startswith("fe80:"):
            return True
        if ip.startswith(PRIVATE_NET_PREFIXES):
            return True
    return False


def _safe_research_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    if not parsed.hostname or _host_is_blocked(parsed.hostname):
        return ""
    return url


def _clean_source_text(html: str) -> str:
    parser = _ReadableTextParser()
    parser.feed(html)
    return parser.text()[:5000]


def _extract_relevant_sentences(query: str, text: str, limit: int = 3) -> list[str]:
    terms = {
        term
        for term in re.findall(r"[a-z0-9]{4,}", query.lower())
        if term not in {"what", "where", "when", "best", "tell", "about", "with", "from", "that"}
    }
    sentences = re.split(r"(?<=[.!?])\s+", text)
    scored: list[tuple[int, str]] = []
    for sentence in sentences:
        cleaned = " ".join(sentence.split())
        if len(cleaned) < 40:
            continue
        score = sum(1 for term in terms if term in cleaned.lower())
        if score:
            scored.append((score, cleaned[:280]))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [sentence for _, sentence in scored[:limit]]


def _normalize_research_query(query: str) -> str:
    normalized = " ".join(query.lower().split())
    normalized = normalized.strip(" ?!.")
    replacements = [
        (r"^(please\s+)?(can you\s+)?(check|find|look up|search for|research)\s+", ""),
        (r"^what\s+(are|is)\s+", ""),
        (r"^the\s+", ""),
        (r"\bwhat\b", ""),
        (r"\bare\b", ""),
        (r"\bis\b", ""),
        (r"\bin\s+deland\s+florida\b", "deland florida"),
        (r"\bin\s+deland\s+fl\b", "deland florida"),
        (r"\btop\s+3\b", "top three"),
    ]
    for pattern, replacement in replacements:
        normalized = re.sub(pattern, replacement, normalized)
    normalized = re.sub(r"\b(the|are|is|what|check)\b", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or " ".join(query.split())


def _candidate_research_queries(query: str) -> list[str]:
    original = " ".join(query.split())[:200]
    normalized = _normalize_research_query(query)[:200]
    candidates = [original]
    if normalized and normalized != original.lower():
        candidates.append(normalized)
    return candidates


def _format_research_response(query: str, sources: list[dict[str, Any]], elapsed_ms: float) -> str:
    usable = [source for source in sources if source.get("facts")]
    if not usable:
        return (
            "I could not find enough usable web text for that research request. "
            "Try a more specific query or include the city, state, or official site."
        )

    lines = [f"Research results for: {query}", ""]
    for idx, source in enumerate(usable, start=1):
        lines.append(f"{idx}. {source['title']}")
        for fact in source["facts"][:2]:
            lines.append(f"   - {fact}")
        lines.append(f"   Source: {source['url']}")
    lines.append("")
    lines.append(f"Checked {len(sources)} source(s) in {elapsed_ms / 1000:.1f}s. Web content is untrusted; verify before acting.")
    return "\n".join(lines)[:1900]


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


async def _update_job_metadata(job_id: int | None, metadata: dict[str, Any]) -> None:
    if DB_POOL is None or job_id is None:
        return
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            update agent_jobs
            set metadata_json = metadata_json || $2::jsonb, updated_at = now()
            where id = $1
            """,
            job_id,
            json.dumps(metadata),
        )


async def _record_tool_call(
    job_id: int | None,
    tool_name: str,
    status: str,
    *,
    classification: str = "read_only",
    input_json: dict[str, Any] | None = None,
    output_json: dict[str, Any] | None = None,
    error_detail: str = "",
) -> None:
    if DB_POOL is None:
        return
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            insert into agent_tool_calls (
                job_id, tool_name, classification, status, input_json,
                output_json, error_detail, completed_at
            )
            values ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, case when $4 in ('completed', 'failed') then now() else null end)
            """,
            job_id,
            tool_name,
            classification,
            status,
            json.dumps(input_json or {}),
            json.dumps(output_json or {}),
            error_detail[:2000],
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
            select role, content, max(created_at) as created_at
            from discord_chat_memory
            where user_id = $1 and content ilike '%' || $2 || '%'
            group by role, content
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


async def _search_web(query: str) -> list[dict[str, str]]:
    safe_query = " ".join(query.split())[:200]
    if len(safe_query) < 3:
        return []

    headers = {"user-agent": RESEARCH_USER_AGENT}
    for candidate in _candidate_research_queries(safe_query):
        url = f"{RESEARCH_SEARCH_URL}?q={quote_plus(candidate)}"
        async with httpx.AsyncClient(
            timeout=RESEARCH_TIMEOUT_SEC,
            follow_redirects=True,
            headers=headers,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text[:RESEARCH_MAX_BYTES]

        parser = _DuckDuckGoParser()
        parser.feed(html)
        results: list[dict[str, str]] = []
        seen: set[str] = set()
        for result in parser.results:
            safe_url = _safe_research_url(result["url"])
            if not safe_url or safe_url in seen:
                continue
            seen.add(safe_url)
            results.append({"title": result["title"], "url": safe_url, "snippet": result.get("snippet", "")})
            if len(results) >= max(1, min(RESEARCH_MAX_RESULTS, 8)):
                break
        if results:
            if candidate != safe_query:
                logger.info("research query normalized original=%r candidate=%r", safe_query, candidate)
            return results
    return []


async def _search_startpage(query: str) -> list[dict[str, str]]:
    safe_query = " ".join(query.split())[:200]
    if len(safe_query) < 3:
        return []
    headers = {"user-agent": RESEARCH_USER_AGENT}
    for candidate in _candidate_research_queries(safe_query):
        async with httpx.AsyncClient(
            timeout=RESEARCH_TIMEOUT_SEC,
            follow_redirects=True,
            headers=headers,
        ) as client:
            resp = await client.get("https://www.startpage.com/sp/search", params={"query": candidate})
            resp.raise_for_status()
            html = resp.text[:RESEARCH_MAX_BYTES]

        parser = _StartpageParser()
        parser.feed(html)
        results: list[dict[str, str]] = []
        seen: set[str] = set()
        for result in parser.results:
            safe_url = _safe_research_url(result["url"])
            if not safe_url or safe_url in seen:
                continue
            seen.add(safe_url)
            results.append({"title": result["title"], "url": safe_url, "snippet": result.get("snippet", "")})
            if len(results) >= max(1, min(RESEARCH_MAX_RESULTS, 8)):
                break
        if results:
            logger.info("research search backend=startpage results=%s", len(results))
            return results
    return []


async def _fetch_research_source(client: httpx.AsyncClient, query: str, result: dict[str, str]) -> dict[str, Any]:
    source: dict[str, Any] = {
        "title": result["title"],
        "url": result["url"],
        "snippet": result.get("snippet", ""),
        "status": "failed",
        "facts": [result["snippet"]] if result.get("snippet") else [],
    }
    try:
        resp = await client.get(result["url"])
        source["http_status"] = resp.status_code
        if resp.status_code >= 400:
            source["error"] = f"http_{resp.status_code}"
            if source["facts"]:
                source["status"] = "snippet_only"
            return source
        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            source["error"] = "unsupported_content_type"
            if source["facts"]:
                source["status"] = "snippet_only"
            return source
        text = _clean_source_text(resp.text[:RESEARCH_MAX_BYTES])
        facts = _extract_relevant_sentences(query, text)
        if source["facts"]:
            facts = source["facts"] + [fact for fact in facts if fact not in source["facts"]]
        source["status"] = "completed" if facts else "no_relevant_text"
        source["facts"] = facts
        source["text_chars"] = len(text)
        return source
    except Exception as exc:
        source["error"] = type(exc).__name__
        if source["facts"]:
            source["status"] = "snippet_only"
        return source


async def _run_web_research(job_id: int | None, query: str) -> tuple[str, dict[str, Any]]:
    started = time.perf_counter()
    search_results = await _search_web(query)
    search_backend = "duckduckgo"
    if not search_results:
        search_results = await _search_startpage(query)
        search_backend = "startpage"
    limited = search_results[: max(1, min(RESEARCH_MAX_FETCHES, 5))]

    async with httpx.AsyncClient(
        timeout=RESEARCH_TIMEOUT_SEC,
        follow_redirects=True,
        headers={"user-agent": RESEARCH_USER_AGENT},
        limits=httpx.Limits(max_connections=3),
    ) as client:
        sources = await asyncio.gather(
            *[_fetch_research_source(client, query, result) for result in limited]
        )

    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    metadata = {
        "research_ms": elapsed_ms,
        "search_backend": search_backend,
        "search_results": len(search_results),
        "sources_checked": len(sources),
        "sources_with_facts": sum(1 for source in sources if source.get("facts")),
    }
    await _record_tool_call(
        job_id,
        "web_research",
        "completed",
        input_json={
            "query_chars": len(query),
            "max_results": RESEARCH_MAX_RESULTS,
            "max_fetches": RESEARCH_MAX_FETCHES,
        },
        output_json={
            **metadata,
            "sources": [
                {
                    "title": source.get("title"),
                    "url": source.get("url"),
                    "status": source.get("status"),
                    "http_status": source.get("http_status"),
                }
                for source in sources
            ],
        },
    )
    return _format_research_response(query, sources, elapsed_ms), metadata


async def _recent_memory(ctx: dict[str, str], limit: int = RECENT_MEMORY_LIMIT) -> list[dict[str, str]]:
    if DB_POOL is None:
        return []

    async with DB_POOL.acquire() as conn:
        rows = await conn.fetch(
            """
            select role, content, max(created_at) as created_at
            from discord_chat_memory
            where session_id = $1
            group by role, content
            order by created_at desc
            limit $2
            """,
            _session_id(ctx),
            max(1, min(limit, 10)),
        )

    return [
        {
            "role": str(row["role"]),
            "content": " ".join(str(row["content"]).split())[:1200],
            "created_at": row["created_at"].isoformat(),
        }
        for row in reversed(rows)
    ]


async def _relevant_memory(ctx: dict[str, str], prompt: str, limit: int = RELEVANT_MEMORY_LIMIT) -> list[dict[str, str]]:
    if DB_POOL is None:
        return []
    query = prompt.strip()
    if len(query) < 3:
        return []

    followup_research_terms = ("link", "links", "source", "sources", "provided", "options", "recommend", "last message")
    if any(term in query.lower() for term in followup_research_terms):
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch(
                """
                select role, content, created_at
                from discord_chat_memory
                where session_id = $1
                  and role = 'assistant'
                  and metadata_json->>'command' = 'research'
                order by created_at desc
                limit $2
                """,
                _session_id(ctx),
                max(1, min(limit, 5)),
            )
        if rows:
            return [
                {
                    "role": str(row["role"]),
                    "content": " ".join(str(row["content"]).split())[:1600],
                    "created_at": row["created_at"].isoformat(),
                }
                for row in rows
            ]

    async with DB_POOL.acquire() as conn:
        rows = await conn.fetch(
            """
            select role, content, max(created_at) as created_at
            from discord_chat_memory
            where user_id = $1 and content ilike '%' || $2 || '%'
            group by role, content
            order by created_at desc
            limit $3
            """,
            ctx["user_id"] or "unknown",
            query[:200],
            max(1, min(limit, 5)),
        )

    return [
        {
            "role": str(row["role"]),
            "content": " ".join(str(row["content"]).split())[:600],
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]


async def _build_agent_context(ctx: dict[str, str], prompt: str) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    recent, relevant = await asyncio.gather(
        _recent_memory(ctx),
        _relevant_memory(ctx, prompt),
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    context = {
        "memory_version": 1,
        "recent_limit": RECENT_MEMORY_LIMIT,
        "relevant_limit": RELEVANT_MEMORY_LIMIT,
        "recent_messages": recent,
        "relevant_memories": relevant,
    }
    metrics = {
        "memory_ms": elapsed_ms,
        "recent_count": len(recent),
        "relevant_count": len(relevant),
    }
    return context, metrics


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


async def _forward_to_n8n(raw_body: bytes, agent_context: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    headers = {"content-type": "application/json"}
    if N8N_WEBHOOK_SHARED_SECRET:
        headers["x-n8n-shared-secret"] = N8N_WEBHOOK_SHARED_SECRET

    body = json.loads(raw_body)
    if agent_context is not None:
        body["agent_context"] = agent_context

    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=N8N_PROXY_TIMEOUT_SEC) as client:
        n8n_resp = await client.post(
            N8N_WEBHOOK_URL,
            content=json.dumps(body).encode("utf-8"),
            headers=headers,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info("n8n forward status=%s elapsed_ms=%s", n8n_resp.status_code, elapsed_ms)
        n8n_resp.raise_for_status()
        content = _extract_content(n8n_resp.json())
        return content, {
            "n8n_ms": elapsed_ms,
            "n8n_status": n8n_resp.status_code,
            "response_chars": len(content),
        }


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
    risk_level = "read_only"
    requires_approval = False
    if command == "task":
        risk_level = "safe_write"
    elif command == "codex":
        risk_level = "approval_required"
        requires_approval = True

    job_id = await _insert_job(
        ctx,
        command or "legacy",
        prompt,
        risk_level=risk_level,
        requires_approval=requires_approval,
        metadata={"phase": "discord-agent-mvp"},
    )

    try:
        if command == "status":
            content = await _status_report()
        elif command == "memory":
            content = await _search_memory(ctx, prompt)
            await _record_memory_event(ctx, "search", prompt, {"command": command})
        elif command == "research":
            if len(prompt) < 3:
                content = "Give me at least 3 characters to research."
            else:
                content, research_metrics = await _run_web_research(job_id, prompt)
                await _update_job_metadata(job_id, {"research": research_metrics})
                await _save_chat_memory(ctx, "user", prompt, {"command": "research"})
                await _save_chat_memory(
                    ctx,
                    "assistant",
                    content,
                    {
                        "command": "research",
                        "job_id": job_id,
                        "sources_with_facts": research_metrics.get("sources_with_facts", 0),
                    },
                )
        elif command in {"task", "codex"}:
            await _finish_job(job_id, "queued", "Queued for future worker implementation.")
            await _audit_interaction(ctx, "queued", prompt, model=f"router:{command}")
            return (
                f"Queued `{command}` job"
                f"{f' #{job_id}' if job_id else ''}.\n"
                f"Risk class: `{risk_level}`.\n"
                "The Phase 1 router has recorded it, but execution bridge workers are not enabled yet."
            )
        elif command == "ask" or not command:
            # The existing n8n workflow already persists chat turns in
            # discord_chat_memory. Avoid double-writing the same prompt/reply here.
            command_started = time.perf_counter()
            agent_context = None
            metrics: dict[str, Any] = {}
            if prompt:
                agent_context, metrics = await _build_agent_context(ctx, prompt)
            content, n8n_metrics = await _forward_to_n8n(raw_body, agent_context)
            metrics.update(n8n_metrics)
            metrics["total_ms"] = round((time.perf_counter() - command_started) * 1000, 2)
            logger.info(
                "ask timing job_id=%s memory_ms=%s n8n_ms=%s total_ms=%s recent=%s relevant=%s",
                job_id,
                metrics.get("memory_ms"),
                metrics.get("n8n_ms"),
                metrics.get("total_ms"),
                metrics.get("recent_count"),
                metrics.get("relevant_count"),
            )
            await _update_job_metadata(job_id, {"timing": metrics})
            await _record_tool_call(
                job_id,
                "n8n_ollama_chat",
                "completed",
                input_json={
                    "prompt_chars": len(prompt),
                    "recent_count": metrics.get("recent_count", 0),
                    "relevant_count": metrics.get("relevant_count", 0),
                },
                output_json=metrics,
            )
        else:
            content, _ = await _forward_to_n8n(raw_body)

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


@app.get("/agent/research")
async def agent_research(q: str):
    content, metadata = await _run_web_research(None, q)
    return {"ok": True, "metadata": metadata, "content": content}


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
