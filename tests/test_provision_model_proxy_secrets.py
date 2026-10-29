"""scripts/setup/provision_model_proxy_secrets.py — offline, synthetic values in a private temp dir; nothing is ever printed.

    PYTHONPATH=worker python3 -m unittest tests.test_provision_model_proxy_secrets -v
"""
from __future__ import annotations

import contextlib
import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "setup"))

import provision_model_proxy_secrets as pm  # noqa: E402

KEY = "gsk_SYNTHETIC_provision_test_key_0000000000000000000000"
KEY2 = "gsk_SYNTHETIC_provision_test_key_rotated_111111111111111"


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="prqa-prov-", dir=os.environ.get("XDG_RUNTIME_DIR") or None)
        self.dir = Path(self.tmp.name)
        self.secrets = self.dir / "secrets.env"
        self.write(f"# comment kept\nGROQ_API_KEY={KEY}\nOTHER=value kept\n")
        self.fake_root = self.dir / "project"; self.fake_root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text, mode=0o600):
        self.secrets.write_text(text); self.secrets.chmod(mode)

    def run_provision(self, check=False):
        return pm.provision(self.secrets, check_only=check, project_root=self.fake_root)

    def env(self):
        return pm.parse(self.secrets.read_text())

    def test_creates_key_file_and_token_without_printing(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = self.run_provision()
        self.assertEqual((status["provider_key_file"], status["admin_token"], status["secrets_file_entries"]), ("created", "generated", "updated"))
        key_file = self.dir / "model-provider.key"
        self.assertEqual(key_file.read_text(), KEY)
        self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600); self.assertEqual(stat.S_IMODE(self.secrets.stat().st_mode), 0o600)
        env = self.env()
        self.assertEqual(env["PRQA_MODEL_PROVIDER_KEY_FILE"], str(key_file)); self.assertEqual(env["PRQA_MODEL_PROXY_UID"], str(os.getuid()))
        token = env["PRQA_MODEL_PROXY_ADMIN_TOKEN"]; launcher = env["PRQA_LAUNCHER_TOKEN"]
        self.assertRegex(token, r"^[A-Za-z0-9_-]{64}$"); self.assertNotEqual(token, KEY)
        self.assertEqual(status["launcher_token"], "generated")
        self.assertRegex(launcher, r"^[A-Za-z0-9_-]{64}$"); self.assertNotIn(launcher, (KEY, token), "independent credentials")
        self.assertIn("# comment kept", self.secrets.read_text()); self.assertEqual(env["OTHER"], "value kept"); self.assertEqual(env["GROQ_API_KEY"], KEY)
        printed = out.getvalue() + err.getvalue() + repr(status)
        for secret in (KEY, token, launcher):
            self.assertNotIn(secret, printed)
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.endswith(".tmp")], [], "no temp file left behind")
        bdir = self.dir / "budget"
        self.assertEqual((status["budget_dir"], env["PRQA_PROVIDER_BUDGET_DIR"], oct(stat.S_IMODE(bdir.stat().st_mode))), ("created (700)", str(bdir), "0o700"))

    def test_budget_dir_must_stay_private(self):
        self.run_provision(); bdir = self.dir / "budget"
        (bdir / "ledger.json").write_text("{}"); self.run_provision()
        self.assertEqual((bdir / "ledger.json").read_text(), "{}", "an existing ledger is never touched")
        bdir.chmod(0o755)
        self.assertFalse(pm.is_ready(self.run_provision(check=True)))
        with self.assertRaises(pm.ProvisionError):
            self.run_provision()
        bdir.chmod(0o700); self.assertTrue(pm.is_ready(self.run_provision(check=True)))

    def test_second_run_preserves_everything(self):
        self.run_provision()
        token = self.env()["PRQA_MODEL_PROXY_ADMIN_TOKEN"]; launcher = self.env()["PRQA_LAUNCHER_TOKEN"]; before = self.secrets.read_bytes()
        status = self.run_provision()
        self.assertEqual(status["launcher_token"], "preserved"); self.assertEqual(self.env()["PRQA_LAUNCHER_TOKEN"], launcher)
        self.assertEqual((status["provider_key_file"], status["admin_token"], status["secrets_file_entries"]), ("unchanged", "preserved", "unchanged"))
        self.assertEqual(self.env()["PRQA_MODEL_PROXY_ADMIN_TOKEN"], token); self.assertEqual(self.secrets.read_bytes(), before)
        self.assertTrue(pm.is_ready(self.run_provision(check=True)))

    def test_key_rotation_resynchronises_the_key_file_only(self):
        self.run_provision(); token = self.env()["PRQA_MODEL_PROXY_ADMIN_TOKEN"]
        self.write(self.secrets.read_text().replace(KEY, KEY2))
        self.assertFalse(pm.is_ready(self.run_provision(check=True)))
        self.assertEqual(self.run_provision()["provider_key_file"], "updated")
        self.assertEqual((self.dir / "model-provider.key").read_text(), KEY2); self.assertEqual(self.env()["PRQA_MODEL_PROXY_ADMIN_TOKEN"], token)

    def test_invalid_or_reused_admin_token_is_replaced(self):
        for bad in ("short", KEY):
            self.write(f"GROQ_API_KEY={KEY}\nPRQA_MODEL_PROXY_ADMIN_TOKEN={bad}\n")
            self.assertTrue(self.run_provision()["admin_token"].startswith("replaced"))
            self.assertNotIn(self.env()["PRQA_MODEL_PROXY_ADMIN_TOKEN"], (bad, KEY))

    def test_invalid_or_reused_launcher_token_is_replaced(self):
        self.run_provision(); admin = self.env()["PRQA_MODEL_PROXY_ADMIN_TOKEN"]
        for bad in ("short", KEY, admin, "has spaces in it " * 3):
            text = "\n".join(l for l in self.secrets.read_text().splitlines() if not l.startswith("PRQA_LAUNCHER_TOKEN="))
            self.write(text + f"\nPRQA_LAUNCHER_TOKEN={bad}\n")
            self.assertFalse(pm.is_ready(self.run_provision(check=True)), bad)
            self.assertTrue(self.run_provision()["launcher_token"].startswith("replaced"), bad)
            self.assertNotIn(self.env()["PRQA_LAUNCHER_TOKEN"], (bad, KEY, admin))
            self.assertEqual(self.env()["PRQA_MODEL_PROXY_ADMIN_TOKEN"], admin, "the admin token is not rotated")

    def test_mint_and_prompt_tokens_are_distinct_least_privilege_credentials(self):
        status = self.run_provision(); env = self.env()
        self.assertEqual((status["mint_token"], status["prompt_token"]), ("generated", "generated"))
        names = ("PRQA_MODEL_PROXY_ADMIN_TOKEN", "PRQA_LAUNCHER_TOKEN", "PRQA_MODEL_PROXY_MINT_TOKEN", "PRQA_MODEL_PROXY_PROMPT_TOKEN")
        values = [env[n] for n in names]
        self.assertEqual(len(set(values + [KEY])), 5, "four independent credentials, none equal to the provider key")
        self.assertNotIn(env["PRQA_MODEL_PROXY_MINT_TOKEN"], repr(status)); self.assertNotIn(env["PRQA_MODEL_PROXY_PROMPT_TOKEN"], repr(status))
        self.assertTrue(pm.is_ready(self.run_provision(check=True)))
        # an existing secrets file without the new entries is not ready until provisioned (preserving the others)
        text = "\n".join(l for l in self.secrets.read_text().splitlines() if not l.startswith("PRQA_MODEL_PROXY_MINT_TOKEN="))
        self.write(text + f"\nPRQA_MODEL_PROXY_MINT_TOKEN={env['PRQA_MODEL_PROXY_ADMIN_TOKEN']}\n")
        self.assertFalse(pm.is_ready(self.run_provision(check=True)), "a mint token equal to the admin token is refused")
        self.assertTrue(self.run_provision()["mint_token"].startswith("replaced"))
        after = self.env()
        self.assertEqual([after[n] for n in names if n != "PRQA_MODEL_PROXY_MINT_TOKEN"], [v for n, v in zip(names, values) if n != "PRQA_MODEL_PROXY_MINT_TOKEN"])
        self.assertNotEqual(after["PRQA_MODEL_PROXY_MINT_TOKEN"], after["PRQA_MODEL_PROXY_ADMIN_TOKEN"])

    def test_check_mode_changes_nothing(self):
        before = self.secrets.read_bytes()
        status = self.run_provision(check=True)
        self.assertFalse(pm.is_ready(status)); self.assertEqual(self.secrets.read_bytes(), before)
        self.assertFalse((self.dir / "model-provider.key").exists())

    def test_fails_closed(self):
        cases = [("GROQ_API_KEY=\n", 0o600, "missing"), ("OTHER=1\n", 0o600, "missing"), (f"GROQ_API_KEY={KEY} trailing\n", 0o600, "printable"),
                 (f"GROQ_API_KEY={KEY}\n", 0o640, "group/others"), (f"GROQ_API_KEY={KEY}\n", 0o604, "group/others")]
        for text, mode, why in cases:
            self.write(text, mode)
            with self.assertRaises(pm.ProvisionError, msg=why) as ctx:
                self.run_provision()
            self.assertIn(why, str(ctx.exception)); self.assertNotIn(KEY, str(ctx.exception))
        self.write(f"GROQ_API_KEY={KEY}\n")
        with self.assertRaises(pm.ProvisionError):                       # inside the project tree
            pm.provision(self.secrets, project_root=self.dir)
        (self.dir / "model-provider.key").symlink_to(self.dir / "elsewhere")
        with self.assertRaises(pm.ProvisionError):
            self.run_provision()
        self.assertFalse((self.dir / "elsewhere").exists(), "never writes through a symlink")

    def test_cli_prints_statuses_only(self):
        env = {**os.environ, "PRQA_SECRETS_FILE": str(self.secrets)}
        p = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/setup/provision_model_proxy_secrets.py")], env=env, capture_output=True, text=True, timeout=60)
        # the real project root is not an ancestor of the temp dir, so the CLI provisions it
        self.assertEqual(p.returncode, 0, p.stderr)
        token = self.env()["PRQA_MODEL_PROXY_ADMIN_TOKEN"]
        for secret in (KEY, token):
            self.assertNotIn(secret, p.stdout + p.stderr)
        p = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/setup/provision_model_proxy_secrets.py"), "--check"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.write("GROQ_API_KEY=\n")
        p = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/setup/provision_model_proxy_secrets.py")], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 3); self.assertIn("NOT ready", p.stderr)

    def test_fuse_or_project_drive_is_refused(self):
        if pm._fs_type(ROOT) in pm.UNSAFE_FS:                            # the project drive (fuseblk here) cannot hold credentials
            with self.assertRaises(pm.ProvisionError):
                pm._check_private_location(ROOT / "runs" / "x.key", Path("/nonexistent-root"))


if __name__ == "__main__":
    unittest.main()
