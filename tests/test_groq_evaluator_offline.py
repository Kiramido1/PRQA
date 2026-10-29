"""Offline tests for scripts/eval/evaluate_groq_live.py: no network, no real key, artifacts in a temp folder.

The Groq evaluator drives the production request (Policy settings + build_llm_request) for the active provider and
shares outcome classification, truncation handling, redaction, archiving and version labels with the OpenRouter
evaluator; these tests pin that the Groq path applies them too, takes its key only from the secrets file and makes no
network call other than the generation request.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("evaluate_groq_live", ROOT / "scripts" / "eval" / "evaluate_groq_live.py")
gv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gv)

from prqa_worker.prompt_builder import Templates, reference_fallback_prompt  # noqa: E402
from tests.qa_benchmark import CASES, context_for  # noqa: E402

CID = "C01-clear-behaviour"
C01 = next(c for c in CASES if c.cid == CID)
FAKE_KEY = "gsk_" + "f" * 52                 # synthetic, Groq key shape; never a real key
OPENROUTER_SHAPED = "sk-or-v1-" + "0" * 64       # synthetic, OpenRouter key shape; never a real key
MODEL = "openai/gpt-oss-120b"
# compose/.env: non-secret configuration only (the key lives in the secrets file)
ENV = {"AUTOMATION_MODE": "review_only", "PROMPT_LLM_LIVE_IN_REVIEW_ONLY": "false",
       "PROMPT_LLM_API_BASE": gv.EXPECTED_BASE, "PROMPT_LLM_MODEL": MODEL, "PROMPT_LLM_MAX_TOKENS": "3000",
       "PROMPT_LLM_REASONING_EFFORT": "low",
       # generous offline budget (the real caps live in compose/.env); a private temp ledger per test
       "PRQA_BUDGET_DAILY_REQUESTS": "50", "PRQA_BUDGET_DAILY_TOKENS": "1000000", "PRQA_BUDGET_JOB_TOKENS": "100000",
       "PRQA_BUDGET_TOKENS_PER_MINUTE": "100000", "PRQA_BUDGET_MAX_IN_FLIGHT": "1", "PRQA_BUDGET_PROMPT_CHARS_PER_TOKEN": "3.6",
       "PRQA_BUDGET_MAX_WAIT_S": "0"}
PRODUCTION_SHAPE = {"model": MODEL, "temperature": 0.2, "max_completion_tokens": 3000, "stream": False,
                    "reasoning_effort": "low", "include_reasoning": False}


def body(content, finish="stop"):
    return {"id": "req-synthetic", "model": "synthetic-groq-model",
            "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}


class GroqEvaluatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t = Templates.load(ROOT / "prompts")
        cls.valid = reference_fallback_prompt(t, repository="synthetic-org/bench-repo", pr_number=C01.pr_number,
                                              test_level="standard", context=context_for(C01))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.chat_calls = 0
        self.env = dict(ENV)
        self.btmp = tempfile.TemporaryDirectory(); self.addCleanup(self.btmp.cleanup)     # outside OUT_DIR
        self.budget_dir = Path(self.btmp.name) / "budget"; self.budget_dir.mkdir(mode=0o700)
        self.secrets = {"GROQ_API_KEY": FAKE_KEY, "PRQA_PROVIDER_BUDGET_DIR": str(self.budget_dir)}
        for p in (mock.patch.object(gv, "OUT_DIR", self.out), mock.patch.object(gv, "BACKUP_DIR", self.out / "_backups"),
                  mock.patch.object(gv, "load_env_records", lambda: dict(self.env)),
                  mock.patch.object(gv, "load_secret_records", lambda: dict(self.secrets)),
                  mock.patch("urllib.request.urlopen", side_effect=AssertionError("network"))):
            p.start()
            self.addCleanup(p.stop)

    def run_main(self, response, *argv, status=200):
        self.payloads = []

        def fake_http(url, **kw):
            assert url.endswith("/chat/completions"), url
            self.chat_calls += 1
            self.payloads.append(kw)
            return status, response
        with mock.patch.object(gv, "http_json", side_effect=fake_http), \
                mock.patch.object(sys, "argv", ["x", *(argv or ("--only", CID))]), contextlib.redirect_stdout(io.StringIO()):
            return gv.main()

    def results(self):
        return json.loads((self.out / "results.json").read_text(encoding="utf-8"))

    def blocked(self, *argv):
        with self.assertRaises(SystemExit) as cm:
            self.run_main(body(self.valid), *argv)
        self.assertEqual(self.chat_calls, 0)
        return str(cm.exception.code)

    def ledger(self):
        return gv.pb.Ledger(gv.pb.BudgetConfig.from_env({**self.env, "PRQA_PROVIDER_BUDGET_DIR": str(self.budget_dir)}))

    def test_missing_or_invalid_budget_blocks_before_any_request(self):
        del self.secrets["PRQA_PROVIDER_BUDGET_DIR"]
        self.assertIn("provider budget not configured", self.blocked())
        self.secrets["PRQA_PROVIDER_BUDGET_DIR"] = str(self.budget_dir)
        self.env["PRQA_BUDGET_DAILY_REQUESTS"] = ""
        self.assertIn("provider budget not configured", self.blocked())
        self.env["PRQA_BUDGET_DAILY_REQUESTS"] = "50"; self.env["PRQA_BUDGET_PROMPT_CHARS_PER_TOKEN"] = "0"
        self.assertIn("provider budget not configured", self.blocked())

    def test_exhausted_or_too_small_budget_blocks_before_any_request(self):
        self.env["PRQA_BUDGET_DAILY_REQUESTS"] = "1"
        led = self.ledger(); rid = led.reserve(source="test", job_id="earlier", tokens=10, completion_allowance=1)
        led.settle(rid, total_tokens=10, outcome="ok")
        self.assertIn("remaining provider budget", self.blocked())
        self.env["PRQA_BUDGET_DAILY_REQUESTS"] = "50"; self.env["PRQA_BUDGET_TOKENS_PER_MINUTE"] = "5000"
        self.assertIn("remaining provider budget", self.blocked(), "C01 (prompt estimate + 3000) never fits 5000/min")

    def test_the_request_is_reserved_and_settled_with_the_reported_usage(self):
        self.assertEqual(self.run_main(body(self.valid)), 0)
        snap = self.ledger().snapshot()
        self.assertEqual((snap["requests_today"], snap["tokens_today"], snap["in_flight"]), (1, 30, 0))
        self.assertEqual(len(snap["jobs"]), 1); self.assertTrue(next(iter(snap["jobs"])).startswith(f"evaluator-{CID}-"))

    def test_payload_is_the_production_groq_shape(self):
        self.run_main(body(self.valid))
        kw = self.payloads[0]
        self.assertEqual({k: v for k, v in kw["payload"].items() if k != "messages"}, PRODUCTION_SHAPE)
        self.assertNotIn("max_tokens", kw["payload"])
        self.assertNotIn("reasoning", kw["payload"])
        self.assertNotIn("reasoning_format", kw["payload"])        # mutually exclusive with include_reasoning
        self.assertEqual(kw["timeout"], gv.GENERATION_TIMEOUT_S)
        auth = [v for n, v in kw["headers"] if n == "Authorization"]
        self.assertEqual(auth, ["Bearer " + FAKE_KEY])
        meta = json.loads((self.out / "run_meta.json").read_text(encoding="utf-8"))
        self.assertEqual((meta["base"], meta["model_slug"]), (gv.EXPECTED_BASE, MODEL))
        self.assertEqual(meta["groq_specific"]["token_parameter"], "max_completion_tokens")
        self.assertFalse(meta["groq_specific"]["openrouter_fallback"])
        self.assertNotIn(FAKE_KEY, json.dumps(meta))
        self.assertNotIn("fingerprint", json.dumps(meta))

    def test_payload_matches_the_worker_prompts_build_request(self):
        from prqa_worker.policy import Policy
        from prqa_worker.prompt_builder import build_llm_request
        gen = Policy.from_env(ENV).generation_settings()
        expected = build_llm_request(Templates.load(ROOT / "prompts"), model=MODEL, provider="groq",
                                     repository="git@synthetic-org/bench-repo", pr_number=C01.pr_number,
                                     test_level="standard", synthetic=True, context=context_for(C01),
                                     max_tokens=gen["max_tokens"], reasoning_effort=gen["reasoning_effort"])
        self.run_main(body(self.valid))
        self.assertEqual(self.payloads[0]["payload"], expected)

    def test_key_comes_only_from_the_secrets_file(self):
        self.secrets.clear()
        self.env["PROMPT_LLM_API_KEY"] = FAKE_KEY            # never a substitute for GROQ_API_KEY
        self.assertIn("GROQ_API_KEY missing", self.blocked("--only", CID))
        self.env["GROQ_API_KEY"] = FAKE_KEY                  # compose/.env sits on a permission-less drive
        msg = self.blocked("--only", CID)
        self.assertIn("set in compose/.env", msg)
        self.assertNotIn(FAKE_KEY, msg)
        self.secrets["GROQ_API_KEY"] = FAKE_KEY              # refused even when the secrets file also has one
        self.assertIn("set in compose/.env", self.blocked("--only", CID))

    def test_a_groq_key_under_the_legacy_name_is_named_but_never_used(self):
        self.secrets = {"PROMPT_LLM_API_KEY": FAKE_KEY}
        msg = self.blocked("--only", CID)
        self.assertIn("stored under PROMPT_LLM_API_KEY", msg)
        self.assertNotIn(FAKE_KEY, msg)

    def test_an_openrouter_shaped_key_is_never_sent_to_groq(self):
        self.secrets["GROQ_API_KEY"] = OPENROUTER_SHAPED
        msg = self.blocked("--only", CID)
        self.assertIn("openrouter key format", msg)
        self.assertNotIn(OPENROUTER_SHAPED, msg)

    def test_non_groq_base_or_unpinned_model_or_unsafe_flags_block_before_any_io(self):
        for change, needle in (({"PROMPT_LLM_API_BASE": "https://openrouter.ai/api/v1"}, "no OpenRouter fallback"),
                               ({"PROMPT_LLM_MODEL": "llama-3.3-70b-versatile"}, "no pinned Groq limits"),
                               ({"PROMPT_LLM_REASONING": "on"}, "generation settings"),
                               ({"PROMPT_LLM_MAX_TOKENS": "24001"}, "generation settings"),
                               ({"AUTOMATION_MODE": "live"}, "must be review_only"),
                               ({"PROMPT_LLM_LIVE_IN_REVIEW_ONLY": "true"}, "must be false")):
            with self.subTest(change=change):
                self.env = {**ENV, **change}
                self.assertIn(needle, self.blocked("--only", CID))

    def test_context_overflow_blocks_before_sending(self):
        with mock.patch.dict(gv.GROQ_MODEL_LIMITS, {MODEL: {"context_window": 6000, "max_completion_tokens": 65536}}):
            self.assertIn("vs context_window 6000", self.blocked("--only", CID))
        with mock.patch.dict(gv.GROQ_MODEL_LIMITS, {MODEL: {"context_window": 131072, "max_completion_tokens": 2048}}):
            self.assertIn("exceeds the model's", self.blocked("--only", CID))

    def test_check_config_is_local_only_and_writes_nothing(self):
        with mock.patch.object(gv, "http_json", side_effect=AssertionError("network")), \
                mock.patch.object(sys, "argv", ["x", "--check-config", "--only", CID]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(gv.main(), 0)
        self.assertNotIn(FAKE_KEY, out.getvalue())
        self.assertEqual(list(self.out.iterdir()), [])
        self.secrets.clear()
        with mock.patch.object(sys, "argv", ["x", "--check-config"]), self.assertRaises(SystemExit) as cm:
            gv.main()
        self.assertIn("GROQ_API_KEY missing", str(cm.exception.code))

    def test_dry_run_needs_no_key_and_sends_nothing(self):
        self.secrets.clear()
        with mock.patch.object(gv, "load_secret_records", side_effect=AssertionError("secrets read")), \
                mock.patch.object(gv, "http_json", side_effect=AssertionError("network")), \
                mock.patch.object(sys, "argv", ["x", "--only", CID, "--dry-run"]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(gv.main(), 0)
        self.assertIn('"max_completion_tokens": 3000', out.getvalue())
        self.assertIn('"reasoning_effort": "low"', out.getvalue())
        self.assertEqual(list(self.out.iterdir()), [])

    def test_limit_bounds_the_run(self):
        second = next(c.cid for c in CASES if c.cid != CID)
        self.run_main(body(self.valid), "--only", f"{CID},{second}", "--limit", "1")
        self.assertEqual(self.chat_calls, 1)

    def test_reasoning_text_is_never_the_prompt_and_never_persisted(self):
        hidden = "HIDDEN-CHAIN-OF-THOUGHT-" + "z" * 40
        reasoning_only = body("")
        reasoning_only["choices"][0]["message"]["reasoning"] = hidden
        with mock.patch.object(gv, "validate_generated_prompt", side_effect=AssertionError("gate called")):
            self.run_main(reasoning_only)
        self.assertEqual(self.results()["results"][0]["outcome"], "empty")
        mixed = body(self.valid)
        mixed["choices"][0]["message"]["reasoning"] = hidden
        self.run_main(mixed)
        dumped = "".join(f.read_text(encoding="utf-8") for f in self.out.rglob("*.json"))
        self.assertNotIn(hidden, dumped)

    def test_usable_response_is_redacted_and_versioned(self):
        self.run_main(body(self.valid + "\nAuthorization: Bearer " + FAKE_KEY + "\n"))
        art = json.loads((self.out / f"case-{CID}.response.json").read_text(encoding="utf-8"))
        self.assertNotIn(FAKE_KEY, json.dumps(art))
        self.assertEqual(art["generation"]["provider"], "groq")
        self.assertEqual(art["generation"]["reasoning"], {"effort": "low", "include_reasoning": False})
        self.assertEqual(art["source_sha256"], gv.source_hashes())
        self.assertIn("scripts/eval/evaluate_groq_live.py", art["source_sha256"])

    def test_truncated_response_is_a_diagnostic_never_gated_and_stops(self):
        second = next(c.cid for c in CASES if c.cid != CID)
        with mock.patch.object(gv, "validate_generated_prompt", side_effect=AssertionError("gate called")):
            self.run_main(body(self.valid, finish="length"), "--only", f"{CID},{second}")
        self.assertEqual(self.chat_calls, 1)
        r = self.results()["results"][0]
        self.assertEqual((r["outcome"], r["would_source"], r["failure_reason"]), ("truncated", "would_fallback", "TRUNCATED_MODEL_OUTPUT"))
        self.assertFalse((self.out / f"case-{CID}.response.json").exists())
        d = json.loads((self.out / f"case-{CID}.diagnostic.json").read_text(encoding="utf-8"))
        self.assertTrue(d["content_present"])
        self.assertTrue(self.results()["stopped_reasons"])

    def test_empty_response_stops_the_run(self):
        second = next(c.cid for c in CASES if c.cid != CID)
        self.run_main(body(""), "--only", f"{CID},{second}")
        self.assertEqual(self.chat_calls, 1)
        self.assertEqual(self.results()["results"][0]["outcome"], "empty")

    def test_http_failure_is_classified_and_stops(self):
        second = next(c.cid for c in CASES if c.cid != CID)
        for status in (429, 500, 400):
            with self.subTest(status=status):
                self.chat_calls = 0
                self.run_main({"error": {"message": "failed; key " + FAKE_KEY}}, "--only", f"{CID},{second}",
                              status=status)
                r = self.results()["results"][0]
                self.assertEqual((r["failure_reason"], r["outcome"]), (f"HTTP_{status}", "http_error"))
                self.assertEqual(self.chat_calls, 1)
                self.assertNotIn(FAKE_KEY, json.dumps(self.results()))

    def test_rate_limit_413_keeps_no_account_identifier(self):
        org = "org_" + "0a" * 13                        # synthetic organization id
        err = {"error": {"code": "rate_limit_exceeded", "type": "tokens",
                         "message": f"Request too large for model `openai/gpt-oss-120b` in organization `{org}` service "
                                    "tier `on_demand` on tokens per minute (TPM): Limit 8000, Requested 10430"}}
        self.run_main(err, status=413)
        r = self.results()["results"][0]
        self.assertEqual((r["failure_reason"], r["outcome"]), ("HTTP_413", "http_error"))
        dumped = json.dumps(self.results()) + (self.out / f"case-{CID}.diagnostic.json").read_text(encoding="utf-8")
        self.assertNotIn(org, dumped)
        self.assertIn("Limit 8000", dumped)                  # the actionable part stays

    def test_413_status_and_provider_classification_are_kept_apart(self):
        # rate-limit error object: the outcome comes from the status, the provider code/type are recorded beside it
        self.run_main({"error": {"code": "rate_limit_exceeded", "type": "tokens", "message": "TPM: Limit 8000"}}, status=413)
        r = self.results()["results"][0]
        self.assertEqual((r["http_status"], r["failure_reason"], r["outcome"]), (413, "HTTP_413", "http_error"))
        self.assertEqual((r["provider_error_code"], r["provider_error_type"]), ("rate_limit_exceeded", "tokens"))
        diag = json.loads((self.out / f"case-{CID}.diagnostic.json").read_text(encoding="utf-8"))["diagnostics"]
        self.assertEqual(diag["http_status"], 413)
        self.assertEqual(diag["provider_error"]["code"], "rate_limit_exceeded")

    def test_413_without_an_error_object_infers_no_cause(self):
        # a bare 413 (e.g. a proxy's body-size page) gets the same status outcome and no invented provider category
        for response in ("<html>413 Request Entity Too Large</html>", {}, {"error": "too large"}):
            with self.subTest(response=response):
                self.run_main(response, status=413)
                r = self.results()["results"][0]
                self.assertEqual((r["failure_reason"], r["outcome"]), ("HTTP_413", "http_error"))
                self.assertIsNone(r["provider_error_code"])
                self.assertIsNone(r["provider_error_type"])
                dumped = json.dumps(r).lower()
                for guess in ("rate_limit", "body size", "payload too large", "tokens per minute"):
                    self.assertNotIn(guess, dumped.replace("request entity too large", ""))

    def test_same_provider_code_on_another_status_keeps_that_status(self):
        # the provider code does not rewrite the HTTP classification: a 429 stays HTTP_429
        self.run_main({"error": {"code": "rate_limit_exceeded", "type": "tokens", "message": "slow down"}}, status=429)
        r = self.results()["results"][0]
        self.assertEqual((r["http_status"], r["failure_reason"], r["outcome"]), (429, "HTTP_429", "http_error"))
        self.assertEqual(r["provider_error_code"], "rate_limit_exceeded")

    def test_malformed_body_is_classified(self):
        self.run_main({"id": "x", "object": "chat.completion"})
        self.assertEqual(self.results()["results"][0]["outcome"], "malformed")
        self.run_main("<html>not json</html>")
        self.assertEqual(self.results()["results"][0]["outcome"], "malformed")

    def test_200_error_body_is_a_sanitized_provider_error_never_gated(self):
        err = {"error": {"code": 503, "type": "server_error", "message": "busy; key " + FAKE_KEY, "raw": {"x": 1}}}
        with mock.patch.object(gv, "validate_generated_prompt", side_effect=AssertionError("gate called")):
            self.run_main(err)
        r = self.results()["results"][0]
        self.assertEqual((r["failure_reason"], r["outcome"]), ("PROVIDER_ERROR_RESPONSE", "provider_error"))
        self.assertFalse((self.out / f"case-{CID}.response.json").exists())
        dumped = (self.out / f"case-{CID}.diagnostic.json").read_text(encoding="utf-8")
        pe = json.loads(dumped)["diagnostics"]["provider_error"]
        self.assertEqual((pe["code"], pe["type"]), (503, "server_error"))
        self.assertNotIn(FAKE_KEY, dumped + json.dumps(self.results()))
        self.assertNotIn("raw", pe)

    def test_failed_attempt_archives_and_replaces_an_older_success(self):
        self.run_main(body(self.valid))
        self.run_main(body(""))
        self.assertFalse((self.out / f"case-{CID}.response.json").exists())
        archives = list((self.out / "_backups").iterdir())
        self.assertEqual(len(archives), 1)
        self.assertTrue((archives[0] / f"case-{CID}.response.json").exists())
        self.assertTrue((archives[0] / "SHA256SUMS").exists())

    def test_merge_is_offline_and_labels_versions(self):
        self.run_main(body(self.valid))
        legacy = next(c.cid for c in CASES if c.cid != CID)
        (self.out / f"case-{legacy}.response.json").write_text(json.dumps(
            {"cid": legacy, "http_status": 200, "seconds": 1.0, "finish_reason": "length", "usage": {}, "content": self.valid}))
        with mock.patch.object(gv, "http_json", side_effect=AssertionError("network")), \
                mock.patch.object(gv, "load_secret_records", side_effect=AssertionError("secrets read")), \
                mock.patch.object(sys, "argv", ["x", "--merge"]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gv.main(), 0)
        res = self.results()
        rows = {r["cid"]: r for r in res["results"]}
        self.assertEqual(res["evaluation_mode"], "offline_rescore")
        self.assertEqual((rows[CID]["outcome"], rows[CID]["version_status"]), ("usable", "current"))
        self.assertEqual((rows[legacy]["outcome"], rows[legacy]["version_status"]), ("truncated", "unknown"))
        self.assertNotIn("quality_score", rows[legacy])


class SecretsFileTests(unittest.TestCase):
    """load_secret_records itself (unpatched): owner-only file required, values never echoed."""
    def test_secrets_file_readable_by_others_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "secrets.env"
            f.write_text("GROQ_API_KEY=" + FAKE_KEY + "\n", encoding="utf-8")
            with mock.patch.object(gv, "SECRETS_FILE", f):
                f.chmod(0o644)
                with self.assertRaises(SystemExit) as cm:
                    gv.load_secret_records()
                self.assertIn("readable by other users", str(cm.exception.code))
                self.assertNotIn(FAKE_KEY, str(cm.exception.code))
                f.chmod(0o600)
                self.assertEqual(gv.load_secret_records(), {"GROQ_API_KEY": FAKE_KEY})
            with mock.patch.object(gv, "SECRETS_FILE", Path(d) / "absent.env"):
                self.assertEqual(gv.load_secret_records(), {})


if __name__ == "__main__":
    unittest.main()
