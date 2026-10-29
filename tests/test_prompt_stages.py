"""worker/prqa_worker/prompt_stages.py — staged Groq QA prompt generation (scope → coverage → sections).

Offline only: no provider, no model proxy, no OpenCode. Stage outputs are canned model answers; the shared budget
ledger is a real proxy/provider_budget.Ledger in a temporary directory; the final gate is the real worker gate.

Run: cd "<project>" && PYTHONPATH=worker python3 -m unittest tests.test_prompt_stages -v
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "proxy"))
sys.path.insert(0, str(ROOT))

import provider_budget as pbud  # noqa: E402
from prqa_worker import prompt_builder as pb  # noqa: E402
from prqa_worker import prompt_stages as ps  # noqa: E402
from prqa_worker import qa_rubric  # noqa: E402
from prqa_worker.validation import build_context  # noqa: E402

REPO = "acme/utils-api"
SHA = "ab" * 20
SHA2 = "cd" * 20
DIFF = """diff --git a/backend/app/api/v1/utils.py b/backend/app/api/v1/utils.py
new file mode 100644
--- /dev/null
+++ b/backend/app/api/v1/utils.py
@@ -0,0 +1,22 @@
+router = APIRouter()
+_request_log: list[float] = []
+
+class CalcRequest(BaseModel):
+    a: float
+    b: float
+    op: str
+
+@router.get("/ping")
+async def ping() -> Any:
+    _request_log.append(time.time())
+    return {"status": "ok", "requests_served": len(_request_log)}
+
+@router.post("/calc")
+async def calculate(body: CalcRequest) -> Any:
+    if body.op == "add":
+        result = body.a + body.b
+    elif body.op == "sub":
+        result = body.b - body.a
+    return {"result": result}
diff --git a/backend/app/api/v1/router.py b/backend/app/api/v1/router.py
--- a/backend/app/api/v1/router.py
+++ b/backend/app/api/v1/router.py
@@ -20,3 +20,4 @@
 api_router.include_router(users_router, prefix="/users", tags=["Users"])
+api_router.include_router(utils_router, prefix="/utils", tags=["Utilities"])
"""
BODY = """Adds a **Utilities** router mounted at `/api/v1/utils`.

| Method | Path | Input | Expected behavior |
|---|---|---|---|
| `GET` | `/utils/ping` | none | Returns `{"status": "ok"}` plus a counter of requests served |
| `POST` | `/utils/calc` | `{"a": float, "b": float, "op": "add" \\| "sub"}` | Returns `a <op> b`; invalid input should produce a clear 4xx error, never a 500 |
"""


def openapi(extra_ops: int = 60) -> dict:
    paths = {
        "/api/v1/utils/ping": {"get": {"responses": {"200": {"description": "Successful Response"}}}},
        "/api/v1/utils/calc": {"post": {"requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/CalcRequest"}}}},
                                        "responses": {"200": {"description": "Successful Response"}, "422": {"description": "Validation Error"}}}},
    }
    for i in range(extra_ops):           # an API much larger than the change: the single-request context grows with it
        paths[f"/api/v1/reports/area{i}/items/{{item_id}}"] = {"get": {"parameters": [{"name": "item_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                                                                       "security": [{"bearerAuth": []}],
                                                                       "responses": {"200": {"description": "Successful Response"}, "422": {"description": "Validation Error"}}}}
    return {"openapi": "3.1.0", "info": {"title": "Utils API", "version": "1"}, "paths": paths,
            "components": {"schemas": {"CalcRequest": {"type": "object", "required": ["a", "b", "op"], "properties": {
                "a": {"type": "number"}, "b": {"type": "number"}, "op": {"type": "string"}}}}}}


def make_context(*, sha: str = SHA, diff: str = DIFF, body: str = BODY, extra_ops: int = 60) -> dict:
    pr = {"number": 5, "title": "feat(utils): add stateless utility endpoints", "body": body, "head": {"sha": sha, "ref": "feat/utils"},
          "base": {"ref": "main", "sha": "ef" * 20}, "html_url": f"https://github.com/{REPO}/pull/5", "user": {"login": "dev"}, "labels": []}
    files = [{"filename": "backend/app/api/v1/utils.py", "status": "added", "additions": 20, "deletions": 0},
             {"filename": "backend/app/api/v1/router.py", "status": "modified", "additions": 1, "deletions": 0}]
    return build_context(pr=pr, files=files, diff_text=diff, openapi=openapi(extra_ops), repository=REPO, synthetic=True)


PING, CALC = "GET /api/v1/utils/ping", "POST /api/v1/utils/calc"


def scope_answer() -> dict:
    return {"summary": "Adds a utilities router with a ping and a calc endpoint.",
            "changed_behavior": [
                {"text": "New file defines `ping` and `calculate` handlers", "source": "diff", "ref": "backend/app/api/v1/utils.py"},
                {"text": "`calculate` computes `body.b - body.a` for sub", "source": "diff", "ref": "backend/app/api/v1/utils.py"}],
            "requirements": [{"id": "S1", "header": "Utilities router", "claims": [{"text": "Adds a Utilities router mounted at /api/v1/utils", "source": "description", "ref": "S1"}]},
                             {"id": "S2", "header": "Ping endpoint", "claims": [{"text": "Returns status ok plus a counter of requests served", "source": "description", "ref": "S2"}]},
                             {"id": "S3", "header": "Calc endpoint", "claims": [{"text": "Returns a op b; invalid input should produce a clear 4xx error, never a 500", "source": "description", "ref": "S3"}]}],
            "operations": [{"op": PING, "source": "diff", "ref": "backend/app/api/v1/utils.py"}, {"op": CALC, "source": "diff", "ref": "backend/app/api/v1/utils.py"}],
            "assumptions": [{"text": "Whether the counter is shared between worker processes", "source": "unverified", "ref": "S2"}],
            "risks": [{"id": "RISK-1", "text": "Division by zero may raise an unhandled error", "ref": CALC}],
            "out_of_scope": ["Authentication is not changed"]}


def case(cid, label, title, op, links, request="", expected="per contract (verify)", **kw):
    return {"id": cid, "label": label, "title": title, "op": op, "links": links, "request": request, "expected": expected,
            "evidence": kw.get("evidence", "description"), "preconditions": kw.get("pre", ""), "blocked_reason": kw.get("br", "")}


def coverage_answer() -> dict:
    return {"test_cases": [
        case("PING-001", "Required", "Ping returns status and counter", PING, ["S2"], 'curl -sS "$BASE_URL/api/v1/utils/ping"', "HTTP 200 with status ok and requests_served"),
        case("PING-002", "Conditional", "Counter increases on consecutive calls", PING, ["S2"], 'for i in 1 2 3; do curl -sS "$BASE_URL/api/v1/utils/ping"; done', "requests_served increases (verify)"),
        case("CALC-001", "Required", "Add and sub return a op b", CALC, ["S3"],
             'for op in add sub; do curl -sS -X POST "$BASE_URL/api/v1/utils/calc" -H "Content-Type: application/json" -d "{\\"a\\": 10, \\"b\\": 4, \\"op\\": \\"$op\\"}"; done',
             "14 for add and 6 for sub"),
        case("CALC-002", "Required", "Unsupported op and division by zero", CALC, ["S3", "RISK-1"], "", "a clear 4xx error, never a 500"),
        case("REG-001", "Regression", "Router registration keeps ping reachable", PING, ["S1"], 'curl -sS "$BASE_URL/api/v1/utils/ping"', "ping still answers"),
        case("UTIL-001", "Blocked", "Counter after a worker restart", PING, ["S2"], "", "not observable", br="restarting the API is not allowed")],
        "uncovered": []}


def sections_answer() -> dict:
    return {"execution_notes": ["Run PING-001 before PING-002 so the counter is observed from a known start."],
            "evidence_notes": ["For CALC-002 save the status and body of each invalid input."],
            "severity_guidance": [{"ref": "CALC-002", "severity": "P2", "rationale": "an unhandled server error on invalid input"}],
            "regression_analysis": ["Both utilities operations are new; a failure there is a defect of this PR."],
            "blockers": ["UTIL-001 stays BLOCKED: the API must not be restarted."],
            "report_notes": ["State for ping and calc whether the stated behaviour was confirmed."]}


def ok(obj) -> dict:
    return {"generated_text": json.dumps(obj), "finish_reason": "stop"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = Path(self.tmp.name) / "run"
        self.run_dir.mkdir()
        self.context = make_context()
        self.calls: list[int] = []

    def pipe(self, *, context=None, sha=SHA, cfg=None, capacity="none", run_dir=None):
        cap = None if capacity == "none" else capacity
        return ps.Pipeline(run_dir or self.run_dir, prompts_dir=ROOT / "prompts", context=context or self.context, openapi=openapi(),
                           repository=REPO, pr_number=5, head_sha=sha, test_level="standard", model="openai/gpt-oss-120b",
                           provider="groq", cfg=cfg or ps.StageConfig(), capacity=cap)

    def run_all(self, p, answers=None):
        act = p.start()
        for ans in answers or (scope_answer(), coverage_answer(), sections_answer()):
            self.assertEqual(act["action"], "call", act)
            act = p.record(ok(ans), {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
        return act

    def ledger(self, **limits):
        d = Path(self.tmp.name) / "budget"
        d.mkdir(mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        vals = {"PRQA_BUDGET_DAILY_REQUESTS": "1000", "PRQA_BUDGET_DAILY_TOKENS": "1000000", "PRQA_BUDGET_JOB_TOKENS": "180000",
                "PRQA_BUDGET_TOKENS_PER_MINUTE": "8000", "PRQA_BUDGET_MAX_IN_FLIGHT": "1"}
        vals.update({f"PRQA_BUDGET_{k.upper()}": str(v) for k, v in limits.items()})
        self.clock = [1_000_000.0]
        return pbud.Ledger(pbud.BudgetConfig.from_env(vals, directory=str(d)), clock=lambda: self.clock[0])


class PartitionTests(Base):
    def test_single_request_is_oversized_but_every_stage_fits_the_target(self):
        single = pb.build_llm_request(pb.Templates.load(ROOT / "prompts"), model="openai/gpt-oss-120b", provider="groq", max_tokens=3000,
                                      repository=REPO, pr_number=5, test_level="standard", synthetic=False, context=self.context)
        cfg = ps.StageConfig()
        self.assertGreater(ps.estimate_request(single, cfg)["total_tokens"], 8000)      # what the provider refused
        p = self.pipe()
        act = p.start()
        seen = []
        for ans in (scope_answer(), coverage_answer(), sections_answer()):
            seen.append((act["stage"], act["estimate"]["total_tokens"], act["llm_request"]["max_completion_tokens"]))
            self.assertLessEqual(act["estimate"]["total_tokens"], cfg.target_tokens)
            act = p.record(ok(ans))
        self.assertEqual([s for s, _, _ in seen], ["scope", "coverage", "sections"])
        self.assertEqual([c for _, _, c in seen], [cfg.completion["scope"], cfg.completion["coverage"], cfg.completion["sections"]])

    def test_large_diff_is_trimmed_to_the_target_and_the_truncation_recorded(self):
        big = DIFF + "".join(f"+    value_{i} = compute_something_long_{i}(argument_{i}, another_{i})\n" for i in range(1500))
        p = self.pipe(context=make_context(diff=big))
        act = p.start()
        self.assertLessEqual(act["estimate"]["total_tokens"], ps.StageConfig().target_tokens)
        self.assertTrue(p.meta()["stages"]["scope"]["diff_truncated"])
        self.assertTrue(json.loads(act["llm_request"]["messages"][1]["content"])["diff_truncated"])

    def test_later_stages_do_not_resend_the_original_context(self):
        p = self.pipe()
        act = p.start()
        first = act["llm_request"]["messages"][1]["content"]
        self.assertIn("requests_served", first)                       # stage 1 sees the diff
        act = p.record(ok(scope_answer()))
        second = act["llm_request"]["messages"][1]["content"]
        act = p.record(ok(coverage_answer()))
        third = act["llm_request"]["messages"][1]["content"]
        for later in (second, third):
            self.assertNotIn("_request_log.append", later)            # no diff text
            self.assertNotIn("Expected behavior |", later)            # no raw description table
            self.assertNotIn("/api/v1/reports/area", later)           # no unrelated operations
        self.assertIn('"contracts"', second)
        self.assertNotIn('"contracts"', third)                        # stage 3 gets the case index only
        self.assertLess(len(third), len(first))

    def test_relevant_operations_are_filtered_locally(self):
        cands = ps.candidate_operations(self.context)
        self.assertEqual(sorted(cands), sorted([PING, CALC]))          # 60 unrelated operations left out
        ex = ps.contract_excerpt(CALC, self.context, openapi())
        self.assertEqual(ex["request_body"]["schema"]["required"], ["a", "b", "op"])
        self.assertEqual(sorted(ex["responses"]), ["200", "422"])
        p = self.pipe()
        p.start()
        act = p.record(ok(scope_answer()))
        payload = json.loads(act["llm_request"]["messages"][1]["content"])
        self.assertEqual([c["op"] for c in payload["contracts"]], [PING, CALC])

    def test_operation_count_beyond_the_coverage_capacity_is_reported_not_trimmed(self):
        p = self.pipe(cfg=ps.StageConfig(max_operations=1))
        act = p.start()
        self.assertEqual(act["action"], "failed")
        self.assertIn("CONTEXT_LIMIT", act["reason"])

    def test_a_stage_over_the_target_is_never_sent(self):
        cfg = ps.StageConfig(target_tokens=2500, completion={"scope": 2400, "coverage": 2400, "sections": 1400, "scope_coverage": 2400})
        act = self.pipe(cfg=cfg).start()
        self.assertEqual(act["action"], "failed")
        self.assertIn("CONTEXT_LIMIT", act["reason"])


class ValidationTests(Base):
    def test_stage_outputs_are_schema_checked(self):
        scope, _, errors = ps.validate_scope({"operations": []}, self.context, ps.candidate_operations(self.context))
        self.assertIsNone(scope)
        self.assertTrue(errors)
        self.assertIsNone(ps.validate_coverage({"cases": []}, self.context, ps.validate_scope(scope_answer(), self.context, [PING, CALC])[0])[0])
        self.assertIsNone(ps.validate_sections("nope", self.context, {}, {})[0])
        self.assertIsNone(ps.parse_json_object("no json here"))
        self.assertEqual(ps.parse_json_object('```json\n{"a": 1}\n```'), {"a": 1})

    def test_unsupported_facts_are_downgraded_never_upgraded(self):
        ans = scope_answer()
        ans["changed_behavior"] = [
            {"text": "`calculate` rejects `divisor` values of 0", "source": "diff", "ref": "backend/app/api/v1/utils.py"},   # not in the diff
            {"text": "The calc endpoint returns 422 for an unknown op", "source": "diff", "ref": "backend/app/api/v1/utils.py"},  # a code is never a diff fact
            {"text": "`ping` appends to `_request_log`", "source": "unverified", "ref": "x"}]                                  # stays unverified
        scope, notes, _ = ps.validate_scope(ans, self.context, [PING, CALC])
        self.assertEqual([c["source"] for c in scope["changed_behavior"]], ["unverified", "unverified", "unverified"])
        self.assertTrue(any("downgraded" in n for n in notes))

    def test_operations_outside_the_openapi_document_are_dropped_everywhere(self):
        ans = scope_answer()
        ans["operations"].append({"op": "DELETE /api/v1/utils/everything", "source": "description", "ref": "S1"})
        ans["assumptions"].append({"text": "DELETE /api/v1/utils/everything may exist", "source": "unverified", "ref": "S1"})
        scope, notes, _ = ps.validate_scope(ans, self.context, [PING, CALC])
        self.assertEqual([o["op"] for o in scope["operations"]], [PING, CALC])
        self.assertNotIn("everything", json.dumps(scope))
        self.assertTrue(notes)

    def test_one_statement_is_never_both_a_fact_and_an_assumption(self):
        ans = scope_answer()
        ans["assumptions"].append({"text": "New file defines `ping` and `calculate` handlers", "source": "unverified", "ref": "x"})
        scope, _, _ = ps.validate_scope(ans, self.context, [PING, CALC])
        texts = [c["text"] for c in scope["changed_behavior"] + scope["assumptions"]]
        self.assertEqual(len(texts), len(set(texts)))

    def test_every_stated_requirement_survives_stage_one(self):
        ans = scope_answer()
        ans["requirements"] = ans["requirements"][:1] + [{"id": "S9", "header": "invented", "claims": []}]
        scope, notes, _ = ps.validate_scope(ans, self.context, [PING, CALC])
        self.assertEqual([r["id"] for r in scope["requirements"]], ["S1", "S2", "S3"])
        self.assertTrue(any("S2 missing" in n for n in notes))

    def test_duplicate_test_cases_are_detected(self):
        scope = ps.validate_scope(scope_answer(), self.context, [PING, CALC])[0]
        cov = coverage_answer()
        cov["test_cases"].append(copy.deepcopy(cov["test_cases"][0]))                              # same id
        dup = copy.deepcopy(cov["test_cases"][2])
        dup["id"] = "CALC-009"                                                                       # same case, new id
        cov["test_cases"].append(dup)
        out, notes, _ = ps.validate_coverage(cov, self.context, scope)
        ids = [c["id"] for c in out["test_cases"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn("CALC-009", ids)
        self.assertEqual(sum("duplicate" in n for n in notes), 2)

    def test_unsafe_requests_are_replaced_by_the_contract_instruction(self):
        scope = ps.validate_scope(scope_answer(), self.context, [PING, CALC])[0]
        cov = coverage_answer()
        cov["test_cases"][0]["request"] = 'curl -sS "https://prod.example.com/api/v1/utils/ping"'   # concrete host
        cov["test_cases"][2]["request"] = 'curl -sS -X POST "$BASE_URL/api/v1/utils/calc" -d \'{"a": ...}\''  # placeholder body
        out, notes, _ = ps.validate_coverage(cov, self.context, scope)
        self.assertIsNone(out["test_cases"][0]["request"])
        self.assertIsNone(out["test_cases"][2]["request"])
        self.assertEqual(sum("request replaced" in n for n in notes), 2)


class AssemblyTests(Base):
    def test_assembled_prompt_is_complete_and_passes_the_unchanged_gate(self):
        act = self.run_all(self.pipe())
        self.assertEqual(act["action"], "done")
        text = (self.run_dir / ps.ASSEMBLED).read_text()
        for h in pb.REQUIRED_HEADINGS:
            self.assertEqual(text.count(h), 1, h)
        self.assertIn(pb.FINAL_REQUIREMENT_MARKER, text)
        for cid in ("PING-001", "PING-002", "CALC-001", "CALC-002", "REG-001", "UTIL-001"):
            self.assertIn(f"**{cid} (", text)
        for sid in ("S1", "S2", "S3"):
            self.assertIn(f"**{sid}**", text)
        self.assertIn("8.3 PR-specific execution notes", text)
        v = pb.validate_generated_prompt(text, pr_title=self.context["pr"]["title"], context=self.context)
        q = qa_rubric.quality_decision(text, pr_title=self.context["pr"]["title"], context=self.context, gate=v)
        self.assertTrue(v["valid"], v["errors"])
        self.assertTrue(q["accepted"])
        self.assertGreaterEqual(q["score"], qa_rubric.THRESHOLD)
        self.assertEqual(qa_rubric.THRESHOLD, 85.0)                    # never lowered
        for name in ("stage1_scope.json", "stage2_api_coverage.json", "stage3_prompt_sections.json", "stage3_prompt_sections.md",
                     "generation-metadata.json"):
            self.assertTrue((self.run_dir / name).is_file(), name)
        meta = json.loads((self.run_dir / ps.METADATA).read_text())
        self.assertEqual(meta["assembled_sha256"], pb.sha256(text))
        self.assertEqual((meta["head_sha"], meta["model"], meta["generator_version"]), (SHA, "openai/gpt-oss-120b", ps.GENERATOR_VERSION))
        self.assertEqual({s: v["usage"]["total_tokens"] for s, v in meta["stages"].items()}, {"scope": 150, "coverage": 150, "sections": 150})

    def test_requirements_without_cases_are_filled_not_lost(self):
        cov = coverage_answer()
        cov["test_cases"] = [c for c in cov["test_cases"] if "S2" not in c["links"]]
        act = self.run_all(self.pipe(), (scope_answer(), cov, sections_answer()))
        self.assertEqual(act["action"], "done")
        text = (self.run_dir / ps.ASSEMBLED).read_text()
        self.assertIn("Stated requirements without a generated case", text)
        self.assertEqual(json.loads((self.run_dir / ps.METADATA).read_text())["unresolved"]["gaps"], ["S2"])
        cov_scn = pb.check_coverage(text, self.context, pb.parse_scenarios(text))
        self.assertNotIn("S2", cov_scn["missing"])

    def test_operations_without_cases_still_get_a_scenario(self):
        cov = coverage_answer()
        cov["test_cases"] = [c for c in cov["test_cases"] if c["op"] != PING] + [cov["test_cases"][-1] | {"op": CALC, "id": "CALC-009"}]
        self.run_all(self.pipe(), (scope_answer(), cov, sections_answer()))
        text = (self.run_dir / ps.ASSEMBLED).read_text()
        self.assertIn("**GAP-001 (Conditional) — Documented happy path of `GET /api/v1/utils/ping`**", text)
        v = pb.validate_generated_prompt(text, pr_title=self.context["pr"]["title"], context=self.context)
        self.assertTrue(v["valid"], v["errors"])

    def test_raw_model_output_is_kept_with_each_artifact(self):
        self.run_all(self.pipe())
        art = json.loads((self.run_dir / "stage3_prompt_sections.json").read_text())
        self.assertEqual(json.loads(art["raw_output"]), sections_answer())

    def test_no_case_is_duplicated_when_outputs_are_merged(self):
        self.run_all(self.pipe())
        text = (self.run_dir / ps.ASSEMBLED).read_text()
        ids = [s["id"] for s in pb.parse_scenarios(text)]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(pb.check_duplicates(pb.parse_scenarios(text))["errors"], [])

    def test_an_unsupported_fact_reaching_the_prompt_is_caught_by_the_gate(self):
        """The per-stage checks are not acceptance: the complete prompt is gated."""
        self.run_all(self.pipe())
        text = (self.run_dir / ps.ASSEMBLED).read_text().replace(
            "- Changed behaviour:", "- Changed behaviour:\n  - `calculate` enforces `max_value` of 1000 [FACT: diff]", 1)
        v = pb.validate_generated_prompt(text, pr_title=self.context["pr"]["title"], context=self.context)
        q = qa_rubric.quality_decision(text, pr_title=self.context["pr"]["title"], context=self.context, gate=v)
        self.assertFalse(q["accepted"])
        self.assertTrue(any(h.startswith(("HF-FABRICATED-FACT", "HF-UNUSABLE")) for h in q["hard_fails"]))


class TokenBudgetTests(Base):
    def test_stage_estimate_is_the_proxy_reservation(self):
        self.assertEqual(ps.ESTIMATE_SAFETY, pbud.ESTIMATE_SAFETY)
        cfg = ps.StageConfig()
        for content in ("x" * 9000, "é" * 3000, ""):
            req = {"model": "m", "messages": [{"role": "user", "content": content}], "max_completion_tokens": 2400}
            est = ps.estimate_request(req, cfg)
            self.assertEqual(est["total_tokens"], pbud.reservation_tokens(est["request_bytes"], cfg.bytes_per_token, 2400))
            self.assertEqual(est["completion_tokens"], 2400)                 # input AND the full completion allowance

    def test_observed_scope_request_reserves_at_least_its_actual_usage(self):
        # live 2026-10-28 (Amenly PR #2, scope stage, 1800-token completion cap): reserved 4187, actual 4715
        req = {"model": "m", "messages": [{"role": "user", "content": "d" * 10500}], "max_completion_tokens": 1800}
        est = ps.estimate_request(req, ps.StageConfig())
        self.assertLess(int(est["request_bytes"] / 4.5) + 1800, 4715)       # the unmargined estimate was too low
        self.assertGreaterEqual(est["total_tokens"], 4715)
        self.assertLessEqual(est["total_tokens"], pbud.max_request_tokens(8000))

    def test_capacity_is_read_only_and_matches_reserve(self):
        led = self.ledger()
        before = led.snapshot()
        cap = led.capacity(job_id="prompt-x", tokens=4000)
        self.assertTrue(cap["fits"])
        self.assertEqual(led.snapshot(), before)                      # nothing reserved, nothing written
        rid = led.reserve(source="model-proxy", job_id="prompt-x", tokens=6000, completion_allowance=1800)
        led.settle(rid, total_tokens=5000, outcome="ok")
        cap = led.capacity(job_id="prompt-x", tokens=4000)            # 5000 + 4000 > 8000 within the window
        self.assertFalse(cap["fits"])
        self.assertEqual(cap["kind"], "tpm_window")
        self.assertFalse(cap["hard"])
        self.assertGreater(cap["retry_after_s"], 0)
        with self.assertRaises(pbud.BudgetDenied):
            led.reserve(source="model-proxy", job_id="prompt-x", tokens=4000, completion_allowance=1800)
        self.clock[0] += 61                                          # the window rolls over
        self.assertTrue(led.capacity(job_id="prompt-x", tokens=4000)["fits"])

    def test_reconciled_usage_frees_window_capacity(self):
        led = self.ledger()
        rid = led.reserve(source="model-proxy", job_id="prompt-x", tokens=6000, completion_allowance=3000)
        self.assertFalse(led.capacity(job_id="prompt-x", tokens=4000)["fits"])
        led.settle(rid, total_tokens=2500, outcome="ok")             # provider-reported usage replaces the reservation
        self.assertTrue(led.capacity(job_id="prompt-x", tokens=4000)["fits"])

    def test_next_stage_waits_for_the_rolling_window(self):
        led = self.ledger()
        cap = lambda t: led.capacity(job_id="prompt-acme", tokens=t)
        p = self.pipe(capacity=cap)
        act = p.start()
        self.assertEqual(act["wait_s"], 0.0)
        rid = led.reserve(source="model-proxy", job_id="prompt-acme", tokens=act["estimate"]["total_tokens"], completion_allowance=1800)
        led.settle(rid, total_tokens=5000, outcome="ok")             # stage 1 as the provider reported it
        act = p.record(ok(scope_answer()))
        self.assertEqual(act["stage"], "coverage")
        self.assertGreater(act["wait_s"], 0)                          # aggregate window: stage 2 must wait
        self.assertLessEqual(act["wait_s"], 61)
        self.assertEqual(p.meta()["stages"]["coverage"]["capacity"]["kind"], "tpm_window")

    def test_provider_reported_remaining_tokens_defer_the_request(self):
        led = self.ledger()
        rid = led.reserve(source="model-proxy", job_id="other", tokens=500, completion_allowance=100)
        led.settle(rid, total_tokens=500, outcome="ok", remaining_tokens=1000, reset_s=12.5)
        self.clock[0] += 61
        act = self.pipe(capacity=lambda t: led.capacity(job_id="prompt-acme", tokens=t)).start()
        self.assertEqual(act["action"], "call")
        self.assertAlmostEqual(act["wait_s"], 0.5, delta=1)          # reset already past: remaining no longer binding
        led2 = self.ledger()
        rid = led2.reserve(source="model-proxy", job_id="other", tokens=500, completion_allowance=100)
        led2.settle(rid, total_tokens=500, outcome="ok", remaining_tokens=1000, reset_s=40)
        self.clock[0] += 10
        cap = led2.capacity(job_id="prompt-acme", tokens=4000)
        self.assertEqual(cap["kind"], "provider_remaining")
        self.assertAlmostEqual(cap["retry_after_s"], 30.5, delta=0.1)

    def test_daily_budget_exhaustion_stops_with_an_explicit_error(self):
        led = self.ledger(daily_tokens=1000)
        act = self.pipe(capacity=lambda t: led.capacity(job_id="prompt-acme", tokens=t)).start()
        self.assertEqual(act["action"], "failed")
        self.assertIn("BUDGET_daily_tokens", act["reason"])
        self.assertIn("not sent", act["reason"])

    def test_per_job_budget_is_distinguished_from_the_minute_window(self):
        led = self.ledger(job_tokens=5000)
        led.reserve(source="model-proxy", job_id="prompt-acme", tokens=4000, completion_allowance=1000)
        self.clock[0] += 120
        cap = led.capacity(job_id="prompt-acme", tokens=4000)
        self.assertEqual((cap["kind"], cap["hard"]), ("job_tokens", True))

    def test_target_above_the_tpm_limit_is_a_configuration_error(self):
        with self.assertRaises(ps.StageConfigError):
            ps.StageConfig(target_tokens=9000).check(tokens_per_minute=8000)
        with self.assertRaises(ps.StageConfigError):
            ps.StageConfig.from_env({"PRQA_PROMPT_STAGE_MODE": "5"})


class RetryAndResumeTests(Base):
    def test_rate_limit_is_retried_once_after_a_window_then_stops(self):
        p = self.pipe()
        p.start()
        act = p.record({"llm_error": "prompt LLM call failed", "http_status": "429"})
        self.assertEqual((act["action"], act["stage"], act["attempt"]), ("call", "scope", 2))
        self.assertGreaterEqual(act["wait_s"], ps.DEFAULT_RETRY_WAIT_S)
        act = p.record({"llm_error": "prompt LLM call failed", "http_status": "429"})
        self.assertEqual(act["action"], "failed")
        self.assertIn("rate_limited (HTTP 429)", act["reason"])
        meta = p.meta()
        self.assertEqual(meta["stages"]["scope"]["attempts"], 2)
        self.assertEqual(len(meta["stages"]["scope"]["retries"]), 2)

    def test_retry_sends_the_identical_request(self):
        p = self.pipe()
        first = p.start()["llm_request"]
        again = p.record({"llm_error": "x", "http_status": "503"})["llm_request"]
        self.assertEqual(first, again)                                # same body, same temperature

    def test_a_rejected_request_is_not_retried(self):
        p = self.pipe()
        p.start()
        act = p.record({"llm_error": "prompt LLM call failed", "http_status": "400"})
        self.assertEqual(act["action"], "failed")
        self.assertEqual(p.meta()["stages"]["scope"]["attempts"], 1)

    def test_invalid_output_gets_one_retry_and_is_kept_for_debugging(self):
        p = self.pipe()
        p.start()
        act = p.record({"generated_text": "I cannot do that", "finish_reason": "stop"})
        self.assertEqual((act["action"], act["attempt"]), ("call", 2))
        self.assertEqual(json.loads((self.run_dir / "stage1_scope.json").read_text())["status"], "invalid")
        act = p.record({"generated_text": "still not json", "finish_reason": "stop"})
        self.assertEqual(act["action"], "failed")

    def test_truncated_output_is_never_used(self):
        p = self.pipe(cfg=ps.StageConfig(max_retries=0))
        p.start()
        act = p.record({"generated_text": json.dumps(scope_answer()), "finish_reason": "length"})
        self.assertEqual(act["action"], "failed")
        self.assertIn("truncated", act["reason"])

    def test_completed_stages_are_not_repeated_when_a_later_stage_fails(self):
        p = self.pipe(cfg=ps.StageConfig(max_retries=0))
        p.start()
        p.record(ok(scope_answer()))
        act = p.record({"llm_error": "x", "http_status": "400"})
        self.assertEqual(act["action"], "failed")
        meta = p.meta()
        self.assertEqual((meta["stages"]["scope"]["status"], meta["stages"]["scope"]["attempts"]), ("valid", 1))
        act = self.pipe().start()                                     # a new attempt for the same head
        self.assertEqual(act["stage"], "coverage")                    # stage 1 resumed from its validated artifact
        self.assertTrue(self.pipe().meta()["stages"]["scope"]["cached"])

    def test_resume_after_restart_continues_with_the_open_stage(self):
        p = self.pipe()
        p.start()
        p.record(ok(scope_answer()))
        act = self.pipe().start()                                     # a new process: same metadata, same head
        self.assertEqual((act["action"], act["stage"]), ("call", "coverage"))

    def test_cache_is_never_reused_for_a_different_head(self):
        p = self.pipe()
        p.start()
        p.record(ok(scope_answer()))
        other = self.pipe(context=make_context(sha=SHA2), sha=SHA2)
        act = other.start()
        self.assertEqual(act["stage"], "scope")
        self.assertFalse(other.meta()["stages"]["scope"]["cached"])
        self.assertIsNone(other.assembled_text())

    def test_two_stage_mode_only_when_it_fits(self):
        small = make_context(extra_ops=0)
        p = self.pipe(context=small, cfg=ps.StageConfig(mode="2", target_tokens=6000))  # the safety margin needs ~5.7k here
        act = p.start()
        self.assertEqual((p.meta()["mode"], act["stage"]), ("2", "scope_coverage"))
        act = p.record(ok({"scope": scope_answer(), "coverage": coverage_answer()}))
        self.assertEqual(act["stage"], "sections")
        self.assertEqual(p.record(ok(sections_answer()))["action"], "done")
        tight = ps.StageConfig(mode="2", target_tokens=4500, completion={"scope": 1500, "coverage": 2500, "sections": 1400, "scope_coverage": 3000})
        q = self.pipe(context=small, cfg=tight, run_dir=Path(self.tmp.name))
        q.start()
        self.assertEqual(q.meta()["mode"], "3")
        self.assertIn("two-stage refused", q.meta()["mode_reason"])


if __name__ == "__main__":
    unittest.main()
