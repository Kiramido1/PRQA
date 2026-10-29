"""Regression tests for rubric/gate parsing defects found in the offline C01 rubric audit.

1. executability: scenarios in the system prompt's mandated format (`Request: curl ... "$BASE_URL/path"`)
   were never recognised as naming a concrete operation (only a literal `METHOD /path` token or builder
   boilerplate counted), so fully runnable curl scenarios scored 0/N.
2. positive coverage: "succeeds"/"succeeded" are not substrings of "success".
3. rubric `_path_set` collected HTTP methods instead of paths.
4. gate `_norm_path`: sentence punctuation after a path ("GET /v1/orders.") made a supported endpoint
   look unsupported — a false HF-FABRICATED-FACT on a correct [FACT] line.

Each fix is paired with tests that keep genuine defects penalised. Offline: fixtures only.
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "tests"))

from prqa_worker import prompt_builder as pb  # noqa: E402
from prqa_worker.prompt_builder import Templates, reference_fallback_prompt, validate_generated_prompt  # noqa: E402
import qa_rubric  # noqa: E402
from qa_benchmark import CASES, context_for  # noqa: E402

C01 = CASES[0]
CTX = context_for(C01)
AUTH = '-H "Authorization: Bearer $TOKEN"'


def section5(*blocks: str) -> str:
    return "## 5. Prioritized Test Scenarios\n\n" + "\n\n".join(blocks) + "\n\n## 6. Authorization and Security Checks\n"


def block(sid: str, label: str, title: str, request: str | None, expected: str | None = "HTTP 200 per contract (verify).") -> str:
    lines = [f"**{sid} ({label}) — {title}**", "- Requirement/Risk: R1."]
    if request is not None:
        lines.append(f"- Request: `{request}`")
    if expected is not None:
        lines.append(f"- Expected: {expected}")
    return "\n".join(lines)


def score(text: str) -> qa_rubric.RubricResult:
    return qa_rubric.score_prompt(text, pr_title=C01.title, context=CTX)


class CurlOperationParsingTests(unittest.TestCase):
    def test_method_inference(self):
        cases = {
            f'curl -sS {AUTH} "$BASE_URL/v1/orders?status=paid" -o o.json': ("GET", "/v1/orders"),
            f"curl -sS {AUTH} -d '{{\"reason\":\"x\"}}' \"$BASE_URL/v1/orders/{{orderId}}/cancel\"": ("POST", "/v1/orders/{orderId}/cancel"),
            'curl -sS --data-raw \'{}\' "$BASE_URL/v1/orders/1/cancel"': ("POST", "/v1/orders/1/cancel"),
            'curl -sS -X DELETE "$BASE_URL/v1/orders/1"': ("DELETE", "/v1/orders/1"),
            'curl -sS --request patch "${BASE_URL}/v1/orders/1"': ("PATCH", "/v1/orders/1"),
            'curl -sS -I "$BASE_URL/v1/orders"': ("HEAD", "/v1/orders"),
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(qa_rubric.curl_operations(cmd), [expected])

    def test_curl_without_base_url_yields_nothing(self):
        self.assertEqual(qa_rubric.curl_operations('curl -sS "https://example.com/v1/orders"'), [])


class ExecutabilityTests(unittest.TestCase):
    def test_mandated_format_curl_scenarios_are_concrete(self):
        text = section5(
            block("ORD-001", "Required", "Status filter (verify)", f'curl -sS {AUTH} "$BASE_URL/v1/orders?status=pending" -o o.json -w \'%{{http_code}}\''),
            block("ORD-002", "Required", "Cancel", f"curl -sS {AUTH} -d '{{\"reason\":\"r\"}}' \"$BASE_URL/v1/orders/{{orderId}}/cancel\" -o c.json"),
            block("ORD-003", "Required", "Cancel unknown order", f"curl -sS {AUTH} -d '{{\"reason\":\"r\"}}' \"$BASE_URL/v1/orders/00000000-0000-0000-0000-000000000000/cancel\""),
            block("ORD-004", "Regression", "Cancel placeholder id", f"curl -sS {AUTH} -d '{{\"reason\":\"r\"}}' \"$BASE_URL/v1/orders/<cancelled-id>/cancel\""),
        )
        r = score(text)
        self.assertTrue(r.dimensions["executability"].evidence[0].startswith("4/4 "), r.dimensions["executability"].evidence)
        self.assertEqual(r.score("executability"), 5)

    def test_invented_endpoint_earns_no_executability_credit(self):
        text = section5(block("ORD-001", "Required", "Invoices", f'curl -sS {AUTH} "$BASE_URL/v1/invoices" -o o.json'))
        r = score(text)
        self.assertTrue(r.dimensions["executability"].evidence[0].startswith("0/1 "))
        self.assertLessEqual(r.score("executability"), 1)

    def test_method_mismatch_earns_no_executability_credit(self):
        text = section5(
            block("ORD-001", "Required", "POST to list", f"curl -sS {AUTH} -d '{{}}' \"$BASE_URL/v1/orders\""),
            block("ORD-002", "Required", "DELETE cancel", f'curl -sS {AUTH} -X DELETE "$BASE_URL/v1/orders/{{orderId}}/cancel"'),
        )
        r = score(text)
        self.assertTrue(r.dimensions["executability"].evidence[0].startswith("0/2 "), r.dimensions["executability"].evidence)

    def test_scenario_without_request_or_base_url_is_still_penalised(self):
        text = section5(
            block("ORD-001", "Required", "Filter", f'curl -sS {AUTH} "$BASE_URL/v1/orders?status=paid"'),
            block("ORD-002", "Required", "No request line", None),
            block("ORD-003", "Required", "Hard-coded host", f'curl -sS {AUTH} "https://prod.example.com/v1/orders"'),
        )
        r = score(text)
        self.assertTrue(r.dimensions["executability"].evidence[0].startswith("1/3 "), r.dimensions["executability"].evidence)
        self.assertLessEqual(r.score("executability"), 2)

    def test_missing_expected_results_still_penalised(self):
        text = section5(
            block("ORD-001", "Required", "Filter", f'curl -sS {AUTH} "$BASE_URL/v1/orders?status=paid"', expected=None),
            block("ORD-002", "Required", "Cancel", f"curl -sS {AUTH} -d '{{\"reason\":\"r\"}}' \"$BASE_URL/v1/orders/1/cancel\"", expected=None),
        )
        r = score(text)
        self.assertEqual(r.score("executability"), 5)
        self.assertEqual(r.score("expected_results"), 0)


class PositiveCoverageTests(unittest.TestCase):
    def _positive(self, text: str) -> str:
        return score(text).dimensions["test_coverage"].evidence[0]

    def test_required_scenario_that_succeeds_is_positive(self):
        text = section5(block("ORD-001", "Required", "Cancel with valid reason succeeds", f"curl -sS {AUTH} -d '{{\"reason\":\"r\"}}' \"$BASE_URL/v1/orders/1/cancel\""))
        self.assertTrue(self._positive(text).startswith("positive: yes"), self._positive(text))

    def test_conditional_success_is_not_counted_as_positive(self):
        text = section5(block("ORD-001", "Conditional", "Cancel succeeds", f"curl -sS {AUTH} -d '{{\"reason\":\"r\"}}' \"$BASE_URL/v1/orders/1/cancel\""))
        self.assertTrue(self._positive(text).startswith("positive: MISSING"), self._positive(text))

    def test_purely_negative_required_scenario_is_not_positive(self):
        text = section5(block("ORD-001", "Required", "Missing reason rejected", f"curl -sS {AUTH} -d '{{}}' \"$BASE_URL/v1/orders/1/cancel\"", expected="HTTP 422."))
        self.assertTrue(self._positive(text).startswith("positive: MISSING"), self._positive(text))


class DescriptionPathSetTests(unittest.TestCase):
    def test_path_set_returns_paths_not_methods(self):
        self.assertEqual(qa_rubric._path_set("`GET /v1/orders` and `POST /v1/orders/{orderId}/cancel`"),
                         {"/v1/orders", "/v1/orders/{}/cancel"})


class ProvenanceNotExcusedTests(unittest.TestCase):
    """Parsing fixes must not let unsupported claims through."""

    @classmethod
    def setUpClass(cls):
        cls.body = reference_fallback_prompt(Templates.load(ROOT / "prompts"), repository="synthetic-org/bench-repo",
                                             pr_number=C01.pr_number, test_level="standard", context=CTX)
        assert validate_generated_prompt(cls.body, pr_title=C01.title, context=CTX)["valid"]

    def _with_section2_line(self, line: str) -> dict:
        head = "## 2. Verified PR Summary and Scope\n"
        self.assertIn(head, self.body)
        return validate_generated_prompt(self.body.replace(head, head + "\n" + line + "\n", 1), pr_title=C01.title, context=CTX)

    def test_norm_path_strips_sentence_punctuation_only(self):
        self.assertEqual(pb._norm_path("/v1/orders."), "/v1/orders")
        self.assertEqual(pb._norm_path("/v1/orders/{orderId}/cancel,"), "/v1/orders/{}/cancel")
        self.assertEqual(pb._norm_path("/v1/orders"), "/v1/orders")

    def test_supported_fact_endpoint_followed_by_period_is_accepted(self):
        gate = self._with_section2_line("- Added `status` query parameter to GET /v1/orders. [FACT: diff]")
        self.assertTrue(gate["valid"], gate["errors"])

    def test_invented_fact_endpoint_followed_by_period_still_hard_fails(self):
        gate = self._with_section2_line("- Added GET /v1/invoices. [FACT: diff]")
        self.assertFalse(gate["valid"])
        self.assertTrue(any("unsupported [FACT] claim" in e and "/v1/invoices" in e for e in gate["errors"]), gate["errors"])
        rub = qa_rubric.score_prompt(gate["text"], pr_title=C01.title, context=CTX, gate=gate)
        self.assertTrue(any(h.startswith("HF-FABRICATED-FACT") for h in rub.hard_fails), rub.hard_fails)

    def test_description_only_identifier_as_fact_still_hard_fails_with_curl_scenarios(self):
        gate = self._with_section2_line("- Diff adds the `cancellation_reason` response field [FACT: diff]")
        self.assertFalse(gate["valid"])
        rub = qa_rubric.score_prompt(gate["text"], pr_title=C01.title, context=CTX, gate=gate)
        self.assertTrue(any(h.startswith("HF-FABRICATED-FACT") for h in rub.hard_fails), rub.hard_fails)

    def test_invented_endpoint_in_required_scenario_with_period_still_errors(self):
        extra = ("**ORDERS-099 (Required) — Void an invoice**\n- Requirement/Risk: R1.\n"
                 "- Request: POST /v1/invoices/{id}/void.\n- Expected: HTTP 200.\n\n")
        text = self.body.replace("## 6. ", extra + "## 6. ", 1)
        gate = validate_generated_prompt(text, pr_title=C01.title, context=CTX)
        self.assertFalse(gate["valid"])
        self.assertTrue(any("unsupported claim in a Required/Regression scenario" in e and "/v1/invoices" in e
                            for e in gate["errors"]), gate["errors"])



class FullPathParsingTests(unittest.TestCase):
    """A path is parsed in full and checked as a complete method/path pair. Live C01 (prompt v3) wrote
    `GET /v1/orders/$ORDER_ID`; the old parser stopped at `$` and accepted it as the documented `GET /v1/orders`."""

    @classmethod
    def setUpClass(cls):
        cls.body = reference_fallback_prompt(Templates.load(ROOT / "prompts"), repository="synthetic-org/bench-repo",
                                             pr_number=C01.pr_number, test_level="standard", context=CTX)

    def gate_fact(self, claim: str) -> dict:
        head = "## 2. Verified PR Summary and Scope\n"
        return validate_generated_prompt(self.body.replace(head, head + "\n- " + claim + " [FACT: openapi]\n", 1),
                                         pr_title=C01.title, context=CTX)

    def test_variable_segments_stay_in_the_path(self):
        for raw, norm in {"/v1/orders/$ORDER_ID": "/v1/orders/{}", "/v1/orders/${ORDER_ID}/cancel": "/v1/orders/{}/cancel",
                          "/v1/orders/<order-id>/cancel": "/v1/orders/{}/cancel", "/v1/orders/{orderId}/cancel.": "/v1/orders/{}/cancel"}.items():
            with self.subTest(raw=raw):
                self.assertEqual(pb._norm_path(raw), norm)
        self.assertEqual([m.group(2) for m in pb.OPERATION_RE.finditer("then `GET /v1/orders/$ORDER_ID` shows it")],
                         ["/v1/orders/$ORDER_ID"])

    def test_documented_operations_are_accepted(self):
        for claim in ("Documents GET /v1/orders", "Documents POST /v1/orders/$ORDER_ID/cancel",
                      "Documents POST /v1/orders/<order-id>/cancel", "Documents GET /v1/orders."):
            with self.subTest(claim=claim):
                gate = self.gate_fact(claim)
                self.assertTrue(gate["valid"], gate["errors"])

    def test_undocumented_paths_and_methods_are_rejected(self):
        for claim, needle in (("Documents GET /v1/orders/$ORDER_ID", "/v1/orders/$ORDER_ID"),        # item from collection
                              ("Documents GET /v1/orders/{orderId}", "/v1/orders/{orderId}"),
                              ("Documents POST /v1/orders/$ORDER_ID/refund", "/refund"),              # shares a valid prefix
                              ("Documents GET /v1/orders-archive", "/v1/orders-archive"),
                              ("Documents DELETE /v1/orders/{orderId}/cancel", "DELETE")):           # wrong method
            with self.subTest(claim=claim):
                gate = self.gate_fact(claim)
                self.assertFalse(gate["valid"], claim)
                self.assertTrue(any("unsupported [FACT] claim" in e and needle in e for e in gate["errors"]), gate["errors"])

    def test_invented_item_endpoint_in_required_scenario_is_a_scope_violation(self):
        block = ("**ORDERS-099 (Required) — Cancel stores the reason**\n- Requirement/Risk: R1.\n"
                 "- Request: `curl -sS -d '{\"reason\":\"r\"}' \"$BASE_URL/v1/orders/$ORDER_ID/cancel\"`\n"
                 "- Expected: HTTP 200; subsequent `GET /v1/orders/$ORDER_ID` shows the order.\n\n")
        gate = validate_generated_prompt(self.body.replace("## 6. ", block + "## 6. ", 1), pr_title=C01.title, context=CTX)
        self.assertFalse(gate["valid"])
        self.assertTrue(any("Required/Regression" in e and "GET /v1/orders/$ORDER_ID" in e for e in gate["errors"]), gate["errors"])
        rub = qa_rubric.score_prompt(gate["text"], pr_title=C01.title, context=CTX, gate=gate)
        self.assertTrue(any(h.startswith("HF-SCOPE-VIOLATION") for h in rub.hard_fails), rub.hard_fails)

    def test_rubric_keeps_variable_segments_in_curl_paths(self):
        self.assertEqual(qa_rubric.curl_operations('curl -sS -d \'{}\' "$BASE_URL/v1/orders/$ORDER_ID/cancel" -o c.json'),
                         [("POST", "/v1/orders/$ORDER_ID/cancel")])
        ok = section5(block("ORD-001", "Required", "Cancel", f"curl -sS {AUTH} -d '{{\"reason\":\"r\"}}' \"$BASE_URL/v1/orders/$ORDER_ID/cancel\""))
        self.assertTrue(score(ok).dimensions["executability"].evidence[0].startswith("1/1 "))
        item = section5(block("ORD-001", "Required", "Read one order", f'curl -sS {AUTH} "$BASE_URL/v1/orders/$ORDER_ID"'))
        self.assertTrue(score(item).dimensions["executability"].evidence[0].startswith("0/1 "))   # no item endpoint inferred

    def test_rubric_scope_flags_item_endpoint_without_verify(self):
        text = section5(block("ORD-001", "Required", "Read one order", None, expected="HTTP 200 from GET /v1/orders/$ORDER_ID."))
        r = score(text)
        self.assertLess(r.score("scope_accuracy"), 5)
        self.assertIn("ORD-001: GET /v1/orders/{}", r.dimensions["scope_accuracy"].evidence)



class CurlTargetGateTests(unittest.TestCase):
    """Live C01 (prompt v4) passed the gate with Required scenarios calling `curl "$BASE_URL/v1/orders/$ORDER_ID"`:
    the gate read only `METHOD /path` text, never the method/path of a curl request. Every curl target in a
    scenario block is now matched as a full method/path pair; (verify) does not make an endpoint exist."""

    @classmethod
    def setUpClass(cls):
        cls.body = reference_fallback_prompt(Templates.load(ROOT / "prompts"), repository="synthetic-org/bench-repo",
                                             pr_number=C01.pr_number, test_level="standard", context=CTX)
        cls.ev = pb.Evidence.from_context(CTX)

    def gate_with(self, request: str, label: str = "Required", context=None) -> dict:
        blk = (f"**PROBE-001 ({label}) — Probe request (verify)**\n- Requirement/Risk: R1 (verify).\n"
               f"- Request: `{request}`\n- Expected: per contract (verify).\n\n")
        return validate_generated_prompt(self.body.replace("## 6. ", blk + "## 6. ", 1), pr_title=C01.title, context=context or CTX)

    def curl_findings(self, gate: dict) -> tuple[list[str], list[str]]:
        return ([e for e in gate["errors"] if "curl request" in e], [w for w in gate["warnings"] if "curl request" in w])

    def assertAccepted(self, request: str):
        gate = self.gate_with(request)
        self.assertEqual(self.curl_findings(gate), ([], []), request)
        self.assertTrue(gate["valid"], gate["errors"])

    def assertRejected(self, request: str, shown: str):
        gate = self.gate_with(request)
        errs, _ = self.curl_findings(gate)
        self.assertFalse(gate["valid"], request)
        self.assertTrue(any(shown in e for e in errs), (request, gate["errors"]))
        rub = qa_rubric.score_prompt(gate["text"], pr_title=C01.title, context=CTX, gate=gate)
        self.assertTrue(any(h.startswith("HF-SCOPE-VIOLATION") for h in rub.hard_fails), rub.hard_fails)

    def test_documented_targets_are_accepted(self):
        for req in (f'curl -sS {AUTH} "$BASE_URL/v1/orders" -o o.json',                                         # 1
                    f"curl -sS {AUTH} -d '{{\"reason\":\"r\"}}' \"$BASE_URL/v1/orders/$ORDER_ID/cancel\"",          # 2
                    f"curl -sS {AUTH} -d '{{\"reason\":\"r\"}}' \"$BASE_URL/v1/orders/nonexistent/cancel\""):       # 3 literal ID
            with self.subTest(req=req):
                self.assertAccepted(req)
        # 4: a literal `{orderId}` template is in endpoint scope, but curl globbing rewrites it, so it is not executable
        template = f"curl -sS -X POST {AUTH} -d '{{}}' \"$BASE_URL/v1/orders/{{orderId}}/cancel\""
        errors = self.gate_with(template)["errors"]
        self.assertFalse([e for e in errors if "matches no operation" in e or "named only" in e], errors)
        self.assertTrue([e for e in errors if "literal path template" in e], errors)

    def test_item_endpoint_is_rejected_even_with_verify(self):                                                    # 4, 5
        self.assertRejected(f'curl -sS {AUTH} "$BASE_URL/v1/orders/$ORDER_ID" -o o.json', "GET /v1/orders/$ORDER_ID")
        self.assertRejected(f'curl -sS {AUTH} "$BASE_URL/v1/orders/{{orderId}}"', "GET /v1/orders/{orderId}")

    def test_unevidenced_operation_and_method_are_rejected(self):                                                 # 6, 7
        self.assertRejected(f"curl -sS {AUTH} -d '{{}}' \"$BASE_URL/v1/orders/$ORDER_ID/refund\"", "POST /v1/orders/$ORDER_ID/refund")
        self.assertRejected(f'curl -sS {AUTH} -X DELETE "$BASE_URL/v1/orders/$ORDER_ID/cancel"', "DELETE /v1/orders/$ORDER_ID/cancel")

    def test_trailing_period_after_url_is_not_part_of_the_route(self):                                            # 8
        self.assertAccepted(f"curl -sS {AUTH} $BASE_URL/v1/orders.")

    def test_collection_does_not_authorize_extra_segments(self):                                                   # 9
        self.assertRejected(f'curl -sS {AUTH} "$BASE_URL/v1/orders/archive"', "GET /v1/orders/archive")
        self.assertRejected(f"curl -sS {AUTH} -d '{{}}' \"$BASE_URL/v1/orders/$ORDER_ID/cancel/now\"", "/cancel/now")

    def test_query_string_is_not_part_of_the_route(self):                                                          # 10
        self.assertAccepted(f'curl -sS {AUTH} "$BASE_URL/v1/orders?status=paid" -o o.json')
        self.assertRejected(f'curl -sS {AUTH} "$BASE_URL/v1/orders/$ORDER_ID?expand=items"', "GET /v1/orders/$ORDER_ID")

    def test_method_follows_curl_semantics_not_the_endpoint_name(self):                                            # 11
        self.assertRejected(f'curl -sS {AUTH} "$BASE_URL/v1/orders/$ORDER_ID/cancel"', "GET /v1/orders/$ORDER_ID/cancel")    # no body: GET
        self.assertRejected(f"curl -sS {AUTH} -d '{{}}' \"$BASE_URL/v1/orders\"", "POST /v1/orders")                          # body: POST
        self.assertAccepted(f'curl -sS {AUTH} -G -d status=paid "$BASE_URL/v1/orders"')                                      # -G: GET
        self.assertRejected(f'curl -sS {AUTH} -X "$METHOD" "$BASE_URL/v1/orders/$ORDER_ID/cancel"', "<undetermined method>")
        self.assertEqual(pb.curl_operations('curl -i "$BASE_URL/v1/orders"'), [("GET", "/v1/orders")])          # -i is not -I
        self.assertEqual(pb.curl_operations('curl -I "$BASE_URL/v1/orders"'), [("HEAD", "/v1/orders")])
        self.assertEqual(pb.curl_operations('curl -T o.json "$BASE_URL/v1/x"'), [("PUT", "/v1/x")])

    def test_gate_and_rubric_agree_on_every_target(self):                                                         # 12
        probes = {f'curl -sS {AUTH} "$BASE_URL/v1/orders"': True,
                  f"curl -sS {AUTH} -d '{{}}' \"$BASE_URL/v1/orders/$ORDER_ID/cancel\"": True,
                  f"curl -sS {AUTH} -d '{{}}' \"$BASE_URL/v1/orders/nonexistent/cancel\"": True,
                  f'curl -sS {AUTH} "$BASE_URL/v1/orders/$ORDER_ID"': False,
                  f'curl -sS {AUTH} -X DELETE "$BASE_URL/v1/orders/$ORDER_ID/cancel"': False}
        self.assertIs(qa_rubric.curl_operations, pb.curl_operations)
        for req, ok in probes.items():
            with self.subTest(req=req):
                (m, path), = pb.curl_operations(req)
                self.assertEqual(qa_rubric._known_operation(m, path, self.ev.paths, self.ev.operations), ok)
                self.assertEqual(not self.curl_findings(self.gate_with(req))[0], ok)
                credit = score(section5(block("ORD-001", "Required", "Probe", req))).dimensions["executability"].evidence[0]
                self.assertTrue(credit.startswith("1/1 " if ok else "0/1 "), credit)

    def test_every_curl_on_a_line_is_checked(self):
        self.assertRejected(f'curl -sS {AUTH} "$BASE_URL/v1/orders" && curl -sS {AUTH} "$BASE_URL/v1/orders/$ORDER_ID"',
                            "GET /v1/orders/$ORDER_ID")

    def test_conditional_label_does_not_admit_an_invented_endpoint(self):
        # GET /v1/orders/{id} is in neither the evidence nor the description: Conditional does not make it exist
        gate = self.gate_with(f'curl -sS {AUTH} "$BASE_URL/v1/orders/$ORDER_ID"', label="Conditional")
        self.assertFalse(gate["valid"])
        self.assertTrue(any("Conditional scenario" in e and "GET /v1/orders/$ORDER_ID" in e for e in gate["errors"]), gate["errors"])

    def test_description_only_operation_needs_verify_in_required(self):
        ctx = copy.deepcopy(CTX)
        ctx["description"]["text"] += "\n- Order history is available at `GET /v1/orders/{orderId}/history`."
        req = f'curl -sS {AUTH} "$BASE_URL/v1/orders/$ORDER_ID/history"'
        with_verify = self.gate_with(req, context=ctx)
        self.assertEqual(self.curl_findings(with_verify)[0], [])
        self.assertTrue(any("named only by the PR description" in w for w in self.curl_findings(with_verify)[1]))
        blk = (f"**PROBE-002 (Required) — History**\n- Requirement/Risk: R1.\n- Request: `{req}`\n- Expected: HTTP 200.\n\n")
        no_verify = validate_generated_prompt(self.body.replace("## 6. ", blk + "## 6. ", 1), pr_title=C01.title, context=ctx)
        self.assertTrue(any("named only by the PR description" in e for e in self.curl_findings(no_verify)[0]), no_verify["errors"])

    def test_discovery_curl_outside_scenarios_is_not_an_operation_claim(self):
        extra = self.body.replace("## 5. ", 'Discovery: `curl -sS "$BASE_URL/openapi.json" -o spec.json`\n\n## 5. ', 1)
        gate = validate_generated_prompt(extra, pr_title=C01.title, context=CTX)
        self.assertEqual(self.curl_findings(gate), ([], []))



class HostAndOperationConsistencyTests(unittest.TestCase):
    """(A) A curl target with a concrete or non-$BASE_URL host used to escape endpoint validation entirely.
    (B) Plain `METHOD /path` text required an exact path, so a literal ID in a documented `{param}` route was
    rejected while the same curl request was accepted. Both forms now use Evidence.operation_mismatch."""

    @classmethod
    def setUpClass(cls):
        cls.body = reference_fallback_prompt(Templates.load(ROOT / "prompts"), repository="synthetic-org/bench-repo",
                                             pr_number=C01.pr_number, test_level="standard", context=CTX)
        cls.ev = pb.Evidence.from_context(CTX)

    def gate_block(self, request: str, label: str = "Required") -> dict:
        blk = (f"**PROBE-001 ({label}) — Probe request (verify)**\n- Requirement/Risk: R1 (verify).\n"
               f"- Request: `{request}`\n- Expected: per contract (verify).\n\n")
        return validate_generated_prompt(self.body.replace("## 6. ", blk + "## 6. ", 1), pr_title=C01.title, context=CTX)

    def gate_text(self, op: str) -> dict:
        head = "## 2. Verified PR Summary and Scope\n"
        return validate_generated_prompt(self.body.replace(head, head + f"\n- OpenAPI documents {op} [FACT: openapi]\n", 1),
                                         pr_title=C01.title, context=CTX)

    @staticmethod
    def found(gate: dict, needle: str) -> list[str]:
        return [e for e in gate["errors"] if needle in e]

    def test_hard_coded_hosts_cannot_evade_validation(self):
        for target, host in (("https://api.example.com", "https://api.example.com"), ("http://localhost:8000", "http://localhost:8000"),
                             ("$API_URL", "$API_URL"), ("localhost:8000", "localhost:8000")):
            with self.subTest(host=host):
                ok_route = self.gate_block(f'curl -sS {AUTH} "{target}/v1/orders"')        # documented route, wrong host
                self.assertFalse(ok_route["valid"])
                self.assertTrue(self.found(ok_route, f"host '{host}'"), ok_route["errors"])
                self.assertFalse(self.found(ok_route, "curl request GET /v1/orders "), "a documented route is not a different operation")
                bad_route = self.gate_block(f'curl -sS {AUTH} "{target}/v1/orders/123"')    # undocumented route, wrong host
                self.assertTrue(self.found(bad_route, "curl request GET /v1/orders/123 matches no operation"), bad_route["errors"])
                self.assertTrue(self.found(bad_route, f"host '{host}'"))

    def test_hard_coded_host_in_conditional_is_warned(self):
        gate = self.gate_block(f'curl -sS {AUTH} "https://api.example.com/v1/orders"', label="Conditional")
        self.assertFalse(self.found(gate, "host"))
        self.assertTrue(any("https://api.example.com" in w for w in gate["warnings"]), gate["warnings"])

    def test_discovery_with_absolute_host_outside_scenarios_keeps_previous_handling(self):
        extra = self.body.replace("## 5. ", 'Discovery: `curl -sS "https://api.example.com/openapi.json" -o spec.json`\n\n## 5. ', 1)
        gate = validate_generated_prompt(extra, pr_title=C01.title, context=CTX)
        self.assertFalse([e for e in gate["errors"] if "host" in e or "curl request" in e], gate["errors"])

    def test_option_values_are_not_mistaken_for_targets(self):
        self.assertEqual([(r["method"], r["path"], r["base"]) for r in
                          pb.curl_requests('curl -sS -o "$OUT/x.json" -H "X-Trace: $TRACE/1" "$BASE_URL/v1/orders"')],
                         [("GET", "/v1/orders", "BASE_URL")])
        self.assertEqual(pb.curl_operations('curl --url "$BASE_URL/v1/orders" -sS'), [("GET", "/v1/orders")])
        self.assertEqual(pb.curl_operations('curl -sSo out.json "https://api.example.com/v1/orders"'), [])   # host != $BASE_URL

    def test_text_and_curl_reach_the_same_decision(self):
        probes = {("GET", "/v1/orders"): True, ("POST", "/v1/orders/123/cancel"): True,
                  ("POST", "/v1/orders/{orderId}/cancel"): True, ("GET", "/v1/orders/123"): False,
                  ("POST", "/v1/orders/123/refund"): False, ("DELETE", "/v1/orders/123/cancel"): False,
                  ("GET", "/v1/orders/123/cancel"): False, ("GET", "/v1/orders/archive"): False}
        for (method, path), ok in probes.items():
            with self.subTest(op=f"{method} {path}"):
                self.assertEqual(self.ev.operation_mismatch(method, path) is None, ok)
                text = self.gate_text(f"{method} {path}")
                self.assertEqual(text["valid"], ok, text["errors"])
                flag = "-X DELETE " if method == "DELETE" else ("-d '{}' " if method == "POST" else "")
                curl = self.gate_block(f'curl -sS {AUTH} {flag}"$BASE_URL{path}"')
                self.assertEqual(not self.found(curl, "curl request"), ok, curl["errors"])
                credit = score(section5(block("ORD-001", "Required", "Probe", f'curl -sS {AUTH} {flag}"$BASE_URL{path}"')))
                self.assertTrue(credit.dimensions["executability"].evidence[0].startswith("1/1 " if ok else "0/1 "))

    def test_trailing_period_in_prose_keeps_route_identity(self):
        self.assertTrue(self.gate_text("POST /v1/orders/123/cancel.")["valid"])
        self.assertIsNone(self.ev.operation_mismatch("POST", "/v1/orders/{orderId}/cancel."))

    def test_most_specific_route_wins(self):
        ev = pb.Evidence.from_context({"api_spec": {"known_operations": ["GET /v1/orders/{id}", "POST /v1/orders/export"]}})
        self.assertEqual(ev.operation_mismatch("GET", "/v1/orders/export"), "method")   # literal route, not the {id} route
        self.assertIsNone(ev.operation_mismatch("POST", "/v1/orders/export"))
        self.assertIsNone(ev.operation_mismatch("GET", "/v1/orders/42"))

    def test_rubric_scope_uses_the_same_matcher(self):
        self.assertIs(qa_rubric._known_operation, pb.known_operation)
        literal = section5(block("ORD-001", "Required", "Cancel one", None, expected="HTTP 200 from POST /v1/orders/123/cancel."))
        self.assertEqual(score(literal).score("scope_accuracy"), 5)
        child = section5(block("ORD-001", "Required", "Archive", None, expected="HTTP 200 from GET /v1/orders/archive."))
        self.assertLess(score(child).score("scope_accuracy"), 5)


if __name__ == "__main__":
    unittest.main()
