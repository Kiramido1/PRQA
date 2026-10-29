"""Offline regressions from the 2026-10-27 live C01 output (gate INVALID, rubric 78.0 < 85 → reference fallback).

The output is re-scored here from a read-only pinned copy (a labelled historical RE-SCORE; nothing is regenerated and
the artifact is never rewritten). Its gate errors fall into four defect classes, each reproduced below on the
deterministic reference prompt and checked against the UNCHANGED gate, together with the corrected form the revised
prompt asks for (so every new instruction is satisfiable):

  1. split provenance — a requirement header copies the description sentence untagged and the tags sit on sub-bullets;
     "Unverified / to confirm" items repeat description-only details untagged
  2. `[STATED: PR description]` on a line whose claim comes from OpenAPI (401 on AUTH scenarios)
  3. a `[STATED]` scenario block naming things the description does not contain (a backticked output file)
  4. a Blocked scenario requesting / naming an operation outside the evidence (`GET /v1/orders/{orderId}`)

Whether the model follows the revised instructions is NOT tested: that needs a separately authorized live generation.

    PYTHONPATH=worker python3 -m unittest tests.test_c01_live_20261027_regressions -v
"""
from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "worker"), str(ROOT), str(ROOT / "tests")]

from prqa_worker import qa_rubric as rubric  # noqa: E402
from tests.test_groq_c01_regressions import C01, _GroqC01  # noqa: E402

PIN = ROOT / "fixtures" / "recorded" / "groq-c01" / "groq-c01-gate-rejected-rubric78-20261027-live"
ARTIFACT = PIN / "case-C01-clear-behaviour.response.json"
SYSTEM = (ROOT / "prompts" / "llm" / "system.v2.md").read_text(encoding="utf-8")
AUTH = '-H "Authorization: Bearer $TOKEN"'
H3, H4, H11 = "## 3. ", "## 4. ", "## 11. "


class _Base(_GroqC01):
    def before(self, heading, text, base=None):
        base = base or self.good
        self.assertIn(heading, base)
        return base.replace(heading, text + "\n\n" + heading, 1)

    def scenario(self, sid, label, title, risk, request, expected):
        return self.before("## 6. ", f"**{sid} ({label}) — {title}**\n- Requirement/Risk: {risk}\n- Request: {request}\n- Expected: {expected}")


class HistoricalRescoreTests(_Base):
    """Labelled re-score of the saved live output with today's (unchanged) gate and rubric."""

    def test_pinned_copy_is_intact(self):
        want = (PIN / "SHA256SUMS").read_text(encoding="utf-8").split()[0]
        self.assertEqual(hashlib.sha256(ARTIFACT.read_bytes()).hexdigest(), want)

    def test_output_stays_rejected_with_the_recorded_score(self):
        content = json.loads(ARTIFACT.read_text(encoding="utf-8"))["content"]
        gate = self.validate(content)
        res = rubric.score_prompt(content, pr_title=C01.title, context=self.ctx, gate=gate)
        decision = rubric.accept_result(res, gate)
        self.assertEqual((gate["valid"], res.quality_score, decision["accepted"], decision["failure"]), (False, 78.0, False, "gate_rejected"))
        self.assertIn("HF-UNUSABLE", decision["hard_fails"])
        self.assertEqual({d: res.score(d) for d in rubric.DIMENSIONS},
                         {"scope_accuracy": 1, "business_rules": 5, "test_coverage": 5, "executability": 5, "expected_results": 5,
                          "context_grounding": 0, "ambiguity_handling": 5, "pr_specificity": 5, "output_structure": 4,
                          "safety_isolation": 4})
        errors = " | ".join(gate["errors"])
        # 10 errors when recorded; 12 since the 2026-10-27 scope tightening (the invented single-order read in the
        # Blocked curl, line 96, and in the section-11 narrative, line 136, were warnings)
        self.assertEqual(len(gate["errors"]), 12)
        for needle in ("untagged summary/requirement line (line 18)", "untagged summary/requirement line (line 23)",
                       "untagged summary/requirement line (line 29)", "untagged summary/requirement line (line 34)",
                       "untagged summary/requirement line (line 35)", "'cancel_ok.json'", "status 401 meaning 'auth missing'",
                       "endpoint GET /v1/orders/{orderId}", "line 96 (Blocked): curl request GET /v1/orders/$ORDER_ID",
                       "unsupported endpoint (line 136, narrative)"):
            self.assertIn(needle, errors)


class SplitProvenanceTests(_Base):
    def test_copied_description_header_with_tagged_sub_bullets_is_rejected(self):
        text = self.before(H4, "- **S9** – `POST /v1/orders/{orderId}/cancel` must require a `reason` string of at most 500 chars.\n"
                               "  - Length limit 500 mentioned only in description [STATED: PR description]")
        self.rejected(text, "limit 500 not in the PR evidence")

    def test_neutral_header_with_same_line_tags_is_accepted(self):
        self.accepted(self.before(H4, "- **S9: Cancel with a reason on `POST /v1/orders/{orderId}/cancel`**\n"
                                      "  - Description: `reason` is required, at most 500 characters [STATED: PR description]"))

    def test_untagged_to_confirm_item_is_rejected_and_tagged_item_accepted(self):
        self.rejected(self.before(H4, "- Enforcement of the 500-char limit for `reason`."), "limit 500")
        self.rejected(self.before(H4, "- Presence of `cancellation_reason` for cancelled orders."), "`cancellation_reason`")
        self.accepted(self.before(H4, "- Enforcement of the 500-character limit on `reason` [STATED: PR description] (verify)\n"
                                      "- Presence of `cancellation_reason` for cancelled orders [STATED: PR description] (verify)"))


class TagBySourceTests(_Base):
    def test_openapi_status_tagged_stated_is_rejected(self):
        text = self.scenario("AUTH-901", "Required", "`GET /v1/orders` without credentials (verify)",
                             "S1 [STATED: PR description] — endpoint returns 401 when auth missing",
                             """`curl -sS "$BASE_URL/v1/orders" -o auth_none.json -w '%{http_code}'`""", "HTTP 401 (per contract)")
        self.rejected(text, "[STATED] claim not found in the PR description")

    def test_openapi_status_cited_as_openapi_is_accepted(self):
        self.accepted(self.scenario("AUTH-901", "Required", "`GET /v1/orders` without credentials (verify)",
                                    "`GET /v1/orders` documents 401 [FACT: openapi]",
                                    """`curl -sS "$BASE_URL/v1/orders" -o auth_none.json -w '%{http_code}'`""",
                                    "per contract (verify); record the status and body"))


class StatedBlockNamesTests(_Base):
    REQUEST = (f"""`ORDER_ID=$(curl -sS {AUTH} "$BASE_URL/v1/orders" | jq -r 'first(..|.id? // empty)'); curl -sS -X POST {AUTH} """
               """-H "Content-Type: application/json" -d '{"reason":"customer request"}' "$BASE_URL/v1/orders/$ORDER_ID/cancel" -o cancel_ok.json`""")

    def test_backticked_output_file_in_a_stated_block_is_rejected(self):
        text = self.scenario("CAN-901", "Required", "Cancel with a reason (verify)",
                             "S2 [STATED: PR description] — a valid `reason` cancels the order", self.REQUEST,
                             "per contract (verify); `cancellation_reason` present in `cancel_ok.json`")
        self.rejected(text, "'cancel_ok.json'")

    def test_output_file_only_inside_the_curl_is_accepted(self):
        self.accepted(self.scenario("CAN-901", "Required", "Cancel with a reason (verify)",
                                    "S2 [STATED: PR description] — a valid `reason` cancels the order", self.REQUEST,
                                    "per contract (verify); `cancellation_reason` present in the saved response"))


class BlockedOperationTests(_Base):
    def test_blocked_scenario_naming_an_unsupported_operation_is_rejected(self):
        text = self.scenario("BLK-901", "Blocked", "`cancellation_reason` is null for other orders",
                             "S3 [STATED: PR description] — needs a single-order read",
                             "`GET /v1/orders` — not run (BLOCKED)",
                             "per contract (verify); BLOCKED – no endpoint `GET /v1/orders/{orderId}` is documented")
        self.rejected(text, "endpoint GET /v1/orders/{orderId}")

    def test_blocked_scenario_naming_the_gap_in_words_is_accepted(self):
        self.accepted(self.scenario("BLK-901", "Blocked", "`cancellation_reason` is null for other orders",
                                    "S3 [STATED: PR description] — needs a single-order read",
                                    "`POST /v1/orders/{orderId}/cancel` — not run: no single-order read operation is in the PR evidence (BLOCKED)",
                                    "per contract (verify)"))


class PromptInstructionTests(unittest.TestCase):
    def test_each_defect_class_has_an_instruction(self):
        for needle in ("never the description's sentence",                        # 1 header
                       'then "Unverified / to confirm during discovery" (tag these items too)',   # 1 to-confirm items
                       "is `[FACT: openapi]`, not `[FACT: diff]` or `[STATED]`",  # 2 tag by source
                       "so name output files only inside its curl",               # 3 stated block
                       "Name it in words, never by path."):                       # 4 missing operation
            self.assertIn(needle, SYSTEM)

    def test_pre_existing_rules_are_kept(self):
        for needle in ("**Requirement headers, summaries and other narrative lines follow the same rules.**",
                       "A tag on another bullet never covers a line.", "`(verify)` is not permission to invent an endpoint",
                       "Tag factual assertions only"):
            self.assertIn(needle, SYSTEM)


if __name__ == "__main__":
    unittest.main()
