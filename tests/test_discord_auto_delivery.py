"""Trusted host-side Discord delivery for automatic PRQA runs + masked secret configuration.

Offline: the Discord endpoint is a recording fake opener; the webhook URL is synthetic. Nothing reaches the network.

Run: cd "<project>" && PYTHONPATH=worker python3 -m unittest tests.test_discord_auto_delivery -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))

from tests.test_auto_runner import A, AutoBase, FakeDiscord, R, REJECT, SHA, WEBHOOK  # noqa: E402


def load(name):
    spec = importlib.util.spec_from_file_location(name, next((ROOT / "scripts").rglob(f"{name}.py")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


DD = load("prqa_discord_delivery")
CS = load("prqa_configure_secret")

RESULT = {
    "result_version": "1.0", "synthetic": False, "job_id": "20261027-abc", "repository": R.REPO,
    "pr": {"number": 7, "title": "feat(org): working days @everyone", "head_sha": SHA, "head_ref": "feat/x", "base_ref": "Dev"},
    "environment": {"name": "local", "base_url_redacted": "http://prqa-target-api:8000"}, "executed_by": {"engine": "opencode", "mode": "live"},
    "summary": {"overall_status": "FAIL", "total": 3, "passed": 1, "failed": 1, "blocked": 1, "not_applicable": 0, "executive_summary": "one defect"},
    "coverage_matrix": [], "test_cases": [],
    "defects": [{"id": "DEF-001", "title": "PATCH accepts invalid day", "severity": "P2", "severity_rationale": "validation gap",
                 "endpoint": "PATCH /api/v1/organizations/{organization_id}", "preconditions": "synthetic org",
                 "repro_curl": "curl -sS -X PATCH -H \"Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcDEF123\" -H \"Cookie: session=abc123def456\" "
                               "-d '{\"working_days\":[\"xyz\"],\"password\":\"Sup3rSecret!\"}' $BASE_URL/api/v1/organizations/1",
                 "expected": "422", "actual": "200 with working_days [\"xyz\"]", "http_status": 200, "impact": "bad data", "origin": "introduced_by_pr",
                 "test_case_ids": ["ORGANI-UPDATE-002"]}],
    "blockers": [], "regression_summary": "ok", "recommendation": {"verdict": "FAIL", "rationale": "defect"},
    "evidence": {"directory": "e", "files": []}, "report_markdown": "# r"}
PV = {"repository": R.REPO, "pr_number": 7, "pr_title": "feat(org) @here", "head_sha": SHA, "pr_url": f"https://github.com/{R.REPO}/pull/7",
      "prompt_sha256": "a" * 64, "prompt_source": "reference_fallback"}
ENTRY = {"key": A.run_key(7, SHA), "pr": 7, "sha": SHA, "status": "executed", "job_id": "20261027-abc", "cleanup": "ok",
         "run_dir": f"runs/prqa-local-pr-runs/acme-pr-7-{SHA}"}


class ValidationTests(unittest.TestCase):
    def test_only_https_discord_webhooks_are_accepted(self):
        self.assertTrue(DD.valid_webhook(WEBHOOK))
        self.assertTrue(DD.valid_webhook(WEBHOOK.replace("discord.com", "discordapp.com")))
        for bad in ("http://discord.com/api/webhooks/123456789012/abcdefghijklmnopqrstuvwxyz",
                    "https://evil.example/api/webhooks/123456789012/abcdefghijklmnopqrstuvwxyz",
                    "https://discord.com.evil.example/api/webhooks/123456789012/abcdefghijklmnopqrstuvwxyz",
                    "https://localhost/api/webhooks/123456789012/abcdefghijklmnopqrstuvwxyz",
                    "https://127.0.0.1/api/webhooks/123456789012/abcdefghijklmnopqrstuvwxyz",
                    "https://discord.com/api/webhooks/123456789012/abcdefghijklmnopqrstuvwxyz?wait=true",
                    "https://discord.com/api/channels/123456789012/messages", WEBHOOK + "#x", ""):
            self.assertFalse(DD.valid_webhook(bad), bad)

    def test_redirects_are_refused_not_followed(self):
        import urllib.error
        h = DD._NoRedirect()
        req = mock.Mock(full_url=WEBHOOK)
        with self.assertRaises(urllib.error.HTTPError):
            h.redirect_request(req, None, 302, "Found", {}, "https://evil.example/x")


class RenderingTests(unittest.TestCase):
    def test_messages_are_sanitized_and_mentions_disabled(self):
        msgs = DD.build_messages(result=RESULT, pv=PV, entry=ENTRY, prompt={"source": "reference_fallback", "score": 98.0, "accepted": True})
        blob = json.dumps(msgs)
        for secret in ("eyJhbGciOiJIUzI1NiJ9", "abc123def456", "Sup3rSecret!", WEBHOOK):
            self.assertNotIn(secret, blob)
        self.assertNotIn("@everyone", blob.replace("@\u200beveryone", ""))
        for m in msgs:
            self.assertEqual(m["allowed_mentions"], {"parse": []})
        self.assertIn("DEF-001", blob)
        self.assertIn("PATCH /api/v1/organizations", blob)
        self.assertIn("acme-pr-7-", blob)                        # artifact id, not a link
        self.assertNotIn("file://", blob)
        self.assertIn("Cleanup", blob)

    def test_no_defects_wording_never_claims_defect_free(self):
        res = dict(RESULT, defects=[], summary=dict(RESULT["summary"], overall_status="PASS", failed=0))
        blob = json.dumps(DD.build_messages(result=res, pv=PV, entry=ENTRY, prompt={}))
        self.assertIn("No defects were identified within the executed test coverage.", blob)
        self.assertNotIn("defect-free", blob)

    def test_blocked_notice_has_no_result_claim(self):
        e = dict(ENTRY, status="blocked", reason="QUALITY_GATE_REJECTED: score 70", job_id=None)
        msgs = DD.build_messages(result=None, pv=PV, entry=e, prompt={})
        self.assertEqual(len(msgs), 1)
        self.assertIn("no test was executed", json.dumps(msgs))
        self.assertIn("QUALITY_GATE_REJECTED", json.dumps(msgs))


class DeliveryTests(unittest.TestCase):
    def msgs(self):
        return DD.build_messages(result=RESULT, pv=PV, entry=ENTRY, prompt={})

    def test_delivery_is_idempotent_per_delivery_id(self):
        fake, ledger = FakeDiscord(), {}
        r1 = DD.deliver(url=WEBHOOK, delivery_id="k:job", ledger=ledger, messages=self.msgs(), opener=fake, sleep=lambda s: None)
        n = len(fake.posts)
        r2 = DD.deliver(url=WEBHOOK, delivery_id="k:job", ledger=ledger, messages=self.msgs(), opener=fake, sleep=lambda s: None)
        self.assertTrue(r1["delivered"])
        self.assertEqual(r2["skipped"], "already delivered")
        self.assertEqual(len(fake.posts), n)
        self.assertEqual({p["host"] for p in fake.posts}, {"discord.com"})

    def test_rate_limit_is_retried_and_failure_is_recorded(self):
        slept = []
        fake = FakeDiscord([429, 204] + [204] * 5)
        r = DD.deliver(url=WEBHOOK, delivery_id="a", ledger={}, messages=self.msgs(), opener=fake, sleep=slept.append)
        self.assertTrue(r["delivered"])
        fake = FakeDiscord([500, 500, 500])
        ledger = {}
        r = DD.deliver(url=WEBHOOK, delivery_id="b", ledger=ledger, messages=self.msgs(), opener=fake, sleep=lambda s: None)
        self.assertFalse(r["delivered"])
        self.assertEqual(r["status"], "failed")
        self.assertNotIn(WEBHOOK, json.dumps(ledger))
        # a failed delivery may be retried later (not marked delivered); a delivered one never
        r = DD.deliver(url=WEBHOOK, delivery_id="b", ledger=ledger, messages=self.msgs(), opener=FakeDiscord(), sleep=lambda s: None)
        self.assertTrue(r["delivered"])

    def test_missing_or_invalid_url_never_sends(self):
        fake = FakeDiscord()
        self.assertEqual(DD.deliver(url="", delivery_id="x", ledger={}, messages=self.msgs(), opener=fake)["status"], "not_configured")
        self.assertEqual(DD.deliver(url="https://evil.example/api/webhooks/1/x", delivery_id="y", ledger={}, messages=self.msgs(), opener=fake)["status"],
                         "invalid_webhook_url")
        self.assertEqual(fake.posts, [])

    def test_setup_test_sends_exactly_one_labeled_message(self):
        fake = FakeDiscord()
        r = DD.setup_test(WEBHOOK, opener=fake)
        self.assertEqual((r["ok"], r["status_code"]), (True, 204))
        self.assertEqual(len(fake.posts), 1)
        self.assertEqual(fake.posts[0]["body"]["content"], DD.SETUP_TEST_TEXT)
        self.assertEqual(set(r), {"ok", "status_code", "at"})


class AutoRunnerDiscordTests(AutoBase):
    def test_blocked_run_sends_one_sanitized_notice_and_verdict_unchanged(self):
        host = self.host()
        with mock.patch.object(R, "score_prompt", return_value=REJECT):
            res = self.once(host)
        entry = res["processed"]
        self.assertEqual(entry["status"], "blocked")
        self.assertEqual(entry["discord"]["status"], "delivered")
        self.assertEqual(len(self.discord.posts), 1)
        self.assertIn("QUALITY_GATE_REJECTED", json.dumps(self.discord.posts))
        self.once(host)                                             # duplicate poll: no second message
        self.assertEqual(len(self.discord.posts), 1)

    def test_discord_failure_does_not_change_verdict_or_rerun(self):
        self.discord.codes = [500] * 10
        host = self.host()
        with mock.patch.object(R, "score_prompt", return_value=REJECT):
            res = self.once(host)
        self.assertEqual(res["processed"]["status"], "blocked")
        self.assertEqual(res["processed"]["discord"]["status"], "failed")
        self.once(host)
        self.assertEqual(len(self.state()["keys"]), 1)
        self.assertTrue((R.RUNS_DIR / f"acme-pr-7-{SHA}" / "report.md").is_file())

    def test_missing_or_untested_discord_blocks_before_anything_starts(self):
        host = self.host(webhook="")
        res = self.once(host)
        self.assertTrue(any("DISCORD_WEBHOOK_URL" in b for b in res["blockers"]))
        self.assert_nothing_started(host)
        (self.auto_dir / "discord-setup-test.json").write_text(json.dumps({"ok": False, "status_code": 404}))
        host = self.host()
        res = self.once(host)
        self.assertTrue(any("setup test" in b for b in res["blockers"]))
        self.assert_nothing_started(host)

    def test_webhook_never_reaches_worker_sandbox_or_artifacts(self):
        host = self.host()
        seen = []
        real = host.worker

        def spy(method, path, body=None):
            seen.append(json.dumps(body))
            return real(method, path, body)
        host.worker = spy
        self.once(host)
        self.assertEqual(len(self.jobs()), 1)
        blobs = seen + [self.out, (self.state_dir / "state.json").read_text()]
        for d in (R.RUNS_DIR, self.auto_dir):
            blobs += [p.read_text(errors="ignore") for p in d.rglob("*") if p.is_file()]
        self.assertNotIn(WEBHOOK.rsplit("/", 1)[1], "\n".join(blobs))
        src = (ROOT / "worker" / "prqa_worker" / "opencode_runner.py").read_text()
        self.assertIn('"DISCORD_WEBHOOK_URL"', src)                 # stripped from the sandbox environment


class ConfigureSecretTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name) / "prqa"
        d.mkdir(mode=0o700)
        self.path = d / "secrets.env"
        self.path.write_text("GROQ_API_KEY=keep-me\nDISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1/old\nOTHER=x\n")
        os.chmod(self.path, 0o600)
        self.env = mock.patch.dict(os.environ, {"PRQA_SECRETS_FILE": str(self.path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, argv, value):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = CS.main(argv, prompt=lambda _p: value)
        return rc, buf.getvalue()

    def test_discord_url_replaces_without_duplicates_and_is_never_printed(self):
        rc, out = self.run_cli(["discord"], WEBHOOK)
        self.assertEqual(rc, 0)
        text = self.path.read_text()
        self.assertEqual(text.count("DISCORD_WEBHOOK_URL="), 1)
        self.assertIn(f"DISCORD_WEBHOOK_URL={WEBHOOK}", text)
        self.assertIn("GROQ_API_KEY=keep-me", text)
        self.assertIn("OTHER=x", text)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        baks = list(self.path.parent.glob("secrets.env.bak-*"))
        self.assertEqual(len(baks), 1)
        self.assertEqual(stat.S_IMODE(baks[0].stat().st_mode), 0o600)
        self.assertNotIn(WEBHOOK, out)
        self.assertNotIn(WEBHOOK.rsplit("/", 1)[1], out)

    def test_invalid_discord_url_is_refused_and_nothing_changes(self):
        before = self.path.read_text()
        rc, out = self.run_cli(["discord"], "https://evil.example/api/webhooks/123456789012/abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(rc, 2)
        self.assertEqual(self.path.read_text(), before)
        self.assertNotIn("evil.example", out)

    def test_github_token_requires_fine_grained_and_verified_reads(self):
        before = self.path.read_text()
        rc, _ = self.run_cli(["github"], "ghp_" + "a" * 36)
        self.assertEqual(rc, 2)
        token = "github_pat_" + "A" * 30
        with mock.patch.object(CS, "verify_github", return_value={"ok": False, "repository": 404}):
            rc, out = self.run_cli(["github"], token)
        self.assertEqual(rc, 2)
        self.assertEqual(self.path.read_text(), before)
        with mock.patch.object(CS, "verify_github", return_value={"ok": True, "repository": 200, "pulls": 200, "contents": 200}):
            rc, out = self.run_cli(["github"], token)
        self.assertEqual(rc, 0)
        self.assertIn(f"GITHUB_TOKEN={token}", self.path.read_text())
        self.assertNotIn(token, out)

    def test_no_terminal_means_no_prompt(self):
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(sys.stdin, "isatty", return_value=False):
            rc = CS.main(["discord"])
        self.assertEqual(rc, 2)
        self.assertIn("own terminal", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
