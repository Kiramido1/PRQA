# Example: Failure and Retry Scenario

This illustrative walk-through shows how PR QA Pilot classifies and retries failures during one run. All timestamps, IDs, and results are fictional. No tests were executed.

> Status: Planned — documentation only (v0.1, 2026-10-18). Rules: [ARCHITECTURE.md §9](../ARCHITECTURE.md#9-failure-handling), [WORKFLOW_DESIGN.md §6](../WORKFLOW_DESIGN.md#6-retry-rules)

## Setup

| Field | Value |
|---|---|
| Project | `acme-orders` (`example-org/orders-service`) |
| PR | #42 "Add status filter and cancellation reason to orders API" |
| Run ID | `20261018T143000Z-acme-orders-pr42-a1b2c3d` |
| Level | `qa-test:standard` |
| Target | `https://orders.staging.example.test` |
| Plan | 8 approved test cases, `destructive_allowed=false` |

## Timeline

| Time (UTC) | Actor | Event | Classification / decision |
|---|---|---|---|
| 14:41:05 | runner | Pre-flight `GET /healthz` on target → curl exit 7 (connection refused) | All cases BLOCKED, `ENVIRONMENT_UNAVAILABLE`; `overall_status=BLOCKED`, exit 3 |
| 14:41:06 | n8n | `Env unavailable?` = true, first time | Wait 60 s, then re-execute with `force_rerun=true` (run-level retry 1/1) |
| 14:42:07 | runner | Pre-flight OK (HTTP 200) | Execution starts; previous result kept as `result.attempt-1.json` |
| 14:42:08 | runner | `TC-001` `GET /v1/orders?status=pending` → 200, schema OK | PASS |
| 14:42:09 | runner | `TC-002` `GET /v1/orders?status=unknown_value` → curl exit 28 (timeout at 15 s) | Transport retry 1/2 after 2 s (GET is idempotent) |
| 14:42:26 | runner | `TC-002` retry 1 → 400 with expected error body | PASS, with `attempts=2` recorded and the first attempt's evidence kept |
| 14:42:27 | runner | `TC-003` `POST /v1/orders/{orderId}/cancel` with `reason` → curl exit 56 (connection reset) | **Not retried** (POST is non-idempotent) → ERROR, `TRANSPORT_ERROR` |
| 14:42:27 | runner | `TC-004` depends on `TC-003` | BLOCKED, `DEPENDENCY_FAILED` |
| 14:42:28 | runner | `TC-005` `GET /v1/orders/{orderId}` as tenant B for a tenant A order → expected 404, actual 200 | FAIL, `ASSERTION_FAILED` (tenant isolation); finding confidence `SUSPECTED` until confirmed |
| 14:42:29–31 | runner | `TC-006`–`TC-008` | PASS |
| 14:42:31 | runner | `result.json`, `report.md` written | Counts: pass 5, fail 1, error 1, blocked 1 → `overall_status=FAIL`, **exit 1** (FAIL takes precedence over ERROR) |
| 14:42:32 | n8n | `prqa-notify` → Discord returns 429 with `retry_after` | Wait as instructed, retry 1/3 |
| 14:42:34 | n8n | Discord returns 204 | `notification_status=sent` |
| 14:55:00 | QA engineer | Triggers a confirmation re-run of `TC-005` only | Approval A5, see [WORKFLOW_DESIGN.md §9](../WORKFLOW_DESIGN.md#9-manual-approval-points) |
| 14:55:03 | runner | `TC-005` re-run 1 and re-run 2 → both 200 with tenant A data | Finding promoted to `CONFIRMED`, severity proposed `S1` (cross-tenant data exposure), with evidence for both re-runs attached |

## Resulting per-case summary (illustrative)

| ID | Status | failure_class | Attempts | Notes |
|---|---|---|---|---|
| TC-001 | PASS | — | 1 | |
| TC-002 | PASS | — | 2 | First attempt TIMEOUT, recovered by an idempotent retry |
| TC-003 | ERROR | TRANSPORT_ERROR | 1 | POST not retried; needs a manual re-check |
| TC-004 | BLOCKED | DEPENDENCY_FAILED | 0 | Depends on TC-003 |
| TC-005 | FAIL | ASSERTION_FAILED | 1 (+2 confirmation) | Finding CONFIRMED, S1 |
| TC-006 | PASS | — | 1 | |
| TC-007 | PASS | — | 1 | |
| TC-008 | PASS | — | 1 | |

## What this scenario demonstrates

1. **Environment problems are not test failures.** The first attempt was BLOCKED/`ENVIRONMENT_UNAVAILABLE`, not FAIL.
2. **Only idempotent requests are retried automatically.** TC-002 (GET) was retried. TC-003 (POST) was not.
3. **Transport errors (ERROR) are kept separate from assertion failures (FAIL).**
4. **Dependencies propagate as BLOCKED**, not as FAIL.
5. **Assertion failures are never retried automatically.** Confirmation is an explicit, recorded step, and a finding becomes `CONFIRMED` only after reproducible evidence.
6. **Notification failures do not change the test outcome.**

## Evidence layout (illustrative)

```
runs/20261018T143000Z-acme-orders-pr42-a1b2c3d/
├── result.attempt-1.json          # BLOCKED run (env unavailable)
├── result.json                    # final run
├── result.rerun-1.json            # TC-005 confirmation
├── report.md
├── evidence/
│   ├── TC-002.attempt-1.curl.json # write-out timing, exit 28
│   ├── TC-002.request.json
│   ├── TC-002.response.json       # redacted
│   ├── TC-005.response.json       # redacted, tenant IDs masked
│   └── ...
└── run.log
```
