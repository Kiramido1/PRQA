"""One-time, scope-bound authorization for ONE real PR job while PRQA stays in review-only ("single-run grant").

A grant is an operator decision, minted on the host by ``scripts/pipeline/prqa_authorize_run.py`` with ``PRQA_RUN_GRANT_KEY``
(a dedicated HMAC key held only by the operator's secrets file and the worker; n8n, the launcher and the sandbox never
see it, so a component that only holds the worker token cannot mint one).

Token: ``prqa-grant.v1.<base64url(canonical JSON claims)>.<hex HMAC-SHA256(key, "prqa-grant.v1." + payload)>``
Claims: grant_id (32 hex, single use), repository, pr_number, head_sha (40 hex), test_level, issued_at, expires_at
(at most MAX_TTL_S after issued_at), purpose="single_pr_run" | "auto_pr_run", and optionally prompt_sha256 (64 hex).
With prompt_sha256 the grant also binds the exact prompt: the job's prompt text must hash to it (GRANT_PROMPT_MISMATCH).
purpose="auto_pr_run" (issued by scripts/pipeline/prqa_auto_runner.py after the quality gate accepted the prompt) REQUIRES it.

What a valid grant replaces, for exactly one job bound to those claims: the review-only rejection, the
REAL_PROJECT_TESTING_ENABLED / AUTO_EXECUTION_ENABLED gates and the registry enabled/approved gates
(``policy.decide_with_grant``). Everything else stays mandatory: real registry entry, non-production environment, fork
and label rules, synthetic separation, schema validation, dedupe, the quality-gated prompt carried by the job, the
worker/launcher/sandbox isolation, the model proxy and its budget ledger. Consumption is atomic with job creation
(``JobStore.create_with_grant``); every creation/validation/consumption/rejection/outcome is appended to the immutable
``run_grant_audit`` table. Global configuration is never changed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from typing import Any
from collections.abc import Mapping

PREFIX = "prqa-grant.v1."
MAX_TTL_S = 1800
MAX_SKEW_S = 60
PURPOSE = "single_pr_run"
AUTO_PURPOSE = "auto_pr_run"
PURPOSES = (PURPOSE, AUTO_PURPOSE)
PROMPT_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
KEY_RE = re.compile(r"^[0-9a-f]{64}$")
GRANT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
LEVELS = ("smoke", "standard", "comprehensive", "security")
CLAIM_KEYS = {"grant_id", "repository", "pr_number", "head_sha", "test_level", "issued_at", "expires_at", "purpose"}
OPTIONAL_CLAIM_KEYS = {"prompt_sha256"}
MAX_TOKEN_CHARS = 2048


class GrantError(ValueError):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: str, payload: str) -> str:
    return hmac.new(bytes.fromhex(key), (PREFIX + payload).encode("ascii"), hashlib.sha256).hexdigest()


def validate_claims(claims: Mapping[str, Any]) -> None:
    if not CLAIM_KEYS <= set(claims) <= CLAIM_KEYS | OPTIONAL_CLAIM_KEYS:
        raise GrantError("GRANT_MALFORMED", "unexpected or missing claims")
    if not GRANT_ID_RE.match(str(claims["grant_id"])) or not REPO_RE.match(str(claims["repository"])) \
            or not SHA_RE.match(str(claims["head_sha"])) or claims["test_level"] not in LEVELS or claims["purpose"] not in PURPOSES:
        raise GrantError("GRANT_MALFORMED", "malformed claim value")
    if "prompt_sha256" in claims and not PROMPT_SHA_RE.match(str(claims["prompt_sha256"])):
        raise GrantError("GRANT_MALFORMED", "malformed prompt_sha256")
    if claims["purpose"] == AUTO_PURPOSE and "prompt_sha256" not in claims:
        raise GrantError("GRANT_MALFORMED", "an automatic grant must bind the prompt hash")
    for k in ("pr_number", "issued_at", "expires_at"):
        if not isinstance(claims[k], int) or isinstance(claims[k], bool) or claims[k] < 1:
            raise GrantError("GRANT_MALFORMED", f"{k} must be a positive integer")
    if not 0 < claims["expires_at"] - claims["issued_at"] <= MAX_TTL_S:
        raise GrantError("GRANT_TTL_INVALID", f"lifetime must be 1..{MAX_TTL_S}s")
    if str(claims["repository"]).startswith("synthetic-org/"):
        raise GrantError("GRANT_SYNTHETIC_FORBIDDEN", "grants are for real repositories only")


def mint(key: str, *, repository: str, pr_number: int, head_sha: str, now: int, ttl_s: int = 900,
         test_level: str = "standard", prompt_sha256: str | None = None, purpose: str = PURPOSE) -> tuple[str, dict[str, Any]]:
    if not KEY_RE.match(key or ""):
        raise GrantError("GRANT_KEY_INVALID", "PRQA_RUN_GRANT_KEY must be 64 hex characters")
    claims = {"grant_id": secrets.token_hex(16), "repository": repository, "pr_number": int(pr_number), "head_sha": head_sha,
              "test_level": test_level, "issued_at": int(now), "expires_at": int(now) + int(ttl_s), "purpose": purpose}
    if prompt_sha256 is not None:
        claims["prompt_sha256"] = prompt_sha256
    validate_claims(claims)
    payload = _b64(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"{PREFIX}{payload}.{_mac(key, payload)}", claims


def verify(token: Any, key: str, *, now: int) -> dict[str, Any]:
    """Signature, structure and time window. Binding to a job and single use are checked by the caller/store."""
    if not KEY_RE.match(key or ""):
        raise GrantError("GRANT_KEY_NOT_CONFIGURED", "single-run grants are disabled on this worker (no PRQA_RUN_GRANT_KEY)")
    if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS or not token.startswith(PREFIX):
        raise GrantError("GRANT_MALFORMED", "not a prqa-grant.v1 token")
    body = token[len(PREFIX):]
    payload, _, sig = body.rpartition(".")
    if not payload or not re.fullmatch(r"[0-9a-f]{64}", sig) or not re.fullmatch(r"[A-Za-z0-9_-]+", payload):
        raise GrantError("GRANT_MALFORMED", "token structure invalid")
    if not hmac.compare_digest(_mac(key, payload), sig):
        raise GrantError("GRANT_SIGNATURE_INVALID", "grant signature does not verify")
    try:
        claims = json.loads(_unb64(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise GrantError("GRANT_MALFORMED", "claims are not JSON")
    if not isinstance(claims, dict):
        raise GrantError("GRANT_MALFORMED", "claims are not an object")
    validate_claims(claims)
    if claims["issued_at"] > now + MAX_SKEW_S:
        raise GrantError("GRANT_NOT_YET_VALID", "issued in the future")
    if now >= claims["expires_at"]:
        raise GrantError("GRANT_EXPIRED", "grant has expired")
    return claims


def check_binding(claims: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    """The job request must be exactly the granted repository / PR / head SHA / level, real (not synthetic)."""
    pr = request.get("pr") or {}
    if request.get("synthetic"):
        raise GrantError("GRANT_SYNTHETIC_FORBIDDEN", "a grant never applies to a synthetic job")
    if request.get("repository") != claims["repository"]:
        raise GrantError("GRANT_REPOSITORY_MISMATCH", "job repository differs from the granted repository")
    if int(pr.get("number") or 0) != claims["pr_number"]:
        raise GrantError("GRANT_PR_MISMATCH", "job PR number differs from the granted PR")
    if pr.get("head_sha") != claims["head_sha"]:
        raise GrantError("GRANT_SHA_MISMATCH", "job head SHA differs from the granted SHA (PR changed: authorize again)")
    if request.get("test_level") != claims["test_level"]:
        raise GrantError("GRANT_LEVEL_MISMATCH", "job test level differs from the granted level")
    if "prompt_sha256" in claims:
        prompt = request.get("prompt") or {}
        text = prompt.get("text")
        if not isinstance(text, str) or hashlib.sha256(text.encode("utf-8")).hexdigest() != claims["prompt_sha256"] \
                or prompt.get("sha256") != claims["prompt_sha256"]:
            raise GrantError("GRANT_PROMPT_MISMATCH", "job prompt differs from the prompt the grant was issued for")


def public_claims(claims: Mapping[str, Any]) -> dict[str, Any]:
    """Audit-safe view (claims carry no secret; the token and key are never stored)."""
    return {k: claims[k] for k in sorted(CLAIM_KEYS | OPTIONAL_CLAIM_KEYS) if k in claims}
