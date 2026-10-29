"""Offline regressions from the Groq gpt-oss-120b C01 candidate run (2026-10-26, gate-rejected 72.0, HF-SCOPE-VIOLATION).

That output invented a single-item read route (`GET /v1/orders/{id}`), asserted 401/404 triggers the evidence does not
establish, and restated description-only details (`cancellation_reason`, a 500-character limit, requiredness and allowed
values) as untagged verified facts. Each pattern is pinned here against the synthetic C01 fixture and the shared gate;
the evidence-backed C01 content and the description-only `[STATED]` / `(verify)` treatment must stay accepted.
No network, no model call; the recorded output (if present) is only read, never printed.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from prqa_worker.prompt_builder import (  # noqa: E402
    Evidence, Templates, build_llm_messages, claim_ledger, operation_mismatch, reference_fallback_prompt,
    validate_generated_prompt,
)
from tests.qa_benchmark import CASES, context_for  # noqa: E402
import qa_rubric  # noqa: E402

C01 = next(c for c in CASES if c.cid == "C01-clear-behaviour")
# the gate-rejected output of 2026-10-26, pinned (runs/groq-live/ is archived and replaced by later live runs)
GROQ_C01 = ROOT / "fixtures" / "recorded" / "groq-c01" / "groq-c01-gate-rejected-20261026" / "case-C01-clear-behaviour.response.json"
# second live run (2026-10-26, score 70.0): description-only claims tagged on a separate bullet, 404/401 triggers asserted
GROQ_C01_RUN2 = ROOT / "fixtures" / "recorded" / "groq-c01" / "groq-c01-gate-rejected-20261026-run2" / "case-C01-clear-behaviour.response.json"
AUTH = '-H "Authorization: Bearer $TOKEN"'
ITEM_CURL = f'curl -sS {AUTH} "$BASE_URL/v1/orders/$ORDER_ID" -o item.json'
CANCEL_CURL = (f"""curl -sS {AUTH} -H "Content-Type: application/json" -d '{{"reason":"Test"}}' """
               '"$BASE_URL/v1/orders/$ORDER_ID/cancel" -o c.json')
LIST_CURL = f'curl -sS {AUTH} "$BASE_URL/v1/orders" -o orders.json'
H2 = "## 2. Verified PR Summary and Scope"


class _GroqC01(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = context_for(C01)
        cls.ev = Evidence.from_context(cls.ctx)
        cls.good = reference_fallback_prompt(Templates.load(ROOT / "prompts"), repository="synthetic-org/bench-repo",
                                             pr_number=C01.pr_number, test_level="standard", context=cls.ctx)

    def validate(self, text):
        return validate_generated_prompt(text, pr_title=C01.title, context=self.ctx)

    def scenario(self, title, risk, request, expected):
        return self.good.replace("## 6. ", f"**{title}**\n- Requirement/Risk: {risk}\n- Request: `{request}`\n"
                                          f"- Expected: {expected}\n\n## 6. ", 1)

    def summary(self, *lines):
        self.assertIn(H2 + "\n", self.good)
        return self.good.replace(H2 + "\n", H2 + "\n\n" + "\n".join(lines) + "\n", 1)

    def rejected(self, text, *needles):
        v = self.validate(text)
        self.assertFalse(v["valid"], "gate accepted a defective prompt")
        joined = " | ".join(v["errors"])
        for n in needles:
            self.assertIn(n, joined)
        return v

    def accepted(self, text):
        v = self.validate(text)
        self.assertTrue(v["valid"], v["errors"])
        return v


class UnsupportedEndpointTests(_GroqC01):
    def test_1_item_read_route_is_outside_the_supported_scope(self):
        for label, title in (("Required", "ORD-090 (Required) — Fetch cancelled order"),
                             ("Required+verify", "ORD-090 (Required) — Fetch cancelled order (verify)"),
                             ("Regression", "ORD-090 (Regression) — Fetch active order")):
            with self.subTest(label=label):
                self.rejected(self.scenario(title, "R3.", ITEM_CURL, "HTTP 200."),
                              "GET /v1/orders/$ORDER_ID matches no operation")

    def test_2_collection_route_does_not_imply_an_item_route(self):
        self.assertIsNone(operation_mismatch("GET", "/v1/orders", self.ev.paths, self.ev.documented_ops | self.ev.operations,
                                             True))
        self.assertIsNone(operation_mismatch("POST", "/v1/orders/123/cancel", self.ev.paths,
                                             self.ev.documented_ops | self.ev.operations, True))
        self.assertIsNotNone(operation_mismatch("GET", "/v1/orders/123", self.ev.paths,
                                                self.ev.documented_ops | self.ev.operations, True))
        # neither (verify) nor a Conditional label makes the item route exist
        self.rejected(self.scenario("ORD-091 (Conditional) — Cancelled order exposes the reason (verify)",
                                    "R3 — hypothesis (verify).", ITEM_CURL, "per contract (verify)."),
                      "Conditional scenario", "GET /v1/orders/$ORDER_ID")
        self.rejected(self.scenario("ORD-092 (Conditional) — Read back the order (verify)", "R3 (verify).",
                                    "see section 4", "GET /v1/orders/{orderId} shows the reason (verify)."),
                      "Conditional scenario", "GET /v1/orders/{orderId}")


class StatusTriggerTests(_GroqC01):
    def test_3_missing_auth_401_is_not_established(self):
        # GET /v1/orders documents 401, but nothing establishes that *missing auth* triggers it
        list_noauth = 'curl -sS "$BASE_URL/v1/orders" -o noauth.json'
        self.rejected(self.scenario("ORD-093 (Regression) — GET orders without auth returns 401", "Auth regression.",
                                    list_noauth, "HTTP 401."), "status 401 trigger")
        # POST …/cancel does not even document 401 (OpenAPI lists 200, 404, 409, 422)
        cancel_noauth = """curl -sS -H "Content-Type: application/json" -d '{"reason":"x"}' "$BASE_URL/v1/orders/$ORDER_ID/cancel" -o n.json"""
        self.rejected(self.scenario("ORD-094 (Regression) — POST cancel without auth", "Auth regression.",
                                    cancel_noauth, "HTTP 401."), "expected status 401 is not documented for POST")

    def test_4_nonexistent_order_404_trigger_is_not_established(self):
        self.rejected(self.scenario("ORD-095 (Required) — Cancel non-existent order returns 404", "R2.",
                                    CANCEL_CURL, "HTTP 404."), "status 404 trigger")

    def test_5_documented_code_is_not_its_meaning(self):
        self.accepted(self.summary("- OpenAPI documents 404 for `POST /v1/orders/{orderId}/cancel` [FACT: openapi]"))
        self.rejected(self.summary("- OpenAPI documents 404 for `POST /v1/orders/{orderId}/cancel`, which means the "
                                   "order does not exist [FACT: openapi]"), "unsupported [FACT] claim")
        # the hypothesis wording stays acceptable
        self.accepted(self.scenario("ORD-096 (Required) — Cancel an unknown order id (verify)",
                                    "R2 — Verify whether an unknown order id returns 404 (verify).", CANCEL_CURL,
                                    "per contract (verify)."))


class FieldLimitValidationTests(_GroqC01):
    def test_6_cancellation_reason_cannot_be_asserted_as_established(self):
        v = self.rejected(self.summary("- Cancelled orders expose a `cancellation_reason` field."),
                          "untagged summary/requirement line", "identifier `cancellation_reason`")
        self.assertIn("[STATED: PR description]", " ".join(v["errors"]))          # description-only: re-attribute it
        self.rejected(self.summary("- Cancelled orders expose `cancellation_reason` [FACT: diff]"), "unsupported [FACT] claim")
        self.rejected(self.scenario("ORD-097 (Required) — Cancelled order shows `cancellation_reason`", "R3.",
                                    LIST_CURL, "`cancellation_reason` equals the sent reason."),
                      "identifier `cancellation_reason`")
        self.accepted(self.summary("- Description says cancelled orders expose `cancellation_reason` [STATED: PR description]"))

    def test_7_500_character_limit_cannot_be_asserted(self):
        self.rejected(self.summary("- `reason` is limited to 500 characters."), "untagged summary/requirement line", "limit 500")
        self.rejected(self.summary("- `reason` max 500 characters [FACT: diff]"), "unsupported [FACT] claim", "limit 500")
        self.rejected(self.scenario("ORD-098 (Required) — Over-long reason is rejected", "R2.", CANCEL_CURL,
                                    "a reason over 500 characters returns 422."), "limit 500")
        self.accepted(self.summary("- Description says `reason` is at most 500 chars [STATED: PR description]"))

    def test_8_conventional_validation_claims_do_not_pass(self):
        self.rejected(self.summary("- `POST /v1/orders/{orderId}/cancel` requires a `reason` field."),
                      "untagged summary/requirement line", "a field's existence is not its validation")
        self.rejected(self.scenario("ORD-099 (Required) — Reason is mandatory", "R2 — `reason` is required.",
                                    CANCEL_CURL, "HTTP 200."), "unsupported claim in a Required/Regression scenario")
        # an identifier in neither the evidence nor the description gets no [STATED] hint: it is invented
        v = self.rejected(self.summary("- Cancel also records a `refund_amount`."), "identifier `refund_amount`")
        self.assertNotIn("[STATED: PR description]", " ".join(e for e in v["errors"] if "refund_amount" in e))


class StillAcceptedTests(_GroqC01):
    def test_9_evidence_backed_c01_content_is_accepted(self):
        v = self.accepted(self.good)
        rub = qa_rubric.score_prompt(self.good, pr_title=C01.title, context=self.ctx, gate=v)
        self.assertGreaterEqual(rub.quality_score, qa_rubric.THRESHOLD)
        self.assertEqual(rub.hard_fails, [])
        self.accepted(self.summary("- Cancel handler reads `payload.reason` [FACT: diff]",
                                   "- OpenAPI documents 409 for `POST /v1/orders/{orderId}/cancel` [FACT: openapi]"))
        self.accepted(self.scenario("ORD-100 (Required) — List orders", "R1.", LIST_CURL, "HTTP 200 per contract (verify)."))

    def test_10_description_only_provenance_and_verify_still_work(self):
        self.accepted(self.summary("- Description says `reason` is required, max 500 characters [STATED: PR description]"))
        self.accepted(self.scenario("ORD-101 (Required) — Cancelled order exposes `cancellation_reason` (verify)",
                                    "R3 — the description says cancelled orders expose it (verify).", LIST_CURL,
                                    "`cancellation_reason` set for cancelled orders (verify)."))
        hedged = self.summary("- Whether `reason` is required and its max length is enforced is unverified (verify).")
        v = self.validate(hedged)
        self.assertFalse([m for m in v["errors"] if "validation" in m], v["errors"])

    def test_11_scope_violation_is_still_a_hard_fail(self):
        text = self.scenario("ORD-102 (Required) — Fetch cancelled order", "R3.", ITEM_CURL, "HTTP 200.")
        v = self.validate(text)
        rub = qa_rubric.score_prompt(text, pr_title=C01.title, context=self.ctx, gate=v)
        self.assertTrue(any(h.startswith("HF-SCOPE-VIOLATION") for h in rub.hard_fails), rub.hard_fails)


class RecordedGroqOutputTests(_GroqC01):
    @unittest.skipUnless(GROQ_C01.exists(), "archived Groq C01 output not present")
    def test_recorded_groq_output_is_still_rejected_for_each_observed_defect(self):
        content = json.loads(GROQ_C01.read_text(encoding="utf-8"))["content"]
        v = self.validate(content)
        self.assertFalse(v["valid"])
        joined = " | ".join(v["errors"])
        for needle in ("GET /v1/orders/$ORDER_ID", "status 404 trigger", "status 401 trigger",
                       "expected status 401 is not documented for POST", "cancellation_reason", "limit 500"):
            self.assertIn(needle, joined, needle)
        rub = qa_rubric.score_prompt(content, pr_title=C01.title, context=self.ctx, gate=v)
        self.assertTrue(any(h.startswith("HF-SCOPE-VIOLATION") for h in rub.hard_fails))
        self.assertLess(rub.quality_score, 85)



class ClaimLedgerTests(_GroqC01):
    """The claim ledger sent with the request names exactly what the gate rejects when asserted."""

    def test_12_ledger_lists_the_description_only_claims_and_unestablished_triggers(self):
        ledger = claim_ledger(self.ctx)
        for needle in ("identifier `cancellation_reason`", "limit 500", "validation claim 'require' for reason",
                       "404 on POST /v1/orders/{id}/cancel (synthetic)", "401 on GET /v1/orders (synthetic)",
                       "on that same line", "`(verify)`"):
            self.assertIn(needle, ledger)
        self.assertNotIn("identifier `reason`", ledger)          # `reason` is in the diff
        user = build_llm_messages(Templates.load(ROOT / "prompts"), repository="synthetic-org/bench-repo",
                                  pr_number=C01.pr_number, test_level="standard", synthetic=True,
                                  context=self.ctx)[1]["content"]
        self.assertIn(ledger, user)
        self.assertNotIn("{{CLAIM_LEDGER}}", user)
        self.assertIn("literal word BLOCKED", user)

    def test_13_ledger_items_match_the_gate(self):
        # each listed item, stated as an untagged requirement, is rejected; tagged on the same line it is accepted
        for item in ("`cancellation_reason`", "at most 500 characters"):
            with self.subTest(item=item):
                self.rejected(self.summary(f"- Cancel records {item}."), "untagged summary/requirement line")
                self.accepted(self.summary(f"- Description says cancel records {item} [STATED: PR description]"))
        # a [STATED] tag on the next bullet does not cover the untagged headline (the run-2 pattern)
        self.rejected(self.summary("**R3: Cancellation outcome** – Cancelled orders expose `cancellation_reason`.",
                                   "- Field exposure described [STATED: PR description]"),
                      "identifier `cancellation_reason`")

    def test_14_ledger_names_nothing_for_evidence_backed_context(self):
        ctx = json.loads(json.dumps(self.ctx))
        ctx["description"] = {"text": "Adds a `reason` to the cancel request."}
        ledger = claim_ledger(ctx)
        self.assertNotIn("Description-only", ledger)
        self.assertNotIn("identifier `", ledger)

    @unittest.skipUnless(GROQ_C01_RUN2.exists(), "archived second Groq C01 output not present")
    def test_15_recorded_second_output_is_rejected_for_each_observed_defect(self):
        content = json.loads(GROQ_C01_RUN2.read_text(encoding="utf-8"))["content"]
        v = self.validate(content)
        self.assertFalse(v["valid"])
        joined = " | ".join(v["errors"])
        for needle in ("missing required phrase: BLOCKED", "validation claim 'require' for reason", "limit 500",
                       "identifier `cancellation_reason`", "status 404 trigger", "status 401 trigger"):
            self.assertIn(needle, joined, needle)
        rub = qa_rubric.score_prompt(content, pr_title=C01.title, context=self.ctx, gate=v)
        self.assertTrue(any(h.startswith("HF-SCOPE-VIOLATION") for h in rub.hard_fails))
        self.assertLess(rub.quality_score, qa_rubric.THRESHOLD)


if __name__ == "__main__":
    unittest.main()
