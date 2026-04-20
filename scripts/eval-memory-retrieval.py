#!/usr/bin/env python3
"""Compare current Postgres memory lookup with read-only MemPalace retrieval."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


STACK_DIR = Path("/opt/dcss-n8n")
READONLY = STACK_DIR / "scripts/mempalace-readonly.py"
MEMPALACE_PYTHON = STACK_DIR / "labs/mempalace/.venv/bin/python"
DEFAULT_PALACE = STACK_DIR / "labs/mempalace/palace-readonly-trial"
REPORT_DIR = STACK_DIR / "model-evals"
POSTGRES_CONTAINER = os.environ.get("POSTGRES_CONTAINER", "dcss-postgres")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "n8n_dcss")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "n8n")


DEFAULT_QUERIES = [
    "current production model qwen3",
    "Codex jobs explicit /codex approval",
    "News Flash Hacker News OpenAI Anthropic",
    "scheduled tasks daily at 10am",
    "prompt injection treated as untrusted",
    "what is the assistant name and purpose",
]


def _run(cmd: list[str], *, capture: bool = True) -> tuple[str, float]:
    started = time.monotonic()
    result = subprocess.run(cmd, check=True, text=True, capture_output=capture)
    return result.stdout, (time.monotonic() - started) * 1000


def _postgres_search(query: str, limit: int) -> tuple[list[dict], float]:
    escaped_query = query.replace("'", "''")
    safe_limit = max(1, min(int(limit), 20))
    sql = """
select jsonb_build_object(
  'created_at', created_at,
  'role', role,
  'content', left(regexp_replace(content, E'\\s+', ' ', 'g'), 700),
  'metadata_json', metadata_json
)::text
from discord_chat_memory
where content ilike '%%{query}%%'
order by created_at desc
limit {limit};
""".format(query=escaped_query, limit=safe_limit)
    cmd = [
        "docker",
        "exec",
        "-i",
        POSTGRES_CONTAINER,
        "psql",
        "-U",
        POSTGRES_USER,
        "-d",
        POSTGRES_DB,
        "-X",
        "-q",
        "-t",
        "-A",
        "-c",
        sql,
    ]
    output, elapsed = _run(cmd)
    hits = [json.loads(line) for line in output.splitlines() if line.strip()]
    return hits, elapsed


def _mempalace_search(query: str, palace: Path, limit: int) -> tuple[dict, float]:
    cmd = [
        str(MEMPALACE_PYTHON),
        str(READONLY),
        "--palace",
        str(palace),
        "search",
        query,
        "--results",
        str(limit),
    ]
    output, elapsed = _run(cmd)
    return json.loads(output), elapsed


def _write_report(payload: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = REPORT_DIR / f"memory-retrieval-eval-{stamp}.json"
    md_path = REPORT_DIR / f"memory-retrieval-eval-{stamp}.md"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Memory Retrieval Eval",
        "",
        f"- Created: `{payload['created_at']}`",
        f"- Palace: `{payload['palace']}`",
        "",
        "| Query | Postgres Hits | Postgres ms | MemPalace Hits | MemPalace ms |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for result in payload["results"]:
        lines.append(
            "| {query} | {pg_hits} | {pg_ms:.1f} | {mp_hits} | {mp_ms:.1f} |".format(
                query=result["query"].replace("|", "\\|"),
                pg_hits=len(result["postgres"]["hits"]),
                pg_ms=result["postgres"]["elapsed_ms"],
                mp_hits=len(result["mempalace"]["hits"]),
                mp_ms=result["mempalace"]["elapsed_ms"],
            )
        )
    lines.extend(
        [
            "",
            "Interpretation:",
            "",
            "- Postgres is the current exact/substring baseline.",
            "- MemPalace is the semantic/verbatim retrieval candidate.",
            "- Use this report for direction, not as a final benchmark; manual relevance review is still needed.",
            "",
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare Postgres and MemPalace memory retrieval.")
    parser.add_argument("--palace", default=str(DEFAULT_PALACE))
    parser.add_argument("--query", action="append", dest="queries")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    palace = Path(args.palace).resolve()
    queries = args.queries or DEFAULT_QUERIES
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "palace": str(palace),
        "results": [],
    }
    for query in queries:
        pg_hits, pg_ms = _postgres_search(query, args.limit)
        mp_raw, mp_ms = _mempalace_search(query, palace, args.limit)
        mp_hits = mp_raw.get("results") or []
        payload["results"].append(
            {
                "query": query,
                "postgres": {"elapsed_ms": pg_ms, "hits": pg_hits},
                "mempalace": {"elapsed_ms": mp_ms, "hits": mp_hits, "error": mp_raw.get("error")},
            }
        )
    report = _write_report(payload)
    print(f"Wrote {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
