"""Offline tests for scripts/eval/evaluate_openrouter_live.py: empty/malformed HTTP 200 diagnostics and --check-config.

Every network path is replaced: `http_json` and `slug_meta` are fakes, and `urllib.request.urlopen` raises if
anything reaches it. Configuration is synthetic (fake key, temporary output directory); compose/.env and the
real runs/ directory are never read or written.

Run: PYTHONPATH=worker python3 -m unittest tests.test_openrouter_evaluator_offline -v
"""
from __future__ import annotations

import contextlib
import importlib.util
import hashlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("prqa_eval_openrouter_live", ROOT / "scripts" / "eval" / "evaluate_openrouter_live.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)

from prqa_worker.prompt_builder import GenerationConfigError, Templates, reference_fallback_prompt  # noqa: E402
from tests.qa_benchmark import CASES, context_for  # noqa: E402

CID = "C01-clear-behaviour"
C01 = next(c for c in CASES if c.cid == CID)
FAKE_KEY = "sk-or-v1-" + "f" * 56            # synthetic, matches the OpenRouter key shape; never a real key
HIDDEN_REASONING = "HIDDEN-REASONING-SENTINEL-7f3a"
SYNTHETIC_ENV = {
    "AUTOMATION_MODE": "review_only",
    "PROMPT_LLM_LIVE_IN_REVIEW_ONLY": "false",
    "PROMPT_LLM_API_BASE": ev.EXPECTED_BASE,
    "PROMPT_LLM_MODEL": "synthetic/test-model:free",
    "PROMPT_LLM_API_KEY": FAKE_KEY,
}


def _no_network(*_a, **_k):
    raise AssertionError("network access attempted in an offline test")


def body(content=..., *, finish="stop", usage=None, choices=..., message=..., extra=None):
    """Fake chat-completion body. `...` means 'use the default'."""
    if message is ...:
        message = {"role": "assistant", "reasoning": HIDDEN_REASONING,
                   "reasoning_details": [{"type": "reasoning.text", "text": HIDDEN_REASONING}]}
        if content is not ...:
            message["content"] = content
    if choices is ...:
        choices = [{"index": 0, "finish_reason": finish, "native_finish_reason": finish, "message": message}]
    b = {"id": "gen-synthetic-0001", "provider": "SyntheticProvider", "model": "synthetic/test-model:free",
         "choices": choices}
    if usage is not None:
        b["usage"] = usage
    b.update(extra or {})
    return b


class EvaluatorHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t = Templates.load(ROOT / "prompts")
        cls.valid_prompt = reference_fallback_prompt(t, repository="synthetic-org/bench-repo", pr_number=C01.pr_number,
                                                     test_level="standard", context=context_for(C01))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.chat_calls = 0
        self.meta = {"model_list_available": True, "context_length": 1, "pricing": {"prompt": "0", "completion": "0"},
                     "supported_parameters": ["max_tokens", "reasoning", "temperature"]}
        self.payloads: list[dict] = []
        self.http_calls: list[str] = []
        patches = [
            mock.patch.object(ev, "OUT_DIR", self.out),
            mock.patch.object(ev, "BACKUP_DIR", self.out / "_backups"),       # never the real runs/baseline-backups
            mock.patch.object(ev, "load_env_records", lambda: dict(SYNTHETIC_ENV)),
            mock.patch.object(ev, "slug_meta", lambda *a, **k: dict(self.meta)),
            mock.patch("urllib.request.urlopen", _no_network),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_main(self, response_body=None, *argv, status=200):
        def fake_http(url, **_kw):
            self.http_calls.append(url)
            if url.endswith("/chat/completions"):
                self.chat_calls += 1
                self.payloads.append(_kw.get("payload"))
                return status, response_body
            raise AssertionError(f"unexpected HTTP call: {url}")
        out = io.StringIO()
        with mock.patch.object(ev, "http_json", side_effect=fake_http), \
                mock.patch.object(sys, "argv", ["evaluate_openrouter_live.py", *(argv or ("--only", CID))]), \
                contextlib.redirect_stdout(out):
            rc = ev.main()
        self.stdout = out.getvalue()
        return rc

    def results(self):
        return json.loads((self.out / "results.json").read_text(encoding="utf-8"))

    def row(self):
        rows = self.results()["results"]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def all_written_text(self):
        return self.stdout + "".join(p.read_text(encoding="utf-8") for p in self.out.iterdir() if p.is_file())

    def assert_failed_without_gate(self, response_body, reason):
        return self.assert_failed_without_gate_args(response_body, reason)

    def assert_failed_without_gate_args(self, response_body, reason, *argv):
        with mock.patch.object(ev, "validate_generated_prompt", side_effect=AssertionError("gate called")) as gate, \
                mock.patch.object(ev.qa_rubric, "score_prompt", side_effect=AssertionError("rubric called")) as rub:
            self.assertEqual(self.run_main(response_body, *argv), 0)
        gate.assert_not_called()
        rub.assert_not_called()
        r = self.row()
        self.assertEqual(r["request"], "failed")
        self.assertEqual(r["failure_reason"], reason)
        self.assertTrue(r["error"].startswith(reason))
        self.assertNotIn("quality_score", r)
        self.assertNotIn("gate_valid", r)
        self.assertFalse((self.out / f"case-{CID}.response.json").exists(), "empty output must not become a response artifact")
        diag_file = json.loads((self.out / f"case-{CID}.diagnostic.json").read_text(encoding="utf-8"))
        self.assertEqual(diag_file["failure_reason"], reason)
        self.assertEqual(diag_file["content_present"], reason == ev.TRUNCATED_MODEL_OUTPUT)   # only truncation keeps text
        self.assertEqual(self.chat_calls, 1)
        totals = self.results()["usage_totals"]
        self.assertEqual((totals["requests"], totals["successful"], totals["failed"]), (1, 0, 1))
        return r


class EmptyContentTests(EvaluatorHarness):
    def test_empty_string_content_is_failed_empty_model_content(self):
        r = self.assert_failed_without_gate(body(""), ev.EMPTY_MODEL_CONTENT)
        self.assertIn("empty", r["error"])

    def test_whitespace_only_content_is_failed_empty_model_content(self):
        r = self.assert_failed_without_gate(body(" \n\t  "), ev.EMPTY_MODEL_CONTENT)
        self.assertIn("whitespace-only", r["error"])

    def test_null_content_is_failed_empty_model_content(self):
        r = self.assert_failed_without_gate(body(None), ev.EMPTY_MODEL_CONTENT)
        self.assertIn("null", r["error"])

    def test_absent_content_key_is_failed_empty_model_content(self):
        r = self.assert_failed_without_gate(body(), ev.EMPTY_MODEL_CONTENT)
        self.assertIn("absent", r["error"])

    def test_length_finish_reason_and_usage_are_preserved_on_empty_content(self):
        usage = {"prompt_tokens": 10045, "completion_tokens": 16000, "total_tokens": 26045}
        r = self.assert_failed_without_gate(body("", finish="length", usage=usage), ev.EMPTY_MODEL_CONTENT)
        d = r["diagnostics"]
        self.assertEqual(d["finish_reason"], "length")
        self.assertEqual(d["native_finish_reason"], "length")
        self.assertEqual(d["usage"], usage)
        self.assertEqual(d["http_status"], 200)
        self.assertEqual(d["model_returned"], "synthetic/test-model:free")
        self.assertEqual(d["provider"], "SyntheticProvider")
        self.assertEqual(d["request_id"], "gen-synthetic-0001")
        totals = self.results()["usage_totals"]
        self.assertEqual(totals["total_tokens"], 26045)   # provider-reported tokens are no longer dropped

    def test_explicit_reasoning_tokens_preserved_only_when_supplied(self):
        nested = {"prompt_tokens": 1, "completion_tokens": 9, "total_tokens": 10,
                  "completion_tokens_details": {"reasoning_tokens": 9}}
        r = self.assert_failed_without_gate(body("", finish="length", usage=nested), ev.EMPTY_MODEL_CONTENT)
        self.assertEqual(r["diagnostics"]["usage"]["reasoning_tokens"], 9)
        self.assertEqual(ev.extract_usage({"usage": {"reasoning_tokens": 4}}), {"reasoning_tokens": 4})
        self.assertNotIn("reasoning_tokens", ev.extract_usage({"usage": {"prompt_tokens": 1, "completion_tokens": 9}}))
        self.assertNotIn("reasoning_tokens", ev.extract_usage({"usage": {"completion_tokens_details": {}}}))

    def test_missing_usage_fields_stay_missing(self):
        r = self.assert_failed_without_gate(body("", usage={"completion_tokens": 7}), ev.EMPTY_MODEL_CONTENT)
        self.assertEqual(r["diagnostics"]["usage"], {"completion_tokens": 7})
        self.assert_no_synthetic_usage()

    def assert_no_synthetic_usage(self):
        self.assertEqual(ev.extract_usage({}), {})
        self.assertEqual(ev.extract_usage({"usage": None}), {})
        self.assertEqual(ev.extract_usage({"usage": {"prompt_tokens": None, "total_tokens": "12", "completion_tokens": True}}), {})
        no_usage = ev.parse_completion(200, body(""))
        self.assertNotIn("usage", no_usage["diagnostics"])
        self.assertNotIn("finish_reason", ev.parse_completion(200, body("", finish=None))["diagnostics"])

    def test_empty_content_is_not_retried_and_stops_the_run(self):
        second = next(c.cid for c in CASES if c.cid != CID)
        with mock.patch.object(ev, "validate_generated_prompt", side_effect=AssertionError("gate called")):
            self.run_main(body(""), "--only", f"{CID},{second}")
        self.assertEqual(self.chat_calls, 1, "an empty-content response must not be retried or followed by more requests")
        self.assertEqual([r["cid"] for r in self.results()["results"]], [CID])


class MalformedResponseTests(EvaluatorHarness):
    def test_missing_choices_is_malformed(self):
        self.assert_failed_without_gate(body(choices=None), ev.MALFORMED_MODEL_RESPONSE)
        b = body()
        del b["choices"]
        self.assertEqual(ev.parse_completion(200, b)["failure_reason"], ev.MALFORMED_MODEL_RESPONSE)

    def test_empty_choices_list_is_malformed(self):
        self.assert_failed_without_gate(body(choices=[]), ev.MALFORMED_MODEL_RESPONSE)

    def test_malformed_choice_structures_never_raise(self):
        cases = {
            "choices not a list": body(choices={"0": {}}),
            "choice not an object": body(choices=["text"]),
            "message missing": body(choices=[{"finish_reason": "stop"}]),
            "message not an object": body(message="hello"),
            "content not a string": body(message={"content": ["a", "b"]}),
            "body not an object": "upstream error page",
        }
        for label, b in cases.items():
            with self.subTest(label):
                self.assertEqual(ev.parse_completion(200, b)["failure_reason"], ev.MALFORMED_MODEL_RESPONSE)

    def test_malformed_choice_through_main_records_diagnostics(self):
        r = self.assert_failed_without_gate(body(choices=[{"finish_reason": "error"}]), ev.MALFORMED_MODEL_RESPONSE)
        self.assertEqual(r["diagnostics"]["finish_reason"], "error")


class ArtifactSafetyTests(EvaluatorHarness):
    def test_diagnostics_never_persist_credentials_headers_or_hidden_reasoning(self):
        leaky = body("", finish="length", usage={"total_tokens": 5}, extra={
            "headers": {"Authorization": f"Bearer {FAKE_KEY}"},
            "debug": {"echo_key": FAKE_KEY, "reasoning": HIDDEN_REASONING},
        })
        leaky["choices"][0]["message"]["refusal"] = f"Bearer {FAKE_KEY}"
        self.assert_failed_without_gate(leaky, ev.EMPTY_MODEL_CONTENT)
        written = self.all_written_text()
        for needle in (FAKE_KEY, HIDDEN_REASONING, "Authorization", "Bearer"):
            self.assertNotIn(needle, written)

    def test_unsafe_identifier_values_are_dropped_not_copied(self):
        d = ev.parse_completion(200, body("", extra={"id": f"Bearer {FAKE_KEY} x", "model": "a\nb", "provider": 5}))["diagnostics"]
        self.assertNotIn("request_id", d)
        self.assertNotIn("model_returned", d)
        self.assertNotIn("provider", d)

    def test_generated_content_is_redacted_before_persistence_but_gated_unredacted(self):
        leaked = self.valid_prompt.replace("## 11. Blockers", f"Use key {FAKE_KEY} here.\n\n## 11. Blockers", 1)
        seen = []
        real_gate = ev.validate_generated_prompt
        with mock.patch.object(ev, "validate_generated_prompt",
                               side_effect=lambda text, **kw: seen.append(text) or real_gate(text, **kw)):
            self.run_main(body(leaked))
        self.assertIn(FAKE_KEY, seen[0], "the gate must see the original output so it can reject the secret")
        r = self.row()
        self.assertFalse(r["gate_valid"], "a leaked credential must still fail the gate")
        stored = json.loads((self.out / f"case-{CID}.response.json").read_text(encoding="utf-8"))
        self.assertNotIn(FAKE_KEY, stored["content"])
        self.assertTrue(stored["redaction"]["applied"])
        self.assertIn("prompt_llm_api_key", stored["redaction"]["rules"])
        self.assertNotIn(FAKE_KEY, self.all_written_text())
        self.assertFalse(any(p.suffix == ".tmp" for p in self.out.iterdir()), "atomic write left a temp file")


class ValidResponseTests(EvaluatorHarness):
    def test_valid_content_still_flows_through_gate_rubric_and_artifact(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        self.assertEqual(self.run_main(body(self.valid_prompt, usage=usage)), 0)
        r = self.row()
        self.assertEqual(r["request"], "ok")
        self.assertTrue(r["gate_valid"], r["gate_errors"])
        self.assertEqual(r["would_source"], "llm")
        self.assertIsInstance(r["quality_score"], float)
        self.assertEqual(set(r["dimensions"]), set(ev.qa_rubric.DIMENSIONS))
        self.assertEqual(r["usage"], usage)
        self.assertEqual(r["finish_reason"], "stop")
        self.assertNotIn("failure_reason", r)
        self.assertNotIn("artifact_redaction", r)
        stored = json.loads((self.out / f"case-{CID}.response.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["content"], self.valid_prompt)          # nothing to redact: byte-identical
        self.assertEqual(stored["redaction"], {"applied": False, "rules": []})
        for k in ("cid", "http_status", "seconds", "model_returned", "finish_reason", "usage", "content"):
            self.assertIn(k, stored)                                     # existing artifact fields kept for --merge
        self.assertFalse((self.out / f"case-{CID}.diagnostic.json").exists())
        self.assertEqual(self.chat_calls, 1)
        self.assertNotIn(HIDDEN_REASONING, self.all_written_text())

    def test_non_200_path_is_unchanged_and_not_retried(self):
        self.run_main({"error": {"message": "rate limited"}}, status=429)
        r = self.row()
        self.assertEqual((r["request"], r["http_status"]), ("failed", 429))
        self.assertEqual((r["failure_reason"], r["outcome"], r["would_source"]), ("HTTP_429", "http_error", "would_fallback"))
        self.assertNotIn("gate_valid", r)
        self.assertEqual(self.chat_calls, 1)
        self.assertTrue(self.results()["stopped_reasons"])


class CheckConfigTests(EvaluatorHarness):
    def run_check_config(self, env):
        out = io.StringIO()
        with mock.patch.object(ev, "load_env_records", lambda: dict(env)), \
                mock.patch.object(ev, "http_json", side_effect=AssertionError("http_json called")) as http, \
                mock.patch.object(ev, "slug_meta", side_effect=AssertionError("slug_meta called")) as meta, \
                mock.patch.object(ev, "build_llm_request", side_effect=AssertionError("generation built")) as build, \
                mock.patch.object(sys, "argv", ["evaluate_openrouter_live.py", "--check-config"]), \
                contextlib.redirect_stdout(out):
            rc = ev.main()
        http.assert_not_called()
        meta.assert_not_called()
        build.assert_not_called()
        self.assertEqual(list(self.out.iterdir()), [], "--check-config must not write artifacts")
        self.assertNotIn(FAKE_KEY, out.getvalue())
        self.assertNotIn(FAKE_KEY[:12], out.getvalue())
        self.assertNotIn(ev.key_fingerprint(FAKE_KEY)["sha256_12"], out.getvalue())
        return rc, out.getvalue()

    def test_valid_local_config_exits_zero_without_network(self):
        rc, text = self.run_check_config(SYNTHETIC_ENV)
        self.assertEqual(rc, 0)
        self.assertIn("[check-config] OK", text)
        self.assertIn("no network traffic", text)

    def test_invalid_local_config_exits_nonzero_without_network(self):
        bad_envs = {
            "live flag on": SYNTHETIC_ENV | {"PROMPT_LLM_LIVE_IN_REVIEW_ONLY": "true"},
            "wrong mode": SYNTHETIC_ENV | {"AUTOMATION_MODE": "full"},
            "wrong endpoint": SYNTHETIC_ENV | {"PROMPT_LLM_API_BASE": "https://example.invalid/v1"},
            "no slug": SYNTHETIC_ENV | {"PROMPT_LLM_MODEL": ""},
            "no key": {k: v for k, v in SYNTHETIC_ENV.items() if k != "PROMPT_LLM_API_KEY"},
            "placeholder key": SYNTHETIC_ENV | {"PROMPT_LLM_API_KEY": "<your-key>"},
        }
        for label, env in bad_envs.items():
            with self.subTest(label):
                rc, text = self.run_check_config(env)
                self.assertEqual(rc, 1)
                self.assertIn("[check-config] FAILED", text)

    def test_check_config_wins_over_other_flags(self):
        out = io.StringIO()
        with mock.patch.object(ev, "http_json", side_effect=AssertionError("http_json called")), \
                mock.patch.object(ev, "slug_meta", side_effect=AssertionError("slug_meta called")), \
                mock.patch.object(sys, "argv", ["x", "--check-config", "--only", CID]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(ev.main(), 0)


class MaxTokensOptionTests(EvaluatorHarness):
    """--max-tokens: default unchanged (16000 from build_llm_request), 24000 selectable, invalid values
    rejected by argument parsing before configuration is read or any network helper runs."""

    def build_spy(self):
        return mock.patch.object(ev, "build_llm_request", wraps=ev.build_llm_request)

    def test_default_is_build_llm_request_default_of_16000(self):
        import inspect
        self.assertEqual(inspect.signature(ev.build_llm_request).parameters["max_tokens"].default, 16000)
        self.assertEqual(ev.DEFAULT_MAX_TOKENS, 16000)
        self.assertEqual(ev.CLI_MAX_TOKENS_LIMIT, 24000)

    def test_omitting_the_option_sends_16000(self):
        with self.build_spy() as spy:
            self.run_main(body(self.valid_prompt))
        self.assertEqual(spy.call_args.kwargs["max_tokens"], 16000)
        self.assertEqual([p["max_tokens"] for p in self.payloads], [16000])
        self.assertEqual(self.results()["requested_max_tokens"], 16000)

    def test_24000_is_parsed_passed_to_the_builder_and_sent(self):
        with self.build_spy() as spy:
            self.run_main(body(self.valid_prompt), "--only", CID, "--max-tokens", "24000")
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(spy.call_args.kwargs["max_tokens"], 24000)
        payload = self.payloads[0]
        self.assertEqual(payload["max_tokens"], 24000)
        self.assertEqual(payload["reasoning"], {"enabled": True})       # reasoning settings untouched
        self.assertEqual(payload["temperature"], 0.2)
        self.assertEqual(self.results()["requested_max_tokens"], 24000)
        self.assertEqual(self.chat_calls, 1)

    def test_value_applies_to_every_selected_case(self):
        ids = [c.cid for c in CASES[:2]]
        self.run_main(body(self.valid_prompt), "--only", ",".join(ids), "--max-tokens", "20000")
        self.assertEqual([p["max_tokens"] for p in self.payloads], [20000, 20000])

    def test_only_payload_field_that_changes_is_max_tokens(self):
        self.run_main(body(self.valid_prompt))
        self.run_main(body(self.valid_prompt), "--only", CID, "--max-tokens", "24000")
        default, raised = self.payloads
        # the stated output budget names the completion limit, so the user message differs only in that number
        normalise = lambda p: re.sub(r"at most \d+ characters in total \(completion limit \d+ tokens", "N",
                                     json.dumps({k: v for k, v in p.items() if k != "max_tokens"}))
        self.assertEqual(normalise(default), normalise(raised))

    def test_builder_default_unchanged_for_callers_without_the_argument(self):
        t = Templates.load(ROOT / "prompts")
        req = ev.build_llm_request(t, model="synthetic/test-model:free", provider="openrouter",
                                   repository="synthetic-org/bench-repo", pr_number=C01.pr_number,
                                   test_level="standard", synthetic=True, context=context_for(C01))
        self.assertEqual(req["max_tokens"], 16000)

    def test_upper_bound_24000_accepted_and_smallest_value_accepted(self):
        self.assertEqual(ev.max_tokens_arg("24000"), 24000)
        self.assertEqual(ev.max_tokens_arg("1"), 1)

    def test_invalid_values_exit_nonzero_before_config_or_network(self):
        invalid = {"zero": "0", "negative": "-5", "float": "24000.0", "word": "lots", "empty": "",
                   "hex": "0x5DC0", "exponent": "2e4", "above maximum": "24001", "far above": "512000"}
        for label, value in invalid.items():
            with self.subTest(label):
                env_loader = mock.Mock(side_effect=AssertionError("config read"))
                err = io.StringIO()
                with mock.patch.object(ev, "load_env_records", env_loader), \
                        mock.patch.object(ev, "http_json", side_effect=AssertionError("http_json called")) as http, \
                        mock.patch.object(ev, "slug_meta", side_effect=AssertionError("slug_meta called")) as meta, \
                        mock.patch.object(ev, "build_llm_request", side_effect=AssertionError("request built")) as build, \
                        mock.patch.object(sys, "argv", ["x", "--only", CID, f"--max-tokens={value}"]), \
                        contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as cm:
                        ev.main()
                self.assertEqual(cm.exception.code, 2)
                # rejected by the option's own validator, not as an unrecognized argument
                self.assertIn("argument --max-tokens: must be", err.getvalue())
                self.assertNotIn("unrecognized arguments", err.getvalue())
                for m in (env_loader, http, meta, build):
                    m.assert_not_called()
                self.assertEqual(list(self.out.iterdir()), [])

    def test_only_requires_the_full_case_id(self):
        # audit loop 2026-10-26: an unknown id used to proceed (a /models call, an archive and an empty results.json
        # replacing the current one); it now exits 2 before any network call or artifact write
        with mock.patch.object(ev, "slug_meta", side_effect=AssertionError("slug_meta called")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = self.run_main(body(self.valid_prompt), "--only", "C01", "--max-tokens", "24000")
        self.assertEqual(rc, 2)
        self.assertIn("unknown case id", err.getvalue())
        self.assertEqual((self.chat_calls, self.http_calls), (0, []))
        self.assertEqual(list(self.out.iterdir()), [])
        self.run_main(body(self.valid_prompt), "--only", CID, "--max-tokens", "24000")
        self.assertEqual(self.chat_calls, 1)
        self.assertEqual([r["cid"] for r in self.results()["results"]], [CID])

    def test_check_config_with_max_tokens_stays_offline(self):
        out = io.StringIO()
        with mock.patch.object(ev, "http_json", side_effect=AssertionError("http_json called")) as http, \
                mock.patch.object(ev, "slug_meta", side_effect=AssertionError("slug_meta called")) as meta, \
                mock.patch.object(ev, "build_llm_request", side_effect=AssertionError("request built")) as build, \
                mock.patch.object(sys, "argv", ["x", "--check-config", "--max-tokens", "24000"]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(ev.main(), 0)
        for m in (http, meta, build):
            m.assert_not_called()
        self.assertEqual(list(self.out.iterdir()), [])

    def test_empty_content_diagnostics_and_no_retry_unchanged_at_24000(self):
        second = next(c.cid for c in CASES if c.cid != CID)
        with mock.patch.object(ev, "validate_generated_prompt", side_effect=AssertionError("gate called")), \
                mock.patch.object(ev.qa_rubric, "score_prompt", side_effect=AssertionError("rubric called")):
            self.run_main(body("", finish="length", usage={"completion_tokens": 24000}),
                          "--only", f"{CID},{second}", "--max-tokens", "24000")
        self.assertEqual(self.chat_calls, 1)
        r = self.row()
        self.assertEqual(r["failure_reason"], ev.EMPTY_MODEL_CONTENT)
        self.assertEqual(r["diagnostics"]["finish_reason"], "length")
        self.assertEqual(r["diagnostics"]["usage"], {"completion_tokens": 24000})
        self.assertTrue((self.out / f"case-{CID}.diagnostic.json").exists())

    def test_truncated_output_at_24000_is_its_own_outcome_and_never_gated(self):
        truncated = self.valid_prompt.split("## 4. Environment and API Discovery")[0]
        r = self.assert_failed_without_gate_args(body(truncated, finish="length"), ev.TRUNCATED_MODEL_OUTPUT,
                                                 "--only", CID, "--max-tokens", "24000")
        self.assertEqual((r["outcome"], r["would_source"]), ("truncated", "would_fallback"))



class ArtifactIntegrityTests(EvaluatorHarness):
    """A failed attempt must never leave an older successful response that --merge would re-score as current, and
    everything a run overwrites is archived (with a verified manifest) first."""

    def merge(self):
        with mock.patch.object(ev, "http_json", side_effect=AssertionError("http_json called")), \
                mock.patch.object(ev, "slug_meta", side_effect=AssertionError("slug_meta called")), \
                mock.patch.object(sys, "argv", ["evaluate_openrouter_live.py", "--merge"]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.main(), 0)
        return self.results()["results"]

    def archives(self):
        root = self.out / "_backups"
        return sorted(root.iterdir()) if root.is_dir() else []

    def test_failed_attempt_after_success_is_not_merged_as_success(self):
        self.run_main(body(self.valid_prompt))
        self.run_main(body(""))
        self.assertFalse((self.out / f"case-{CID}.response.json").exists(), "stale response left beside the failure")
        rows = self.merge()
        self.assertEqual([(r["cid"], r["request"], r["row_source"]) for r in rows], [(CID, "failed", "rebuilt_from_diagnostic")])
        self.assertEqual(rows[0]["failure_reason"], "EMPTY_MODEL_CONTENT")

    def test_success_after_failure_clears_the_stale_diagnostic(self):
        self.run_main(body(""))
        self.run_main(body(self.valid_prompt))
        self.assertFalse((self.out / f"case-{CID}.diagnostic.json").exists())
        self.assertEqual([r["request"] for r in self.merge()], ["ok"])

    def test_legacy_pair_on_disk_prefers_the_diagnostic(self):
        self.run_main(body(self.valid_prompt))
        (self.out / f"case-{CID}.diagnostic.json").write_text(json.dumps(
            {"cid": CID, "request": "failed", "failure_reason": "EMPTY_MODEL_CONTENT", "detail": "x", "diagnostics": {}}))
        rows = self.merge()
        self.assertEqual([r["request"] for r in rows], ["failed"])

    def test_http_failure_writes_a_diagnostic(self):
        self.run_main(body(self.valid_prompt))
        self.run_main({"error": {"message": "upstream unavailable"}}, status=503)
        d = json.loads((self.out / f"case-{CID}.diagnostic.json").read_text())
        self.assertEqual((d["failure_reason"], d["content_present"]), ("HTTP_503", False))
        self.assertEqual([r["request"] for r in self.merge()], ["failed"])

    def test_overwritten_artifacts_are_archived_with_a_verified_manifest(self):
        self.run_main(body(self.valid_prompt))
        before = {p.name: p.read_bytes() for p in self.out.glob("*.json")}
        self.run_main(body(self.valid_prompt))
        (arch,) = self.archives()
        self.assertEqual({p.name: p.read_bytes() for p in arch.glob("*.json")}, before)
        for line in (arch / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(hashlib.sha256((arch / name).read_bytes()).hexdigest(), digest)

    def test_merge_archives_results_before_replacing_them(self):
        self.run_main(body(self.valid_prompt))
        old = (self.out / "results.json").read_bytes()
        self.merge()
        self.assertTrue(any((a / "results.json").is_file() and (a / "results.json").read_bytes() == old for a in self.archives()))

    def test_merge_labels_whether_a_response_matches_the_current_sources(self):
        self.run_main(body(self.valid_prompt))
        self.assertEqual([r["generated_under_current_sources"] for r in self.merge()], [True])
        art = self.out / f"case-{CID}.response.json"
        data = json.loads(art.read_text())
        data["source_sha256"]["prompts/llm/system.v2.md"] = "0" * 64          # generated under an older prompt
        art.write_text(json.dumps(data))
        (row,) = self.merge()
        self.assertFalse(row["generated_under_current_sources"])
        self.assertEqual(row["sources_changed_since_generation"], ["prompts/llm/system.v2.md"])
        del data["source_sha256"]
        art.write_text(json.dumps(data))
        (row,) = self.merge()
        self.assertIsNone(row["generated_under_current_sources"])          # unknown stays unknown, never assumed

    def test_samples_record_source_hashes(self):
        self.run_main(body(self.valid_prompt))
        art = json.loads((self.out / f"case-{CID}.response.json").read_text())
        meta = json.loads((self.out / "run_meta.json").read_text())
        self.assertEqual(art["source_sha256"], meta["source_sha256"])
        self.assertEqual(set(art["source_sha256"]), set(ev.HASHED_SOURCES))
        self.assertEqual(art["source_sha256"]["prompts/llm/system.v2.md"],
                         hashlib.sha256((ROOT / "prompts/llm/system.v2.md").read_bytes()).hexdigest())
        self.assertEqual(art["requested_max_tokens"], 16000)


class PricingGuardTests(EvaluatorHarness):
    """No generation request unless the provider's /models metadata confirms the slug, zero prompt and completion
    prices and (when listed) max_tokens support. Nothing is written or archived when blocked."""

    def assertBlocked(self, meta, needle):
        self.meta = meta
        self.assertEqual(self.run_main(body(self.valid_prompt)), 3)
        self.assertEqual(self.chat_calls, 0)
        self.assertIn(needle, self.stdout)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [])

    def test_nonzero_completion_price_blocks(self):
        self.assertBlocked({**self.meta, "pricing": {"prompt": "0", "completion": "0.000002"}}, "completion price")

    def test_missing_pricing_blocks(self):
        self.assertBlocked({**self.meta, "pricing": None}, "pricing not reported")
        self.assertBlocked({**self.meta, "pricing": {"completion": "0"}}, "prompt price")

    def test_unlisted_model_blocks(self):
        self.assertBlocked({"model_list_available": False, "status": 200}, "not confirmed")

    def test_max_tokens_must_be_supported_when_listed(self):
        self.assertBlocked({**self.meta, "supported_parameters": ["temperature"]}, "max_tokens is not a supported")

    def test_zero_price_proceeds_with_one_request(self):
        self.assertEqual(self.run_main(body(self.valid_prompt)), 0)
        self.assertEqual(self.chat_calls, 1)


class RetiredNetworkPathTests(unittest.TestCase):
    """2026-10-27: OpenRouter is not an approved provider and this evaluator has no shared-budget ledger, so its only
    network function refuses before any connection (the other tests replace it with fakes)."""

    def test_http_json_refuses_without_opening_a_connection(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")) as urlopen:
            for call in (lambda: ev.http_json("https://openrouter.ai/api/v1/models"),
                         lambda: ev.http_json("https://openrouter.ai/api/v1/chat/completions", method="POST", payload={"x": 1}),
                         lambda: ev.slug_meta("https://openrouter.ai/api/v1", "a/b:free", [])):
                with self.assertRaises(SystemExit) as cm:
                    call()
                self.assertIn("retired", str(cm.exception.code))
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class ReasoningBudgetOptionTests(EvaluatorHarness):
    """--reasoning-max-tokens: live C01 sample 3 spent 21,765 of 24,000 completion tokens on hidden reasoning and was
    truncated after section 5. The option caps reasoning inside the same budget; omitted, the payload is unchanged."""

    def test_omitted_leaves_reasoning_enabled_without_a_cap(self):
        self.run_main(body(self.valid_prompt), "--only", CID, "--max-tokens", "24000")
        self.assertEqual(self.payloads[0]["reasoning"], {"enabled": True})
        self.assertIsNone(self.results().get("requested_reasoning_max_tokens"))

    def test_cap_is_sent_and_recorded(self):
        self.run_main(body(self.valid_prompt), "--only", CID, "--max-tokens", "24000", "--reasoning-max-tokens", "12000")
        p = self.payloads[0]
        self.assertEqual(p["reasoning"], {"enabled": True, "max_tokens": 12000})
        self.assertEqual(p["max_tokens"], 24000)
        self.assertEqual(self.results()["requested_reasoning_max_tokens"], 12000)
        self.assertEqual(self.chat_calls, 1)

    def test_cap_must_leave_room_for_the_document(self):
        for value in ("24000", "30000", "0", "-1", "abc"):
            with self.subTest(value=value), \
                    mock.patch.object(ev, "http_json", side_effect=AssertionError("http_json called")), \
                    mock.patch.object(ev, "slug_meta", side_effect=AssertionError("slug_meta called")), \
                    mock.patch.object(sys, "argv", ["x", "--only", CID, "--max-tokens", "24000", f"--reasoning-max-tokens={value}"]), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    ev.main()
                self.assertEqual(cm.exception.code, 2)

    def test_cap_blocks_when_the_model_does_not_list_reasoning(self):
        self.assertIsNotNone(ev.pricing_block({"model_list_available": True, "pricing": {"prompt": "0", "completion": "0"},
                                               "supported_parameters": ["max_tokens"]}, 24000, reasoning_max_tokens=12000))
        self.assertIsNone(ev.pricing_block({"model_list_available": True, "pricing": {"prompt": "0", "completion": "0"},
                                            "supported_parameters": ["max_tokens", "reasoning"]}, 24000, reasoning_max_tokens=12000))

    def test_builder_default_is_unchanged(self):
        t = Templates.load(ROOT / "prompts")
        req = ev.build_llm_request(t, model="synthetic/test-model:free", provider="openrouter",
                                   repository="synthetic-org/bench-repo", pr_number=C01.pr_number,
                                   test_level="standard", synthetic=True, context=context_for(C01))
        self.assertEqual(req["reasoning"], {"enabled": True})
        groq = ev.build_llm_request(t, model="x", provider="groq", repository="synthetic-org/bench-repo",
                                    pr_number=C01.pr_number, test_level="standard", synthetic=True, context=context_for(C01))
        self.assertNotIn("reasoning", groq)
        # a cap the adapter cannot send is refused, not silently dropped
        with self.assertRaises(GenerationConfigError):
            ev.build_llm_request(t, model="x", provider="groq", reasoning_max_tokens=5000, repository="synthetic-org/bench-repo",
                                 pr_number=C01.pr_number, test_level="standard", synthetic=True, context=context_for(C01))


class OutcomeAndProvenanceTests(EvaluatorHarness):
    """Phase 1 hardening: every attempt has one explicit outcome, diagnostics never pass for responses, and every new
    artifact records the versions it was generated under; legacy artifacts are labelled, never back-filled."""

    def diag(self):
        return json.loads((self.out / f"case-{CID}.diagnostic.json").read_text(encoding="utf-8"))

    def test_truncated_with_complete_looking_text_is_still_truncated(self):
        # a document the gate would accept is still unusable when the provider says it was cut
        r = self.assert_failed_without_gate(body(self.valid_prompt, finish="length"), ev.TRUNCATED_MODEL_OUTPUT)
        self.assertEqual(r["outcome"], "truncated")
        d = self.diag()
        self.assertEqual(d["partial_content_chars"], len(self.valid_prompt))
        self.assertIn("## 1. Role and Objective", d["partial_content"])
        self.assertNotIn(HIDDEN_REASONING, json.dumps(d))

    def test_native_truncation_reason_alone_is_detected(self):
        b = body(self.valid_prompt)
        b["choices"][0]["finish_reason"], b["choices"][0]["native_finish_reason"] = "stop", "MAX_TOKENS"
        r = self.assert_failed_without_gate(b, ev.TRUNCATED_MODEL_OUTPUT)
        self.assertEqual(r["diagnostics"]["native_finish_reason"], "MAX_TOKENS")

    def test_truncated_partial_content_is_redacted(self):
        leaky = self.valid_prompt + "\nAuthorization: Bearer " + FAKE_KEY + "\n"
        self.assert_failed_without_gate(body(leaky, finish="length"), ev.TRUNCATED_MODEL_OUTPUT)
        self.assertNotIn(FAKE_KEY, self.all_written_text())
        self.assertTrue(self.diag()["partial_content_redaction"]["applied"])

    def test_empty_beats_truncated(self):
        r = self.assert_failed_without_gate(body(None, finish="length"), ev.EMPTY_MODEL_CONTENT)
        self.assertEqual(r["outcome"], "empty")

    def test_malformed_outcome(self):
        r = self.assert_failed_without_gate(body(choices=[]), ev.MALFORMED_MODEL_RESPONSE)
        self.assertEqual(r["outcome"], "malformed")

    def test_usable_and_gate_rejected_outcomes(self):
        self.run_main(body(self.valid_prompt))
        self.assertEqual((self.row()["outcome"], self.row()["would_source"]), ("usable", "llm"))
        self.run_main(body("## 1. Role and Objective\nnot a plan"))
        self.assertEqual((self.row()["outcome"], self.row()["would_source"]), ("gate_rejected", "would_fallback"))

    def _transport(self, exc):
        def boom(url, **_kw):
            if url.endswith("/chat/completions"):
                self.chat_calls += 1
                raise exc
            raise AssertionError(url)
        with mock.patch.object(ev, "http_json", side_effect=boom), \
                mock.patch.object(sys, "argv", ["x", "--only", CID]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.main(), 0)
        return self.row()

    def test_timeout_is_a_transport_outcome_and_not_retried(self):
        r = self._transport(TimeoutError("timed out"))
        self.assertEqual((r["failure_reason"], r["outcome"], r["http_status"]), (ev.TRANSPORT_TIMEOUT, "transport_error", 0))
        self.assertEqual(self.chat_calls, 1)
        self.assertTrue(self.results()["stopped_reasons"])
        self.assertEqual(self.diag()["outcome"], "transport_error")

    def test_connection_failure_is_a_transport_outcome(self):
        r = self._transport(ev.urllib.error.URLError("connection refused"))
        self.assertEqual((r["failure_reason"], r["outcome"]), (ev.TRANSPORT_ERROR, "transport_error"))

    def test_request_uses_the_shared_generation_timeout(self):
        seen = {}
        def fake_http(url, **kw):
            seen["timeout"] = kw.get("timeout")
            return 200, body(self.valid_prompt)
        with mock.patch.object(ev, "http_json", side_effect=fake_http), \
                mock.patch.object(sys, "argv", ["x", "--only", CID]), contextlib.redirect_stdout(io.StringIO()):
            ev.main()
        self.assertEqual(seen["timeout"], ev.GENERATION_TIMEOUT_S)
        self.assertGreaterEqual(ev.GENERATION_TIMEOUT_S, 273)    # slowest observed synthetic C01 generation: 272.4 s

    def test_new_artifacts_carry_generation_provenance(self):
        self.run_main(body(self.valid_prompt), "--only", CID, "--max-tokens", "20000", "--reasoning-max-tokens", "9000")
        art = json.loads((self.out / f"case-{CID}.response.json").read_text(encoding="utf-8"))
        g = art["generation"]
        self.assertEqual(g["model_slug"], SYNTHETIC_ENV["PROMPT_LLM_MODEL"])
        self.assertEqual((g["provider"], g["template_version"], g["request_count"], g["retries"]), ("openrouter", "v2", 1, 0))
        self.assertEqual(g["source_sha256"], ev.source_hashes())
        self.assertEqual((g["requested_max_tokens"], g["reasoning"]),
                         (20000, {"enabled": True, "max_tokens": 9000, "explicitly_disabled": False}))
        self.assertRegex(g["generated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual((art["outcome"], g["outcome"]), ("usable", "usable"))
        self.assertNotIn(FAKE_KEY, json.dumps(art))
        meta = json.loads((self.out / "run_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["generation_timeout_s"], ev.GENERATION_TIMEOUT_S)
        self.assertIn("not guarantees", meta["reasoning"]["note"])

    def test_diagnostic_carries_generation_provenance(self):
        self.run_main(body(""))
        self.assertEqual(self.diag()["generation"]["outcome"], "empty")
        self.assertEqual(self.diag()["generation"]["source_sha256"], ev.source_hashes())

    def test_invalid_settings_fail_before_config_network_or_artifacts(self):
        for argv in (["--max-tokens", "0"], ["--max-tokens", "24001"], ["--max-tokens", "100", "--reasoning-max-tokens", "100"]):
            with self.subTest(argv=argv), \
                    mock.patch.object(ev, "load_env_records", side_effect=AssertionError("config read")), \
                    mock.patch.object(ev, "http_json", side_effect=AssertionError("network")), \
                    mock.patch.object(sys, "argv", ["x", "--only", CID, *argv]), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                ev.main()
            self.assertEqual(cm.exception.code, 2)
        self.assertEqual(list(self.out.iterdir()), [])

    def merge_rows(self):
        with mock.patch.object(ev, "http_json", side_effect=AssertionError("http_json called")), \
                mock.patch.object(ev, "slug_meta", side_effect=AssertionError("slug_meta called")), \
                mock.patch.object(sys, "argv", ["x", "--merge"]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.main(), 0)
        return self.results()

    def write_legacy(self, name, obj):
        (self.out / name).write_text(json.dumps(obj), encoding="utf-8")

    def test_merge_marks_legacy_truncated_response_and_never_scores_it(self):
        self.write_legacy(f"case-{CID}.response.json", {"cid": CID, "http_status": 200, "seconds": 1.0, "finish_reason": "length",
                                                       "usage": {}, "content": self.valid_prompt})
        with mock.patch.object(ev, "validate_generated_prompt", side_effect=AssertionError("gate called")):
            res = self.merge_rows()
        r = res["results"][0]
        self.assertEqual((r["outcome"], r["would_source"], r["request"]), ("truncated", "would_fallback", "failed"))
        self.assertNotIn("quality_score", r)
        self.assertEqual((r["version_status"], r["sources_changed_since_generation"]), ("unknown", None))
        self.assertEqual(res["evaluation_mode"], "offline_rescore")

    def test_merge_labels_current_historical_and_unknown(self):
        self.run_main(body(self.valid_prompt))                    # current sources
        second = next(c for c in CASES if c.cid != CID)
        self.write_legacy(f"case-{second.cid}.response.json", {"cid": second.cid, "http_status": 200, "seconds": 1.0,
                                                              "finish_reason": "stop", "usage": {}, "content": self.valid_prompt,
                                                              "source_sha256": {k: "0" * 64 for k in ev.HASHED_SOURCES}})
        third = [c for c in CASES if c.cid not in (CID, second.cid)][0]
        self.write_legacy(f"case-{third.cid}.response.json", {"cid": third.cid, "http_status": 200, "seconds": 1.0,
                                                             "finish_reason": "stop", "usage": {}, "content": self.valid_prompt})
        res = self.merge_rows()
        status = {r["cid"]: r["version_status"] for r in res["results"]}
        self.assertEqual(status, {CID: "current", second.cid: "historical", third.cid: "unknown"})
        self.assertEqual(res["usage_totals"]["version_status"], {"current": 1, "historical": 1, "unknown": 1})
        rows = {r["cid"]: r for r in res["results"]}
        self.assertEqual(rows[CID]["outcome"], "usable")
        self.assertIsNone(rows[third.cid]["generation"])          # never back-filled

    def test_merge_maps_legacy_diagnostic_reasons_to_outcomes(self):
        for reason, outcome in (("EMPTY_MODEL_CONTENT", "empty"), ("HTTP_0", "transport_error"), ("HTTP_502", "http_error"),
                                ("MALFORMED_MODEL_RESPONSE", "malformed"), ("SOMETHING_NEW", None)):
            with self.subTest(reason=reason):
                for f in self.out.glob("case-*"):
                    f.unlink()
                self.write_legacy(f"case-{CID}.diagnostic.json", {"cid": CID, "request": "failed", "failure_reason": reason})
                r = self.merge_rows()["results"][0]
                self.assertEqual((r["outcome"], r["would_source"], r["version_status"]), (outcome, "would_fallback", "unknown"))

    def test_diagnostic_is_never_globbed_as_a_response(self):
        self.run_main(body(self.valid_prompt, finish="length"))
        self.assertEqual(sorted(p.name for p in self.out.glob("case-*.response.json")), [])
        r = self.merge_rows()["results"][0]
        self.assertEqual((r["row_source"], r["outcome"], r["version_status"]), ("rebuilt_from_diagnostic", "truncated", "current"))


class CandidateOverrideTests(EvaluatorHarness):
    """--model / --disable-reasoning: an evaluator-only candidate run. The configured slug (compose/.env) is never
    changed, reasoning is opted out explicitly, and every pre-generation guard still applies to the candidate."""

    CANDIDATE = "synthetic-vendor/candidate-model:free"

    def setUp(self):
        super().setUp()
        self.meta["supported_parameters"] = ["max_tokens", "reasoning", "temperature"]
        self.meta_slugs = []
        orig = dict(self.meta)

        def meta_for(base, slug, headers):
            self.meta_slugs.append(slug)
            return dict(self.meta) if self.meta else orig
        p = mock.patch.object(ev, "slug_meta", side_effect=meta_for)
        p.start()
        self.addCleanup(p.stop)

    def run_candidate(self, response=None, *extra):
        return self.run_main(response if response is not None else body(self.valid_prompt), "--only", CID, "--max-tokens", "24000",
                             "--model", self.CANDIDATE, "--disable-reasoning", *extra)

    def test_payload_uses_candidate_and_explicitly_disables_reasoning(self):
        self.assertEqual(self.run_candidate(), 0)
        self.assertEqual(self.chat_calls, 1)
        payload = self.payloads[0]
        self.assertEqual(payload["model"], self.CANDIDATE)
        self.assertEqual(payload["reasoning"], {"enabled": False})        # no cap, no effort alongside
        self.assertEqual((payload["max_tokens"], payload["temperature"]), (24000, 0.2))
        self.assertEqual(self.meta_slugs, [self.CANDIDATE])                # price/availability checked for the candidate

    def test_override_is_recorded_and_never_persisted(self):
        self.run_candidate()
        meta = json.loads((self.out / "run_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["model_slug"], self.CANDIDATE)
        self.assertEqual(meta["model_override"]["configured_slug"], SYNTHETIC_ENV["PROMPT_LLM_MODEL"])
        self.assertEqual(meta["reasoning"]["explicitly_disabled"], True)
        art = json.loads((self.out / f"case-{CID}.response.json").read_text(encoding="utf-8"))
        self.assertEqual(art["generation"]["model_slug"], self.CANDIDATE)
        self.assertEqual(art["generation"]["reasoning"], {"enabled": False, "max_tokens": None, "explicitly_disabled": True})
        self.assertEqual(self.row()["model_slug"], self.CANDIDATE)

    def test_without_override_the_configured_model_and_default_reasoning_are_sent(self):
        self.run_main(body(self.valid_prompt))
        self.assertEqual(self.payloads[0]["model"], SYNTHETIC_ENV["PROMPT_LLM_MODEL"])
        self.assertEqual(self.payloads[0]["reasoning"], {"enabled": True})
        meta = json.loads((self.out / "run_meta.json").read_text(encoding="utf-8"))
        self.assertIsNone(meta["model_override"])

    def test_nonzero_or_missing_price_blocks_the_candidate(self):
        for pricing in ({"prompt": "0", "completion": "0.000001"}, {"prompt": "0"}, None):
            with self.subTest(pricing=pricing):
                self.meta["pricing"] = pricing
                self.chat_calls = 0
                self.assertEqual(self.run_candidate(), 3)
                self.assertEqual(self.chat_calls, 0)

    def test_reasoning_must_be_a_listed_parameter(self):
        self.meta["supported_parameters"] = ["max_tokens", "temperature"]
        self.assertEqual(self.run_candidate(), 3)
        self.assertEqual(self.chat_calls, 0)
        self.assertIn("reasoning-disabled", self.stdout)

    def test_unlisted_candidate_is_blocked(self):
        self.meta.clear()
        self.meta.update({"model_list_available": False, "status": 200})
        self.assertEqual(self.run_candidate(), 3)
        self.assertEqual(self.chat_calls, 0)

    def test_invalid_combinations_fail_before_config_or_network(self):
        for argv in (["--model", "not a slug"], ["--model", "a/b c"], ["--disable-reasoning", "--reasoning-max-tokens", "1000"]):
            with self.subTest(argv=argv), \
                    mock.patch.object(ev, "load_env_records", side_effect=AssertionError("config read")), \
                    mock.patch.object(ev, "http_json", side_effect=AssertionError("network")), \
                    mock.patch.object(sys, "argv", ["x", "--only", CID, *argv]), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                ev.main()
            self.assertEqual(cm.exception.code, 2)

    def test_check_config_with_override_stays_offline(self):
        out = io.StringIO()
        with mock.patch.object(ev, "http_json", side_effect=AssertionError("http_json called")) as http, \
                mock.patch.object(ev, "slug_meta", side_effect=AssertionError("slug_meta called")) as meta, \
                mock.patch.object(ev, "build_llm_request", side_effect=AssertionError("request built")) as build, \
                mock.patch.object(sys, "argv", ["x", "--check-config", "--max-tokens", "24000", "--model", self.CANDIDATE,
                                                "--disable-reasoning"]), contextlib.redirect_stdout(out):
            self.assertEqual(ev.main(), 0)
        for m in (http, meta, build):
            m.assert_not_called()
        self.assertIn("evaluator-only override", out.getvalue())
        self.assertEqual(list(self.out.iterdir()), [])

    def test_failed_candidate_attempt_is_not_retried(self):
        self.run_candidate(body(None, finish="length"))
        self.assertEqual(self.chat_calls, 1)
        self.assertEqual(self.row()["outcome"], "empty")


class ProviderErrorBodyTests(EvaluatorHarness):
    """HTTP 200 bodies that carry a provider `error` object instead of choices (the Xing4.0 attempt lost this): an
    allowlisted, sanitized summary is kept, the raw body never is, and nothing reaches the gate, the rubric or a retry."""

    FAKE_BEARER = "Bearer abcDEF1234567890ghijKLMNOPqrstuv"
    FAKE_GSK = "gsk_" + "Q" * 40
    FAKE_SK = "sk-" + "Z" * 40

    def err_body(self, error, **extra):
        b = {"id": "gen-synthetic-err-1", "model": "synthetic/test-model:free", "provider": "SyntheticProvider", "error": error}
        b.update(extra)
        return b

    def diag(self):
        return json.loads((self.out / f"case-{CID}.diagnostic.json").read_text(encoding="utf-8"))

    def pe(self):
        return self.diag()["diagnostics"]["provider_error"]

    # 1, 2, 9, 14, 15
    def test_structured_error_is_a_provider_error_with_code_and_message(self):
        second = next(c.cid for c in CASES if c.cid != CID)
        with mock.patch.object(ev, "validate_generated_prompt", side_effect=AssertionError("gate called")), \
                mock.patch.object(ev.qa_rubric, "score_prompt", side_effect=AssertionError("rubric called")):
            self.run_main(self.err_body({"code": 502, "message": "Upstream provider returned an error",
                                         "metadata": {"provider_name": "SyntheticUpstream", "raw": {"nested": "x" * 50}}}),
                          "--only", f"{CID},{second}")
        self.assertEqual(self.chat_calls, 1)                                  # no retry, run stops
        r = self.row()
        self.assertEqual((r["http_status"], r["request"], r["failure_reason"], r["outcome"], r["would_source"]),
                         (200, "failed", ev.PROVIDER_ERROR_RESPONSE, "provider_error", "would_fallback"))
        self.assertNotIn("gate_valid", r)
        self.assertEqual(self.pe(), {"code": 502, "provider_name": "SyntheticUpstream",
                                     "message": "Upstream provider returned an error", "message_status": "kept"})
        d = self.diag()
        self.assertEqual(d["diagnostics"]["http_status"], 200)
        self.assertEqual(d["diagnostics"]["request_id"], "gen-synthetic-err-1")
        self.assertEqual(d["diagnostics"]["model_returned"], "synthetic/test-model:free")
        self.assertNotIn("raw", json.dumps(d["diagnostics"]))               # arbitrary nested objects are dropped
        self.assertFalse((self.out / f"case-{CID}.response.json").exists())

    # 3
    def test_missing_code_stays_missing(self):
        self.run_main(self.err_body({"message": "no code here"}))
        self.assertNotIn("code", self.pe())
        self.assertEqual(self.pe()["message"], "no code here")

    def test_string_codes_type_and_param_are_allowlisted(self):
        self.run_main(self.err_body({"code": "invalid_request_error", "type": "invalid_request", "param": "reasoning",
                                     "message": "reasoning is not supported"}))
        self.assertEqual((self.pe()["code"], self.pe()["type"], self.pe()["param"]), ("invalid_request_error", "invalid_request", "reasoning"))

    # 4
    def test_malformed_error_objects_do_not_crash(self):
        for error in ({"code": {"x": 1}, "message": {"y": 2}, "type": "bad type with spaces", "param": ["p"]},
                      {"code": True, "message": None}, {}):
            with self.subTest(error=error):
                self.chat_calls = 0
                self.assertEqual(self.run_main(self.err_body(error)), 0)
                pe = self.pe()
                self.assertEqual(set(pe) - {"message_status"}, set())
                self.assertIn(pe["message_status"], ("not_text", "absent"))
                self.assertEqual(self.row()["outcome"], "provider_error")

    def test_non_object_error_is_not_invented_into_a_provider_error(self):
        for error in ("plain string error", ["list", "error"], 42):
            with self.subTest(error=error):
                self.chat_calls = 0
                r = self.assert_failed_without_gate(self.err_body(error), ev.MALFORMED_MODEL_RESPONSE)
                self.assertEqual(r["outcome"], "malformed")
                self.assertEqual(self.pe()["message_status"], "unparsed")
                self.assertNotIn("plain string error", self.all_written_text())

    # 5
    def test_large_messages_are_capped_or_omitted(self):
        self.run_main(self.err_body({"code": 500, "message": "word " * 100}))
        self.assertLessEqual(len(self.pe()["message"]), ev.ERROR_MESSAGE_MAX_CHARS + 1)
        self.assertTrue(self.pe()["message"].endswith("…"))
        self.run_main(self.err_body({"code": 500, "message": "x" * (ev.ERROR_MESSAGE_MAX_INPUT + 1)}))
        self.assertEqual(self.pe(), {"code": 500, "message_status": "too_long"})

    # 6, 7, 19
    def test_credentials_in_error_text_are_redacted(self):
        msg = (f"auth failed: Authorization: {self.FAKE_BEARER}; key {FAKE_KEY}; groq {self.FAKE_GSK}; "
               f"openai {self.FAKE_SK}; api_key=supersecretvalue123")
        self.run_main(self.err_body({"code": 401, "message": msg}))
        text = self.all_written_text()
        for secret in (self.FAKE_BEARER.split()[1], FAKE_KEY, self.FAKE_GSK, self.FAKE_SK, "supersecretvalue123"):
            self.assertNotIn(secret, text)
        self.assertEqual(self.pe()["message_status"], "redacted")
        self.assertIn("[REDACTED", self.pe()["message"])

    def test_configured_key_is_never_kept_even_in_an_unusual_shape(self):
        self.run_main(self.err_body({"code": 400, "message": f"bad key<{FAKE_KEY}>"}))
        self.assertNotIn(FAKE_KEY, self.all_written_text())

    # 8
    def test_echoed_request_content_is_not_persisted(self):
        system = Templates.load(ROOT / "prompts").system
        echoed = system[400:470]
        for message in (f"could not parse: {echoed}", "bad input near <<<PR_CONTEXT>>> {...}",
                        'invalid body {"messages": [{"role": "user"}]}'):
            with self.subTest(message=message[:30]):
                self.run_main(self.err_body({"code": 400, "message": message}))
                self.assertEqual(self.pe(), {"code": 400, "message_status": "echo"})
                self.assertNotIn(echoed, self.all_written_text())

    def test_error_on_the_first_choice_is_recognised(self):
        b = body(None, finish="error")
        b["choices"][0]["error"] = {"code": 429, "message": "rate limited upstream"}
        self.run_main(b)
        self.assertEqual(self.row()["outcome"], "provider_error")
        self.assertEqual((self.pe()["code"], self.diag()["diagnostics"]["finish_reason"]), (429, "error"))
        self.assertNotIn(HIDDEN_REASONING, self.all_written_text())

    def test_content_wins_over_a_stray_error_field(self):
        b = body(self.valid_prompt)
        b["error"] = {"code": 1, "message": "ignored"}
        self.run_main(b)
        self.assertEqual(self.row()["outcome"], "usable")

    # 10, 11
    def test_no_choices_and_no_error_is_still_malformed(self):
        r = self.assert_failed_without_gate(body(choices=[]), ev.MALFORMED_MODEL_RESPONSE)
        self.assertNotIn("provider_error", r["diagnostics"])

    def test_empty_content_without_error_is_still_empty(self):
        r = self.assert_failed_without_gate(body("   "), ev.EMPTY_MODEL_CONTENT)
        self.assertEqual(r["outcome"], "empty")

    # 12, 13
    def test_usage_is_kept_only_when_supplied(self):
        self.run_main(self.err_body({"code": 502, "message": "x"}))
        self.assertNotIn("usage", self.diag()["diagnostics"])
        self.assertIsNone(self.diag()["generation"]["reasoning_tokens_reported"])
        usage = {"prompt_tokens": 12, "completion_tokens": 0, "total_tokens": 12,
                 "completion_tokens_details": {"reasoning_tokens": 0}}
        self.run_main(self.err_body({"code": 502, "message": "x"}, usage=usage))
        self.assertEqual(self.diag()["diagnostics"]["usage"],
                         {"prompt_tokens": 12, "completion_tokens": 0, "total_tokens": 12, "reasoning_tokens": 0})
        self.assertEqual(self.diag()["generation"]["reasoning_tokens_reported"], 0)

    def test_requested_and_returned_models_stay_distinct(self):
        self.run_main(self.err_body({"code": 502, "message": "x"}, model="other-vendor/served-model"),
                      "--only", CID, "--model", "synthetic-vendor/candidate:free", "--disable-reasoning")
        g = self.diag()["generation"]
        self.assertEqual((g["model_slug"], g["model_returned"]), ("synthetic-vendor/candidate:free", "other-vendor/served-model"))
        self.assertEqual(g["reasoning"]["explicitly_disabled"], True)
        self.assertIsNone(g["reasoning_tokens_reported"])                    # disabled was requested, not shown

    # 16
    def test_provider_error_diagnostic_never_merges_as_success(self):
        self.run_main(self.err_body({"code": 502, "message": "x"}))
        self.assertEqual(sorted(p.name for p in self.out.glob("case-*.response.json")), [])
        with mock.patch.object(ev, "http_json", side_effect=AssertionError("network")), \
                mock.patch.object(ev, "slug_meta", side_effect=AssertionError("network")), \
                mock.patch.object(ev, "validate_generated_prompt", side_effect=AssertionError("gate called")), \
                mock.patch.object(sys, "argv", ["x", "--merge"]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.main(), 0)
        r = self.results()["results"][0]
        self.assertEqual((r["row_source"], r["outcome"], r["request"], r["would_source"]),
                         ("rebuilt_from_diagnostic", "provider_error", "failed", "would_fallback"))
        self.assertNotIn("quality_score", r)

    # 17
    def test_non_200_error_text_is_sanitized_and_classification_unchanged(self):
        self.run_main({"error": {"code": 429, "message": f"slow down, {self.FAKE_BEARER}"}}, status=429)
        r = self.row()
        self.assertEqual((r["failure_reason"], r["outcome"], r["http_status"]), ("HTTP_429", "http_error", 429))
        self.assertTrue(r["error"].startswith("provider error 429: slow down"))
        self.assertEqual(self.pe()["code"], 429)
        self.assertNotIn(self.FAKE_BEARER.split()[1], self.all_written_text())
        self.assertEqual(self.chat_calls, 1)

    def test_non_200_plain_text_body_is_sanitized(self):
        self.run_main(f"<html>gateway error {FAKE_KEY}</html>", status=502)
        self.assertEqual(self.row()["failure_reason"], "HTTP_502")
        self.assertNotIn(FAKE_KEY, self.all_written_text())

    # 20
    def test_safety_preconditions_still_block_before_any_request(self):
        unsafe = dict(SYNTHETIC_ENV, AUTOMATION_MODE="live")
        with mock.patch.object(ev, "load_env_records", lambda: dict(unsafe)), self.assertRaises(SystemExit):
            self.run_main(self.err_body({"code": 502, "message": "x"}))
        self.assertEqual(self.chat_calls, 0)

    def test_sanitizer_unit_cases(self):
        f = ev.sanitize_provider_message
        self.assertEqual(f(None), (None, "absent"))
        self.assertEqual(f(123), (None, "not_text"))
        self.assertEqual(f("   "), (None, "absent"))
        self.assertEqual(f("ok  text\n\twith   spaces"), ("ok text with spaces", "kept"))
        self.assertEqual(f("abc", echo_source="xyz")[1], "kept")
        self.assertEqual(f("prefix " + "q" * 40, echo_source="zz" + "q" * 40)[1], "echo")



class CrossProviderKeyTests(unittest.TestCase):
    """The OpenRouter evaluator never sends a Groq-format key, and names the key's variable (never its value)."""
    BASE_ENV = {"AUTOMATION_MODE": "review_only", "PROMPT_LLM_LIVE_IN_REVIEW_ONLY": "false",
                "PROMPT_LLM_API_BASE": "https://openrouter.ai/api/v1", "PROMPT_LLM_MODEL": "vendor/model:free"}

    def test_groq_format_key_is_refused_without_echoing_it(self):
        groq_shaped = "gsk_" + "g" * 52                    # synthetic, Groq key shape; never a real key
        for var in ("PROMPT_LLM_API_KEY", "OPENROUTER_API_KEY"):
            with self.subTest(var=var), self.assertRaises(SystemExit) as cm:
                ev.verify_preconditions({**self.BASE_ENV, var: groq_shaped})
            self.assertIn("Groq key format", str(cm.exception.code))
            self.assertNotIn(groq_shaped, str(cm.exception.code))

    def test_key_source_names_the_variable(self):
        self.assertEqual(ev.verify_preconditions({**self.BASE_ENV, "PROMPT_LLM_API_KEY": FAKE_KEY})["key_source"],
                         "PROMPT_LLM_API_KEY")
        self.assertEqual(ev.verify_preconditions({**self.BASE_ENV, "OPENROUTER_API_KEY": FAKE_KEY})["key_source"],
                         "OPENROUTER_API_KEY")

    def test_missing_key_blocks(self):
        with self.assertRaises(SystemExit) as cm:
            ev.verify_preconditions(dict(self.BASE_ENV))
        self.assertIn("missing/placeholder", str(cm.exception.code))
