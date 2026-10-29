"""Offline regressions from the third live Groq C01 output (2026-10-26, gate-rejected 70.0, HF-FABRICATED-FACT).

That output stated `reason` requiredness as a diff fact, read `orders.json` / `cancelled_orders.json` that no request
writes, and its section 1 objective ("Validate that the new `status` query filter … behave per …") was misread as a
claim that the application validates `status`. The first two stay rejected; the imperative objective is a test
directive, while third-person validation claims stay rejected. No network, no model call; the pinned output is only
read, never printed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from prqa_worker import prompt_builder as pb  # noqa: E402
from tests.test_groq_c01_regressions import AUTH, CANCEL_CURL, LIST_CURL, _GroqC01  # noqa: E402
import qa_rubric  # noqa: E402

GROQ_C01_RUN3 = ROOT / "fixtures" / "recorded" / "groq-c01" / "groq-c01-gate-rejected-20261026-run3" / "case-C01-clear-behaviour.response.json"
H1 = "## 1. Role and Objective\n"
H4 = "## 4. Environment and API Discovery\n"
JSON_HDR = '-H "Content-Type: application/json"'


def cancel_with_id_from(file: str) -> str:
    return (f"""curl -sS -X POST {AUTH} {JSON_HDR} -d '{{"reason":"x"}}' """
            f""""$BASE_URL/v1/orders/$(jq -r '.[0].id' {file})/cancel" -o c.json""")


SELF_CONTAINED = (f"""ORDER_ID=$(curl -sS {AUTH} "$BASE_URL/v1/orders" | jq -r '.[0].id'); """
                  f"""curl -sS -X POST {AUTH} {JSON_HDR} -d '{{"reason":"Customer request"}}' """
                  f""""$BASE_URL/v1/orders/$ORDER_ID/cancel" -o cancel_ok.json -w '%{{http_code}}'""")


class Run3RemediationTests(_GroqC01):
    def hard_fails(self, text):
        v = self.validate(text)
        return qa_rubric.score_prompt(text, pr_title=self.ctx["pr"]["title"], context=self.ctx, gate=v).hard_fails

    # 1. unsupported requiredness stays a hard failure
    def test_01_reason_requiredness_as_fact_is_rejected(self):
        text = self.summary("- Request body must contain a `reason` field [FACT: diff]")
        self.rejected(text, "unsupported [FACT] claim", "validation claim 'Request body must contain' for reason")
        self.assertTrue(any(h.startswith("HF-FABRICATED-FACT") for h in self.hard_fails(text)))
        for wording in ("- `reason` is required [FACT: diff]", "- The cancel payload must provide `reason` [FACT: openapi]"):
            with self.subTest(wording=wording):
                self.rejected(self.summary(wording), "unsupported [FACT] claim")
        # an expected failure for omitting it, asserted in a Required scenario, is rejected too
        self.rejected(self.scenario("ORD-301 (Required) — Missing reason is rejected", "R2 — `reason` is required.",
                                    CANCEL_CURL.replace('{"reason":"Test"}', "{}"), "HTTP 422."),
                      "unsupported claim in a Required/Regression scenario")

    # 2. evidence-backed use of the reason field stays valid
    def test_02_sending_reason_and_its_diff_presence_are_accepted(self):
        self.accepted(self.summary("- Cancel handler reads `payload.reason` [FACT: diff]"))
        self.accepted(self.scenario("ORD-302 (Required) — Cancel a listed order with a reason (verify)",
                                    "R2 — the documented request carries `reason`.", SELF_CONTAINED,
                                    "per contract (verify)."))
        # requiredness as a hypothesis is the permitted treatment
        self.accepted(self.scenario("ORD-303 (Conditional) — Whether omitting reason is rejected (verify)",
                                    "R2 — Verify whether a cancel without `reason` is rejected (verify).",
                                    CANCEL_CURL.replace('{"reason":"Test"}', "{}"), "per contract (verify)."))

    # 3. imperative directives are not factual claims
    def test_03_imperative_validate_directive_is_not_a_claim(self):
        objective = ("Validate that the new `status` query filter on GET /v1/orders and the cancellation on "
                     "POST /v1/orders/{orderId}/cancel behave per the PR description and OpenAPI contract.\n")
        self.accepted(self.good.replace(H1, H1 + objective, 1))
        self.accepted(self.summary("- Validate the `status` filter for each listed value."))
        self.accepted(self.summary("**R1: Status filter** — Validate that `status` filters the list."))
        # run-3 line 3 also ended "…and any gaps that must be flagged as blockers": a reporting duty, not validation
        self.accepted(self.good.replace(H1, H1 + "Validate the `status` filter and record any gaps that must be "
                                        "flagged as blockers.\n", 1))

    # 4. genuinely unsupported untagged assertions stay rejected
    def test_04_untagged_factual_validation_claims_are_still_rejected(self):
        for claim in ("- The handler validates `status` against the enum.",
                      "- `status` is validated against the listed values.",
                      "- Validate the list; the API validates `status` against the enum.",
                      "- VALIDATE `status` against the enum.",
                      "- `reason` is required.",
                      "- `status` must be one of the listed values.",
                      "- A `status` outside the list must be rejected."):
            with self.subTest(claim=claim):
                self.rejected(self.summary(claim), "untagged summary/requirement line")
        # the imperative exemption never covers an untagged invented field or limit on the same line
        self.rejected(self.summary("- Validate that `cancellation_reason` is at most 500 characters."), "limit 500")

    # 5. same-line provenance stays mandatory
    def test_05_description_only_claim_needs_its_tag_on_the_same_line(self):
        self.rejected(self.summary("- `reason` length limit is 500 characters.",
                                   "- (described in the PR) [STATED: PR description]"), "limit 500")
        self.accepted(self.summary("- `reason` length limit is 500 characters [STATED: PR description]"))

    # 6./7. response files must be written before they are read
    def test_06_07_unestablished_response_files_are_rejected(self):
        for name in ("orders.json", "cancelled_orders.json"):
            with self.subTest(file=name):
                self.rejected(self.scenario("ORD-304 (Required) — Cancel an order (verify)", "R2.",
                                            cancel_with_id_from(name), "per contract (verify)."),
                              f"jq reads {name}, which no request in the document writes")

    # 8. self-contained and properly established response processing stays accepted
    def test_08_self_contained_or_established_files_are_accepted(self):
        self.accepted(self.scenario("ORD-305 (Required) — Cancel a listed order (verify)", "R2.", SELF_CONTAINED,
                                    "per contract (verify)."))
        # a file written by a discovery request in section 4 is an established prerequisite
        discovery = f'- List orders: `curl -sS {AUTH} "$BASE_URL/v1/orders" -o orders.json`\n'
        with_discovery = self.good.replace(H4, H4 + discovery, 1)
        self.assertNotEqual(with_discovery, self.good)
        self.accepted(with_discovery.replace("## 6. ", "**ORD-306 (Required) — Cancel the first listed order (verify)**\n"
                                             "- Requirement/Risk: R2.\n- Request: `" + cancel_with_id_from("orders.json")
                                             + "`\n- Expected: per contract (verify).\n\n## 6. ", 1))

    # 9. supported endpoints and methods stay accepted
    def test_09_supported_operations_are_accepted(self):
        self.accepted(self.scenario("ORD-307 (Required) — List orders", "R1.", LIST_CURL, "HTTP 200 per contract (verify)."))
        self.accepted(self.scenario("ORD-308 (Required) — Cancel a listed order (verify)", "R2.", SELF_CONTAINED,
                                    "per contract (verify)."))

    # 10. BLOCKED stays mandatory
    def test_10_blocked_item_is_still_enforced(self):
        self.rejected(re.sub(r"BLOCKED", "Pending", self.good), "missing BLOCKED item")
        self.rejected(re.sub(r"(?i)blocked", "Pending", self.good), "missing required phrase: BLOCKED")

    def test_prompt_states_the_three_rules(self):
        s = (ROOT / "prompts" / "llm" / "system.v2.md").read_text(encoding="utf-8")
        for needle in ("Sending `reason` is fine", "a tag does not make it a fact", "| jq -r 'first(..|.id? // empty)')",
                       "every file a `jq` command reads is written earlier in this document",
                       "no `orders.json` exists otherwise", "already-cancelled order"):
            with self.subTest(needle=needle):
                self.assertIn(needle, s)
        self.assertNotIn("ORDER_ID=$(jq -r '.[0].id' orders.json)", s)        # the example the model copied
        self.assertNotIn("'.[0].id'", s)              # C01 documents no response body: an example must not assume one

    def test_the_prompts_own_runnable_examples_pass_the_gate(self):
        """Contradiction guard: an example the prompt tells the model to copy must itself satisfy the gate."""
        s = (ROOT / "prompts" / "llm" / "system.v2.md").read_text(encoding="utf-8")
        id_example = re.search(r"\(`(ORDER_ID=\$\(curl [^`]+)`\)", s).group(1)
        request = (id_example + f"""; curl -sS -X POST {AUTH} {JSON_HDR} -d '{{"reason":"Customer request"}}' """
                   '"$BASE_URL/v1/orders/$ORDER_ID/cancel" -o c.json')
        self.accepted(self.scenario("ORD-309 (Required) — Cancel a listed order (verify)", "R2.", request,
                                    "per contract (verify)."))
        all_lines = s.splitlines()
        start = next(i for i, l in enumerate(all_lines) if l.strip().startswith("**ORD-001 ("))
        end = next(i for i in range(start, len(all_lines)) if all_lines[i].strip() == "```")
        lines = [l.strip() for l in all_lines[start:end]]
        self.assertEqual(len(lines), 4)
        self.accepted(self.good.replace("## 6. ", "\n".join(lines) + "\n\n## 6. ", 1))

    @unittest.skipUnless(shutil.which("jq"), "jq not installed")
    def test_id_example_filter_compiles_and_is_shape_neutral(self):
        s = (ROOT / "prompts" / "llm" / "system.v2.md").read_text(encoding="utf-8")
        flt = re.search(r"\| jq -r '([^']+)'\)", s).group(1)
        for doc, want in (('[{"id":"a1"},{"id":"a2"}]', "a1"), ('{"data":[{"id":7}],"pagination":{}}', "7"),
                          ('{"items":[]}', "")):
            out = subprocess.run(["jq", "-r", flt], input=doc, capture_output=True, text=True, timeout=10)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), want)

    @unittest.skipUnless(GROQ_C01_RUN3.exists(), "pinned run-3 output not present")
    def test_recorded_run3_output_still_fails_for_its_real_defects_only(self):
        content = json.loads(GROQ_C01_RUN3.read_text(encoding="utf-8"))["content"]
        v = self.validate(content)
        self.assertFalse(v["valid"])
        joined = " | ".join(v["errors"])
        for needle in ("validation claim 'Request body must contain' for reason", "jq reads orders.json",
                       "jq reads cancelled_orders.json"):
            self.assertIn(needle, joined)
        self.assertNotIn("validation claim 'Validate' for status", joined)     # the objective is a directive
        self.assertNotIn("(line 3)", joined)
        rub = qa_rubric.score_prompt(content, pr_title=self.ctx["pr"]["title"], context=self.ctx, gate=v)
        self.assertTrue(any(h.startswith("HF-FABRICATED-FACT") for h in rub.hard_fails))
        self.assertLess(rub.quality_score, qa_rubric.THRESHOLD)
        self.assertEqual(qa_rubric.THRESHOLD, 85.0)


class ImperativeHelperTests(unittest.TestCase):
    def test_only_a_sentence_initial_base_form_validate_is_a_directive(self):
        def directive(line):
            m = pb.VALIDATION_CLAIM_RE.search(line)
            return pb._is_imperative_directive(line, m)
        for line in ("Validate that the filter works.", "- Validate `status`.", "1. Validate `status`.",
                     "**R1:** Validate `status`.", "Objective: Validate the cancel flow."):
            self.assertTrue(directive(line), line)
        for line in ("The API validates `status`.", "- validates `status`", "`status` is validated.",
                     "We then validate `status`.", "VALIDATE `status`.", "`status` must be one of a, b.",
                     "`status` must be rejected when unknown."):
            self.assertFalse(directive(line), line)
        self.assertTrue(directive("gaps that must be flagged as blockers"))


if __name__ == "__main__":
    unittest.main()
