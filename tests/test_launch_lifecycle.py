"""Stage 1 — worker launch lifecycle: launch_state, atomic claims with leases, hand-off endpoints, recovery.
SYNTHETIC: in-process App, temporary SQLite store, no Docker, no network.

Run: cd "<project>" && PYTHONPATH=worker python3 -m unittest tests.test_launch_lifecycle -v
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, UTC
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from prqa_worker import opencode_runner  # noqa: E402
from prqa_worker.jobs import JobStore  # noqa: E402
from prqa_worker.server import App  # noqa: E402

FIX = ROOT / "fixtures" / "synthetic"
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
PROMPT = "# PRQA prompt\n" + ("Test the orders API with curl against $BASE_URL using $TOKEN. " * 8)
LT = "launcher-token-SYNTHETIC-0123456789abcdef0123456789"
WT = "worker-token-SYNTHETIC-0123456789abcdef"


def live_env(tmp: str, **extra) -> dict:
    """A LIVE-mode worker with the placeholder repository approved — only ever used inside these tests."""
    text = (ROOT / "config/repositories.yaml").read_text(encoding="utf-8").replace(
        "    kind: real\n    enabled: false\n    approval_status: pending_approval", "    kind: real\n    enabled: true\n    approval_status: approved")
    reg = Path(tmp) / "registry.yaml"; reg.write_text(text)
    env = {"AUTOMATION_MODE": "live", "REAL_PROJECT_TESTING_ENABLED": "true", "AUTO_EXECUTION_ENABLED": "true",
           "MOCK_SERVICES_URL": "http://prqa-mocks:8090", "PRQA_WORKER_TOKEN": WT, "PRQA_LAUNCHER_TOKEN": LT,
           "PRQA_REGISTRY_PATH": str(reg), "PRQA_SCHEMA_DIR": str(ROOT / "schemas"), "PRQA_PROMPTS_DIR": str(ROOT / "prompts"),
           "PRQA_FIXTURES_DIR": str(ROOT / "fixtures"), "PRQA_DATA_DIR": tmp, "MOCK_EXECUTION_DELAY_S": "0",
           "PRQA_WORKSPACE_ROOT": tmp + "/workspaces", "PRQA_WORKSPACE_MIN_FREE_MB": "0", "PRQA_WORKER_CHECKOUT_ENABLED": "true"}
    env.update(extra)
    return env


def live_job_request(delivery="l-1", sha=SHA):
    return {"synthetic": False, "repository": "your-org/your-first-real-repo",
            "pr": {"number": 7, "head_sha": sha, "base_sha": "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432", "title": "Real PR", "is_fork": False,
                   "url": "https://github.com/your-org/your-first-real-repo/pull/7"},
            "test_level": "standard", "trigger": {"source": "github_webhook", "delivery_id": delivery, "label": "qa-test:standard"},
            "prompt": {"text": PROMPT, "source": "reference_fallback", "sha256": hashlib.sha256(PROMPT.encode()).hexdigest()}}


class _FakeCheckout:
    """Replaces the real git checkout with a synthetic tree so the executor's hand-off path runs offline."""

    def __init__(self):
        self.calls = 0

    def __call__(self, plan, *, token=None, max_checkout_mb=500.0, **kw):
        self.calls += 1
        wd = Path(plan.workdir); (wd / "src").mkdir(parents=True, exist_ok=True)
        (wd / "src" / "app.py").write_text("print('hi')\n"); (wd / "README.md").write_text("# synthetic\n")
        return {"head": plan.head_sha, "steps": 5, "duration_ms": 1, "remote_url": plan.remote_url, "size_bytes": 20}


class LiveWorkerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(live_env(self.tmp.name))
        import prqa_worker.executor as ex
        self._orig = ex.run_checkout
        self.fake_checkout = _FakeCheckout()
        ex.run_checkout = self.fake_checkout

    def tearDown(self):
        import prqa_worker.executor as ex
        ex.run_checkout = self._orig
        self.app.executor.stop()
        self.tmp.cleanup()

    def worker(self, method, path, body=None):
        return self.app.route(method, path, {}, body, {"x-worker-token": WT})

    def stub_discord(self):
        from prqa_worker.discord import DiscordSender
        from tests.test_discord_reporting import FakeDiscord
        fake = FakeDiscord([204, 204, 204])
        self.app.make_sender = lambda url: DiscordSender(url, opener=fake, sleep=lambda s: None, max_attempts=3, backoff_s=0)
        return fake

    def launcher(self, method, path, body=None, *, token=LT, launcher_id="launcher-A"):
        return self.app.route(method, path, {}, body, {"x-launcher-token": token, "x-launcher-id": launcher_id})

    def create_and_handoff(self, delivery="l-1", sha=SHA) -> str:
        status, body = self.worker("POST", "/jobs", live_job_request(delivery, sha))
        self.assertEqual(status, 201, body)
        job_id = body["job"]["id"]
        running = self.app.store.next_queued()
        self.assertEqual(running["id"], job_id)
        self.app.executor.handoff_live(running)
        return job_id


class HandoffTests(LiveWorkerBase):
    def test_executor_hands_off_instead_of_launching(self):
        job_id = self.create_and_handoff()
        job = self.app.store.get(job_id)
        self.assertEqual((job["status"], job["launch_state"]), ("running", "pending"))
        art = job["launch"]["artifact"]
        self.assertTrue(Path(art["path"]).is_file()); self.assertEqual(art["size"], Path(art["path"]).stat().st_size)
        self.assertEqual(art["sha256"], hashlib.sha256(Path(art["path"]).read_bytes()).hexdigest())
        self.assertEqual(len(art["tree_sha256"]), 64); self.assertEqual(len(art["prompt_sha256"]), 64)
        self.assertEqual(self.fake_checkout.calls, 1)
        kinds = [e["kind"] for e in self.app.store.events(job_id=job_id)]
        self.assertIn("launch_pending", kinds)
        # the worker process never launches: run_live is a dead end even when called directly
        entry = self.app.registry.get(job["repository"])
        with self.assertRaises(opencode_runner.RunnerUnavailable) as ctx:
            opencode_runner.run_live(job, entry, self.app.policy, artifact_path=art["path"], expected_hashes=art)
        self.assertIn("in-process launch is disabled", str(ctx.exception))
        with self.assertRaises(opencode_runner.RunnerUnavailable):
            self.app.executor.execute(job)

    def test_executor_loop_leaves_live_job_pending_and_keeps_artifact(self):
        status, body = self.worker("POST", "/jobs", live_job_request("loop-1"))
        job_id = body["job"]["id"]
        self.app.executor.start()
        try:
            import time
            for _ in range(50):
                if (self.app.store.get(job_id) or {}).get("launch_state") == "pending":
                    break
                time.sleep(0.1)
        finally:
            self.app.executor.stop()
        job = self.app.store.get(job_id)
        self.assertEqual((job["status"], job["launch_state"]), ("running", "pending"))
        self.assertTrue(Path(job["launch"]["artifact"]["path"]).is_file(), "artifact kept for the launcher")

    def test_handoff_failure_fails_the_job(self):
        import prqa_worker.executor as ex
        ex.run_checkout = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git exploded ghp_SYNTHETICCANARY0000000000000000000000000"))
        status, body = self.worker("POST", "/jobs", live_job_request("fail-1"))
        job_id = body["job"]["id"]
        running = self.app.store.next_queued()
        with self.assertRaises(RuntimeError):
            self.app.executor.handoff_live(running)
        job = self.app.store.get(job_id)
        self.assertIn(job["status"], ("queued", "failed"))       # re-queued within max_retries or failed
        self.assertNotIn("SYNTHETICCANARY", json.dumps(job))

    def test_review_only_worker_has_inert_handoff(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            app = App(live_env(tmp.name, AUTOMATION_MODE="review_only"))
            try:
                status, body = app.route("GET", "/launch/pending", {}, None, {"x-launcher-token": LT})
                self.assertEqual((status, body["error"]), (403, "REVIEW_ONLY_MODE"))
                self.assertEqual(app.config()["launch"]["in_process_launch"], False)
                # and no live job can even be created
                status, body = app.route("POST", "/jobs", {}, live_job_request("ro-1"), {"x-worker-token": WT})
                self.assertEqual(status, 403)
            finally:
                app.executor.stop()
        finally:
            tmp.cleanup()


class ClaimLeaseTests(LiveWorkerBase):
    def test_auth_separation(self):
        self.create_and_handoff()
        self.assertEqual(self.launcher("GET", "/launch/pending", token=WT)[0], 401, "worker token cannot drive launches")
        self.assertEqual(self.worker("GET", "/launch/pending")[0], 401)
        status, body = self.launcher("GET", "/launch/pending")
        self.assertEqual(status, 200); self.assertEqual(len(body["pending"]), 1)
        tmp = tempfile.TemporaryDirectory()
        try:
            app = App(live_env(tmp.name, PRQA_LAUNCHER_TOKEN=""))
            try:
                self.assertEqual(app.route("GET", "/launch/pending", {}, None, {"x-launcher-token": ""})[0], 401, "no token configured → closed")
            finally:
                app.executor.stop()
        finally:
            tmp.cleanup()

    def test_claim_is_atomic_and_exclusive(self):
        job_id = self.create_and_handoff()
        s1, c1 = self.launcher("POST", f"/jobs/{job_id}/launch/claim", {"launcher_id": "launcher-A", "lease_s": 120})
        self.assertEqual(s1, 200, c1); self.assertEqual(c1["claim"], "claimed")
        for key in ("job_id", "repository", "pr", "model", "max_runtime_minutes", "expected_hashes", "artifact"):
            self.assertIn(key, c1)
        self.assertEqual(c1["pr"]["head_sha"], SHA); self.assertEqual(c1["model"], self.app.policy.opencode_model)
        s2, c2 = self.launcher("POST", f"/jobs/{job_id}/launch/claim", {"launcher_id": "launcher-B", "lease_s": 120}, launcher_id="launcher-B")
        self.assertEqual((s2, c2["error"]), (409, "CLAIMED_BY_OTHER"))
        s3, c3 = self.launcher("POST", f"/jobs/{job_id}/launch/claim", {"launcher_id": "launcher-A", "lease_s": 120})
        self.assertEqual((s3, c3["claim"]), (200, "reclaimed"))
        self.assertEqual(self.launcher("GET", "/launch/pending")[1]["pending"], [], "claimed job is no longer pending")

    def test_two_launchers_race_for_the_same_job(self):
        job_id = self.create_and_handoff()
        results = {}
        barrier = threading.Barrier(2)

        def claim(name):
            barrier.wait()
            results[name] = self.app.store.claim_launch(job_id, name, 120)[0]
        t = [threading.Thread(target=claim, args=(n,)) for n in ("launcher-A", "launcher-B")]
        [x.start() for x in t]; [x.join() for x in t]
        self.assertEqual(sorted(results.values()), ["busy", "claimed"])
        self.assertEqual(self.app.store.get(job_id)["launch_owner"], next(k for k, v in results.items() if v == "claimed"))

    def test_artifact_download_owner_only_with_metadata(self):
        job_id = self.create_and_handoff()
        self.assertEqual(self.launcher("GET", f"/jobs/{job_id}/artifact")[0], 403, "unclaimed → not owner")
        self.launcher("POST", f"/jobs/{job_id}/launch/claim", {"launcher_id": "launcher-A"})
        self.assertEqual(self.launcher("GET", f"/jobs/{job_id}/artifact", launcher_id="launcher-B")[0], 403)
        status, body = self.launcher("GET", f"/jobs/{job_id}/artifact")
        self.assertEqual(status, 200)
        data = body["__bytes__"]
        self.assertEqual(hashlib.sha256(data).hexdigest(), body["headers"]["X-PRQA-Artifact-SHA256"])
        self.assertEqual(str(len(data)), body["headers"]["X-PRQA-Artifact-Size"])

    def test_heartbeat_carries_cancellation_and_rejects_foreign_launcher(self):
        job_id = self.create_and_handoff()
        self.launcher("POST", f"/jobs/{job_id}/launch/claim", {"launcher_id": "launcher-A"})
        status, body = self.launcher("POST", f"/jobs/{job_id}/launch/heartbeat", {"launcher_id": "launcher-A", "state": "launching"})
        self.assertEqual((status, body["cancel_requested"], body["launch_state"]), (200, False, "launching"))
        self.assertEqual(self.launcher("POST", f"/jobs/{job_id}/launch/heartbeat", {"launcher_id": "launcher-B"}, launcher_id="launcher-B")[0], 403)
        self.worker("POST", f"/jobs/{job_id}/cancel", {"reason": "LABEL_REMOVED"})
        status, body = self.launcher("POST", f"/jobs/{job_id}/launch/heartbeat", {"launcher_id": "launcher-A"})
        self.assertEqual((status, body["cancel_requested"]), (200, True))

    def test_expired_lease_recovers_to_pending_then_fails_after_max_attempts(self):
        job_id = self.create_and_handoff()
        self.launcher("POST", f"/jobs/{job_id}/launch/claim", {"launcher_id": "launcher-A", "lease_s": 30})
        past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat(timespec="seconds").replace("+00:00", "Z")
        self.app.store._conn.execute("UPDATE jobs SET lease_until=? WHERE id=?", (past, job_id))
        swept = self.app.store.sweep_expired_leases()
        self.assertEqual(swept[0]["action"], "re-pending"); self.assertEqual(self.app.store.get(job_id)["launch_state"], "pending")
        self.assertIsNone(self.app.store.get(job_id)["launch_owner"])
        self.assertEqual(self.app.store.sweep_expired_leases(), [], "idempotent")
        # a different launcher can now claim (attempt 2) — recovery after a dead launcher
        status, body = self.launcher("POST", f"/jobs/{job_id}/launch/claim", {"launcher_id": "launcher-B", "lease_s": 30}, launcher_id="launcher-B")
        self.assertEqual((status, body["launch_attempts"]), (200, 2))
        self.app.store._conn.execute("UPDATE jobs SET lease_until=? WHERE id=?", (past, job_id))
        swept = self.app.store.sweep_expired_leases()
        self.assertEqual(swept[0]["action"], "failed")
        job = self.app.store.get(job_id)
        self.assertEqual((job["status"], job["launch_state"]), ("failed", "failed"))
        self.assertIn("lease expired", job["error"]["message"])

    def test_stale_claim_by_dead_launcher_can_be_reclaimed_directly(self):
        job_id = self.create_and_handoff()
        self.launcher("POST", f"/jobs/{job_id}/launch/claim", {"launcher_id": "launcher-A", "lease_s": 30})
        past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat(timespec="seconds").replace("+00:00", "Z")
        self.app.store._conn.execute("UPDATE jobs SET lease_until=? WHERE id=?", (past, job_id))
        verdict, job = self.app.store.claim_launch(job_id, "launcher-B", 30)
        self.assertEqual((verdict, job["launch_owner"]), ("claimed", "launcher-B"))


class ResultHandoffTests(LiveWorkerBase):
    def result_for(self, job_id):
        r = json.loads((FIX / "opencode-result.synthetic.json").read_text(encoding="utf-8"))
        r.update({"synthetic": False, "job_id": job_id, "repository": "your-org/your-first-real-repo"})
        r["pr"].update({"number": 7, "head_sha": SHA, "title": "Real PR"})
        r["executed_by"] = {"engine": "opencode", "mode": "live"}
        r["environment"] = {"name": "staging", "base_url_redacted": "https://staging.example.test"}
        return r

    def test_result_is_validated_idempotent_and_owner_bound(self):
        job_id = self.create_and_handoff()
        self.launcher("POST", f"/jobs/{job_id}/launch/claim", {"launcher_id": "launcher-A"})
        self.launcher("POST", f"/jobs/{job_id}/launch/heartbeat", {"launcher_id": "launcher-A", "state": "launching"})
        art_path = Path(self.app.store.get(job_id)["launch"]["artifact"]["path"])
        self.assertTrue(art_path.is_file())
        res = self.result_for(job_id)
        cleanup = {"ok": True, "resources": [{"kind": "volume", "name": f"prqa-checkout-{job_id}", "action": "removed", "verified_absent": True}]}
        self.assertEqual(self.launcher("POST", f"/jobs/{job_id}/launch/result", {"launcher_id": "launcher-B", "result": res, "cleanup": cleanup}, launcher_id="launcher-B")[0], 409)
        status, body = self.launcher("POST", f"/jobs/{job_id}/launch/result", {"launcher_id": "launcher-A", "result": res, "cleanup": cleanup})
        self.assertEqual((status, body["status"], body["launch_state"], body["already"]), (200, "succeeded", "finished", False))
        job = self.app.store.get(job_id)
        self.assertEqual(job["result"]["summary"]["overall_status"], "FAIL")
        self.assertEqual(job["launch"]["cleanup"]["ok"], True)
        self.assertFalse(art_path.exists(), "sources purged once the launcher handed the result back")
        kinds = [e["kind"] for e in self.app.store.events(job_id=job_id)]
        self.assertIn("sandbox_cleanup", kinds); self.assertIn("launch_finished", kinds)
        # duplicate delivery: idempotent, no second transition, stored result unchanged
        status, body = self.launcher("POST", f"/jobs/{job_id}/launch/result", {"launcher_id": "launcher-A", "result": self.result_for(job_id), "cleanup": cleanup})
        self.assertEqual((status, body["already"]), (200, True))
        self.assertEqual(self.app.store.get(job_id)["status"], "succeeded")
        # the Discord stage can report it (succeeded), as before (scripted mock sink, nothing leaves the process)
        self.stub_discord()
        status, body = self.worker("POST", "/notifications/discord", {"kind": "result", "job_id": job_id})
        self.assertEqual((status, body["status"]), (200, "mock_delivered"))

    def test_invalid_result_from_launcher_fails_the_job(self):
        job_id = self.create_and_handoff()
        self.launcher("POST", f"/jobs/{job_id}/launch/claim", {"launcher_id": "launcher-A"})
        bad = self.result_for(job_id); bad["summary"]["passed"] = 99
        status, body = self.launcher("POST", f"/jobs/{job_id}/launch/result", {"launcher_id": "launcher-A", "result": bad, "cleanup": {"ok": True, "resources": []}})
        self.assertEqual(status, 200)
        job = self.app.store.get(job_id)
        self.assertIn(job["status"], ("queued", "failed")); self.assertIsNone(job["result"])

    def test_cancel_during_launch_wins_over_result(self):
        job_id = self.create_and_handoff()
        self.launcher("POST", f"/jobs/{job_id}/launch/claim", {"launcher_id": "launcher-A"})
        self.worker("POST", f"/jobs/{job_id}/cancel", {"reason": "PR_CLOSED"})
        status, body = self.launcher("POST", f"/jobs/{job_id}/launch/result", {"launcher_id": "launcher-A", "result": self.result_for(job_id), "cleanup": {"ok": True, "resources": []}})
        self.assertEqual((status, body["status"], body["launch_state"]), (200, "cancelled", "cancelled"))
        self.assertIsNone(self.app.store.get(job_id)["result"])
        status, body = self.launcher("POST", f"/jobs/{job_id}/launch/cancelled", {"launcher_id": "launcher-A", "cleanup": {"ok": True, "resources": []}})
        self.assertEqual((status, body["already"]), (200, True))

    def test_failed_and_cancelled_handoffs(self):
        job_id = self.create_and_handoff()
        self.launcher("POST", f"/jobs/{job_id}/launch/claim", {"launcher_id": "launcher-A"})
        status, body = self.launcher("POST", f"/jobs/{job_id}/launch/failed", {"launcher_id": "launcher-A", "error": {"type": "RuntimeError", "message": "sandbox step 'run' exited 66"},
                                                                               "cleanup": {"ok": False, "failed": [{"kind": "volume", "name": f"prqa-checkout-{job_id}", "action": "still_present"}], "resources": []}})
        self.assertEqual(status, 200)
        job = self.app.store.get(job_id)
        self.assertIn(job["status"], ("queued", "failed"))       # QA-level retry policy applies
        self.assertFalse(job["launch"]["cleanup"]["ok"], "cleanup failure recorded, not hidden")
        self.assertIn("sandbox_cleanup", [e["kind"] for e in self.app.store.events(job_id=job_id)])
        if job["status"] == "queued":                              # QA-level retry re-queued it: take it out of the queue for the next step
            self.worker("POST", f"/jobs/{job_id}/cancel", {"reason": "PR_CLOSED"})
        # cancelled without a cancel request is treated as a failure, never as success
        job2 = self.create_and_handoff("l-2", "b" * 40)
        self.launcher("POST", f"/jobs/{job2}/launch/claim", {"launcher_id": "launcher-A"})
        status, body = self.launcher("POST", f"/jobs/{job2}/launch/cancelled", {"launcher_id": "launcher-A", "cleanup": {"ok": True, "resources": []}})
        self.assertEqual(status, 200); self.assertIn(self.app.store.get(job2)["status"], ("queued", "failed"))


class WorkerRestartTests(unittest.TestCase):
    def test_store_is_persistent_and_restart_requeues_running_mock_jobs(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db = Path(tmp.name) / "jobs.db"
            store = JobStore(db)
            from tests.test_hardening import synthetic_job_request
            job = store.create(synthetic_job_request("r-1"), execution_mode="mock", max_retries=1, delivery_id="r-1")
            store.next_queued()
            self.assertEqual(store.get(job["id"])["status"], "running")
            del store                                           # "worker died"
            store2 = JobStore(db)                               # same file → same rows (WAL SQLite on the data volume)
            self.assertEqual(store2.get(job["id"])["status"], "running")
            recovered = store2.recover_worker_restart()
            self.assertEqual(recovered, [job["id"]]); self.assertEqual(store2.get(job["id"])["status"], "queued")
            self.assertEqual(store2.recover_worker_restart(), [], "idempotent")
        finally:
            tmp.cleanup()

    def test_startup_refuses_docker_access_when_enforced(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            fake_sock = Path(tmp.name) / "docker.sock"; fake_sock.write_text("")
            base = live_env(tmp.name, AUTOMATION_MODE="review_only")
            with self.assertRaises(SystemExit) as ctx:
                App(dict(base, PRQA_ENFORCE_NO_DOCKER="true", PRQA_DOCKER_SOCKET_PATH=str(fake_sock), PATH="/nonexistent"))
            self.assertIn("docker socket present", str(ctx.exception)); self.assertNotIn(WT, str(ctx.exception)); self.assertNotIn(LT, str(ctx.exception))
            with self.assertRaises(SystemExit) as ctx:
                App(dict(base, PRQA_ENFORCE_NO_DOCKER="true", PRQA_DOCKER_SOCKET_PATH=str(tmp.name) + "/none", DOCKER_HOST="tcp://x", PATH="/nonexistent"))
            self.assertIn("DOCKER_HOST", str(ctx.exception))
            app = App(dict(base, PRQA_ENFORCE_NO_DOCKER="true", PRQA_DOCKER_SOCKET_PATH=str(tmp.name) + "/none", PATH="/nonexistent"))
            app.executor.stop()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
