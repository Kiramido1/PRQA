"""Job execution: mock executor for synthetic jobs, live sandbox for approved real jobs."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from . import opencode_runner
from .jobs import JobStore
from .policy import Policy
from .redaction import redact_text
from .registry import Registry
from .validation import SchemaStore, validate_result
from .workspace import (ARTIFACT_NAME, CheckoutError, WorkspaceError, WorkspaceManager, build_checkout_plan,  # noqa: F401
                        pack_checkout_artifact, run_checkout)

log = logging.getLogger("prqa.executor")


LOCAL_MIRROR_ROOT = "file:///data/mirrors"


def validate_local_mirror_base(value: str) -> str:
    """PRQA_CHECKOUT_REMOTE_BASE: empty (GitHub over https, the default) or a local bare-mirror directory under
    /data/mirrors inside the worker volume (simulation without a GitHub credential). Nothing else is accepted."""
    value = (value or "").strip().rstrip("/")
    if not value:
        return ""
    if not re.match(r"^file:///data/mirrors(/[A-Za-z0-9._-]+)*$", value) or "/.." in value or "/./" in value + "/":
        raise ValueError("PRQA_CHECKOUT_REMOTE_BASE must be empty or file:///data/mirrors[/<dir>]")
    return value


class Executor:
    def __init__(self, store: JobStore, policy: Policy, registry: Registry, schemas: SchemaStore,
                 fixtures_dir: str | Path, *, mock_delay_s: float = 2.0, workspaces: WorkspaceManager | None = None,
                 worker_checkout_enabled: bool = False, checkout_timeout_s: int = 300, github_token: str = "",
                 max_checkout_mb: float = 500.0, checkout_remote_base: str = "") -> None:
        self.store = store
        self.policy = policy
        self.registry = registry
        self.schemas = schemas
        self.fixtures_dir = Path(fixtures_dir)
        self.mock_delay_s = mock_delay_s
        self.workspaces = workspaces
        self.worker_checkout_enabled = bool(worker_checkout_enabled)
        self.checkout_timeout_s = int(checkout_timeout_s)
        self._github_token = github_token
        self.max_checkout_mb = float(max_checkout_mb)
        self.checkout_remote_base = validate_local_mirror_base(checkout_remote_base)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle --
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="prqa-executor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _maybe_sweep(self, interval_s: float = 5.0) -> None:
        now = time.monotonic()
        if now - getattr(self, "_last_sweep", 0.0) >= interval_s:
            self._last_sweep = now
            for item in self.store.sweep_expired_leases():
                log.warning("launch lease recovery for job %s: %s (attempts=%s)", item["id"], item["action"], item["attempts"])

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._maybe_sweep()
            job = self.store.next_queued()
            if not job:
                self._stop.wait(0.5)
                continue
            try:
                if job["execution_mode"] == "live":
                    self.handoff_live(job)        # prepare + launch_state=pending; the launcher does the rest
                    continue
                result = self.execute(job)
                check = validate_result(result, self.schemas)
                if not check["valid"]:
                    raise ValueError("executor produced an invalid result: " + "; ".join(check["errors"][:5]))
                # Atomic: succeeds only while still running with no cancellation requested; otherwise the store
                # marks the job cancelled and the computed result is discarded (never reported).
                outcome = self.store.succeed(job["id"], result)
                if outcome != "succeeded":
                    log.info("job %s not completed: %s (result discarded)", job["id"], outcome)
            except Exception as exc:  # noqa: BLE001 — every failure is recorded, never raised out of the loop
                log.exception("job %s failed", job["id"])
                self.store.fail(job["id"], {"type": type(exc).__name__, "message": redact_text(str(exc))[:2000]})
            finally:
                if job["execution_mode"] != "live":
                    self._release_workspace(job["id"])
            self._maybe_sweep()

    # ------------------------------------------------------------ workspace --
    def prepare_workspace(self, job: dict[str, Any]) -> dict[str, Any] | None:
        """Isolated per-job directory + pinned checkout plan. The checkout itself runs only for live jobs
        when PRQA_WORKER_CHECKOUT_ENABLED=true (never in review-only); mock jobs only record the plan."""
        if self.workspaces is None:
            return None
        pr = job["request"]["pr"]
        path = self.workspaces.create(job["id"], repository=job["repository"], head_sha=pr["head_sha"], execution_mode=job["execution_mode"])
        remote = {"remote_base": self.checkout_remote_base, "allowed_protocols": ("file",)} if self.checkout_remote_base else {}
        plan = build_checkout_plan(repository=job["repository"], head_sha=pr["head_sha"], workdir=path / "repo", timeout_s=self.checkout_timeout_s, **remote)
        info: dict[str, Any] = {"path": str(path), "repo_dir": plan.workdir, "head_sha": plan.head_sha, "remote_url": plan.remote_url,
                                "plan_steps": plan.step_names, "checkout": "planned"}
        (path / "checkout-plan.json").write_text(json.dumps(plan.to_dict(), indent=1), encoding="utf-8")
        granted = job["execution_mode"] == "live" and self.grant_valid_for_start(job)
        if job["execution_mode"] == "live" and (self.worker_checkout_enabled or granted):
            if self.policy.review_only and not granted:
                raise opencode_runner.PolicyViolation("worker-side checkout requested while review-only")
            outcome = run_checkout(plan, token=self._github_token or None, max_checkout_mb=self.max_checkout_mb)
            # The sandbox never sees .git, the remote or any credential: it gets this per-job artifact only.
            prompt_text = str(((job.get("request") or {}).get("prompt") or {}).get("text") or "")
            manifest = pack_checkout_artifact(repo_dir=plan.workdir, out_path=path / ARTIFACT_NAME, job_id=job["id"],
                                              repository=job["repository"], head_sha=pr["head_sha"], prompt_text=prompt_text,
                                              max_mb=self.max_checkout_mb,
                                              known_secrets={"github_token": self._github_token} if self._github_token else None)
            info.update({"checkout": "done", "checked_out_head": outcome["head"], "duration_ms": outcome["duration_ms"],
                         "size_bytes": outcome["size_bytes"], "artifact": str(path / ARTIFACT_NAME),
                         "artifact_files": manifest["files"], "tree_sha256": manifest["tree_sha256"], "prompt_sha256": manifest["prompt_sha256"]})
        self.workspaces.mark(path, state="in_use", checkout=info["checkout"])
        self.store.set_workspace(job["id"], info)
        return info

    def _purge_sources(self, job_id: str, workspace: dict[str, Any] | None) -> None:
        if self.workspaces is None:
            return
        try:
            path = self.workspaces.path_for(job_id)
            if path.is_dir():
                diag = {"job_id": job_id, "checkout": (workspace or {}).get("checkout"), "head_sha": (workspace or {}).get("head_sha"),
                        "tree_sha256": (workspace or {}).get("tree_sha256"), "artifact_files": (workspace or {}).get("artifact_files"),
                        "purged_at": datetime.now(UTC).isoformat(timespec="seconds")}
                (path / "diagnostics.json").write_text(json.dumps(diag, indent=1), encoding="utf-8")
                self.workspaces.purge_sources(job_id)
        except WorkspaceError as exc:
            log.warning("source purge skipped for %s: %s", job_id, exc)

    def _release_workspace(self, job_id: str) -> None:
        if self.workspaces is None:
            return
        try:
            path = self.workspaces.path_for(job_id)
            if path.is_dir():
                self.workspaces.mark(path, state="released", released_at=datetime.now(UTC).isoformat(timespec="seconds"))
            swept = self.workspaces.cleanup()
            if swept["removed"]:
                log.info("workspace retention sweep removed %s", swept["removed"])
        except WorkspaceError as exc:
            log.warning("workspace release skipped for %s: %s", job_id, exc)

    # ----------------------------------------------------- single-run grants --
    def grant_valid_for_start(self, job: dict[str, Any]) -> bool:
        """Re-validated immediately before the checkout/hand-off: a consumed grant bound to exactly this job's
        repository/PR/head SHA and not yet expired. Audited either way."""
        g = self.store.grant_for_job(job["id"])
        if not g:
            return False
        bound = g["repository"] == job["repository"] and int(g["pr_number"]) == int(job["pr_number"]) and g["head_sha"] == job["head_sha"]
        fresh = time.time() < int(g["expires_at"])
        self.store.grant_audit("start_validated" if bound and fresh else "start_refused",
                               {"bound": bound, "expired": not fresh}, grant_id=g["grant_id"], job_id=job["id"])
        if not bound or not fresh:
            raise opencode_runner.PolicyViolation("single-run grant expired or no longer bound to this job; authorize again")
        return True

    # ------------------------------------------------------------- hand-off --
    def handoff_live(self, job: dict[str, Any]) -> dict[str, Any]:
        """Live jobs: checkout + pack in the worker (credentialed, trusted), then hand the artifact to the launcher via
        launch_state=pending. The worker NEVER launches containers. Any failure here fails the job normally."""
        try:
            workspace = self.prepare_workspace(job)
            if not workspace or workspace.get("checkout") != "done" or not workspace.get("artifact"):
                raise opencode_runner.RunnerUnavailable("live job without a packed artifact: enable PRQA_WORKER_CHECKOUT_ENABLED (Phase 2)")
            ap = Path(workspace["artifact"])
            digest = hashlib.sha256(ap.read_bytes()).hexdigest()
            meta = {"path": str(ap), "size": ap.stat().st_size, "sha256": digest, "tree_sha256": workspace["tree_sha256"],
                    "prompt_sha256": workspace["prompt_sha256"], "files": workspace.get("artifact_files")}
            job_after = self.store.set_launch_pending(job["id"], meta)
            if not job_after or job_after.get("launch_state") != "pending":
                raise RuntimeError("could not mark the job launch-pending (cancelled or not running)")
            log.info("job %s handed to the launcher (artifact %s bytes, sha256 %s…)", job["id"], meta["size"], digest[:12])
            return meta
        except Exception as exc:  # noqa: BLE001
            log.error("hand-off for job %s failed: %s: %s", job["id"], type(exc).__name__, redact_text(str(exc))[:500])   # no raw traceback: may carry tokens
            self.store.fail(job["id"], {"type": type(exc).__name__, "message": redact_text(str(exc))[:2000]})
            self._purge_sources(job["id"], None)
            self._release_workspace(job["id"])
            raise

    # ------------------------------------------------------------ execution --
    def execute(self, job: dict[str, Any]) -> dict[str, Any]:
        entry = self.registry.get(job["repository"])
        self.prepare_workspace(job)
        if job["execution_mode"] == "mock":
            return self._execute_mock(job, entry)
        if job["execution_mode"] == "live":
            raise opencode_runner.RunnerUnavailable("live jobs are not executed in the worker process; use handoff_live()")
        raise ValueError(f"unknown execution_mode {job['execution_mode']!r}")

    def _execute_mock(self, job: dict[str, Any], entry: Any) -> dict[str, Any]:
        if not job["synthetic"]:
            raise opencode_runner.PolicyViolation("mock executor refuses non-synthetic jobs")
        fixture_rel = (entry.fixtures if entry else {}).get("result") or "synthetic/opencode-result.synthetic.json"
        fixture_path = self.fixtures_dir / fixture_rel
        result = json.loads(fixture_path.read_text(encoding="utf-8"))
        started = datetime.now(UTC)
        time.sleep(min(self.mock_delay_s, 30))
        finished = datetime.now(UTC)
        result = copy.deepcopy(result)
        pr = job["request"]["pr"]
        result["synthetic"] = True
        result["job_id"] = job["id"]
        result["repository"] = job["repository"]
        result["pr"]["number"] = int(pr["number"])
        result["pr"]["head_sha"] = pr["head_sha"]
        result["pr"]["title"] = pr.get("title", result["pr"].get("title", ""))
        if pr.get("url"):
            result["pr"]["url"] = str(pr["url"])[:500]
        result["executed_by"].update({
            "engine": "mock_executor", "mode": "mock",
            "prompt_sha256": job["request"]["prompt"]["sha256"],
            "prompt_source": job["request"]["prompt"]["source"],
            "started_at": started.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "finished_at": finished.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "duration_ms": int((finished - started).total_seconds() * 1000),
        })
        return result
