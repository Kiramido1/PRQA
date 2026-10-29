"""Offline prompt-quality optimization from the saved C01 evidence (2026-10-26).

Every saved C01 output (4 Groq, 14 earlier OpenRouter dots-3) lost points in the same places: no PR number in the
title and requirement IDs `R1…` where the gate and the reference use `S1…` (pr_specificity 3 in all 18); no
Regression or AUTH scenario and no boundary case (test_coverage); a Blocked scenario without Request/Expected lines
(executability, expected_results); description-only details tagged only `(verify)`, which the gate accepts with an
unverified-claim warning (context_grounding); the rubric's safety anchors missing. The prompt now asks for each,
and these tests check that every new instruction is satisfiable together with the unchanged gate.

The historical outputs are re-scored here (labelled re-scores, read only). Their scores and rejections must not
move: the rubric, the threshold and the gate are unchanged. Whether Groq follows the new instructions is untested
until a separately authorized generation; nothing here predicts a live score.
"""
from __future__ import annotations

import copy
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from prqa_worker import prompt_builder as pb  # noqa: E402
from prqa_worker import qa_rubric as rubric  # noqa: E402
from tests.test_groq_c01_regressions import C01, _GroqC01  # noqa: E402

PINS = ROOT / "fixtures" / "recorded" / "groq-c01"
# read-only pinned copy of the 2026-10-27 live C01 output (identical bytes to runs/groq-live at the time, SHA256SUMS)
LIVE_20261027 = json.loads((PINS / "groq-c01-gate-rejected-rubric78-20261027-live" / "case-C01-clear-behaviour.response.json")
                           .read_text(encoding="utf-8"))["content"]
# read-only pinned copy of the second 2026-10-27 live C01 output (rotated key, revised prompt; gate INVALID, 80.0)
LIVE_20261027_B = json.loads((PINS / "groq-c01-gate-rejected-rubric80-20261027-rotated-key-live"
                              / "case-C01-clear-behaviour.response.json").read_text(encoding="utf-8"))["content"]
POST_OPTIMIZATION_OUTPUTS = (LIVE_20261027, LIVE_20261027_B)
SYSTEM = (ROOT / "prompts" / "llm" / "system.v2.md").read_text(encoding="utf-8")
AUTH = '-H "Authorization: Bearer $TOKEN"'
# (pin directory, original live score, offline re-score with today's gate and rubric, decision today)
GROQ_C01 = (
    ("groq-c01-gate-rejected-20261026", 72.0, 72.0, "gate_rejected"),
    ("groq-c01-gate-rejected-20261026-run2", 70.0, 70.0, "gate_rejected"),
    ("groq-c01-gate-rejected-20261026-run3", 70.0, 72.0, "gate_rejected"),
    ("groq-c01-gate-valid-rubric78-20261026-run4", 78.0, 78.0, "rubric_threshold"),
)
BOUNDARY_REQUEST = (f"""for n in 500 501; do ORDER_ID=$(curl -sS {AUTH} "$BASE_URL/v1/orders" | jq -r 'first(..|.id? // empty)'); """
                    f"""R=$(python3 -c "print('x'*$n)"); curl -sS -X POST {AUTH} -H "Content-Type: application/json" """
                    f"""-d "{{\\"reason\\":\\"$R\\"}}" "$BASE_URL/v1/orders/$ORDER_ID/cancel" -o "cancel_$n.json" -w '%{{http_code}}'; done""")


def block(sid, label, title, risk, request, expected):
    return f"**{sid} ({label}) — {title}**\n- Requirement/Risk: {risk}\n- Request: {request}\n- Expected: {expected}\n"


# one scenario per new instruction, written the way the system prompt now asks for it
CONFORMING = {
    "auth": block("AUTH-101", "Required", "`GET /v1/orders` without credentials (verify)",
                  "`GET /v1/orders` documents 401 [FACT: openapi]",
                  """`curl -sS "$BASE_URL/v1/orders" -o auth_none.json -w '%{http_code}'`""",
                  "per contract (verify); record the status and body"),
    "regression": block("REG-101", "Regression", "`GET /v1/orders` without the new filter behaves as before (verify)",
                        "S1 — listing without `status` is unchanged",
                        f"""`curl -sS {AUTH} "$BASE_URL/v1/orders" -o reg_list.json -w '%{{http_code}}'`""",
                        "per contract (verify); 200 and the same orders as the unfiltered list before the change"),
    "boundary": block("CAN-110", "Required", "`reason` at the stated limit and one character over (verify)",
                      "S2 [STATED: PR description] — `reason` at most 500 characters", f"`{BOUNDARY_REQUEST}`",
                      "per contract (verify); record both statuses (at the limit accepted, one over rejected)"),
    "blocked": block("CAN-111", "Blocked", "Cancel an already-cancelled order",
                     "S2 [STATED: PR description] — 409 when the order is already cancelled",
                     "`POST /v1/orders/{orderId}/cancel` — not run: no already-cancelled fixture (BLOCKED)",
                     "per contract (verify)"),
}


class _Base(_GroqC01):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.t = pb.Templates.load(ROOT / "prompts")
        cls.base = cls().validate(cls.good)

    def with_block(self, text):
        return self.good.replace("## 6. ", text + "\n## 6. ", 1)

    def in_section_11(self, lines):
        i = self.good.index("## 11. ")
        j = self.good.index("\n", i)
        return self.good[:j + 1] + lines + self.good[j + 1:]

    def new_warnings(self, v):
        return [w for w in v["warnings"] if w not in self.base["warnings"]]


class InstructionGateConsistencyTests(_Base):
    """Following an instruction must never get a plan rejected or flagged by the (unchanged) gate."""

    def test_reference_is_the_valid_starting_point(self):
        self.assertTrue(self.base["valid"])

    def test_each_new_scenario_kind_passes_the_gate_without_new_warnings(self):
        for name, text in CONFORMING.items():
            with self.subTest(name=name):
                v = self.validate(self.with_block(text))
                self.assertTrue(v["valid"], v["errors"])
                self.assertEqual(self.new_warnings(v), [])

    def test_all_new_scenario_kinds_together(self):
        v = self.validate(self.with_block("\n".join(CONFORMING.values())))
        self.assertTrue(v["valid"], v["errors"])
        self.assertEqual(self.new_warnings(v), [])

    def test_writing_the_over_limit_number_under_a_stated_tag_is_rejected(self):
        # why the boundary rule says "never write the over-limit number": a [STATED] block that names 501 claims a
        # limit the description does not state
        text = block("CAN-112", "Required", "`reason` of 500 and 501 characters (verify)",
                     "S2 [STATED: PR description] — `reason` at most 500 characters", f"`{BOUNDARY_REQUEST}`",
                     "per contract (verify); 500 characters accepted, 501 rejected")
        v = self.validate(self.with_block(text))
        self.assertFalse(v["valid"])
        self.assertIn("limit 501", " | ".join(v["errors"]))

    def test_section_11_items_tagged_on_their_line_add_no_warning(self):
        tagged = ("- `cancellation_reason` on cancelled orders is description-only [STATED: PR description]\n"
                  "- limit 500 for `reason` is description-only [STATED: PR description]\n")
        verify_only = "- `cancellation_reason` on cancelled orders, to confirm (verify)\n- limit 500 for `reason`, to confirm (verify)\n"
        ok = self.validate(self.in_section_11(tagged))
        self.assertTrue(ok["valid"])
        self.assertEqual(self.new_warnings(ok), [])
        # the old "(verify) only" form still passes, but each line costs context grounding a warning
        old = self.validate(self.in_section_11(verify_only))
        self.assertTrue(old["valid"])
        self.assertEqual(len(self.new_warnings(old)), 2)

    def test_the_prompts_own_scenario_example_passes(self):
        m = re.search(r"  ```\n(  \*\*ORD-001.*?)  ```", SYSTEM, re.S)
        example = "\n".join(l[2:] for l in m.group(1).splitlines()) + "\n"
        v = self.validate(self.with_block(example))
        self.assertTrue(v["valid"], v["errors"])
        self.assertEqual(self.new_warnings(v), [])


class PromptContentTests(unittest.TestCase):
    def test_title_carries_the_pr_number_and_repository(self):
        self.assertIn("API Testing for PR: <PR title> (PR #<pr_number>, <repository>)", SYSTEM)

    def test_requirement_ids_are_the_gates_ids(self):
        self.assertNotIn("`R1…`", SYSTEM)
        self.assertIn("use the `S1…` IDs `CLAIM_LEDGER` gives", SYSTEM)
        self.assertIn("Requirement/Risk: S1 [STATED: PR description]", SYSTEM)

    def test_coverage_mix_is_bounded_by_the_evidence(self):
        for needle in ("**Coverage mix, only as the evidence supports:**", "one **Regression** scenario",
                       "for an operation documenting 401, `AUTH-001 (Required) — <operation> without credentials (verify)`",
                       "never write the over-limit number", "No scenario for behaviour the evidence does not mention"):
            self.assertIn(needle, SYSTEM)

    def test_scenarios_state_observable_results_and_blocked_keeps_four_lines(self):
        self.assertIn("`Expected:` names what to observe (status, field/value checked) or `per contract (verify)`", SYSTEM)
        self.assertIn("a Blocked scenario keeps all four lines", SYSTEM)

    def test_same_line_stated_tag_in_every_section(self):
        self.assertIn("every line (any section, section 11 included) naming a description-only item it lists carries "
                      "`[STATED: PR description]`", SYSTEM)

    def test_execution_rules_keep_their_anchors(self):
        for needle in ("never invent results", "never fabricate evidence", "never print secrets"):
            self.assertIn(needle, SYSTEM)


class LedgerAndContextTests(_Base):
    def messages(self, context=None, level="standard"):
        return pb.build_llm_messages(self.t, repository="synthetic-org/bench-repo", pr_number=C01.pr_number,
                                     test_level=level, synthetic=True, context=context or self.ctx)

    def test_ledger_gives_the_gates_stated_requirement_ids(self):
        ledger = pb.claim_ledger(self.ctx)
        desc = self.ctx["description"]["text"]
        ids = [r["id"] for r in pb.extract_stated_requirements(desc, C01.title)]
        self.assertEqual(ids, ["S1", "S2", "S3"])
        line = next(l for l in ledger.splitlines() if l.startswith("Stated requirements (use these IDs): "))
        self.assertEqual(re.findall(r"\b(S\d+) = ", line), ids)

    def test_ledger_without_stated_requirements_has_no_id_line(self):
        ctx = copy.deepcopy(self.ctx)
        ctx["description"]["text"] = ""
        self.assertNotIn("Stated requirements", pb.claim_ledger(ctx))

    def test_model_copy_drops_metadata_and_keeps_every_evidence_field(self):
        full = copy.deepcopy(self.ctx)
        view = pb.llm_context_view(self.ctx)
        self.assertEqual(self.ctx, full)                       # the gate's context is not mutated
        for gone in ("context_sha256", "package_version", "changed_files_truncated"):
            self.assertNotIn(gone, view)
        self.assertEqual(set(view["pr"]), {"number", "title"})
        self.assertNotIn("known_operations", view["api_spec"])   # listed with its source under SUPPORTED_OPERATIONS
        for kept in ("description", "diff", "changed_files", "trust", "repository", "synthetic"):
            self.assertEqual(view[kept], self.ctx[kept])
        for k in ("changed_operations", "documented_responses", "documented_status_codes"):
            self.assertEqual(view["api_spec"][k], self.ctx["api_spec"][k])
        user = self.messages()[1]["content"]
        self.assertNotIn(self.ctx["context_sha256"], user)
        self.assertNotIn(self.ctx["pr"]["head_sha"], user)
        for op in self.ctx["api_spec"]["known_operations"]:   # still visible to the model, once
            self.assertIn(op, user.split("<<<SUPPORTED_OPERATIONS>>>", 1)[1])

    def test_a_truncation_flag_that_is_set_is_kept(self):
        ctx = copy.deepcopy(self.ctx)
        ctx["changed_files_truncated"] = True
        self.assertIs(pb.llm_context_view(ctx)["changed_files_truncated"], True)

    def test_only_the_selected_test_level_is_explained(self):
        for level, scope in pb.TEST_LEVEL_SCOPE.items():
            with self.subTest(level=level):
                user = self.messages(level=level)[1]["content"]
                self.assertIn(f"- test_level: {level} ({scope}; the budget wins)", user)
                for other, text in pb.TEST_LEVEL_SCOPE.items():
                    if other != level:
                        self.assertNotIn(text, user)
        self.assertNotIn("{{", self.messages(level="unknown")[1]["content"])

    def test_gate_and_fallback_still_use_the_full_context(self):
        # the model's copy is smaller, but validation and the fallback see what they saw before
        self.assertEqual(self.validate(self.good)["valid"], True)
        ref = pb.reference_fallback_prompt(self.t, repository="synthetic-org/bench-repo", pr_number=C01.pr_number,
                                           test_level="standard", context=self.ctx)
        self.assertEqual(ref, self.good)


class HistoricalRescoreTests(_Base):
    """Offline RE-SCORES of saved live outputs (read only). They pin that nothing was loosened."""

    def load(self, pin):
        path = PINS / pin / "case-C01-clear-behaviour.response.json"
        raw = path.read_bytes()
        return json.loads(raw)["content"], raw, path

    def test_saved_groq_outputs_keep_their_scores_and_stay_rejected(self):
        for pin, live, rescore, decision in GROQ_C01:
            with self.subTest(pin=pin):
                content, raw, path = self.load(pin)
                gate = self.validate(content)
                res = rubric.score_prompt(content, pr_title=C01.title, context=self.ctx, gate=gate)
                self.assertEqual(res.quality_score, rescore)
                self.assertLessEqual(abs(res.quality_score - live), 2.0)
                self.assertEqual(rubric.accept_result(res, gate)["failure"], decision)
                self.assertEqual(path.read_bytes(), raw)

    def test_run4_dimensions_are_unchanged(self):
        content, _, _ = self.load("groq-c01-gate-valid-rubric78-20261026-run4")
        res = rubric.score_prompt(content, pr_title=C01.title, context=self.ctx, gate=self.validate(content))
        self.assertEqual({d: res.score(d) for d in rubric.DIMENSIONS},
                         {"scope_accuracy": 5, "business_rules": 5, "test_coverage": 3.0, "executability": 4,
                          "expected_results": 3, "context_grounding": 3, "ambiguity_handling": 5, "pr_specificity": 3,
                          "output_structure": 4, "safety_isolation": 4})

    def test_every_saved_c01_output_is_still_rejected(self):
        seen = set()
        for path in sorted(PINS.glob("**/case-C01*response.json")):
            content = json.loads(path.read_text(encoding="utf-8")).get("content") or ""
            if content in seen:
                continue
            seen.add(content)
            gate = self.validate(content)
            res = rubric.score_prompt(content, pr_title=C01.title, context=self.ctx, gate=gate)
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertFalse(rubric.accept_result(res, gate)["accepted"])
                # the shared gap of the 18 outputs before the 2026-10-26 optimization: no "PR #<n>", no S-IDs. The
                # 2026-10-27 live outputs (generated with it) closed that gap and failed on provenance instead
                # (tests/test_c01_live_20261027_regressions.py pins the first one)
                self.assertEqual(res.score("pr_specificity"), 5 if content in POST_OPTIMIZATION_OUTPUTS else 3)
        self.assertGreaterEqual(len(seen), 19)

    def test_candidate_output_structure_ceiling_is_documented(self):
        # the result contract is appended by the worker after scoring, so a raw candidate cannot carry it: 4/5 at most
        # (the reference prompt has the same ceiling before finalize). Recorded, not changed.
        res = rubric.score_prompt(self.good, pr_title=C01.title, context=self.ctx, gate=self.base)
        self.assertEqual(res.score("output_structure"), 4)
        self.assertNotIn(rubric.CONTRACT_MARKER, self.good)


if __name__ == "__main__":
    unittest.main()
