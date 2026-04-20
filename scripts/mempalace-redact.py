#!/usr/bin/env python3
import argparse
from pathlib import Path

from mempalace_redact import redact_text, should_copy


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a redacted copy for MemPalace ingestion.")
    parser.add_argument("source")
    parser.add_argument("dest")
    args = parser.parse_args()

    source = Path(args.source).resolve()
    dest = Path(args.dest).resolve()
    if not source.is_dir():
        raise SystemExit(f"source is not a directory: {source}")
    dest.mkdir(parents=True, exist_ok=True)

    copied = 0
    redactions = 0
    for path in source.rglob("*"):
        if not should_copy(path):
            continue
        rel = path.relative_to(source)
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8", errors="replace")
        clean = redact_text(text)
        redactions += text != clean
        out.write_text(clean, encoding="utf-8")
        copied += 1

    print(f"copied_files={copied}")
    print(f"files_with_redactions={redactions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
