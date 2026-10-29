# Reporting

This document defines the report formats, status semantics, evidence handling and notification format for PR QA Pilot runs.

> Status: Planned — documentation only (v0.1, 2026-10-18)

## Table of Contents

1. [Report Outputs](#1-report-outputs)
2. [Status Definitions](#2-status-definitions)
3. [Failure Classification and Finding Confidence](#3-failure-classification-and-finding-confidence)
4. [JSON Result Structure](#4-json-result-structure)
5. [Markdown Report Template](#5-markdown-report-template)
6. [Evidence Management](#6-evidence-management)
7. [Discord Notification Format](#7-discord-notification-format)
8. [Retention Policy](#8-retention-policy)
9. [Related Documents](#9-related-documents)

---

## 1. Report Outputs

Every run writes into `runs/<run_id>/` ([ADR-007](./DECISIONS.md)):

| File | Producer | Audience | Contents |
|---|---|---|---|
| `result.json` | `qa-runner run` | machines, n8n, history | Complete structured result (§4), the source of truth |
| `report.md` | `qa-runner report` | humans, PR reviewers | Rendered from `result.json` (§5); never contains data not in `result.json` |
| `evidence/TC-xxx.attempt-N.json` | `qa-runner run` | investigators | Sanitized request/response per attempt (§6); reproduction runs use `TC-xxx.repro-N.json` |
| `run.log` | runner | operators | Structured log lines (JSON Lines), already redacted |
| Discord message | `qa-runner notify` / n8n | team channel | Short summary (§7) |

Rules:

- `report.md` and the Discord message are **derived** from `result.json`. They never re-interpret results or add AI claims.
- Every report states the plan provider (for example `manual_chatgpt`) and the approver.
- A report never says "bug" unless the finding is `CONFIRMED` (§3).

Illustrative examples (not produced by real executions):

- [execution-result.example.json](./examples/execution-result.example.json)
- [qa-report.example.md](./examples/qa-report.example.md)
- [discord-notification.example.json](./examples/discord-notification.example.json)

## 2. Status Definitions

| Status | Meaning | Typical cause | Exit-code effect |
|---|---|---|---|
| `PASS` | The request completed and all deterministic assertions held. | n/a | 0 |
| `FAIL` | The request completed (an HTTP response was received), but at least one assertion did not hold. | Wrong status code, schema mismatch, wrong field value, unexpected 5xx | 1 (takes precedence) |
| `ERROR` | No valid response could be evaluated. | Connection refused, DNS, TLS, timeout, runner bug | 3 (if no FAIL) |
| `BLOCKED` | Not executed because a precondition failed or policy prevented it. | `depends_on` test failed, auth setup failed, request budget exhausted, destructive test not approved | not a failure; reported prominently |
| `SKIPPED` | Intentionally not run. | Filtered out by test level, disabled by reviewer | none |

Run-level status (`summary.overall_status`):

| Overall | Condition |
|---|---|
| `PASSED` | All executed tests PASS; no FAIL/ERROR (BLOCKED/SKIPPED are listed) |
| `FAILED` | ≥ 1 FAIL |
| `ERRORED` | ≥ 1 ERROR and no FAIL |
| `REJECTED` | Plan invalid or policy rejected before execution (exit 2) |
| `ABORTED` | Runner/config error (exit 4/5) or global timeout |

## 3. Failure Classification and Finding Confidence

### 3.1 `failure_class` (required for FAIL, ERROR, BLOCKED)

| `failure_class` | Status | Description |
|---|---|---|
| `ASSERTION_FAILED` | FAIL | A body/header/status assertion did not hold |
| `UNEXPECTED_5XX` | FAIL | The server returned 5xx where the contract expects a non-5xx response |
| `SCHEMA_MISMATCH` | FAIL | The response body does not validate against the expected JSON Schema |
| `TRANSPORT_ERROR` | ERROR | Connection refused/reset, DNS failure, TLS error |
| `TIMEOUT` | ERROR | `--max-time` exceeded after allowed retries |
| `ENVIRONMENT_UNAVAILABLE` | ERROR | Pre-flight health check failed. The target environment is down. |
| `AUTH_SETUP_FAILED` | BLOCKED/ERROR | Token missing/expired for the required `auth_profile` |
| `POLICY_BLOCKED` | BLOCKED | Not approved, destructive not allowed, budget exceeded |
| `DEPENDENCY_FAILED` | BLOCKED | A test in `depends_on` did not PASS |
| `RUNNER_ERROR` | ERROR | Internal runner defect (stack trace in `run.log`, not in the report) |

### 3.2 Finding confidence

A **finding** is attached to a FAIL that may indicate a defect in the system under test.

| Confidence | Requirements | Report wording |
|---|---|---|
| `CONFIRMED` | Deterministic assertion failed; reproduced on re-run (≥ 2/2); sanitized evidence for every attempt; expected behavior backed by a documented contract (OpenAPI, PR description, acceptance criteria) | "Confirmed defect" |
| `SUSPECTED` | Single occurrence, flaky, ambiguous or undocumented contract, or expectation comes only from the AI plan | "Suspected issue — needs verification" |
| `NOT_A_DEFECT` | Investigation showed the test expectation was wrong | "Test expectation corrected" (kept for history) |

Severity (`S1` Critical, `S2` High, `S3` Medium, `S4` Low) is assigned **only** to `CONFIRMED` findings, or to `SUSPECTED` findings when a human reviewer sets it explicitly. The AI's opinion alone never establishes a defect or its severity. See [QA Strategy](./QA_STRATEGY.md).

## 4. JSON Result Structure

`result.json` (planned schema `result_version: "1.0"`). Abbreviated shape. Placeholders in angle brackets:

```json
{
  "result_version": "1.0",
  "illustrative": false,
  "run_id": "20261018T143000Z-acme-orders-pr42-a1b2c3d",
  "project_id": "acme-orders",
  "environment": "staging",
  "base_url": "https://orders.staging.example.test",
  "pr": {
    "repository": "example-org/orders-service",
    "number": 42,
    "base_ref": "main",
    "head_ref": "feature/order-status-filter",
    "base_sha": "<40-hex>",
    "head_sha": "<40-hex>"
  },
  "plan": {
    "plan_id": "plan-acme-orders-pr42-v1",
    "plan_sha256": "<64-hex>",
    "test_level": "standard",
    "generated_by": { "provider": "manual_chatgpt", "model": "ChatGPT Free (web interface)", "prompt_version": "v1" },
    "approval": { "approved_by": "<reviewer>", "approved_at": "<ISO-8601>", "destructive_allowed": false }
  },
  "runner": { "version": "0.1.0", "python_version": "3.13.11", "curl_version": "8.17.0", "host": "qa-runner" },
  "timing": { "started_at": "<ISO-8601>", "finished_at": "<ISO-8601>", "duration_ms": 0 },
  "summary": {
    "overall_status": "FAILED",
    "total": 0, "pass": 0, "fail": 0, "error": 0, "blocked": 0, "skipped": 0,
    "requests_sent": 0, "request_budget": 60,
    "exit_code": 1
  },
  "test_cases": [
    {
      "id": "TC-001",
      "title": "<title>",
      "purpose": "<purpose>",
      "scope_ref": "<PR scope reference>",
      "category": "happy_path",
      "auth_profile": "tenant_a_user",
      "status": "PASS",
      "failure_class": null,
      "request": { "method": "GET", "url": "<sanitized url>", "headers": { "Authorization": "[REDACTED]" }, "body": null },
      "expected": { "status": [200], "schema": "#/components/schemas/OrderList", "assertions": [] },
      "actual": { "status": 200, "duration_ms": 0, "body_excerpt": {} },
      "assertions": [ { "description": "<text>", "result": "PASS", "expected": "<v>", "actual": "<v>" } ],
      "attempts": 1,
      "duration_ms": 0,
      "evidence": ["evidence/TC-001.attempt-1.json"],
      "finding": null
    }
  ],
  "findings": [
    {
      "finding_id": "F-001",
      "test_case_id": "TC-005",
      "title": "<title>",
      "confidence": "CONFIRMED",
      "severity": "S2",
      "reproduced": "2/2",
      "contract_reference": "<openapi pointer>",
      "evidence": ["evidence/TC-005.attempt-1.json"]
    }
  ],
  "redaction": { "applied": true, "rules_version": "1.0", "redacted_values": 0 },
  "limitations": ["<text>"]
}
```

Field rules:

- All URLs, headers and bodies are **post-redaction**.
- `actual.body_excerpt` holds at most 2 KiB. Full bodies (truncated to 16 KiB, sanitized) stay in evidence files.
- Transport failures set `actual.status: null` and `actual.transport_error`. Retried tests include `retry_history`.
- `illustrative: true` is required on every example file so it cannot be mistaken for a real run.
- `summary.exit_code` follows the CLI exit codes (0 PASS, 1 FAIL, 2 rejected, 3 ERROR, 4 config, 5 internal).

Full illustrative instance: [execution-result.example.json](./examples/execution-result.example.json).

## 5. Markdown Report Template

`report.md` is rendered from `result.json` with this structure (placeholders in `{{ }}`):

````markdown
# QA Report — {{project_id}} PR #{{pr.number}}

| Field | Value |
|---|---|
| Run ID | `{{run_id}}` |
| Repository | {{pr.repository}} |
| PR | #{{pr.number}} `{{pr.head_ref}}` → `{{pr.base_ref}}` (head `{{pr.head_sha | short}}`) |
| Environment | {{environment}} (`{{base_url}}`) |
| Test level | {{plan.test_level}} |
| Plan | {{plan.plan_id}}, generated by {{plan.generated_by.provider}}, approved by {{plan.approval.approved_by}} |
| Started / Duration | {{timing.started_at}} / {{timing.duration_ms | seconds}} s |
| **Overall** | **{{summary.overall_status}}** |

## Summary

| Total | PASS | FAIL | ERROR | BLOCKED | SKIPPED | Requests |
|---|---|---|---|---|---|---|
| {{total}} | {{pass}} | {{fail}} | {{error}} | {{blocked}} | {{skipped}} | {{requests_sent}}/{{request_budget}} |

## Findings
<!-- CONFIRMED first, then SUSPECTED. Omit section if none. -->
### {{finding_id}}: {{title}} ({{confidence}}, {{severity}})
- Test case: {{test_case_id}} · Reproduced: {{reproduced}}
- Contract: {{contract_reference}}
- Evidence: {{evidence}}

## Test Results

| ID | Title | Category | Status | HTTP | Duration | Class |
|---|---|---|---|---|---|---|
| {{id}} | {{title}} | {{category}} | {{status}} | {{actual.status}} | {{duration_ms}} ms | {{failure_class or "—"}} |

## Details
### {{id}}: {{title}} ({{status}})
- **Purpose:** {{purpose}}
- **PR scope:** {{scope_ref}}
- **Request:** `{{method}} {{sanitized path+query}}` as `{{auth_profile}}`
- **Payload (sanitized):** {{body as json block}}
- **Expected:** status {{expected.status}}; {{assertion summaries}}
- **Actual:** status {{actual.status}}; {{body_excerpt as json block}}
- **Assertions:** ✅/❌ list
- **Attempts:** {{attempts}} · **Evidence:** {{evidence}}

## Limitations
- {{limitations}}
- Results reflect the environment at `{{base_url}}` at run time, not the PR source code directly.

---
_Generated by PR QA Pilot runner {{runner.version}}. All request/response data is redacted._
````

Full illustrative instance: [qa-report.example.md](./examples/qa-report.example.md).

## 6. Evidence Management

Each test attempt writes one file: `runs/<run_id>/evidence/<TC-id>.attempt-<n>.json`. Confirmation re-runs write `<TC-id>.repro-<n>.json`.

```json
{
  "test_case_id": "TC-003",
  "attempt": 1,
  "timestamp": "2026-10-18T14:30:04.120Z",
  "request": {
    "method": "POST",
    "url": "https://orders.staging.example.test/v1/orders/ord_qa_0001/cancel",
    "headers": { "Authorization": "[REDACTED]", "Content-Type": "application/json" },
    "body": { "reason": "customer_request" }
  },
  "response": {
    "status": 200,
    "headers": { "content-type": "application/json", "set-cookie": "[REDACTED]" },
    "body": { "id": "ord_qa_0001", "status": "cancelled", "cancellation_reason": "customer_request" },
    "body_truncated": false
  },
  "curl": { "exit_code": 0, "time_total_ms": 212, "remote_ip": "[REDACTED:ip]", "http_version": "2" },
  "redaction": { "applied": true, "rules_version": "1.0" }
}
```

Rules:

- Evidence is written only **after** redaction ([Security §8](./SECURITY.md#8-secret-handling-and-redaction)).
- Raw curl argv is never stored. Only sanitized method, URL, headers and body are stored.
- Response bodies are truncated to 16 KiB. Binary bodies are stored as `{"binary": true, "bytes": n, "content_type": "..."}`.
- Evidence files are immutable once written. A SHA-256 of each file is recorded in `run.log`. A re-run creates new files or a new run ID.
- `result.json` references evidence by relative path. `report.md` links to the same paths.
- Remote IPs are redacted by default (they reveal internal topology).

## 7. Discord Notification Format

Discord receives a **summary only**: never full response bodies, tokens, secret-bearing URLs or stack traces.

### 7.1 Content

| Element | Content |
|---|---|
| `username` | `PR QA Pilot` |
| `content` | One line: status emoji, project, PR number |
| `allowed_mentions` | `{"parse": []}` so PR text cannot trigger @everyone/@here pings |
| Embed title | `QA {{overall_status}} — {{repository}} #{{number}}` |
| Embed description | PR title (truncated to 200 chars), test level, environment |
| Embed color | PASSED green `3066993`, FAILED red `15158332`, ERRORED orange `15105570`, REJECTED/ABORTED grey `9807270` |
| Fields | Counts; up to 5 findings (`F-001 · CONFIRMED · S2 · TC-005 …`); notable ERRORs; run ID; report location |
| Footer | `plan: <provider> · approved by <reviewer> · runner <version>` |
| Report reference | Path or internal link to `runs/<run_id>/report.md`; the full report is never attached |

### 7.2 Limits

Discord webhook embeds have size limits [Verify against https://discord.com/developers/docs/resources/message#embed-object-embed-limits]. They are commonly documented as: message `content` ≤ 2000 characters, ≤ 10 embeds, title ≤ 256, description ≤ 4096, ≤ 25 fields, field name ≤ 256, field value ≤ 1024, footer ≤ 2048, and ≤ 6000 characters total across embeds. The notifier enforces stricter internal limits:

- 1 embed, ≤ 8 fields, each field value ≤ 512 characters, total ≤ 3000 characters.
- More than 5 findings → show 5, then "…and N more in report".
- On HTTP 429, wait for `retry_after` and retry up to 3 times [Verify: https://discord.com/developers/docs/topics/rate-limits]. On final failure, record `notification: FAILED` in `run.log`. The test result is never changed.

### 7.3 Sending

- Phase 5: `qa-runner notify runs/<run_id> --dry-run` prints the payload. The n8n HTTP Request node (Discord webhook credential) posts it.
- See the [Operations Runbook](./OPERATIONS_RUNBOOK.md) for handling failed notifications.

Illustrative payload: [discord-notification.example.json](./examples/discord-notification.example.json).

## 8. Retention Policy

[Recommendation]: adjust per organization. [Needs approval] if evidence may contain personal data.

| Artifact | Retention | Notes |
|---|---|---|
| `result.json`, `report.md` | 180 days | Small; kept for history and trends; may be archived compressed |
| `evidence/*.json` | 30 days (90 days for runs with CONFIRMED findings) | Sanitized but may still contain business data |
| `run.log` | 30 days | |
| `context/` (AI context package) | 30 days | Contains PR text / diff summary |
| `plan.json` + `approval.json` | 180 days | Audit trail of what was approved and executed |
| Discord messages | Per channel policy | Summary only |
| n8n execution data | Prune after 14 days (`EXECUTIONS_DATA_PRUNE=true`, `EXECUTIONS_DATA_MAX_AGE=336`) [Verify: https://docs.n8n.io/hosting/configuration/environment-variables/executions/] | |

A planned `qa-runner prune --older-than <days>` command (or a scheduled n8n workflow calling the runner) deletes expired runs. A run can be pinned with a `runs/<run_id>/.keep` file.

## 9. Related Documents

- [QA Strategy](./QA_STRATEGY.md): severity and bug confirmation rules
- [Security](./SECURITY.md): redaction and data handling
- [AI Test Plan Specification](./AI_TEST_PLAN_SPEC.md)
- [Workflow Design](./WORKFLOW_DESIGN.md): where reporting and notification steps run
- [Operations Runbook](./OPERATIONS_RUNBOOK.md)
