#!/usr/bin/env python3
"""Mine curated project docs into the read-only MemPalace trial palace."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from mempalace_redact import redact_text


STACK_DIR = Path("/opt/dcss-n8n")
DEFAULT_PALACE = STACK_DIR / "labs/mempalace/palace-readonly-trial"
DEFAULT_IMPORT_ROOT = STACK_DIR / "labs/mempalace/imports"
MEMPALACE_BIN = STACK_DIR / "labs/mempalace/.venv/bin/mempalace"
DEFAULT_DOCS = [
    STACK_DIR / "AGENTS.md",
    STACK_DIR / "OPERATIONS.md",
]


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, text=True)


def _copy_docs(source_dir: Path, docs: list[Path]) -> int:
    count = 0
    source_dir.mkdir(parents=True, exist_ok=True)
    for doc in docs:
        resolved = doc.resolve()
        if not resolved.exists() or not resolved.is_file():
            continue
        out = source_dir / "project_docs" / resolved.name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(redact_text(resolved.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Import curated project docs into the read-only MemPalace trial palace.")
    parser.add_argument("--palace", default=str(DEFAULT_PALACE))
    parser.add_argument("--import-root", default=str(DEFAULT_IMPORT_ROOT))
    parser.add_argument("--wing", default="project_ops")
    parser.add_argument("--doc", action="append", dest="docs")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    docs = [Path(item) for item in args.docs] if args.docs else DEFAULT_DOCS
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    import_dir = Path(args.import_root).resolve() / f"project-docs-{timestamp}"
    source_dir = import_dir / "sanitized"
    copied = _copy_docs(source_dir, docs)

    palace = Path(args.palace).resolve()
    if not args.dry_run:
        palace.mkdir(parents=True, exist_ok=True)
        _run([str(MEMPALACE_BIN), "--palace", str(palace), "init", str(source_dir), "--yes"])
        generated_entities = source_dir / "entities.json"
        if generated_entities.exists():
            generated_entities.unlink()
        _run([str(MEMPALACE_BIN), "--palace", str(palace), "mine", str(source_dir), "--wing", args.wing, "--agent", "dcss-import"])

    manifest = {
        "created_at": timestamp,
        "docs_copied": copied,
        "source_dir": str(source_dir),
        "palace": str(palace),
        "wing": args.wing,
        "dry_run": args.dry_run,
    }
    import_dir.mkdir(parents=True, exist_ok=True)
    (import_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
