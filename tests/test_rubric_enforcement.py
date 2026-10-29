"""The production worker enforces the 85-point rubric (2026-10-26).

Before: /prompts/finalize accepted any complete candidate the deterministic validator passed, so the gate-valid Groq
C01 run-4 output (rubric 78.0) came back `source: llm`. Now a candidate is used only when the validator passes with
no errors, the shared rubric (prqa_worker.qa_rubric, the same module the evaluators and the benchmark import) reports
no hard fail, and its score is a finite number >= 85. Anything else, including a scoring failure, returns the
deterministic fallback. Offline: no network, no model call; pinned outputs are only read, never printed.
"""
from __future__ import annotations

import importlib
import json
import math
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts" / "eval"))

from prqa_worker import prompt_builder as pb  # noqa: E402
from prqa_worker import qa_rubric as rubric  # noqa: E402
from prqa_worker.server import App  # noqa: E402
from tests.test_groq_c01_regressions import C01, _GroqC01  # noqa: E402

PINNED = ROOT / "fixtures" / "recorded" / "groq-c01"
RUN3 = PINNED / "groq-c01-gate-rejected-20261026-run3" / "case-C01-clear-behaviour.response.json"
RUN4 = PINNED / "groq-c01-gate-valid-rubric78-20261026-run4" / "case-C01-clear-behaviour.response.json"
WF03 = ROOT / "n8n" / "workflows" / "03-prqa-llm-prompt-builder.json"
AUTH_HDR = {"x-worker-token": "test-token"}
BAD = "## 1. Role and Objective\nnot a plan"


def fake(score, hard=()):
    return mock.Mock(return_value=SimpleNamespace(quality_score=score, hard_fails=list(hard)))


class _Finalize(_GroqC01):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.t = pb.Templates.load(ROOT / "prompts")
        # a gate-valid complete plan distinguishable from the fallback; its real score is irrelevant once mocked
        cls.candidate = cls.good.replace("Act as a Senior QA Engineer", "Act as a skeptical Senior QA Engineer", 1)
        assert cls.candidate != cls.good

    def finalize(self, text, **kw):
        kw.setdefault("finish_reason", "stop")
        return pb.finalize_prompt(self.t, generated_text=text, repository="synthetic-org/bench-repo",
                                  pr_number=C01.pr_number, test_level="standard", context=self.ctx, synthetic=True, **kw)

    def fallback_text(self):
        return self.finalize(None)["prompt"]["text"]

    def with_score(self, score, hard=(), text=None):
        with mock.patch.object(rubric, "score_prompt", fake(score, hard)):
            return self.finalize(self.candidate if text is None else text)

    def assert_fallback(self, out, failure):
        self.assertEqual(out["prompt"]["source"], "reference_fallback")
        self.assertEqual((out["generation"]["outcome"], out["generation"]["failure"]), ("gate_rejected", failure))
        self.assertEqual(out["prompt"]["text"], self.fallback_text())
        self.assertFalse(out["quality"]["accepted"])
        self.assertEqual(out["quality"]["threshold"], 85.0)

    def assert_llm(self, out):
        self.assertEqual(out["prompt"]["source"], "llm")
        self.assertEqual((out["generation"]["outcome"], out["generation"]["failure"]), ("usable", None))
        self.assertTrue(out["quality"]["accepted"])


class ThresholdTests(_Finalize):
    def test_candidate_is_gate_valid(self):
        self.assertTrue(self.validate(self.candidate)["valid"])

    # 1, 2: below the threshold is rejected, however close
    def test_01_02_below_85_is_rejected(self):
        for score in (78.0, 84.99, 84.999999, 0, -5):
            with self.subTest(score=score):
                out = self.with_score(score)
                self.assert_fallback(out, "rubric_threshold")
                self.assertEqual(out["quality"]["score"], round(score, 2))

    # 3, 4: exactly 85 and above are accepted when everything else passes
    def test_03_04_85_and_above_are_accepted(self):
        for score in (85, 85.0, 85.01, 92.0, 100.0):
            with self.subTest(score=score):
                out = self.with_score(score)
                self.assert_llm(out)
                self.assertNotEqual(out["prompt"]["text"], self.fallback_text())

    # 5: a high score never overrides a hard fail
    def test_05_high_score_with_hard_fail_is_rejected(self):
        for hf in ("HF-SCOPE-VIOLATION: invented endpoint", "HF-FABRICATED-FACT: x", "HF-MISSING-CRITICAL: y"):
            with self.subTest(hf=hf):
                out = self.with_score(99.0, [hf])
                self.assert_fallback(out, "rubric_hard_fail")
                self.assertEqual(out["quality"]["hard_fails"], [hf.split(":")[0]])   # ids only, no detail text

    # 6: a high score never overrides gate errors, and the errors are preserved
    def test_06_high_score_with_gate_errors_is_rejected(self):
        out = self.with_score(100.0, text=self.candidate.replace("## 7. ", "## 7x. ", 1))
        self.assert_fallback(out, "gate_rejected")
        self.assertFalse(out["validation"]["valid"])
        self.assertTrue(out["validation"]["errors"])
        self.assertFalse(out["quality"]["gate_valid"])

    def test_gate_valid_flag_with_errors_is_still_rejected(self):
        gate = {"valid": True, "errors": ["something"]}
        self.assertFalse(rubric.accept_result(SimpleNamespace(quality_score=100.0, hard_fails=[]), gate)["accepted"])


class ScoringFailureTests(_Finalize):
    # 7: a missing score
    def test_07_missing_score_falls_back(self):
        for result in (SimpleNamespace(hard_fails=[]), SimpleNamespace(quality_score=None, hard_fails=[]), None, object()):
            with self.subTest(result=type(result).__name__), \
                    mock.patch.object(rubric, "score_prompt", mock.Mock(return_value=result)):
                out = self.finalize(self.candidate)
                self.assertEqual(out["prompt"]["source"], "reference_fallback")
                self.assertIn(out["generation"]["failure"], ("rubric_unavailable", "rubric_threshold"))
                self.assertIsNone(out["quality"]["score"])

    # 8: a scorer exception fails closed
    def test_08_scorer_exception_falls_back(self):
        for exc in (RuntimeError("boom"), ValueError(), ZeroDivisionError(), KeyError("x"), RecursionError()):
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch.object(rubric, "score_prompt", mock.Mock(side_effect=exc)):
                out = self.finalize(self.candidate)
                self.assert_fallback(out, "rubric_unavailable")
                self.assertNotIn("boom", json.dumps(out["quality"]))

    def test_scorer_exception_on_a_gate_invalid_candidate_reports_the_gate(self):
        with mock.patch.object(rubric, "score_prompt", mock.Mock(side_effect=RuntimeError())):
            self.assertEqual(self.finalize(BAD)["generation"]["failure"], "gate_rejected")

    # 9: NaN, infinities, strings, bools and other non-numbers never pass
    def test_09_non_finite_and_non_numeric_scores_fall_back(self):
        for score in (math.nan, math.inf, -math.inf, float("nan"), "90", "85.0", b"90", True, [90], {"s": 90}, None, 1e309):
            with self.subTest(score=repr(score)):
                out = self.with_score(score)
                self.assert_fallback(out, "rubric_threshold")
                self.assertIsNone(out["quality"]["score"])
        self.assertFalse(rubric.score_passes(True))   # bool is an int subclass; never a score

    def test_malformed_hard_fails_fail_closed(self):
        for hard in (None, "HF-X", [1], [None]):
            with self.subTest(hard=repr(hard)), mock.patch.object(
                    rubric, "score_prompt", mock.Mock(return_value=SimpleNamespace(quality_score=99.0, hard_fails=hard))):
                self.assert_fallback(self.finalize(self.candidate), "rubric_unavailable")

    def test_score_passes_boundaries(self):
        self.assertFalse(rubric.score_passes(84.99))
        self.assertFalse(rubric.score_passes(85 - 1e-9))
        self.assertTrue(rubric.score_passes(85))
        self.assertTrue(rubric.score_passes(85.0))
        self.assertEqual(rubric.THRESHOLD, 85.0)


class FallbackAndGenerationTests(_Finalize):
    # 10: the fallback is never scored as a candidate
    def test_10_fallback_is_never_scored(self):
        for kw in (dict(llm_error="missing_credential"), dict(llm_error="x", http_status="413"),
                   dict(response_shape="no_choices"), dict(response_shape="non_json_body"), dict(finish_reason="length")):
            with self.subTest(kw=sorted(kw)):
                scorer = fake(100.0)
                with mock.patch.object(rubric, "score_prompt", scorer):
                    out = pb.finalize_prompt(self.t, generated_text=None if "finish_reason" not in kw else self.candidate,
                                             repository="r/r", pr_number=1, test_level="standard", context=self.ctx,
                                             synthetic=True, **kw)
                scorer.assert_not_called()
                self.assertEqual(out["prompt"]["source"], "reference_fallback")
                self.assertIsNone(out["quality"])
        # a rejected candidate is scored exactly once, and the fallback that replaces it is not scored at all
        scorer = fake(50.0)
        with mock.patch.object(rubric, "score_prompt", scorer):
            out = self.finalize(self.candidate)
        scorer.assert_called_once()
        self.assertEqual(scorer.call_args.args[0], self.candidate)
        self.assertEqual(out["prompt"]["source"], "reference_fallback")

    # 13: a score rejection returns the deterministic prompt, not source llm
    def test_13_score_rejection_returns_the_deterministic_prompt(self):
        out = self.with_score(84.99)
        self.assertNotEqual(out["prompt"]["source"], "llm")
        expected = pb.reference_fallback_prompt(self.t, repository="synthetic-org/bench-repo", pr_number=C01.pr_number,
                                                test_level="standard", context=self.ctx)
        self.assertIn(expected.strip()[:400], out["prompt"]["text"])
        self.assertIn(self.t.contract.strip()[:200], out["prompt"]["text"])

    # 14: the reason distinguishes a rubric rejection from a gate failure
    def test_14_reasons_are_distinct(self):
        reasons = {self.with_score(70.0)["generation"]["failure"],
                   self.with_score(99.0, ["HF-SCOPE-VIOLATION: x"])["generation"]["failure"],
                   self.with_score(99.0, text=BAD)["generation"]["failure"]}
        with mock.patch.object(rubric, "score_prompt", mock.Mock(side_effect=RuntimeError())):
            reasons.add(self.finalize(self.candidate)["generation"]["failure"])
        self.assertEqual(reasons, {"rubric_threshold", "rubric_hard_fail", "gate_rejected", "rubric_unavailable"})
        for f in reasons:
            self.assertEqual(pb.FAILURE_OUTCOME[f], pb.GENERATION_GATE_REJECTED)

    # 16: rejection never triggers another generation or any network call
    def test_16_no_second_generation(self):
        def no_net(*a, **k):
            raise AssertionError("network attempted during finalize")
        with mock.patch.object(pb, "build_llm_messages", side_effect=AssertionError("regenerated")) as msgs, \
                mock.patch.object(pb, "build_llm_request", side_effect=AssertionError("regenerated")) as req, \
                mock.patch.object(socket.socket, "connect", no_net), mock.patch.object(socket, "create_connection", no_net):
            for score in (10.0, 84.99, math.nan):
                self.assert_fallback(self.with_score(score), "rubric_threshold")
            with mock.patch.object(rubric, "score_prompt", mock.Mock(side_effect=RuntimeError())):
                self.finalize(self.candidate)
        msgs.assert_not_called()
        req.assert_not_called()

    def test_quality_metadata_carries_no_prompt_content(self):
        out = self.with_score(70.0, ["HF-FABRICATED-FACT: line 21 says `reason` is required"])
        q = json.dumps(out["quality"])
        self.assertEqual(set(out["quality"]), {"accepted", "failure", "score", "threshold", "hard_fails", "gate_valid"})
        self.assertNotIn("line 21", q)
        self.assertNotIn("Senior QA", q)


class PinnedLiveOutputTests(_Finalize):
    """The historical outputs are read, never modified, and re-run through today's worker decision."""

    def content(self, path):
        return json.loads(path.read_text(encoding="utf-8"))["content"]

    def test_run4_gate_valid_78_now_falls_back(self):
        raw = RUN4.read_bytes()
        out = self.finalize(self.content(RUN4))
        self.assertTrue(out["validation"]["valid"])   # the validator alone still passes it
        self.assert_fallback(out, "rubric_threshold")
        self.assertEqual(out["quality"]["score"], 78.0)
        self.assertEqual(out["quality"]["hard_fails"], [])
        self.assertEqual(RUN4.read_bytes(), raw)

    # 15: hard-fail and gate diagnostics are preserved
    def test_15_run3_keeps_gate_errors_and_hard_fail_ids(self):
        out = self.finalize(self.content(RUN3))
        self.assert_fallback(out, "gate_rejected")
        self.assertEqual(len(out["validation"]["errors"]), 3)
        self.assertIn("HF-FABRICATED-FACT", out["quality"]["hard_fails"])
        self.assertLess(out["quality"]["score"], 85)


class SharedRubricTests(unittest.TestCase):
    # 11: the worker and the evaluators use the same rubric object
    def test_11_worker_and_evaluators_share_one_rubric(self):
        import qa_rubric as legacy_name          # tests/qa_rubric.py is an alias, not a copy
        self.assertIs(legacy_name, rubric)
        for name in ("evaluate_groq_live", "evaluate_openrouter_live"):
            with self.subTest(name=name):
                mod = importlib.import_module(name)
                self.assertIs(mod.qa_rubric, rubric)
                src = (ROOT / "scripts" / "eval" / f"{name}.py").read_text(encoding="utf-8")
                self.assertNotIn('if gate["valid"] else "would_fallback"', src)
                self.assertGreaterEqual(src.count("qa_rubric.accept_result(rub, gate)"), 2)
        self.assertIn("worker/prqa_worker/qa_rubric.py", importlib.import_module("evaluate_openrouter_live").HASHED_SOURCES)
        self.assertLess(len((ROOT / "tests" / "qa_rubric.py").read_text(encoding="utf-8")), 1000)

    # 12: the benchmark uses the same definition and threshold
    def test_12_benchmark_shares_the_definition(self):
        bench = importlib.import_module("evaluate_prompt_quality")
        self.assertIs(bench.qa_rubric, rubric)
        self.assertEqual(bench.THRESHOLD, rubric.THRESHOLD)

    def test_single_threshold_definition(self):
        hits = [p for p in list((ROOT / "worker").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py"))
                + list((ROOT / "tests").glob("*.py"))
                if "THRESHOLD = " + "85" in p.read_text(encoding="utf-8")]
        self.assertEqual(hits, [ROOT / "worker" / "prqa_worker" / "qa_rubric.py"])

    def test_rubric_module_is_pure(self):
        src = (ROOT / "worker" / "prqa_worker" / "qa_rubric.py").read_text(encoding="utf-8")
        for banned in ("urllib", "http.client", "socket", "requests", "subprocess", "open(", "os.environ"):
            self.assertNotIn(banned, src)


class Workflow03Tests(unittest.TestCase):
    # 17: workflow 03 consumes the worker decision and stays inactive
    def test_17_workflow_03_follows_the_worker(self):
        wf = json.loads(WF03.read_text(encoding="utf-8"))
        self.assertFalse(wf.get("active"))
        nodes = {n["name"]: n for n in wf["nodes"]}
        fin = nodes["Validate Prompt / Fallback to Reference (worker)"]
        self.assertIn("/prompts/finalize", json.dumps(fin["parameters"]))
        cond = json.dumps(nodes["Prompt Source?"]["parameters"])
        self.assertIn("$json.prompt.source", cond)
        self.assertIn('"llm"', cond)
        conns = wf["connections"]
        self.assertEqual([c["node"] for c in conns[fin["name"]]["main"][0]], ["Prompt Source?"])
        attach = json.dumps(nodes["Attach Prompt to Pipeline Data"]["parameters"])
        self.assertIn("const fin = $input.first().json", attach)   # the finalize response, via Prompt Source?
        self.assertIn("prompt: fin.prompt", attach)
        self.assertNotIn("generated_text", attach)   # the raw candidate never bypasses the worker
        # every path into the attach node comes from the worker's decision branch
        into_attach = {src for src, c in conns.items() for out in c.get("main", []) for t in out
                       if t["node"] == "Attach Prompt to Pipeline Data"}
        self.assertEqual(into_attach, {"Prompt: LLM-generated ✓", "Prompt: Reference fallback"})


class WorkerRouteTests(_Finalize):
    """The HTTP route returns the same decision (offline, in-process)."""

    def test_route_rejects_run4_and_accepts_with_a_passing_score(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = App({"AUTOMATION_MODE": "review_only", "PRQA_WORKER_TOKEN": "test-token",
                   "PRQA_REGISTRY_PATH": str(ROOT / "config/repositories.yaml"), "PRQA_SCHEMA_DIR": str(ROOT / "schemas"),
                   "PRQA_PROMPTS_DIR": str(ROOT / "prompts"), "PRQA_FIXTURES_DIR": str(ROOT / "fixtures"),
                   "PRQA_DATA_DIR": tmp.name, "MOCK_EXECUTION_DELAY_S": "0"})
        self.addCleanup(app.executor.stop)
        body = {"repository": "synthetic-org/bench-repo", "pr_number": C01.pr_number, "test_level": "standard",
                "synthetic": True, "context": self.ctx, "finish_reason": "stop",
                "generated_text": json.loads(RUN4.read_text(encoding="utf-8"))["content"]}
        status, out = app.route("POST", "/prompts/finalize", {}, body, AUTH_HDR)
        self.assertEqual(status, 200)
        self.assertEqual((out["prompt"]["source"], out["generation"]["failure"]), ("reference_fallback", "rubric_threshold"))
        with mock.patch.object(rubric, "score_prompt", fake(85.0)):
            status, out = app.route("POST", "/prompts/finalize", {}, {**body, "generated_text": self.candidate},
                                    AUTH_HDR)
        self.assertEqual((status, out["prompt"]["source"]), (200, "llm"))


if __name__ == "__main__":
    unittest.main()
