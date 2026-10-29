# Senior QA Engineer Prompt — Comprehensive API Testing for PR: Fix Organization Module Issues

## 1. Role and Objective

Act as a Senior QA Engineer specializing in Backend API Testing, REST API validation, Authorization and RBAC testing, Data Integrity, Negative Testing, Edge Cases, and Regression Testing.

Your task is to independently test the following PR against the actual running HSE Backend API:

**PR Title:** Fix organization module issues

Perform comprehensive, evidence-based testing using **curl exclusively for API requests**.

Your objective is to identify reproducible functional defects, authorization vulnerabilities, incorrect response data, validation gaps, report-generation failures, pagination inconsistencies, and regressions within the exact PR scope.

Do not assume the PR is correct just because its implementation matches the description. Verify actual runtime behavior.

---

## 2. Strict Testing Constraints

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
11. Do not create, update, delete, activate, or deactivate real organizations without explicit authorization and a verified safe test environment.
12. If a prerequisite is unavailable, document the blocker and continue with all independent tests.
13. Do not fix any defect discovered. Report it with reproducible evidence.
14. Keep every test strictly within the PR scope or directly necessary regression coverage.

Use the existing shell and repository tools for inspection, but use `curl` exclusively to communicate with HTTP endpoints.

---

## 3. PR Scope

The PR changes four files:

- `app/routers/organizations_routes.py`
- `app/services/organizations_service.py`
- `app/repositories/organizations_repo.py`
- `app/schemas/organizations_schema.py`

The intended changes are:

### A. Authorization

Allow `hse_super_admin` to access these endpoints:

- `POST /organizations/`
- `PUT /organizations/`
- `DELETE /organizations/{id}`
- `PATCH /organizations/{id}/activate`
- `PATCH /organizations/{id}/deactivate`
- `GET /organizations/report`
- `GET /organizations/{id}/report`

### B. Organization Detail Accuracy

`GET /organizations/{id}` must return actual values for:

- `employees_count`
- `certified_count`
- `compliance_percentage`
- `risk_level`

The values must match the corresponding organization returned by `GET /organizations/`.

Inactive organizations must continue to return `null` for compliance, as specified in the PR.

### C. Organization Report Failure

The organization report must retrieve region information through the governorate relationship instead of the removed `org.region` attribute.

Verify both PDF and XLSX exports.

### D. governorate_id Validation

`POST /organizations/` and `PUT /organizations/` must reject invalid `governorate_id` values using the `Int4Id` validation type.

Values explicitly covered by the PR:

- `0`
- `-1`
- `2147483648`

The valid positive int4 range is `1` through `2147483647`, subject to normal database foreign-key and business validation.

### E. Stable Pagination Ordering

`GET /organizations/` must order results by:

`created_at DESC, id DESC`

Pagination must not produce duplicate or missing records when the underlying dataset remains unchanged.

---

## 4. Phase 1 — Environment and API Discovery

Before testing, inspect the repository and discover the actual API environment.

### 4.1 Identify the API Base URL

Inspect existing project documentation, `.env.example`, README files, Swagger configuration, and shell environment variables.

Check for an existing local API before selecting another environment.

Potential local URLs may include:

- `http://127.0.0.1:8000`
- The documented HSE development Swagger URL

These are examples only. Verify which environment is actually available.

Never print secrets while inspecting environment variables. Do not dump entire `.env` files.

Define a configurable base URL:

`BASE_URL`

Use a configurable token variable:

`TOKEN`

Do not invent credentials or retrieve unauthorized credentials.

### 4.2 Inspect OpenAPI

If the API is running, retrieve the OpenAPI document using curl and inspect the relevant endpoints.

For example:

```bash
curl -sS "$BASE_URL/openapi.json" -o /tmp/org_pr_openapi.json
```

Verify:

- Actual route paths and HTTP methods.
- Required request fields.
- Request body schemas.
- Authentication mechanisms.
- Available response status codes.
- Query parameters and export format values.
- Pagination parameter names.
- Whether `PUT /organizations/` uses a full or partial update schema.
- Whether the report endpoints use authentication and specific response content types.

Use the real schema instead of guessing request bodies.

If OpenAPI is unavailable, inspect the relevant repository files and report that runtime schema verification was blocked.

### 4.3 Establish a Safe Test Dataset

Identify existing organization records through read-only API requests.

Identify, if available:

- An active organization.
- An inactive organization.
- An organization with nonzero employee and certification counts.
- An organization with zero employees or certifications.
- An organization with a known governorate and region.
- An organization suitable for testing a not-found ID.
- A valid governorate ID for positive validation testing.

Prefer existing records and compare responses rather than assuming any ID or value.

For mutating endpoints, use an explicitly authorized, isolated test environment and disposable fixtures only. If safe fixtures are unavailable, mark destructive scenarios BLOCKED and do not execute them.

Do not use guessed IDs as evidence that records exist.

---

## 5. Phase 2 — Test Execution Rules

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
  -o /tmp/org_response.json \
  -w '%{http_code}\n' \
  "$BASE_URL/organizations/"
```

Adapt authentication headers and request details to the actual API contract.

Use temporary files with restricted permissions where necessary. Redact tokens and personal information from all retained evidence.

Do not use `curl -k` to bypass TLS certificate verification unless explicitly justified for a local test environment.

---

## 6. Phase 3 — Authorization and RBAC Tests

Test every endpoint listed in the PR.

### 6.1 Required Endpoint Matrix

For each of the seven endpoints, verify:

- An authenticated `hse_super_admin` request is no longer incorrectly rejected with `403`.
- The endpoint executes the intended operation or returns a legitimate business validation response.
- Unauthorized and unauthenticated behavior remains consistent with the API security contract.
- Other roles do not gain unintended permissions.
- An invalid or nonexistent organization ID is handled appropriately.
- A missing token and an invalid or expired token are rejected appropriately.

Expected access-control behavior must be based on the actual authorization contract. Do not assume all unauthorized cases must return the same status code.

### 6.2 Role-Specific Coverage

Where suitable test accounts and authorization are available, cover:

1. `hse_super_admin`.
2. The previous authorized administrative role, if one exists.
3. An authenticated user who should not have organization-management permissions.
4. A user with report access but no write permissions, if applicable.
5. An unauthenticated request.
6. A malformed, invalid, or expired token.

Verify that the PR has not introduced privilege escalation.

### 6.3 Mutation Safety

For `POST`, `PUT`, `DELETE`, `activate`, and `deactivate`:

- Execute mutation scenarios only against disposable, authorized test fixtures.
- Verify persisted results through subsequent read-only API requests.
- Verify state transitions and repeated operations.
- Restore fixtures only when restoration is safe, authorized, and supported by the API.
- Never delete or deactivate an existing business record merely to complete a test.

If no safe test fixture exists, document the exact request that would be needed and mark it BLOCKED rather than executing it.

---

## 7. Phase 4 — Organization Detail Data Accuracy

Test:

`GET /organizations/`

`GET /organizations/{id}`

Use the same organization ID in both responses.

### Required Test Cases

**ORG-DTL-001 — List/detail aggregate consistency**

Compare all four fields:

- `employees_count`
- `certified_count`
- `compliance_percentage`
- `risk_level`

Expected: values match for the same organization, with documented handling of numeric representation and `null`.

**ORG-DTL-002 — Multiple organizations**

Repeat the comparison for multiple existing organizations with different data characteristics.

**ORG-DTL-003 — Nonzero aggregates**

Use an organization with known nonzero employee and certification counts, if available.

Expected: actual aggregate values are returned instead of schema defaults.

**ORG-DTL-004 — Zero aggregates**

Test an organization with no matching employees or certifications, if one exists.

Expected: the response matches the API's documented zero-count behavior and list/detail consistency.

**ORG-DTL-005 — Inactive organization**

Expected: `compliance_percentage` remains `null`, as specified by the PR. Verify the other fields against the actual contract and list response.

**ORG-DTL-006 — Not-found organization**

Request a confirmed nonexistent organization ID.

Expected: an appropriate not-found response, without fabricated organization details.

**ORG-DTL-007 — Invalid path IDs**

Test malformed IDs, zero, and negative IDs where accepted by the routing layer.

Expected: appropriate validation or not-found behavior according to the API contract, without unexpected `500` errors.

**ORG-DTL-008 — Response schema**

Verify field types, missing fields, unexpected nulls, and accidental fallback/default values.

**ORG-DTL-009 — Repeatability**

Repeat read requests without changing data and check whether aggregate values remain consistent.

Do not claim aggregate correctness from list/detail equality alone if both endpoints could share the same incorrect calculation. Use independent, documented source-of-truth data only if available through authorized read-only API endpoints.

---

## 8. Phase 5 — Organization Reports: PDF and XLSX

Test:

- `GET /organizations/report`
- `GET /organizations/{id}/report?format=xlsx`
- `GET /organizations/{id}/report?format=pdf`

First inspect OpenAPI to establish the actual supported formats, parameter locations, and response types.

### Required Test Cases

**ORG-RPT-001 — Organization report endpoint**

Verify the report endpoint returns the expected report representation without an unexpected server error.

**ORG-RPT-002 — XLSX export**

Verify:

- Correct successful HTTP status.
- Expected Excel MIME type or documented content type.
- A nonempty file.
- A valid ZIP-based XLSX container.
- Required workbook structures such as `[Content_Types].xml` and `xl/workbook.xml`.
- No JSON error response saved with an `.xlsx` extension.
- Expected organization data is present where the report contract specifies it.

**ORG-RPT-003 — PDF export**

Verify:

- Correct successful HTTP status.
- Expected PDF MIME type.
- A nonempty file.
- Valid PDF signature and basic structural integrity.
- No HTML error page or JSON exception saved as a PDF.
- Expected report content is present.

**ORG-RPT-004 — Region relationship**

Use an organization with a known governorate and region.

Verify the generated report includes the correct region where the report contract requires it.

**ORG-RPT-005 — Missing or nullable relationships**

If the API allows an organization without a governorate or with incomplete related data, test how the report handles it.

Expected: documented graceful behavior, not an unhandled exception.

**ORG-RPT-006 — Nonexistent organization**

Request a report for a confirmed nonexistent organization ID.

Expected: a documented not-found response, not a generic `500`.

**ORG-RPT-007 — Invalid format**

Test unsupported format values, capitalization variations, and a missing format parameter where applicable.

Expected: documented default behavior or a validation error. Do not assume invalid formats must return `422` without checking the API contract.

**ORG-RPT-008 — Report consistency**

Where equivalent information appears in the list, detail, and report, verify consistency for the same organization.

**ORG-RPT-009 — Content headers**

Inspect `Content-Type`, `Content-Disposition`, and relevant download headers.

**ORG-RPT-010 — Repeated exports**

Generate each supported format more than once and verify stable behavior and valid files.

Use shell utilities such as `file`, `unzip -t`, and `unzip -l` for local inspection. Use additional standard-library checks where useful.

Do not mark a report as passing solely because it returned `200`.

---

## 9. Phase 6 — governorate_id Validation

Test both:

- `POST /organizations/`
- `PUT /organizations/`

Use request bodies derived from the actual OpenAPI schema.

For each request, vary only the target field when possible to isolate the behavior.

### Required Boundary Cases

| Input | Expected validation outcome |
|---|---|
| `0` | Rejected |
| `-1` | Rejected |
| `2147483648` | Rejected |
| `1` | Accepted by numeric range validation, subject to remaining constraints |
| `2147483647` | Accepted by numeric range validation, subject to remaining constraints |
| A known existing positive governorate ID | Accepted by field validation |
| A nonexistent positive governorate ID | Must be checked against foreign-key/business validation |
| `null` | Determined by field requiredness and nullability |
| Missing field | Determined by create/update schema |
| `"abc"` | Rejected as an invalid integer |
| `1.5` | Must not silently become an unintended valid integer |
| `true` | Verify whether strict integer validation rejects booleans |
| Very large integer | Rejected without an unhandled exception |
| Numeric string such as `"1"` | Verify documented coercion behavior |
| Empty string | Rejected if supplied as an invalid ID |

### Additional Assertions

1. Confirm invalid values are rejected by the correct validation layer.
2. Record the actual response status and error body.
3. Ensure invalid requests do not create or modify organization data.
4. After each safe negative test, use a read-only API request to verify that the relevant fixture was not changed.
5. Confirm validation failures do not produce `500 Internal Server Error`.
6. Verify the update endpoint does not partially modify unrelated fields when `governorate_id` is invalid.

For positive mutation tests, use disposable fixtures in a safe test environment only.

Do not assume `2147483647` must result in a successful database operation: it may pass integer-range validation but fail because the referenced governorate does not exist.

---

## 10. Phase 7 — Stable Ordering and Pagination

Test:

`GET /organizations/?limit=2`

Inspect the actual pagination schema first to determine whether the API uses `offset`, `page`, or another pagination parameter.

### Required Test Cases

**ORG-PAG-001 — Default ordering**

Verify organization rows are ordered by:

`created_at DESC, id DESC`

Check both ordering keys, not just the first page.

**ORG-PAG-002 — Equal timestamps**

If existing records share the same `created_at`, verify the higher ID appears first within that timestamp group.

Do not alter real data to manufacture equal timestamps.

**ORG-PAG-003 — Sequential pages**

Retrieve every page with a small page size, such as two records per page.

Aggregate all returned organization IDs and verify:

- No duplicates.
- No missing IDs relative to a complete enumeration of the same unchanged dataset.
- Correct order across page boundaries.
- Correct page sizes, except for the final page.

**ORG-PAG-004 — Repeated identical requests**

Request the same page several times without changing data.

Expected: identical ordered IDs.

**ORG-PAG-005 — Page boundaries**

Test empty results, the final partial page, and the page beyond the end of the dataset where supported.

**ORG-PAG-006 — Different limits**

Test the default limit, `limit=1`, `limit=2`, a normal larger limit, the documented maximum, zero, negative values, and a value exceeding the maximum.

Use documented pagination limits; do not assume a particular maximum.

**ORG-PAG-007 — Invalid pagination parameters**

Test malformed, non-integer, negative, zero, and excessively large page/offset values where applicable.

Expected: contract-compliant validation and no unexpected server errors.

**ORG-PAG-008 — Stable dataset comparison**

Enumerate the complete list using pagination, then compare the aggregated IDs and ordering against an independently retrieved complete dataset if the API provides a suitable safe method.

If no independent enumeration method exists, clearly describe the limits of the verification.

**ORG-PAG-009 — Concurrent data changes**

Do not mutate production data to test concurrent changes. If a dedicated test environment and authorized concurrent fixture are available, evaluate how inserts or deletes affect offset-based pagination and report the behavior separately from the PR's stable-ordering guarantee.

Do not report offset-pagination shifts under concurrent mutation as a regression of the tie-breaker fix without evidence.

---

## 11. Phase 8 — Cross-Feature Regression Tests

Perform focused regression tests only within the PR's scope.

### Authorization and CRUD

- Existing permitted roles retain their expected access.
- Unauthorized roles cannot gain access to organization management.
- Successful create/update operations persist expected fields.
- Delete and activation transitions behave correctly on disposable fixtures.
- Invalid IDs do not trigger unexpected server errors.

### Detail and Reports

- List and detail aggregate values remain consistent.
- Inactive organizations retain `null` compliance.
- PDF and XLSX exports continue to work.
- Report data matches the selected organization.
- Missing organizations are handled consistently.

### Validation and Pagination

- Valid governorate IDs continue to work.
- Invalid governorate IDs are rejected.
- Failed validation does not change persisted data.
- Ordering is deterministic when timestamps are equal.
- Page boundaries do not duplicate or skip records in an unchanged dataset.

Avoid unrelated endpoint testing or broad exploratory testing outside the specified module.

---

## 12. Phase 9 — Defect Identification and Evidence

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

## 13. Phase 10 — Final QA Report

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
| Authorization | | | | | |
| Organization detail | | | | | |
| PDF/XLSX reports | | | | | |
| governorate_id validation | | | | | |
| Pagination | | | | | |
| Regression | | | | | |
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

## 14. Execution Instructions

Start immediately with environment discovery and OpenAPI inspection.

Then:

1. Identify available credentials and roles without exposing secrets.
2. Inspect the actual endpoint contracts.
3. Determine which tests can safely run with existing read-only data.
4. Execute all available read-only scenarios first.
5. Run mutation tests only when a verified disposable test environment and explicit authorization are available.
6. Validate every response against the real API contract.
7. Inspect exported PDF/XLSX files locally.
8. Verify pagination using actual returned IDs.
9. Record every result and blocker.
10. Produce the final QA report.

Do not stop after generating a plan. Execute all safe and authorized tests that the environment supports.

If execution is blocked, do not fabricate results or repeatedly retry destructive operations. Complete all independent tests and provide exact commands and prerequisites for the remaining cases.

**Final requirement:** Behave as a skeptical Senior QA Engineer. Verify the behavior, collect evidence, challenge assumptions, prioritize reproducibility, and report only what the available evidence supports.
