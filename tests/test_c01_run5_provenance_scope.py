"""Offline regressions for the three defect classes of the 2026-10-27 live C01 output (rotated key, run 5: gate INVALID,
rubric 80.0 < 85 → reference fallback), and for the gate tightening made from them:

  1. requirement headers that restate the PR description without `[STATED: PR description]` on the same line
  2. description-only claims (limit 500, `reason` required) tagged `[FACT: openapi]`; a `[FACT]` tag must name the
     source that shows each identifier — `[FACT: openapi]` for a diff-only item and `[FACT: diff]` for an
     OpenAPI-only item are now errors (were a warning / unchecked)
  3. an invented single-order read `GET /v1/orders/{orderId}`: now an error on ANY line — narrative, section 11, gap
     notes, headings and Blocked curls (were warnings) — not only in Required/Regression/Conditional scenarios

Every rejected form is paired with an accepted control, so the stricter gate does not reject legitimate plans. Whether
the model follows the revised prompt is NOT tested here: that needs a separately authorized live generation.

    PYTHONPATH=worker python3 -m unittest tests.test_c01_run5_provenance_scope -v
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "worker"), str(ROOT), str(ROOT / "tests")]

from prqa_worker import qa_rubric as rubric  # noqa: E402
from prqa_worker.prompt_builder import (  # noqa: E402
    Templates, build_llm_messages, claim_ledger, finalize_prompt, validate_generated_prompt,
)
from tests.test_groq_c01_regressions import AUTH, C01, CANCEL_CURL, H2, ITEM_CURL, LIST_CURL, _GroqC01  # noqa: E402

PIN = ROOT / "fixtures" / "recorded" / "groq-c01" / "groq-c01-gate-rejected-rubric80-20261027-rotated-key-live"
ARTIFACT = PIN / "case-C01-clear-behaviour.response.json"
H3, H11 = "## 3. Requirements and Evidence", "## 11. "


class _Base(_GroqC01):
    def under(self, heading, *lines):
        self.assertIn(heading, self.good)
        return self.good.replace(heading + "\n", heading + "\n\n" + "\n".join(lines) + "\n", 1)

    def blocked(self, request, expected="per contract (verify)", extra=""):
        return self.good.replace("## 6. ", "**BLK-901 (Blocked) — `cancellation_reason` visible on a cancelled order (BLOCKED)**\n"
                                           "- Requirement/Risk: S3 [STATED: PR description] — needs a single-order read\n"
                                           f"- Request: {request}\n- Expected: {expected}\n{extra}\n## 6. ", 1)


class RequirementHeaderTests(_Base):
    """Defect 1: a requirement header is a claim line; its provenance tag must be on that line."""

    def test_01_untagged_description_only_header_is_rejected(self):
        for header in ("- **S2** – `POST /v1/orders/{orderId}/cancel` must require a `reason` string of at most 500 chars.",
                       "- **S1** – `GET /v1/orders` should support an optional `status` query filter (one of `pending|paid|shipped|cancelled`).",
                       "- **S3** – Cancelled orders should expose `cancellation_reason`; other orders keep it null."):
            with self.subTest(header=header[:40]):
                self.rejected(self.under(H3, header), "untagged summary/requirement line")

    def test_02_same_header_with_same_line_description_provenance_is_accepted(self):
        for header in ("- **S2** – `POST /v1/orders/{orderId}/cancel` must require a `reason` string of at most 500 chars [STATED: PR description]",
                       "- **S1** – `GET /v1/orders` should support an optional `status` query filter (one of `pending|paid|shipped|cancelled`) [STATED: PR description]",
                       "- **S3** – Cancelled orders should expose `cancellation_reason`; other orders keep it null [STATED: PR description]"):
            with self.subTest(header=header[:40]):
                self.accepted(self.under(H3, header))

    def test_03_diff_supported_header_uses_the_diff_tag(self):
        self.accepted(self.under(H3, "- **S1: `GET /v1/orders` accepts a `status` query parameter** [FACT: diff]"))
        self.accepted(self.under(H3, "- **S2: cancel reads `payload.reason`** [FACT: diff]"))

    def test_04_openapi_supported_header_uses_the_openapi_tag(self):
        self.accepted(self.under(H3, "- **S2: `POST /v1/orders/{orderId}/cancel` documents 404, 409 and 422** [FACT: openapi]"))
        self.accepted(self.under(H3, "- **S1: `GET /v1/orders` is a changed operation** [FACT: openapi]"))

    def test_09_a_tag_on_another_line_does_not_qualify_the_header(self):
        for tag_line in ("  - Length limit 500 is description-only [STATED: PR description]",
                         "[STATED: PR description]"):
            with self.subTest(tag_line=tag_line):
                self.rejected(self.under(H3, "- **S2** – `reason` of at most 500 chars is required.", tag_line), "limit 500")

    def test_neutral_header_is_accepted_untagged(self):
        self.accepted(self.under(H3, "- **S2: Cancel with a reason**", "  - Description: `reason` is required, at most 500 characters [STATED: PR description]"))


class SourceAttributionTests(_Base):
    """Defect 2: a [FACT] tag must name the source that actually shows the claim."""

    def test_05_limit_500_tagged_openapi_is_rejected(self):
        self.rejected(self.summary("- `POST /v1/orders/{orderId}/cancel` accepts a `reason` of at most 500 chars [FACT: openapi]"),
                      "unsupported [FACT] claim", "limit 500")

    def test_06_limit_500_with_description_provenance_is_accepted(self):
        self.accepted(self.summary("- Description says `reason` is at most 500 characters [STATED: PR description]"))

    def test_07_reason_requiredness_tagged_openapi_is_rejected(self):
        self.rejected(self.summary("- `POST /v1/orders/{orderId}/cancel` requires a `reason` field [FACT: openapi]"),
                      "unsupported [FACT] claim", "validation claim")

    def test_07b_reason_requiredness_with_description_provenance_is_accepted(self):
        self.accepted(self.summary("- Description says `reason` is required [STATED: PR description]"))

    def test_08_field_existence_does_not_establish_requiredness(self):
        self.accepted(self.summary("- Cancel handler reads `payload.reason` [FACT: diff]"))
        for line in ("- Cancel handler reads `payload.reason`, so `reason` is required [FACT: diff]",
                     "- `reason` is required for cancellation"):
            with self.subTest(line=line):
                self.rejected(self.summary(line), "validation claim")

    def test_diff_only_identifier_tagged_openapi_is_misattributed(self):
        v = self.rejected(self.summary("- `GET /v1/orders` supports an optional `status` filter [FACT: openapi]"),
                          "misattributed evidence source", "'status' tagged [FACT: openapi] but not shown by OpenAPI")
        self.assertTrue(any("tag it [FACT: diff]" in e for e in v["errors"]), v["errors"])
        self.accepted(self.summary("- `GET /v1/orders` reads an optional `status` query parameter [FACT: diff]"))

    def test_openapi_only_identifier_tagged_diff_is_misattributed(self):
        ctx = copy.deepcopy(self.ctx)
        ctx["api_spec"]["known_operations"].append("GET /v1/orders/{orderId}/history")
        good = self.good.replace(H2 + "\n", H2 + "\n\n{}\n", 1)

        def check(line):
            return validate_generated_prompt(good.replace("{}", line, 1), pr_title=C01.title, context=ctx)
        bad = check("- OpenAPI lists an order `history` operation [FACT: diff]")
        self.assertFalse(bad["valid"])
        self.assertTrue(any("'history' tagged [FACT: diff] but not shown by the diff" in e for e in bad["errors"]), bad["errors"])
        ok = check("- OpenAPI lists an order `history` operation [FACT: openapi]")
        self.assertTrue(ok["valid"], ok["errors"])

    def test_a_valid_tag_does_not_legalize_an_unsupported_endpoint(self):
        for tag in ("[FACT: openapi]", "[FACT: diff]", "[STATED: PR description]"):
            with self.subTest(tag=tag):
                self.rejected(self.summary(f"- `GET /v1/orders/{{orderId}}` returns the order {tag}"), "GET /v1/orders/{orderId}")


class ItemReadEndpointTests(_Base):
    """Defect 3: an operation in no evidence and not in the description is rejected wherever it is named by path."""

    def test_10_item_read_is_rejected_in_every_location(self):
        locations = {
            "heading": self.under(H3, "### GET /v1/orders/{orderId}"),
            "requirement text": self.under(H3, "- S3 needs `GET /v1/orders/{orderId}` to read the reason (verify)"),
            "section 11 narrative": self.under(H11 + "Blockers, Assumptions and Ambiguities",
                                               "- No `GET /v1/orders/{orderId}` endpoint exists; S3 stays BLOCKED."),
            "Blocked curl": self.blocked(f"`{ITEM_CURL}`"),
            "Blocked expected": self.blocked("`" + LIST_CURL + "` — not run (BLOCKED)",
                                             "BLOCKED – no endpoint `GET /v1/orders/{orderId}` is documented"),
            "Required curl": self.scenario("ORD-090 (Required) — Fetch cancelled order (verify)", "S3 (verify).", ITEM_CURL, "HTTP 200."),
            "Conditional text": self.scenario("ORD-091 (Conditional) — Read back (verify)", "S3 (verify).", LIST_CURL,
                                              "GET /v1/orders/{orderId} shows the reason (verify)."),
        }
        for where, text in locations.items():
            with self.subTest(where=where):
                v = self.validate(text)
                self.assertFalse(v["valid"], f"{where}: accepted")
                # rejected through the rule of that location (scope, [STATED] or curl check), always naming the route
                self.assertTrue(any("GET /v1/orders/{orderId}" in e or "GET /v1/orders/$ORDER_ID" in e for e in v["errors"]), v["errors"])

    def test_11_verify_or_gap_note_cannot_legalize_the_item_read(self):
        for line in ("- Read `GET /v1/orders/{orderId}` to confirm the reason (verify)",
                     "- `GET /v1/orders/{orderId}` is not documented; persistence stays unverified (verify)",
                     "- Conditional: if `GET /v1/orders/{orderId}` exists, read the reason (verify)"):
            with self.subTest(line=line):
                self.rejected(self.under(H11 + "Blockers, Assumptions and Ambiguities", line), "unsupported endpoint")

    def test_gap_described_in_words_is_accepted(self):
        self.accepted(self.under(H11 + "Blockers, Assumptions and Ambiguities",
                                 "- No single-order read operation is in the PR evidence, so S3 stays BLOCKED (verify)."))
        self.accepted(self.blocked("`" + LIST_CURL + "` — not run: no single-order read operation is in the PR evidence (BLOCKED)"))

    def test_12_supported_collection_and_cancel_operations_stay_accepted(self):
        self.accepted(self.scenario("ORD-095 (Required) — List orders (verify)", "S1 [STATED: PR description]",
                                    LIST_CURL, "per contract (verify)"))
        self.accepted(self.scenario("CAN-095 (Required) — Cancel with a reason (verify)", "S2 [STATED: PR description]",
                                    CANCEL_CURL, "per contract (verify)"))
        self.accepted(self.under(H11 + "Blockers, Assumptions and Ambiguities",
                                 "- `GET /v1/orders` and `POST /v1/orders/{orderId}/cancel` are the only operations [FACT: openapi]"))
        self.accepted(self.blocked("`" + CANCEL_CURL + "` — not run: no already-cancelled fixture (BLOCKED)"))

    def test_description_only_route_keeps_the_description_policy(self):
        ctx = copy.deepcopy(self.ctx)
        ctx["description"]["text"] += "\n- Also adds `GET /v1/orders/{orderId}/audit`."
        text = self.blocked(f'`curl -sS {AUTH} "$BASE_URL/v1/orders/$ORDER_ID/audit" -o audit.json`')
        v = validate_generated_prompt(text, pr_title=C01.title, context=ctx)
        self.assertFalse([e for e in v["errors"] if "audit" in e], v["errors"])
        self.assertTrue([w for w in v["warnings"] if "named only by the PR description" in w], v["warnings"])


class PolicyIntactTests(_Base):
    def test_13_reference_fallback_passes_and_blocked_scope_hardfails_hold(self):
        self.accepted(self.good)
        v = self.validate(self.good.replace("BLOCKED", "Pending").replace("(Blocked)", "(Conditional)"))
        self.assertFalse(v["valid"])
        self.assertTrue(any("BLOCKED" in e for e in v["errors"]), v["errors"])
        bad = self.scenario("ORD-090 (Required) — Fetch cancelled order", "S3.", ITEM_CURL, "HTTP 200.")
        gate = self.validate(bad)
        res = rubric.score_prompt(bad, pr_title=C01.title, context=self.ctx, gate=gate)
        self.assertFalse(rubric.accept_result(res, gate)["accepted"])
        self.assertTrue(any(h.startswith("HF-") for h in res.hard_fails), res.hard_fails)

    def test_15_threshold_and_rubric_are_unchanged(self):
        self.assertEqual(rubric.THRESHOLD, 85.0)
        self.assertEqual(len(rubric.DIMENSIONS), 10)
        src = (ROOT / "worker" / "prqa_worker" / "qa_rubric.py").read_text(encoding="utf-8")
        self.assertIn("quality_score = sum(scores) / (5 * 10) * 100", src)

    def test_ledger_attributes_each_description_only_item_to_its_requirement(self):
        ledger = claim_ledger(self.ctx)
        line = next(l for l in ledger.splitlines() if l.startswith("Description-only"))
        self.assertIn("never `[FACT: …]`", line)
        self.assertIn("S1: identifiers `pending`, `paid`, `shipped`, validation claim 'one of' for status", line)
        self.assertIn("S2: limit 500, validation claim 'require' for reason", line)
        self.assertIn("S3: identifier `cancellation_reason`", line)
        self.assertNotIn("other:", line)
        self.assertIn("A header restating one is a `[STATED: PR description]` line.", ledger)
        user = build_llm_messages(Templates.load(ROOT / "prompts"), repository="synthetic-org/bench-repo",
                                  pr_number=C01.pr_number, test_level="standard", synthetic=True, context=self.ctx)[1]["content"]
        self.assertIn("(complete: name no other method/path anywhere, Blocked and section 11 included)", user)


class HistoricalRun5Tests(_Base):
    """Labelled offline RE-SCORE of the saved run-5 output with today's gate and the unchanged rubric."""

    def test_pinned_copy_is_intact(self):
        want = (PIN / "SHA256SUMS").read_text(encoding="utf-8").split()[0]
        self.assertEqual(hashlib.sha256(ARTIFACT.read_bytes()).hexdigest(), want)

    def test_14_run5_output_stays_rejected_and_the_worker_selects_the_fallback(self):
        a = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        gate = self.validate(a["content"])
        res = rubric.score_prompt(a["content"], pr_title=C01.title, context=self.ctx, gate=gate)
        decision = rubric.accept_result(res, gate)
        self.assertEqual((gate["valid"], res.quality_score, decision["accepted"], decision["failure"]),
                         (False, 80.0, False, "gate_rejected"))
        self.assertIn("HF-FABRICATED-FACT", decision["hard_fails"])
        errors = " | ".join(gate["errors"])
        for needle in ("unsupported [FACT] claim (line 13)",                                   # defect 2
                       "untagged summary/requirement line (line 20)", "(line 25)", "(line 30)",   # defect 1
                       "endpoint GET /v1/orders/{orderId}",                                    # defect 3, [STATED] line 98
                       "misattributed evidence source (line 12)", "misattributed evidence source (line 13)",
                       "line 96 (Blocked): curl request GET /v1/orders/$ORDER_ID", "unsupported endpoint (line 126, narrative)"):
            self.assertIn(needle, errors)
        self.assertEqual(len(gate["errors"]), 10)            # 6 when recorded; +4 from the tightened rules above
        out = finalize_prompt(Templates.load(ROOT / "prompts"), generated_text=a["content"], repository="git@synthetic-org/bench-repo",
                              pr_number=C01.pr_number, test_level="standard", context=self.ctx, synthetic=True,
                              finish_reason=a["finish_reason"], http_status=200)
        self.assertEqual(out["prompt"]["source"], "reference_fallback")
        self.assertEqual(out["generation"]["outcome"], "gate_rejected")
        self.assertFalse(out["quality"]["accepted"])


if __name__ == "__main__":
    unittest.main()
