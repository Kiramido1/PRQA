"""Single-run grants: one-time, scope-bound authorization for ONE real PR job while PRQA stays review-only.
Negative paths first: expiry, replay, concurrent consumption, wrong repository / PR / SHA / level, bad signature,
synthetic jobs, non-replaceable policy rules, audit immutability, start-time re-validation, launch hand-off filtering.
SYNTHETIC: in-process App, temporary SQLite store, fake checkout, no Docker, no network.

Run: cd "<project>" && PYTHONPATH=worker python3 -m unittest tests.test_run_grants -v
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))

from prqa_worker import run_grants  # noqa: E402
from prqa_worker.server import App  # noqa: E402
from tests.test_launch_lifecycle import LT, SHA, WT, _FakeCheckout, live_env, live_job_request  # noqa: E402

KEY = "0f" * 32
OTHER_KEY = "a5" * 32
REPO = "your-org/your-first-real-repo"
NOW = 1_800_000_000


def review_only_env(tmp: str, **extra) -> dict:
    """The production posture: review-only, REAL_* off, the shipped registry (repository disabled + pending)."""
    env = live_env(tmp, AUTOMATION_MODE="review_only", REAL_PROJECT_TESTING_ENABLED="false", AUTO_EXECUTION_ENABLED="false",
                   PRQA_WORKER_CHECKOUT_ENABLED="false", PRQA_REGISTRY_PATH=str(ROOT / "config/repositories.yaml"),
                   PRQA_RUN_GRANT_KEY=KEY)
    env.update(extra)
    return env


def mint(**kw):
    args = {"repository": REPO, "pr_number": 7, "head_sha": SHA, "now": NOW, "ttl_s": 600}
    args.update(kw)
    return run_grants.mint(KEY if "key" not in kw else kw.pop("key"), **{k: v for k, v in args.items() if k != "key"})


class GrantTokenTests(unittest.TestCase):
    def test_roundtrip_and_claims(self):
        token, claims = mint()
        self.assertEqual(run_grants.verify(token, KEY, now=NOW + 1), claims)
        self.assertEqual(claims["expires_at"] - claims["issued_at"], 600)
        self.assertNotEqual(mint()[1]["grant_id"], claims["grant_id"])          # unique per mint

    def test_expired_and_future(self):
        token, _ = mint()
        with self.assertRaises(run_grants.GrantError) as c:
            run_grants.verify(token, KEY, now=NOW + 600)
        self.assertEqual(c.exception.code, "GRANT_EXPIRED")
        with self.assertRaises(run_grants.GrantError) as c:
            run_grants.verify(token, KEY, now=NOW - run_grants.MAX_SKEW_S - 5)
        self.assertEqual(c.exception.code, "GRANT_NOT_YET_VALID")

    def test_ttl_bounds(self):
        for ttl in (0, -5, run_grants.MAX_TTL_S + 1):
            with self.assertRaises(run_grants.GrantError):
                mint(ttl_s=ttl)

    def test_signature_and_tamper(self):
        token, claims = mint()
        with self.assertRaises(run_grants.GrantError) as c:
            run_grants.verify(token, OTHER_KEY, now=NOW)
        self.assertEqual(c.exception.code, "GRANT_SIGNATURE_INVALID")
        # widen the scope by re-encoding the claims with the original signature
        forged = dict(claims, pr_number=8)
        payload = run_grants._b64(json.dumps(forged, sort_keys=True, separators=(",", ":")).encode())
        with self.assertRaises(run_grants.GrantError) as c:
            run_grants.verify(run_grants.PREFIX + payload + "." + token.rsplit(".", 1)[1], KEY, now=NOW)
        self.assertEqual(c.exception.code, "GRANT_SIGNATURE_INVALID")
        for bad in (None, 5, "", "prqa-grant.v1.", "prqa-grant.v2." + token[14:], token + "x", "x" * 5000):
            with self.assertRaises(run_grants.GrantError):
                run_grants.verify(bad, KEY, now=NOW)

    def test_no_key_disables_grants(self):
        token, _ = mint()
        for key in ("", "short", "G" * 64):
            with self.assertRaises(run_grants.GrantError) as c:
                run_grants.verify(token, key, now=NOW)
            self.assertEqual(c.exception.code, "GRANT_KEY_NOT_CONFIGURED")

    def test_synthetic_repository_cannot_be_granted(self):
        with self.assertRaises(run_grants.GrantError) as c:
            mint(repository="synthetic-org/orders-api")
        self.assertEqual(c.exception.code, "GRANT_SYNTHETIC_FORBIDDEN")


class GrantServerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(review_only_env(self.tmp.name))
        self.app.clock = lambda: NOW + 10
        import prqa_worker.executor as ex
        self._orig = ex.run_checkout
        self.fake_checkout = _FakeCheckout()
        ex.run_checkout = self.fake_checkout

    def tearDown(self):
        import prqa_worker.executor as ex
        ex.run_checkout = self._orig
        self.app.executor.stop()
        self.tmp.cleanup()

    def post(self, request, token=None):
        body = dict(request)
        if token is not None:
            body["run_grant"] = token
        return self.app.route("POST", "/jobs", {}, body, {"x-worker-token": WT})

    def launcher(self, method, path, body=None):
        return self.app.route(method, path, {}, body, {"x-launcher-token": LT, "x-launcher-id": "launcher-A"})

    def audit_actions(self, grant_id=None):
        return [a["action"] for a in self.app.store.grant_audit_log(grant_id)]


class GrantServerTests(GrantServerBase):
    def test_without_grant_review_only_still_rejects(self):
        status, body = self.post(live_job_request("ng-1"))
        self.assertEqual((status, body["error"], body["decision"]["reason_code"]), (403, "POLICY_REJECTED", "REVIEW_ONLY_MODE"))
        self.assertEqual(self.app.store.list(), [])

    def test_valid_grant_creates_exactly_one_live_job(self):
        token, claims = mint()
        status, body = self.post(live_job_request("g-1"), token)
        self.assertEqual(status, 201, body)
        job = self.app.store.get(body["job"]["id"])
        self.assertEqual((job["execution_mode"], job["max_retries"], body["grant_id"]), ("live", 0, claims["grant_id"]))
        self.assertEqual(body["decision"]["reason_code"], "SINGLE_RUN_GRANT")
        self.assertEqual(self.audit_actions(claims["grant_id"]), ["validated", "consumed"])
        # the token is never stored: not in the job, the events, the audit or the DB file
        self.assertNotIn("run_grant", job["request"])
        dump = Path(self.app.store.path).read_bytes()
        self.assertNotIn(token.rsplit(".", 1)[1].encode(), dump)
        self.assertNotIn(KEY.encode(), dump)
        # global configuration untouched
        self.assertTrue(self.app.policy.review_only)
        self.assertFalse(self.app.registry.get(REPO).enabled)

    def test_replay_is_rejected(self):
        token, claims = mint()
        self.assertEqual(self.post(live_job_request("r-1"), token)[0], 201)
        status, body = self.post(live_job_request("r-2"), token)                 # same grant, new delivery id
        self.assertEqual((status, body["error"]), (403, "GRANT_ALREADY_CONSUMED"))
        status, body = self.post(live_job_request("r-1"), token)                 # exact replay
        self.assertEqual((status, body["error"]), (403, "GRANT_ALREADY_CONSUMED"))
        self.assertEqual(len(self.app.store.list()), 1)

    def test_concurrent_consumption_creates_one_job(self):
        token, _ = mint()
        results, barrier = [], threading.Barrier(8)

        def go(i):
            barrier.wait()
            results.append(self.post(live_job_request(f"c-{i}"), token)[0])
        threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [201] + [403] * 7)
        self.assertEqual(len(self.app.store.list()), 1)

    def test_scope_mismatches(self):
        cases = [("GRANT_REPOSITORY_MISMATCH", mint(repository="your-org/another-repo")[0], live_job_request("m-1")),
                 ("GRANT_PR_MISMATCH", mint(pr_number=8)[0], live_job_request("m-2")),
                 ("GRANT_SHA_MISMATCH", mint(head_sha="b" * 40)[0], live_job_request("m-3")),
                 ("GRANT_LEVEL_MISMATCH", mint(test_level="smoke")[0], live_job_request("m-4"))]
        for code, token, req in cases:
            status, body = self.post(req, token)
            self.assertEqual((status, body["error"]), (403, code))
        self.assertEqual(self.app.store.list(), [])
        self.assertEqual(self.audit_actions().count("rejected"), 4)

    def test_expired_grant_rejected_at_submission(self):
        token, _ = mint()
        self.app.clock = lambda: NOW + 600
        status, body = self.post(live_job_request("e-1"), token)
        self.assertEqual((status, body["error"]), (403, "GRANT_EXPIRED"))
        self.assertEqual(self.app.store.list(), [])

    def test_foreign_signature_and_no_worker_key(self):
        token, _ = run_grants.mint(OTHER_KEY, repository=REPO, pr_number=7, head_sha=SHA, now=NOW)
        self.assertEqual(self.post(live_job_request("s-1"), token)[1]["error"], "GRANT_SIGNATURE_INVALID")
        tmp = tempfile.TemporaryDirectory()
        try:
            app = App(review_only_env(tmp.name, PRQA_RUN_GRANT_KEY=""))
            app.clock = lambda: NOW
            body = dict(live_job_request("s-2"), run_grant=mint()[0])
            status, out = app.route("POST", "/jobs", {}, body, {"x-worker-token": WT})
            self.assertEqual((status, out["error"]), (403, "GRANT_KEY_NOT_CONFIGURED"))
            self.assertEqual(app.route("GET", "/launch/pending", {}, None, {"x-launcher-token": LT})[0], 403)
            app.executor.stop()
        finally:
            tmp.cleanup()

    def test_synthetic_job_never_uses_a_grant(self):
        req = dict(live_job_request("syn-1"), synthetic=True)
        status, body = self.post(req, mint()[0])
        self.assertEqual(status, 403)
        self.assertIn(body.get("error"), ("GRANT_SYNTHETIC_FORBIDDEN", "INVALID_JOB_REQUEST", "POLICY_REJECTED"))
        self.assertEqual(self.app.store.list(), [])

    def test_non_replaceable_rules_still_apply(self):
        # unregistered repository: a grant for it verifies and binds, but the registry rule is not replaceable
        req = live_job_request("u-1")
        req["repository"] = "your-org/unregistered"
        req["pr"] = dict(req["pr"], url="https://github.com/your-org/unregistered/pull/7")
        status, body = self.post(req, mint(repository="your-org/unregistered")[0])
        self.assertEqual((status, body["error"]), (403, "POLICY_REJECTED"))
        # disallowed label
        req = live_job_request("u-2")
        req["trigger"] = dict(req["trigger"], label="qa-test:yolo")
        status, body = self.post(req, mint()[0])
        self.assertEqual(status, 403)
        # fork PR on a repository that does not allow forks
        req = live_job_request("u-3")
        req["pr"] = dict(req["pr"], is_fork=True)
        status, body = self.post(req, mint()[0])
        self.assertEqual((status, body["error"]), (403, "POLICY_REJECTED"))
        self.assertEqual(self.app.store.list(), [])

    def test_grant_not_needed_when_policy_already_allows(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            app = App(live_env(tmp.name, PRQA_RUN_GRANT_KEY=KEY))
            app.clock = lambda: NOW
            body = dict(live_job_request("nn-1"), run_grant=mint()[0])
            status, out = app.route("POST", "/jobs", {}, body, {"x-worker-token": WT})
            self.assertEqual((status, out["error"]), (403, "GRANT_NOT_NEEDED"))
            app.executor.stop()
        finally:
            tmp.cleanup()

    def test_audit_is_append_only(self):
        token, claims = mint()
        self.post(live_job_request("a-1"), token)
        conn = sqlite3.connect(self.app.store.path)
        try:
            for sql in ("UPDATE run_grant_audit SET action='x'", "DELETE FROM run_grant_audit",
                        "UPDATE run_grants SET pr_number=99", "DELETE FROM run_grants"):
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute(sql)
        finally:
            conn.close()
        self.assertEqual(self.audit_actions(claims["grant_id"]), ["validated", "consumed"])


class GrantStartAndHandoffTests(GrantServerBase):
    def create(self, delivery="h-1"):
        status, body = self.post(live_job_request(delivery), mint()[0])
        self.assertEqual(status, 201, body)
        return body["job"]["id"]

    def handoff(self, at):
        running = self.app.store.next_queued()
        with mock.patch("prqa_worker.executor.time.time", lambda: at):            # the executor's start-time check
            return self.app.executor.handoff_live(running)

    def test_handoff_filters_to_granted_jobs_only(self):
        self.assertEqual(self.launcher("GET", "/launch/pending"), (200, {"pending": []}))
        job_id = self.create()
        self.handoff(NOW + 20)
        self.assertEqual(self.fake_checkout.calls, 1)
        status, body = self.launcher("GET", "/launch/pending")
        self.assertEqual((status, [j["job_id"] for j in body["pending"]]), (200, [job_id]))
        self.assertIn("start_validated", self.audit_actions())

    def test_expired_grant_refused_at_start(self):
        job_id = self.create("h-2")
        with self.assertRaises(Exception):
            self.handoff(NOW + 600)                                              # consumed in time, expired before start
        self.assertEqual(self.app.store.get(job_id)["status"], "failed")         # max_retries=0: no retry
        self.assertEqual(self.fake_checkout.calls, 0)
        self.assertEqual(self.launcher("GET", "/launch/pending"), (200, {"pending": []}))
        actions = self.audit_actions()
        self.assertIn("start_refused", actions)
        self.assertIn("outcome", actions)

    def test_ungranted_live_job_is_never_served(self):
        # a live job row without a grant (e.g. inserted by a bug) is invisible to the review-only hand-off
        job = self.app.store.create(live_job_request("x-1"), execution_mode="live", max_retries=0, delivery_id="x-1")
        self.assertFalse(self.app.granted_job_ok(job))
        with self.assertRaises(Exception):
            self.handoff(NOW + 20)                                               # no grant: no checkout, job fails
        self.assertEqual(self.fake_checkout.calls, 0)
        self.assertEqual(self.app.store.get(job["id"])["status"], "failed")
        self.assertEqual(self.launcher("GET", "/launch/pending"), (200, {"pending": []}))
        status, _ = self.launcher("POST", f"/jobs/{job['id']}/launch/claim", {})
        self.assertEqual(status, 403)


class AuthorizeCliTests(unittest.TestCase):
    """scripts/pipeline/prqa_authorize_run.py: key provisioning without echo, token to a mode-600 file only, gh pre-check."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("prqa_authorize_run", ROOT / "scripts" / "pipeline" / "prqa_authorize_run.py")
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)  # type: ignore[union-attr]
        self.tmp = tempfile.TemporaryDirectory()
        self.secrets = Path(self.tmp.name) / "secrets.env"
        self.secrets.write_text("GROQ_API_KEY=keep-me-verbatim\n")
        self.env = mock.patch.dict("os.environ", {"PRQA_SECRETS_FILE": str(self.secrets)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, *argv):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.cli.main(list(argv))
        return rc, buf.getvalue()

    def test_init_key_is_idempotent_and_silent(self):
        rc, out = self.run_cli("--init-key")
        key = self.cli.read_key(self.secrets)
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertNotIn(key, out)
        self.assertEqual(json.loads(out)["PRQA_RUN_GRANT_KEY"], "generated")
        self.assertEqual(self.secrets.stat().st_mode & 0o777, 0o600)
        self.assertIn("GROQ_API_KEY=keep-me-verbatim", self.secrets.read_text())
        rc, out = self.run_cli("--init-key")
        self.assertEqual((json.loads(out)["PRQA_RUN_GRANT_KEY"], self.cli.read_key(self.secrets)), ("present", key))

    def test_mint_writes_token_file_only(self):
        self.run_cli("--init-key")
        key = self.cli.read_key(self.secrets)
        rc, out = self.run_cli("--repo", REPO, "--pr", "7", "--sha", SHA, "--ttl", "300")
        info = json.loads(out)
        token = Path(info["token_file"]).read_text()
        self.assertNotIn(token, out)
        self.assertNotIn(key, out)
        self.assertEqual(Path(info["token_file"]).stat().st_mode & 0o777, 0o600)
        claims = run_grants.verify(token, key, now=info["grant"]["issued_at"])
        self.assertEqual((claims["repository"], claims["pr_number"], claims["head_sha"]), (REPO, 7, SHA))

    def test_mint_refuses_without_key(self):
        with self.assertRaises(SystemExit):
            self.cli.main(["--repo", REPO, "--pr", "7", "--sha", SHA])

    def test_github_precheck_refuses_moved_head_draft_fork_and_missing_label(self):
        import subprocess as sp

        def runner_for(**over):
            pr = {"state": "open", "draft": False, "title": "t", "html_url": "u", "labels": [{"name": "to be test"}],
                  "head": {"sha": SHA, "repo": {"full_name": REPO}}, "base": {"ref": "Dev"}}
            pr.update(over)
            calls = []

            def run(argv, **kw):
                calls.append(argv)
                return sp.CompletedProcess(argv, 0, json.dumps(pr), "")
            run.calls = calls
            return run
        ok = runner_for()
        self.assertTrue(self.cli.verify_on_github(REPO, 7, SHA, runner=ok)["labelled"])
        self.assertEqual(ok.calls[0][:4], ["gh", "api", "--method", "GET"])
        for over in ({"head": {"sha": "c" * 40, "repo": {"full_name": REPO}}}, {"draft": True}, {"state": "closed"},
                     {"head": {"sha": SHA, "repo": {"full_name": "x/fork"}}}, {"labels": [{"name": "To Be Test"}]}):
            with self.assertRaises(SystemExit):
                self.cli.verify_on_github(REPO, 7, SHA, runner=runner_for(**over))


if __name__ == "__main__":
    unittest.main()
