"""Redaction applied before anything is stored, rendered or sent (SECURITY.md §8)."""
from __future__ import annotations

import re
from typing import Any

SECRET_HEADER_NAMES = {
    "authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key",
    "x-worker-token", "x-runner-token", "x-hub-signature", "x-hub-signature-256",
}
SECRET_JSON_KEYS = {
    "password", "passwd", "secret", "token", "access_token", "refresh_token", "api_key",
    "apikey", "client_secret", "private_key", "authorization", "session", "ssn",
    "card_number", "cvv", "webhook_url", "discord_webhook_url", "github_token", "prompt_llm_api_key", "openrouter_api_key",
    "groq_api_key",
}
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("xai_key", re.compile(r"xai-[A-Za-z0-9]{20,}")),
    ("openrouter_key", re.compile(r"sk-or-v1-[A-Za-z0-9]{32,}")),
    ("groq_key", re.compile(r"gsk_[A-Za-z0-9]{20,}")),
    ("discord_webhook", re.compile(r"https?://(?:[a-z]+\.)?discord(?:app)?\.com/api/webhooks/\S+", re.I)),
    ("jwt", re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+(?!\$TOKEN\b)[A-Za-z0-9._~+/=-]{8,}")),
    ("basic_auth_url", re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
]
MAX_TEXT = 200_000


def redact_text(text: str, known_secrets: dict[str, str] | None = None) -> str:
    if not isinstance(text, str):
        return text
    out = text
    for name, value in (known_secrets or {}).items():
        if value and len(value) >= 6:
            out = out.replace(value, f"[REDACTED:{name}]")
    for name, pattern in PATTERNS:
        if name == "basic_auth_url":
            out = pattern.sub(r"\1[REDACTED:userinfo]@", out)
        elif name == "bearer":
            out = pattern.sub("Bearer [REDACTED:bearer]", out)
        else:
            out = pattern.sub(f"[REDACTED:{name}]", out)
    if len(out) > MAX_TEXT:
        out = out[:MAX_TEXT] + f"…[TRUNCATED {len(out) - MAX_TEXT} chars]"
    return out


def redact_value(value: Any, known_secrets: dict[str, str] | None = None) -> Any:
    """Recursively redact JSON-like data: secret-looking keys and secret-looking strings."""
    try:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                lowered = str(key).lower()
                if lowered in SECRET_JSON_KEYS or lowered in SECRET_HEADER_NAMES:
                    result[key] = "[REDACTED]"
                else:
                    result[key] = redact_value(item, known_secrets)
            return result
        if isinstance(value, list):
            return [redact_value(item, known_secrets) for item in value]
        if isinstance(value, str):
            return redact_text(value, known_secrets)
        return value
    except Exception:  # fail closed
        return "[REDACTION_FAILED]"
