"""Recognizing text that looks like a credential.

Used wherever Forge would otherwise persist or display text it did not
write itself (memory, logs of tool arguments). It is a heuristic: it catches
common key formats and `name = value` assignments of secret-looking names,
not every possible secret.
"""

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "secret assignment",
        re.compile(
            r"\b[\w.-]*(?:api[_-]?key|secret|password|passwd|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*['\"]?[^\s'\"]{6,}",
            re.IGNORECASE,
        ),
    ),
    ("credentials in URL", re.compile(r"://[^/\s:@]+:[^/\s@]+@")),
]

REDACTED = "[REDACTED]"


def find_secrets(text: str) -> list[str]:
    """The kinds of secret found in `text` (empty if none)."""
    return [name for name, pattern in _PATTERNS if pattern.search(text)]


def redact_secrets(text: str) -> str:
    for _, pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text
