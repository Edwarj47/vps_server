#!/usr/bin/env python3
import argparse
import re
from pathlib import Path


SECRET_PATTERNS = [
    re.compile(r"(?i)\b(N8N_[A-Z0-9_]*SECRET|N8N_API_KEY|DISCORD_BOT_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY|API[_-]?KEY|TOKEN|PASSWORD)\s*=\s*[^\s]+"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{16,})\b"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{16,})\b"),
    re.compile(r"\b([A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,})\b"),
]


def redact_text(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


def should_copy(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in {".txt", ".md", ".json", ".yaml", ".yml", ".log"}


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
