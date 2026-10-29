"""GitHub Actions intake: authentication, validation and idempotency for ``POST /webhook/actions/intake``.

The n8n receiver (``PRQA 01``, path ``/webhook/prqa/actions``) forwards the RAW request bytes (base64) and the
``X-PRQA-*`` headers here. Everything security-relevant happens in this module so it can be unit-tested
without n8n and so the webhook secret never has to be present in the n8n container.

Contract (client side, see ``.github/workflows/prqa.yml``):

* ``X-PRQA-Timestamp``: unix seconds (integer) when the request was signed.
* ``X-PRQA-Signature``: ``sha256=<hex HMAC-SHA256(PRQA_WEBHOOK_SECRET, "<timestamp>.<raw body>")>``.
  Signing ``timestamp.body`` binds the timestamp to the body; the body is signed byte-for-byte.
* ``X-PRQA-Idempotency-Key`` (optional): must equal the key the worker derives itself from
  ``repository#pr@head_sha:test_level`` (plus the run id when ``force_rerun`` is set). The worker never
  trusts a caller-chosen key.

Decision order (fail closed, cheapest check first):

1. secret configured → 2. headers present and well-formed → 3. timestamp inside the window
→ 4. constant-time signature comparison → 5. rate limit → 6. JSON + schema → 7. consistency
(PR URL, repository, number, SHA, fork) → 8. eligibility (open, not draft) → 9. registry membership
→ 10. idempotency claim (replay protection) → accepted.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from collections.abc import Callable

from . import actions_connections as conns
from .policy import SYNTHETIC_PREFIX, Policy, decide
from .redaction import redact_text

log = logging.getLogger("prqa.actions")

DEFAULT_TOLERANCE_S = 300
MAX_BODY_BYTES = 256 * 1024
SIGNATURE_RE = re.compile(r"^sha256=([0-9a-f]{64})$")
TIMESTAMP_RE = re.compile(r"^[0-9]{9,11}$")
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@#-]{7,99}$")
LEVEL_TO_LABEL = {"smoke": "qa-test:smoke", "standard": "qa-test:standard",
                  "comprehensive": "qa-test:comprehensive", "security": "qa-test:security"}


def sign_actions_request(secret: str, timestamp: int | str, raw_body: bytes) -> str:
    """Reference signer (used by the Actions client, the synthetic tests and the docs)."""
    mac = hmac.new(secret.encode("utf-8"), f"{int(timestamp)}.".encode() + raw_body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def derive_idempotency_key(repository: str, pr_number: int, head_sha: str, test_level: str,
                           *, force_run_id: str | None = None) -> str:
    key = f"gha:{repository}#{int(pr_number)}@{head_sha}:{test_level}"
    if force_run_id:
        key += f":run-{force_run_id}"
    return key


class RateLimiter:
    """Fixed-size sliding window per key (in-process; the worker is a single process)."""

    def __init__(self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = max(1, int(limit))
        self.window_s = float(window_s)
        self.clock = clock
        self._events: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
        now = self.clock()
        with self._lock:
            q = self._events.setdefault(key, collections.deque())
            while q and now - q[0] >= self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return False, max(0.0, self.window_s - (now - q[0]))
            q.append(now)
            return True, 0.0


@dataclass
class IntakeOutcome:
    http_status: int
    outcome: str                      # accepted | duplicate | cancelled | unauthorized | forbidden | rejected | rate_limited
                                      # (+ connection dispatch: blocked | replayed | too_large | unavailable)
    reason_code: str
    reason: str
    body: dict[str, Any]

    def to_response(self) -> tuple[int, dict[str, Any]]:
        payload = {"outcome": self.outcome, "reason_code": self.reason_code, "reason": self.reason}
        payload.update(self.body)
        return self.http_status, payload


class ActionsIntake:
    def __init__(self, *, secret: str, policy: Policy, registry: Any, store: Any, schemas: Any,
                 tolerance_s: int = DEFAULT_TOLERANCE_S, per_repo_limit: int = 20, global_limit: int = 60,
                 auth_failure_limit: int = 30, window_s: float = 600.0,
                 clock: Callable[[], float] = time.time, connections: conns.ConnectionRegistry | None = None,
                 github_read_configured: bool = False) -> None:
        self.secret = secret or ""
        self.connections = connections or conns.ConnectionRegistry(None)
        self.github_read_configured = bool(github_read_configured)
        self.policy = policy
        self.registry = registry
        self.store = store
        self.schemas = schemas
        self.tolerance_s = int(tolerance_s)
        self.clock = clock
        self._per_repo = RateLimiter(per_repo_limit, window_s)
        self._global = RateLimiter(global_limit, window_s)
        self._auth_failures = RateLimiter(auth_failure_limit, window_s)
        if not self.secret:
            log.warning("PRQA_WEBHOOK_SECRET is not configured: every GitHub Actions request will be rejected (401)")

    # ------------------------------------------------------------------ auth --
    def verify(self, *, signature: str | None, timestamp: str | None, raw_body: bytes,
               secret: str | None = None) -> tuple[bool, str, str]:
        """Constant-time HMAC check over ``<timestamp>.<raw body>`` with a replay window. Returns (ok, code, reason).
        ``secret`` defaults to PRQA_WEBHOOK_SECRET; a per-repository connection passes its own."""
        secret = self.secret if secret is None else secret
        if not secret:
            return False, "WEBHOOK_SECRET_NOT_CONFIGURED", "PRQA_WEBHOOK_SECRET is not configured on the worker"
        ts = (timestamp or "").strip()
        sig = (signature or "").strip()
        if not ts or not sig:
            return False, "SIGNATURE_MISSING", "X-PRQA-Signature and X-PRQA-Timestamp are required"
        if not TIMESTAMP_RE.match(ts):
            return False, "TIMESTAMP_MALFORMED", "X-PRQA-Timestamp must be unix seconds"
        if not SIGNATURE_RE.match(sig):
            return False, "SIGNATURE_MALFORMED", "X-PRQA-Signature must be 'sha256=<64 hex chars>'"
        skew = int(self.clock()) - int(ts)
        if abs(skew) > self.tolerance_s:
            return False, "TIMESTAMP_EXPIRED", f"timestamp outside the ±{self.tolerance_s}s window (skew {skew}s)"
        expected = sign_actions_request(secret, ts, raw_body)
        if not hmac.compare_digest(expected.encode("utf-8"), sig.encode("utf-8")):
            return False, "SIGNATURE_INVALID", "signature mismatch"
        return True, "OK", "ok"

    # ---------------------------------------------------------------- intake --
    def handle(self, request: dict[str, Any]) -> IntakeOutcome:
        """``request`` is what the n8n Code node sends: body_b64, signature, timestamp, idempotency_key, user_agent."""
        try:
            raw = base64.b64decode(str(request.get("body_b64") or ""), validate=True)
        except (ValueError, TypeError):
            return self._unauthorized("BODY_UNDECODABLE", "body_b64 is not valid base64")
        if len(raw) > MAX_BODY_BYTES:
            return self._unauthorized("BODY_TOO_LARGE", f"body exceeds {MAX_BODY_BYTES} bytes")
        connection_id = self._peek_connection_id(raw)
        if connection_id is not None:
            return self._handle_connection(request, raw, connection_id)

        ok, code, reason = self.verify(signature=request.get("signature"), timestamp=request.get("timestamp"), raw_body=raw)
        if not ok:
            allowed, _ = self._auth_failures.allow("auth")
            log.warning("actions intake rejected: %s", code)   # never the signature, never the body
            if not allowed:
                return IntakeOutcome(429, "rate_limited", "AUTH_FAILURE_RATE_LIMIT", "too many failed authentications", {"retry_after_s": 60})
            return self._unauthorized(code, reason)

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._rejected("PAYLOAD_INVALID", "body is not valid JSON", 422)
        errors = self.schemas.errors(self.schemas.actions_request, payload)
        if errors:
            return self._rejected("PAYLOAD_INVALID", "payload does not match actions-request.schema.json", 422, errors=errors[:10])

        repo = payload["repository"]
        pr = payload["pull_request"]
        req = payload["request"]
        acts = payload["actions"]
        repository = repo["full_name"]
        number = int(pr["number"])
        head_sha = pr["head"]["sha"]
        level = req["test_level"]

        # Authenticated + schema-valid: an exact repeat (same derived key) is answered as 'duplicate' WITHOUT consuming
        # rate-limit capacity. The probe is read-only; nothing is claimed here. (Replay protection is unchanged: the
        # signature already verified above, the timestamp window applies, and the key was claimed by the first request.)
        force_run = str(acts["run_id"]) if req.get("force_rerun") else None
        key = derive_idempotency_key(repository, number, head_sha, level, force_run_id=force_run)
        if payload.get("source") == "local_poller":
            key = key.replace("gha:", "poll:", 1)
        if req["trigger"] != "unlabel":
            seen = self.store.delivery_seen(key)
            if seen:
                log.info("actions intake duplicate (pre-limit): key=%s", key)
                return IntakeOutcome(200, "duplicate", "DUPLICATE_REQUEST",
                                     "this repository/PR/head SHA/level was already requested; nothing new was started",
                                     {"idempotency_key": key, "first_seen_at": seen.get("first_seen_at"), "job_id": seen.get("job_id"),
                                      "repository": repository, "pr": number, "head_sha": head_sha})

        allowed, retry_after = self._global.allow("global")
        if allowed:
            allowed, retry_after = self._per_repo.allow(repository)
        if not allowed:
            log.warning("actions intake rate-limited: repo=%s", repository)
            return IntakeOutcome(429, "rate_limited", "RATE_LIMITED", "too many requests for this repository; retry later",
                                 {"retry_after_s": int(retry_after) + 1, "repository": repository})

        problems = self._consistency_errors(repository, repo, pr, number)
        if problems:
            return self._rejected("PAYLOAD_INCONSISTENT", "payload fields do not agree with each other", 422, errors=problems)
        if req["trigger"] == "unlabel":
            # Lifecycle through the authenticated route (local poller observed the label gone / PR closed):
            # cancel active jobs of the PR, never start anything. Requires a registered repository like everything else.
            if self.registry.get(repository) is None:
                return self._rejected("REPOSITORY_NOT_REGISTERED", "repository is not in config/repositories.yaml", 403, repository=repository)
            reason = "LABEL_REMOVED" if pr["state"] == "open" else "PR_CLOSED"
            cancelled = self.store.cancel_active(repository, number, reason=reason,
                                                 detail={"source": payload.get("source"), "run_id": str(acts["run_id"])})
            self.store.log_event("lifecycle_cancel", {"repository": repository, "pr_number": number, "reason": reason, "via": "actions_intake",
                                                      "cancelled": [j["id"] for j in cancelled]})
            log.info("actions intake lifecycle cancel: repo=%s pr=%s reason=%s cancelled=%s", repository, number, reason, len(cancelled))
            return IntakeOutcome(200, "cancelled", "LIFECYCLE_CANCEL", f"{reason}: active jobs cancelled, nothing started",
                                 {"cancelled": [{"id": j["id"], "head_sha": j["head_sha"]} for j in cancelled], "repository": repository, "pr": number})
        if pr["state"] != "open":
            return self._rejected("PR_NOT_OPEN", "pull request is not open", 422)
        if pr.get("draft"):
            return self._rejected("PR_DRAFT", "draft pull requests are not tested", 422)

        head_repo = pr["head"].get("repo") or {}
        is_fork = bool(head_repo.get("fork")) or (bool(head_repo.get("full_name")) and head_repo.get("full_name") != repository)
        synthetic = repository.startswith(SYNTHETIC_PREFIX)
        entry = self.registry.get(repository)
        if entry is None:
            log.warning("actions intake rejected: repository not registered: %s", repository)
            return self._rejected("REPOSITORY_NOT_REGISTERED", "repository is not in config/repositories.yaml; onboarding is a manual step", 403,
                                  repository=repository)
        if synthetic and entry.kind != "synthetic":
            return self._rejected("SYNTHETIC_REPO_NOT_REGISTERED", "synthetic repository is not registered with kind=synthetic", 403)

        supplied = str(request.get("idempotency_key") or "").strip()
        if supplied and not hmac.compare_digest(supplied.encode("utf-8"), key.encode("utf-8")):
            return self._rejected("IDEMPOTENCY_KEY_MISMATCH", "X-PRQA-Idempotency-Key does not match the key derived from the payload", 422,
                                  expected_key=key)

        run_url = str(acts.get("run_url") or "")[:500]
        if run_url and not run_url.startswith("https://"):
            run_url = ""
        job_seed = {
            "repository": repository,
            "synthetic": synthetic,
            "test_level": level,
            "pr": {
                "number": number,
                "title": redact_text(str(pr.get("title", ""))[:300]),
                "head_sha": head_sha,
                "base_sha": pr["base"]["sha"],
                "head_ref": str(pr["head"].get("ref", ""))[:255],
                "base_ref": str(pr["base"].get("ref", ""))[:255],
                "is_fork": is_fork,
                "url": f"https://github.com/{repository}/pull/{number}",
            },
            "trigger": {
                "source": payload.get("source") or "github_actions",
                "label": LEVEL_TO_LABEL[level],
                "requested_label": str(req.get("label") or "")[:100] or None,
                "requested_by": redact_text(str(req.get("requested_by") or "")[:100]) or None,
                "delivery_id": key,
                "run_url": run_url or None,
                "actions_trigger": req["trigger"],
            },
        }
        job_seed["trigger"] = {k: v for k, v in job_seed["trigger"].items() if v is not None}

        preview = decide(self.policy, entry, {"repository": repository, "synthetic": synthetic, "pr": job_seed["pr"],
                                               "trigger": {"source": job_seed["trigger"]["source"], "label": LEVEL_TO_LABEL[level]}})
        policy_preview = {
            "automation_mode": self.policy.automation_mode,
            "review_only": self.policy.review_only,
            "expected_decision": {"allowed": preview.allowed, "reason_code": preview.reason_code, "reason": preview.reason,
                                  "execution_mode": preview.execution_mode},
        }

        claim = self.store.claim_delivery(key)
        if not claim["claimed"]:
            log.info("actions intake duplicate: key=%s", key)
            return IntakeOutcome(200, "duplicate", "DUPLICATE_REQUEST",
                                 "this repository/PR/head SHA/level was already requested; nothing new was started",
                                 {"idempotency_key": key, "first_seen_at": claim.get("first_seen_at"), "job_id": claim.get("job_id"),
                                  "repository": repository, "pr": number, "head_sha": head_sha})
        self.store.log_event("actions_request_accepted", {
            "idempotency_key": key, "repository": repository, "pr": number, "head_sha": head_sha, "test_level": level,
            "trigger": req["trigger"], "run_id": str(acts["run_id"]), "run_url": run_url or None, "synthetic": synthetic,
            "policy_preview": policy_preview["expected_decision"]["reason_code"],
            "user_agent": str(request.get("user_agent") or "")[:200],
        })
        log.info("actions intake accepted: repo=%s pr=%s level=%s key=%s", repository, number, level, key)
        return IntakeOutcome(202, "accepted", "ACCEPTED", "request accepted; the pipeline will run the policy gate, the job, validation and reporting", {
            "idempotency_key": key, "job_seed": job_seed, "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())),
            "automation_mode": self.policy.automation_mode, "policy_preview": policy_preview,
            "repository": repository, "pr": number, "head_sha": head_sha, "test_level": level, "synthetic": synthetic,
        })

    # -------------------------------------------- per-repository connection (schema 2.0) --
    @staticmethod
    def _peek_connection_id(raw: bytes) -> str | None:
        """Routing only (nothing is trusted yet): a JSON object carrying `connection_id` is a schema-2.0 dispatch and is
        verified with THAT connection's secret; everything else keeps the original schema-1.0 path and secret."""
        if b'"connection_id"' not in raw:
            return None
        if len(raw) > conns.MAX_CONNECTION_BODY_BYTES:
            return ""                                                  # answered 413 before any parsing
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict) or "connection_id" not in data:
            return None
        return str(data.get("connection_id"))[:64]

    def _auth_failed(self, code: str, reason: str) -> IntakeOutcome:
        allowed, _ = self._auth_failures.allow("auth")
        log.warning("connection dispatch rejected: %s", code)     # never the signature, the secret or the body
        if not allowed:
            return IntakeOutcome(429, "rate_limited", "AUTH_FAILURE_RATE_LIMIT", "too many failed authentications", {"retry_after_s": 60})
        return self._unauthorized(code, reason)

    def _handle_connection(self, request: dict[str, Any], raw: bytes, connection_id: str) -> IntakeOutcome:
        """`to be test` dispatch from .github/workflows/prqa-to-be-test.yml. Fail closed, cheapest check first:
        size → known connection → HMAC with the connection's secret (timestamp window, constant time) → action/label
        → schema → repository bound to the connection → delivery replay → head-SHA idempotency → rate limit →
        consistency → open/not draft → registry → GitHub read credential → review-only/policy → claim → accepted."""
        if len(raw) > conns.MAX_CONNECTION_BODY_BYTES:
            return IntakeOutcome(413, "too_large", "BODY_TOO_LARGE", f"body exceeds {conns.MAX_CONNECTION_BODY_BYTES} bytes", {})
        if not conns.CONNECTION_ID_RE.match(connection_id):
            return self._auth_failed("CONNECTION_ID_MALFORMED", "connection_id is malformed")
        connection = self.connections.get(connection_id)
        if connection is None:
            return self._auth_failed("CONNECTION_UNKNOWN", "unknown or revoked connection")
        ok, code, reason = self.verify(signature=request.get("signature"), timestamp=request.get("timestamp"), raw_body=raw,
                                       secret=connection.secret)
        if not ok:
            return self._auth_failed(code, reason)

        payload = json.loads(raw.decode("utf-8"))                       # parsed once already by _peek_connection_id
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        if event.get("action") not in conns.DISPATCH_ACTIONS:
            return self._rejected("ACTION_NOT_ELIGIBLE", "event action does not dispatch a run (unlabeled/closed/edited never do)", 422)
        if event.get("label") != connection.label:                      # exact, case-sensitive
            return self._rejected("LABEL_NOT_ELIGIBLE", f"label must be exactly '{connection.label}'", 422)
        errors = self.schemas.errors(self.schemas.actions_connection_request, payload)
        if errors:
            return self._rejected("PAYLOAD_INVALID", "payload does not match actions-connection-request.schema.json", 422, errors=errors[:10])
        if abs(int(payload["delivery"]["created_at"]) - int(str(request.get("timestamp")).strip())) > self.tolerance_s:
            return self._rejected("DELIVERY_TIMESTAMP_MISMATCH", "delivery.created_at disagrees with X-PRQA-Timestamp", 422)

        repository = payload["repository"]["full_name"]
        if repository != connection.repository:
            log.warning("connection dispatch rejected: repository mismatch for %s", connection_id)
            return self._rejected("REPOSITORY_MISMATCH", "repository is not the one bound to this connection", 403)
        pr = payload["pull_request"]
        number = int(pr["number"])
        head_sha, base_sha = pr["head"]["sha"], pr["base"]["sha"]
        delivery_id = payload["delivery"]["id"]
        if delivery_id != f"gha-{payload['actions']['run_id']}-{payload['actions']['run_attempt']}":
            return self._rejected("PAYLOAD_INCONSISTENT", "delivery.id does not match the run id and attempt", 422)

        d_key = conns.delivery_key(connection_id, delivery_id)
        if self.store.delivery_seen(d_key):
            return IntakeOutcome(409, "replayed", "DELIVERY_REPLAYED", "this delivery id was already received; nothing new was started",
                                 {"delivery_id": delivery_id})
        key = conns.derive_dispatch_key(connection_id, number, head_sha)
        seen = self.store.delivery_seen(key)
        if seen:
            self.store.claim_delivery(d_key)
            return IntakeOutcome(200, "duplicate", "DUPLICATE_REQUEST",
                                 "this PR head SHA was already dispatched for this connection; nothing new was started",
                                 {"idempotency_key": key, "first_seen_at": seen.get("first_seen_at"), "job_id": seen.get("job_id"),
                                  "repository": repository, "pr": number, "head_sha": head_sha})

        allowed, retry_after = self._global.allow("global")
        if allowed:
            allowed, retry_after = self._per_repo.allow(repository)
        if not allowed:
            return IntakeOutcome(429, "rate_limited", "RATE_LIMITED", "too many requests for this repository; retry later",
                                 {"retry_after_s": int(retry_after) + 1, "repository": repository})
        if head_sha == base_sha:
            return self._rejected("PAYLOAD_INCONSISTENT", "head sha equals base sha", 422)
        if pr["state"] != "open":
            return self._rejected("PR_NOT_OPEN", "pull request is not open", 422)
        if pr["draft"]:
            return self._rejected("PR_DRAFT", "draft pull requests are not tested", 422)

        synthetic = repository.startswith(SYNTHETIC_PREFIX)
        entry = self.registry.get(repository)
        if entry is None:
            return self._rejected("REPOSITORY_NOT_REGISTERED", "repository is not in config/repositories.yaml; onboarding is a manual step", 403,
                                  repository=repository)
        if synthetic and entry.kind != "synthetic":
            return self._rejected("SYNTHETIC_REPO_NOT_REGISTERED", "synthetic repository is not registered with kind=synthetic", 403)
        context_preflight = {"source": "synthetic_fixture" if synthetic else "github_api",
                             "github_api_mode": "mock" if synthetic else self.policy.endpoints()["github_api_mode"],
                             "github_read_credential_configured": self.github_read_configured}
        if not synthetic and not self.github_read_configured:
            # fail closed: PR context is never invented; nothing is claimed so a retry after provisioning can succeed
            return IntakeOutcome(503, "unavailable", "GITHUB_READ_CREDENTIAL_MISSING",
                                 "the read-only GitHub credential for PR context is not configured on the worker; nothing was started",
                                 {"repository": repository, "pr": number, "context_preflight": context_preflight})

        head_repo = pr["head"].get("repo_full_name")
        job_seed = {
            "repository": repository,
            "synthetic": synthetic,
            "test_level": "standard",
            "pr": {"number": number, "title": "", "head_sha": head_sha, "base_sha": base_sha,
                   "head_ref": str(pr["head"]["ref"])[:255], "base_ref": str(pr["base"]["ref"])[:255],
                   "is_fork": bool(head_repo) and head_repo != repository,
                   "url": f"https://github.com/{repository}/pull/{number}"},
            "trigger": {"source": "github_actions_connection", "label": LEVEL_TO_LABEL["standard"],
                        "requested_label": connection.label, "event_action": event["action"],
                        "requested_by": redact_text(str(event.get("sender_login") or "")[:100]) or None,
                        "delivery_id": key, "connection_id": connection_id, "actions_trigger": "label"},
        }
        job_seed["trigger"] = {k: v for k, v in job_seed["trigger"].items() if v is not None}
        preview = decide(self.policy, entry, {"repository": repository, "synthetic": synthetic, "pr": job_seed["pr"],
                                               "trigger": {"source": "github_actions", "label": LEVEL_TO_LABEL["standard"]}})
        policy_preview = {"automation_mode": self.policy.automation_mode, "review_only": self.policy.review_only,
                          "expected_decision": {"allowed": preview.allowed, "reason_code": preview.reason_code, "reason": preview.reason,
                                                "execution_mode": preview.execution_mode}}

        claim = self.store.claim_delivery(key)
        self.store.claim_delivery(d_key)
        if not claim["claimed"]:
            return IntakeOutcome(200, "duplicate", "DUPLICATE_REQUEST",
                                 "this PR head SHA was already dispatched for this connection; nothing new was started",
                                 {"idempotency_key": key, "first_seen_at": claim.get("first_seen_at"), "job_id": claim.get("job_id"),
                                  "repository": repository, "pr": number, "head_sha": head_sha})
        common = {"idempotency_key": key, "repository": repository, "pr": number, "head_sha": head_sha, "event_action": event["action"],
                  "connection_id": connection_id, "automation_mode": self.policy.automation_mode, "policy_preview": policy_preview,
                  "context_preflight": context_preflight, "synthetic": synthetic}
        if not preview.allowed:
            # review-only (or any other policy block): recorded, answered, never handed to the pipeline
            self.store.log_event("connection_dispatch_blocked", {"idempotency_key": key, "repository": repository, "pr": number,
                                                                 "head_sha": head_sha, "reason_code": preview.reason_code})
            log.info("connection dispatch blocked by policy: repo=%s pr=%s reason=%s", repository, number, preview.reason_code)
            return IntakeOutcome(200, "blocked", preview.reason_code, f"dispatch recorded but not executed: {preview.reason}", common)
        self.store.log_event("actions_request_accepted", {"idempotency_key": key, "repository": repository, "pr": number, "head_sha": head_sha,
                                                          "test_level": "standard", "trigger": "label", "event_action": event["action"],
                                                          "run_id": str(payload["actions"]["run_id"]), "synthetic": synthetic,
                                                          "connection_id": connection_id, "policy_preview": preview.reason_code})
        log.info("connection dispatch accepted: repo=%s pr=%s action=%s key=%s", repository, number, event["action"], key)
        return IntakeOutcome(202, "accepted", "ACCEPTED", "request accepted; the pipeline will run the policy gate, the job, validation and reporting",
                             dict(common, job_seed=job_seed, test_level="standard",
                                  received_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))))

    # --------------------------------------------------------------- helpers --
    @staticmethod
    def _consistency_errors(repository: str, repo: dict[str, Any], pr: dict[str, Any], number: int) -> list[str]:
        problems: list[str] = []
        if not REPO_RE.match(repository):
            problems.append("repository.full_name is malformed")
        expected_pr_url = f"https://github.com/{repository}/pull/{number}"
        if pr.get("html_url") != expected_pr_url:
            problems.append(f"pull_request.html_url must be {expected_pr_url}")
        if repo.get("html_url") and repo["html_url"] != f"https://github.com/{repository}":
            problems.append("repository.html_url does not match repository.full_name")
        base_repo = (pr.get("base") or {}).get("repo") or {}
        if base_repo.get("full_name") and base_repo["full_name"] != repository:
            problems.append("pull_request.base.repo.full_name does not match repository.full_name")
        if not SHA_RE.match(str(pr["head"]["sha"])) or not SHA_RE.match(str(pr["base"]["sha"])):
            problems.append("head/base sha must be 40 lowercase hex characters")
        if pr["head"]["sha"] == pr["base"]["sha"]:
            problems.append("head sha equals base sha")
        return problems

    @staticmethod
    def _unauthorized(code: str, reason: str) -> IntakeOutcome:
        return IntakeOutcome(401, "unauthorized", code, reason, {})

    @staticmethod
    def _rejected(code: str, reason: str, status: int, **extra: Any) -> IntakeOutcome:
        return IntakeOutcome(status, "forbidden" if status == 403 else "rejected", code, reason, dict(extra))
