"""Deterministic fallback for a schema-only API change (the shape of zedny-labs/Fluxera PR #7, 2026-10-27).

The shipped gate rejected the fallback for that PR (HF-UNUSABLE: "unsupported database assertion"). Root causes,
each pinned here with a compact SYNTHETIC fixture of the same shape (no private repository content):
  D1  the untrusted description's "Migration … adds the column …" was echoed verbatim (quote, S-requirement, SREG
      scenario) although the diff holds no SQL → masked; only the API-visible effect is tested;
  D2  the author's test-plan checkboxes (test files, tsc, alembic round-trip, "still pass") became API requirements;
  D3  a migration revision id in backticks was treated as a request field;
  D4  a change made through request/response *models* (no route change) produced no changed operation → operations
      are now derived from changed model classes in the diff that the OpenAPI document uses;
  D5  several operations under one resource shared one scenario-ID prefix (duplicate IDs) and got identical
      "nonexistent identifier" scenarios for the same path;
  D7  an operation reached only through a model that inherits the added field named that model on a [FACT] line
      although the diff never names it (the gate rejected it as an unsupported identifier) → described, not named;
  D6  a validation statement with a stated default ("omitted = the default week") only expected a rejection.
The gate, the rubric, the 85 threshold and every hard-fail rule are unchanged; the accepted prompt below is scored by
the shipped code.

Run: cd "<project>" && PYTHONPATH=worker python3 -m unittest tests.test_fallback_schema_pr_fixes -v
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from prqa_worker import qa_rubric  # noqa: E402
from prqa_worker.prompt_builder import (  # noqa: E402
    DB_ASSERTION_RE, Templates, extract_stated_requirements, parse_scenarios, reference_fallback_prompt, validate_generated_prompt,
)
from prqa_worker.validation import _schema_changed_operations, build_context  # noqa: E402

TITLE = "feat(team): choose the team's meeting days when creating it (SYNTHETIC)"
DESCRIPTION = """## Summary

Creating a team now asks which days it meets. The choice is stored on the team and returned on every read.

### Backend
- `teams.meeting_days` — JSON list of lowercase day codes (`sun` … `sat`) in week order.
- `POST /teams` accepts `meeting_days`; omitted = the default week. Input is validated and normalised (lowercase, week order).
- `PATCH /teams/{id}` accepts `meeting_days` so the owner can change it later.
- Migration `b1c2d3e4f5a6` adds the column and backfills existing teams.

## Test plan
- [x] `backend/test_team_meeting_days.py` — create with days, default week, rejected input, update
- [x] `backend/test_team_api.py` still pass
- [x] Migration upgrade → `alembic check` → downgrade on a stamped SQLite DB
- [x] `tsc --noEmit` and `src/services/api.test.ts`
"""
DIFF = """diff --git a/backend/app/schemas/team.py b/backend/app/schemas/team.py
--- a/backend/app/schemas/team.py
+++ b/backend/app/schemas/team.py
@@ -40,6 +40,8 @@ class TeamCreate(BaseModel):
     name: str = Field(min_length=1, max_length=120)
+    # The days this team meets; omitted = the default week.
+    meeting_days: list[str] | None = Field(default=None, max_length=7)
@@ -50,6 +52,7 @@ class TeamUpdate(BaseModel):
     name: str | None = Field(default=None, max_length=120)
+    meeting_days: list[str] | None = Field(default=None, max_length=7)
@@ -60,6 +63,7 @@ class TeamUpdate(BaseModel):
 class TeamRead(BaseModel):
     id: str
+    meeting_days: list[str] = DEFAULT_MEETING_DAYS
diff --git a/backend/migrations/versions/b1c2d3e4f5a6_team_meeting_days.py b/backend/migrations/versions/b1c2d3e4f5a6_team_meeting_days.py
new file mode 100644
--- /dev/null
+++ b/backend/migrations/versions/b1c2d3e4f5a6_team_meeting_days.py
@@ -0,0 +1,4 @@
+def upgrade() -> None:
+    with op.batch_alter_table("teams") as batch:
+        batch.add_column(sa.Column("meeting_days", sa.JSON(), nullable=True))
"""


def ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def body(schema):
    return {"content": {"application/json": {"schema": schema}}}


OPENAPI = {
    "openapi": "3.1.0", "info": {"title": "Synthetic Teams API", "version": "1"},
    "paths": {
        "/api/v1/teams": {
            "get": {"responses": {"200": {"description": "Successful Response", **body({"type": "array", "items": ref("TeamRead")})}}},
            "post": {"requestBody": body(ref("TeamCreate")),
                     "responses": {"201": {"description": "Successful Response", **body(ref("TeamDetail"))}, "422": {"description": "Validation Error"}}}},
        "/api/v1/teams/{team_id}": {
            "get": {"responses": {"200": {"description": "Successful Response", **body(ref("TeamDetail"))}, "422": {"description": "Validation Error"}}},
            "patch": {"requestBody": body(ref("TeamUpdate")),
                      "responses": {"200": {"description": "Successful Response", **body(ref("TeamDetail"))}, "422": {"description": "Validation Error"}}}},
        "/api/v1/projects": {"get": {"responses": {"200": {"description": "Successful Response", **body({"type": "array", "items": ref("ProjectRead")})}}}},
    },
    "components": {"schemas": {
        "TeamCreate": {"properties": {"name": {}, "meeting_days": {}}},
        "TeamUpdate": {"properties": {"name": {}, "meeting_days": {}}},
        "TeamRead": {"properties": {"id": {}, "meeting_days": {}}},
        "TeamDetail": {"properties": {"id": {}, "meeting_days": {}, "members": {}}},   # inherits the field (no diff line)
        "ProjectRead": {"properties": {"id": {}, "name": {}}},
    }},
}
PR = {"number": 7, "title": TITLE, "body": DESCRIPTION, "state": "open", "draft": False, "user": {"login": "dev"},
      "head": {"sha": "a" * 40, "ref": "feat/x", "repo": {"fork": False}}, "base": {"ref": "Dev", "sha": "b" * 40},
      "html_url": "https://github.com/synthetic-org/teams/pull/7"}
FILES = [{"filename": "backend/app/schemas/team.py", "status": "modified", "additions": 4, "deletions": 0},
         {"filename": "backend/migrations/versions/b1c2d3e4f5a6_team_meeting_days.py", "status": "added", "additions": 3, "deletions": 0}]
OPS = ["GET /api/v1/teams", "POST /api/v1/teams", "GET /api/v1/teams/{team_id}", "PATCH /api/v1/teams/{team_id}"]


class SchemaPrFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = build_context(pr=PR, files=FILES, diff_text=DIFF, openapi=OPENAPI, repository="synthetic-org/teams", synthetic=True)
        cls.t = Templates.load(ROOT / "prompts")
        cls.text = reference_fallback_prompt(cls.t, repository="synthetic-org/teams", pr_number=7, test_level="standard", context=cls.ctx)
        cls.gate = validate_generated_prompt(cls.text, pr_title=TITLE, context=cls.ctx)
        cls.rub = qa_rubric.score_prompt(cls.text, pr_title=TITLE, context=cls.ctx, gate=cls.gate)
        cls.acc = qa_rubric.accept_result(cls.rub, cls.gate)

    def test_d4_operations_derived_from_changed_models(self):
        via = _schema_changed_operations(DIFF, OPENAPI)
        self.assertEqual(sorted(via), sorted(OPS))                      # ProjectRead's operation is not touched
        self.assertEqual(via["GET /api/v1/teams/{team_id}"], ["TeamDetail"])  # inherited field, documented by OpenAPI
        self.assertEqual(sorted(self.ctx["api_spec"]["changed_operations"]), sorted(OPS))
        self.assertIn("changed_operations_via_schemas", self.ctx["api_spec"])
        for op in OPS:
            self.assertIn(f"`{op}` [FACT: diff + openapi", self.text)
        self.assertEqual(_schema_changed_operations(DIFF, None), {})      # no document → nothing derived
        self.assertEqual(_schema_changed_operations(DIFF.replace("class Team", "class Other"), OPENAPI), {})

    def test_d4_no_key_when_nothing_is_derived(self):
        ctx = build_context(pr=PR, files=FILES, diff_text="", openapi=OPENAPI, repository="synthetic-org/teams", synthetic=True)
        self.assertNotIn("changed_operations_via_schemas", ctx["api_spec"])

    def test_d1_database_claims_without_sql_are_masked(self):
        self.assertFalse(DB_ASSERTION_RE.search(self.text), "an untestable column/table claim is still asserted")
        self.assertIn("[schema change claimed by the PR — no SQL in the diff; test only its API-visible effect]", self.text)
        self.assertNotIn("unsupported database assertion", " ".join(self.gate["errors"]))
        # with real SQL evidence the claim is kept verbatim
        ctx = build_context(pr=PR, files=FILES, diff_text=DIFF + "+ALTER TABLE teams ADD COLUMN meeting_days JSON;\n",
                            openapi=OPENAPI, repository="synthetic-org/teams", synthetic=True)
        kept = reference_fallback_prompt(self.t, repository="synthetic-org/teams", pr_number=7, test_level="standard", context=ctx)
        self.assertIn("adds the column", kept)

    def test_d2_test_plan_checkboxes_are_not_requirements(self):
        texts = [r["text"] for r in extract_stated_requirements(DESCRIPTION, TITLE)]
        self.assertFalse([t for t in texts if re.search(r"tsc|alembic|still pass|test_team", t)], texts)
        self.assertEqual(len(texts), 5, texts)   # 2 summary sentences + 3 behaviour bullets; no test-plan item
        # a checkbox describing behaviour (not a test run) is still a requirement
        more = extract_stated_requirements("- [ ] The API must reject meeting days outside sun..sat", TITLE)
        self.assertEqual(len(more), 1)

    def test_d3_revision_ids_and_file_names_are_not_fields(self):
        self.assertNotIn("field(s) `b1c2d3e4f5a6`", self.text)
        self.assertIn("field(s) `meeting_days`", self.text)

    def test_d7_inherited_model_is_not_named_as_a_diff_fact(self):
        line = next(l for l in self.text.splitlines() if l.startswith("- `GET /api/v1/teams/{team_id}`"))
        self.assertNotIn("TeamDetail", line)
        self.assertIn("documents a field added in the diff", line)
        self.assertFalse([e for e in self.gate["errors"] if "TeamDetail" in e], self.gate["errors"])

    def test_d5_unique_ids_and_one_identifier_case_per_path(self):
        ids = [b["id"] for b in parse_scenarios(self.text)]
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertEqual(sum(1 for b in parse_scenarios(self.text) if "Nonexistent or malformed identifier" in b["body"]), 1)
        self.assertIn("one case per changed method on this path (GET, PATCH)", self.text)
        self.assertFalse(any("Invalid and missing inputs" in b["body"] and "`GET " in b["body"] for b in parse_scenarios(self.text)))

    def test_d6_stated_default_expects_acceptance_and_rejection(self):
        block = next(b["body"] for b in parse_scenarios(self.text) if b["id"].startswith("STATED-") and "default week" in b["body"])
        self.assertIn("including any stated default when the input is omitted", block)
        self.assertIn("validation response documented by the contract", block)

    def test_grounded_prompt_passes_the_unchanged_gate(self):
        self.assertEqual(qa_rubric.THRESHOLD, 85.0)
        self.assertEqual(self.gate["errors"], [], self.gate["errors"])
        self.assertEqual(self.rub.hard_fails, [])
        self.assertTrue(self.acc["accepted"], self.acc)
        self.assertGreaterEqual(self.rub.quality_score, 85.0)
        for required in ("curl", "## 10. Final QA Report", "Do not modify application source code", "AUTH-001", "REG-001"):
            self.assertIn(required, self.text)

    def test_invented_operation_is_still_rejected(self):
        bad = self.text.replace("**AUTH-001 (Required)", "**TEAM-EXTRA-001 (Required) — Archive a team\n- Request: `POST /api/v1/teams/{team_id}/archive` [FACT: diff]\n- Expected: 200.\n\n**AUTH-001 (Required)", 1)
        gate = validate_generated_prompt(bad, pr_title=TITLE, context=self.ctx)
        self.assertFalse(gate["valid"])
        self.assertFalse(qa_rubric.accept_result(qa_rubric.score_prompt(bad, pr_title=TITLE, context=self.ctx, gate=gate), gate)["accepted"])

    def test_unmasked_database_claim_is_still_rejected(self):
        bad = self.text.replace("## 11. Blockers", "The migration adds the column meeting_days to teams.\n\n## 11. Blockers", 1)
        gate = validate_generated_prompt(bad, pr_title=TITLE, context=self.ctx)
        self.assertIn("unsupported database assertion: column/table/index claim without SQL evidence in the diff", gate["errors"])


if __name__ == "__main__":
    unittest.main()
