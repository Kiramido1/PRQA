"""Offline guards for the 2026-10-26 remediation: the size of the constructed C01 request, the completion budget the
request actually carries, and one regression per defect seen in the two live Groq C01 outputs.

Token counts are an ESTIMATE: no tokenizer for openai/gpt-oss-120b is installed. The estimate is calibrated on the
provider's own count for this request shape, live request 3 (2026-10-26): 16,633 message characters were billed as
4,125 prompt tokens, i.e. 4.032 characters per token (request 1, the older and larger prompt, gave 4.237). A
pessimistic 3.5 characters per token is checked as a second bound.
No network, no model call.
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from prqa_worker import prompt_builder as pb  # noqa: E402
from prqa_worker.policy import DEFAULT_PROMPT_LLM_MODEL, Policy  # noqa: E402
from prqa_worker.prompt_builder import Templates, build_llm_request, claim_ledger  # noqa: E402
from tests.qa_benchmark import CASES, context_for  # noqa: E402
from tests.test_groq_c01_regressions import AUTH, CANCEL_CURL, ITEM_CURL, LIST_CURL, _GroqC01  # noqa: E402
import qa_rubric  # noqa: E402

CALIBRATED_CHARS_PER_TOKEN = 16_633 / 4_125        # live request 3: message characters / billed prompt tokens
PESSIMISTIC_CHARS_PER_TOKEN = 3.5
C01_PROMPT_TOKEN_TARGET = 4_200
COMPLETION_BUDGET = 3_000
OBSERVED_TPM_LIMIT = 8_000                        # from the sanitized 413 message of live request 2


def env_value(path: Path, name: str) -> str:
    """One non-secret assignment from an env file (nothing else is read into the test)."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"{name} not set in {path.name}")


def production_c01_request(env: dict | None = None) -> dict:
    """The C01 request exactly as /prompts/build and the evaluator construct it (policy settings -> build_llm_request)."""
    policy = Policy.from_env(env or {})
    gen = policy.generation_settings()
    c01 = next(c for c in CASES if c.cid == "C01-clear-behaviour")
    # the configured production model (review-only routes the same request shape to the mock under its own name)
    return build_llm_request(Templates.load(ROOT / "prompts"), model=DEFAULT_PROMPT_LLM_MODEL,
                             provider=policy.prompt_llm_provider, max_tokens=gen["max_tokens"],
                             reasoning=gen["reasoning"], reasoning_max_tokens=gen["reasoning_max_tokens"],
                             reasoning_effort=gen["reasoning_effort"], repository="git@synthetic-org/bench-repo",
                             pr_number=c01.pr_number, test_level="standard", synthetic=True, context=context_for(c01))


def message_chars(request: dict) -> int:
    return sum(len(m["content"]) for m in request["messages"])


class PromptBudgetTests(unittest.TestCase):
    def test_constructed_c01_prompt_is_within_the_estimated_token_target(self):
        chars = message_chars(production_c01_request())
        estimate = chars / CALIBRATED_CHARS_PER_TOKEN
        self.assertLessEqual(estimate, C01_PROMPT_TOKEN_TARGET, f"{chars} chars ~ {estimate:.0f} tokens (estimate)")
        # the pre-remediation request was 33,833 chars: keep at least the reduction to about half
        self.assertLess(chars, 17_000)

    def test_prompt_plus_completion_fits_the_observed_tpm_limit_even_pessimistically(self):
        chars = message_chars(production_c01_request())
        self.assertLess(chars / PESSIMISTIC_CHARS_PER_TOKEN + COMPLETION_BUDGET, OBSERVED_TPM_LIMIT)

    def test_no_duplicated_reference_material_or_indentation(self):
        req = production_c01_request()
        system, user = (m["content"] for m in req["messages"])
        self.assertNotIn("<<<REFERENCE_PROMPT>>>", user)
        self.assertNotIn("## 2. Strict Testing Constraints", system + user)
        ctx_json = user.split("<<<PR_CONTEXT>>>\n", 1)[1].split("\n<<<END_PR_CONTEXT>>>", 1)[0]
        self.assertNotIn('\n  "', ctx_json)                    # compact JSON
        self.assertEqual(json.loads(ctx_json)["pr"]["number"], next(c for c in CASES if c.cid == "C01-clear-behaviour").pr_number)
        # each mandatory heading is specified once (not repeated across system rules and reference excerpt)
        for heading in ("## 5. Prioritized Test Scenarios", "## 9. Defect Reporting", "## 11. Blockers, Assumptions and Ambiguities"):
            self.assertEqual((system + user).count(heading), 1, heading)

    def test_every_benchmark_case_stays_near_the_target(self):
        t = Templates.load(ROOT / "prompts")
        for case in CASES:
            with self.subTest(case=case.cid):
                req = build_llm_request(t, model="openai/gpt-oss-120b", provider="groq", max_tokens=COMPLETION_BUDGET,
                                        reasoning_effort="low", repository="r/r", pr_number=case.pr_number,
                                        test_level="standard", synthetic=True, context=context_for(case))
                self.assertLessEqual(message_chars(req) / CALIBRATED_CHARS_PER_TOKEN, C01_PROMPT_TOKEN_TARGET + 300)


class CompletionBudgetTests(unittest.TestCase):
    def test_configured_budget_is_3000_in_every_source(self):
        self.assertEqual(Policy.from_env({}).generation_settings()["max_tokens"], COMPLETION_BUDGET)
        self.assertEqual(env_value(ROOT / "compose" / ".env.example", "PROMPT_LLM_MAX_TOKENS"), str(COMPLETION_BUDGET))
        env_file = ROOT / "compose" / ".env"
        if env_file.exists():
            self.assertEqual(env_value(env_file, "PROMPT_LLM_MAX_TOKENS"), str(COMPLETION_BUDGET))
        compose = (ROOT / "compose" / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("${PROMPT_LLM_MAX_TOKENS:-3000}", compose)

    def test_request_serializes_the_budget_and_the_fixed_groq_parameters(self):
        req = production_c01_request({"PROMPT_LLM_MAX_TOKENS": str(COMPLETION_BUDGET)})
        wire = json.loads(json.dumps(req))
        self.assertEqual({k: v for k, v in wire.items() if k != "messages"},
                         {"model": "openai/gpt-oss-120b", "temperature": 0.2, "max_completion_tokens": 3000,
                          "stream": False, "reasoning_effort": "low", "include_reasoning": False})
        self.assertNotIn("max_tokens", wire)
        self.assertNotIn("reasoning", wire)

    def test_stated_output_budget_matches_the_completion_limit(self):
        user = production_c01_request()["messages"][1]["content"]
        self.assertIn("completion limit 3000 tokens, shared with reasoning", user)
        stated = int(re.search(r"at most (\d+) characters in total", user).group(1))
        self.assertEqual(stated, COMPLETION_BUDGET * pb.CHARS_PER_COMPLETION_TOKEN)
        # the gate's own character check is unchanged
        self.assertEqual(pb.plan_budget(context_for(CASES[0]))["max_chars"], pb.OUTPUT_CHAR_BUDGET)

    def test_evaluator_uses_the_policy_budget(self):
        src = (ROOT / "scripts" / "eval" / "evaluate_groq_live.py").read_text(encoding="utf-8")
        self.assertIn("generation_settings()", src)
        self.assertNotRegex(src, r"max_tokens\s*=\s*\d")      # no hard-coded completion limit


class DefectMatrixTests(_GroqC01):
    """One check per defect seen in the live Groq C01 outputs. The gate is not relaxed for any of them."""

    def test_d1_description_only_fact_presented_as_verified(self):
        self.rejected(self.summary("- Cancelling requires a `reason` of at most 500 characters [FACT: openapi]"),
                      "unsupported [FACT] claim")
        self.rejected(self.summary("- Cancelled orders expose `cancellation_reason`."), "untagged summary/requirement line")

    def test_d2_tag_on_a_different_line(self):
        self.rejected(self.summary("**R3: Cancellation** – a cancel stores `cancellation_reason`.",
                                   "- Described in the PR [STATED: PR description]"), "identifier `cancellation_reason`")
        self.accepted(self.summary("- Description says a cancel stores `cancellation_reason` [STATED: PR description]"))

    def test_d3_unestablished_404_trigger(self):
        self.rejected(self.scenario("ORD-201 (Required) — Unknown order returns 404", "R2.", CANCEL_CURL, "HTTP 404."),
                      "status 404 trigger")

    def test_d4_unestablished_401_missing_auth_trigger(self):
        self.rejected(self.scenario("ORD-202 (Regression) — List without auth returns 401", "Auth.",
                                    'curl -sS "$BASE_URL/v1/orders" -o n.json', "HTTP 401."), "status 401 trigger")

    def test_d5_invented_field(self):
        self.rejected(self.scenario("ORD-203 (Required) — Cancel stores `cancellation_reason`", "R3.", LIST_CURL,
                                    "`cancellation_reason` equals the reason."), "identifier `cancellation_reason`")
        self.rejected(self.summary("- Cancel also stores a `cancel_note`."), "identifier `cancel_note`")

    def test_d6_invented_limit(self):
        self.rejected(self.scenario("ORD-204 (Required) — Reason over 500 characters", "R2.", CANCEL_CURL,
                                    "a reason over 500 characters returns 422."), "limit 500")

    def test_d7_reason_is_required(self):
        self.rejected(self.summary("- `reason` is required."), "a field's existence is not its validation")
        self.rejected(self.scenario("ORD-205 (Required) — Reason is mandatory", "R2 — `reason` is required.",
                                    CANCEL_CURL, "HTTP 200."), "unsupported claim in a Required/Regression scenario")

    def test_d8_unsupported_endpoint_or_method(self):
        v = self.rejected(self.scenario("ORD-206 (Required) — Read one order", "R3.", ITEM_CURL, "HTTP 200."),
                          "GET /v1/orders/$ORDER_ID")
        self.assertTrue(any(h.startswith("HF-SCOPE-VIOLATION") for h in qa_rubric.score_prompt(
            self.scenario("ORD-206 (Required) — Read one order", "R3.", ITEM_CURL, "HTTP 200."),
            pr_title=self.ctx["pr"]["title"], context=self.ctx, gate=v).hard_fails))
        # a supported path with a method the evidence does not list
        self.rejected(self.scenario("ORD-207 (Required) — Delete the cancel resource", "R2.",
                                    f'curl -sS {AUTH} -X DELETE "$BASE_URL/v1/orders/$ORDER_ID/cancel" -o d.json',
                                    "per contract (verify)."), "DELETE /v1/orders/$ORDER_ID/cancel")
        self.rejected(self.scenario("ORD-208 (Required) — Patch the list", "R1.",
                                    f'curl -sS {AUTH} -X PATCH "$BASE_URL/v1/orders" -o p.json',
                                    "per contract (verify)."), "PATCH /v1/orders")

    def test_d9_missing_blocked_item(self):
        self.assertIn("BLOCKED", pb.REQUIRED_PHRASES)
        no_word = re.sub(r"(?i)blocked", "Pending", self.good)
        self.rejected(no_word, "missing required phrase: BLOCKED")
        # the run-2 shape: "Blocked" only in a label legend / result vocabulary, no BLOCKED item anywhere
        legend_only = re.sub(r"BLOCKED", "Pending", self.good)
        self.assertIn("blocked", legend_only.lower())
        self.rejected(legend_only, "missing BLOCKED item")
        # either form of a real item satisfies the gate
        self.accepted(legend_only.replace("## 11. Blockers, Assumptions and Ambiguities\n",
                                          "## 11. Blockers, Assumptions and Ambiguities\n- BLOCKED: no disposable cancelled-order fixture.\n", 1))
        self.accepted(self.scenario("ORD-210 (Blocked) — Cancel an already-cancelled order", "R2 — needs a cancelled fixture.",
                                    CANCEL_CURL, "per contract (verify)."))

    def test_legitimate_evidence_backed_assertions_still_pass(self):
        v = self.accepted(self.good)
        rub = qa_rubric.score_prompt(self.good, pr_title=self.ctx["pr"]["title"], context=self.ctx, gate=v)
        self.assertGreaterEqual(rub.quality_score, qa_rubric.THRESHOLD)
        self.assertEqual(qa_rubric.THRESHOLD, 85.0)
        self.accepted(self.summary("- Cancel handler reads `payload.reason` [FACT: diff]",
                                   "- OpenAPI documents 409 for `POST /v1/orders/{orderId}/cancel` [FACT: openapi]",
                                   "- Description says `reason` is required, max 500 characters [STATED: PR description]"))
        self.accepted(self.scenario("ORD-209 (Required) — Cancel an unknown order id (verify)",
                                    "R2 — Verify whether an unknown order id returns 404 (verify).", CANCEL_CURL,
                                    "per contract (verify)."))

    def test_gate_does_not_consult_the_claim_ledger(self):
        # the ledger only informs the model; it can never make an unsupported assertion acceptable
        bad = self.summary("- `reason` is limited to 500 characters.")
        before = self.validate(bad)
        with mock.patch.object(pb, "claim_ledger", side_effect=AssertionError("gate read the ledger")):
            during = self.validate(bad)
        self.assertEqual(before["errors"], during["errors"])
        self.assertFalse(during["valid"])
        # quoting the ledger inside the plan does not cover a claim either
        ledger_text = "- Ledger: " + claim_ledger(self.ctx).replace("\n", " ") + "\n"
        self.rejected(bad.replace("## 3.", ledger_text + "\n## 3.", 1), "limit 500")


if __name__ == "__main__":
    unittest.main()
