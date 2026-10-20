"""Basic secret redaction for anything we persist or return.

The rule is deliberately blunt and conservative: redact anything that *looks*
like a credential before it reaches the SQLite store, the API response, or a log
line.  We never attempt to be complete -- we attempt to never leak the obvious.

Redaction happens *before* saving, not at read time, so a secret is never on
disk in the first place.
"""

from __future__ import annotations

import re
import threading
from typing import Callable, Iterable, TypeAlias

__all__ = ["redact_text", "redact_mapping", "REDACTED"]

REDACTED = "[REDACTED]"
_KNOWN_SECRETS: set[str] = set()
_SECRET_LOCK = threading.RLock()


def register_secrets(values):
    with _SECRET_LOCK:
        _KNOWN_SECRETS.update(value for value in values if isinstance(value, str) and value)

# Patterns for well-known credential shapes.  Order matters: the more specific
# `KEY=value` forms run before the generic bearer/auth header forms.
_Replace: TypeAlias = "str | Callable[[re.Match[str]], str]"
_PATTERNS: tuple[tuple[re.Pattern[str], _Replace], ...] = (
    # OPENAI_API_KEY=sk-..., ANTHROPIC_API_KEY=sk-ant-..., GITHUB_TOKEN=ghp_...
    (
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD|CREDENTIAL)[A-Z0-9_]*)\s*[:=]\s*(?P<val>\"[^\"\n]{3,}\"|'[^'\n]{3,}'|[^\s,;)\]}]+)"
        ),
        r"\1=" + REDACTED,
    ),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"), r"\1 " + REDACTED),
    # JSON-ish credential fields: {"api_key": "..."}
    (
        re.compile(
            r"(?i)\"(?P<k>[A-Za-z0-9_]*(?:api[_-]?key|secret|token|password|credential)[A-Za-z0-9_]*)\"\s*:\s*\"[^\"\n]{3,}\""
        ),
        lambda m: '"' + m.group("k") + '":"' + REDACTED + '"',
    ),
    # Vendor key prefixes that are recognisable on their own.
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), REDACTED),
)

# Environment variables whose *values* must never be echoed, even if a model
# prints them without the NAME= prefix.
_SENSITIVE_ENV_HINTS: tuple[str, ...] = (
    "API_KEY",
    "APIKEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
)


def _env_value_patterns() -> Iterable[tuple[re.Pattern[str], str]]:
    import os

    with _SECRET_LOCK:
        known = tuple(_KNOWN_SECRETS)
    for value in known:
        yield re.compile(re.escape(value)), REDACTED

    for name, value in os.environ.items():
        upper = name.upper()
        if not any(hint in upper for hint in _SENSITIVE_ENV_HINTS):
            continue
        if not value or len(value) < 8:
            # Too short to be a real credential; redacting it would just mangle
            # unrelated output without protecting anything.
            continue
        yield re.compile(re.escape(value)), REDACTED


def redact_text(text: str | None, *, extra_values: Iterable[str] = ()) -> str:
    """Return ``text`` with credential-looking substrings replaced."""
    if not text:
        return ""
    out = text
    for value in (*extra_values,):
        if value:
            out = out.replace(value, REDACTED)
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    for pattern, replacement in _env_value_patterns():
        out = pattern.sub(replacement, out)
    return out


def redact_mapping(data: dict[str, object] | None) -> dict[str, object]:
    """Redact every string value in a shallow mapping (recurses one level)."""
    if not data:
        return {}
    out: dict[str, object] = {}
    for key, value in data.items():
        upper = key.upper()
        if any(hint in upper for hint in _SENSITIVE_ENV_HINTS) and isinstance(value, str):
            out[key] = REDACTED
        elif isinstance(value, str):
            out[key] = redact_text(value)
        elif isinstance(value, dict):
            out[key] = redact_mapping(value)
        else:
            out[key] = value
    return out
