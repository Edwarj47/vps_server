import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("discord-interactions-proxy")

DISCORD_PUBLIC_KEY = os.getenv("DISCORD_PUBLIC_KEY", "").strip()
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "").strip()
N8N_WEBHOOK_SHARED_SECRET = os.getenv("N8N_WEBHOOK_SHARED_SECRET", "").strip()
N8N_PROXY_TIMEOUT_SEC = float(os.getenv("N8N_PROXY_TIMEOUT_SEC", "30"))
DISCORD_MAX_TIMESTAMP_AGE_SEC = int(os.getenv("DISCORD_MAX_TIMESTAMP_AGE_SEC", "300"))

if not DISCORD_PUBLIC_KEY:
    raise RuntimeError("DISCORD_PUBLIC_KEY is required")
if not N8N_WEBHOOK_URL:
    raise RuntimeError("N8N_WEBHOOK_URL is required")

try:
    VERIFY_KEY = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
except ValueError as exc:
    raise RuntimeError("DISCORD_PUBLIC_KEY must be valid hex Ed25519") from exc

app = FastAPI()


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


async def _run_interaction(payload: dict, raw_body: bytes) -> None:
    app_id = payload.get("application_id")
    token = payload.get("token")
    if not app_id or not token:
        logger.error("forward failure: missing application_id/token in interaction payload")
        return

    followup_url = f"https://discord.com/api/v10/webhooks/{app_id}/{token}"
    content = "Sorry, I hit a timeout while generating a reply."

    headers = {"content-type": "application/json"}
    if N8N_WEBHOOK_SHARED_SECRET:
        headers["x-n8n-shared-secret"] = N8N_WEBHOOK_SHARED_SECRET

    try:
        async with httpx.AsyncClient(timeout=N8N_PROXY_TIMEOUT_SEC) as client:
            n8n_resp = await client.post(
                N8N_WEBHOOK_URL,
                content=raw_body,
                headers=headers,
            )
            logger.info("forward success status=%s", n8n_resp.status_code)
            n8n_resp.raise_for_status()
            content = _extract_content(n8n_resp.json())
    except Exception:
        logger.exception("forward failure: n8n request error")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            discord_resp = await client.post(
                followup_url,
                json={
                    "content": content[:1900],
                    "allowed_mentions": {"parse": []},
                },
            )
            logger.info("followup send status=%s", discord_resp.status_code)
    except Exception:
        logger.exception("followup failure: could not send message to Discord")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


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
