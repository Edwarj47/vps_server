#!/usr/bin/env python3
"""Read-only MemPalace wrapper for the DCSS agent memory trial.

This intentionally exposes only status, namespace listing, and search. Do not
add writes here; ingestion and retention belong in separate admin scripts.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_PALACE = Path("/opt/dcss-n8n/labs/mempalace/palace-readonly-trial")
ALLOWED_ROOT = Path("/opt/dcss-n8n/labs/mempalace").resolve()
MAX_QUERY_CHARS = 500
MAX_RESULTS = 8
MAX_TEXT_CHARS = 1200


def _resolve_palace(path: str | None) -> Path:
    palace = Path(path).expanduser().resolve() if path else DEFAULT_PALACE.resolve()
    if ALLOWED_ROOT not in palace.parents and palace != ALLOWED_ROOT:
        raise SystemExit(f"palace path must stay under {ALLOWED_ROOT}")
    return palace


def _ensure_imports() -> None:
    try:
        import mempalace  # noqa: F401
        import chromadb  # noqa: F401
    except Exception as exc:
        raise SystemExit(
            "MemPalace imports failed. Run this with "
            "/opt/dcss-n8n/labs/mempalace/.venv/bin/python"
        ) from exc


def _collection(palace: Path):
    from mempalace.palace import get_collection

    return get_collection(str(palace), create=False)


def _status(palace: Path) -> dict[str, Any]:
    started = time.monotonic()
    if not palace.exists():
        return {
            "ok": False,
            "mode": "read_only",
            "palace": str(palace),
            "error": "palace_not_found",
        }

    col = _collection(palace)
    count = col.count()
    namespaces = _namespaces_from_collection(col)
    return {
        "ok": True,
        "mode": "read_only",
        "palace": str(palace),
        "drawers": count,
        "namespaces": namespaces,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
    }


def _namespaces_from_collection(col) -> list[dict[str, Any]]:
    data = col.get(include=["metadatas"])
    counts: dict[tuple[str, str], int] = {}
    for meta in data.get("metadatas") or []:
        wing = str(meta.get("wing") or "unknown")
        room = str(meta.get("room") or "unknown")
        counts[(wing, room)] = counts.get((wing, room), 0) + 1

    grouped: dict[str, dict[str, Any]] = {}
    for (wing, room), count in sorted(counts.items()):
        item = grouped.setdefault(wing, {"wing": wing, "drawers": 0, "rooms": []})
        item["drawers"] += count
        item["rooms"].append({"room": room, "drawers": count})
    return list(grouped.values())


def _namespaces(palace: Path) -> dict[str, Any]:
    col = _collection(palace)
    return {
        "ok": True,
        "mode": "read_only",
        "palace": str(palace),
        "namespaces": _namespaces_from_collection(col),
    }


def _search(palace: Path, query: str, wing: str | None, room: str | None, results: int) -> dict[str, Any]:
    from mempalace.searcher import search_memories

    safe_query = " ".join(query.split())[:MAX_QUERY_CHARS]
    if len(safe_query) < 3:
        raise SystemExit("query must be at least 3 characters")
    safe_results = max(1, min(results, MAX_RESULTS))

    started = time.monotonic()
    raw = search_memories(
        query=safe_query,
        palace_path=str(palace),
        wing=wing,
        room=room,
        n_results=safe_results,
    )
    hits = []
    seen: set[tuple[str | None, str]] = set()
    for item in raw.get("results") or []:
        text = str(item.get("text") or "")[:MAX_TEXT_CHARS]
        key = (item.get("source_file"), " ".join(text.split())[:240])
        if key in seen:
            continue
        seen.add(key)
        hits.append(
            {
                "wing": item.get("wing"),
                "room": item.get("room"),
                "source_file": item.get("source_file"),
                "similarity": item.get("similarity"),
                "matched_via": item.get("matched_via"),
                "text": text,
            }
        )

    return {
        "ok": "error" not in raw,
        "mode": "read_only",
        "palace": str(palace),
        "query": safe_query,
        "filters": {"wing": wing, "room": room},
        "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
        "results": hits,
        "error": raw.get("error"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only MemPalace trial wrapper.")
    parser.add_argument("--palace", help=f"Palace path, default {DEFAULT_PALACE}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status")
    sub.add_parser("namespaces")

    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--wing")
    search.add_argument("--room")
    search.add_argument("--results", type=int, default=5)

    args = parser.parse_args()
    palace = _resolve_palace(args.palace)
    _ensure_imports()

    if args.command == "status":
        payload = _status(palace)
    elif args.command == "namespaces":
        payload = _namespaces(palace)
    elif args.command == "search":
        payload = _search(palace, args.query, args.wing, args.room, args.results)
    else:
        raise SystemExit(f"unsupported command: {args.command}")

    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
