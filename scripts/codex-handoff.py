#!/usr/bin/env python3
"""Inspect and manage approved Codex handoff files.

This script does not start Codex. It is a manual pickup aid for the queue that
Discord approvals create.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


QUEUE_ROOT = Path("/opt/dcss-n8n/codex-jobs")


def _job_path(job_id: int, status: str = "approved") -> Path:
    return QUEUE_ROOT / status / f"codex-job-{job_id:06d}.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _list(status: str) -> int:
    folder = QUEUE_ROOT / status
    paths = sorted(folder.glob("codex-job-*.json")) if folder.exists() else []
    if not paths:
        print(f"No {status} Codex handoffs.")
        return 0
    for path in paths:
        data = _load(path)
        prompt = " ".join(str(data.get("prompt") or "").split())
        print(
            f"#{data.get('job_id')} {data.get('session_mode')} "
            f"workdir={data.get('requested_workdir') or 'manual'} "
            f"requested_by={data.get('requested_by')} prompt={prompt[:120]}"
        )
    return 0


def _show(job_id: int) -> int:
    for status in ("approved", "completed", "rejected"):
        path = _job_path(job_id, status)
        if path.exists():
            print(path)
            print(json.dumps(_load(path), indent=2))
            return 0
    raise SystemExit(f"No handoff file found for job #{job_id}.")


def _move(job_id: int, target_status: str) -> int:
    src = _job_path(job_id, "approved")
    if not src.exists():
        raise SystemExit(f"No approved handoff file found for job #{job_id}.")
    dst_dir = QUEUE_ROOT / target_status
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.move(str(src), str(dst))
    print(f"Moved job #{job_id} to {target_status}: {dst}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect approved Codex handoff files.")
    sub = parser.add_subparsers(dest="command", required=True)

    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--status", choices=["approved", "completed", "rejected"], default="approved")

    show_cmd = sub.add_parser("show")
    show_cmd.add_argument("job_id", type=int)

    done_cmd = sub.add_parser("complete")
    done_cmd.add_argument("job_id", type=int)

    reject_cmd = sub.add_parser("reject")
    reject_cmd.add_argument("job_id", type=int)

    args = parser.parse_args()
    if args.command == "list":
        return _list(args.status)
    if args.command == "show":
        return _show(args.job_id)
    if args.command == "complete":
        return _move(args.job_id, "completed")
    if args.command == "reject":
        return _move(args.job_id, "rejected")
    raise SystemExit(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
