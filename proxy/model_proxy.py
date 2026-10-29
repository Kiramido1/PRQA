#!/usr/bin/env python3
"""prqa-model-proxy: the only path from a sandbox to a model provider. Holds the provider credential (never the sandbox,
never the worker), forwards exactly one endpoint to one fixed upstream origin, and authenticates sandboxes with
short-lived per-job tokens bound to the job's pinned model.

Egress policy
  * fixed upstream origin + prefix (PRQA_MODEL_UPSTREAM_ORIGIN, PRQA_MODEL_UPSTREAM_PREFIX): https://api.groq.com + /openai/v1
  * locked upstream request contract (build_upstream_body): the client's body is never relayed verbatim. Only model,
    messages, tools, tool_choice, parallel_tool_calls, stop and response_format are copied; the proxy then sets
    stream=false, temperature (PRQA_MODEL_TEMPERATURE, 0.2), reasoning_effort (PRQA_MODEL_REASONING_EFFORT, low),
    include_reasoning=false and max_completion_tokens = min(client max_tokens/max_completion_tokens,
    PRQA_MODEL_MAX_COMPLETION_TOKENS = 3000). The upstream response is validated and rebuilt (sanitize_completion:
    no reasoning, no provider extras); a client that asked for stream=true (OpenCode always does) receives that
    validated completion as a synthesized OpenAI SSE stream (content and tool_calls deltas, finish, usage, [DONE])
  * allowed: POST /v1/chat/completions (and GET /v1/models only when PRQA_MODEL_ALLOW_MODELS_LISTING=true, for verification)
  * rejected: CONNECT, absolute-form request targets, any other path/method, `model` != the token's model,
    request body > PRQA_MODEL_MAX_REQUEST_BYTES, upstream response > PRQA_MODEL_MAX_RESPONSE_BYTES (stream truncated → 502),
    more than PRQA_MODEL_MAX_CONCURRENT per job, more than PRQA_MODEL_MAX_REQUESTS per job, upstream slower than PRQA_MODEL_UPSTREAM_TIMEOUT_S
  * headers: upstream headers are a fixed allowlist (Authorization = provider key, Host, Content-Type, Content-Length,
    Accept, Accept-Encoding, User-Agent, X-Title); nothing from the client (session ids, cookies, referers) is relayed
  * two listeners, two roles: INFERENCE (TCP --bind/--port; POST /v1/chat/completions only) and ADMIN (unix socket
    --admin-socket only, 0660 to the launcher's group; /admin/tokens only). Neither serves the other's routes;
    administrative spellings (encoded, dot segments, any method) and the administrative credentials are refused on the
    inference listener; request targets are matched verbatim, never normalised
  * four credentials, four capabilities, all distinct (validate):
      - PRQA_MODEL_PROXY_ADMIN_TOKEN (proxy only; operator via `docker exec`): full token administration — list, issue,
        revoke any token
      - PRQA_MODEL_PROXY_MINT_TOKEN (the launcher): issue a per-job token and revoke the tokens IT issued — nothing else
        (no listing, no other issuer's tokens, no reserved prompt-* job ids)
      - per-job prqa_mt_ tokens (a sandbox): inference only, bound to job + model + TTL (+ optional caps)
      - PRQA_MODEL_PROXY_PROMPT_TOKEN (n8n workflow 03, the prompt-generation service): inference only, no tools, one
        upstream request per call, ledger job id prompt-<X-PRQA-Job-Id>
  * tokens: issued/revoked through the admin socket, TTL-bound, revoked at job end; audit log lines name the job and
    the issuing principal, never a token value
  * upstream errors and malformed or oversized responses are replaced by a sanitized proxy error (status kept)
  * provider budget (proxy/provider_budget.py, shared with scripts/eval/evaluate_groq_live.py): every upstream request is
    reserved in the ledger under PRQA_PROVIDER_BUDGET_DIR first — daily requests/tokens, per-job tokens, a rolling
    tokens-per-minute window (the proxy waits up to PRQA_BUDGET_MAX_WAIT_S for it), one request in flight, and the
    provider's reported rate-limit headers — and settled with the reported usage afterwards. Required for any https
    (real provider) upstream: a missing or invalid budget configuration refuses to start. Never retried.
  * per-token caps (admin-set, only ever tighter than the proxy-wide values): max_requests, max_completion_tokens
  * logging: job id, model, status, latency, byte counts, upstream request id, message roles, finish reason, tool-call
    names and usage — never headers, tokens, prompts, tool arguments or bodies
  * fail closed on any auth/policy/upstream error

Offline tests use a local fake upstream (tests/test_model_proxy.py). Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import logging
import os
import re
import secrets
import socketserver
import ssl
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from collections.abc import Callable
from urllib.parse import unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))        # -I drops the script directory; the module is beside it
import provider_budget as pb  # noqa: E402

log = logging.getLogger("prqa.model-proxy")
DEFAULT_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"
ADMIN_TOKENS_PATH = "/admin/tokens"
ADMIN_ROLE, INFERENCE_ROLE = "admin", "inference"
ADMIN_PRINCIPAL, MINT_PRINCIPAL, PROMPT_PRINCIPAL = "admin", "mint", "prompt"
PROMPT_JOB_PREFIX = "prompt-"                                  # ledger job ids of the prompt-generation credential; never mintable
PLAIN_PATH_RE = re.compile(r"^/[A-Za-z0-9/_.-]*$")            # no query, fragment, %-encoding, backslash or other bytes
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
RELAYED_UPSTREAM_STATUSES = {400, 404, 408, 409, 413, 422, 429, 500, 502, 503, 504}   # 401/403 are the proxy's own credential: 502
FORWARDED_REQUEST_KEYS = ("model", "messages", "tools", "tool_choice", "parallel_tool_calls", "stop", "response_format")
MESSAGE_ROLES = {"system", "developer", "user", "assistant", "tool"}
REASONING_EFFORTS = {"low", "medium", "high", "none"}   # none: the upstream gets no reasoning parameters (non-Groq providers)
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


@dataclass
class ProxyConfig:
    upstream_origin: str                    # scheme://host[:port], fixed
    upstream_prefix: str = "/api/v1"        # upstream path prefix that replaces our /v1
    allowed_model: str = ""
    provider_key: str = ""                  # loaded from PRQA_MODEL_PROVIDER_KEY_FILE; never logged
    admin_token: str = ""
    mint_token: str = ""                    # launcher: issue/revoke own per-job tokens only (empty = disabled)
    prompt_token: str = ""                  # prompt-generation service (n8n): inference only, no tools (empty = disabled)
    max_request_bytes: int = 1024 * 1024
    max_response_bytes: int = 8 * 1024 * 1024
    upstream_timeout_s: float = 120.0
    max_concurrent_per_job: int = 2
    max_requests_per_job: int = 60
    default_ttl_s: int = 3600
    allow_models_listing: bool = False
    title: str = "PR QA Pilot"
    temperature: float = 0.2                # locked upstream request contract (see build_upstream_body)
    reasoning_effort: str = "low"
    max_completion_tokens: int = 3000
    budget: pb.BudgetConfig | None = None   # required for an https upstream (see validate)
    bytes_per_token: float = 4.5            # calibrated request-bytes per prompt token for the budget estimate
    budget_max_wait_s: float = 90.0         # longest wait for the tokens-per-minute window before a 429

    @classmethod
    def from_env(cls, env: dict[str, str]) -> ProxyConfig:
        key = ""
        key_file = env.get("PRQA_MODEL_PROVIDER_KEY_FILE", "")
        if key_file and os.path.isfile(key_file):
            with open(key_file, encoding="utf-8") as fh:
                key = fh.read().strip()
        return cls(
            upstream_origin=env.get("PRQA_MODEL_UPSTREAM_ORIGIN", "https://api.groq.com").rstrip("/"),
            upstream_prefix=env.get("PRQA_MODEL_UPSTREAM_PREFIX", "/openai/v1").rstrip("/"),
            allowed_model=env.get("PRQA_MODEL_ALLOWED_MODEL", ""),
            provider_key=key,
            admin_token=env.get("PRQA_MODEL_PROXY_ADMIN_TOKEN", ""),
            mint_token=env.get("PRQA_MODEL_PROXY_MINT_TOKEN", ""),
            prompt_token=env.get("PRQA_MODEL_PROXY_PROMPT_TOKEN", ""),
            max_request_bytes=int(env.get("PRQA_MODEL_MAX_REQUEST_BYTES", str(1024 * 1024))),
            max_response_bytes=int(env.get("PRQA_MODEL_MAX_RESPONSE_BYTES", str(8 * 1024 * 1024))),
            upstream_timeout_s=float(env.get("PRQA_MODEL_UPSTREAM_TIMEOUT_S", "120")),
            max_concurrent_per_job=int(env.get("PRQA_MODEL_MAX_CONCURRENT", "2")),
            max_requests_per_job=int(env.get("PRQA_MODEL_MAX_REQUESTS", "60")),
            temperature=float(env.get("PRQA_MODEL_TEMPERATURE", "0.2")),
            reasoning_effort=env.get("PRQA_MODEL_REASONING_EFFORT", "low").strip().lower(),
            max_completion_tokens=int(env.get("PRQA_MODEL_MAX_COMPLETION_TOKENS", "3000")),
            allow_models_listing=env.get("PRQA_MODEL_ALLOW_MODELS_LISTING", "false").lower() in ("1", "true", "yes", "on"),
            budget=cls._budget_from_env(env),
            bytes_per_token=float(env.get("PRQA_BUDGET_PROXY_BYTES_PER_TOKEN", "4.5")),
            budget_max_wait_s=float(env.get("PRQA_BUDGET_MAX_WAIT_S", "90")),
        )

    @staticmethod
    def _budget_from_env(env: dict[str, str]) -> pb.BudgetConfig | None:
        if not env.get("PRQA_PROVIDER_BUDGET_DIR") and not env.get("PRQA_MODEL_UPSTREAM_ORIGIN", "https://").startswith("https://"):
            return None                                   # loopback http test fake only (validate() enforces that pairing)
        try:
            return pb.BudgetConfig.from_env(env)
        except pb.BudgetError as exc:
            raise SystemExit(f"provider budget configuration invalid: {exc}") from None

    def validate(self) -> None:
        u = urlsplit(self.upstream_origin)
        if u.scheme not in ("https", "http") or not u.netloc or u.path not in ("", "/") or u.query or u.fragment:
            raise SystemExit("PRQA_MODEL_UPSTREAM_ORIGIN must be scheme://host[:port] only")
        if u.scheme == "http" and u.hostname not in ("127.0.0.1", "localhost"):
            raise SystemExit("plain http upstream is allowed for loopback test fakes only")
        if not self.allowed_model:
            raise SystemExit("PRQA_MODEL_ALLOWED_MODEL is required")
        if not self.admin_token or len(self.admin_token) < 16:
            raise SystemExit("PRQA_MODEL_PROXY_ADMIN_TOKEN (≥16 chars) is required")
        for name, value in (("PRQA_MODEL_PROXY_MINT_TOKEN", self.mint_token), ("PRQA_MODEL_PROXY_PROMPT_TOKEN", self.prompt_token)):
            if value and (len(value) < 32 or not value.isprintable() or " " in value):
                raise SystemExit(f"{name} must be at least 32 printable characters without spaces when set")
        creds = [c for c in (self.admin_token, self.mint_token, self.prompt_token) if c]
        if len(set(creds)) != len(creds) or (self.provider_key and self.provider_key in creds):
            raise SystemExit("the admin, mint and prompt credentials must be distinct from each other and from the provider key")
        if not 0.0 <= self.temperature <= 2.0:
            raise SystemExit("PRQA_MODEL_TEMPERATURE must be within 0..2")
        if self.reasoning_effort not in REASONING_EFFORTS:
            raise SystemExit("PRQA_MODEL_REASONING_EFFORT must be low, medium, high or none")
        if not 1 <= self.max_completion_tokens <= 32768:
            raise SystemExit("PRQA_MODEL_MAX_COMPLETION_TOKENS must be within 1..32768")
        if u.scheme == "https" and self.budget is None:
            raise SystemExit("a real (https) provider upstream requires the provider budget (PRQA_PROVIDER_BUDGET_DIR, PRQA_BUDGET_*)")
        if self.budget is not None:
            try:
                self.budget.check()
                pb.estimate_tokens(1, self.bytes_per_token)
            except pb.BudgetError as exc:
                raise SystemExit(f"provider budget configuration invalid: {exc}") from None
            if not 0 <= self.budget_max_wait_s <= 300:
                raise SystemExit("PRQA_BUDGET_MAX_WAIT_S must be within 0..300")
        if not self.provider_key:
            raise SystemExit("provider key missing (PRQA_MODEL_PROVIDER_KEY_FILE); refusing to start without a credential to inject")


@dataclass
class JobToken:
    job_id: str
    model: str
    secret: str
    expires_at: float
    requests: int = 0
    in_flight: int = 0
    revoked: bool = False
    max_requests: int | None = None      # per-token cap (admin-set, never above the proxy-wide budget); None = proxy-wide
    max_completion_tokens: int | None = None   # per-token completion cap (admin-set, never above the proxy-wide cap)
    halt_on_error: bool = False             # admin-set: the first failed or truncated upstream result revokes the token
    issuer: str = ADMIN_PRINCIPAL           # who issued it: admin, mint (the launcher) or prompt (ephemeral, never stored)
    last_messages_sha: str = ""             # hash of the previous request's messages (repeat detection, see RETRY_TEMPERATURE_STEP)
    repeats: int = 0                        # consecutive identical requests (client retries of the same turn)


class TokenStore:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._by_secret: dict[str, JobToken] = {}
        self._by_job: dict[str, JobToken] = {}
        self._lock = threading.Lock()
        self.clock = clock

    def issue(self, job_id: str, model: str, ttl_s: int, max_requests: int | None = None,
              max_completion_tokens: int | None = None, halt_on_error: bool = False,
              issuer: str = ADMIN_PRINCIPAL) -> JobToken | None:
        """None when a live token of another issuer holds the job id (a mint credential never replaces an admin token)."""
        with self._lock:
            old = self._by_job.get(job_id)
            if old and issuer != ADMIN_PRINCIPAL and old.issuer != issuer and not old.revoked and old.expires_at > self.clock():
                return None
            self._by_job.pop(job_id, None)
            if old:
                old.revoked = True
                self._by_secret.pop(old.secret, None)
            tok = JobToken(job_id=job_id, model=model, secret="prqa_mt_" + secrets.token_urlsafe(32), expires_at=self.clock() + ttl_s,
                           max_requests=max_requests, max_completion_tokens=max_completion_tokens, halt_on_error=halt_on_error,
                           issuer=issuer)
            self._by_secret[tok.secret] = tok
            self._by_job[job_id] = tok
            return tok

    def revoke(self, job_id: str, issuer: str | None = None) -> bool:
        """Revoke a job's token; with ``issuer`` only a token that principal issued (others look absent)."""
        with self._lock:
            tok = self._by_job.get(job_id)
            if not tok or (issuer is not None and tok.issuer != issuer):
                return False
            self._by_job.pop(job_id, None)
            tok.revoked = True
            self._by_secret.pop(tok.secret, None)
            return True

    def lookup(self, secret: str) -> JobToken | None:
        with self._lock:
            for s, tok in self._by_secret.items():
                if hmac.compare_digest(s, secret):
                    if tok.revoked or tok.expires_at <= self.clock():
                        return None
                    return tok
            return None

    def active(self) -> list[dict[str, Any]]:
        with self._lock:
            now = self.clock()
            return [{"job_id": t.job_id, "model": t.model, "expires_in_s": int(t.expires_at - now), "requests": t.requests,
                     "max_requests": t.max_requests, "max_completion_tokens": t.max_completion_tokens, "halt_on_error": t.halt_on_error,
                     "issuer": t.issuer}
                    for t in self._by_job.values() if not t.revoked and t.expires_at > now]


@dataclass
class Stats:
    allowed: int = 0
    denied: int = 0
    upstream_errors: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)


def _mentions_admin(target: str) -> bool:
    """True when the raw request target names the administrative API in any spelling: percent-encoded (also double),
    upper-case, backslashes or dot segments (/v1/../admin, /%61dmin, //ADMIN, /v1\\admin)."""
    t = target
    for _ in range(3):
        t = unquote(t)
    return "admin" in t.replace("\\", "/").lower()


# Upstream 400s caused by the model's own output, not by the request: Groq rejects a generation whose tool call does not
# validate (gpt-oss occasionally names a tool that was not offered). Relayed as 503 so the client retries the same
# request as a new, separately budgeted and counted upstream call.
RETRYABLE_UPSTREAM_CODES = frozenset({"tool_use_failed"})
RETRY_TEMPERATURE_STEP = 0.3
RETRY_TEMPERATURE_MAX = 1.0
# A 429 whose Retry-After exceeds this is a spent quota (e.g. a daily free-tier limit), not a busy minute: relayed as a
# final 400 `provider_quota_exhausted`, so OpenCode ends the session instead of retrying for the rest of the job's runtime.
QUOTA_RETRY_AFTER_S = 600
RETRY_MAX_REPEATS = 3   # after this many rejected repeats of one turn, relay the 400 as final: the session ends instead of looping
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def upstream_error_code(raw: bytes) -> str:
    """Only the machine error code of an upstream error body (a short [a-z0-9_] token); never its message or any echo of
    the request, which may carry account details or prompt text."""
    try:
        err = json.loads(raw.decode("utf-8")).get("error")
        code = err.get("code") if isinstance(err, dict) else None
    except (ValueError, UnicodeDecodeError, AttributeError):
        return ""
    return code if isinstance(code, str) and _ERROR_CODE_RE.match(code) else ""


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


# A single tool result (e.g. a whole OpenAPI document printed by curl) can push one request past the provider's
# tokens-per-minute limit, which then fails every retry of that turn. Long tool outputs are cut to head + tail with a note
# telling the model how to get at the rest; the full output still exists in the sandbox (the model saved or can re-run it).
TOOL_OUTPUT_MAX_CHARS = 3000
TOOL_OUTPUT_HEAD_CHARS = 2200
TOOL_OUTPUT_TAIL_CHARS = 500


def clip_tool_output(content: Any) -> Any:
    if not isinstance(content, str) or len(content) <= TOOL_OUTPUT_MAX_CHARS:
        return content
    cut = len(content) - TOOL_OUTPUT_HEAD_CHARS - TOOL_OUTPUT_TAIL_CHARS
    return (content[:TOOL_OUTPUT_HEAD_CHARS] + f"\n[... {cut} characters cut by the PRQA model proxy: write long output to a "
            "file and read only what you need with head -c / jq ...]\n" + content[-TOOL_OUTPUT_TAIL_CHARS:])


def build_upstream_body(payload: dict[str, Any], cfg: ProxyConfig) -> dict[str, Any]:
    """The exact upstream request for a validated client payload (locked contract; raises ValueError on a malformed
    shape). Unknown client keys (stream_options, temperature, top_p, n, provider routing, ...) are dropped, the
    generation parameters are the proxy's own, and the completion budget can only shrink below the configured cap."""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not all(isinstance(m, dict) and m.get("role") in MESSAGE_ROLES for m in messages):
        raise ValueError("messages must be a list of objects with a known role")
    tools = payload.get("tools")
    if tools is not None and not (isinstance(tools, list) and all(isinstance(t, dict) and t.get("type") == "function"
                                                                  and isinstance(t.get("function"), dict) for t in tools)):
        raise ValueError("tools must be a list of function tools")
    body = {k: payload[k] for k in FORWARDED_REQUEST_KEYS if k in payload}
    body["messages"] = [dict(m, content=clip_tool_output(m.get("content"))) if m.get("role") == "tool" else m for m in messages]
    asked = [v for v in (_positive_int(payload.get("max_tokens")), _positive_int(payload.get("max_completion_tokens"))) if v]
    body.update({"stream": False, "temperature": cfg.temperature, "max_completion_tokens": min([cfg.max_completion_tokens] + asked)})
    if cfg.reasoning_effort != "none":
        body.update({"reasoning_effort": cfg.reasoning_effort, "include_reasoning": False})
    return body


def _tool_call(tc: Any) -> dict[str, Any]:
    fn = tc.get("function") if isinstance(tc, dict) else None
    if not (isinstance(fn, dict) and isinstance(tc.get("id"), str) and tc.get("type", "function") == "function"
            and isinstance(fn.get("name"), str) and fn["name"] and isinstance(fn.get("arguments"), str)):
        raise ValueError("malformed tool call")
    return {"id": tc["id"], "type": "function", "function": {"name": fn["name"], "arguments": fn["arguments"]}}


def sanitize_completion(doc: Any) -> dict[str, Any]:
    """Rebuild an upstream chat completion from its validated fields only: no reasoning text, no provider extras
    (x_groq, system_fingerprint, ...). Raises ValueError on anything that is not a well-formed completion."""
    if not isinstance(doc, dict) or not isinstance(doc.get("choices"), list) or not doc["choices"]:
        raise ValueError("no choices")
    choices = []
    for i, ch in enumerate(doc["choices"]):
        msg = ch.get("message") if isinstance(ch, dict) else None
        if not isinstance(msg, dict):
            raise ValueError("choice without a message")
        content = msg.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError("message content is not text")
        out_msg: dict[str, Any] = {"role": "assistant", "content": content}
        if msg.get("tool_calls"):
            if not isinstance(msg["tool_calls"], list):
                raise ValueError("tool_calls is not a list")
            out_msg["tool_calls"] = [_tool_call(tc) for tc in msg["tool_calls"]]
        finish = ch.get("finish_reason")
        choices.append({"index": ch.get("index", i) if isinstance(ch.get("index", i), int) else i, "message": out_msg,
                        "finish_reason": finish if isinstance(finish, str) else None})
    out: dict[str, Any] = {"id": str(doc.get("id") or "prqa-completion")[:128], "object": "chat.completion",
                           "created": doc["created"] if isinstance(doc.get("created"), int) else int(time.time()),
                           "model": str(doc.get("model") or ""), "choices": choices}
    usage = doc.get("usage")
    if isinstance(usage, dict):
        out["usage"] = {k: usage[k] for k in USAGE_KEYS if isinstance(usage.get(k), int) and not isinstance(usage.get(k), bool)}
    return out


def synthesize_sse(doc: dict[str, Any], include_usage: bool) -> bytes:
    """A sanitized completion as the OpenAI chat.completion.chunk stream a stream=true client expects: role+content,
    one delta per tool call (complete arguments), the finish reason, optionally usage, then [DONE]."""
    base = {"id": doc["id"], "object": "chat.completion.chunk", "created": doc["created"], "model": doc["model"]}
    events = []
    for ch in doc["choices"]:
        msg, idx = ch["message"], ch["index"]
        events.append({**base, "choices": [{"index": idx, "delta": {"role": "assistant", "content": msg.get("content") or ""}, "finish_reason": None}]})
        for n, tc in enumerate(msg.get("tool_calls") or []):
            events.append({**base, "choices": [{"index": idx, "delta": {"tool_calls": [{"index": n, **tc}]}, "finish_reason": None}]})
        events.append({**base, "choices": [{"index": idx, "delta": {}, "finish_reason": ch["finish_reason"] or "stop"}]})
    if include_usage and "usage" in doc:
        events.append({**base, "choices": [], "usage": doc["usage"]})
    return b"".join(b"data: " + json.dumps(e, separators=(",", ":")).encode("utf-8") + b"\n\n" for e in events) + b"data: [DONE]\n\n"


class Handler(BaseHTTPRequestHandler):
    """One class, two roles. ``role`` decides which routes exist at all: the ADMIN role serves only token management
    (and only ever on a unix socket, see make_admin_server); the INFERENCE role serves only model forwarding and refuses
    every administrative spelling explicitly, whatever the method, credential or encoding."""

    role: str = INFERENCE_ROLE
    cfg: ProxyConfig
    tokens: TokenStore
    ledger: pb.Ledger | None = None             # shared provider budget (None only for a loopback test fake)
    stats: Stats
    connection_factory: Callable[[str, float], Any]   # (origin, timeout) -> http.client.HTTPConnection-like
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: D102
        pass

    def __getattr__(self, name: str):
        # every HTTP method (GET, PUT, PATCH, OPTIONS, TRACE, PROPFIND, ...) reaches the same dispatcher, so a method
        # without a route is an explicit policy denial rather than the framework's generic 501
        if name.startswith("do_"):
            return self._dispatch
        raise AttributeError(name)

    # ----------------------------------------------------------------- helpers --
    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _deny(self, status: int, code: str, job_id: str | None = None) -> None:
        self.stats.denied += 1
        self.stats.log.append({"ts": time.time(), "interface": self.role, "job_id": job_id, "decision": "deny", "code": code, "status": status})
        log.warning("deny [%s] %s %r: %s", self.role, self.command, self.path.split("?")[0][:120], code)
        self._reply(status, {"error": {"message": f"prqa-model-proxy: {code}", "type": "proxy_policy", "interface": self.role}})

    def _principal(self) -> str | None:
        """ADMIN_PRINCIPAL or MINT_PRINCIPAL for a valid administrative credential, else None (job and prompt
        credentials are never administrative)."""
        bearer = self._bearer()
        if not bearer:
            return None
        if hmac.compare_digest(bearer, self.cfg.admin_token):
            return ADMIN_PRINCIPAL
        if self.cfg.mint_token and hmac.compare_digest(bearer, self.cfg.mint_token):
            return MINT_PRINCIPAL
        return None

    def _bearer(self) -> str:
        auth = self.headers.get("Authorization") or ""
        return auth[7:] if auth.startswith("Bearer ") else ""

    def _plain_path(self) -> str | None:
        """The request target as an exact origin-form path, or None: no query, fragment, percent-encoding, backslash,
        empty or dot segments. Routes are compared against this string verbatim — no normalisation is ever applied."""
        t = self.path
        if not PLAIN_PATH_RE.match(t):
            return None
        if t != "/" and any(seg in ("", ".", "..") for seg in t.split("/")[1:]):
            return None
        return t

    def _read_body(self) -> bytes | None:
        if self.headers.get("Transfer-Encoding"):
            self._deny(400, "chunked or encoded request bodies are not accepted")
            return None
        raw = self.headers.get("Content-Length") or "0"
        if not raw.isdigit():
            self._deny(400, "invalid Content-Length")
            return None
        length = int(raw)
        if length > self.cfg.max_request_bytes:
            self._deny(413, "request body too large")
            return None
        return self.rfile.read(length) if length else b""

    # ------------------------------------------------------------------ routes --
    def _dispatch(self) -> None:
        target = self.path
        if self.command == "CONNECT":
            return self._deny(405, "CONNECT is not permitted")
        if not target.startswith("/"):
            return self._deny(400, "absolute-form request target is not permitted")
        if self.role == INFERENCE_ROLE and _mentions_admin(target):
            return self._deny(403, "administrative interface is not served on the inference listener")
        path = self._plain_path()
        if path is None:
            return self._deny(400, "request target must be a plain path (no query, encoding, dot or empty segments)")
        body = self._read_body()
        if body is None:
            return None
        if path == "/healthz":
            if self.command not in ("GET", "HEAD"):
                return self._deny(405, "method not permitted")
            if self.role == ADMIN_ROLE:
                return self._reply(200, {"ok": True, "interface": ADMIN_ROLE, "upstream": urlsplit(self.cfg.upstream_origin).netloc,
                                         "model": self.cfg.allowed_model})
            return self._reply(200, {"ok": True, "interface": INFERENCE_ROLE})   # no configuration on the untrusted side
        if self.role == ADMIN_ROLE:
            return self._admin_route(path, body)
        return self._inference_route(path, body)

    def _admin_route(self, path: str, body: bytes) -> None:
        method = self.command
        if path == ADMIN_TOKENS_PATH:
            if method not in ("GET", "POST"):
                return self._deny(405, "method not permitted")
            who = self._principal()
            if not who:
                return self._deny(401, "admin authentication failed")
            if method == "GET":
                if who != ADMIN_PRINCIPAL:
                    return self._deny(403, "the mint credential may only issue and revoke its own job tokens")
                return self._reply(200, {"tokens": self.tokens.active()})
            try:
                spec = json.loads(body.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return self._deny(400, "admin body is not JSON")
            if not isinstance(spec, dict):
                return self._deny(400, "admin body must be a JSON object")
            job_id = str(spec.get("job_id") or "")
            model = str(spec.get("model") or "")
            if not JOB_ID_RE.match(job_id) or model != self.cfg.allowed_model:
                return self._deny(422, "token request must name a job and the configured model")
            if job_id.startswith(PROMPT_JOB_PREFIX):
                return self._deny(422, f"job ids starting with {PROMPT_JOB_PREFIX!r} are reserved for the prompt-generation credential")
            try:
                ttl = max(60, min(24 * 3600, int(spec.get("ttl_s") or self.cfg.default_ttl_s)))
            except (TypeError, ValueError):
                return self._deny(422, "ttl_s must be an integer")
            cap = spec.get("max_requests")
            if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= self.cfg.max_requests_per_job):
                return self._deny(422, "max_requests must be an integer between 1 and the proxy-wide request budget")
            out_cap = spec.get("max_completion_tokens")
            if out_cap is not None and (isinstance(out_cap, bool) or not isinstance(out_cap, int)
                                        or not 1 <= out_cap <= self.cfg.max_completion_tokens):
                return self._deny(422, "max_completion_tokens must be an integer between 1 and the proxy-wide completion cap")
            halt = spec.get("halt_on_error", False)
            if not isinstance(halt, bool):
                return self._deny(422, "halt_on_error must be a boolean")
            tok = self.tokens.issue(job_id, model, ttl, cap, out_cap, halt, issuer=who)
            if tok is None:
                return self._deny(409, "a token of another issuer is active for this job", job_id)
            log.info("token issued for job %s by %s (ttl %ss, max requests %s, max completion tokens %s)", job_id, who, ttl,
                     cap or self.cfg.max_requests_per_job, out_cap or self.cfg.max_completion_tokens)
            reply = {"token": tok.secret, "job_id": job_id, "model": model, "expires_in_s": ttl}
            if cap is not None:
                reply["max_requests"] = cap
            if out_cap is not None:
                reply["max_completion_tokens"] = out_cap
            if halt:
                reply["halt_on_error"] = True
            return self._reply(201, reply)
        if path.startswith(ADMIN_TOKENS_PATH + "/"):
            job_id = path[len(ADMIN_TOKENS_PATH) + 1:]
            if method != "DELETE":
                return self._deny(405, "method not permitted")
            who = self._principal()
            if not who:
                return self._deny(401, "admin authentication failed")
            if not JOB_ID_RE.match(job_id):
                return self._deny(422, "malformed job id")
            ok = self.tokens.revoke(job_id, issuer=None if who == ADMIN_PRINCIPAL else who)
            log.info("token revoked for job %s by %s (existed=%s)", job_id, who, ok)
            return self._reply(200 if ok else 404, {"revoked": ok, "job_id": job_id})
        if path.startswith("/v1/"):
            return self._deny(404, "inference is not served on the administrative interface")
        return self._deny(404, "path not permitted")

    def _inference_route(self, path: str, body: bytes) -> None:
        method = self.command
        bearer = self._bearer()
        if self._principal():
            return self._deny(403, "administrative credential is not accepted on the inference listener")
        prompt = bool(bearer and self.cfg.prompt_token and hmac.compare_digest(bearer, self.cfg.prompt_token))
        if path == DEFAULT_PATH:
            if method != "POST":
                return self._deny(405, "method not permitted")
            tok = self._prompt_job() if prompt else self.tokens.lookup(bearer)
            if not tok:
                return self._deny(401, "invalid, expired or revoked job token")
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return self._deny(400, "request body is not JSON", tok.job_id)
            if not isinstance(payload, dict) or payload.get("model") != tok.model:
                return self._deny(403, "model identifier is not the one bound to this job", tok.job_id)
            if prompt and any(k in payload for k in ("tools", "tool_choice", "parallel_tool_calls")):
                return self._deny(403, "tools are not permitted for the prompt-generation credential", tok.job_id)
            try:
                upstream = build_upstream_body(payload, self.cfg)
            except ValueError as exc:
                return self._deny(400, f"request does not satisfy the chat-completions contract: {exc}", tok.job_id)
            if tok.max_completion_tokens:
                upstream["max_completion_tokens"] = min(upstream["max_completion_tokens"], tok.max_completion_tokens)
            # A client retry of the same turn (identical messages) after a rejected generation would, at the locked low
            # temperature, mostly reproduce the same invalid output: each repeat samples a little more freely.
            msha = hashlib.sha256(json.dumps(upstream["messages"], sort_keys=True).encode("utf-8")).hexdigest()
            with self.tokens._lock:
                tok.repeats = tok.repeats + 1 if msha == tok.last_messages_sha else 0
                tok.last_messages_sha = msha
            if tok.repeats:
                upstream["temperature"] = round(min(RETRY_TEMPERATURE_MAX, upstream["temperature"] + RETRY_TEMPERATURE_STEP * tok.repeats), 2)
                log.info("job %s: repeated request #%d, temperature %s", tok.job_id, tok.repeats, upstream["temperature"])
            opts = payload.get("stream_options")
            self._shape = {"roles": [m.get("role") for m in upstream["messages"]][-6:], "n_messages": len(upstream["messages"]),
                           "tools_offered": len(upstream.get("tools") or [])}
            return self._forward(tok, "POST", DEFAULT_PATH, json.dumps(upstream).encode("utf-8"), stream=payload.get("stream") is True,
                                 include_usage=isinstance(opts, dict) and opts.get("include_usage") is True)
        if path == MODELS_PATH and self.cfg.allow_models_listing and not prompt:
            if method != "GET":
                return self._deny(405, "method not permitted")
            tok = self.tokens.lookup(bearer)
            if not tok:
                return self._deny(401, "invalid, expired or revoked job token")
            return self._forward(tok, "GET", MODELS_PATH, b"", stream=False)
        return self._deny(404, "path not permitted")

    def _prompt_job(self) -> JobToken:
        """An ephemeral, never-stored job for one prompt-generation request: exactly one upstream attempt, the proxy-wide
        completion cap, ledger job id prompt-<X-PRQA-Job-Id> (prompt-builder without a valid header). Concurrency and
        all request/token caps are the shared ledger's."""
        ref = (self.headers.get("X-PRQA-Job-Id") or "").strip()
        job_id = PROMPT_JOB_PREFIX + (ref if JOB_ID_RE.match(ref) and len(ref) <= 100 else "builder")
        return JobToken(job_id=job_id, model=self.cfg.allowed_model, secret="", expires_at=float("inf"), max_requests=1,
                        issuer=PROMPT_PRINCIPAL)

    # ---------------------------------------------------------------- forward --
    def _reserve_budget(self, tok: JobToken, body: bytes):
        """Reservation id, None when no budget applies (loopback test fake), or False after a denial was sent."""
        if self.ledger is None:
            return None
        try:
            allowance = int(json.loads(body).get("max_completion_tokens") or self.cfg.max_completion_tokens)
            need = pb.reservation_tokens(len(body), self.cfg.bytes_per_token, allowance)
        except (ValueError, pb.BudgetError):
            self._deny(503, "provider budget estimate failed", tok.job_id)
            return False
        deadline = time.time() + self.cfg.budget_max_wait_s
        while True:
            try:
                return self.ledger.reserve(source="model-proxy", job_id=tok.job_id, tokens=need, completion_allowance=allowance)
            except pb.BudgetDenied as exc:
                wait = exc.retry_after_s
                if wait is None or time.time() + wait > deadline:
                    self._deny(429, f"provider budget: {exc.reason}", tok.job_id)
                    return False
                log.info("job %s waits %.1fs for the provider budget: %s", tok.job_id, wait, exc.reason)
                time.sleep(wait)
            except (pb.BudgetError, OSError) as exc:
                self._deny(503, f"provider budget unavailable: {type(exc).__name__}", tok.job_id)
                return False

    def _upstream_failure(self, tok: JobToken, started: float, code: str, status: int, upstream_status: int | None = None) -> None:
        """Sanitized failure: the caller learns the class of failure and the upstream status, never the upstream body or
        headers (which may echo the provider account, credential hints or internal details)."""
        self.stats.upstream_errors += 1
        self.stats.log.append({"ts": started, "job_id": tok.job_id, "decision": "upstream_error", "code": code, "upstream_status": upstream_status})
        log.error("upstream failure for job %s: %s (upstream status %s)", tok.job_id, code, upstream_status)
        payload: dict[str, Any] = {"message": f"prqa-model-proxy: {code}", "type": "proxy_upstream"}
        if upstream_status is not None:
            payload["upstream_status"] = upstream_status
        self._reply(status, {"error": payload})

    def _forward(self, tok: JobToken, method: str, path: str, body: bytes, *, stream: bool, include_usage: bool = False) -> None:
        with self.tokens._lock:
            if tok.in_flight >= self.cfg.max_concurrent_per_job:
                return self._deny(429, "too many concurrent requests for this job", tok.job_id)
            # counted BEFORE the upstream call, failures included: a capped token can never cause more upstream attempts
            if tok.requests >= min(self.cfg.max_requests_per_job, tok.max_requests or self.cfg.max_requests_per_job):
                return self._deny(429, "request budget exhausted for this job", tok.job_id)
            tok.in_flight += 1
            tok.requests += 1
        try:
            rid = self._reserve_budget(tok, body)
        except Exception:
            with self.tokens._lock:
                tok.in_flight -= 1
            raise
        if rid is False:
            with self.tokens._lock:
                tok.in_flight -= 1
            return None
        started = time.time()
        settled = {"total": None, "outcome": "transport_error", "remaining": None, "reset": None, "done": False}

        def settle() -> None:
            # BEFORE any reply reaches the client: a client that sends its next request immediately (OpenCode does)
            # must find this one settled, not still in flight
            if settled["done"]:
                return
            settled["done"] = True
            if tok.halt_on_error and settled["outcome"] != "ok":
                with self.tokens._lock:              # no client-side retry or continuation can reach the upstream again
                    tok.revoked = True
                    self.tokens._by_secret.pop(tok.secret, None)
                log.warning("job %s: token revoked after %s (halt_on_error)", tok.job_id, settled["outcome"])
            if self.ledger is None or not rid:
                return
            try:
                self.ledger.settle(rid, total_tokens=settled["total"], outcome=settled["outcome"],
                                   remaining_tokens=settled["remaining"], reset_s=settled["reset"])
            except (pb.BudgetError, OSError) as exc:
                log.error("budget settle failed for job %s: %s (reservation kept)", tok.job_id, type(exc).__name__)

        upstream_path = self.cfg.upstream_prefix + path[len("/v1"):]    # fixed origin + fixed prefix + one of two fixed paths
        headers = {"Authorization": "Bearer " + self.cfg.provider_key, "Host": urlsplit(self.cfg.upstream_origin).netloc,
                   "X-Title": self.cfg.title, "Content-Type": "application/json", "Accept": "application/json",
                   "Accept-Encoding": "identity", "User-Agent": "prqa-model-proxy"}   # fixed allowlist; nothing from the client
        if body:
            headers["Content-Length"] = str(len(body))
        conn = None
        sent = 0
        try:
            conn = self.connection_factory(self.cfg.upstream_origin, self.cfg.upstream_timeout_s)
            conn.request(method, upstream_path, body=body or None, headers=headers)
            resp = conn.getresponse()
            status = int(resp.status)
            settled["remaining"] = _int_header(resp.getheader("x-ratelimit-remaining-tokens"))
            settled["reset"] = pb.parse_reset_seconds(resp.getheader("x-ratelimit-reset-tokens"))
            settled["outcome"] = f"http_{status}"
            request_id = resp.getheader("x-request-id") or ""
            request_id = request_id if REQUEST_ID_RE.match(request_id) else ""
            if not 200 <= status < 300:
                err_code = upstream_error_code(resp.read(self.cfg.max_response_bytes))
                if status in (413, 429):
                    settled["total"] = 0   # refused before any processing: nothing was consumed, release the reservation
                relay = status if status in RELAYED_UPSTREAM_STATUSES else 502
                if status == 400 and err_code in RETRYABLE_UPSTREAM_CODES and tok.repeats < RETRY_MAX_REPEATS:
                    relay = 503                    # a bad *generation* (not a bad request): the client may retry it
                retry_after = _int_header(resp.getheader("retry-after"))
                if status == 429 and retry_after is not None and retry_after > QUOTA_RETRY_AFTER_S:
                    settle()
                    return self._upstream_failure(tok, started, f"provider_quota_exhausted (upstream 429, retry after {retry_after}s)", 400, status)
                settle()
                return self._upstream_failure(tok, started, f"upstream returned HTTP {status}" + (f" ({err_code})" if err_code else ""),
                                              relay, status)
            data = resp.read(self.cfg.max_response_bytes + 1)          # upstream is always non-streaming (locked contract)
            if len(data) > self.cfg.max_response_bytes:
                settle()
                return self._upstream_failure(tok, started, "upstream response exceeds the size limit", 502, status)
            try:
                doc = json.loads(data.decode("utf-8"))
                if path == DEFAULT_PATH:
                    doc = sanitize_completion(doc)
                elif not isinstance(doc, dict):
                    raise ValueError("not an object")
            except (ValueError, UnicodeDecodeError):
                settle()
                return self._upstream_failure(tok, started, "upstream returned a malformed response", 502, status)
            if stream:
                out, ctype = synthesize_sse(doc, include_usage), "text/event-stream"
            else:
                out, ctype = json.dumps(doc).encode("utf-8"), "application/json"
            sent = len(out)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if request_id:
                self.send_header("X-Request-Id", request_id)
            self.send_header("Content-Length", str(sent))
            self.send_header("Connection", "close")
            self.end_headers()
            if path == DEFAULT_PATH and isinstance(doc.get("usage"), dict) and isinstance(doc["usage"].get("total_tokens"), int):
                settled["total"] = doc["usage"]["total_tokens"]
            truncated = path == DEFAULT_PATH and any(c.get("finish_reason") == "length" for c in doc["choices"])
            settled["outcome"] = "truncated" if truncated else "ok"
            settle()
            self.stats.allowed += 1
            self.stats.log.append({"ts": started, "job_id": tok.job_id, "decision": "allow", "model": tok.model, "status": status,
                                   "latency_ms": int((time.time() - started) * 1000), "request_bytes": len(body), "response_bytes": sent,
                                   "stream": stream, "upstream_request_id": request_id or None})
            log.info("job %s %s %s → %d (%d ms, %dB in, %dB out)", tok.job_id, method, path, status, int((time.time() - started) * 1000), len(body), sent)
            if path == DEFAULT_PATH:
                ch = doc["choices"][0]
                shape = getattr(self, "_shape", {})
                log.info("job %s shape: messages=%s roles=%s tools_offered=%s max_completion_tokens=%s finish=%s tool_calls=%s usage=%s",
                         tok.job_id, shape.get("n_messages"), ",".join(str(r) for r in shape.get("roles", [])), shape.get("tools_offered"),
                         json.loads(body).get("max_completion_tokens"), ch.get("finish_reason"),
                         ",".join(tc["function"]["name"] for tc in ch["message"].get("tool_calls") or []) or "-",
                         json.dumps(doc.get("usage") or {}, sort_keys=True))
            self.wfile.write(out)                                      # last: a client acting on the reply sees settled state
        except (OSError, http.client.HTTPException) as exc:
            settle()
            self.stats.upstream_errors += 1
            self.stats.log.append({"ts": started, "job_id": tok.job_id, "decision": "upstream_error", "error": type(exc).__name__})
            log.error("upstream failure for job %s: %s", tok.job_id, type(exc).__name__)
            if sent == 0:
                try:
                    self._reply(502, {"error": {"message": "prqa-model-proxy: upstream unavailable or timed out", "type": "proxy_upstream"}})
                except OSError:
                    pass
        finally:
            settle()                                                   # transport errors: reservation kept
            with self.tokens._lock:
                tok.in_flight -= 1
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass


def _int_header(value: str | None) -> int | None:
    return int(value) if value and value.strip().isdigit() else None


def default_connection_factory(origin: str, timeout: float) -> http.client.HTTPConnection:
    u = urlsplit(origin)
    if u.scheme == "https":
        return http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ssl.create_default_context())
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)


def _handler_class(role: str, cfg: ProxyConfig, tokens: TokenStore, stats: Stats,
                   connection_factory: Callable[[str, float], Any]) -> type:
    handler = type(f"PrqaModelProxy{role.capitalize()}Handler", (Handler,), {})
    handler.role = role
    handler.cfg = cfg
    handler.tokens = tokens
    handler.stats = stats
    handler.connection_factory = staticmethod(connection_factory)   # plain callable, not a bound method
    handler.ledger = pb.Ledger(cfg.budget) if cfg.budget is not None else None
    return handler


def make_server(cfg: ProxyConfig, bind: str = "127.0.0.1", port: int = 0, *, tokens: TokenStore | None = None,
                stats: Stats | None = None, connection_factory: Callable[[str, float], Any] = default_connection_factory) -> ThreadingHTTPServer:
    """The INFERENCE listener (TCP). It has no administrative route in any form."""
    cfg.validate()
    handler = _handler_class(INFERENCE_ROLE, cfg, tokens or TokenStore(), stats or Stats(), connection_factory)
    srv = ThreadingHTTPServer((bind, port), handler)
    srv.daemon_threads = True
    srv.handler_class = handler  # type: ignore[attr-defined]
    return srv


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):  # BaseHTTPRequestHandler expects a (host, port)-like client address
        conn, _ = self.socket.accept()
        return conn, ("unix-socket", 0)


def make_admin_server(cfg: ProxyConfig, socket_path: str, *, socket_gid: int | None = None, tokens: TokenStore | None = None,
                      stats: Stats | None = None) -> _UnixHTTPServer:
    """The ADMIN listener: a unix socket only (mode 0660, group = the launcher's), never a TCP port, so no network —
    the sandbox network, the launcher network or the egress network — carries the administrative API."""
    cfg.validate()
    if os.path.lexists(socket_path):
        if not stat.S_ISSOCK(os.lstat(socket_path).st_mode):
            raise SystemExit(f"{socket_path} exists and is not a socket; refusing to replace it")
        os.unlink(socket_path)                                       # stale socket from a previous run
    handler = _handler_class(ADMIN_ROLE, cfg, tokens or TokenStore(), stats or Stats(), default_connection_factory)
    old = os.umask(0o177)
    try:
        srv = _UnixHTTPServer(socket_path, handler)
    finally:
        os.umask(old)
    if socket_gid is not None and socket_gid >= 0:
        os.chown(socket_path, -1, socket_gid)
    os.chmod(socket_path, 0o660)
    srv.handler_class = handler  # type: ignore[attr-defined]
    return srv


def make_servers(cfg: ProxyConfig, bind: str, port: int, admin_socket: str, *, admin_socket_gid: int | None = None,
                 connection_factory: Callable[[str, float], Any] = default_connection_factory):
    """Both listeners over ONE token store: tokens issued on the admin socket authenticate on the inference port."""
    tokens, stats = TokenStore(), Stats()
    inference = make_server(cfg, bind, port, tokens=tokens, stats=stats, connection_factory=connection_factory)
    admin = make_admin_server(cfg, admin_socket, socket_gid=admin_socket_gid, tokens=tokens, stats=stats)
    return inference, admin


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PRQA model egress proxy")
    ap.add_argument("--bind", default="0.0.0.0", help="inference listener address")
    ap.add_argument("--port", type=int, default=8089, help="inference listener port")
    ap.add_argument("--admin-socket", required=True, help="unix socket path of the administrative listener (never TCP)")
    ap.add_argument("--admin-socket-gid", type=int, default=-1, help="group allowed to open the admin socket (the launcher's)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    cfg = ProxyConfig.from_env(dict(os.environ))
    inference, admin = make_servers(cfg, args.bind, args.port, args.admin_socket, admin_socket_gid=args.admin_socket_gid)
    threading.Thread(target=admin.serve_forever, name="admin", daemon=True).start()
    log.info("prqa-model-proxy inference on %s:%s, admin on unix:%s → %s%s (model %s)", args.bind, args.port, args.admin_socket,
             cfg.upstream_origin, cfg.upstream_prefix, cfg.allowed_model)
    inference.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
