#!/usr/bin/env python3
"""Export Discord memory from Postgres, redact it, and mine it into MemPalace.

This is an admin import tool, not an agent-facing write path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from mempalace_redact import redact_text


STACK_DIR = Path("/opt/dcss-n8n")
DEFAULT_PALACE = STACK_DIR / "labs/mempalace/palace-readonly-trial"
DEFAULT_IMPORT_ROOT = STACK_DIR / "labs/mempalace/imports"
MEMPALACE_BIN = STACK_DIR / "labs/mempalace/.venv/bin/mempalace"
POSTGRES_CONTAINER = os.environ.get("POSTGRES_CONTAINER", "dcss-postgres")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "n8n_dcss")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "n8n")


def _slug(value: str, fallback: str = "unknown") -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip())[:80].strip("-")
    return value or fallback


def _run(cmd: list[str], *, input_text: str | None = None, capture: bool = False) -> str:
    result = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        check=True,
        capture_output=capture,
    )
    return result.stdout if capture else ""


def _export_rows(limit: int, user_id: str | None) -> list[dict]:
    safe_limit = max(1, min(int(limit), 2000))
    where = "where content is not null and length(trim(content)) > 0"
    if user_id:
        escaped_user_id = user_id.replace("'", "''")
        where += f" and user_id = '{escaped_user_id}'"
    else:
        where += """
 and coalesce(user_id, '') !~* '(test|manual|local|injection|route|smoke)'
 and coalesce(session_id, '') !~* '(test|manual|local|injection|route|smoke)'
 and coalesce(metadata_json->>'interaction_id', '') !~* '(test|manual|local|route|injection|smoke)'
"""
    sql = f"""
select jsonb_build_object(
  'id', id,
  'session_id', session_id,
  'role', role,
  'content', content,
  'guild_id', guild_id,
  'channel_id', channel_id,
  'user_id', user_id,
  'metadata_json', metadata_json,
  'created_at', created_at
)::text
from (
  select *
  from discord_chat_memory
  {where}
  order by created_at desc
  limit {safe_limit}
) recent
order by created_at asc;
"""
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
    ]
    cmd.extend(["-c", sql])
    output = _run(cmd, capture=True)
    rows = []
    for line in output.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _write_source_files(rows: list[dict], source_dir: Path) -> None:
    by_session: dict[str, list[dict]] = {}
    for row in rows:
        by_session.setdefault(str(row.get("session_id") or "unknown"), []).append(row)

    for idx, (_session_id, items) in enumerate(sorted(by_session.items()), start=1):
        safe_session = f"session-{idx:04d}"
        path = source_dir / "discord_chat_memory" / f"{safe_session}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# Discord Memory Session {safe_session}",
            "",
            "Imported from `discord_chat_memory` through the sanitized MemPalace trial pipeline.",
            "",
        ]
        for row in items:
            created_at = str(row.get("created_at") or "")
            role = _slug(str(row.get("role") or "unknown"))
            content = " ".join(str(row.get("content") or "").split())
            metadata = row.get("metadata_json") or {}
            command = metadata.get("command") if isinstance(metadata, dict) else None
            command_text = f" command={command}" if command else ""
            lines.extend(
                [
                    f"## {created_at} {role}{command_text}",
                    "",
                    content,
                    "",
                ]
            )
        path.write_text(redact_text("\n".join(lines)), encoding="utf-8")


def _mine(source_dir: Path, palace: Path, wing: str, dry_run: bool) -> None:
    if dry_run:
        print(f"DRY_RUN source={source_dir}")
        print(f"DRY_RUN palace={palace}")
        print(f"DRY_RUN wing={wing}")
        return
    palace.mkdir(parents=True, exist_ok=True)
    _run([str(MEMPALACE_BIN), "--palace", str(palace), "init", str(source_dir), "--yes"])
    # Entity detection is useful for humans, but the generated file can turn
    # prompt-injection terms into searchable "projects." Keep the trial import
    # limited to sanitized conversation files.
    generated_entities = source_dir / "entities.json"
    if generated_entities.exists():
        generated_entities.unlink()
    _run(
        [
            str(MEMPALACE_BIN),
            "--palace",
            str(palace),
            "mine",
            str(source_dir),
            "--wing",
            wing,
            "--agent",
            "dcss-import",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Import sanitized Discord memory into the read-only MemPalace trial palace.")
    parser.add_argument("--limit", type=int, default=250)
    parser.add_argument("--user-id")
    parser.add_argument("--palace", default=str(DEFAULT_PALACE))
    parser.add_argument("--import-root", default=str(DEFAULT_IMPORT_ROOT))
    parser.add_argument("--wing", default="discord_memory")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not MEMPALACE_BIN.exists():
        raise SystemExit(f"missing MemPalace binary: {MEMPALACE_BIN}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    import_dir = Path(args.import_root).resolve() / f"discord-memory-{timestamp}"
    source_dir = import_dir / "sanitized"
    rows = _export_rows(args.limit, args.user_id)
    _write_source_files(rows, source_dir)
    _mine(source_dir, Path(args.palace).resolve(), args.wing, args.dry_run)

    manifest = {
        "created_at": timestamp,
        "rows_exported": len(rows),
        "source_dir": str(source_dir),
        "palace": str(Path(args.palace).resolve()),
        "wing": args.wing,
        "dry_run": args.dry_run,
    }
    import_dir.mkdir(parents=True, exist_ok=True)
    (import_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
