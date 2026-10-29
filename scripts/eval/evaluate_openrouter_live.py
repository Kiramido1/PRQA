#!/usr/bin/env python3
"""PRQA — authorized OpenRouter live-model prompt quality evaluation (bounded, review-only). RETIRED 2026-10-27.

RETIRED: OpenRouter is not an approved provider and this script does not reserve the shared provider budget, so its
only network function (http_json) refuses before connecting. Offline modes (--check-config, --merge) still work on the
saved artifacts. The text below documents the historical behaviour.

Drives the PRODUCTION prompt-builder LLM request path
    build_llm_request(templates, model=PROMPT_LLM_MODEL, ...) -> POST {PROMPT_LLM_API_BASE}/chat/completions
directly (outside the n8n pipeline), for the 15 synthetic benchmark cases only. Nothing in the
pipeline state changes: AUTOMATION_MODE stays review_only, PROMPT_LLM_LIVE_IN_REVIEW_ONLY stays false.

Hard safety invariants (checked before any send; run refuses otherwise):
- PROMPT_LLM_API_BASE == https://openrouter.ai/api/v1 (exact configured endpoint)
- PROMPT_LLM_MODEL == the configured slug from compose/.env (never guessed)
- AUTOMATION_MODE == review_only and PROMPT_LLM_LIVE_IN_REVIEW_ONLY == false in compose/.env
- key present (compose/.env PROMPT_LLM_API_KEY or ~/.config/prqa/secrets.env, mode 600 preferred);
  the key is used ONLY for this endpoint and never written to any artifact.

Bounded: max 15 requests (1 per case), one attempt per request (no automatic retries), stop
on 429/provider throttling or auth failure, no model substitution, no fallback substitution.

Exit 0 = all attempts completed (success of the MODEL deltas is in the artifacts, not the exit code).

--max-tokens N sets the completion-token limit for every selected case (default: the
build_llm_request() default of 16000; accepted range 1..24000). Invalid values exit with status 2
from argument parsing, before configuration is read or any network helper runs.

--reasoning-max-tokens N (optional) caps hidden reasoning inside --max-tokens and must be smaller than it;
omitted, the request payload is unchanged. It is refused unless /models lists `reasoning` for the slug.

--check-config is offline: it runs the local precondition checks only (no /models call, no generation
request, no artifact writes) and exits 0 when they pass, 1 otherwise.

An HTTP 200 without usable content (missing/empty/malformed `choices`, or empty/whitespace/null/absent
`message.content`) is a failed request (EMPTY_MODEL_CONTENT / MALFORMED_MODEL_RESPONSE). Its allowlisted,
non-sensitive diagnostics (status, model, provider, request id, finish reasons, provider-returned token
counts) go to the results row and to case-<cid>.diagnostic.json; it never reaches the gate or the rubric.
A response the provider cut at the token limit (finish_reason "length" or a native equivalent) is TRUNCATED_MODEL_OUTPUT:
also a failed request, its redacted partial text is kept only in the diagnostic for inspection, and it is never gated.
Generated content is credential-redacted before it is written to case-<cid>.response.json.

Every row and artifact carries an `outcome` (usable, gate_rejected, empty, malformed, truncated, http_error,
transport_error) and every new artifact a `generation` block (model, provider, template version, source hashes,
token limit, reasoning settings, UTC timestamp, request count). --merge labels each re-scored sample current,
historical or unknown against the present sources; nothing is inferred for artifacts that predate these fields.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
from urllib.parse import urlsplit
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "tests"))

from prqa_worker.prompt_builder import (  # noqa: E402
    GENERATION_EMPTY, GENERATION_GATE_REJECTED, GENERATION_HTTP_ERROR, GENERATION_MALFORMED, GENERATION_PROVIDER_ERROR,
    GENERATION_TIMEOUT_S,
    GENERATION_TRANSPORT_ERROR, GENERATION_TRUNCATED, GENERATION_USABLE, MAX_TOKENS_CEILING, TEMPLATE_VERSION,
    GenerationConfigError, Templates, build_llm_request, is_truncated, resolve_generation_settings,
    validate_generated_prompt,
)
from prqa_worker.redaction import redact_text  # noqa: E402
from prqa_worker import qa_rubric  # noqa: E402  (the production rubric the worker enforces)
from qa_benchmark import CASES, context_for  # noqa: E402

EXPECTED_BASE = "https://openrouter.ai/api/v1"
OUT_DIR = ROOT / "runs" / "openrouter-live"
BACKUP_DIR = ROOT / "runs" / "baseline-backups"     # live runs archive what they would overwrite here first
# Files whose exact content determines a generation and its verdict; their hashes are recorded with every sample.
HASHED_SOURCES = ("prompts/llm/system.v2.md", "prompts/llm/user.v2.md", "prompts/reference/Senior_QA_Reference_Prompt.md",
                  "worker/prqa_worker/prompt_builder.py",
                  "worker/prqa_worker/validation.py", "worker/prqa_worker/qa_rubric.py", "tests/qa_benchmark.py",
                  "scripts/eval/evaluate_openrouter_live.py")
MAX_REQUESTS = 15
# --model: evaluator-only candidate override (never written to compose/.env or the worker configuration)
MODEL_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:-]{0,95}")
# The CLI default is read from build_llm_request() so the two can never drift apart.
DEFAULT_MAX_TOKENS = int(inspect.signature(build_llm_request).parameters["max_tokens"].default)
CLI_MAX_TOKENS_LIMIT = MAX_TOKENS_CEILING   # shared ceiling (24000); not inferred from the context window


def max_tokens_arg(text: str) -> int:
    """argparse type for --max-tokens: a plain positive integer no larger than CLI_MAX_TOKENS_LIMIT."""
    if not re.fullmatch(r"[0-9]+", text.strip()):
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {text!r}")
    value = int(text)
    if not 1 <= value <= CLI_MAX_TOKENS_LIMIT:
        raise argparse.ArgumentTypeError(f"must be between 1 and {CLI_MAX_TOKENS_LIMIT}, got {value}")
    return value


EMPTY_MODEL_CONTENT = "EMPTY_MODEL_CONTENT"
MALFORMED_MODEL_RESPONSE = "MALFORMED_MODEL_RESPONSE"
TRUNCATED_MODEL_OUTPUT = "TRUNCATED_MODEL_OUTPUT"
PROVIDER_ERROR_RESPONSE = "PROVIDER_ERROR_RESPONSE"   # HTTP 200 whose body carries a provider `error` object, no content
TRANSPORT_ERROR = "TRANSPORT_ERROR"
TRANSPORT_TIMEOUT = "TRANSPORT_TIMEOUT"
# failure_reason -> outcome; any other HTTP_<code> is an http_error (legacy HTTP_0 was a transport failure)
FAILURE_OUTCOMES = {EMPTY_MODEL_CONTENT: GENERATION_EMPTY, MALFORMED_MODEL_RESPONSE: GENERATION_MALFORMED,
                    TRUNCATED_MODEL_OUTPUT: GENERATION_TRUNCATED, TRANSPORT_ERROR: GENERATION_TRANSPORT_ERROR,
                    PROVIDER_ERROR_RESPONSE: GENERATION_PROVIDER_ERROR,
                    TRANSPORT_TIMEOUT: GENERATION_TRANSPORT_ERROR, "HTTP_0": GENERATION_TRANSPORT_ERROR}
GENERATION_POLICY_NOTE = (f"n8n workflow 03 generation node: single attempt (retryOnFail=false), timeout "
                          f"{GENERATION_TIMEOUT_S}s, failure -> deterministic fallback; this runner also makes one attempt "
                          "per request and never retries")
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
# Explicit allowlist: only these short identifiers are ever copied out of a provider response.
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")


def _safe_id(value: object) -> str | None:
    """A short provider identifier (model, provider, request id, finish reason) or None."""
    return value if isinstance(value, str) and _SAFE_ID_RE.fullmatch(value) else None


def failure_outcome(reason: object) -> str | None:
    """Outcome for a recorded failure_reason (None when the reason is missing or unrecognised)."""
    if not isinstance(reason, str):
        return None
    if reason in FAILURE_OUTCOMES:
        return FAILURE_OUTCOMES[reason]
    return GENERATION_HTTP_ERROR if re.fullmatch(r"HTTP_[1-9][0-9]{2}", reason) else None


def version_status(generated_with: object, current: dict[str, str]) -> tuple[str, list[str] | None]:
    """("current" | "historical" | "unknown", changed source paths). No recorded hashes is unknown, never assumed."""
    if not isinstance(generated_with, dict) or not generated_with:
        return "unknown", None
    changed = sorted(k for k in current if generated_with.get(k) != current[k])
    return ("historical" if changed else "current"), changed


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def extract_usage(body: object) -> dict[str, int]:
    """Provider-returned token counts only. Missing fields stay missing; nothing is estimated.

    `reasoning_tokens` is recorded only when the provider states it explicitly, either at
    usage.reasoning_tokens or nested at usage.completion_tokens_details.reasoning_tokens."""
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return {}
    out = {k: _count(usage.get(k)) for k in USAGE_FIELDS}
    details = usage.get("completion_tokens_details")
    nested = _count(details.get("reasoning_tokens")) if isinstance(details, dict) else None
    reasoning = nested if nested is not None else _count(usage.get("reasoning_tokens"))
    if reasoning is not None:
        out["reasoning_tokens"] = reasoning
    return {k: v for k, v in out.items() if v is not None}


# Provider error text is untrusted: it may echo the request, a header or a key. Only a short, redacted message survives.
ERROR_MESSAGE_MAX_CHARS = 200          # stored length after sanitizing
ERROR_MESSAGE_MAX_INPUT = 4000         # longer raw messages are omitted, not truncated (likely an echoed payload)
ECHO_WINDOW = 32                       # a message sharing any 32-char run with the prompt is treated as an echo
_EXTRA_SECRET_PATTERNS = [             # beyond the worker's redaction rules: generic key shapes seen in provider errors
    ("api_key", re.compile(r"\b(?:sk|gsk|rk|pk)[-_][A-Za-z0-9_-]{16,}")),
    ("key_param", re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|token|secret|password)\s*[=:]\s*\S+")),
    ("token_like", re.compile(r"[A-Za-z0-9+/_=-]{32,}")),
    # account identifiers (Groq rate-limit errors name the organization): not secrets, but never stored
    ("account_id", re.compile(r"\b(?:org|proj|project|user|acct)_[A-Za-z0-9]{8,}\b")),
]
_ECHO_MARKERS = ("<<<PR_CONTEXT>>>", "<<<REFERENCE_PROMPT>>>", "<<<SUPPORTED_OPERATIONS>>>", "<<<CLAIM_LEDGER>>>",
                 "\"messages\"", "\"role\":")


def sanitize_provider_message(message: object, key: str = "", echo_source: str = "") -> tuple[str | None, str]:
    """(sanitized message or None, status). status is one of absent, not_text, too_long, echo, redacted, kept.
    The raw text is never returned, logged or raised; a message that might echo the request is dropped entirely."""
    if message is None:
        return None, "absent"
    if not isinstance(message, str):
        return None, "not_text"
    if len(message) > ERROR_MESSAGE_MAX_INPUT:
        return None, "too_long"
    if any(m in message for m in _ECHO_MARKERS) or (echo_source and any(
            message[i:i + ECHO_WINDOW] in echo_source for i in range(0, max(len(message) - ECHO_WINDOW + 1, 0), 4))):
        return None, "echo"
    text = redact_text(message, {"prompt_llm_api_key": key} if key else None)
    for name, pattern in _EXTRA_SECRET_PATTERNS:
        text = pattern.sub(f"[REDACTED:{name}]", text)
    text = re.sub(r"\s+", " ", text).strip()
    if key and key in text:                          # belt and braces: never keep the configured key
        return None, "omitted"
    if len(text) > ERROR_MESSAGE_MAX_CHARS:
        text = text[:ERROR_MESSAGE_MAX_CHARS] + "…"
    return (text or None), ("redacted" if "[REDACTED" in text else "kept") if text else "absent"


def provider_error(err: object, *, key: str = "", echo_source: str = "") -> dict | None:
    """Allowlisted summary of a provider `error` object: code, type, param, provider_name, sanitized message.
    Returns None when `err` is not an object (no error is invented); missing fields stay missing."""
    if not isinstance(err, dict):
        return None
    out: dict = {}
    code = err.get("code")
    if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 999:
        out["code"] = code
    elif _safe_id(code) is not None and len(code) <= 64:
        out["code"] = code
    for field in ("type", "param"):
        value = _safe_id(err.get(field))
        if value is not None and len(value) <= 64:
            out[field] = value
    meta = err.get("metadata")
    if isinstance(meta, dict):
        name = _safe_id(meta.get("provider_name"))
        if name is not None and len(name) <= 64:
            out["provider_name"] = name
    message, status = sanitize_provider_message(err.get("message"), key, echo_source)
    if message is not None:
        out["message"] = message
    out["message_status"] = status
    return out


def describe_http_failure(body: object, *, key: str = "", echo_source: str = "") -> tuple[str, dict | None]:
    """Sanitized one-line description of a non-200 / transport failure plus the allowlisted provider error (or None).
    Provider text goes through sanitize_provider_message; the raw body is never stored."""
    err = body.get("error") if isinstance(body, dict) else None
    summary = provider_error(err, key=key, echo_source=echo_source)
    if summary is not None:
        label = summary.get("code", summary.get("type", "no code"))
        return f"provider error {label}: {summary.get('message') or '<message ' + summary['message_status'] + '>'}", summary
    if isinstance(err, str):
        raw: object = err
    elif isinstance(body, str):
        raw = body
    else:
        raw = "no choices in response" if not err else None
    message, status = sanitize_provider_message(raw, key, echo_source)
    return (message or f"<provider message {status}>"), None


def parse_completion(code: int, body: object, *, key: str = "", echo_source: str = "") -> dict:
    """Classify an HTTP 200 chat-completion body without ever raising.

    Returns {"content": str | None, "failure_reason": None | EMPTY_MODEL_CONTENT | MALFORMED_MODEL_RESPONSE |
    PROVIDER_ERROR_RESPONSE | TRUNCATED_MODEL_OUTPUT, "detail": str, "diagnostics": {...allowlisted metadata...}} plus "partial_content" for a
    truncated response (content stays None: a cut document is never handed to the gate). Hidden reasoning text,
    headers and any other response fields are never copied."""
    diag: dict = {"http_status": code}
    if not isinstance(body, dict):
        return {"content": None, "failure_reason": MALFORMED_MODEL_RESPONSE,
                "detail": "response body is not a JSON object", "diagnostics": diag}
    for key, field in (("model_returned", "model"), ("provider", "provider"), ("request_id", "id")):
        value = _safe_id(body.get(field))
        if value is not None:
            diag[key] = value
    usage = extract_usage(body)
    if usage:
        diag["usage"] = usage
    choices = body.get("choices")
    first_choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    first_message = first_choice.get("message") if isinstance(first_choice.get("message"), dict) else {}
    has_content = isinstance(first_message.get("content"), str) and bool(first_message["content"].strip())
    # an `error` object (top level, or on the first choice) with no usable content is a provider error, not malformed
    err_obj = body.get("error") if "error" in body else first_choice.get("error")
    if err_obj is not None and not has_content:
        summary = provider_error(err_obj, key=key, echo_source=echo_source)
        if summary is not None:
            diag["provider_error"] = summary
            for k in ("finish_reason", "native_finish_reason"):
                value = _safe_id(first_choice.get(k))
                if value is not None:
                    diag[k] = value
            label = summary.get("code", summary.get("type", "no code"))
            return {"content": None, "failure_reason": PROVIDER_ERROR_RESPONSE,
                    "detail": f"HTTP {code} body carries a provider error ({label}) and no usable content",
                    "diagnostics": diag}
        diag["provider_error"] = {"message_status": "unparsed", "shape": type(err_obj).__name__}
    if not isinstance(choices, list) or not choices:
        return {"content": None, "failure_reason": MALFORMED_MODEL_RESPONSE,
                "detail": "choices missing or empty" if not choices else "choices is not a list",
                "diagnostics": diag}
    first = choices[0]
    if not isinstance(first, dict):
        return {"content": None, "failure_reason": MALFORMED_MODEL_RESPONSE,
                "detail": "choices[0] is not an object", "diagnostics": diag}
    for key in ("finish_reason", "native_finish_reason"):
        value = _safe_id(first.get(key))
        if value is not None:
            diag[key] = value
    message = first.get("message")
    if not isinstance(message, dict):
        return {"content": None, "failure_reason": MALFORMED_MODEL_RESPONSE,
                "detail": "choices[0].message missing or not an object", "diagnostics": diag}
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        return {"content": None, "failure_reason": MALFORMED_MODEL_RESPONSE,
                "detail": "choices[0].message.content is not a string", "diagnostics": diag}
    if not content or not content.strip():
        state = "absent" if "content" not in message else "null" if content is None else (
            "empty" if content == "" else "whitespace-only")
        return {"content": None, "failure_reason": EMPTY_MODEL_CONTENT,
                "detail": f"choices[0].message.content is {state}", "diagnostics": diag}
    if is_truncated(diag.get("finish_reason"), diag.get("native_finish_reason")):
        return {"content": None, "failure_reason": TRUNCATED_MODEL_OUTPUT, "partial_content": content,
                "detail": f"output cut at the token limit (finish_reason={diag.get('finish_reason')}, "
                          f"native_finish_reason={diag.get('native_finish_reason')})", "diagnostics": diag}
    return {"content": content, "failure_reason": None, "detail": "", "diagnostics": diag}


def redact_for_artifact(content: str, key: str) -> tuple[str, dict]:
    """Deterministic credential redaction before persistence (reuses the worker's redaction rules).
    Reports which rule names fired, never the redacted values."""
    redacted = redact_text(content, {"prompt_llm_api_key": key})
    fired = sorted(set(re.findall(r"\[REDACTED:([a-z_]+)\]", redacted)) - set(re.findall(r"\[REDACTED:([a-z_]+)\]", content)))
    return redacted, {"applied": redacted != content, "rules": fired}


def write_json_atomic(path: Path, obj: object) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def run_check_config(env: dict[str, str]) -> int:
    """Offline configuration check: local preconditions only. Never touches the network, never
    writes artifacts and never prints the key or anything derived from it."""
    try:
        info = verify_preconditions(env)
    except SystemExit as e:
        print(f"[check-config] FAILED (offline, no network traffic)\n{e}")
        return 1
    print(f"[check-config] OK (offline, no network traffic) base={info['base']} slug={info['slug']} "
          f"key=present key_source={info['key_source']} AUTOMATION_MODE=review_only "
          f"PROMPT_LLM_LIVE_IN_REVIEW_ONLY=false")
    return 0


def source_hashes() -> dict[str, str]:
    """sha256 of every file that shapes the request, the gate and the rubric (no timestamps by policy)."""
    out = {}
    for rel in HASHED_SOURCES:
        f = ROOT / rel
        out[rel] = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else "missing"
    return out


def pricing_block(meta: dict | None, max_tokens: int, *, reasoning_max_tokens: int | None = None,
                  reasoning_disabled: bool = False) -> str | None:
    """Pre-generation guard on the provider's own /models metadata: the slug must be listed, prompt and completion
    prices must both be exactly zero, and max_tokens must be a supported parameter when the list is provided.
    Returns the blocking reason, or None. Anything unconfirmed blocks."""
    if not isinstance(meta, dict) or not meta.get("model_list_available"):
        return "model not confirmed in the provider's /models list"
    pricing = meta.get("pricing")
    if not isinstance(pricing, dict):
        return "pricing not reported by the provider"
    for k in ("prompt", "completion"):
        try:
            if k not in pricing or float(pricing[k]) != 0.0:
                return f"{k} price is not confirmed as zero ({pricing.get(k)!r})"
        except (TypeError, ValueError):
            return f"{k} price is not numeric ({pricing.get(k)!r})"
    params = meta.get("supported_parameters")
    if isinstance(params, list) and "max_tokens" not in params:
        return "max_tokens is not a supported parameter for this model"
    if max_tokens < 1:
        return "invalid max_tokens"
    if reasoning_disabled and (not isinstance(params, list) or "reasoning" not in params):
        return "reasoning-disabled was requested but reasoning is not a listed supported parameter"
    if reasoning_max_tokens is not None:
        if not isinstance(params, list) or "reasoning" not in params:
            return "a reasoning cap was requested but reasoning is not a listed supported parameter"
        if not 1 <= reasoning_max_tokens < max_tokens:
            return "invalid reasoning cap"
    return None


def archive_before_attempt(cids: list[str], *, label: str = "live", remove_stale: bool = True,
                           out_dir: Path | None = None, backup_dir: Path | None = None) -> Path | None:
    """Copy every artifact a live attempt may overwrite (run_meta, results, comparison, and the selected cases'
    response/diagnostic files) to a fresh BACKUP_DIR/auto-live-<UTC> folder with a SHA256SUMS manifest, verify the
    copies, then remove the selected cases' stale response/diagnostic so a failed attempt can never leave an older
    successful response behind. Returns the archive folder (None when there was nothing to archive).
    out_dir/backup_dir default to this evaluator's folders (the Groq evaluator passes its own)."""
    OUT_DIR, BACKUP_DIR = out_dir or globals()["OUT_DIR"], backup_dir or globals()["BACKUP_DIR"]   # noqa: N806
    names = ["run_meta.json", "results.json", "comparison.json"]
    for cid in cids:
        names += [f"case-{cid}.response.json", f"case-{cid}.diagnostic.json"]
    present = [OUT_DIR / n for n in names if (OUT_DIR / n).is_file()]
    if not present:
        return None
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    dest = BACKUP_DIR / f"auto-{label}-{stamp}"
    n = 1
    while dest.exists():
        n += 1
        dest = BACKUP_DIR / f"auto-{label}-{stamp}-{n}"
    dest.mkdir(parents=True)
    lines = []
    for f in present:
        data = f.read_bytes()
        (dest / f.name).write_bytes(data)
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {f.name}")
    (dest / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines:                                   # verify before anything is removed
        digest, name = line.split("  ", 1)
        if hashlib.sha256((dest / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"BLOCKED — archive verification failed for {name}; nothing removed")
    if remove_stale:
        for cid in cids:
            for suffix in ("response", "diagnostic"):
                (OUT_DIR / f"case-{cid}.{suffix}.json").unlink(missing_ok=True)
    return dest


def artifact_cid(p: Path) -> str:
    return p.name.removeprefix("case-").removesuffix(".response.json")


def load_env_records() -> dict[str, str]:
    """compose/.env plus optional ~/.config/prqa/secrets.env (secrets.env wins on overlap)."""
    env: dict[str, str] = {}
    p = ROOT / "compose" / ".env"
    if p.is_file():
        env.update(_parse_env(p.read_text(encoding="utf-8").splitlines()))
    s = Path.home() / ".config" / "prqa" / "secrets.env"
    if s.is_file():
        env.update(_parse_env(s.read_text(encoding="utf-8").splitlines()))
    return env


def _parse_env(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def key_fingerprint(key: str) -> dict:
    return {
        "length": len(key),
        "sha256_12": hashlib.sha256(key.encode()).hexdigest()[:12],
        "placeholder": bool(re.fullmatch(r"(?i)(<.*>|generate.*|your.*|xxx.*)", key)),
    }


def verify_preconditions(env: dict[str, str]) -> dict[str, str]:
    problems = []
    mode = env.get("AUTOMATION_MODE", "").strip().lower()
    live_flag = env.get("PROMPT_LLM_LIVE_IN_REVIEW_ONLY", "").strip().lower()
    if mode != "review_only":
        problems.append(f"AUTOMATION_MODE={mode!r} (must be review_only)")
    if live_flag not in ("false", ""):
        problems.append(f"PROMPT_LLM_LIVE_IN_REVIEW_ONLY={live_flag!r} (must be false)")
    base = env.get("PROMPT_LLM_API_BASE", "").strip()
    slug = env.get("PROMPT_LLM_MODEL", "").strip()
    key = env.get("PROMPT_LLM_API_KEY", "") or env.get("OPENROUTER_API_KEY", "")
    if base != EXPECTED_BASE:
        problems.append(f"PROMPT_LLM_API_BASE={base!r} (must be exactly {EXPECTED_BASE})")
    if not slug or ":" not in slug:
        problems.append(f"PROMPT_LLM_MODEL={slug!r} (no configured slug)")
    if key.startswith("gsk_"):
        # a Groq key is never sent to OpenRouter (the value is never echoed)
        problems.append("the prompt-LLM key has the Groq key format and is never sent to OpenRouter; set the OpenRouter "
                        "key as PROMPT_LLM_API_KEY in ~/.config/prqa/secrets.env (chmod 600)")
    elif not key or key_fingerprint(key)["placeholder"]:
        problems.append("prompt-LLM API key missing/placeholder in compose/.env or ~/.config/prqa/secrets.env")
    if problems:
        raise SystemExit("BLOCKED — safety preconditions failed:\n  - " + "\n  - ".join(problems))
    source = "PROMPT_LLM_API_KEY" if env.get("PROMPT_LLM_API_KEY") else "OPENROUTER_API_KEY"   # variable, not file
    return {"base": base, "slug": slug, "key": key, "key_source": source}


class RetiredProviderPath(SystemExit):
    """Raised instead of any network request: OpenRouter is not an approved provider (2026-10-27)."""


def http_json(url: str, *, method: str = "GET", payload: dict | None = None,
              headers: list[tuple[str, str]] | None = None, timeout: int = 120) -> tuple[int, dict | str]:
    # RETIRED (2026-10-27): direct Groq is the only approved provider and every model request must reserve the shared
    # provider-budget ledger (proxy/provider_budget.py). This evaluator has no ledger and targets OpenRouter, so its
    # only network function refuses before any connection. --check-config and --merge stay offline and usable;
    # the offline tests replace this function with fakes.
    raise RetiredProviderPath("BLOCKED — OpenRouter live evaluation is retired: not an approved provider and not under the "
                              "shared provider budget. Use scripts/eval/evaluate_groq_live.py (ledger-guarded).")


def short_error(provider_msg: str, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", provider_msg).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def slug_meta(base: str, slug: str, headers: list[tuple[str, str]]) -> dict | None:
    """GET /models: verify the slug is actually offered and record provider-returned pricing
    (no invented numbers). Unauthenticated-but-public call on the same verified host."""
    code, body = http_json(f"{base.rstrip('/')}/models", headers=headers)
    if code != 200 or not isinstance(body, dict):
        return {"model_list_available": False, "status": code}
    for m in body.get("data", []):
        if m.get("id") == slug:
            return {
                "model_list_available": True,
                "name": m.get("name"),
                "context_length": m.get("context_length"),
                "pricing": m.get("pricing"),
                "supported_parameters": m.get("supported_parameters"),
            }
    return {"model_list_available": False, "status": code, "note": f"slug {slug!r} not found in /models"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check-config", action="store_true",
                    help="offline: validate local configuration only, then exit (no network traffic)")
    ap.add_argument("--limit", type=int, default=MAX_REQUESTS, help="safety cap, default 15")
    ap.add_argument("--only", type=str, default="",
                    help="comma-separated case ids to attempt (subset of the budget)")
    ap.add_argument("--merge", action="store_true",
                    help="offline stage: rebuild results from saved case-*.response.json "
                         "artifacts (no network traffic), then emit results.json")
    ap.add_argument("--max-tokens", type=max_tokens_arg, default=DEFAULT_MAX_TOKENS, metavar="N",
                    help=f"completion-token limit per request (default {DEFAULT_MAX_TOKENS}, "
                         f"max {CLI_MAX_TOKENS_LIMIT})")
    ap.add_argument("--reasoning-max-tokens", type=max_tokens_arg, default=None, metavar="N",
                    help="optional cap on hidden reasoning tokens, inside --max-tokens (default: no cap; "
                         "must be smaller than --max-tokens so the document keeps room)")
    ap.add_argument("--model", type=str, default=None, metavar="SLUG",
                    help="evaluator-only candidate model override for this run (production configuration unchanged); "
                         "still blocked unless /models lists it with zero prompt and completion prices")
    ap.add_argument("--disable-reasoning", action="store_true",
                    help="send reasoning {enabled: false} explicitly (evaluator-only; requires /models to list "
                         "`reasoning`; cannot be combined with --reasoning-max-tokens)")
    args = ap.parse_args()
    if args.model is not None and not MODEL_SLUG_RE.fullmatch(args.model):
        ap.error(f"--model must be an OpenRouter slug like vendor/model:free, got {args.model!r}")
    if args.disable_reasoning and args.reasoning_max_tokens is not None:
        ap.error("--disable-reasoning cannot be combined with --reasoning-max-tokens")
    if args.reasoning_max_tokens is not None and args.reasoning_max_tokens >= args.max_tokens:
        ap.error(f"--reasoning-max-tokens ({args.reasoning_max_tokens}) must be smaller than --max-tokens ({args.max_tokens})")
    try:   # the same validation the worker applies at start-up (prompt_builder.resolve_generation_settings)
        settings = resolve_generation_settings("openrouter", max_tokens=args.max_tokens,
                                               reasoning="off" if args.disable_reasoning else "auto",
                                               reasoning_max_tokens=args.reasoning_max_tokens)
    except GenerationConfigError as e:
        ap.error(str(e))

    unknown = [s.strip() for s in args.only.split(",") if s.strip() and s.strip() not in {c.cid for c in CASES}]
    if unknown:   # before configuration, network or artifacts: a typo must not replace results with an empty run
        print(f"unknown case id(s) for --only: {', '.join(unknown)} (use the full id, e.g. {CASES[0].cid})", file=sys.stderr)
        return 2

    env = load_env_records()
    if args.check_config:
        rc = run_check_config(env)   # never falls through to /models or the live loop
        if rc == 0 and (args.model or args.disable_reasoning):
            print(f"[check-config] evaluator-only override (offline, not applied to production): "
                  f"model={args.model or 'configured'} reasoning={'disabled' if args.disable_reasoning else 'default'}")
        return rc
    info = verify_preconditions(env)
    base, slug, key, key_source = info["base"], info["slug"], info["key"], info["key_source"]
    configured_slug = slug
    if args.model and not args.merge:
        slug = args.model           # this run only; compose/.env and the worker keep configured_slug
    reasoning_record = {"enabled": settings["reasoning"], "max_tokens": settings["reasoning_max_tokens"],
                        "explicitly_disabled": bool(args.disable_reasoning)}

    templates = Templates.load(ROOT / "prompts")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.merge:
        rows: list[dict] = []
        budget_used = 0
        failed_cids = {p.name.removeprefix("case-").removesuffix(".diagnostic.json")
                       for p in OUT_DIR.glob("case-*.diagnostic.json")}
        current = source_hashes()
        for diag_file in sorted(OUT_DIR.glob("case-*.diagnostic.json")):
            # a diagnostic records the latest attempt as failed: never re-score a response for that case
            d = json.loads(diag_file.read_text(encoding="utf-8"))
            budget_used += 1
            status, changed = version_status(d.get("source_sha256"), current)
            rows.append({"cid": d.get("cid"), "model_slug": (d.get("generation") or {}).get("model_slug") or slug,
                         "request": "failed",
                         "failure_reason": d.get("failure_reason"), "error": d.get("detail"),
                         "outcome": d.get("outcome") or failure_outcome(d.get("failure_reason")),
                         "would_source": "would_fallback",
                         "diagnostics": d.get("diagnostics", {}), "row_source": "rebuilt_from_diagnostic",
                         "generation": d.get("generation"), "version_status": status,
                         "sources_changed_since_generation": changed})
        for artifact in sorted(OUT_DIR.glob("case-*.response.json")):
            data = json.loads(artifact.read_text(encoding="utf-8"))
            if data.get("cid") in failed_cids:
                print(f"[merge] {data.get('cid')}: diagnostic present — response not re-scored")
                continue
            budget_used += 1
            case = next((c for c in CASES if c.cid == data["cid"]), None)
            if case is None:
                continue
            ctx = context_for(case)
            # a re-scored response is historical unless every hashed source is unchanged; no hashes = unknown
            gen = data.get("source_sha256")
            status, changed = version_status(gen, current)
            diag_in = data.get("diagnostics") or {}
            if is_truncated(data.get("finish_reason"), diag_in.get("finish_reason"), diag_in.get("native_finish_reason")):
                # a legacy response saved before truncation was its own outcome: never gated or scored as a document
                rows.append({
                    "cid": case.cid, "model_slug": (data.get("generation") or {}).get("model_slug") or slug,
                    "http_status": data.get("http_status"),
                    "seconds": data.get("seconds"), "request": "failed", "failure_reason": TRUNCATED_MODEL_OUTPUT,
                    "error": f"{TRUNCATED_MODEL_OUTPUT}: saved response has finish_reason={data.get('finish_reason')}",
                    "outcome": GENERATION_TRUNCATED, "would_source": "would_fallback",
                    "model_returned": data.get("model_returned"), "finish_reason": data.get("finish_reason"),
                    "usage": data.get("usage", {}), "row_source": "rebuilt_from_artifact",
                    "generation": data.get("generation"), "generated_with_sha256": gen,
                    "generated_under_current_sources": None if changed is None else not changed,
                    "version_status": status, "sources_changed_since_generation": changed,
                })
                continue
            gate = validate_generated_prompt(data["content"], pr_title=case.title, context=ctx)
            rub = qa_rubric.score_prompt(data["content"], pr_title=case.title, context=ctx, gate=gate)
            dec = qa_rubric.accept_result(rub, gate)   # the worker's acceptance rule
            rows.append({
                "cid": case.cid, "model_slug": (data.get("generation") or {}).get("model_slug") or slug,
                "http_status": data["http_status"],
                "seconds": data["seconds"], "request": "ok",
                "model_returned": data.get("model_returned"), "finish_reason": data.get("finish_reason"),
                "usage": data.get("usage", {}),
                "gate_valid": gate["valid"], "would_source": "llm" if dec["accepted"] else "would_fallback",
                "quality_failure": dec["failure"],
                "outcome": GENERATION_USABLE if dec["accepted"] else GENERATION_GATE_REJECTED,
                "gate_errors": gate["errors"][:8], "gate_warnings": gate.get("warnings", [])[:6],
                "quality_score": round(rub.quality_score, 1),
                "dimensions": {k: rub.score(k) for k in qa_rubric.DIMENSIONS},
                "hard_fails": rub.hard_fails, "human_flags": rub.human_flags,
                "row_source": "rebuilt_from_artifact",
                "generation": data.get("generation"),
                "generated_with_sha256": data.get("source_sha256"),
                "generated_under_current_sources": None if changed is None else not changed,
                "version_status": status,
                "sources_changed_since_generation": changed,
            })
        ok_rows = [r for r in rows if r["request"] == "ok"]
        summary = {
            "evaluation_mode": "offline_rescore",
            "evaluation_note": ("saved responses re-scored with the current gate and rubric; no generation request was "
                                "made. version_status says whether each sample was generated under the current sources "
                                "(historical/unknown samples are not evidence for the current prompt)"),
            "endpoint_host": base, "model_slug": slug,
            "n8n_retry_policy_note": GENERATION_POLICY_NOTE,
            "stopped_reasons": [],
            "usage_totals": {
                "request_budget": MAX_REQUESTS,
                "requests_spent": budget_used,
                "successful": sum(r["request"] == "ok" for r in rows),
                "failed": sum(r["request"] != "ok" for r in rows),
                "outcomes": {o: sum((r.get("outcome") or "unknown") == o for r in rows) for o in sorted({r.get("outcome") or "unknown" for r in rows})},
                "version_status": {v: sum((r.get("version_status") or "unknown") == v for r in rows)
                                   for v in sorted({r.get("version_status") or "unknown" for r in rows})},
                "prompt_tokens": sum((r.get("usage") or {}).get("prompt_tokens") or 0 for r in ok_rows),
                "completion_tokens": sum((r.get("usage") or {}).get("completion_tokens") or 0 for r in ok_rows),
                "total_tokens": sum((r.get("usage") or {}).get("total_tokens") or 0 for r in ok_rows),
            },
            "scored_with_sha256": source_hashes(),
            "results": rows,
        }
        archive_before_attempt([], label="merge", remove_stale=False)       # results.json is about to be replaced
        (OUT_DIR / "results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"[merge] {budget_used} artifacts rebuilt; budget spent {budget_used}/{MAX_REQUESTS}; "
              f"no network traffic")
        return 0

    fp = key_fingerprint(key)
    headers = [
        ("Authorization", f"Bearer {key}"),
        ("Content-Type", "application/json"),
        ("X-Title", "PR QA Pilot"),
    ]
    print(f"[config] base={base} slug={slug} key_source={key_source} "
          f"key[fingerprint]={fp['sha256_12']} len={fp['length']}")

    meta = slug_meta(base, slug, headers)
    print(f"[models] slug found: {meta.get('model_list_available', meta)} "
          f"context_length={meta.get('context_length')} "
          f"pricing={json.dumps(meta.get('pricing'))[:160]}")
    blocked = pricing_block(meta, args.max_tokens, reasoning_max_tokens=settings["reasoning_max_tokens"],
                            reasoning_disabled=args.disable_reasoning)
    if blocked:
        print(f"[blocked] no generation request sent: {blocked}")
        return 3

    wanted_preview = ([s.strip() for s in args.only.split(",") if s.strip()] if args.only
                      else [c.cid for c in CASES[: args.limit]])
    archived = archive_before_attempt([c.cid for c in CASES if c.cid in wanted_preview][: MAX_REQUESTS]) if args.only else \
        archive_before_attempt([], remove_stale=False)
    if archived:
        print(f"[archive] previous artifacts copied to {archived.relative_to(ROOT) if archived.is_relative_to(ROOT) else archived.name}")
    hashes = source_hashes()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "run_meta.json").write_text(json.dumps({
        "design_note": "no timestamps in run_meta (hash-comparable); each case artifact records its own "
                       "generation.generated_at; slug verified against /models pricing as_provider_returned",
        "endpoint_host": urlsplit(base).hostname,
        "base": base,
        "model_slug": slug,
        "slug_meta": meta,
        "key_fingerprint_sha256_12": fp["sha256_12"],
        "request_budget": MAX_REQUESTS,
        "requested_max_tokens": args.max_tokens,
        "requested_reasoning_max_tokens": args.reasoning_max_tokens,
        "reasoning": {**reasoning_record,
                      "note": "reasoning settings are requests to the provider, not guarantees"},
        "model_override": ({"configured_slug": configured_slug, "evaluated_slug": slug,
                            "scope": "evaluator-only, this run"} if slug != configured_slug else None),
        "generation_timeout_s": GENERATION_TIMEOUT_S,
        "template_version": TEMPLATE_VERSION,
        "reference_sha256": templates.reference_sha256,
        "source_sha256": hashes,
        "pipeline_state": {"AUTOMATION_MODE": "review_only",
                           "PROMPT_LLM_LIVE_IN_REVIEW_ONLY": "false"},
    }, indent=2), encoding="utf-8")

    wanted = ([s.strip() for s in args.only.split(",") if s.strip()]
              if args.only else [c.cid for c in CASES[: args.limit]])
    already = {artifact_cid(p) for p in OUT_DIR.glob("case-*.response.json")} if not args.only else set()
    if already:
        print(f"[resume] skipping already-attempted cases: {len(already)} artifacts on disk")
        wanted = [c for c in wanted if c not in already]
        if not wanted:
            print("[resume] nothing left to attempt under the budget")

    n8n_policy = GENERATION_POLICY_NOTE
    results: list[dict] = []
    stopped: list[str] = []
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                    "requests": 0, "successful": 0, "failed": 0}

    def generation_meta(outcome: str, diag: dict | None = None) -> dict:
        """Version provenance for one attempt (no secrets). `reasoning` is what was requested; whether the provider
        honoured it is evidenced only by `reasoning_tokens_reported` (None = the provider did not say)."""
        usage = (diag or {}).get("usage") or {}
        return {"model_slug": slug, "model_returned": (diag or {}).get("model_returned"),
                "provider": "openrouter", "endpoint_host": urlsplit(base).hostname,
                "template_version": TEMPLATE_VERSION, "reference_sha256": templates.reference_sha256,
                "source_sha256": hashes, "requested_max_tokens": settings["max_tokens"],
                "reasoning": dict(reasoning_record),
                "reasoning_tokens_reported": usage.get("reasoning_tokens"),
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "request_count": 1, "retries": 0, "outcome": outcome}

    for case in [c for c in CASES if c.cid in wanted][: MAX_REQUESTS]:
        ctx = context_for(case)
        request_payload = build_llm_request(
            templates, model=slug, provider="openrouter",
            repository="git@synthetic-org/bench-repo", pr_number=case.pr_number,
            test_level="standard", synthetic=True, context=ctx, max_tokens=settings["max_tokens"],
            reasoning="off" if args.disable_reasoning else "auto",
            reasoning_max_tokens=settings["reasoning_max_tokens"])
        if args.disable_reasoning:
            # explicit opt-out (OpenRouter `reasoning.enabled=false`); never combined with a cap or effort
            request_payload["reasoning"] = {"enabled": False}
        # credential-escape sanity: the key value must never ride the payload, and the
        # payload must contain only chat-completion request fields (no headers inside)
        payload_keys = set(request_payload)
        assert not payload_keys - {"model", "messages", "temperature", "max_tokens",
                                   "stream", "reasoning"}, payload_keys
        assert key not in json.dumps(request_payload)
        t0 = time.monotonic()
        try:
            code, body = http_json(
                f"{base.rstrip('/')}/chat/completions", method="POST",
                payload=request_payload, headers=headers,
                timeout=GENERATION_TIMEOUT_S)
            transport = None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            timed_out = isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError)
            transport = TRANSPORT_TIMEOUT if timed_out else TRANSPORT_ERROR
            code, body = 0, {"error": {"message": f"transport failure: {type(e).__name__}: {e}"}}
        elapsed = round(time.monotonic() - t0, 1)

        row: dict = {"cid": case.cid, "model_slug": slug, "http_status": code, "seconds": elapsed}
        echo_source = "\n".join(m.get("content", "") for m in request_payload["messages"])
        if code != 200:
            failure = transport or f"HTTP_{code}"
            error_text, perr = describe_http_failure(body, key=key, echo_source=echo_source)
            http_diag: dict = {"http_status": code}
            if perr is not None:
                http_diag["provider_error"] = perr
            outcome = failure_outcome(failure)
            row.update({
                "failure_reason": failure, "outcome": outcome, "would_source": "would_fallback",
                "version_status": "current",
                "request": "failed",
                "error": error_text,
            })
            if http_diag.get("provider_error"):
                row["diagnostics"] = http_diag
            results.append(row)
            try:   # per-case record of the failed attempt (status and the sanitized provider error only)
                write_json_atomic(OUT_DIR / f"case-{case.cid}.diagnostic.json", {
                    "cid": case.cid, "request": "failed", "failure_reason": failure, "outcome": outcome,
                    "detail": row.get("error"), "seconds": elapsed, "content_present": False,
                    "diagnostics": http_diag, "source_sha256": hashes,
                    "generation": generation_meta(outcome)})
            except OSError as e:
                row["diagnostic_write_error"] = type(e).__name__
            usage_totals["requests"] += 1
            usage_totals["failed"] += 1
            if code in (401, 403, 429) or code == 0 and "transport" in str(row.get("error")):
                stopped.append(f"{case.cid}: HTTP {code} — stopping remaining cases "
                               "(auth failure / rate limit / transport abort)")
            break
        parsed = parse_completion(code, body, key=key, echo_source=echo_source)
        diag = parsed["diagnostics"]
        if parsed["failure_reason"]:
            # HTTP 200 without a usable complete document (empty, malformed or truncated): failed, diagnostics kept,
            # never gated or scored, no retry. Truncated partial text is kept redacted in the diagnostic only.
            outcome = failure_outcome(parsed["failure_reason"])
            row.update({"request": "failed", "failure_reason": parsed["failure_reason"], "outcome": outcome,
                        "would_source": "would_fallback", "version_status": "current",
                        "error": f"{parsed['failure_reason']}: {parsed['detail']}", "diagnostics": diag})
            results.append(row)
            record = {"cid": case.cid, "request": "failed", "failure_reason": parsed["failure_reason"],
                      "outcome": outcome, "detail": parsed["detail"], "seconds": elapsed, "content_present": False,
                      "diagnostics": diag, "source_sha256": hashes, "generation": generation_meta(outcome, diag)}
            partial = parsed.get("partial_content")
            if partial:
                stored_partial, partial_redaction = redact_for_artifact(partial, key)
                record.update({"content_present": True, "partial_content_chars": len(partial),
                               "partial_content_redaction": partial_redaction, "partial_content": stored_partial})
            try:
                write_json_atomic(OUT_DIR / f"case-{case.cid}.diagnostic.json", record)
            except OSError as e:
                row["diagnostic_write_error"] = type(e).__name__
            usage_totals["requests"] += 1
            usage_totals["failed"] += 1
            for k in USAGE_FIELDS:   # provider-reported only; absent counts add nothing
                usage_totals[k] += diag.get("usage", {}).get(k, 0)
            break
        content = parsed["content"]
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        raw_model = body.get("model")
        finish_reason = body["choices"][0].get("finish_reason")
        gate = validate_generated_prompt(content, pr_title=case.title, context=ctx)
        rub = qa_rubric.score_prompt(content, pr_title=case.title, context=ctx, gate=gate)
        dec = qa_rubric.accept_result(rub, gate)   # the worker's acceptance rule (gate + no hard fail + >= 85)
        would_source = "llm" if dec["accepted"] else "would_fallback"
        outcome = GENERATION_USABLE if dec["accepted"] else GENERATION_GATE_REJECTED
        stored, redaction = redact_for_artifact(content, key)
        write_json_atomic(OUT_DIR / f"case-{case.cid}.response.json", {
            "cid": case.cid, "http_status": code, "seconds": elapsed,
            "model_returned": raw_model, "finish_reason": finish_reason,
            "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
            "diagnostics": diag, "redaction": redaction, "source_sha256": hashes,
            "requested_max_tokens": args.max_tokens,
            "requested_reasoning_max_tokens": args.reasoning_max_tokens,
            "outcome": outcome, "generation": generation_meta(outcome, diag),
            "content": stored,
        })
        row.update({
            "request": "ok",
            "model_returned": raw_model,
            "finish_reason": finish_reason,
            "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
            "gate_valid": gate["valid"],
            "would_source": would_source,
            "quality_failure": dec["failure"],
            "outcome": outcome,
            "version_status": "current",
            "gate_errors": gate["errors"][:8],
            "gate_warnings": gate.get("warnings", [])[:6],
            "quality_score": round(rub.quality_score, 1),
            "dimensions": {k: rub.score(k) for k in qa_rubric.DIMENSIONS},
            "hard_fails": rub.hard_fails,
            "human_flags": rub.human_flags,
            "diagnostics": diag,
        })
        if redaction["applied"]:
            row["artifact_redaction"] = redaction
        results.append(row)
        usage_totals["requests"] += 1
        usage_totals["successful"] += 1
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            usage_totals[k] += usage.get(k) or 0
        print(f"[{row['cid']}] HTTP {code} {elapsed}s tokens={row['usage']['total_tokens']} "
              f"gate={'valid' if gate['valid'] else 'INVALID'} score={row['quality_score']} "
              f"hf={len(rub.hard_fails)}")

    summary = {
        "endpoint_host": base, "model_slug": slug,
        "n8n_retry_policy_note": n8n_policy,
        "stopped_reasons": stopped,
        "requested_max_tokens": args.max_tokens,
        "requested_reasoning_max_tokens": args.reasoning_max_tokens,
        "usage_totals": usage_totals,
        "results": results,
    }
    write_json_atomic(OUT_DIR / "results.json", summary)
    print(f"\n[done] requests={usage_totals['requests']} successful={usage_totals['successful']} "
          f"failed={usage_totals['failed']} total_tokens={usage_totals['total_tokens']}")
    if stopped:
        print("[stopped] " + " | ".join(stopped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
