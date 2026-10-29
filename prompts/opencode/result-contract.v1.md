

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
