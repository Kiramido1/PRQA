"""HTTP API of the OpenCode worker (stdlib only; no shell, no arbitrary commands).

All endpoints except GET /healthz require the header ``X-Worker-Token``.

  GET  /healthz                       liveness (no auth)
  GET  /config                        policy (public view), endpoints handed to n8n, registry summary
  GET  /registry                      repository registry with effective enablement
  GET  /prompts/reference             canonical reference prompt + sha256
  POST /prompts/build                 {repository, pr_number, test_level, synthetic, context} -> chat-completion request body for the prompt LLM
  POST /prompts/finalize              {generated_text|null, ...} -> validated prompt or reference fallback
  POST /context/build                 {pr, files, diff, openapi?, repository, synthetic} -> sanitized context package
  POST /webhook/evaluate              {event, payload} -> eligibility decision (mirrors n8n Code node)
  POST /webhook/verify                {secret_env?, signature, body_b64} -> {valid}
  POST /webhook/actions/intake        {body_b64, signature, timestamp, idempotency_key?} -> GitHub Actions request:
                                      202 accepted (job_seed) | 200 duplicate | 401 | 403 | 422 | 429  (actions_intake.py);
                                      a body with connection_id is a schema-2.0 `to be test` dispatch verified with that
                                      connection's own secret: + 200 blocked | 409 replayed | 413 | 503 (actions_connections.py)
  POST /deliveries/claim              {delivery_id} -> 200 first time, 409 when already seen (webhook dedupe layer 1)
  POST /jobs                          create job (policy gate + dedupe) -> 201 | 403 | 409 | 422
  GET  /jobs?status=&limit=           list
  GET  /jobs/{id}                     status + result
  POST /jobs/{id}/retry               manual retry of a failed job
  POST /jobs/{id}/cancel              {reason} -> cancel (queued: immediately; running: outcome discarded at the end of the attempt)
  POST /jobs/cancel                   {repository, pr_number, reason, except_head_sha?} -> cancel active jobs of a PR
                                      (receiver lifecycle: label removed / PR closed / new head SHA)
  GET  /workspaces                    per-job isolated workspaces (marker data only) + retention policy
  --- launcher hand-off (X-Launcher-Token; the worker never launches containers) ---
  GET  /launch/pending                live jobs with launch_state=pending (artifact ready)
  POST /jobs/{id}/launch/claim        {launcher_id, lease_s} -> 200 claim (fields to rebuild the plan) | 409 busy | 410 unavailable
  GET  /jobs/{id}/artifact            checkout.tar bytes (owner only; X-Launcher-Id); size + sha256 in the claim
  POST /jobs/{id}/launch/heartbeat    {launcher_id, state?} -> {cancel_requested, lease_until}
  POST /jobs/{id}/launch/result       {launcher_id, result, cleanup}   (idempotent)
  POST /jobs/{id}/launch/failed       {launcher_id, error, cleanup}    (idempotent)
  POST /jobs/{id}/launch/cancelled    {launcher_id, cleanup}           (idempotent)
  POST /workspaces/cleanup            retention sweep (removes only PRQA-marked workspaces past retention)
  POST /jobs/{id}/notification        {channel, status, detail}
  POST /jobs/{id}/notification/claim  {channel} -> 200 claimed | 409 already delivered / in flight (duplicate guard)
  POST /notifications/discord         {kind: result|blocked, job_id?, result?, result_validation?, job_seed?, decision?, pipeline?, prompt?}
                                      -> renders + delivers the Discord report (real only when policy allows AND DISCORD_WEBHOOK_URL is set;
                                      otherwise the mock sink). Never before the job succeeded; never without validating the result.
  POST /results/validate              {result} -> {valid, errors, reconciliation}
  POST /reports/render                {result} -> markdown, discord payload, github comment
  POST /events                        {kind, detail, job_id?} -> log a sanitized event
  GET  /events?limit=&job_id=
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .actions_connections import ConnectionRegistry
from .actions_intake import ActionsIntake
from .discord import DiscordSender, build_blocked_message, build_result_messages, deliver_messages
from .executor import Executor
from .jobs import DuplicateDelivery, DuplicateJob, JobStore
from . import run_grants
from .policy import GRANT_REPLACEABLE, Policy, decide, decide_with_grant
from .prompt_builder import GENERATION_TIMEOUT_S, TEMPLATE_VERSION, Templates, build_llm_request, finalize_prompt
from .redaction import redact_value
from .registry import Registry
from .report import inconclusive_placeholder, render_all
from .validation import DEFAULT_TEST_LEVEL, DEFAULT_TRIGGER_LABEL, SchemaStore, build_context, evaluate_event, parse_actor_allowlist, validate_result, verify_signature
from .workspace import WorkspaceManager

log = logging.getLogger("prqa.server")
LAUNCHER_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")   # same rule as launcher_service.LAUNCHER_TOKEN_RE
MAX_BODY = 8 * 1024 * 1024
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$")


class App:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = dict(os.environ if env is None else env)
        self.env = env
        self.token = env.get("PRQA_WORKER_TOKEN", "")
        if not self.token:
            raise SystemExit("PRQA_WORKER_TOKEN must be set")
        self.policy = Policy.from_env(env)
        self.policy.self_check()
        self.registry = Registry.load(env.get("PRQA_REGISTRY_PATH", "config/repositories.yaml"), self.policy)
        self.schemas = SchemaStore(env.get("PRQA_SCHEMA_DIR", "schemas"))
        self.templates = Templates.load(env.get("PRQA_PROMPTS_DIR", "prompts"))
        self.fixtures_dir = Path(env.get("PRQA_FIXTURES_DIR", "fixtures"))
        data_dir = Path(env.get("PRQA_DATA_DIR", "data"))
        self.store = JobStore(data_dir / "jobs.db")
        self.trigger_label = (env.get("PRQA_TRIGGER_LABEL") or DEFAULT_TRIGGER_LABEL).strip() or DEFAULT_TRIGGER_LABEL
        self.default_test_level = (env.get("PRQA_DEFAULT_TEST_LEVEL") or DEFAULT_TEST_LEVEL).strip().lower()
        if self.default_test_level not in ("smoke", "standard", "comprehensive", "security"):
            self.default_test_level = DEFAULT_TEST_LEVEL
        self.allowed_actors = parse_actor_allowlist(env.get("PRQA_TRIGGER_ALLOWED_ACTORS"))
        ws_root = env.get("PRQA_WORKSPACE_ROOT") or str(data_dir / "workspaces")
        self.workspaces = WorkspaceManager(ws_root, retention_hours=float(env.get("PRQA_WORKSPACE_RETENTION_HOURS", "24")),
                                           min_free_mb=float(env.get("PRQA_WORKSPACE_MIN_FREE_MB", "1024")),
                                           max_workspaces=int(env.get("PRQA_WORKSPACE_MAX_COUNT", "50")))
        self.executor = Executor(self.store, self.policy, self.registry, self.schemas, self.fixtures_dir,
                                 mock_delay_s=float(env.get("MOCK_EXECUTION_DELAY_S", "2")), workspaces=self.workspaces,
                                 worker_checkout_enabled=env.get("PRQA_WORKER_CHECKOUT_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on"),
                                 checkout_timeout_s=int(env.get("PRQA_CHECKOUT_TIMEOUT_S", "300")),
                                 github_token=env.get("GITHUB_TOKEN", ""), max_checkout_mb=float(env.get("PRQA_CHECKOUT_MAX_MB", "500")),
                                 checkout_remote_base=env.get("PRQA_CHECKOUT_REMOTE_BASE", ""))
        self.intake = ActionsIntake(
            secret=env.get("PRQA_WEBHOOK_SECRET", ""), policy=self.policy, registry=self.registry, store=self.store, schemas=self.schemas,
            tolerance_s=int(env.get("PRQA_WEBHOOK_TOLERANCE_S", "300")),
            per_repo_limit=int(env.get("PRQA_ACTIONS_RATE_LIMIT_PER_REPO", "20")),
            global_limit=int(env.get("PRQA_ACTIONS_RATE_LIMIT_GLOBAL", "60")),
            window_s=float(env.get("PRQA_ACTIONS_RATE_LIMIT_WINDOW_S", "600")),
            connections=ConnectionRegistry(env.get("PRQA_ACTIONS_CONNECTIONS_PATH", "").strip() or None),
            github_read_configured=bool(env.get("GITHUB_TOKEN", "").strip()))
        # single-run grants (run_grants.py): verification key only; empty = grants disabled (every grant is refused)
        self.run_grant_key = env.get("PRQA_RUN_GRANT_KEY", "").strip()
        if self.run_grant_key and not run_grants.KEY_RE.match(self.run_grant_key):
            log.error("PRQA_RUN_GRANT_KEY is malformed: single-run grants stay disabled")
            self.run_grant_key = ""
        self.clock = time.time
        self.launcher_token = env.get("PRQA_LAUNCHER_TOKEN", "").strip()
        if self.launcher_token and (not LAUNCHER_TOKEN_RE.match(self.launcher_token) or self.launcher_token == self.token):
            # fail closed: a malformed (or worker-token-equal) launcher credential leaves the launcher API disabled
            log.error("PRQA_LAUNCHER_TOKEN is malformed or equals the worker token: the launcher API stays disabled")
            self.launcher_token = ""
        self._assert_no_docker_access(env)
        recovered = self.store.recover_worker_restart()
        if recovered:
            log.warning("worker restart recovery: re-queued %d mock job(s) left running", len(recovered))
        self.discord_webhook_url = env.get("DISCORD_WEBHOOK_URL", "").strip()
        self.discord_timeout_s = float(env.get("DISCORD_HTTP_TIMEOUT_S", "10"))
        self.discord_max_attempts = int(env.get("DISCORD_MAX_ATTEMPTS", "3"))
        self.store.log_event("worker_started", {"version": __version__, "policy": self.policy.to_public_dict()})
        for name in self.policy.forced_off:
            log.warning("review-only mode: %s was set to true and has been FORCED OFF", name)
        if self.policy.review_only and self.policy.prompt_llm_live_in_review_only:
            log.warning("PROMPT_LLM_LIVE_IN_REVIEW_ONLY=true: prompt-builder LLM calls will reach %s (%s); "
                        "all other integrations remain mock/blocked", self.policy.prompt_llm_api_base, self.policy.prompt_llm_model)

    @staticmethod
    def _assert_no_docker_access(env: dict[str, str]) -> None:
        """Startup safety: the worker must never be able to launch containers. Enforced when PRQA_ENFORCE_NO_DOCKER is
        set (compose sets it for the worker service). Reports only paths/names, never values."""
        if env.get("PRQA_ENFORCE_NO_DOCKER", "false").strip().lower() not in ("1", "true", "yes", "on"):
            return
        import shutil as _shutil
        problems = []
        sockets = [x.strip() for x in (env.get("PRQA_DOCKER_SOCKET_PATH") or "/var/run/docker.sock,/run/docker.sock").split(",") if x.strip()]
        for sock in sockets:
            if Path(sock).exists():
                problems.append(f"docker socket present at {sock}")
        if env.get("DOCKER_HOST"):
            problems.append("DOCKER_HOST is set")
        if _shutil.which("docker", path=env.get("PATH")):
            problems.append("docker CLI on PATH")
        if problems:
            raise SystemExit("worker refuses to start with Docker access (PRQA_ENFORCE_NO_DOCKER): " + "; ".join(problems))

    # ----------------------------------------------------------- handlers --
    def config(self) -> dict[str, Any]:
        return {
            "worker_version": __version__,
            "policy": self.policy.to_public_dict(),
            "endpoints": self.policy.endpoints(),
            "registry": self.registry.summary(self.policy),
            "prompt_template_version": TEMPLATE_VERSION,
            "reference_prompt_sha256": self.templates.reference_sha256,
            "job_stats": self.store.stats(),
            "trigger": {"label": self.trigger_label, "default_test_level": self.default_test_level, "qa_test_labels": True,
                        "allowed_actors": list(self.allowed_actors), "actor_check": "webhook sender.login (GitHub-authenticated) only; Actions/poller requested_by is caller-supplied",
                        "lifecycle": {"unlabeled": "cancel active jobs", "closed": "cancel active jobs", "synchronize": "cancel jobs of older head SHAs; re-label to test the new SHA"}},
            "launch": {"handoff": "launch_state lifecycle (pending → claimed → launching → finished|failed|cancelled)",
                       "launcher_token_configured": bool(self.launcher_token), "in_process_launch": False,
                       "single_run_grants_enabled": bool(self.run_grant_key),
                       "lease_max_attempts": self.store.LAUNCH_MAX_ATTEMPTS, "pending": len(self.store.list_launch_pending(100))},
            "workspaces": {"root": str(self.workspaces.root), "retention_hours": self.workspaces.retention_s / 3600.0,
                           "worker_checkout_enabled": self.executor.worker_checkout_enabled, "checkout_timeout_s": self.executor.checkout_timeout_s,
                           "max_checkout_mb": self.executor.max_checkout_mb,
                           "checkout_remote": self.executor.checkout_remote_base or "https://github.com", "min_free_mb": self.workspaces.min_free_mb,
                           "max_workspaces": self.workspaces.max_workspaces, "github_token_configured": bool(self.env.get("GITHUB_TOKEN")),
                           "sandbox_network": self.env.get("PRQA_SANDBOX_NETWORK", "none"), "sandbox_launcher": "none (no Docker CLI in the worker image; Phase 2 decision)"},
            "actions_intake": {"path": "/webhook/prqa/actions", "webhook_secret_configured": bool(self.intake.secret),
                               "timestamp_tolerance_s": self.intake.tolerance_s, "schema": "schemas/actions-request.schema.json",
                               "connections": self.intake.connections.summary(),
                               "connection_schema": "schemas/actions-connection-request.schema.json"},
            "discord": {"mode": self.policy.endpoints()["discord_mode"], "webhook_url_configured": bool(self.discord_webhook_url),
                        "real_delivery_possible": self.discord_real},
        }

    # ------------------------------------------------------------ discord --
    @property
    def discord_real(self) -> bool:
        """Real delivery needs BOTH the policy flag (live mode + REAL_DISCORD_REPORTING_ENABLED) and a configured URL."""
        return self.policy.endpoints()["discord_mode"] == "real" and bool(self.discord_webhook_url)

    def make_sender(self, url: str) -> DiscordSender:  # overridable in tests
        return DiscordSender(url, timeout_s=self.discord_timeout_s, max_attempts=self.discord_max_attempts)

    # ------------------------------------------------------------ launcher --
    def _claim_payload(self, job: dict[str, Any]) -> dict[str, Any]:
        entry = self.registry.get(job["repository"])
        ws = job.get("workspace") or {}
        launch = job.get("launch") or {}
        art = launch.get("artifact") or {}
        req = job["request"]
        return {
            "job_id": job["id"], "repository": job["repository"], "test_level": job["test_level"],
            "pr": {"number": req["pr"]["number"], "head_sha": req["pr"]["head_sha"], "title": req["pr"].get("title", "")},
            "model": ((entry.opencode if entry else {}) or {}).get("model") or self.policy.opencode_model,
            "base_url": str(((entry.api if entry else {}) or {}).get("base_url") or ""),   # target API (non-secret) → sandbox BASE_URL
            "max_runtime_minutes": entry.max_runtime_minutes if entry else 30,
            "expected_hashes": {"tree_sha256": ws.get("tree_sha256") or art.get("tree_sha256"), "prompt_sha256": ws.get("prompt_sha256") or art.get("prompt_sha256")},
            "artifact": {"size": art.get("size"), "sha256": art.get("sha256"), "files": art.get("files")},
            "launch_state": job.get("launch_state"), "launch_owner": job.get("launch_owner"), "lease_until": job.get("lease_until"),
            "launch_attempts": job.get("launch_attempts"), "cancel_requested": bool(job.get("cancel")), "status": job["status"],
            "automation_mode": self.policy.automation_mode,
        }

    def launch_route(self, method: str, path: str, query: dict[str, list[str]], body: dict[str, Any], headers: dict[str, str]) -> tuple[int, Any]:
        granted_only = self.policy.review_only
        if granted_only and not self.run_grant_key:
            # Defense in depth: while review-only no live job can exist (POST /jobs rejects them); the hand-off is inert too.
            return 403, {"error": "REVIEW_ONLY_MODE", "detail": "launch hand-off is disabled while AUTOMATION_MODE=review_only"}
        # review-only with single-run grants enabled: the hand-off serves ONLY jobs bound to a consumed grant.
        launcher_id = str(body.get("launcher_id") or headers.get("x-launcher-id") or "").strip()[:80]
        if method == "GET" and path == "/launch/pending":
            limit = max(1, min(100, int((query.get("limit") or ["20"])[0])))
            pending = [j for j in self.store.list_launch_pending(limit) if not granted_only or self.granted_job_ok(j)]
            return 200, {"pending": [self._claim_payload(j) for j in pending]}
        m = re.match(r"^/jobs/([^/]+)/(launch/[a-z]+|artifact)$", path)
        if not m or not JOB_ID_RE.match(m.group(1)):
            return 404, {"error": "NOT_FOUND"}
        job_id, action = m.group(1), m.group(2)
        if granted_only and not self.granted_job_ok(self.store.get(job_id)):
            return 403, {"error": "REVIEW_ONLY_MODE", "detail": "launch hand-off is disabled while AUTOMATION_MODE=review_only"}
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$", launcher_id):
            return 422, {"error": "LAUNCHER_ID_REQUIRED"}
        if action == "launch/claim" and method == "POST":
            lease_s = max(30, min(3600, int(body.get("lease_s") or 120)))
            verdict, job = self.store.claim_launch(job_id, launcher_id, lease_s)
            if verdict in ("claimed", "reclaimed"):
                return 200, {"claim": verdict, **self._claim_payload(job)}
            if verdict == "busy":
                return 409, {"error": "CLAIMED_BY_OTHER", "launch_owner": job.get("launch_owner"), "lease_until": job.get("lease_until")}
            return 410, {"error": "NOT_CLAIMABLE", "status": job["status"] if job else None, "launch_state": (job or {}).get("launch_state")}
        job = self.store.get(job_id)
        if not job:
            return 404, {"error": "JOB_NOT_FOUND"}
        if action == "artifact" and method == "GET":
            if job.get("launch_owner") != launcher_id or job.get("launch_state") not in ("claimed", "launching"):
                return 403, {"error": "NOT_OWNER"}
            art = ((job.get("launch") or {}).get("artifact") or {})
            ap = Path(str(art.get("path") or ""))
            try:
                owned = self.workspaces.path_for(job_id)
            except Exception:  # noqa: BLE001
                return 404, {"error": "ARTIFACT_NOT_FOUND"}
            if not ap.is_file() or ap.parent != owned or ap.name != "checkout.tar":
                return 404, {"error": "ARTIFACT_NOT_FOUND"}
            return 200, {"__bytes__": ap.read_bytes(), "content_type": "application/x-tar",
                         "headers": {"X-PRQA-Artifact-SHA256": str(art.get("sha256") or ""), "X-PRQA-Artifact-Size": str(art.get("size") or "")}}
        if action == "launch/heartbeat" and method == "POST":
            lease_s = max(30, min(3600, int(body.get("lease_s") or 120)))
            verdict, job = self.store.heartbeat_launch(job_id, launcher_id, lease_s, state=str(body.get("state") or "") or None)
            if verdict == "ok":
                return 200, {"ok": True, "cancel_requested": bool(job.get("cancel")), "lease_until": job.get("lease_until"), "launch_state": job.get("launch_state")}
            if verdict == "terminal":
                return 409, {"error": "JOB_TERMINAL", "status": job["status"], "cancel_requested": bool(job.get("cancel"))}
            return 403, {"error": "NOT_OWNER"}
        if action in ("launch/result", "launch/failed", "launch/cancelled") and method == "POST":
            outcome = {"launch/result": "succeeded", "launch/failed": "failed", "launch/cancelled": "cancelled"}[action]
            cleanup = redact_value(body.get("cleanup") or {}) if isinstance(body.get("cleanup"), dict) else None
            result = body.get("result") if isinstance(body.get("result"), dict) else None
            if outcome == "succeeded":
                check = validate_result(result, self.schemas)
                if not check["valid"]:
                    outcome = "failed"
                    err = {"type": "InvalidResult", "message": "launcher result failed validation: " + "; ".join(check["errors"][:5])}
                else:
                    err = None
            else:
                err = redact_value(body.get("error") or {"type": "LaunchFailed", "message": "no detail"})
            verdict, job = self.store.finish_launch(job_id, launcher_id, outcome, result=result if outcome == "succeeded" else None,
                                                    error=err, cleanup=cleanup)
            if verdict == "rejected":
                return 409, {"error": "NOT_OWNER_OR_NOT_RUNNING", "status": job["status"] if job else None}
            if cleanup is not None:
                self.store.log_event("sandbox_cleanup", cleanup, job_id)
            if verdict == "done":
                self.executor._purge_sources(job_id, job.get("workspace"))
                self.executor._release_workspace(job_id)
            return 200, {"ok": True, "already": verdict == "already", "status": job["status"], "launch_state": job.get("launch_state")}
        return 404, {"error": "NOT_FOUND"}

    def notify_discord(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        kind = str(body.get("kind") or "result")
        mode = "real" if self.discord_real else "mock"
        url = self.discord_webhook_url if mode == "real" else self.policy.endpoints()["discord_mock_url"]
        pipeline = redact_value(body.get("pipeline") or {})
        if kind == "blocked":
            job_seed = body.get("job_seed") or {}
            decision = body.get("decision") or {}
            if not job_seed.get("repository") or not decision.get("reason_code"):
                return 422, {"error": "INVALID_REQUEST", "detail": "kind=blocked needs job_seed.repository and decision.reason_code"}
            delivery_id = str(body.get("delivery_id") or (job_seed.get("trigger") or {}).get("delivery_id") or "")
            if delivery_id:
                claim = self.store.claim_delivery(f"discord-blocked:{delivery_id}"[:100])
                if not claim["claimed"]:
                    return 200, {"kind": kind, "mode": mode, "status": "skipped_duplicate", "delivered": False, "reason": "ALREADY_NOTIFIED"}
            outcome = deliver_messages([build_blocked_message(job_seed, decision, pipeline=pipeline, mode=mode)], self.make_sender(url))
            self.store.log_event("discord_blocked_notice", {"mode": mode, "status": outcome["status"], "repository": job_seed.get("repository"),
                                                            "pr": (job_seed.get("pr") or {}).get("number"), "reason_code": decision.get("reason_code"),
                                                            "results": outcome["results"]})
            return 200, {**outcome, "kind": kind, "mode": mode, "status": f"{mode}_{outcome['status']}"}
        if kind != "result":
            return 422, {"error": "INVALID_REQUEST", "detail": "kind must be result or blocked"}
        job_id = str(body.get("job_id") or "")
        job = self.store.get(job_id) if job_id else None
        if not job:
            return 404, {"error": "JOB_NOT_FOUND"}
        if job["status"] != "succeeded":
            # Accepted ≠ completed: the pipeline must wait for the job before anything is reported.
            return 409, {"error": "JOB_NOT_COMPLETED", "job_status": job["status"], "detail": "reports are rendered only from a succeeded job"}
        result = body.get("result") if isinstance(body.get("result"), dict) else job["result"]
        check = validate_result(result, self.schemas)
        validation = {"valid": check["valid"], "errors": list(check["errors"])}
        if check["valid"]:
            mismatches = []
            if result.get("job_id") != job["id"]:
                mismatches.append("result.job_id does not match the job")
            if result.get("repository") != job["repository"]:
                mismatches.append("result.repository does not match the job")
            if (result.get("pr") or {}).get("head_sha") != job["head_sha"]:
                mismatches.append("result.pr.head_sha does not match the job")
            if mismatches:
                result = inconclusive_placeholder(job, mismatches, reason="RESULT_JOB_MISMATCH")
                validation = {"valid": False, "errors": mismatches}
        else:
            result = inconclusive_placeholder(job, check["errors"])
        claim = self.store.claim_notification(job_id, "discord")
        if not claim["claimed"]:
            return 200, {"kind": kind, "mode": mode, "status": "skipped_duplicate", "delivered": False, "reason": claim["reason"],
                         "overall_status": result["summary"]["overall_status"], "previous": redact_value(claim.get("previous") or {})}
        run_url = ((job.get("request") or {}).get("trigger") or {}).get("run_url")
        messages = build_result_messages(result, validation=validation, pipeline=pipeline, prompt=body.get("prompt"), run_url=run_url, mode=mode)
        outcome = deliver_messages(messages, self.make_sender(url))
        status = f"{mode}_{outcome['status']}"
        self.store.set_notification(job_id, "discord", status, {"mode": mode, "messages": outcome["messages"], "sent": outcome["sent"],
                                                                 "results": outcome["results"], "overall_status": result["summary"]["overall_status"],
                                                                 "validation_valid": validation["valid"], "pipeline": pipeline})
        if not outcome["delivered"]:
            log.warning("discord delivery %s for job %s (%s mode)", outcome["status"], job_id, mode)
        return 200, {**outcome, "kind": kind, "mode": mode, "status": status, "overall_status": result["summary"]["overall_status"],
                     "validation_valid": validation["valid"]}

    def create_job(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        body = dict(body or {})
        grant_token = body.pop("run_grant", None)        # never validated into, stored with or logged from the request
        errors = self.schemas.errors(self.schemas.job_request, body)
        if grant_token is not None and not errors:
            return self._create_granted_job(body, grant_token)
        if errors:
            return 422, {"error": "INVALID_JOB_REQUEST", "errors": errors}
        entry = self.registry.get(body["repository"])
        decision = decide(self.policy, entry, body)
        delivery_id = (body.get("trigger") or {}).get("delivery_id")
        if not decision.allowed:
            self.store.record_rejection(body, decision.to_dict(), delivery_id)
            return 403, {"error": "POLICY_REJECTED", "decision": decision.to_dict(), "policy": self.policy.to_public_dict()["flags"],
                         "automation_mode": self.policy.automation_mode}
        try:
            job = self.store.create(body, execution_mode=decision.execution_mode or "mock",
                                    max_retries=entry.max_retries if entry else 0, delivery_id=delivery_id)
        except DuplicateDelivery as dup:
            return 409, {"error": "DUPLICATE_DELIVERY", "delivery_id": dup.delivery_id, "job_id": dup.job_id}
        except DuplicateJob as dup:
            return 409, {"error": "DUPLICATE_JOB", "existing_job": self._public_job(dup.existing)}
        return 201, {"job": self._public_job(job), "decision": decision.to_dict()}

    def _create_granted_job(self, body: dict[str, Any], token: Any) -> tuple[int, dict[str, Any]]:
        """One-time grant path. Order: signature/structure/expiry → exact binding → the normal decision must fail ONLY on
        GRANT_REPLACEABLE gates → decide_with_grant (every other rule) → atomic consume + create. Every outcome is audited."""
        pr = body.get("pr") or {}
        scope = {"repository": body.get("repository"), "pr_number": pr.get("number"), "head_sha": pr.get("head_sha")}
        grant_id = None
        try:
            claims = run_grants.verify(token, self.run_grant_key, now=int(self.clock()))
            grant_id = claims["grant_id"]
            self.store.grant_audit("validated", {"claims": run_grants.public_claims(claims)}, grant_id=grant_id)
            run_grants.check_binding(claims, body)
            entry = self.registry.get(body["repository"])
            normal = decide(self.policy, entry, body)
            if normal.allowed:
                raise run_grants.GrantError("GRANT_NOT_NEEDED", "policy already allows this job; submit it without a grant")
            if normal.reason_code not in GRANT_REPLACEABLE:
                raise run_grants.GrantError("POLICY_REJECTED", f"{normal.reason_code}: a grant cannot override this rule")
            decision = decide_with_grant(self.policy, entry, body)
            if not decision.allowed:
                raise run_grants.GrantError("POLICY_REJECTED", f"{decision.reason_code}: {decision.reason}")
            job = self.store.create_with_grant(body, claims, delivery_id=(body.get("trigger") or {}).get("delivery_id"))
        except run_grants.GrantError as exc:
            self.store.grant_audit("rejected", {"code": exc.code, "reason": exc.reason, "scope": scope}, grant_id=grant_id)
            log.warning("single-run grant rejected: %s", exc.code)
            return 403, {"error": exc.code, "detail": exc.reason}
        except DuplicateDelivery as dup:
            self.store.grant_audit("rejected", {"code": "DUPLICATE_DELIVERY", "scope": scope}, grant_id=grant_id)
            return 409, {"error": "DUPLICATE_DELIVERY", "delivery_id": dup.delivery_id, "job_id": dup.job_id}
        except DuplicateJob as dup:
            self.store.grant_audit("rejected", {"code": "DUPLICATE_JOB", "scope": scope}, grant_id=grant_id)
            return 409, {"error": "DUPLICATE_JOB", "existing_job": self._public_job(dup.existing)}
        log.info("single-run grant %s consumed by job %s (%s#%s@%s)", grant_id, job["id"], scope["repository"], scope["pr_number"],
                 str(scope["head_sha"])[:12])
        return 201, {"job": self._public_job(job), "decision": decision.to_dict(), "grant_id": grant_id}

    def granted_job_ok(self, job: dict[str, Any] | None) -> bool:
        """A live job may pass the review-only launch/checkout gates only when a consumed, unexpired grant is bound to it
        with exactly the job's repository/PR/head SHA."""
        if not job or job.get("execution_mode") != "live" or job.get("synthetic"):
            return False
        g = self.store.grant_for_job(job["id"])
        return bool(g and g["repository"] == job["repository"] and int(g["pr_number"]) == int(job["pr_number"])
                    and g["head_sha"] == job["head_sha"])

    @staticmethod
    def _public_job(job: dict[str, Any]) -> dict[str, Any]:
        public = dict(job)
        request = dict(public.pop("request"))
        prompt = request.get("prompt") or {}
        request["prompt"] = {k: v for k, v in prompt.items() if k != "text"}
        request["prompt"]["chars"] = len(prompt.get("text", ""))
        public["request"] = request
        return redact_value(public)

    def route(self, method: str, path: str, query: dict[str, list[str]], body: Any, headers: dict[str, str]) -> tuple[int, Any]:
        if method == "GET" and path == "/healthz":
            return 200, {"ok": True, "version": __version__, "automation_mode": self.policy.automation_mode}
        if path.startswith("/launch/") or re.match(r"^/jobs/[^/]+/(launch/[a-z]+|artifact)$", path):
            # Separate credential for the separate process: the worker token (n8n) cannot drive launches and vice versa.
            if not self.launcher_token or not hmac.compare_digest(headers.get("x-launcher-token", ""), self.launcher_token):
                return 401, {"error": "UNAUTHORIZED"}
            return self.launch_route(method, path, query, body or {}, headers)
        if not hmac.compare_digest(headers.get("x-worker-token", ""), self.token):
            return 401, {"error": "UNAUTHORIZED"}
        if method == "GET" and path == "/config":
            return 200, self.config()
        if method == "GET" and path == "/registry":
            return 200, self.registry.summary(self.policy)
        if method == "GET" and path == "/prompts/reference":
            return 200, {"text": self.templates.reference, "sha256": self.templates.reference_sha256,
                         "path": "prompts/reference/Senior_QA_Reference_Prompt.md", "template_version": TEMPLATE_VERSION}
        if method == "POST" and path == "/prompts/build":
            b = body or {}
            gen = self.policy.generation_settings()   # validated at start-up; the same rules the evaluators apply
            request = build_llm_request(self.templates, model=self.policy.endpoints()["prompt_llm_model"],
                                        provider=self.policy.prompt_llm_provider, max_tokens=gen["max_tokens"],
                                        reasoning="on" if gen["reasoning"] else "off",
                                        reasoning_max_tokens=gen["reasoning_max_tokens"],
                                        reasoning_effort=gen["reasoning_effort"],
                                         repository=b["repository"], pr_number=int(b["pr_number"]),
                                         test_level=b["test_level"], synthetic=bool(b.get("synthetic")),
                                         context=b.get("context") or {})
            return 200, {"llm_request": request, "prompt_llm_api_base": self.policy.endpoints()["prompt_llm_api_base"],
                         "prompt_llm_api_mode": self.policy.endpoints()["prompt_llm_api_mode"], "template_version": TEMPLATE_VERSION,
                         "generation": {**gen, "provider": self.policy.prompt_llm_provider, "timeout_s": GENERATION_TIMEOUT_S,
                                        "retries": 0}}
        if method == "POST" and path == "/prompts/finalize":
            b = body or {}
            out = finalize_prompt(self.templates, generated_text=b.get("generated_text"), repository=b["repository"],
                                  pr_number=int(b["pr_number"]), test_level=b["test_level"],
                                  context=b.get("context") or {}, synthetic=bool(b.get("synthetic")),
                                  finish_reason=b.get("finish_reason"), llm_error=b.get("llm_error"),
                                  http_status=b.get("http_status"), response_shape=b.get("response_shape"))
            return 200, out
        if method == "POST" and path == "/context/build":
            b = body or {}
            ctx = build_context(pr=b.get("pr") or {}, files=b.get("files") or [], diff_text=str(b.get("diff") or ""),
                                openapi=b.get("openapi"), repository=b["repository"], synthetic=bool(b.get("synthetic")))
            return 200, {"context": ctx}
        if method == "POST" and path == "/webhook/evaluate":
            b = body or {}
            return 200, evaluate_event(b.get("event"), b.get("payload") or {}, self.schemas,
                                       trigger_label=self.trigger_label, default_level=self.default_test_level, allowed_actors=self.allowed_actors)
        if method == "POST" and path == "/jobs/cancel":
            b = body or {}
            repository = str(b.get("repository") or "")
            if not re.match(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$", repository):
                return 422, {"error": "INVALID_REPOSITORY"}
            try:
                pr_number = int(b.get("pr_number"))
            except (TypeError, ValueError):
                return 422, {"error": "INVALID_PR_NUMBER"}
            except_sha = b.get("except_head_sha") or None
            if except_sha and not re.match(r"^[0-9a-f]{40}$", str(except_sha)):
                return 422, {"error": "INVALID_HEAD_SHA"}
            reason = str(b.get("reason") or "CANCELLED")[:60]
            cancelled = self.store.cancel_active(repository, pr_number, reason=reason, except_head_sha=except_sha,
                                                 detail=redact_value({"event": b.get("event"), "delivery_id": b.get("delivery_id")}))
            self.store.log_event("lifecycle_cancel", {"repository": repository, "pr_number": pr_number, "reason": reason,
                                                      "except_head_sha": except_sha, "cancelled": [j["id"] for j in cancelled]})
            return 200, {"cancelled": [{"id": j["id"], "status": j["status"], "head_sha": j["head_sha"]} for j in cancelled], "reason": reason}
        if method == "GET" and path == "/workspaces":
            items = []
            for entry in sorted(self.workspaces.root.iterdir()):
                marker = self.workspaces.read_marker(entry) if entry.is_dir() else None
                items.append({"name": entry.name, "marked": marker is not None, **({k: marker[k] for k in ("job_id", "repository", "head_sha", "state", "created_at", "checkout") if k in marker} if marker else {})})
            return 200, {"root": str(self.workspaces.root), "retention_hours": self.workspaces.retention_s / 3600.0, "workspaces": items}
        if method == "POST" and path == "/workspaces/cleanup":
            b = body or {}
            max_age = b.get("max_age_s")
            return 200, self.workspaces.cleanup(max_age_s=float(max_age) if max_age is not None else None)
        if method == "POST" and path == "/webhook/verify":
            b = body or {}
            secret = self.env.get("GITHUB_WEBHOOK_SECRET", "")
            raw = base64.b64decode(b.get("body_b64", "") or "")
            return 200, verify_signature(secret, raw, b.get("signature"))
        if method == "POST" and path == "/webhook/actions/intake":
            return self.intake.handle(body or {}).to_response()
        if method == "POST" and path == "/notifications/discord":
            return self.notify_discord(body or {})
        if method == "POST" and path == "/deliveries/claim":
            delivery_id = str((body or {}).get("delivery_id", "")).strip()[:100]
            if not delivery_id:
                return 422, {"error": "DELIVERY_ID_REQUIRED"}
            claim = self.store.claim_delivery(delivery_id)
            return (200 if claim["claimed"] else 409), claim
        if method == "POST" and path == "/jobs":
            return self.create_job(body or {})
        if method == "GET" and path == "/jobs":
            status = (query.get("status") or [None])[0]
            limit = int((query.get("limit") or ["50"])[0])
            return 200, {"jobs": [self._public_job(j) for j in self.store.list(status, limit)], "stats": self.store.stats()}
        m = re.match(r"^/jobs/([^/]+)$", path)
        if m and method == "GET":
            job = self.store.get(m.group(1))
            return (200, {"job": self._public_job(job)}) if job else (404, {"error": "JOB_NOT_FOUND"})
        m = re.match(r"^/jobs/([^/]+)/cancel$", path)
        if m and method == "POST":
            b = body or {}
            job = self.store.cancel(m.group(1), reason=str(b.get("reason") or "CANCELLED")[:60], detail=redact_value(b.get("detail") or {}))
            return (200, {"job": self._public_job(job)}) if job else (404, {"error": "JOB_NOT_FOUND"})
        m = re.match(r"^/jobs/([^/]+)/retry$", path)
        if m and method == "POST":
            job = self.store.manual_retry(m.group(1))
            return (200, {"job": self._public_job(job)}) if job else (404, {"error": "JOB_NOT_FOUND"})
        m = re.match(r"^/jobs/([^/]+)/notification/claim$", path)
        if m and method == "POST":
            b = body or {}
            claim = self.store.claim_notification(m.group(1), str(b.get("channel", "discord"))[:40], ttl_s=int(b.get("ttl_s", 900)))
            if claim["job"] is None:
                return 404, {"error": "JOB_NOT_FOUND"}
            return (200 if claim["claimed"] else 409), {"claimed": claim["claimed"], "reason": claim["reason"],
                                                        "previous": redact_value(claim.get("previous") or {})}
        m = re.match(r"^/jobs/([^/]+)/notification$", path)
        if m and method == "POST":
            b = body or {}
            job = self.store.set_notification(m.group(1), str(b.get("channel", "unknown"))[:40],
                                              str(b.get("status", "unknown"))[:40], redact_value(b.get("detail") or {}))
            return (200, {"job": self._public_job(job)}) if job else (404, {"error": "JOB_NOT_FOUND"})
        if method == "POST" and path == "/results/validate":
            return 200, validate_result((body or {}).get("result"), self.schemas)
        if method == "POST" and path == "/reports/render":
            result = (body or {}).get("result")
            check = validate_result(result, self.schemas)
            if not check["valid"]:
                return 422, {"error": "INVALID_RESULT", "errors": check["errors"]}
            ep = self.policy.endpoints()
            return 200, render_all(result, (body or {}).get("job"), discord_mode=ep["discord_mode"], github_mode=ep["github_comment_mode"])
        if method == "POST" and path == "/events":
            b = body or {}
            self.store.log_event(str(b.get("kind", "event"))[:60], redact_value(b.get("detail") or {}), b.get("job_id"))
            return 202, {"logged": True}
        if method == "GET" and path == "/events":
            limit = int((query.get("limit") or ["100"])[0])
            job_id = (query.get("job_id") or [None])[0]
            return 200, {"events": self.store.events(limit, job_id)}
        return 404, {"error": "NOT_FOUND", "path": path}


class Handler(BaseHTTPRequestHandler):
    app: App  # set by serve()
    server_version = "prqa-worker/" + __version__

    def log_message(self, fmt: str, *args: Any) -> None:  # keep logs compact and token-free
        log.info("%s %s", self.command, self.path.split("?")[0])

    def _send(self, status: int, payload: Any) -> None:
        if isinstance(payload, dict) and "__bytes__" in payload:
            data = payload["__bytes__"]
            self.send_response(status)
            self.send_header("Content-Type", payload.get("content_type", "application/octet-stream"))
            for k, v in (payload.get("headers") or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method: str) -> None:
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._send(413, {"error": "BODY_TOO_LARGE"})
            return
        raw = self.rfile.read(length) if length else b""
        body: Any = None
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send(400, {"error": "INVALID_JSON"})
                return
        headers = {k.lower(): v for k, v in self.headers.items()}
        try:
            status, payload = self.app.route(method, parts.path, parse_qs(parts.query), body, headers)
        except (KeyError, TypeError, ValueError) as exc:
            status, payload = 400, {"error": "BAD_REQUEST", "detail": str(exc)[:300]}
        except Exception as exc:  # noqa: BLE001
            log.exception("unhandled error")
            status, payload = 500, {"error": "INTERNAL_ERROR", "type": type(exc).__name__}
        self._send(status, payload)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")


def serve(bind: str = "0.0.0.0", port: int = 8080) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    app = App()
    Handler.app = app
    app.executor.start()
    httpd = ThreadingHTTPServer((bind, port), Handler)
    log.info("prqa-worker %s listening on %s:%s — AUTOMATION_MODE=%s review_only=%s", __version__, bind, port,
             app.policy.automation_mode, app.policy.review_only)
    try:
        httpd.serve_forever()
    finally:
        app.executor.stop()


if __name__ == "__main__":
    serve(os.environ.get("PRQA_BIND", "0.0.0.0"), int(os.environ.get("PRQA_PORT", "8080")))
