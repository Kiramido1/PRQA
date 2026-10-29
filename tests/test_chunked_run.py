"""worker/sandbox/chunked_run.py — per-scenario-group OpenCode sessions under the provider's per-request token limit.

Offline: splitting, chunk sizes, aggregation into a schema-valid qa-result and secret scrubbing are pure functions;
the session loop is exercised with a fake `opencode` executable on PATH (no network, no model).

Run: cd "<project>" && PYTHONPATH=worker python3 -m unittest tests.test_chunked_run -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from prqa_worker.prompt_builder import parse_scenarios  # noqa: E402
from prqa_worker.validation import SchemaStore, validate_result  # noqa: E402

spec = importlib.util.spec_from_file_location("chunked_run", ROOT / "worker" / "sandbox" / "chunked_run.py")
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)  # type: ignore[union-attr]

SHA = "fe17f7d3a2cd86af61bf52e3c7e73a1364607411"
JOB = {"job_id": "20261027-abcdef123456", "repository": "zedny-labs/Fluxera", "pr": {"number": 7, "title": "t", "head_sha": SHA}}
PROMPT = """# Senior QA Engineer Prompt — Standard API Testing for PR: x

## 1. Role and Objective

You are a QA engineer.

## 2. Verified PR Summary and Scope

The PR adds working days.

## 3. Requirements and Evidence

Long requirements text that is not sent to sessions.

## 4. Environment and API Discovery

Use $BASE_URL.

## 5. Prioritized Test Scenarios

### ORDERS-LIST — `GET /api/v1/orders`

**ORDERS-LIST-001 (Required) — happy path**
- Expected: 200.

**ORDERS-LIST-002 (Conditional) — bad query**
- Expected: 422.

**ORDERS-LIST-003 (Required) — paging**
- Expected: 200.

**ORDERS-LIST-004 (Required) — sorting**
- Expected: 200.

### Stated requirements — derived from the PR description

**STATED-001 (Required) — default week**
- Expected: Mon–Fri.

## 6. Authorization and Security Checks

**AUTH-001 (Required) — unauthenticated**
- Expected: 401.

## 7. Regression Coverage

**REG-001 (Regression) — old shapes**
- Expected: unchanged.

## 8. Execution and Evidence Rules

### 8.1 Strict testing constraints (mandatory)

Never use production.

### 8.2 Evidence recording

Save files.

## 14. Test Identities (disposable environment — appended by PR QA Pilot)

Create identities through the documented self-registration operation `POST /api/v1/auth/register` (synthetic).

## 15. Machine-Readable Output (appended by PR QA Pilot — mandatory)

Write qa-result.json.
"""


class SplitTests(unittest.TestCase):
    def test_every_planned_scenario_is_in_exactly_one_group_and_matches_the_gate_parser(self):
        sp = C.split_prompt(PROMPT)
        self.assertEqual(sp["planned"], [b["id"] for b in parse_scenarios(PROMPT)])
        self.assertEqual(len(sp["planned"]), len(set(sp["planned"])))
        self.assertTrue(all(len(g["ids"]) <= C.MAX_GROUP_SCENARIOS for g in sp["groups"]))
        self.assertEqual([g["ids"] for g in sp["groups"]][:2], [["ORDERS-LIST-001", "ORDERS-LIST-002", "ORDERS-LIST-003"], ["ORDERS-LIST-004"]])

    def test_header_has_role_environment_constraints_and_identities_but_not_the_full_report_contract(self):
        sp = C.split_prompt(PROMPT)
        for part in ("## 1. Role", "## 4. Environment", "### 8.1 Strict", "## 14. Test Identities"):
            self.assertIn(part, sp["header"])
        for part in ("## 3. Requirements", "## 15. Machine-Readable", "### 8.2"):
            self.assertNotIn(part, sp["header"])

    def test_session_text_is_verbatim_prompt_content_plus_the_record_contract(self):
        sp = C.split_prompt(PROMPT)
        g = sp["groups"][0]
        text = C.chunk_prompt(sp, g, token_ready=True)
        self.assertIn(g["text"], text)
        self.assertIn("prqa-record --id <ID>", text)
        self.assertIn("ORDERS-LIST-001, ORDERS-LIST-002, ORDERS-LIST-003", text)
        self.assertNotIn("STATED-001", text)
        self.assertNotIn("## 2. Verified PR Summary", text)                   # context only for STATED/REG groups
        stated = next(x for x in sp["groups"] if x["ids"] == ["STATED-001"])
        self.assertIn("## 2. Verified PR Summary", C.chunk_prompt(sp, stated, token_ready=False))

    def test_real_pr7_prompt_sessions_stay_far_below_the_provider_request_limit(self):
        text = (ROOT / "fixtures" / "recorded" / "fluxera-pr-7-qa-prompt.md").read_text()
        sp = C.split_prompt(text)
        self.assertEqual(sp["planned"], [b["id"] for b in parse_scenarios(text)])
        for g in sp["groups"]:
            # slim agent overhead (system prompt + bash tool schema) measured at 5,937 bytes; 4.5 bytes/token; 1,500 out
            est = (len(C.chunk_prompt(sp, g, token_ready=True)) + 5937) / 4.5 + 1500
            self.assertLess(est, 6000, g["name"])


class AggregateTests(unittest.TestCase):
    def agg(self, records: dict, outcomes=None, secrets_=()):
        sp = C.split_prompt(PROMPT)
        with tempfile.TemporaryDirectory() as d:
            for sid, rec in records.items():
                (Path(d) / f"{sid}.json").write_text(rec if isinstance(rec, str) else json.dumps(rec))
            return C.aggregate(split=sp, tc_dir=Path(d), job=JOB, outcomes=outcomes or {}, secrets_=list(secrets_),
                               model="prqa/openai/gpt-oss-120b", prompt_sha256="a" * 64, started="2026-10-27T18:00:00Z", finished="2026-10-27T18:30:00Z",
                               duration_ms=5, blockers=[])

    def assert_valid(self, res):
        check = validate_result(res, SchemaStore(ROOT / "schemas"))
        self.assertTrue(check["valid"], check["errors"])

    def test_missing_records_are_blocked_with_the_session_reason_and_the_result_validates(self):
        res = self.agg({"ORDERS-LIST-001": {"status": "PASS", "scenario": "happy", "endpoint": "GET /api/v1/orders", "expected": "200",
                                            "actual": "200", "http_status": 200}},
                       outcomes={"g01": "exit 1; provider budget (tokens per minute)"})
        self.assert_valid(res)
        self.assertEqual(res["summary"]["total"], 7)
        blocked = [c for c in res["test_cases"] if c["status"] == "BLOCKED"]
        self.assertEqual(len(blocked), 6)
        self.assertIn("provider budget", next(c for c in blocked if c["id"] == "ORDERS-LIST-002")["actual"])
        self.assertEqual(res["summary"]["overall_status"], "INCONCLUSIVE")

    def test_failures_become_defects_and_drive_the_verdict(self):
        recs = {i: {"status": "PASS", "scenario": i, "endpoint": "GET /x", "expected": "e", "actual": "a", "http_status": 200}
                for i in ("ORDERS-LIST-001", "ORDERS-LIST-002", "ORDERS-LIST-003", "ORDERS-LIST-004", "STATED-001", "AUTH-001", "REG-001")}
        recs["AUTH-001"] = {"status": "fail", "scenario": "unauth", "endpoint": "GET /x", "expected": "401", "actual": "200", "http_status": 200,
                            "defect": {"title": "no auth", "severity": "P0", "origin": "introduced_by_pr", "repro_curl": "curl $BASE_URL/x"}}
        res = self.agg(recs)
        self.assert_valid(res)
        self.assertEqual(res["summary"]["overall_status"], "FAIL")
        self.assertEqual(res["defects"][0]["severity"], "P0")
        self.assertEqual(next(c for c in res["test_cases"] if c["id"] == "AUTH-001")["defect_ref"], "DEF-001")

    def test_all_pass_is_pass_and_some_blocked_is_pass_with_known_issues(self):
        ids = ("ORDERS-LIST-001", "ORDERS-LIST-002", "ORDERS-LIST-003", "ORDERS-LIST-004", "STATED-001", "AUTH-001", "REG-001")
        ok = {i: {"status": "PASS", "endpoint": "GET /x", "expected": "e", "actual": "a"} for i in ids}
        self.assertEqual(self.agg(ok)["summary"]["overall_status"], "PASS")
        some = dict(ok, **{"REG-001": {"status": "BLOCKED", "actual": "role unavailable"}})
        res = self.agg(some)
        self.assert_valid(res)
        self.assertEqual(res["summary"]["overall_status"], "PASS_WITH_KNOWN_ISSUES")

    def test_garbage_and_invalid_records_never_break_the_result(self):
        res = self.agg({"ORDERS-LIST-001": "{not json", "ORDERS-LIST-002": {"status": "MAYBE"}, "AUTH-001": ["x"],
                        "REG-001": {"status": "PASS", "http_status": True, "defect": "x", "actual": "y" * 99999}})
        self.assert_valid(res)
        self.assertEqual(next(c for c in res["test_cases"] if c["id"] == "ORDERS-LIST-002")["status"], "BLOCKED")
        self.assertLessEqual(len(next(c for c in res["test_cases"] if c["id"] == "REG-001")["actual"]), C.FIELD_MAX)

    def test_tokens_and_passwords_are_scrubbed(self):
        tok = "eyJhbGciOiJIUzI1NiJ9.synthetic.signature0123456789"
        res = self.agg({"AUTH-001": {"status": "PASS", "actual": f"used Bearer {tok} and password Prqa-secretvalue",
                                     "evidence": f'{{"access_token": "{tok}"}}'}}, secrets_=[tok, "Prqa-secretvalue"])
        dumped = json.dumps(res)
        self.assertNotIn(tok, dumped)
        self.assertNotIn("Prqa-secretvalue", dumped)
        self.assertIn("Bearer [REDACTED]", C.scrub("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", []))
        self.assertIn("Bearer $TOKEN", C.scrub("Authorization: Bearer $TOKEN", []))


class RegistrationTests(unittest.TestCase):
    SPEC = {"paths": {"/api/v1/auth/register": {"post": {"requestBody": {"content": {"application/json": {
        "schema": {"$ref": "#/components/schemas/UserRegister"}}}}}}},
        "components": {"schemas": {"UserRegister": {"required": ["email", "password", "full_name", "organization_name"]}}}}

    def test_body_follows_the_documented_schema(self):
        b = C.register_body(self.SPEC, "/api/v1/auth/register", tag="abc", password="Prqa-x")
        self.assertEqual(set(b), {"email", "password", "full_name", "organization_name"})
        self.assertTrue(b["email"].endswith("@prqa-synthetic.dev"))
        self.assertEqual(b["password"], "Prqa-x")
        self.assertEqual(C.register_body({}, "/x", tag="t", password="p"), {"email": "prqa-t@prqa-synthetic.dev", "password": "p"})

    def test_token_is_found_wherever_the_api_puts_it(self):
        for doc in ({"access_token": "a"}, {"token": {"access_token": "a"}}, {"data": {"user": {}, "access_token": "a"}}, [{"access_token": "a"}]):
            self.assertEqual(C.find_token(doc), "a")
        self.assertEqual(C.find_token({"data": None}), "")


class SessionLoopTests(unittest.TestCase):
    """The real loop with a fake `opencode` that writes records for the IDs named in its prompt."""

    def test_one_session_per_group_and_a_valid_result(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            bin_ = d / "bin"
            bin_.mkdir()
            fake = bin_ / "opencode"
            fake.write_text("#!/usr/bin/env python3\n"
                            "import json, os, re, sys\n"
                            "text = sys.argv[-1]\n"
                            "open(os.environ['PRQA_EVIDENCE_DIR'] + '/calls.log', 'a').write(' '.join(sys.argv[1:-1]) + '\\n')\n"
                            "ids = re.search(r'Execute ONLY these scenarios: ([^.]+)\\.', text).group(1).split(', ')\n"
                            "for i in ids:\n"
                            "    json.dump({'status': 'PASS', 'scenario': i, 'endpoint': 'GET /x', 'expected': 'e', 'actual': 'a token=' + os.environ.get('TOKEN', '')},\n"
                            "              open(os.environ['PRQA_EVIDENCE_DIR'] + '/tc/' + i + '.json', 'w'))\n")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            (d / "prompt.md").write_text(PROMPT)
            ev = d / "ev"
            ev.mkdir()
            env = {"PATH": f"{bin_}:/usr/bin:/bin", "PRQA_EVIDENCE_DIR": str(ev), "PRQA_JOB_JSON": json.dumps(JOB), "BASE_URL": ""}
            old = dict(os.environ)
            os.environ.clear()
            os.environ.update(env)
            try:
                rc = C.main(["--prompt", str(d / "prompt.md"), "--repo-dir", str(d), "--evidence", str(ev), "--model", "prqa/openai/gpt-oss-120b",
                             "--timeout-s", "3600", "--prompt-sha256", "b" * 64])
            finally:
                os.environ.clear()
                os.environ.update(old)
            self.assertEqual(rc, 0)
            calls = (ev / "calls.log").read_text().splitlines()
            self.assertEqual(len(calls), len(C.split_prompt(PROMPT)["groups"]))
            self.assertTrue(all("--agent prqa" in c and "--title prqa g" in c for c in calls))
            res = json.loads((ev / "qa-result.json").read_text())
            check = validate_result(res, SchemaStore(ROOT / "schemas"))
            self.assertTrue(check["valid"], check["errors"])
            self.assertEqual((res["summary"]["overall_status"], res["summary"]["passed"]), ("PASS", 7))
            self.assertEqual(res["pr"]["number"], 7)
            self.assertIn("not pre-registered", " ".join(res["blockers"]))      # no BASE_URL → sessions register themselves


class EntrypointConfigTests(unittest.TestCase):
    def test_entrypoint_runs_the_chunked_executor_with_the_slim_agent(self):
        text = (ROOT / "worker" / "sandbox" / "entrypoint.sh").read_text()
        self.assertIn("/usr/local/bin/prqa-chunked-run", text)
        self.assertIn('"limit": {"context": 6500, "output": 1500}', text)
        self.assertIn('"tools": {"bash": True}', text)
        self.assertNotIn('opencode run --pure --format json --dir "$REPO_DIR"', text)
        docker = (ROOT / "worker" / "sandbox" / "Dockerfile.opencode").read_text()
        self.assertIn("COPY chunked_run.py /usr/local/bin/prqa-chunked-run", docker)


if __name__ == "__main__":
    unittest.main()


class PreregisterTests(unittest.TestCase):
    """Loopback fake API: the first register attempts fail, the reason is kept as a status code only."""

    def serve(self, codes):
        import http.server
        import threading
        answers = list(codes)

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(404); self.end_headers()

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                code = answers.pop(0) if answers else 201
                body = json.dumps({"success": True, "data": {"access_token": "tok-" + "x" * 30}} if code == 201 else
                                  {"detail": "SECRET-ish body"}).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(body)

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def test_retries_then_succeeds(self):
        with mock.patch.dict(os.environ, {"BASE_URL": self.serve([429, 500])}):
            tok, scrub, why = C.preregister("/api/v1/auth/register", pause_s=0)
        self.assertTrue(tok.startswith("tok-")); self.assertEqual(why, ""); self.assertIn(tok, scrub)

    def test_failure_reason_is_the_status_only(self):
        with mock.patch.dict(os.environ, {"BASE_URL": self.serve([429, 429, 429])}):
            tok, scrub, why = C.preregister("/api/v1/auth/register", pause_s=0)
        self.assertEqual((tok, why), ("", "register answered HTTP 429")); self.assertNotIn("SECRET", why)

    def test_no_base_url(self):
        with mock.patch.dict(os.environ, {"BASE_URL": ""}):
            self.assertEqual(C.preregister("/x")[2], "BASE_URL not set")


class CauseTests(unittest.TestCase):
    def test_spent_quota_is_named(self):
        self.assertIn("quota exhausted", C.cause("Error: prqa-model-proxy: provider_quota_exhausted (upstream 429, retry after 86293s)"))
        self.assertEqual(C.cause("APIError 429 Too Many Requests"), "rate limited (429)")
