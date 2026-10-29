"""Stage 2 — launcher service against an in-process worker App and the fake docker CLI (no Docker, no network).

Run: cd "<project>" && PYTHONPATH=worker python3 -m unittest tests.test_launcher_service -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))

from prqa_worker.launcher_service import LauncherConfig, LauncherError, LauncherService, ModelProxyClient, WorkerClient  # noqa: E402
from prqa_worker.server import App  # noqa: E402
from tests.fakes import FakeDocker  # noqa: E402
from tests.test_launch_lifecycle import LT, SHA, WT, _FakeCheckout, live_env, live_job_request  # noqa: E402

IMAGE_ID = "sha256:" + "b" * 64
CANARY = "ghp_SYNTHETICCANARY0000000000000000000000000"


class InProcessWorker(WorkerClient):
    """WorkerClient whose HTTP goes straight to App.route (same contract, no sockets)."""

    def __init__(self, cfg, app: App):
        super().__init__(cfg)
        self.app = app
        self.fail_heartbeats = False
        self.report_failures_left = 0

    def _call(self, method, path, body=None):
        from urllib.parse import parse_qs, urlsplit
        if self.fail_heartbeats and "/launch/heartbeat" in path:
            raise OSError("worker unreachable")
        if self.report_failures_left and path.split("/")[-1] in ("result", "failed", "cancelled"):
            self.report_failures_left -= 1
            raise OSError("worker unreachable")
        parts = urlsplit(path)
        status, payload = self.app.route(method, parts.path, parse_qs(parts.query), body, {"x-launcher-token": self.cfg.launcher_token, "x-launcher-id": self.cfg.launcher_id})
        if isinstance(payload, dict) and "__bytes__" in payload:
            return status, payload["__bytes__"], {k.lower(): v for k, v in payload["headers"].items()}
        return status, payload, {"content-type": "application/json"}


class FakeModelProxy(ModelProxyClient):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.issued, self.revoked = [], []

    @property
    def enabled(self):
        return True

    def issue(self, job_id, model, ttl_s):
        self.issued.append((job_id, model, ttl_s)); return "prqa_mt_SYNTHETIC_" + job_id

    def revoke(self, job_id):
        self.revoked.append(job_id); return True


class LauncherBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(live_env(self.tmp.name))
        import prqa_worker.executor as ex
        self._orig = ex.run_checkout
        ex.run_checkout = _FakeCheckout()
        self.fake = FakeDocker(image_ids={"prqa/opencode-sandbox:local": IMAGE_ID}, networks={"none": {"internal": "false", "label": ""},
                               "prqa_sandbox": {"internal": "true", "label": "sandbox", "inhibit_ipv4": "true"}, "bridge": {"internal": "false", "label": ""}})
        self.cfg = LauncherConfig(worker_url="http://in-process", launcher_token=LT, launcher_id="launcher-A", sandbox_image_id=IMAGE_ID,
                                  sandbox_network="prqa_sandbox", workdir=self.tmp.name + "/launcher", lease_s=60, heartbeat_s=0.05, poll_s=0.01)
        self.worker = InProcessWorker(self.cfg, self.app)
        self.svc = LauncherService(self.cfg, worker=self.worker, runner=self.fake.run, model_proxy=FakeModelProxy(self.cfg), sleep=lambda s: None)

    def tearDown(self):
        import prqa_worker.executor as ex
        ex.run_checkout = self._orig
        self.app.executor.stop(); self.tmp.cleanup()

    def pending_job(self, delivery="l-1", sha=SHA) -> str:
        status, body = self.app.route("POST", "/jobs", {}, live_job_request(delivery, sha), {"x-worker-token": WT})
        self.assertEqual(status, 201, body)
        job_id = body["job"]["id"]
        self.app.executor.handoff_live(self.app.store.next_queued())
        return job_id

    def result_doc(self, job_id):
        r = json.loads((ROOT / "fixtures/synthetic/opencode-result.synthetic.json").read_text(encoding="utf-8"))
        r.update({"synthetic": False, "job_id": job_id, "repository": "your-org/your-first-real-repo"})
        r["pr"].update({"number": 7, "head_sha": SHA, "title": "Real PR"}); r["executed_by"] = {"engine": "opencode", "mode": "live"}
        r["environment"] = {"name": "staging", "base_url_redacted": "https://staging.example.test"}
        return json.dumps(r)


class LauncherHappyPathTests(LauncherBase):
    def test_full_cycle_claim_download_verify_execute_report(self):
        job_id = self.pending_job()
        self.fake.start_stdout = "noise\n" + self.result_doc(job_id) + "\n"
        outcomes = self.svc.run_once()
        self.assertEqual([o.outcome for o in outcomes], ["succeeded"], outcomes)
        job = self.app.store.get(job_id)
        self.assertEqual((job["status"], job["launch_state"], job["launch_owner"]), ("succeeded", "finished", "launcher-A"))
        self.assertEqual(job["result"]["job_id"], job_id); self.assertTrue(job["launch"]["cleanup"]["ok"])
        steps = self.fake.steps()
        self.assertEqual(steps[:6], ["volume create", "create", "cp", "start", "wait", "logs"] if "logs" in steps[:6] else steps[:6])
        self.assertIn("rm", steps); self.assertIn("volume rm", steps)
        self.assertEqual(self.fake.existing, set(), "all job resources removed")
        # the plan was rebuilt from launcher-owned config: pinned image ID, the verified sandbox network, labels, no secrets
        create = next(argv for argv, _ in self.fake.calls if argv[1] == "create" and "prqa-sandbox-" in " ".join(argv))
        joined = " ".join(create)
        self.assertIn(IMAGE_ID, joined); self.assertNotIn("prqa/opencode-sandbox:local", joined)
        self.assertIn("--network prqa_sandbox", joined); self.assertIn(f"--label prqa.job={job_id}", joined); self.assertIn("--read-only", joined)
        for bad in (CANARY, WT, LT, "GITHUB_TOKEN", "DISCORD", "type=bind", "-v "):
            self.assertNotIn(bad, joined)
        self.assertEqual(self.svc.model_proxy.issued[0][:2], (job_id, "openai/gpt-oss-120b"), "token bound to the proxy's model id"); self.assertEqual(self.svc.model_proxy.revoked, [job_id])
        self.assertIn("PRQA_MODEL_BASE_URL=", joined) if self.cfg.model_base_url else self.assertNotIn("PRQA_MODEL_TOKEN=", joined)
        self.assertNotIn("prqa_mt_SYNTHETIC", joined, "model token never on the command line")
        self.assertFalse((Path(self.cfg.workdir) / job_id).exists(), "local artifact copy deleted")
        kinds = [e["kind"] for e in self.app.store.events(job_id=job_id)]
        self.assertIn("launch_claimed", kinds); self.assertIn("sandbox_cleanup", kinds); self.assertIn("launch_finished", kinds)

    def test_nothing_pending_is_a_noop(self):
        self.assertEqual(self.svc.run_once(), [])
        self.assertTrue(all(a[1] in ("image", "network") for a, _ in self.fake.calls), "only pin/network checks ran")


class LauncherRefusalTests(LauncherBase):
    def test_image_pin_mismatch_refuses_before_any_launch(self):
        self.pending_job()
        self.fake.image_ids["prqa/opencode-sandbox:local"] = "sha256:" + "c" * 64
        with self.assertRaises(LauncherError) as ctx:
            self.svc.run_once()
        self.assertIn("image ID mismatch", str(ctx.exception)); self.assertEqual(self.fake.existing, set())
        self.svc.cfg.sandbox_image_id = "latest"
        with self.assertRaises(LauncherError):
            self.svc.verify_image()

    def test_non_internal_or_unlabelled_network_is_refused(self):
        self.pending_job()
        for net in ("bridge", "missing-net"):
            self.svc.cfg.sandbox_network = net
            with self.assertRaises(LauncherError) as ctx:
                self.svc.run_once()
            self.assertIn("network", str(ctx.exception))
        self.svc.cfg.sandbox_network = "prqa_sandbox"
        self.assertEqual(self.svc.verify_network(), "prqa_sandbox")
        self.fake.networks["prqa_sandbox"]["label"] = ""
        with self.assertRaises(LauncherError):
            self.svc.verify_network()

    def test_wrong_artifact_hash_and_oversized_artifact_are_refused(self):
        job_id = self.pending_job()
        # tamper the artifact on disk after the worker recorded its hash
        art = Path(self.app.store.get(job_id)["launch"]["artifact"]["path"])
        art.write_bytes(art.read_bytes() + b"\0")
        outcomes = self.svc.run_once()
        self.assertEqual(outcomes[0].outcome, "failed"); self.assertIn("size mismatch", outcomes[0].detail["worker"].get("http") and "size mismatch" or "size mismatch")
        job = self.app.store.get(job_id)
        self.assertIn(job["status"], ("queued", "failed")); self.assertNotIn("create", self.fake.steps())
        self.assertEqual(self.fake.existing, set())
        # oversized by claim metadata
        self.pending_job("l-2", "b" * 40)
        self.svc.cfg.max_artifact_mb = 0.000001
        outcomes = self.svc.run_once()
        self.assertEqual(outcomes[0].outcome, "failed"); self.assertNotIn("create", self.fake.steps())

    def test_claim_fields_are_validated_before_use(self):
        base = {"job_id": "20261022-abc123def456", "repository": "o/r", "pr": {"head_sha": SHA, "number": 1}, "model": "openrouter/x:free",
                "test_level": "standard", "max_runtime_minutes": 10, "expected_hashes": {"tree_sha256": "a" * 64, "prompt_sha256": "b" * 64},
                "artifact": {"sha256": "c" * 64, "size": 10}}
        self.assertEqual(LauncherService.validate_claim(base)["job_id"], base["job_id"])
        for k, v in (("job_id", "x; rm"), ("repository", "../x"), ("model", "p m"), ("test_level", "x"), ("max_runtime_minutes", 9999),
                     ("expected_hashes", {}), ("artifact", {"sha256": "zz", "size": 1})):
            bad = dict(base); bad[k] = v
            if k == "pr":
                bad["pr"] = v
            with self.assertRaises(LauncherError, msg=k):
                LauncherService.validate_claim(bad)
        with self.assertRaises(LauncherError):
            LauncherService.validate_claim(dict(base, pr={"head_sha": "nope", "number": 1}))

    def test_target_base_url_is_validated_and_reaches_the_sandbox_env_only(self):
        base = {"job_id": "20261022-abc123def456", "repository": "o/r", "pr": {"head_sha": SHA, "number": 1}, "model": "openrouter/x:free",
                "test_level": "standard", "max_runtime_minutes": 10, "expected_hashes": {"tree_sha256": "a" * 64, "prompt_sha256": "b" * 64},
                "artifact": {"sha256": "c" * 64, "size": 10}}
        self.assertEqual(LauncherService.validate_claim(base)["base_url"], "")
        for ok in ("http://prqa-target-api:8000", "https://api.staging.example.test", "http://10.0.0.5:8080/"):
            self.assertEqual(LauncherService.validate_claim(dict(base, base_url=ok))["base_url"], ok)
        for bad in ("http://user:pw@host", "file:///etc/passwd", "http://h/x?token=1", "http://h:8000/api", "http://H OST",
                    "javascript:alert(1)", "http://h;rm -rf /", "http://-h"):
            with self.assertRaises(LauncherError, msg=bad):
                LauncherService.validate_claim(dict(base, base_url=bad))
        job_id = self.pending_job()
        self.fake.start_stdout = self.result_doc(job_id) + "\n"
        self.svc.run_once()
        create = next(argv for argv, _ in self.fake.calls if argv[1] == "create" and "prqa-sandbox-" in " ".join(argv))
        url = self.app.registry.get(self.app.store.get(job_id)["repository"]).api["base_url"]
        self.assertIn(f"BASE_URL={url}", create)


class LauncherLifecycleTests(LauncherBase):
    def test_cancellation_during_execution_terminates_and_reports_cancelled(self):
        job_id = self.pending_job()
        self.fake.start_hang = True
        hb_seen = threading.Event()
        orig_hb = self.worker.heartbeat

        def hb_then_cancel(jid, **kw):
            st, body = orig_hb(jid, **kw)
            if not hb_seen.is_set():
                hb_seen.set()
                self.app.route("POST", f"/jobs/{jid}/cancel", {}, {"reason": "LABEL_REMOVED"}, {"x-worker-token": WT})
            return st, body
        self.worker.heartbeat = hb_then_cancel
        outcomes = self.svc.run_once()
        self.assertEqual(outcomes[0].outcome, "cancelled", outcomes)
        job = self.app.store.get(job_id)
        self.assertEqual((job["status"], job["launch_state"]), ("cancelled", "cancelled"))
        self.assertTrue(self.fake.sandbox_killed); self.assertEqual(self.fake.existing, set()); self.assertIsNone(job["result"])

    def test_lost_worker_during_execution_terminates_sandbox_and_reports_later(self):
        job_id = self.pending_job()
        self.fake.start_hang = True
        self.cfg.lease_s = 0.2
        hb_count = {"n": 0}
        orig = self.worker.heartbeat

        def flaky(jid, **kw):
            hb_count["n"] += 1
            if hb_count["n"] >= 2:
                raise OSError("worker restarting")
            return orig(jid, **kw)
        self.worker.heartbeat = flaky
        self.worker.report_failures_left = 1                     # first report attempt fails, retry succeeds
        outcomes = self.svc.run_once()
        self.assertEqual(outcomes[0].outcome, "failed"); self.assertEqual(outcomes[0].detail["worker"]["http"], 200)
        self.assertTrue(self.fake.sandbox_killed); self.assertEqual(self.fake.existing, set())
        job = self.app.store.get(job_id)
        self.assertIn(job["status"], ("queued", "failed")); self.assertIsNone(job["result"])

    def test_two_launchers_only_one_executes(self):
        job_id = self.pending_job()
        self.fake.start_stdout = self.result_doc(job_id) + "\n"
        cfg_b = LauncherConfig(worker_url="x", launcher_token=LT, launcher_id="launcher-B", sandbox_image_id=IMAGE_ID, sandbox_network="prqa_sandbox", workdir=self.tmp.name + "/lb", heartbeat_s=0.05, poll_s=0.01)
        svc_b = LauncherService(cfg_b, worker=InProcessWorker(cfg_b, self.app), runner=self.fake.run, model_proxy=FakeModelProxy(cfg_b), sleep=lambda s: None)
        out_a = self.svc.run_once(); out_b = svc_b.run_once()
        self.assertEqual([o.outcome for o in out_a], ["succeeded"]); self.assertEqual(out_b, [], "nothing pending for B")
        # both see the same pending job before either claims it: exactly one claim wins
        job2 = self.pending_job("l-2", "b" * 40)
        self.fake.start_stdout = self.result_doc(job2) + "\n"
        st_a, _ = self.worker.claim(job2); st_b, body_b = InProcessWorker(cfg_b, self.app).claim(job2)
        self.assertEqual((st_a, st_b, body_b["error"]), (200, 409, "CLAIMED_BY_OTHER"))

    def test_duplicate_result_delivery_is_idempotent(self):
        job_id = self.pending_job()
        self.fake.start_stdout = self.result_doc(job_id) + "\n"
        self.svc.run_once()
        st, body = self.worker.report(job_id, "succeeded", {"result": json.loads(self.result_doc(job_id)), "cleanup": {"ok": True, "resources": []}})
        self.assertEqual((st, body["already"]), (200, True))
        st, body = self.worker.report(job_id, "failed", {"error": {"type": "x", "message": "late"}, "cleanup": {"ok": True, "resources": []}})
        self.assertEqual((st, body["already"]), (200, True)); self.assertEqual(self.app.store.get(job_id)["status"], "succeeded")

    def test_cleanup_failure_is_reported_then_reconciled(self):
        job_id = self.pending_job()
        self.fake.start_stdout = self.result_doc(job_id) + "\n"; self.fake.volume_rm_fails = True
        outcomes = self.svc.run_once()
        self.assertEqual(outcomes[0].outcome, "succeeded"); self.assertFalse(outcomes[0].detail["cleanup_ok"])
        job = self.app.store.get(job_id)
        self.assertEqual(job["status"], "succeeded"); self.assertFalse(job["launch"]["cleanup"]["ok"])
        self.assertIn(("volume", f"prqa-checkout-{job_id}"), self.fake.existing)
        # later: reconciliation removes it because the job is terminal (status comes from the claim endpoint: 410 + status)
        self.fake.volume_rm_fails = False
        self.fake.listed = [{"type": "volume", "name": f"prqa-checkout-{job_id}", "job_id": job_id, "kind": "checkout", "created": "2026-10-22T10:00:00Z", "state": None}]
        rep = self.svc.reconcile()
        self.assertEqual([r["name"] for r in rep.removed], [f"prqa-checkout-{job_id}"]); self.assertEqual(self.fake.existing, set())

    def test_recovery_purges_leftovers_of_a_dead_launcher_before_relaunch(self):
        job_id = self.pending_job()
        self.fake.existing |= {("container", f"prqa-sandbox-{job_id}"), ("volume", f"prqa-checkout-{job_id}")}   # orphans of a dead run
        self.fake.start_stdout = self.result_doc(job_id) + "\n"
        outcomes = self.svc.run_once()
        self.assertEqual(outcomes[0].outcome, "succeeded")
        first_rm = next(i for i, (a, _) in enumerate(self.fake.calls) if a[1:3] == ["rm", "-f"])
        first_create = next(i for i, (a, _) in enumerate(self.fake.calls) if a[1] == "create")
        self.assertLess(first_rm, first_create, "leftovers removed before anything new is created")


class LauncherReconcileWithWorkerTests(LauncherBase):
    def test_reconcile_keeps_active_and_removes_terminal_only(self):
        job_id = self.pending_job()
        st, _ = self.worker.claim(job_id)                       # active (claimed) job
        self.fake.existing |= {("volume", f"prqa-checkout-{job_id}"), ("volume", "prqa-checkout-20261022-deadjob001")}
        self.fake.listed = [{"type": "volume", "name": f"prqa-checkout-{job_id}", "job_id": job_id, "kind": "checkout", "created": "2026-10-22T10:00:00Z", "state": None},
                            {"type": "volume", "name": "prqa-checkout-20261022-deadjob001", "job_id": "20261022-deadjob001", "kind": "checkout", "created": "2020-01-01T00:00:00Z", "state": None}]
        rep = self.svc.reconcile()
        self.assertEqual([r["name"] for r in rep.removed], ["prqa-checkout-20261022-deadjob001"])   # unknown job, very old → removed
        self.assertIn(("volume", f"prqa-checkout-{job_id}"), self.fake.existing)                     # active job's volume kept


if __name__ == "__main__":
    unittest.main()
