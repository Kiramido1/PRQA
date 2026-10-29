"""SQLite-backed job state: dedupe, state transitions, retries, event log."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Any

ACTIVE_STATES = ("queued", "running")
TERMINAL_STATES = ("succeeded", "failed", "rejected", "cancelled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  dedupe_key TEXT NOT NULL,
  repository TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  head_sha TEXT NOT NULL,
  test_level TEXT NOT NULL,
  synthetic INTEGER NOT NULL,
  execution_mode TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_retries INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  request_json TEXT NOT NULL,
  result_json TEXT,
  error_json TEXT,
  notifications_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(dedupe_key, status);
CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id TEXT PRIMARY KEY,
  job_id TEXT,
  seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  job_id TEXT,
  kind TEXT NOT NULL,
  detail_json TEXT NOT NULL
);
-- single-run grants (run_grants.py): a row exists only once a grant has been CONSUMED by exactly one job
CREATE TABLE IF NOT EXISTS run_grants (
  grant_id TEXT PRIMARY KEY,
  repository TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  head_sha TEXT NOT NULL,
  test_level TEXT NOT NULL,
  issued_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  consumed_at TEXT NOT NULL,
  job_id TEXT NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS run_grants_no_delete BEFORE DELETE ON run_grants BEGIN SELECT RAISE(ABORT, 'run_grants is append-only'); END;
CREATE TRIGGER IF NOT EXISTS run_grants_no_update BEFORE UPDATE ON run_grants BEGIN SELECT RAISE(ABORT, 'run_grants is append-only'); END;
-- immutable audit trail of every grant decision (no token, no key, no secret)
CREATE TABLE IF NOT EXISTS run_grant_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  grant_id TEXT,
  job_id TEXT,
  action TEXT NOT NULL,
  detail_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS run_grant_audit_no_delete BEFORE DELETE ON run_grant_audit BEGIN SELECT RAISE(ABORT, 'run_grant_audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS run_grant_audit_no_update BEFORE UPDATE ON run_grant_audit BEGIN SELECT RAISE(ABORT, 'run_grant_audit is append-only'); END;
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def dedupe_key_for(repository: str, pr_number: int, head_sha: str, test_level: str) -> str:
    return f"{repository}#{pr_number}@{head_sha}:{test_level}"


class DuplicateDelivery(Exception):
    def __init__(self, delivery_id: str, job_id: str | None) -> None:
        super().__init__(f"duplicate delivery {delivery_id}")
        self.delivery_id = delivery_id
        self.job_id = job_id


class DuplicateJob(Exception):
    def __init__(self, existing: dict[str, Any]) -> None:
        super().__init__(f"active job exists: {existing['id']}")
        self.existing = existing


class JobStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """Forward-compatible migrations for databases created by earlier worker versions."""
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
        for name, ddl in (("cancel_json", "TEXT"), ("workspace_json", "TEXT"),
                          ("launch_state", "TEXT"), ("launch_owner", "TEXT"), ("lease_until", "TEXT"),
                          ("launch_attempts", "INTEGER NOT NULL DEFAULT 0"), ("launch_json", "TEXT")):
            if name not in existing:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_launch ON jobs(launch_state, lease_until)")

    # ----------------------------------------------------------- helpers ----
    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
        job = dict(row)
        job["synthetic"] = bool(job["synthetic"])
        job["request"] = json.loads(job.pop("request_json"))
        result_json = job.pop("result_json")
        error_json = job.pop("error_json")
        job["result"] = json.loads(result_json) if result_json else None
        job["error"] = json.loads(error_json) if error_json else None
        job["notifications"] = json.loads(job.pop("notifications_json") or "{}")
        cancel_json = job.pop("cancel_json", None)
        workspace_json = job.pop("workspace_json", None)
        job["cancel"] = json.loads(cancel_json) if cancel_json else None
        job["workspace"] = json.loads(workspace_json) if workspace_json else None
        launch_json = job.pop("launch_json", None)
        job["launch"] = json.loads(launch_json) if launch_json else None
        return job

    def _event(self, job_id: str | None, kind: str, detail: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO events(ts, job_id, kind, detail_json) VALUES (?,?,?,?)",
            (now_iso(), job_id, kind, json.dumps(detail, ensure_ascii=False)[:20000]),
        )

    def log_event(self, kind: str, detail: dict[str, Any], job_id: str | None = None) -> None:
        with self._lock:
            self._event(job_id, kind, detail)

    def events(self, limit: int = 100, job_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if job_id:
                rows = self._conn.execute("SELECT * FROM events WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out

    # --------------------------------------------------------- deliveries ----
    def claim_delivery(self, delivery_id: str) -> dict[str, Any]:
        """First dedupe layer for webhook deliveries (called by the n8n receiver before any work).
        Returns {claimed: True} the first time and {claimed: False, job_id} afterwards."""
        with self._lock:
            row = self._conn.execute("SELECT job_id, seen_at FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
            if row:
                self._event(row["job_id"], "duplicate_delivery", {"delivery_id": delivery_id, "layer": "claim"})
                return {"claimed": False, "delivery_id": delivery_id, "job_id": row["job_id"], "first_seen_at": row["seen_at"]}
            self._conn.execute("INSERT INTO deliveries(delivery_id, job_id, seen_at) VALUES (?,?,?)", (delivery_id, None, now_iso()))
            self._event(None, "delivery_claimed", {"delivery_id": delivery_id})
            return {"claimed": True, "delivery_id": delivery_id}

    # -------------------------------------------------------------- jobs ----
    def create(self, request: dict[str, Any], *, execution_mode: str, max_retries: int,
               delivery_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            job_id = self._create_locked(request, execution_mode=execution_mode, max_retries=max_retries, delivery_id=delivery_id)
            return self.get(job_id)  # type: ignore[return-value]

    def _create_locked(self, request: dict[str, Any], *, execution_mode: str, max_retries: int,
                       delivery_id: str | None = None) -> str:
        pr = request["pr"]
        key = dedupe_key_for(request["repository"], int(pr["number"]), pr["head_sha"], request["test_level"])
        claimed_unbound = False
        if delivery_id:
            row = self._conn.execute("SELECT job_id FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
            if row and row["job_id"]:
                self._event(row["job_id"], "duplicate_delivery", {"delivery_id": delivery_id, "layer": "create"})
                raise DuplicateDelivery(delivery_id, row["job_id"])
            claimed_unbound = row is not None  # claimed by the receiver, not yet bound to a job
        existing = self._conn.execute(
            "SELECT * FROM jobs WHERE dedupe_key=? AND status IN (?,?) ORDER BY created_at DESC LIMIT 1",
            (key, *ACTIVE_STATES),
        ).fetchone()
        if existing:
            job = self._row_to_job(existing)
            self._event(job["id"], "duplicate_job", {"dedupe_key": key, "delivery_id": delivery_id})
            raise DuplicateJob(job)
        ts = now_iso()
        job_id = f"{ts[:10].replace('-', '')}-{uuid.uuid4().hex[:12]}"
        self._conn.execute(
            "INSERT INTO jobs(id, dedupe_key, repository, pr_number, head_sha, test_level, synthetic, execution_mode,"
            " status, attempts, max_retries, created_at, updated_at, request_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, key, request["repository"], int(pr["number"]), pr["head_sha"], request["test_level"],
             1 if request.get("synthetic") else 0, execution_mode, "queued", 0, max_retries, ts, ts,
             json.dumps(request, ensure_ascii=False)),
        )
        if delivery_id and claimed_unbound:
            self._conn.execute("UPDATE deliveries SET job_id=? WHERE delivery_id=?", (job_id, delivery_id))
        elif delivery_id:
            self._conn.execute("INSERT INTO deliveries(delivery_id, job_id, seen_at) VALUES (?,?,?)", (delivery_id, job_id, ts))
        self._event(job_id, "created", {"dedupe_key": key, "execution_mode": execution_mode, "delivery_id": delivery_id})
        return job_id

    # ------------------------------------------------------- single-run grants ----
    def grant_audit(self, action: str, detail: dict[str, Any], *, grant_id: str | None = None, job_id: str | None = None) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO run_grant_audit(ts, grant_id, job_id, action, detail_json) VALUES (?,?,?,?,?)",
                               (now_iso(), grant_id, job_id, action, json.dumps(detail, ensure_ascii=False, sort_keys=True)[:4000]))

    def grant_audit_log(self, grant_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            q = "SELECT * FROM run_grant_audit" + (" WHERE grant_id=?" if grant_id else "") + " ORDER BY id ASC LIMIT ?"
            rows = self._conn.execute(q, ((grant_id, limit) if grant_id else (limit,))).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail_json"])} for r in rows]

    def grant_for_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM run_grants WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def create_with_grant(self, request: dict[str, Any], claims: dict[str, Any], *, delivery_id: str | None = None) -> dict[str, Any]:
        """Consume the grant and create its ONE live job in a single SQLite transaction: a second use of the same grant id
        (replay, concurrent request) hits the primary key and nothing is created. Grant jobs never auto-retry."""
        from .run_grants import GrantError
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if self._conn.execute("SELECT 1 FROM run_grants WHERE grant_id=?", (claims["grant_id"],)).fetchone():
                    raise GrantError("GRANT_ALREADY_CONSUMED", "this grant id was already used")
                job_id = self._create_locked(request, execution_mode="live", max_retries=0, delivery_id=delivery_id)
                self._conn.execute(
                    "INSERT INTO run_grants(grant_id, repository, pr_number, head_sha, test_level, issued_at, expires_at, consumed_at, job_id)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (claims["grant_id"], claims["repository"], claims["pr_number"], claims["head_sha"], claims["test_level"],
                     claims["issued_at"], claims["expires_at"], now_iso(), job_id))
                self._conn.execute("INSERT INTO run_grant_audit(ts, grant_id, job_id, action, detail_json) VALUES (?,?,?,?,?)",
                                   (now_iso(), claims["grant_id"], job_id, "consumed", json.dumps({"claims": claims}, sort_keys=True)))
                self._event(job_id, "grant_consumed", {"grant_id": claims["grant_id"]})
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            return self.get(job_id)  # type: ignore[return-value]

    def _grant_outcome_locked(self, job_id: str, outcome: str) -> None:
        row = self._conn.execute("SELECT grant_id FROM run_grants WHERE job_id=?", (job_id,)).fetchone()
        if row:
            self._conn.execute("INSERT INTO run_grant_audit(ts, grant_id, job_id, action, detail_json) VALUES (?,?,?,?,?)",
                               (now_iso(), row["grant_id"], job_id, "outcome", json.dumps({"outcome": outcome})))

    def record_rejection(self, request: dict[str, Any], decision: dict[str, Any], delivery_id: str | None = None) -> None:
        with self._lock:
            self._event(None, "rejected", {
                "repository": request.get("repository"), "pr_number": (request.get("pr") or {}).get("number"),
                "synthetic": request.get("synthetic"), "decision": decision, "delivery_id": delivery_id,
            })

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def list(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            if status:
                rows = self._conn.execute("SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_job(r) for r in rows]

    def next_queued(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at ASC LIMIT 1").fetchone()
            if not row:
                return None
            ts = now_iso()
            self._conn.execute("UPDATE jobs SET status='running', attempts=attempts+1, started_at=?, updated_at=? WHERE id=?", (ts, ts, row["id"]))
            self._event(row["id"], "started", {"attempt": row["attempts"] + 1})
            return self.get(row["id"])

    def succeed(self, job_id: str, result: dict[str, Any]) -> str:
        """Atomic completion: succeeds only while the job is still running AND no cancellation was requested.
        Returns 'succeeded', 'cancelled' (cancel won: result discarded, job marked cancelled) or 'ignored' (already terminal)."""
        with self._lock:
            ts = now_iso()
            cur = self._conn.execute(
                "UPDATE jobs SET status='succeeded', result_json=?, error_json=NULL, finished_at=?, updated_at=?"
                " WHERE id=? AND status='running' AND cancel_json IS NULL",
                (json.dumps(result, ensure_ascii=False), ts, ts, job_id))
            if cur.rowcount == 1:
                self._event(job_id, "succeeded", {"overall_status": (result.get("summary") or {}).get("overall_status")})
                self._grant_outcome_locked(job_id, "succeeded")
                return "succeeded"
            return self._finish_cancelled_locked(job_id, discarded="result")

    def fail(self, job_id: str, error: dict[str, Any]) -> dict[str, Any]:
        """Mark failed; re-queue automatically while attempts <= max_retries. A cancelled job stays cancelled."""
        with self._lock:
            job = self.get(job_id)
            assert job is not None
            ts = now_iso()
            retry = job["attempts"] <= job["max_retries"]
            status = "queued" if retry else "failed"
            cur = self._conn.execute(
                "UPDATE jobs SET status=?, error_json=?, finished_at=?, updated_at=? WHERE id=? AND status='running' AND cancel_json IS NULL",
                (status, json.dumps(error, ensure_ascii=False), None if retry else ts, ts, job_id))
            if cur.rowcount == 1:
                self._event(job_id, "failed_attempt" if retry else "failed", {"error": error, "will_retry": retry, "attempt": job["attempts"]})
                if not retry:
                    self._grant_outcome_locked(job_id, "failed")
            else:
                self._finish_cancelled_locked(job_id, discarded="error")
            return self.get(job_id)  # type: ignore[return-value]

    def _finish_cancelled_locked(self, job_id: str, *, discarded: str) -> str:
        ts = now_iso()
        cur = self._conn.execute("UPDATE jobs SET status='cancelled', finished_at=?, updated_at=? WHERE id=? AND status='running' AND cancel_json IS NOT NULL",
                                 (ts, ts, job_id))
        if cur.rowcount == 1:
            self._event(job_id, "cancelled", {"while": "running", "outcome_discarded": discarded})
            self._grant_outcome_locked(job_id, "cancelled")
            return "cancelled"
        return "ignored"

    def cancel(self, job_id: str, *, reason: str, detail: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Cancel a job. queued → cancelled at once (never picked up); running → cancel requested, the executor
        discards the outcome when the attempt ends; terminal jobs are left untouched (returned as-is)."""
        with self._lock:
            job = self.get(job_id)
            if not job:
                return None
            record = {"reason": str(reason)[:60], "requested_at": now_iso(), "detail": dict(detail or {})}
            if job["status"] == "queued":
                ts = now_iso()
                self._conn.execute("UPDATE jobs SET status='cancelled', cancel_json=?, finished_at=?, updated_at=? WHERE id=?",
                                   (json.dumps(record, ensure_ascii=False), ts, ts, job_id))
                self._event(job_id, "cancelled", {"reason": record["reason"], "while": "queued"})
            elif job["status"] == "running":
                # conditional: a job that completed a moment ago is terminal and must not be rewritten
                cur = self._conn.execute("UPDATE jobs SET cancel_json=?, updated_at=? WHERE id=? AND status='running' AND cancel_json IS NULL",
                                         (json.dumps(record, ensure_ascii=False), now_iso(), job_id))
                if cur.rowcount == 1:
                    self._event(job_id, "cancel_requested", {"reason": record["reason"], "while": "running"})
            return self.get(job_id)

    def finish_cancelled(self, job_id: str) -> dict[str, Any] | None:
        """Finish a running job whose cancellation was requested (no-op when it already completed)."""
        with self._lock:
            self._finish_cancelled_locked(job_id, discarded="attempt")
            return self.get(job_id)

    def delivery_seen(self, delivery_id: str) -> dict[str, Any] | None:
        """Read-only duplicate probe (lets the receiver answer 'duplicate' without consuming rate-limit capacity)."""
        with self._lock:
            row = self._conn.execute("SELECT job_id, seen_at FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        return {"delivery_id": delivery_id, "job_id": row["job_id"], "first_seen_at": row["seen_at"]} if row else None

    def cancel_active(self, repository: str, pr_number: int, *, reason: str, except_head_sha: str | None = None,
                      detail: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Cancel every queued/running job of repo#pr (optionally keeping jobs for one head SHA)."""
        with self._lock:
            rows = self._conn.execute("SELECT id, head_sha FROM jobs WHERE repository=? AND pr_number=? AND status IN (?,?)",
                                      (repository, int(pr_number), *ACTIVE_STATES)).fetchall()
        out = []
        for row in rows:
            if except_head_sha and row["head_sha"] == except_head_sha:
                continue
            job = self.cancel(row["id"], reason=reason, detail=detail)
            if job:
                out.append(job)
        return out

    def set_workspace(self, job_id: str, info: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute("UPDATE jobs SET workspace_json=?, updated_at=? WHERE id=?",
                               (json.dumps(info, ensure_ascii=False)[:20000], now_iso(), job_id))

    def manual_retry(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self.get(job_id)
            if not job or job["status"] not in ("failed",):
                return job
            ts = now_iso()
            self._conn.execute("UPDATE jobs SET status='queued', max_retries=max_retries+1, updated_at=? WHERE id=?", (ts, job_id))
            self._event(job_id, "manual_retry", {})
            return self.get(job_id)

    def set_notification(self, job_id: str, channel: str, status: str, detail: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            job = self.get(job_id)
            if not job:
                return None
            notes = job["notifications"]
            notes[channel] = {"status": status, "at": now_iso(), "detail": detail}
            self._conn.execute("UPDATE jobs SET notifications_json=?, updated_at=? WHERE id=?",
                               (json.dumps(notes, ensure_ascii=False), now_iso(), job_id))
            self._event(job_id, "notification", {"channel": channel, "status": status})
            return self.get(job_id)

    def claim_notification(self, job_id: str, channel: str, *, ttl_s: int = 900) -> dict[str, Any]:
        """Duplicate-delivery guard for reporting channels. Returns {claimed, job, reason}.
        Not claimed when the channel was already delivered, or a delivery is in flight (status 'sending'
        younger than ttl_s). Otherwise marks the channel 'sending' atomically."""
        with self._lock:
            job = self.get(job_id)
            if not job:
                return {"claimed": False, "job": None, "reason": "JOB_NOT_FOUND"}
            current = (job["notifications"] or {}).get(channel) or {}
            status = str(current.get("status") or "")
            if status.endswith("delivered"):
                return {"claimed": False, "job": job, "reason": "ALREADY_DELIVERED", "previous": current}
            if status == "sending":
                try:
                    started = datetime.fromisoformat(str(current.get("at", "")).replace("Z", "+00:00"))
                    age = (datetime.now(UTC) - started).total_seconds()
                except ValueError:
                    age = float("inf")
                if age < ttl_s:
                    return {"claimed": False, "job": job, "reason": "DELIVERY_IN_FLIGHT", "previous": current}
            notes = job["notifications"]
            notes[channel] = {"status": "sending", "at": now_iso(), "detail": {"previous_status": status or None}}
            self._conn.execute("UPDATE jobs SET notifications_json=?, updated_at=? WHERE id=?",
                               (json.dumps(notes, ensure_ascii=False), now_iso(), job_id))
            self._event(job_id, "notification_claimed", {"channel": channel})
            return {"claimed": True, "job": self.get(job_id), "reason": "CLAIMED"}

    # ---------------------------------------------------- launch lifecycle ----
    # launch_state (live jobs only; QA status stays 'running' throughout):
    #   pending  → claimed (launcher owns a lease) → launching → finished | failed | cancelled
    #   expired leases return to pending (recovery) until LAUNCH_MAX_ATTEMPTS, then the QA job fails.
    LAUNCH_MAX_ATTEMPTS = 2

    def set_launch_pending(self, job_id: str, artifact: dict[str, Any]) -> dict[str, Any] | None:
        """Executor hand-off: the artifact is ready, a launcher may claim the job. Only for running live jobs."""
        with self._lock:
            ts = now_iso()
            cur = self._conn.execute(
                "UPDATE jobs SET launch_state='pending', launch_owner=NULL, lease_until=NULL, launch_json=?, updated_at=?"
                " WHERE id=? AND status='running' AND execution_mode='live' AND cancel_json IS NULL",
                (json.dumps({"artifact": artifact, "pending_since": ts}, ensure_ascii=False), ts, job_id))
            if cur.rowcount == 1:
                self._event(job_id, "launch_pending", {"artifact_sha256": artifact.get("sha256"), "size": artifact.get("size")})
            return self.get(job_id)

    def list_launch_pending(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jobs WHERE launch_state='pending' AND status='running' ORDER BY updated_at ASC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_job(r) for r in rows]

    def claim_launch(self, job_id: str, launcher_id: str, lease_s: int) -> tuple[str, dict[str, Any] | None]:
        """Atomic claim. Returns ('claimed', job) | ('reclaimed', job) for the same owner with a live lease |
        ('busy', job) when another launcher holds an unexpired lease | ('unavailable', job|None) otherwise."""
        with self._lock:
            job = self.get(job_id)
            if not job or job["status"] != "running" or job["execution_mode"] != "live" or job.get("cancel"):
                return "unavailable", job
            now = datetime.now(UTC)
            lease_until = (now + timedelta(seconds=int(lease_s))).isoformat(timespec="seconds").replace("+00:00", "Z")
            state, owner, until = job.get("launch_state"), job.get("launch_owner"), job.get("lease_until")
            live_lease = bool(until) and self._parse(until) > now
            if state in ("claimed", "launching") and live_lease and owner == launcher_id:
                self._conn.execute("UPDATE jobs SET lease_until=?, updated_at=? WHERE id=? AND launch_owner=?", (lease_until, now_iso(), job_id, launcher_id))
                return "reclaimed", self.get(job_id)
            if state in ("claimed", "launching") and live_lease:
                return "busy", job
            if state == "pending" or (state in ("claimed", "launching") and not live_lease):
                attempts = int(job.get("launch_attempts") or 0) + 1
                cur = self._conn.execute(
                    "UPDATE jobs SET launch_state='claimed', launch_owner=?, lease_until=?, launch_attempts=?, updated_at=?"
                    " WHERE id=? AND status='running' AND launch_state=? AND cancel_json IS NULL",
                    (launcher_id, lease_until, attempts, now_iso(), job_id, state))
                if cur.rowcount == 1:
                    self._event(job_id, "launch_claimed", {"launcher_id": launcher_id, "lease_until": lease_until, "attempt": attempts,
                                                           "recovered_from_expired_lease": state != "pending"})
                    return "claimed", self.get(job_id)
            return "unavailable", self.get(job_id)

    def heartbeat_launch(self, job_id: str, launcher_id: str, lease_s: int, *, state: str | None = None) -> tuple[str, dict[str, Any] | None]:
        """Extend the owner's lease (and optionally move claimed → launching). Returns ('ok'|'not_owner'|'terminal', job)."""
        with self._lock:
            job = self.get(job_id)
            if not job:
                return "not_owner", None
            if job["status"] != "running":
                return "terminal", job
            if job.get("launch_owner") != launcher_id or job.get("launch_state") not in ("claimed", "launching"):
                return "not_owner", job
            lease_until = (datetime.now(UTC) + timedelta(seconds=int(lease_s))).isoformat(timespec="seconds").replace("+00:00", "Z")
            if state == "launching":
                self._conn.execute("UPDATE jobs SET launch_state='launching', lease_until=?, updated_at=? WHERE id=? AND launch_owner=?", (lease_until, now_iso(), job_id, launcher_id))
            else:
                self._conn.execute("UPDATE jobs SET lease_until=?, updated_at=? WHERE id=? AND launch_owner=?", (lease_until, now_iso(), job_id, launcher_id))
            return "ok", self.get(job_id)

    def finish_launch(self, job_id: str, launcher_id: str, outcome: str, *, result: dict[str, Any] | None = None,
                      error: dict[str, Any] | None = None, cleanup: dict[str, Any] | None = None) -> tuple[str, dict[str, Any] | None]:
        """Terminal hand-off from the owning launcher. outcome: 'succeeded' | 'failed' | 'cancelled'.
        Idempotent: a repeat from the same owner on an already-terminal job returns ('already', job); a foreign launcher
        or a job that is not running returns ('rejected', job). QA-level atomic rules (cancel wins) are reused."""
        with self._lock:
            job = self.get(job_id)
            if not job:
                return "rejected", None
            if job.get("launch_owner") != launcher_id:
                return "rejected", job
            if job["status"] != "running":
                return ("already" if job.get("launch_state") in ("finished", "failed", "cancelled") else "rejected"), job
            launch_info = dict(job.get("launch") or {})
            if cleanup is not None:
                launch_info["cleanup"] = cleanup
            launch_info["finished_at"] = now_iso()
            if outcome == "succeeded" and result is not None:
                verdict = self.succeed(job_id, result)                     # atomic; 'cancelled' when a cancel raced ahead
                new_state = "finished" if verdict == "succeeded" else "cancelled"
            elif outcome == "cancelled":
                self._finish_cancelled_locked(job_id, discarded="launcher_cancel")
                new_state = "cancelled"
                if self.get(job_id)["status"] == "running":              # cancel was never requested: treat as a failure
                    self.fail(job_id, error or {"type": "SandboxCancelled", "message": "launcher reported cancellation without a cancel request"})
                    new_state = "failed"
            else:
                self.fail(job_id, error or {"type": "LaunchFailed", "message": "launcher reported failure"})
                new_state = "cancelled" if self.get(job_id)["status"] == "cancelled" else "failed"
            self._conn.execute("UPDATE jobs SET launch_state=?, launch_json=?, updated_at=? WHERE id=?",
                               (new_state, json.dumps(launch_info, ensure_ascii=False)[:20000], now_iso(), job_id))
            self._event(job_id, "launch_" + new_state, {"launcher_id": launcher_id, "cleanup_ok": (cleanup or {}).get("ok")})
            return "done", self.get(job_id)

    def sweep_expired_leases(self, *, max_attempts: int = LAUNCH_MAX_ATTEMPTS) -> list[dict[str, Any]]:
        """Recovery: expired claimed/launching leases go back to pending (the next claimant purges leftovers) or, past
        max_attempts, the QA job fails. Idempotent; safe to call on every executor tick and at startup."""
        swept = []
        with self._lock:
            now = now_iso()
            rows = self._conn.execute("SELECT id, launch_owner, launch_attempts FROM jobs WHERE status='running' AND launch_state IN ('claimed','launching')"
                                      " AND lease_until IS NOT NULL AND lease_until < ?", (now,)).fetchall()
            for row in rows:
                attempts = int(row["launch_attempts"] or 0)
                if attempts >= max_attempts:
                    # terminal on purpose: a QA-level retry would only hand the same artifact to the same dead launchers again
                    error = {"type": "LaunchLeaseExpired", "message": f"launcher lease expired {attempts} time(s); giving up"}
                    self._conn.execute("UPDATE jobs SET launch_state='failed', launch_owner=NULL, lease_until=NULL, updated_at=? WHERE id=?", (now, row["id"]))
                    cur = self._conn.execute("UPDATE jobs SET status='failed', error_json=?, finished_at=?, updated_at=? WHERE id=? AND status='running' AND cancel_json IS NULL",
                                             (json.dumps(error, ensure_ascii=False), now, now, row["id"]))
                    if cur.rowcount == 1:
                        self._event(row["id"], "failed", {"error": error, "will_retry": False, "attempt": attempts})
                    else:
                        self._finish_cancelled_locked(row["id"], discarded="lease_expired")
                    swept.append({"id": row["id"], "action": "failed", "attempts": attempts})
                else:
                    self._conn.execute("UPDATE jobs SET launch_state='pending', launch_owner=NULL, lease_until=NULL, updated_at=? WHERE id=?", (now, row["id"]))
                    self._event(row["id"], "launch_lease_expired", {"previous_owner": row["launch_owner"], "attempts": attempts})
                    swept.append({"id": row["id"], "action": "re-pending", "attempts": attempts, "previous_owner": row["launch_owner"]})
        return swept

    def recover_worker_restart(self) -> list[dict[str, Any]]:
        """At worker start: mock jobs left 'running' by a dead worker process are re-queued (they run in-process and
        cannot survive a restart). Live jobs are left to the launch lease machinery."""
        recovered = []
        with self._lock:
            rows = self._conn.execute("SELECT id, attempts, max_retries FROM jobs WHERE status='running' AND execution_mode='mock'").fetchall()
            for row in rows:
                ts = now_iso()
                self._conn.execute("UPDATE jobs SET status='queued', updated_at=? WHERE id=? AND status='running'", (ts, row["id"]))
                self._event(row["id"], "requeued_after_worker_restart", {"attempt": row["attempts"]})
                recovered.append(row["id"])
        return recovered

    @staticmethod
    def _parse(ts: str) -> datetime:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}
