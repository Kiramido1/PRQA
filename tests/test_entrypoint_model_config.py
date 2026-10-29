"""worker/sandbox/entrypoint.sh — the PRQA model proxy provider configuration fails closed (local; no Docker, no OpenCode).

The real entrypoint runs against a really packed + extracted checkout; every case below must stop with exit 67 BEFORE
the OpenCode line (no OpenCode exists on this host, so reaching it would instead print the INCONCLUSIVE fallback).
The positive path (config consumed by the real OpenCode 1.19.3 and by the stub) is covered by
tests/test_opencode_proxy_auth.py and tests/test_phase2_containers.py.

    PYTHONPATH=worker python3 -m unittest tests.test_entrypoint_model_config -v
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from prqa_worker import opencode_runner  # noqa: E402
from prqa_worker.workspace import pack_checkout_artifact  # noqa: E402

ENTRY = ROOT / "worker" / "sandbox" / "entrypoint.sh"
VERIFIER = ROOT / "worker" / "sandbox" / "verify_checkout.py"
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
JOB_ID = "20261027-entry0000001"
PROMPT = "# PRQA prompt\n" + ("Test the orders API with curl against $BASE_URL using $TOKEN. " * 8)
TOKEN = "prqa_mt_" + "A" * 43
GOOD_URL = "http://prqa-model-proxy:8089/v1"


@unittest.skipUnless(shutil.which("bash") and shutil.which("python3"), "bash/python3 required")
class EntrypointModelConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        repo = base / "repo"; (repo / "src").mkdir(parents=True); (repo / "src" / "app.py").write_text("print('hi')\n")
        ws = base / JOB_ID; ws.mkdir()
        m = pack_checkout_artifact(repo_dir=repo, out_path=ws / "checkout.tar", job_id=JOB_ID, repository="example-org/svc", head_sha=SHA, prompt_text=PROMPT)
        cls.dest = base / "vol"; cls.dest.mkdir()
        p = subprocess.run([sys.executable, "-I", str(ROOT / "worker/sandbox/populate_checkout.py"), str(cls.dest), JOB_ID],
                           input=(ws / "checkout.tar").read_bytes(), capture_output=True, timeout=60)
        assert p.returncode == 0, p.stderr
        cls.hashes = (m["tree_sha256"], m["prompt_sha256"])
        cls.ro_dirs = [d for d in sorted(cls.dest.rglob("*")) if d.is_dir()]
        for d in reversed(cls.ro_dirs):                     # read-only like the sandbox's volume mount
            d.chmod(0o555)

    @classmethod
    def tearDownClass(cls):
        for d in cls.ro_dirs:
            d.chmod(0o755)
        cls.tmp.cleanup()

    def run_entry(self, model, **env_over):
        evidence = Path(self.tmp.name) / "evidence"
        env = {"PATH": "/usr/bin:/bin", "PRQA_JOB_ID": JOB_ID, "PRQA_VERIFIER": str(VERIFIER), "PRQA_CHECKOUT_DIR": str(self.dest),
               "PRQA_EVIDENCE_DIR": str(evidence), **env_over}
        args = ["--repo", "example-org/svc", "--sha", SHA, "--model", model, "--timeout-minutes", "1",
                "--tree-sha256", self.hashes[0], "--prompt-sha256", self.hashes[1], "--prechecked"]
        return subprocess.run(["bash", str(ENTRY), *args], env={k: v for k, v in env.items() if v is not None}, capture_output=True, text=True, timeout=120)

    def assert_refused(self, proc, text):
        self.assertEqual(proc.returncode, 67, proc.stderr + proc.stdout)
        self.assertIn(text, proc.stderr)
        self.assertNotIn("INCONCLUSIVE", proc.stdout, "stopped before the OpenCode invocation")
        for secret in (TOKEN,):
            self.assertNotIn(secret, proc.stdout + proc.stderr)

    def test_proxy_model_without_a_job_token_is_refused(self):
        self.assert_refused(self.run_entry("prqa/openai/gpt-oss-120b"), "no per-job model token")

    def test_malformed_job_token_is_refused(self):
        for bad in ("Bearer x", "prqa_mt_short", "prqa_mt_" + "A" * 43 + " x", "gsk_" + "A" * 50, TOKEN + "\nOPENCODE_X=1"):
            self.assert_refused(self.run_entry("prqa/openai/gpt-oss-120b", PRQA_MODEL_TOKEN=bad, PRQA_MODEL_BASE_URL=GOOD_URL), "PRQA_MODEL_TOKEN is malformed")

    def test_inference_url_must_be_the_plain_proxy_v1_endpoint(self):
        for bad in (None, "https://prqa-model-proxy:8089/v1", "http://prqa-model-proxy:8089/v1/", "http://prqa-model-proxy:8089/admin",
                    "http://user:pw@prqa-model-proxy:8089/v1", "http://api.groq.com/openai/v1", "http://prqa-model-proxy:8089/v1?x=1",
                    "http://prqa-model-proxy:8089/v1#x", "http://PRQA:8089/v1", 'http://x/v1","apiKey":"leak'):
            self.assert_refused(self.run_entry("prqa/openai/gpt-oss-120b", PRQA_MODEL_TOKEN=TOKEN, PRQA_MODEL_BASE_URL=bad), "PRQA_MODEL_BASE_URL")

    def test_model_must_be_served_by_the_proxy_when_a_token_is_present(self):
        for bad in ("openrouter/z-ai/glm-5.3-flash:free", "openai/gpt-oss-120b", "prqa/", 'prqa/x","npm":"evil'):
            self.assert_refused(self.run_entry(bad, PRQA_MODEL_TOKEN=TOKEN, PRQA_MODEL_BASE_URL=GOOD_URL), "model must be prqa/")

    def test_evidence_directory_must_be_directly_under_work(self):
        self.assert_refused(self.run_entry("prqa/openai/gpt-oss-120b", PRQA_MODEL_TOKEN=TOKEN, PRQA_MODEL_BASE_URL=GOOD_URL), "PRQA_EVIDENCE_DIR")

    def test_static_contract_of_the_generated_configuration(self):
        text = ENTRY.read_text(encoding="utf-8")
        self.assertIn('"apiKey": "{env:PRQA_MODEL_TOKEN}"', text, "the token is referenced, never embedded")
        self.assertIn('"npm": "@ai-sdk/openai-compatible"', text); self.assertIn('"enabled_providers": ["prqa"]', text)
        self.assertIn('"external_directory": {"*": "deny", evidence + "/*": "allow"}', text)
        self.assertIn('"output": 1500', text)
        # OpenCode runs only inside prqa-chunked-run, which is itself started under `env -i` with the allowlist and passes
        # that (allowlisted) environment on to each OpenCode session.
        self.assertRegex(text, r'env -i "\$\{OPENCODE_SANDBOX_ENV\[@\]\}" python3 -I /usr/local/bin/prqa-chunked-run')
        allow = re.search(r"OPENCODE_SANDBOX_ENV=\((.*?)\)\n", text, re.S).group(1)
        self.assertNotIn("PRQA_MODEL_BASE_URL", allow)
        self.assertEqual(re.findall(r"(?m)^(?!\s*#).*\bopencode run", text), [], "no OpenCode invocation outside the env -i'd executor")
        runner = (ENTRY.parent / "chunked_run.py").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r'\["opencode", "run", "--pure"', runner)), 1, "exactly one OpenCode invocation in the executor")
        # the runner's sandbox env and the policy proxy's allowlist are unchanged by this: no new key reaches the container
        self.assertEqual(set(opencode_runner.ALLOWED_ENV), {"BASE_URL", "PRQA_JOB_ID", "PRQA_JOB_JSON", "PRQA_EVIDENCE_DIR", "PRQA_TEST_LEVEL", "PRQA_MODEL_BASE_URL"})
        self.assertEqual(json.dumps(opencode_runner.FIXED_SANDBOX_ENV, sort_keys=True), json.dumps({"OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_PURE": "1"}, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
