"""scripts/setup/check_delivery_readiness.py — offline, synthetic values only, never printed.

    python3 -m unittest tests.test_delivery_readiness -v
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_delivery_readiness", ROOT / "scripts/setup/check_delivery_readiness.py")
dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dr)

FINE = "github_pat_" + "S" * 22 + "_" + "Y" * 59          # synthetic, fine-grained shape
CLASSIC = "ghp_" + "S" * 36                                # synthetic, classic shape
HOOK = "https://discord.com/api/webhooks/123456789012345678/" + "s" * 68
HEX = "ab" * 32


class DeliveryReadinessTests(unittest.TestCase):
    def test_absent_credentials_mean_mock_only_without_a_finding(self):
        r = dr.check({}, {}, None)
        self.assertEqual(r["GITHUB_TOKEN source"], "absent (real delivery not configured; mock sinks only)")
        self.assertEqual(r["DISCORD_WEBHOOK_URL source"], "absent (real delivery not configured; mock sinks only)")
        self.assertEqual((r["github delivery mode"], r["discord delivery mode"]), ("mock (review-only or flag off)",) * 2)
        self.assertFalse([v for v in r.values() if "FINDING" in v])

    def test_secrets_file_values_with_good_formats_pass(self):
        r = dr.check({}, {"GITHUB_TOKEN": FINE, "DISCORD_WEBHOOK_URL": HOOK, "GITHUB_WEBHOOK_SECRET": HEX}, 0o600)
        self.assertEqual(r["GITHUB_TOKEN source"], "secrets file")
        self.assertTrue(r["GITHUB_TOKEN format"].startswith("fine-grained PAT format"))
        self.assertTrue(r["DISCORD_WEBHOOK_URL format"].startswith("webhook URL format ok"))
        self.assertEqual(r["GITHUB_WEBHOOK_SECRET"], "64-hex")
        self.assertFalse([v for v in r.values() if "FINDING" in v])
        self.assertNotIn(FINE, repr(r)); self.assertNotIn(HOOK, repr(r))

    def test_findings(self):
        cases = {
            "compose": ({"GITHUB_TOKEN": FINE}, {}, None, "GITHUB_TOKEN source"),
            "mode": ({}, {"DISCORD_WEBHOOK_URL": HOOK}, 0o644, "DISCORD_WEBHOOK_URL source"),
            "classic": ({}, {"GITHUB_TOKEN": CLASSIC}, 0o600, "GITHUB_TOKEN format"),
            "http hook": ({}, {"DISCORD_WEBHOOK_URL": HOOK.replace("https", "http")}, 0o600, "DISCORD_WEBHOOK_URL format"),
            "query": ({}, {"DISCORD_WEBHOOK_URL": HOOK + "?wait=true"}, 0o600, "DISCORD_WEBHOOK_URL format"),
            "foreign host": ({}, {"DISCORD_WEBHOOK_URL": HOOK.replace("discord.com", "discord.com.evil.example")}, 0o600, "DISCORD_WEBHOOK_URL format"),
            "short secret": ({}, {"PRQA_WEBHOOK_SECRET": "abc"}, 0o600, "PRQA_WEBHOOK_SECRET"),
        }
        for label, (compose_env, secrets, mode, key) in cases.items():
            with self.subTest(case=label):
                self.assertIn("FINDING", dr.check(compose_env, secrets, mode)[key])

    def test_real_mode_needs_live_and_the_flag(self):
        self.assertEqual(dr.check({"REAL_DISCORD_REPORTING_ENABLED": "true"}, {}, None)["discord delivery mode"], "mock (review-only or flag off)")
        r = dr.check({"AUTOMATION_MODE": "live", "REAL_DISCORD_REPORTING_ENABLED": "true"}, {}, None)
        self.assertEqual((r["discord delivery mode"], r["github delivery mode"]), ("real", "mock (review-only or flag off)"))

    def test_main_prints_statuses_never_values(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "secrets.env"; f.write_text(f"GITHUB_TOKEN={FINE}\nDISCORD_WEBHOOK_URL={HOOK}\n"); f.chmod(0o600)
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"PRQA_SECRETS_FILE": str(f)}), mock.patch.object(dr, "ROOT", Path(d)), \
                    contextlib.redirect_stdout(out):
                rc = dr.main()
        self.assertEqual(rc, 0)
        self.assertNotIn(FINE, out.getvalue()); self.assertNotIn(HOOK, out.getvalue()); self.assertNotIn("123456789012345678", out.getvalue())


if __name__ == "__main__":
    unittest.main()
