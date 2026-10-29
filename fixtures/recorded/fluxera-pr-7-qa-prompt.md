# Senior QA Engineer Prompt — Standard API Testing for PR: feat(org): choose the organization's working days when creating it (PR #7, repository zedny-labs/Fluxera)

## 1. Role and Objective

Act as a Senior QA Engineer specializing in Backend API Testing, REST API validation, Authorization and RBAC testing, Data Integrity, Negative Testing, Edge Cases, and Regression Testing.

Your task is to independently test the following PR against the actual running zedny-labs/Fluxera API:

**PR Title:** feat(org): choose the organization's working days when creating it

Perform standard, evidence-based testing using **curl exclusively for API requests**.

Your objective is to identify reproducible functional defects, authorization vulnerabilities, incorrect response data, validation gaps, and regressions within the exact PR scope.

Do not assume the PR is correct just because its implementation matches the description. Verify actual runtime behavior.

---

Test level for this run: **standard**. Scale depth accordingly: cover every stated requirement with at least one Required scenario, add the highest-value negative and boundary cases, and stay inside the output budget stated below. Never exceed the budget silently — mark overflow scenarios Blocked or Conditional with a reason in section 11.
Output budget: at most 21 scenarios in section 5 (3 stated requirements; every one needs at least one Required scenario). Cover every stated requirement; mark overflow scenarios Blocked or Conditional in section 11.

Note: this prompt was assembled deterministically from the PR context (no model analysis). Statements are tagged `[FACT: diff]`, `[FACT: openapi diff]`, `[FACT: diff + openapi]` (an operation whose request/response model changed in the diff), `[STATED: PR description]` or `[UNVERIFIED]`.

---

## 2. Verified PR Summary and Scope

- Repository: `zedny-labs/Fluxera` · PR #7 · head `fe17f7d3a2cd` [FACT: metadata]
- Changed files (11):
- `backend/app/api/v1/endpoints/organizations.py` (modified, +1/-0) [FACT: diff]
- `backend/app/models/org.py` (modified, +8/-0) [FACT: diff]
- `backend/app/schemas/org.py` (modified, +31/-0) [FACT: diff]
- `backend/app/services/org.py` (modified, +3/-0) [FACT: diff]
- `backend/migrations/versions/a7c3e9f1b2d4_organization_working_days.py` (added, +43/-0) [FACT: diff]
- `backend/test_org_working_days.py` (added, +38/-0) [FACT: diff]
- `src/components/onboarding/StepWorkingDays.tsx` (added, +85/-0) [FACT: diff]
- `src/components/onboarding/types.ts` (modified, +25/-0) [FACT: diff]
- `src/pages/Onboarding.tsx` (modified, +18/-2) [FACT: diff]
- `src/services/api.ts` (modified, +4/-0) [FACT: diff]
- `src/store/authStore.ts` (modified, +4/-1) [FACT: diff]
- Changed or added API operations:
- `GET /api/v1/organizations` [FACT: diff + openapi — changed model(s) `OrganizationRead` used by this operation]
- `POST /api/v1/organizations` [FACT: diff + openapi — changed model(s) `OrganizationCreate`, `OrganizationDetail` used by this operation]
- `GET /api/v1/organizations/{organization_id}` [FACT: diff + openapi — changed model(s) `OrganizationDetail` used by this operation]
- `PATCH /api/v1/organizations/{organization_id}` [FACT: diff + openapi — changed model(s) `OrganizationDetail`, `OrganizationUpdate` used by this operation]
- Status codes present in the diff/OpenAPI evidence: 200, 201, 204, 422 [FACT: diff]
- Diff truncated in the context package: no
- Non-API files changed (tests/migrations): `backend/migrations/versions/a7c3e9f1b2d4_organization_working_days.py` — not testable through the API; do not infer database or test-suite behaviour from their names [FACT: diff]

---

## 3. Requirements and Evidence

Stated intent (UNTRUSTED PR description — claims, not verified facts) [STATED: PR description]:

> ## Summary
> 
> Creating an organization now asks which days the team works. The onboarding flow gets a **Working days** step between the account step and the job title: seven toggles in week order plus Sun–Thu / Mon–Fri / Mon–Sat presets. The choice is stored on the organization and returned on every read, so attendance and overtime can count against the organization's real week instead of assuming Monday–Friday.
> 
> ### Backend
> - `organizations.working_days` — JSON list of lowercase day codes (`sun` … `sat`) in week order.
> - `POST /organizations` accepts `working_days`; omitted = the default week. Input is validated and normalised (lowercase, week order).
> - `PATCH /organizations/{id}` accepts `working_days` so the Super Admin can change it later.
> - `working_days` is included in every organization read (list, detail, `/me`).
> - Migration `a7c3e9f1b2d4` [schema change claimed by the PR — no SQL in the diff; test only its API-visible effect] and backfills existing organizations.
> 
> ### Frontend
> - New `StepWorkingDays` onboarding step and a `WEEKDAYS` / `WORKING_DAY_PRESETS` catalogue in the onboarding types.
> - `api.createOrganization` and the auth store's `createOrganization` take the chosen days.
> - `ApiOrganization` carries `working_days`.
> 
> ## Test plan
> - [x] `backend/test_org_working_days.py` — create with days, default week, rejected input, update
> - [x] `backend/test_org_api_hygiene.py`, `backend/test_org_join.py` still pass
> - [x] Migration upgrade → `alembic check` → downgrade on a stamped SQLite DB
> - [x] `tsc --noEmit` and `src/services/api.test.ts`
> - [ ] Manual: sign up → create organization → pick days → organization detail shows them
> 
> 🤖 Generated with [Claude Code](https://claude.com/claude-code)

Requirements derived from the evidence:

- **R1** — `GET /api/v1/organizations` changed in this PR; its documented request/response contract after the change must hold at runtime. [FACT: diff + openapi — changed model(s) `OrganizationRead` used by this operation]
- **R2** — `POST /api/v1/organizations` changed in this PR; its documented request/response contract after the change must hold at runtime. [FACT: diff + openapi — changed model(s) `OrganizationCreate`, `OrganizationDetail` used by this operation]
- **R3** — `GET /api/v1/organizations/{organization_id}` changed in this PR; its documented request/response contract after the change must hold at runtime. [FACT: diff + openapi — changed model(s) `OrganizationDetail` used by this operation]
- **R4** — `PATCH /api/v1/organizations/{organization_id}` changed in this PR; its documented request/response contract after the change must hold at runtime. [FACT: diff + openapi — changed model(s) `OrganizationDetail`, `OrganizationUpdate` used by this operation]

Stated requirements (derived from the description above — intent, not implementation facts; exact codes, schemas and rules must be discovered):

- **S1** — `POST /organizations` accepts `working_days`; omitted = the default week. Input is validated and normalised (lowercase, week order). [STATED: PR description]
- **S2** — `PATCH /organizations/{id}` accepts `working_days` so the Super Admin can change it later. [STATED: PR description]
- **S3** — Migration `a7c3e9f1b2d4` [schema change claimed by the PR — no SQL in the diff; test only its API-visible effect] and backfills existing organizations. [STATED: PR description]

Unverified / to confirm during discovery:

- Every expected status code and response shape not listed above (only 200, 201, 204, 422 appear in the evidence).
- Whether the description's claims correspond to runtime behaviour — verify, do not assume.
- Authentication/authorization requirements of the changed operations (from the live OpenAPI document).
- Availability of safe read-only data and disposable fixtures for any mutation.

---

## 4. Environment and API Discovery

Before testing, discover the real environment. Define a configurable base URL `$BASE_URL` and token `$TOKEN`; never invent credentials and never print secrets or dump `.env` files.

1. Locate the API: project documentation, `.env.example`, README, Swagger configuration, shell variables. Prefer an existing local API; verify which environment is actually available.
2. Retrieve the OpenAPI document with curl, e.g. `curl -sS "$BASE_URL/openapi.json" -o /tmp/pr_openapi.json`, and confirm for every operation in sections 5–7: actual path and method, required fields, request/response schemas, authentication, documented status codes, query parameters. If the document is unavailable, inspect the repository files and report that runtime schema verification was blocked.
3. Identify safe read-only records for the changed operations through GET requests only. Do not use guessed IDs as evidence that records exist.
4. For mutating operations, use only an explicitly authorized, isolated environment and disposable fixtures; otherwise mark those scenarios BLOCKED and record the exact request that would be needed.

---

## 5. Prioritized Test Scenarios

Priority order: critical acceptance criteria → direct positive/negative tests of the changed functionality → validation, boundaries, error handling → authorization/data isolation where relevant → regression → justified edge cases. Labels: **Required**, **Regression**, **Conditional**, **Blocked**. Group inputs that exercise one rule into a single parameterized case. Do not add scenarios about parallel requests, load, multi-tenancy or result ordering unless the evidence in sections 2–3 mentions them.

### ORGANI-LIST — `GET /api/v1/organizations`

**ORGANI-LIST-001 (Required) — Documented happy path of the changed operation**
- Requirement/Risk: R1 — the operation changed in this PR [FACT: diff + openapi — changed model(s) `OrganizationRead` used by this operation].
- Preconditions: contract for `GET /api/v1/organizations` confirmed in section 4; read-only data identified where needed.
- Request: build it from the discovered contract (parameters/body as documented), with `$TOKEN` if the contract requires authentication.
- Expected: the success status and body documented by the contract for `GET /api/v1/organizations` (verify); the business outcome claimed by the PR description must be confirmed in the response body, not assumed from the status alone. Check the documented model(s) `OrganizationRead` field by field.

### ORGANI-CREATE — `POST /api/v1/organizations`

**ORGANI-CREATE-001 (Required) — Documented happy path of the changed operation**
- Requirement/Risk: R2 — the operation changed in this PR [FACT: diff + openapi — changed model(s) `OrganizationCreate`, `OrganizationDetail` used by this operation].
- Preconditions: contract for `POST /api/v1/organizations` confirmed in section 4; read-only data identified where needed.
- Request: build it from the discovered contract (parameters/body as documented), with `$TOKEN` if the contract requires authentication.
- Expected: the success status and body documented by the contract for `POST /api/v1/organizations` (verify); the business outcome claimed by the PR description must be confirmed in the response body, not assumed from the status alone. Check the documented model(s) `OrganizationCreate`, `OrganizationDetail` field by field.

**ORGANI-CREATE-002 (Required) — Invalid and missing inputs for the changed parameters/fields**
- Requirement/Risk: R2 — validation of the inputs added or changed by this PR [FACT: diff].
- Request: one parameterized `POST /api/v1/organizations` case per changed input: missing, empty, wrong type, out-of-range/over-length value, unknown enum value (only inputs that the diff or contract shows). Check the documented model(s) `OrganizationCreate`, `OrganizationDetail` field by field.
- Expected: a documented validation response per contract (verify); never an unhandled 5xx; no data created or changed (confirm with a read-only request afterwards).

**ORGANI-CREATE-003 (Conditional) — Persisted state after a successful `POST /api/v1/organizations`** *(only if a disposable, authorized fixture exists; otherwise BLOCKED)*
- Requirement/Risk: the mutation of `POST /api/v1/organizations` [FACT: diff + openapi — changed model(s) `OrganizationCreate`, `OrganizationDetail` used by this operation].
- Request: the documented success payload for `POST /api/v1/organizations` against the disposable fixture.
- Expected: a subsequent read-only request reflects exactly the change the PR describes in the output of `POST /api/v1/organizations`; otherwise record the discrepancy as a finding with evidence.

### ORGANI-GET — `GET /api/v1/organizations/{organization_id}`

**ORGANI-GET-001 (Required) — Documented happy path of the changed operation**
- Requirement/Risk: R3 — the operation changed in this PR [FACT: diff + openapi — changed model(s) `OrganizationDetail` used by this operation].
- Preconditions: contract for `GET /api/v1/organizations/{organization_id}` confirmed in section 4; read-only data identified where needed.
- Request: build it from the discovered contract (parameters/body as documented), with `$TOKEN` if the contract requires authentication.
- Expected: the success status and body documented by the contract for `GET /api/v1/organizations/{organization_id}` (verify); the business outcome claimed by the PR description must be confirmed in the response body, not assumed from the status alone. Check the documented model(s) `OrganizationDetail` field by field.

**ORGANI-GET-002 (Conditional) — Nonexistent or malformed identifier in the path**
- Requirement/Risk: error handling of `/api/v1/organizations/{organization_id}` after the change [FACT: diff shows a path parameter]; one case per changed method on this path (GET, PATCH).
- Expected: the not-found/validation response documented by the contract (verify); no 5xx.

### ORGANI-UPDATE — `PATCH /api/v1/organizations/{organization_id}`

**ORGANI-UPDATE-001 (Required) — Documented happy path of the changed operation**
- Requirement/Risk: R4 — the operation changed in this PR [FACT: diff + openapi — changed model(s) `OrganizationDetail`, `OrganizationUpdate` used by this operation].
- Preconditions: contract for `PATCH /api/v1/organizations/{organization_id}` confirmed in section 4; read-only data identified where needed.
- Request: build it from the discovered contract (parameters/body as documented), with `$TOKEN` if the contract requires authentication.
- Expected: the success status and body documented by the contract for `PATCH /api/v1/organizations/{organization_id}` (verify); the business outcome claimed by the PR description must be confirmed in the response body, not assumed from the status alone. Check the documented model(s) `OrganizationDetail`, `OrganizationUpdate` field by field.

**ORGANI-UPDATE-002 (Required) — Invalid and missing inputs for the changed parameters/fields**
- Requirement/Risk: R4 — validation of the inputs added or changed by this PR [FACT: diff].
- Request: one parameterized `PATCH /api/v1/organizations/{organization_id}` case per changed input: missing, empty, wrong type, out-of-range/over-length value, unknown enum value (only inputs that the diff or contract shows). Check the documented model(s) `OrganizationDetail`, `OrganizationUpdate` field by field.
- Expected: a documented validation response per contract (verify); never an unhandled 5xx; no data created or changed (confirm with a read-only request afterwards).

**ORGANI-UPDATE-003 (Conditional) — Persisted state after a successful `PATCH /api/v1/organizations/{organization_id}`** *(only if a disposable, authorized fixture exists; otherwise BLOCKED)*
- Requirement/Risk: the mutation of `PATCH /api/v1/organizations/{organization_id}` [FACT: diff + openapi — changed model(s) `OrganizationDetail`, `OrganizationUpdate` used by this operation].
- Request: the documented success payload for `PATCH /api/v1/organizations/{organization_id}` against the disposable fixture.
- Expected: a subsequent read-only request reflects exactly the change the PR describes in the output of `PATCH /api/v1/organizations/{organization_id}`; otherwise record the discrepancy as a finding with evidence.

### Stated requirements — derived from the PR description (intent, each to be verified)

Every scenario below restates what the PR *claims*; none of it is evidence that the behaviour exists. Discover the contract first (section 4), then execute.

**STATED-001 (Required) — `POST /organizations` accepts `working_days`; omitted = the default week. Input is validated and normalised (lowercase, week order)**
- Requirement/Risk: S1 [STATED: PR description] — intent only; not evidence that it is implemented.
- Request: operation `POST /organizations` as named in the PR — confirm path, method and body shape in the contract first; field(s) `working_days` as named in the PR — confirm existence, type and rules in the contract before testing.
- Expected: valid input as stated is accepted with the success status and body documented by the contract (verify; confirm what the response returns, including any stated default when the input is omitted); invalid input gets the validation response documented by the contract (verify the exact code and error shape) with no state change (confirm with a read-only request).

**STATED-002 (Required) — `PATCH /organizations/{id}` accepts `working_days` so the Super Admin can change it later**
- Requirement/Risk: S2 [STATED: PR description] — intent only; not evidence that it is implemented.
- Request: operation `PATCH /organizations/{id}` as named in the PR — confirm path, method and body shape in the contract first; field(s) `working_days` as named in the PR — confirm existence, type and rules in the contract before testing.
- Expected: per the contract discovered in section 4 (verify); report as ambiguous where the contract is silent.

---

## 6. Authorization and Security Checks

**AUTH-001 (Required) — Unauthenticated request to each changed operation**
- Requirement/Risk: the change must not weaken access control on the touched endpoints.
- Expected: rejected as documented by the contract's security scheme (verify the exact code; if the operation is documented as public, mark Not Applicable).

**AUTH-002 (Required) — Malformed or expired token**
- Expected: rejected per contract (verify); no 5xx; no partial data in the response.

**AUTH-003 (Conditional) — Role-based access** *(only if the OpenAPI document or PR evidence defines roles for the changed operations; otherwise Not Applicable)*
- Expected: roles without permission are rejected per contract (verify); no privilege escalation introduced by this PR.

---

## 7. Regression Coverage

**REG-001 (Regression) — Existing request shapes of the 4 changed operations behave as before**
- Requirement/Risk: existing clients of `GET /api/v1/organizations`, `POST /api/v1/organizations`, `GET /api/v1/organizations/{organization_id}`, `PATCH /api/v1/organizations/{organization_id}` [FACT: diff + openapi].
- Request: one parameterized case per operation, using the documented pre-change request shape from section 4 (no new inputs).
- Expected: each still returns its documented response (verify); the new field, if any, is present with its documented default/null.

**SREG-001 (Regression) — Migration `a7c3e9f1b2d4` [schema change claimed by the PR — no SQL in the diff; test only its API-visible effect] and backfills existing organizations**
- Requirement/Risk: S3 [STATED: PR description] — intent only; not evidence that it is implemented.
- Request: the operation(s) identified in section 4 for this behaviour.
- Expected: the pre-change behaviour and response shape (verify against the contract and, where available, a pre-PR baseline).

Keep regression within the changed operations; do not expand into unrelated modules or full-system testing.

---

## 8. Execution and Evidence Rules

### 8.1 Strict testing constraints (mandatory)

These requirements are mandatory:

1. Use `curl` for all HTTP requests.
2. Use the existing API and its Swagger/OpenAPI specification to discover actual request schemas, response schemas, required fields, authentication requirements, and supported query parameters.
3. Use shell utilities such as `jq`, `awk`, `sed`, `grep`, `file`, `unzip`, and Python standard-library utilities only for local response inspection, assertions, and report-file verification. Do not use Python HTTP clients, Postman, browser automation, or other HTTP clients.
4. Do not use pytest or create a test automation framework.
5. Do not modify application source code, database schemas, migrations, environment configuration, permissions, or production data.
6. Do not restart services, install packages, or change the runtime environment unless explicitly authorized.
7. Do not assume that a particular endpoint, request body, status code, role, token, or response field exists. Discover and verify it first.
8. Never invent test results, expected values, successful responses, or database state.
9. Do not expose access tokens, passwords, cookies, or other secrets in the final report.
10. Never perform destructive testing against production or shared environments.
11. Do not create, update, delete, activate, or deactivate real business records without explicit authorization and a verified safe test environment.
12. If a prerequisite is unavailable, document the blocker and continue with all independent tests.
13. Do not fix any defect discovered. Report it with reproducible evidence.
14. Keep every test strictly within the PR scope or directly necessary regression coverage.

Use the existing shell and repository tools for inspection, but use `curl` exclusively to communicate with HTTP endpoints.

---

### 8.2 Evidence recording

Create a numbered test-case checklist before execution.

For every test case, record:

- Test case ID.
- Objective.
- Preconditions.
- HTTP method and endpoint.
- Sanitized request parameters and body.
- Expected result and its justification.
- Actual HTTP status.
- Actual response body or relevant headers.
- Pass, Fail, Blocked, or Not Applicable.
- Evidence file location.
- Defect reference if applicable.

Use `curl -sS -i` when response headers are useful.

For robust HTTP assertions, save response bodies and status codes separately. Avoid silently discarding curl errors or treating an HTTP error status as a transport failure.

Example:

```bash
curl -sS \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json" \
  -o /tmp/pr_response.json \
  -w '%{http_code}\n' \
  "$BASE_URL/<endpoint-under-test>"
```

Adapt authentication headers and request details to the actual API contract.

Use temporary files with restricted permissions where necessary. Redact tokens and personal information from all retained evidence.

Do not use `curl -k` to bypass TLS certificate verification unless explicitly justified for a local test environment.

---

---

## 9. Defect Reporting

For every failed test, determine whether the failure is:

- A regression introduced by this PR.
- A pre-existing defect.
- An environment or configuration issue.
- A test-data limitation.
- An ambiguous API contract.
- An inconclusive result.

Do not attribute a defect to this PR solely because it appears during testing.

Where possible, compare behavior with the base branch or a trusted pre-PR result. Do not change the running application to perform this comparison.

Assign severity based on actual impact:

- **P0 — Critical:** catastrophic security or system impact.


- **P1 — High:** significant authorization bypass, data corruption, or major business workflow failure.
- **P2 — Medium:** important functional defect with a workaround.
- **P3 — Low:** minor functional or response-contract issue.

These labels indicate impact, not the probability that a defect exists.

For every confirmed defect, provide:

1. Defect ID.
2. Concise title.
3. Severity and priority rationale.
4. Affected endpoint and HTTP method.
5. Preconditions and test data, sanitized.
6. Exact reproducible curl command with secrets replaced by placeholders.
7. Expected result.
8. Actual result.
9. HTTP status and sanitized response evidence.
10. Reproduction steps.
11. Business or security impact.
12. Whether the defect is confirmed as introduced by this PR or its origin is unknown.

Do not invent root causes. State the root cause only if supported by code inspection or other evidence.

---

---

## 10. Final QA Report

Produce a professional report in English using the following structure.

### A. Executive Summary

Include:

- PR title and scope.
- Environment tested.
- API base URL with sensitive components redacted.
- Testing method: curl-based API testing.
- Total planned test cases.
- Passed, failed, blocked, and not-applicable counts.
- Overall recommendation: PASS, PASS WITH KNOWN ISSUES, FAIL, or INCONCLUSIVE.

### B. Coverage Matrix

| Area | Planned | Passed | Failed | Blocked | N/A |
|---|---:|---:|---:|---:|---:|
| <area per section 5> | | | | | |
| Total | | | | | |

Ensure counts reconcile with the detailed test-case list.

### C. Detailed Test Results

| Test ID | Scenario | Endpoint | Expected | Actual | Status | Evidence |
|---|---|---|---|---|---|---|

Every planned test must appear in this table, including blocked and not-applicable tests.

### D. Defect Report

List all confirmed defects, their impact, reproduction commands, and evidence.

If no defects are found, say:

"No defects were identified within the executed test coverage."

Do not claim that the application is defect-free.

### E. Regression Results

Summarize which regression scenarios passed, failed, or could not be executed.

### F. Blockers and Limitations

List missing tokens, unavailable roles, inaccessible endpoints, insufficient fixtures, unavailable report formats, missing reference data, and other execution limitations.

Clearly distinguish BLOCKED from PASS.

### G. Final Recommendation

Recommend approval only when all mandatory scenarios have passed or any remaining gaps are explicitly documented and accepted.

A test suite with critical mandatory scenarios blocked must not receive an unconditional PASS.

### H. Evidence Artifacts

Save sanitized evidence in a dedicated temporary directory or the existing designated QA output directory.

Include, where practical:

- Test-case checklist.
- HTTP status and response evidence.
- Report files used for local integrity checks, if they contain no sensitive data.
- Summary of curl commands.
- Final QA report in Markdown.

Do not commit generated artifacts or modify the application repository unless explicitly requested.

---

---

## 11. Blockers, Assumptions and Ambiguities

Record here, before and after execution:

- Every `[UNVERIFIED]` item from sections 2–3 and every expectation left as "per contract (verify)".
- Any operation, parameter or response code that the live OpenAPI document does not confirm — report as ambiguous, do not invent an expectation.
- Missing tokens, roles, fixtures or environments that forced a BLOCKED status.
- Anything the PR description claims that could not be observed at runtime.

**Final requirement:** Behave as a skeptical Senior QA Engineer. Verify the behavior, collect evidence, challenge assumptions, prioritize reproducibility, and report only what the available evidence supports.


---

## 14. Test Identities (disposable environment — appended by PR QA Pilot)

`$BASE_URL` is a disposable environment built for this run from the PR head, holding synthetic seed data only; it is destroyed after the run. No credentials are handed to you and `$TOKEN` is not set. Create the identities you need yourself with curl through the documented self-registration operation `POST /api/v1/auth/register` (synthetic names, emails under `@prqa-synthetic.dev`, passwords generated locally and never written to evidence), and use the returned bearer token as `$TOKEN`. Before this run PR QA Pilot verified, from the sandbox network, that registration answered 201 and that the returned token was accepted by `GET /api/v1/auth/me` (401 without it). Create a second identity when a scenario needs another user or role; if a role cannot be obtained through documented operations, mark that scenario BLOCKED. Redact every token and password from evidence.

---

## 15. Machine-Readable Output (appended by PR QA Pilot — mandatory)

In addition to the Markdown Final QA Report, write a JSON file named `qa-result.json` into the evidence directory given by the environment variable `PRQA_EVIDENCE_DIR` (create it if missing; fall back to `./prqa-evidence/` when the variable is unset).

The file must validate against the PR QA Pilot `qa-result` schema version `1.0`:

- `result_version`: `"1.0"`
- `synthetic`: `false` for a real run
- `job_id`: the value of the environment variable `PRQA_JOB_ID`
- `repository`, `pr` (`number`, `title`, `head_sha`): copy from the job metadata in `PRQA_JOB_JSON`
- `environment`: `{ "name": "<local|staging|preview>", "base_url_redacted": "<scheme://host with no credentials or query>" }`
- `executed_by`: `{ "engine": "opencode", "mode": "live" }`
- `summary`: `overall_status` (`PASS` | `PASS_WITH_KNOWN_ISSUES` | `FAIL` | `INCONCLUSIVE`), integer `total`, `passed`, `failed`, `blocked`, `not_applicable`, and a short `executive_summary`
- `coverage_matrix`: one object per area with `area`, `planned`, `passed`, `failed`, `blocked`, `not_applicable` — counts must reconcile with `test_cases`
- `test_cases`: every planned test, including BLOCKED and NOT_APPLICABLE ones: `id` (the scenario ID used in this prompt), `area`, `scenario`, `endpoint`, `expected`, `actual`, `status`, `http_status`, `evidence` (relative file path), `defect_ref`
- `defects`: every confirmed defect: `id` (`DEF-001` …), `title`, `severity` (`P0`–`P3`), `severity_rationale`, `endpoint`, `preconditions`, `repro_curl` (secrets replaced by `$TOKEN`), `expected`, `actual`, `http_status`, `impact`, `origin` (`introduced_by_pr` | `pre_existing` | `environment` | `test_data` | `ambiguous_contract` | `unknown`), `test_case_ids`
- `blockers`: list of strings
- `regression_summary`: string
- `recommendation`: `{ "verdict": <same enum as overall_status>, "rationale": "..." }`
- `evidence`: `{ "directory": "<relative path>", "files": [ ... ] }`
- `report_markdown`: the full Markdown Final QA Report

Rules for this file:

1. Never include tokens, cookies, passwords, webhook URLs, or personal data. Use placeholders.
2. Counts in `summary` and `coverage_matrix` must equal the number of entries in `test_cases` with each status.
3. If testing could not start (no API reachable, no credentials), still write the file with `overall_status: "INCONCLUSIVE"`, all planned cases as `BLOCKED`, and the reasons in `blockers`.
4. Do not modify the repository. Do not commit evidence.
