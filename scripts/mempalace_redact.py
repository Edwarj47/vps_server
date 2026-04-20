#!/usr/bin/env python3
import re
from pathlib import Path


SECRET_PATTERNS = [
    re.compile(r"(?i)\b(N8N_[A-Z0-9_]*SECRET|N8N_API_KEY|DISCORD_BOT_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY|API[_-]?KEY|TOKEN|PASSWORD)\s*=\s*[^\s]+"),
    re.compile(r"(?i)\b(N8N_[A-Z0-9_]*SECRET|N8N_API_KEY|DISCORD_BOT_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY)\b"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{16,})\b"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{16,})\b"),
    re.compile(r"\b([A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,})\b"),
]

IDENTIFIER_PATTERNS = [
    re.compile(r"\bdiscord-\d{16,22}-\d{16,22}-\d{16,22}\b"),
    re.compile(r"\bdiscord:(?:dm:)?\d{16,22}(?::\d{16,22}){0,2}\b"),
    re.compile(r"\b\d{17,22}\b"),
]

HOSTILE_TEXT_PATTERNS = [
    re.compile(r"(?i)\bignore\s+(?:all\s+)?(?:(?:previous|prior)\s+)?instructions\b"),
    re.compile(
        r"(?i)\b(reveal|print|dump|show|exfiltrate|list)\b[^.;\n]{0,120}\b(system\s+prompts?|api\s*keys?|tokens?|secrets?|passwords?|env(?:ironment)?\s*(?:vars?|variables?))\b"
    ),
]


def redact_text(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    for pattern in IDENTIFIER_PATTERNS:
        redacted = pattern.sub("[REDACTED_ID]", redacted)
    for pattern in HOSTILE_TEXT_PATTERNS:
        redacted = pattern.sub("[REDACTED_UNTRUSTED_INSTRUCTION]", redacted)
    return redacted


def should_copy(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in {".txt", ".md", ".json", ".yaml", ".yml", ".log"}
