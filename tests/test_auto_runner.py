"""scripts/pipeline/prqa_auto_runner.py — automatic label-triggered execution (no manual prompt review; the gate approves).

Offline: GitHub / Docker / the disposable environment are the fake Host of tests/test_run_single_pr.py; the worker is
the real in-process App (review-only, grant key, GITHUB_TOKEN), so policy, grant verification/consumption, the job store
and its dedupe are the shipped code. No network: the runner only talks to the fake host and the in-process worker.

Run: cd "<project>" && PYTHONPATH=worker python3 -m unittest tests.test_auto_runner -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))

from prqa_worker import run_grants  # noqa: E402
from tests.test_run_single_pr import KEY, R, RunnerBase, SHA, SHA2, pr_json  # noqa: E402

spec = importlib.util.spec_from_file_location("prqa_auto_runner", ROOT / "scripts" / "pipeline" / "prqa_auto_runner.py")
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)  # type: ignore[union-attr]
# the auto runner loads its own copy of the single-PR runner: point it at the same patched module the base class patches
A.R = R
REJECT = {"accepted": False, "failure": "HF-UNUSABLE", "score": 70.0, "threshold": 85.0, "hard_fails": ["HF-UNUSABLE: x"], "gate_errors": ["x"],
          "gate_warnings": [], "human_flags": [], "dimensions": {}, "worker_and_local_builder_consistent": True}


WEBHOOK = "https://discord.com/api/webhooks/123456789012345/SYNTHETICwebhookTOKENabcdefghijklmnop"


class FakeDiscord:
    """Records every POST (host + parsed body); answers from a scripted list of status codes (default 204)."""

    def __init__(self, codes=None):
        self.codes = list(codes or [])
        self.posts = []

    def __call__(self, req, timeout=None):
        import urllib.error
        self.posts.append({"host": req.host, "body": json.loads(req.data.decode())})
        code = self.codes.pop(0) if self.codes else 204
        if code >= 300:
            raise urllib.error.HTTPError(req.full_url, code, "x", {"Retry-After": "0"} if code == 429 else {}, None)

        class Resp:
            status = code

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return Resp()


class AutoBase(RunnerBase):
    def setUp(self):
        super().setUp()
        t = Path(self.tmp.name)
        self.state_dir, self.auto_dir = t / "auto-state", t / "auto-runs"
        self.auto_dir.mkdir(parents=True, exist_ok=True)
        (self.auto_dir / "discord-setup-test.json").write_text(json.dumps({"ok": True, "status_code": 204}))
        self.discord = FakeDiscord()

    def host(self, prs=None, *, webhook=WEBHOOK, **kw):
        h = super().host(prs, **kw)
        h.discord_url = lambda: webhook
        h.discord_opener = self.discord
        return h

    def once(self, host, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            res = A.once(host, state_dir=self.state_dir, auto_dir=self.auto_dir, wait_s=kw.pop("wait_s", 0), out=print)
        self.out = buf.getvalue()
        return res

    def state(self):
        return A.load_state(self.state_dir)

    def side_effects(self, host):
        return [c for c in host.calls if c[0] in ("fetch_commit", "env_up", "env_auth_probe")]

    def assert_nothing_started(self, host):
        self.assertEqual(self.side_effects(host), [])
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.app.store.grant_audit_log(), [])


class EligibilityTests(AutoBase):
    def test_exact_label_only(self):
        for labels in (("To Be Test",), ("to be tested",), ("needs-review",), ()):
            host = self.host({7: pr_json(7, labels=labels)})
            res = self.once(host)
            self.assertEqual(res["eligible"], [], labels)
            self.assertIn("idle", res["result"])
            self.assert_nothing_started(host)

    def test_draft_fork_closed_and_unsupported_base_are_rejected(self):
        for kw in ({"draft": True}, {"fork": True}, {"state": "closed"}, {"base": "release"}):
            host = self.host({7: pr_json(7, **kw)})
            res = self.once(host)
            self.assertEqual(res["eligible"], [], kw)
            self.assert_nothing_started(host)

    def test_no_eligible_pr_triggers_nothing(self):
        host = self.host({5: pr_json(5, base="main", labels=())})
        res = self.once(host)
        self.assertEqual(res["result"], "idle: no eligible, unprocessed PR — nothing triggered")
        self.assertEqual(self.state()["keys"], {})
        self.assert_nothing_started(host)


class PrerequisiteTests(AutoBase):
    def test_missing_github_token_or_grant_key_waits_without_claiming(self):
        for kw in ({"secrets": ("PRQA_RUN_GRANT_KEY",)}, {"secrets": ("GITHUB_TOKEN",), "key": ""}):
            host = self.host(**kw)
            res = self.once(host)
            self.assertIn("waiting", res["result"])
            self.assertTrue(res["blockers"])
            self.assertEqual(self.state()["keys"], {})
            self.assert_nothing_started(host)

    def test_registry_not_pointing_at_the_disposable_api_waits(self):
        host = self.host(base_url="https://replace-me.staging.example.test")
        res = self.once(host)
        self.assertTrue(any("base_url" in b for b in res["blockers"]))
        self.assert_nothing_started(host)


class PipelineTests(AutoBase):
    def test_eligible_pr_runs_automatically_with_one_prompt_bound_grant_and_one_job(self):
        host = self.host()
        res = self.once(host)
        entry = res["processed"]
        self.assertEqual((entry["pr"], entry["sha"]), (7, SHA))
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        run_dir = R.RUNS_DIR / f"acme-pr-7-{SHA}"
        pv = json.loads((run_dir / "preview.json").read_text())
        self.assertTrue(pv["quality_accepted"])
        self.assertEqual(jobs[0]["prompt_sha256"] if "prompt_sha256" in jobs[0] else jobs[0]["request"]["prompt"]["sha256"], pv["prompt_sha256"])
        consumed = [a for a in self.app.store.grant_audit_log() if a["action"] == "consumed"]
        self.assertEqual(len(consumed), 1)
        validated = [a for a in self.app.store.grant_audit_log() if a["action"] == "validated"][0]
        claims = validated["detail"]["claims"]
        self.assertEqual((claims["purpose"], claims["prompt_sha256"], claims["head_sha"]), (run_grants.AUTO_PURPOSE, pv["prompt_sha256"], SHA))
        text = (run_dir / "qa-prompt.md").read_text()
        self.assertIn("## 14. Test Identities", text)                      # synthetic self-registration, no handed-over secret
        self.assertEqual(R.sha256(text), pv["prompt_sha256"])
        self.assertFalse(list(Path(pv["private_dir"]).glob("grant-*")) if Path(pv["private_dir"]).exists() else [])

    def test_duplicate_polls_and_restart_never_create_a_second_job(self):
        host = self.host()
        self.once(host)
        self.once(host)
        self.once(host)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(len(self.state()["keys"]), 1)

    def test_concurrent_runner_is_refused_by_the_lock(self):
        host = self.host()
        with A.locked(self.state_dir):
            with self.assertRaises(A.Busy):
                self.once(host)
        self.assert_nothing_started(host)

    def test_concurrent_passes_create_exactly_one_job(self):
        results = []

        def run():
            try:
                results.append(A.once(self.host(), state_dir=self.state_dir, auto_dir=self.auto_dir, wait_s=0, out=lambda s: None))
            except A.Busy:
                results.append("busy")
        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.jobs()), 1)

    def test_new_head_sha_is_a_new_run(self):
        host = self.host()
        self.once(host)
        host.prs[7] = pr_json(7, sha=SHA2)
        res = self.once(host)
        self.assertEqual(res["pending"], [7])
        self.assertEqual(set(self.state()["keys"]), {A.run_key(7, SHA), A.run_key(7, SHA2)})

    def test_gate_rejection_blocks_execution(self):
        host = self.host()
        with mock.patch.object(R, "score_prompt", return_value=REJECT):
            res = self.once(host)
        entry = res["processed"]
        self.assertEqual(entry["status"], "blocked")
        self.assertIn("QUALITY_GATE_REJECTED", entry["reason"])
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.app.store.grant_audit_log(), [])
        self.assertEqual(entry["cleanup"], "ok")
        self.assertTrue((R.RUNS_DIR / f"acme-pr-7-{SHA}" / "report.md").is_file())

    def test_fabricated_operation_in_the_prompt_blocks_execution(self):
        host = self.host()
        real = host.worker

        def tampering(method, path, body=None):
            st, data = real(method, path, body)
            if path == "/prompts/finalize" and st == 200:
                t = data["prompt"]["text"].replace("**AUTH-001 (Required)", "**ORG-ARCHIVE-001 (Required) — Archive\n- Request: `POST /api/v1/organizations/{id}/archive` [FACT: diff]\n- Expected: 200.\n\n**AUTH-001 (Required)", 1)
                data["prompt"].update(text=t, sha256=R.sha256(t))
            return st, data
        host.worker = tampering
        res = self.once(host)
        self.assertEqual(res["processed"]["status"], "blocked")
        self.assertIn("QUALITY_GATE_REJECTED", res["processed"]["reason"])
        self.assertEqual(self.jobs(), [])

    def test_prompt_is_regated_immediately_before_submission(self):
        host = self.host()
        real = R.gate_all
        calls = {"n": 0}

        def second_rejects(text, context):
            calls["n"] += 1
            q = real(text, context)
            return q if calls["n"] == 1 else dict(q, accepted=False, failure="HF-UNUSABLE")
        with mock.patch.object(R, "gate_all", side_effect=second_rejects):
            res = self.once(host)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(res["processed"]["status"], "blocked")
        self.assertIn("re-gating", res["processed"]["reason"])
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.app.store.grant_audit_log(), [])

    def test_missing_test_credentials_block_execution(self):
        host = self.host(auth_ok=False)
        res = self.once(host)
        self.assertEqual(res["processed"]["status"], "blocked")
        self.assertIn("TEST_CREDENTIALS_UNAVAILABLE", res["processed"]["reason"])
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.app.store.grant_audit_log(), [])

    def test_environment_failures_block_and_clean_up(self):
        for kw in ({"env_up_rc": 1}, {"env_healthy": False}, {"probe_ok": False}):
            with self.subTest(**kw):
                self.setUp()
                host = self.host(**kw)
                res = self.once(host)
                self.assertEqual(res["processed"]["status"], "blocked", kw)
                self.assertEqual(self.jobs(), [])
                self.assertTrue([c for c in host.calls if c[0] == "env_down"], kw)

    def test_label_removed_before_submission_blocks(self):
        host = self.host()
        real = host.gh_get
        n = {"c": 0}

        def flip(path):
            pr = real(path)
            n["c"] += 1
            if n["c"] > 2:                                      # preview reads twice; execute's re-check sees no label
                pr["labels"] = []
            return pr
        host.gh_get = flip
        res = self.once(host)
        self.assertEqual(res["processed"]["status"], "blocked")
        self.assertIn("PR_NO_LONGER_ELIGIBLE", res["processed"]["reason"])
        self.assertEqual(self.jobs(), [])

    def test_prompt_changed_after_authorization_is_refused_by_the_worker(self):
        host = self.host()
        real = host.worker

        def swap(method, path, body=None):
            if path == "/jobs" and method == "POST":
                body = dict(body, prompt=dict(body["prompt"], text=body["prompt"]["text"] + "\nextra"))
            return real(method, path, body)
        host.worker = swap
        res = self.once(host)
        self.assertEqual(res["processed"]["status"], "blocked")
        self.assertIn("GRANT_PROMPT_MISMATCH", res["processed"]["reason"])
        self.assertEqual(self.jobs(), [])

    def test_cleanup_failure_is_reported_and_state_kept(self):
        host = self.host(down_rc=1)
        with mock.patch.object(R, "score_prompt", return_value=REJECT):
            res = self.once(host)
        self.assertTrue(res["processed"]["cleanup"].startswith("FAILED"))
        notes = (self.auto_dir / "notifications.jsonl").read_text()
        self.assertIn("FAILED", notes)
        host.down_rc = 0
        with redirect_stdout(io.StringIO()):
            self.assertEqual(R.cleanup(host, run_dir=R.RUNS_DIR / f"acme-pr-7-{SHA}"), 0)

    def test_recovery_never_reruns(self):
        host = self.host()
        self.state_dir.mkdir(parents=True)
        key = A.run_key(7, SHA)
        run_dir = R.RUNS_DIR / f"acme-pr-7-{SHA}"
        A.save_state(self.state_dir, {"policy_version": A.POLICY_VERSION, "keys": {key: {"key": key, "pr": 7, "sha": SHA, "status": "claimed",
                                                                                         "run_dir": R.rel(run_dir)}}})
        res = self.once(host)
        self.assertEqual(res["recovered"], [key])
        self.assertEqual(self.state()["keys"][key]["status"], "interrupted")
        self.assertEqual(res["pending"], [])                        # the same key is never started again
        self.assert_nothing_started(host)

    def test_recovery_adopts_an_existing_job(self):
        host = self.host()
        self.once(host)                                             # job created, not finished (no launcher here)
        key = A.run_key(7, SHA)
        st = self.state()
        st["keys"][key].update(status="submitting", job_id=None)
        A.save_state(self.state_dir, st)
        self.once(host)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.state()["keys"][key]["status"], "submitted")
        self.assertTrue(self.state()["keys"][key]["job_id"])


class SafetyTests(AutoBase):
    def test_no_provider_or_budget_bypass(self):
        src = (ROOT / "scripts" / "pipeline" / "prqa_auto_runner.py").read_text()
        for forbidden in ("api.groq.com", "GROQ_API_KEY", "PROVIDER_KEY", "8089", "model_proxy", "Ledger(", "reset"):
            self.assertNotIn(forbidden, src)
        host = self.host()
        self.once(host)
        self.assertFalse([c for c in host.calls if c[0] not in ("gh_get", "gh_paginate", "gh_diff", "worker", "fetch_commit", "env_up",
                                                              "env_auth_probe", "env_probe", "env_down")])

    def test_secrets_are_absent_from_output_state_and_artifacts(self):
        host = self.host()
        self.once(host)
        blobs = [self.out, (self.state_dir / "state.json").read_text()]
        for d in (R.RUNS_DIR, self.auto_dir):
            blobs += [p.read_text(errors="ignore") for p in d.rglob("*") if p.is_file()]
        joined = "\n".join(blobs)
        for secret in (KEY, run_grants.PREFIX, "github_pat_SYNTHETIC_"):
            self.assertNotIn(secret, joined)

    def test_auto_grant_requires_the_prompt_hash(self):
        with self.assertRaises(run_grants.GrantError) as ctx:
            run_grants.mint(KEY, repository=R.REPO, pr_number=7, head_sha=SHA, now=1_900_000_000, purpose=run_grants.AUTO_PURPOSE)
        self.assertEqual(ctx.exception.code, "GRANT_MALFORMED")

    def test_activation_refuses_without_a_github_token(self):
        host = self.host()
        host.secrets_file = Path(self.tmp.name) / "secrets.env"
        host.secrets_file.write_text("PRQA_RUN_GRANT_KEY=\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = A.activate(host, apply=True, out=print)
        self.assertEqual(rc, 2)
        self.assertIn("GITHUB_TOKEN_MISSING", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
