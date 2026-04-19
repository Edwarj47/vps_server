import asyncio
import json
import logging
import os
import re
import socket
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
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
OLLAMA_CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "llama3.2:3b")
AGENT_WORKER_ENABLED = os.getenv("AGENT_WORKER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
AGENT_WORKER_POLL_SEC = float(os.getenv("AGENT_WORKER_POLL_SEC", "3"))
AGENT_WORKER_BATCH_SIZE = int(os.getenv("AGENT_WORKER_BATCH_SIZE", "2"))
AGENT_SCHEDULE_TIMEZONE = os.getenv("AGENT_SCHEDULE_TIMEZONE", "America/New_York")
AGENT_APPROVER_USER_IDS = {
    user_id.strip()
    for user_id in os.getenv("AGENT_APPROVER_USER_IDS", "").split(",")
    if user_id.strip()
}
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
DEFAULT_NEWS_FLASH_SOURCES = (
    "Hacker News Front Page=https://hnrss.org/frontpage;"
    "Hacker News Best=https://hnrss.org/best;"
    "OpenAI News=https://openai.com/news/rss.xml;"
    "Anthropic News=https://www.anthropic.com/news"
)
NEWS_FLASH_SOURCES = os.getenv("NEWS_FLASH_SOURCES", "").strip() or DEFAULT_NEWS_FLASH_SOURCES
NEWS_FLASH_MAX_ITEMS = int(os.getenv("NEWS_FLASH_MAX_ITEMS", "8"))
NEWS_FLASH_TIMEOUT_SEC = float(os.getenv("NEWS_FLASH_TIMEOUT_SEC", "8"))

if not DISCORD_PUBLIC_KEY:
    raise RuntimeError("DISCORD_PUBLIC_KEY is required")
if not N8N_WEBHOOK_URL:
    raise RuntimeError("N8N_WEBHOOK_URL is required")

try:
    VERIFY_KEY = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
except ValueError as exc:
    raise RuntimeError("DISCORD_PUBLIC_KEY must be valid hex Ed25519") from exc

DB_POOL: asyncpg.Pool | None = None
WORKER_TASK: asyncio.Task | None = None

CODEX_ROUTE_POLICY = "Only the explicit Discord /codex command may create Codex jobs."


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
    global WORKER_TASK
    await _init_db_pool()
    if AGENT_WORKER_ENABLED and DB_POOL is not None:
        WORKER_TASK = asyncio.create_task(_agent_worker_loop())
        logger.info("agent worker started poll_sec=%s batch_size=%s", AGENT_WORKER_POLL_SEC, AGENT_WORKER_BATCH_SIZE)
    yield
    if WORKER_TASK is not None:
        WORKER_TASK.cancel()
        try:
            await WORKER_TASK
        except asyncio.CancelledError:
            pass
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

    await conn.execute("alter table agent_jobs add column if not exists scheduled_at timestamptz;")
    await conn.execute("alter table agent_jobs add column if not exists schedule_label text;")
    await conn.execute("alter table agent_jobs add column if not exists recurrence_rule text;")
    await conn.execute("alter table agent_jobs add column if not exists last_run_at timestamptz;")
    await conn.execute(
        """
        create index if not exists idx_agent_jobs_scheduled_due
            on agent_jobs (status, scheduled_at asc)
            where status = 'scheduled'
        """
    )
    await conn.execute("alter table agent_approvals add column if not exists expires_at timestamptz;")


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


class _NewsLinkParser(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "form"}

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.links: list[dict[str, str]] = []
        self._skip_depth = 0
        self._in_link = False
        self._current_href = ""
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: v or "" for k, v in attrs}
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "a" and self._skip_depth == 0:
            href = attr.get("href", "")
            if href:
                self._in_link = True
                self._current_href = href
                self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._in_link and self._skip_depth == 0:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "a" and self._in_link:
            title = " ".join(" ".join(self._current_text).split())
            url = urljoin(self.base_url, unescape(self._current_href))
            if title and url:
                self.links.append({"title": unescape(title)[:180], "url": url})
            self._in_link = False
            self._current_href = ""
            self._current_text = []


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


def _clean_feed_text(text: str) -> str:
    cleaned = _clean_source_text(unescape(text or ""))
    return " ".join(cleaned.split())[:360]


def _news_sources() -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    for item in NEWS_FLASH_SOURCES.split(";"):
        if not item.strip() or "=" not in item:
            continue
        name, url = item.split("=", 1)
        safe_url = _safe_research_url(url.strip())
        if name.strip() and safe_url:
            sources.append((name.strip()[:80], safe_url))
    return sources


def _feed_child_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text.strip()
    for child in list(node):
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in names and child.text:
            return child.text.strip()
    return ""


def _parse_feed_datetime(value: str) -> float:
    if not value:
        return 0
    try:
        return parsedate_to_datetime(value).timestamp()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def _parse_news_feed(source_name: str, feed_url: str, body: str) -> list[dict[str, Any]]:
    root = ET.fromstring(body)
    nodes = root.findall(".//item")
    if not nodes:
        nodes = [node for node in root.findall(".//{*}entry")]
    items: list[dict[str, Any]] = []
    for node in nodes[:20]:
        title = _clean_feed_text(_feed_child_text(node, ("title",)))
        link = _feed_child_text(node, ("link",))
        if not link:
            for child in list(node):
                if child.tag.rsplit("}", 1)[-1].lower() == "link":
                    link = child.attrib.get("href", "")
                    if link:
                        break
        summary = _clean_feed_text(_feed_child_text(node, ("description", "summary", "content")))
        published = _feed_child_text(node, ("pubDate", "published", "updated"))
        if title and link:
            items.append(
                {
                    "source": source_name,
                    "title": title[:180],
                    "url": _safe_research_url(link) or feed_url,
                    "summary": summary[:360],
                    "published": published,
                    "published_ts": _parse_feed_datetime(published),
                }
            )
    return items


def _parse_news_html(source_name: str, page_url: str, body: str) -> list[dict[str, Any]]:
    parser = _NewsLinkParser(page_url)
    parser.feed(body)
    page_host = urlparse(page_url).hostname or ""
    generic_titles = {
        "about",
        "blog",
        "careers",
        "company",
        "contact",
        "developers",
        "docs",
        "documentation",
        "enterprise",
        "events",
        "news",
        "pricing",
        "privacy",
        "research",
        "safety",
        "terms",
    }
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in parser.links:
        title = _clean_feed_text(link.get("title", ""))
        url = _safe_research_url(link.get("url", ""))
        if not title or not url or url in seen:
            continue
        parsed = urlparse(url)
        path = parsed.path.lower()
        if parsed.hostname != page_host:
            continue
        if len(title) < 8 or title.lower() in generic_titles:
            continue
        if "anthropic.com" in page_host and "/news/" not in path:
            continue
        if "openai.com" in page_host and "/news/" not in path:
            continue
        seen.add(url)
        items.append(
            {
                "source": source_name,
                "title": title[:180],
                "url": url,
                "summary": "",
                "published": "",
                "published_ts": 0,
            }
        )
        if len(items) >= 12:
            break
    return items


async def _fetch_news_source(client: httpx.AsyncClient, source_name: str, feed_url: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    detail = {"source": source_name, "url": feed_url, "status": "failed", "items": 0}
    try:
        resp = await client.get(feed_url)
        detail["http_status"] = resp.status_code
        resp.raise_for_status()
        body = resp.text[: max(RESEARCH_MAX_BYTES, 1_000_000)]
        content_type = resp.headers.get("content-type", "")
        parser = "html" if "html" in content_type.lower() else "feed"
        try:
            items = _parse_news_html(source_name, feed_url, body) if parser == "html" else _parse_news_feed(source_name, feed_url, body)
        except ET.ParseError:
            parser = "html"
            items = _parse_news_html(source_name, feed_url, body)
        detail["parser"] = parser
        detail["status"] = "completed" if items else "no_items"
        detail["items"] = len(items)
        return items, detail
    except Exception as exc:
        detail["error"] = type(exc).__name__
        return [], detail


def _select_news_flash_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    max_items = max(1, min(NEWS_FLASH_MAX_ITEMS, 12))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(str(item.get("source") or "Unknown"), []).append(item)
    for source_items in grouped.values():
        source_items.sort(key=lambda item: (item.get("published_ts") or 0, item.get("title") or ""), reverse=True)
    selected: list[dict[str, Any]] = []
    while len(selected) < max_items:
        progressed = False
        for source_items in grouped.values():
            if not source_items:
                continue
            selected.append(source_items.pop(0))
            progressed = True
            if len(selected) >= max_items:
                break
        if not progressed:
            break
    return selected


def _format_news_flash(items: list[dict[str, Any]], details: list[dict[str, Any]], elapsed_ms: float) -> str:
    if not items:
        return "News Flash could not fetch usable items from the configured sources."

    lines = ["News Flash", ""]
    checked = ", ".join(f"{d['source']}:{d['status']}" for d in details)
    footer = [
        "",
        f"Checked {len(details)} feed(s) in {elapsed_ms / 1000:.1f}s. Feed text is untrusted; verify before acting.",
        f"Feeds: {checked}",
    ]
    for idx, item in enumerate(_select_news_flash_items(items), start=1):
        item_lines = [f"{idx}. {item['title']}"]
        summary = str(item.get("summary") or "")
        if summary and not summary.startswith("Article URL:"):
            item_lines.append(f"   - {summary}")
        item_lines.append(f"   Source: {item['source']} - {item['url']}")
        if len("\n".join(lines + item_lines + footer)) > 1900 and idx > 1:
            break
        lines.extend(item_lines)
    lines.extend(footer)
    return "\n".join(lines)[:1900]


async def _run_news_flash(job_id: int | None) -> tuple[str, dict[str, Any]]:
    started = time.perf_counter()
    sources = _news_sources()
    async with httpx.AsyncClient(
        timeout=NEWS_FLASH_TIMEOUT_SEC,
        follow_redirects=True,
        headers={"user-agent": RESEARCH_USER_AGENT},
        limits=httpx.Limits(max_connections=4),
    ) as client:
        results = await asyncio.gather(*[_fetch_news_source(client, name, url) for name, url in sources])
    items = [item for source_items, _ in results for item in source_items]
    details = [detail for _, detail in results]
    items.sort(key=lambda item: (item.get("published_ts") or 0, item.get("title") or ""), reverse=True)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    metadata = {
        "news_flash_ms": elapsed_ms,
        "sources_checked": len(details),
        "sources_completed": sum(1 for detail in details if detail.get("status") == "completed"),
        "items_found": len(items),
    }
    await _record_tool_call(
        job_id,
        "news_flash",
        "completed",
        input_json={"source_count": len(sources), "max_items": NEWS_FLASH_MAX_ITEMS},
        output_json={**metadata, "sources": details},
    )
    return _format_news_flash(items, details, elapsed_ms), metadata


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


def _subcommand_name(payload: dict) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    options = data.get("options") if isinstance(data.get("options"), list) else []
    for option in options:
        if int(option.get("type") or 0) == 1:
            return str(option.get("name") or "").lower()
    return ""


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


def _option_value(payload: dict, name: str) -> Any:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    options = _flatten_options(data.get("options") if isinstance(data.get("options"), list) else [])
    for option in options:
        if str(option.get("name", "")).lower() == name.lower():
            return option.get("value")
    return None


def _session_id(ctx: dict[str, str]) -> str:
    guild = ctx.get("guild_id") or "dm"
    channel = ctx.get("channel_id") or "unknown"
    user = ctx.get("user_id") or "unknown"
    return f"discord:{guild}:{channel}:{user}"


def _schedule_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(AGENT_SCHEDULE_TIMEZONE)
    except ZoneInfoNotFoundError:
        logger.warning("invalid AGENT_SCHEDULE_TIMEZONE=%s; falling back to UTC", AGENT_SCHEDULE_TIMEZONE)
        return ZoneInfo("UTC")


def _parse_time_fragment(text: str) -> tuple[int, int] | None:
    match = re.search(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text, flags=re.I)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = (match.group(3) or "").lower()
    if minute > 59:
        return None
    if suffix:
        if hour < 1 or hour > 12:
            return None
        if suffix == "pm" and hour != 12:
            hour += 12
        if suffix == "am" and hour == 12:
            hour = 0
    elif hour > 23:
        return None
    return hour, minute


def _parse_task_schedule(prompt: str) -> dict[str, Any] | None:
    normalized = " ".join(prompt.lower().split())
    tz = _schedule_timezone()
    now_local = datetime.now(tz)
    recurrence_rule = ""

    if re.search(r"\b(every day|daily)\b", normalized):
        recurrence_rule = "daily"
    elif re.search(r"\b(every week|weekly)\b", normalized):
        recurrence_rule = "weekly"

    relative = re.search(r"\bin\s+(\d{1,3})\s*(minute|minutes|hour|hours|day|days)\b", normalized)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        if unit.startswith("minute"):
            scheduled_local = now_local + timedelta(minutes=amount)
        elif unit.startswith("hour"):
            scheduled_local = now_local + timedelta(hours=amount)
        else:
            scheduled_local = now_local + timedelta(days=amount)
        return {
            "scheduled_at": scheduled_local.astimezone(timezone.utc),
            "schedule_label": scheduled_local.strftime(f"%Y-%m-%d %H:%M {tz.key}"),
            "recurrence_rule": recurrence_rule,
        }

    time_fragment = _parse_time_fragment(normalized)
    has_schedule_word = any(
        term in normalized
        for term in ("tomorrow", "tonight", "today", "at ", "daily", "every day", "weekly", "every week")
    )
    if not time_fragment or not has_schedule_word:
        return None

    day_offset = 0
    if "tomorrow" in normalized:
        day_offset = 1
    scheduled_local = now_local.replace(
        hour=time_fragment[0],
        minute=time_fragment[1],
        second=0,
        microsecond=0,
    ) + timedelta(days=day_offset)
    if scheduled_local <= now_local:
        scheduled_local += timedelta(days=1)

    return {
        "scheduled_at": scheduled_local.astimezone(timezone.utc),
        "schedule_label": scheduled_local.strftime(f"%Y-%m-%d %H:%M {tz.key}"),
        "recurrence_rule": recurrence_rule,
    }


def _next_recurring_schedule(current: datetime, recurrence_rule: str) -> datetime | None:
    if recurrence_rule == "daily":
        return current + timedelta(days=1)
    if recurrence_rule == "weekly":
        return current + timedelta(days=7)
    return None


async def _insert_job(
    ctx: dict[str, str],
    command: str,
    prompt: str,
    *,
    status: str = "running",
    risk_level: str = "read_only",
    requires_approval: bool = False,
    metadata: dict | None = None,
    scheduled_at: datetime | None = None,
    schedule_label: str = "",
    recurrence_rule: str = "",
) -> int | None:
    if DB_POOL is None:
        return None
    async with DB_POOL.acquire() as conn:
        return await conn.fetchval(
            """
            insert into agent_jobs (
                discord_interaction_id, guild_id, channel_id, user_id,
                command, prompt, status, risk_level, requires_approval, metadata_json,
                scheduled_at, schedule_label, recurrence_rule
            )
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, $13)
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
            scheduled_at,
            schedule_label[:200],
            recurrence_rule[:80],
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


async def _set_job_status(job_id: int | None, status: str, result_summary: str = "") -> None:
    await _finish_job(job_id, status, result_summary)


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


async def _insert_approval(job_id: int | None, requested_by: str, reason: str) -> None:
    if DB_POOL is None or job_id is None:
        return
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            insert into agent_approvals (job_id, status, requested_by, reason, expires_at)
            values ($1, 'pending', $2, $3, now() + interval '24 hours')
            on conflict do nothing
            """,
            job_id,
            requested_by,
            reason[:1000],
        )


async def _decide_approval(ctx: dict[str, str], job_id: int, decision: str) -> str:
    if DB_POOL is None:
        return "Approval updates are unavailable because Postgres is not connected."

    normalized = decision.lower().strip()
    if normalized not in {"approve", "approved", "deny", "denied", "reject", "rejected"}:
        return "Use decision `approve` or `deny`."
    approved = normalized in {"approve", "approved"}
    new_approval_status = "approved" if approved else "denied"
    new_job_status = "approved" if approved else "rejected"

    async with DB_POOL.acquire() as conn:
        async with conn.transaction():
            job = await conn.fetchrow(
                """
                select id, command, status, user_id, requires_approval
                from agent_jobs
                where id = $1
                for update
                """,
                job_id,
            )
            if not job:
                return f"No job found for #{job_id}."
            if not job["requires_approval"]:
                return f"Job #{job_id} does not require approval."
            allowed = ctx["user_id"] == str(job["user_id"] or "")
            if AGENT_APPROVER_USER_IDS:
                allowed = ctx["user_id"] in AGENT_APPROVER_USER_IDS
            if not allowed:
                return "You are not allowed to decide that approval."

            approval_id = await conn.fetchval(
                """
                update agent_approvals
                set status = $2, approved_by = $3, decided_at = now()
                where job_id = $1 and status = 'pending'
                returning id
                """,
                job_id,
                new_approval_status,
                ctx["user_id"],
            )
            if approval_id is None:
                return f"Job #{job_id} has no pending approval."

            await conn.execute(
                """
                update agent_jobs
                set status = $2, result_summary = $3, updated_at = now()
                where id = $1
                """,
                job_id,
                new_job_status,
                (
                    "Approved for future Codex bridge execution. No execution was started."
                    if approved
                    else "Approval denied. No execution was started."
                ),
            )

    return (
        f"Approved job #{job_id}. No Codex/VPS execution bridge is enabled yet."
        if approved
        else f"Denied job #{job_id}. No execution was started."
    )


def _can_manage_job(ctx: dict[str, str], job_user_id: str) -> bool:
    if AGENT_APPROVER_USER_IDS:
        return ctx["user_id"] in AGENT_APPROVER_USER_IDS
    return ctx["user_id"] == job_user_id


async def _list_scheduled_jobs(ctx: dict[str, str]) -> str:
    if DB_POOL is None:
        return "Schedule management is unavailable because Postgres is not connected."
    async with DB_POOL.acquire() as conn:
        if AGENT_APPROVER_USER_IDS and ctx["user_id"] in AGENT_APPROVER_USER_IDS:
            rows = await conn.fetch(
                """
                select id, command, prompt, status, schedule_label, recurrence_rule, user_id
                from agent_jobs
                where status = 'scheduled'
                order by scheduled_at asc
                limit 10
                """
            )
        else:
            rows = await conn.fetch(
                """
                select id, command, prompt, status, schedule_label, recurrence_rule, user_id
                from agent_jobs
                where status = 'scheduled' and user_id = $1
                order by scheduled_at asc
                limit 10
                """,
                ctx["user_id"],
            )

    if not rows:
        return "No scheduled jobs found."

    lines = ["Scheduled jobs:"]
    for row in rows:
        repeat = f", repeats {row['recurrence_rule']}" if row["recurrence_rule"] else ""
        owner = f", user {row['user_id']}" if AGENT_APPROVER_USER_IDS and ctx["user_id"] in AGENT_APPROVER_USER_IDS else ""
        prompt = " ".join(str(row["prompt"] or "").split())[:120]
        lines.append(f"- #{row['id']} at {row['schedule_label']}{repeat}{owner}: {prompt}")
    return "\n".join(lines)[:1900]


async def _cancel_scheduled_job(ctx: dict[str, str], job_id: int) -> str:
    if DB_POOL is None:
        return "Schedule management is unavailable because Postgres is not connected."
    async with DB_POOL.acquire() as conn:
        async with conn.transaction():
            job = await conn.fetchrow(
                """
                select id, status, user_id, prompt
                from agent_jobs
                where id = $1
                for update
                """,
                job_id,
            )
            if not job:
                return f"No job found for #{job_id}."
            if not _can_manage_job(ctx, str(job["user_id"] or "")):
                return "You are not allowed to manage that scheduled job."
            if job["status"] != "scheduled":
                return f"Job #{job_id} is `{job['status']}`, not `scheduled`."
            await conn.execute(
                """
                update agent_jobs
                set status = 'cancelled',
                    result_summary = 'Cancelled by schedule command.',
                    updated_at = now()
                where id = $1
                """,
                job_id,
            )
    return f"Cancelled scheduled job #{job_id}."


async def _reschedule_job(ctx: dict[str, str], job_id: int, when_text: str) -> str:
    if DB_POOL is None:
        return "Schedule management is unavailable because Postgres is not connected."
    schedule = _parse_task_schedule(when_text)
    if not schedule:
        return "I could not parse that schedule. Try `10am tomorrow`, `in 30 minutes`, `daily at 10am`, or `weekly at 9am`."

    async with DB_POOL.acquire() as conn:
        async with conn.transaction():
            job = await conn.fetchrow(
                """
                select id, status, user_id
                from agent_jobs
                where id = $1
                for update
                """,
                job_id,
            )
            if not job:
                return f"No job found for #{job_id}."
            if not _can_manage_job(ctx, str(job["user_id"] or "")):
                return "You are not allowed to manage that scheduled job."
            if job["status"] not in {"scheduled", "queued"}:
                return f"Job #{job_id} is `{job['status']}` and cannot be rescheduled."
            await conn.execute(
                """
                update agent_jobs
                set status = 'scheduled',
                    scheduled_at = $2,
                    schedule_label = $3,
                    recurrence_rule = $4,
                    result_summary = $5,
                    updated_at = now()
                where id = $1
                """,
                job_id,
                schedule["scheduled_at"],
                schedule["schedule_label"],
                schedule["recurrence_rule"],
                f"Rescheduled for {schedule['schedule_label']}.",
            )
    repeat = f" Repeat: `{schedule['recurrence_rule']}`." if schedule["recurrence_rule"] else ""
    return f"Rescheduled job #{job_id} for `{schedule['schedule_label']}`.{repeat}"


async def _handle_schedule_command(ctx: dict[str, str], payload: dict) -> str:
    subcommand = _subcommand_name(payload)
    if subcommand == "list":
        return await _list_scheduled_jobs(ctx)
    if subcommand == "cancel":
        raw_job_id = _option_value(payload, "job_id")
        try:
            job_id = int(raw_job_id)
        except (TypeError, ValueError):
            return "Provide a valid numeric job_id."
        return await _cancel_scheduled_job(ctx, job_id)
    if subcommand in {"reschedule", "update"}:
        raw_job_id = _option_value(payload, "job_id")
        when_text = str(_option_value(payload, "when") or "").strip()
        try:
            job_id = int(raw_job_id)
        except (TypeError, ValueError):
            return "Provide a valid numeric job_id."
        return await _reschedule_job(ctx, job_id, when_text)
    return "Use `/schedule list`, `/schedule cancel`, or `/schedule reschedule`."


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


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


async def _send_job_followup(job: asyncpg.Record, content: str) -> None:
    metadata = _json_object(job["metadata_json"])
    discord = metadata.get("discord") if isinstance(metadata.get("discord"), dict) else {}
    app_id = str(discord.get("application_id") or "")
    token = str(discord.get("interaction_token") or "")
    if not app_id or not token:
        logger.info("job followup skipped job_id=%s missing discord token", job["id"])
        return
    await _send_followup(
        {
            "application_id": app_id,
            "token": token,
        },
        content,
    )


async def _claim_queued_jobs() -> list[asyncpg.Record]:
    if DB_POOL is None:
        return []
    async with DB_POOL.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                select *
                from agent_jobs
                where command in ('task', 'codex')
                  and (
                    status = 'queued'
                    or (status = 'scheduled' and scheduled_at <= now())
                  )
                order by coalesce(scheduled_at, created_at) asc
                limit $1
                for update skip locked
                """,
                max(1, min(AGENT_WORKER_BATCH_SIZE, 5)),
            )
            if not rows:
                return []
            ids = [row["id"] for row in rows]
            await conn.execute(
                """
                update agent_jobs
                set status = 'running', last_run_at = now(), updated_at = now()
                where id = any($1::bigint[])
                """,
                ids,
            )
            return rows


def _task_tool_for_prompt(prompt: str) -> str:
    normalized = prompt.lower()
    if re.search(r"\b(codex|ssh|terminal|shell|sudo|docker exec|systemctl|modify files?|edit files?)\b", normalized):
        return "blocked_codex_or_ops"
    if any(term in normalized for term in ("news flash", "newsflash", "summarize news", "latest news", "hacker news")):
        return "news_flash"
    if any(term in normalized for term in ("health", "status", "is anything down", "check stack", "service check")):
        return "health_status"
    if any(term in normalized for term in ("research", "search web", "look up", "find current", "web search")):
        return "web_research"
    if normalized.startswith(("remember ", "note ", "save memory ", "store memory ")):
        return "memory_note"
    if any(term in normalized for term in ("memory search", "search memory", "find memory")):
        return "memory_search"
    return "unsupported"


def _job_ctx(job: asyncpg.Record) -> dict[str, str]:
    return {
        "interaction_id": str(job["discord_interaction_id"] or ""),
        "application_id": "",
        "token": "",
        "guild_id": str(job["guild_id"] or ""),
        "channel_id": str(job["channel_id"] or ""),
        "user_id": str(job["user_id"] or ""),
        "username": "",
    }


async def _execute_task_job(job: asyncpg.Record) -> str:
    job_id = int(job["id"])
    prompt = str(job["prompt"] or "")
    tool = _task_tool_for_prompt(prompt)
    ctx = _job_ctx(job)

    if tool == "health_status":
        content = await _status_report()
        await _record_tool_call(job_id, "health_status", "completed", input_json={"prompt_chars": len(prompt)}, output_json={"content_chars": len(content)})
        return content

    if tool == "web_research":
        query = re.sub(r"^(please\s+)?(research|search web|look up|find current)\s+", "", prompt, flags=re.I).strip() or prompt
        content, metadata = await _run_web_research(job_id, query)
        await _update_job_metadata(job_id, {"research": metadata, "worker_tool": tool})
        await _save_chat_memory(ctx, "user", prompt, {"command": "task", "tool": tool})
        await _save_chat_memory(ctx, "assistant", content, {"command": "task", "tool": tool, "job_id": job_id})
        return content

    if tool == "news_flash":
        content, metadata = await _run_news_flash(job_id)
        await _update_job_metadata(job_id, {"news_flash": metadata, "worker_tool": tool})
        await _save_chat_memory(ctx, "user", prompt, {"command": "task", "tool": tool})
        await _save_chat_memory(ctx, "assistant", content, {"command": "task", "tool": tool, "job_id": job_id})
        return content

    if tool == "memory_note":
        content = re.sub(r"^(remember|note|save memory|store memory)\s+", "", prompt, flags=re.I).strip()
        if len(content) < 3:
            raise ValueError("memory note was too short")
        await _record_memory_event(ctx, "note", content, {"command": "task", "tool": tool, "job_id": job_id})
        await _save_chat_memory(ctx, "user", content, {"command": "task", "tool": tool, "job_id": job_id})
        await _record_tool_call(job_id, "memory_note", "completed", classification="safe_write", input_json={"content_chars": len(content)}, output_json={"saved": True})
        return f"Saved memory note for your user history: {content[:300]}"

    if tool == "memory_search":
        query = re.sub(r"^(please\s+)?(memory search|search memory|find memory)\s+", "", prompt, flags=re.I).strip() or prompt
        content = await _search_memory(ctx, query)
        await _record_tool_call(job_id, "memory_search", "completed", input_json={"query_chars": len(query)}, output_json={"content_chars": len(content)})
        return content

    if tool == "blocked_codex_or_ops":
        await _record_tool_call(
            job_id,
            "authority_policy",
            "completed",
            classification="approval_required",
            input_json={"prompt_chars": len(prompt)},
            output_json={"policy": CODEX_ROUTE_POLICY},
        )
        return (
            "That request looks like Codex/VPS execution work. "
            "For safety, I will not route it from `/task` or Ollama. Use `/codex` explicitly."
        )

    await _record_tool_call(
        job_id,
        "task_router",
        "completed",
        classification="read_only",
        input_json={"prompt_chars": len(prompt)},
        output_json={"matched_tool": "unsupported"},
    )
    return (
        "I queued that task, but no safe executable tool matched it yet. "
        "Current Phase 2 tools can run health checks, web research, News Flash, memory search, and memory notes."
    )


async def _execute_codex_job(job: asyncpg.Record) -> str:
    job_id = int(job["id"])
    await _insert_approval(job_id, str(job["user_id"] or ""), "Codex/VPS execution requires approval before Phase 4 bridge work.")
    await _record_tool_call(
        job_id,
        "approval_gate",
        "completed",
        classification="approval_required",
        input_json={"command": "codex"},
        output_json={"approval_status": "pending"},
    )
    await _set_job_status(job_id, "pending_approval", "Codex bridge job is waiting for an approval flow.")
    return (
        f"Codex job #{job_id} is waiting for approval.\n"
        "The Phase 2 worker created the approval record, but the VPS Codex execution bridge is not enabled yet."
    )


async def _reschedule_recurring_job(job: asyncpg.Record) -> bool:
    if DB_POOL is None:
        return False
    recurrence_rule = str(job["recurrence_rule"] or "").strip().lower()
    scheduled_at = job["scheduled_at"]
    if not recurrence_rule or scheduled_at is None:
        return False
    next_scheduled_at = _next_recurring_schedule(scheduled_at, recurrence_rule)
    if next_scheduled_at is None:
        return False
    tz = _schedule_timezone()
    schedule_label = next_scheduled_at.astimezone(tz).strftime(f"%Y-%m-%d %H:%M {tz.key}")
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            update agent_jobs
            set status = 'scheduled',
                scheduled_at = $2,
                schedule_label = $3,
                result_summary = $4,
                updated_at = now()
            where id = $1
            """,
            int(job["id"]),
            next_scheduled_at,
            schedule_label,
            f"Recurring job rescheduled for {schedule_label}.",
        )
    return True


async def _execute_queued_job(job: asyncpg.Record) -> None:
    job_id = int(job["id"])
    command = str(job["command"] or "")
    try:
        if command == "task":
            content = await _execute_task_job(job)
            await _set_job_status(job_id, "completed", content)
        elif command == "codex":
            content = await _execute_codex_job(job)
        else:
            content = f"Unsupported worker command: {command}"
            await _set_job_status(job_id, "unsupported", content)
        await _send_job_followup(job, f"Job #{job_id} complete:\n{content}")
        if command == "task" and await _reschedule_recurring_job(job):
            await _send_job_followup(job, f"Job #{job_id} has been rescheduled for the next `{job['recurrence_rule']}` run.")
    except Exception as exc:
        logger.exception("queued job failed job_id=%s command=%s", job_id, command)
        await _record_tool_call(job_id, "agent_worker", "failed", error_detail=str(exc))
        await _set_job_status(job_id, "failed", f"{type(exc).__name__}: {exc}")
        await _send_job_followup(job, f"Job #{job_id} failed: {type(exc).__name__}")


async def _agent_worker_loop() -> None:
    while True:
        try:
            jobs = await _claim_queued_jobs()
            for job in jobs:
                await _execute_queued_job(job)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("agent worker loop error")
        await asyncio.sleep(max(1.0, AGENT_WORKER_POLL_SEC))


async def _handle_agent_command(payload: dict, raw_body: bytes) -> str:
    ctx = _discord_context(payload)
    command = _command_name(payload)
    prompt = _option_text(payload)
    risk_level = "read_only"
    requires_approval = False
    initial_status = "running"
    job_metadata: dict[str, Any] = {"phase": "discord-agent-mvp"}
    schedule: dict[str, Any] | None = None
    if command == "task":
        risk_level = "safe_write"
        initial_status = "queued"
        schedule = _parse_task_schedule(prompt)
        if schedule:
            initial_status = "scheduled"
        job_metadata = {
            "phase": "agent-worker-v1",
            "discord": {
                "application_id": ctx["application_id"],
                "interaction_token": ctx["token"],
            },
        }
        if schedule:
            job_metadata["schedule"] = {
                "label": schedule["schedule_label"],
                "recurrence_rule": schedule["recurrence_rule"],
            }
    elif command == "codex":
        risk_level = "approval_required"
        requires_approval = True
        initial_status = "queued"
        job_metadata = {
            "phase": "agent-worker-v1",
            "discord": {
                "application_id": ctx["application_id"],
                "interaction_token": ctx["token"],
            },
        }

    job_id = await _insert_job(
        ctx,
        command or "legacy",
        prompt,
        status=initial_status,
        risk_level=risk_level,
        requires_approval=requires_approval,
        metadata=job_metadata,
        scheduled_at=schedule["scheduled_at"] if schedule else None,
        schedule_label=schedule["schedule_label"] if schedule else "",
        recurrence_rule=schedule["recurrence_rule"] if schedule else "",
    )

    try:
        if command == "status":
            content = await _status_report()
        elif command == "newsflash":
            content, news_metrics = await _run_news_flash(job_id)
            await _update_job_metadata(job_id, {"news_flash": news_metrics})
            await _save_chat_memory(ctx, "user", "News Flash", {"command": "newsflash"})
            await _save_chat_memory(
                ctx,
                "assistant",
                content,
                {
                    "command": "newsflash",
                    "job_id": job_id,
                    "items_found": news_metrics.get("items_found", 0),
                },
            )
        elif command == "approve":
            raw_job_id = _option_value(payload, "job_id")
            decision = str(_option_value(payload, "decision") or "").strip()
            try:
                target_job_id = int(raw_job_id)
            except (TypeError, ValueError):
                content = "Provide a valid numeric job_id."
            else:
                content = await _decide_approval(ctx, target_job_id, decision)
        elif command == "schedule":
            content = await _handle_schedule_command(ctx, payload)
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
            await _audit_interaction(ctx, "queued", prompt, model=f"router:{command}")
            if command == "task" and schedule:
                repeat = f"\nRepeat: `{schedule['recurrence_rule']}`." if schedule["recurrence_rule"] else ""
                return (
                    f"Scheduled `task` job"
                    f"{f' #{job_id}' if job_id else ''} for `{schedule['schedule_label']}`.{repeat}\n"
                    f"Risk class: `{risk_level}`.\n"
                    "The Phase 2 worker will process it when due and post a completion update."
                )
            return (
                f"Queued `{command}` job"
                f"{f' #{job_id}' if job_id else ''}.\n"
                f"Risk class: `{risk_level}`.\n"
                "The Phase 2 worker will process allowed tools and post a completion update."
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
        "ollama_chat_model": OLLAMA_CHAT_MODEL,
        "agent_worker_enabled": AGENT_WORKER_ENABLED,
        "agent_worker_running": WORKER_TASK is not None and not WORKER_TASK.done(),
        "agent_worker_poll_sec": AGENT_WORKER_POLL_SEC,
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
