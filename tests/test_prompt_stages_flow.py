"""Staged prompt generation through the n8n stages (prepare → promptstep × n → gate), offline.

Same world as tests/test_stages.py (fake GitHub/Docker host, real in-process worker and gate); the model answers are
canned. Checks that the loop never creates a job, grant or OpenCode process, that the assembled prompt reaches the
unchanged gate, and that a rejected assembled prompt is never executed.

Run: cd "<project>" && PYTHONPATH=worker python3 -m unittest tests.test_prompt_stages_flow -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))

from tests.test_stages import S, StageBase  # noqa: E402
from tests.test_run_single_pr import R, SHA  # noqa: E402

OP = "POST /api/v1/organizations"


def answers() -> list[dict]:
    scope = {"summary": "Organization creation accepts working days.",
             "changed_behavior": [{"text": "`OrganizationCreate` gains a `working_days` field", "source": "diff", "ref": "backend/app/schemas/org.py"}],
             "requirements": [{"id": "S1", "header": "Working days on organization creation",
                               "claims": [{"text": "POST /organizations accepts working_days; omitted = the default week", "source": "description", "ref": "S1"}]}],
             "operations": [{"op": OP, "source": "description", "ref": "S1"}],
             "assumptions": [{"text": "Which day names are accepted", "source": "unverified", "ref": "S1"}],
             "risks": [{"id": "RISK-1", "text": "Existing clients that omit the new field", "ref": OP}], "out_of_scope": []}
    coverage = {"test_cases": [
        {"id": "ORG-001", "label": "Required", "title": "Create an organization with working_days", "op": OP, "links": ["S1"],
         "request": "", "expected": "the documented success status; working_days echoed as sent", "evidence": "description"},
        {"id": "ORG-002", "label": "Required", "title": "Create an organization without working_days uses the default week", "op": OP,
         "links": ["S1"], "request": "", "expected": "the documented success status; the default week applies", "evidence": "description"},
        {"id": "REG-001", "label": "Regression", "title": "Pre-change request shape still creates an organization", "op": OP,
         "links": ["RISK-1"], "request": "", "expected": "the documented success status", "evidence": "openapi"},
        {"id": "ORG-003", "label": "Blocked", "title": "Persisted working_days read back", "op": OP, "links": ["S1"], "request": "",
         "expected": "per contract (verify)", "evidence": "description", "blocked_reason": "no documented read of a single organization"}],
        "uncovered": []}
    sections = {"execution_notes": ["Create disposable organizations only."], "evidence_notes": ["Save the body of ORG-001 and ORG-002."],
                "severity_guidance": [{"ref": "ORG-002", "severity": "P2", "rationale": "existing clients would change behaviour"}],
                "regression_analysis": [], "blockers": ["ORG-003 is BLOCKED without a documented read."], "report_notes": []}
    return [scope, coverage, sections]


class StagedFlowTests(StageBase):
    def setUp(self):
        super().setUp()
        pin = mock.patch.dict(os.environ, {"PRQA_PROMPT_GENERATION": "staged", "PRQA_PROVIDER_BUDGET_DIR": ""})
        pin.start()
        self.addCleanup(pin.stop)

    def through_prompt(self, host, outs=None):
        c = self.stage(host, "claim", pr=7)
        p = self.stage(host, "prepare", key=c["key"])
        steps = [p]
        with mock.patch.object(S.subprocess, "Popen", side_effect=AssertionError("no process may start during generation")):
            for out in outs or answers():
                self.assertEqual(steps[-1]["next"], "call", steps[-1])
                steps.append(self.stage(host, "promptstep", key=c["key"],
                                        generation={"generated_text": json.dumps(out), "finish_reason": "stop"},
                                        usage={"prompt_tokens": 900, "completion_tokens": 400, "total_tokens": 1300}))
        return c, steps

    def test_prepare_returns_the_first_stage_request_bounded_and_without_secrets(self):
        host = self.host()
        c = self.stage(host, "claim", pr=7)
        p = self.stage(host, "prepare", key=c["key"])
        self.assertEqual((p["ok"], p["next"], p["prompt_stage"], p["attempt"]), (True, "call", "scope", 1))
        self.assertLessEqual(p["estimate"]["total_tokens"], 5500)
        self.assertGreaterEqual(p["wait_s"], 1)
        self.assertEqual(p["llm_request"]["model"], S.prompt_model())
        self.assertNotIn("Authorization", json.dumps(p))
        self.assertTrue(p["prompt_job_id"].endswith(SHA[:12]))

    def test_generation_loop_creates_no_job_no_grant_and_ends_at_the_gate(self):
        host = self.host()
        c, steps = self.through_prompt(host)
        self.assertEqual([s.get("prompt_stage") for s in steps[:-1]], ["scope", "coverage", "sections"])
        self.assertEqual(steps[-1]["next"], "gate")
        self.assertEqual(steps[-1]["staged"]["action"], "done")
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.app.store.grant_audit_log(), [])
        g = self.stage(host, "gate", key=c["key"], generation=None)
        run_dir = R.RUNS_DIR / f"acme-pr-7-{SHA}"
        gm = json.loads((run_dir / "generation-metadata.json").read_text())
        self.assertEqual(gm["status"], "assembled")
        self.assertIn("worker_quality", gm["gate"])
        self.assertEqual(gm["gate"]["final_gate"]["threshold"], 85.0)
        meta = json.loads((run_dir / "metadata.json").read_text())
        self.assertIn("3 validated stages", meta["prompt"]["generation_path"])
        self.assertEqual(g["gate"]["staged"]["mode"], "3")
        accepted_llm = gm["gate"]["final_prompt_source"] == "llm"
        assembled = (run_dir / "qa-prompt.staged.md").read_text()
        final = (run_dir / "qa-prompt.md").read_text()
        self.assertEqual(accepted_llm, assembled.split("\n## 2.")[1][:200] in final)   # the staged text is used only if accepted
        self.assertEqual(self.jobs(), [])                              # the gate itself never starts a job

    def test_rejected_assembled_prompt_is_never_executed(self):
        host = self.host()
        c, _ = self.through_prompt(host)
        run_dir = R.RUNS_DIR / f"acme-pr-7-{SHA}"
        bad = "# Senior QA Engineer Prompt — broken\n\n## 1. Role and Objective\n\nmarker-of-the-rejected-candidate\n"
        with mock.patch.object(S.PS.Pipeline, "assembled_text", return_value=bad):
            g = self.stage(host, "gate", key=c["key"], generation=None)
        gm = json.loads((run_dir / "generation-metadata.json").read_text())
        self.assertFalse(gm["gate"]["worker_validation"]["valid"])
        self.assertTrue(gm["gate"]["worker_validation"]["errors"])     # the reasons are kept for debugging
        self.assertTrue(gm["result"].startswith("rejected"))
        self.assertEqual(g["gate"]["prompt_source"], "reference_fallback")
        self.assertNotIn("marker-of-the-rejected-candidate", (run_dir / "qa-prompt.md").read_text())
        self.assertEqual(self.jobs(), [])

    def test_failed_generation_falls_back_with_the_recorded_cause(self):
        host = self.host()
        c = self.stage(host, "claim", pr=7)
        self.stage(host, "prepare", key=c["key"])
        s1 = self.stage(host, "promptstep", key=c["key"], generation={"llm_error": "prompt LLM call failed", "http_status": "429"})
        self.assertEqual((s1["next"], s1["attempt"]), ("call", 2))
        self.assertGreaterEqual(s1["wait_s"], 60)
        s2 = self.stage(host, "promptstep", key=c["key"], generation={"llm_error": "prompt LLM call failed", "http_status": "429"})
        self.assertEqual((s2["next"], s2["staged"]["action"]), ("gate", "failed"))
        g = self.stage(host, "gate", key=c["key"], generation=None)
        self.assertEqual(g["gate"]["prompt_source"], "reference_fallback")
        self.assertEqual(g["gate"]["groq_failure"], "http_error")
        gm = json.loads((R.RUNS_DIR / f"acme-pr-7-{SHA}" / "generation-metadata.json").read_text())
        self.assertIn("rate_limited (HTTP 429)", gm["failure"])
        self.assertEqual(gm["stages"]["scope"]["attempts"], 2)

    def test_single_mode_keeps_the_previous_path(self):
        host = self.host()
        with mock.patch.dict(os.environ, {"PRQA_PROMPT_GENERATION": "single"}):
            c = self.stage(host, "claim", pr=7)
            p = self.stage(host, "prepare", key=c["key"])
            self.assertNotIn("next", p)
            gen = {"generated_text": None, "finish_reason": None, "llm_error": "x", "http_status": "413", "response_shape": None}
            s = self.stage(host, "promptstep", key=c["key"], generation=gen)
            self.assertEqual((s["next"], s["generation"]["http_status"]), ("gate", "413"))


if __name__ == "__main__":
    unittest.main()
