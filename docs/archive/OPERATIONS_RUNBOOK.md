# Operations Runbook

Step-by-step procedures for operating PR QA Pilot: starting and stopping, running tests, investigating failures, recovering runs, rotating credentials, troubleshooting integrations, cleaning up test data, handling failed notifications and responding to incidents.

> Status: Planned — documentation only (v0.1, 2026-10-18)

## Contents

1. [Starting and stopping services](#1-starting-and-stopping-services)
2. [Running a test manually](#2-running-a-test-manually)
3. [Investigating failures](#3-investigating-failures)
4. [Recovering interrupted runs](#4-recovering-interrupted-runs)
5. [Rotating credentials](#5-rotating-credentials)
6. [Troubleshooting integrations](#6-troubleshooting-integrations)
7. [Cleaning up test data](#7-cleaning-up-test-data)
8. [Handling failed notifications](#8-handling-failed-notifications)
9. [Incident response](#9-incident-response)
10. [Backups and routine maintenance](#10-backups-and-routine-maintenance)

Related: [LOCAL_SETUP.md](./LOCAL_SETUP.md) · [WORKFLOW_DESIGN.md](./WORKFLOW_DESIGN.md) · [REPORTING.md](./REPORTING.md) · [SECURITY.md](./SECURITY.md) · [AI_TEST_PLAN_SPEC.md](./AI_TEST_PLAN_SPEC.md) · [DEPLOYMENT.md](./DEPLOYMENT.md)

Conventions in this runbook:

- `ROOT="<project dir>"` — the drive mount point changes between boots (`/media/alpha/36A212E9A212ACFD`, `…ACFD1`, …); use the path of the current mount. Always quote it, because the path contains spaces.
- `DC='docker compose -f "$ROOT/compose/docker-compose.yml" --env-file "$ROOT/compose/.env"'`. Commands below write `$DC` for brevity. In practice, use the alias from [LOCAL_SETUP.md §7](./LOCAL_SETUP.md#7-startup).
- All `qa-runner` commands are **planned** ([IMPLEMENTATION_ROADMAP.md](./IMPLEMENTATION_ROADMAP.md)). Run them on the host inside `.venv`, or with `$DC exec qa-runner qa-runner ...`.

---

## 1. Starting and stopping services

| Task | Command |
|---|---|
| Start everything | `$DC up -d` |
| Start n8n only (Phase 1) | `$DC up -d n8n` |
| Start with egress proxy | `$DC --profile egress up -d` |
| Status | `$DC ps` |
| Logs | `$DC logs -f --tail=200 n8n` / `qa-runner` |
| Restart one service | `$DC restart qa-runner` |
| Stop (keep containers) | `$DC stop` |
| Stop and remove containers (keep data) | `$DC down` |

**Before stopping:**

1. In n8n, open **Executions** and check that nothing is *Running*.
2. Look for runs that haven't finished:
   ```bash
   for d in "$ROOT"/runs/*/; do [ -f "$d/run.log" ] && [ ! -f "$d/result.json" ] && echo "IN PROGRESS: $d"; done
   ```
3. If something is in progress, wait for it to finish, or stop anyway and recover afterwards ([§4](#4-recovering-interrupted-runs)).

**After starting:** go through the [verification checklist](./LOCAL_SETUP.md#9-verification-checklist). At minimum, check that n8n is healthy and that the runner's `/health` returns 200 from n8n.

## 2. Running a test manually

The flow is the same for every AI provider. Only step 3 changes.

### 2.1 Fully manual (CLI, Phases 2–3)

```bash
cd "$ROOT" && . .venv/bin/activate
PROJECT=acme-orders PR=42
HEAD_SHA7=$(gh pr view "$PR" -R example-org/orders-service --json headRefOid --jq '.headRefOid[0:7]')
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$PROJECT-pr$PR-$HEAD_SHA7"
mkdir -p "runs/$RUN_ID"

# 1. Build a sanitized context package (PR metadata, diff summary, relevant OpenAPI fragments)
qa-runner context build --project "$PROJECT" --pr "$PR" --out "runs/$RUN_ID/context"

# 2. Review the context for secrets and personal data BEFORE pasting it anywhere
less "runs/$RUN_ID/context/"*

# 3. AI planning (manual ChatGPT Free):
#    - open prompts/test-plan-prompt.v1.md, paste prompt + context into ChatGPT (web)
#    - save ONLY the returned JSON as runs/$RUN_ID/plan.json

# 4. Validate (schema + policy). Exit 2 = rejected; fix or regenerate.
qa-runner plan validate "runs/$RUN_ID/plan.json" --project "$PROJECT"

# 5. Human review, then approve (records plan sha256 in approval.json)
qa-runner plan approve "runs/$RUN_ID/plan.json" --by "<your-name>"

# 6. Execute, report, notify
qa-runner run "runs/$RUN_ID" --project "$PROJECT"; echo "exit=$?"
qa-runner report "runs/$RUN_ID"
qa-runner notify "runs/$RUN_ID" --dry-run      # inspect the payload first
qa-runner notify "runs/$RUN_ID"
```

Exit codes (see [REPORTING.md](./REPORTING.md)):

| Code | Meaning | Next action |
|---|---|---|
| 0 | All executed tests PASS | None |
| 1 | ≥1 FAIL | [§3](#3-investigating-failures) |
| 2 | Plan invalid or policy rejected | Read the validation output; regenerate or edit the plan, then re-approve |
| 3 | ≥1 ERROR, no FAIL | Check the environment and network ([§6](#6-troubleshooting-integrations)) |
| 4 | Configuration error | Check `config/projects.yaml` and env vars |
| 5 | Runner internal error | Collect `run.log`; open a bug against the runner |

### 2.2 Through n8n (Phase 4+)

1. Open the workflow **PRQA – Manual Run** ([WORKFLOW_DESIGN.md](./WORKFLOW_DESIGN.md)).
2. Fill in the input form: `project_id`, `pr_number`, `test_level`, and `plan_json` (pasted) or `run_id` (existing).
3. At the approval point, review the plan summary and approve or reject it.
4. Results show up in the execution, in `runs/<run_id>/`, and in Discord.

Never run tests against a host that isn't in that project's `allowed_hosts`. The runner rejects it (`POLICY_BLOCKED`), so don't try to work around the check.

## 3. Investigating failures

Work through these in order and stop as soon as the cause is clear.

1. **Read the summary:** `runs/<run_id>/report.md`, or `jq '.summary' runs/<run_id>/result.json`.
2. **List the non-passing tests:**
   ```bash
   jq -r '.test_results[] | select(.status!="PASS") | [.id,.status,.failure_class,.title] | @tsv' "runs/$RUN_ID/result.json"
   ```
3. **Act on the classification:**

   | failure_class | First check | Probably a product bug? |
   |---|---|---|
   | `ASSERTION_FAILED` | Is the expectation backed by the OpenAPI spec or PR description, or did the AI assume it? | Possibly. Must be confirmed |
   | `UNEXPECTED_5XX` | Repeat once; check AUT logs if you have access | Likely, if reproducible |
   | `SCHEMA_MISMATCH` | Is the spec at the PR head SHA outdated, or is the implementation wrong? | Possibly |
   | `TRANSPORT_ERROR` / `TIMEOUT` | Is the AUT up? DNS? Proxy allowlist? | No, environment |
   | `ENVIRONMENT_UNAVAILABLE` | Is the AUT deployed at the PR head SHA? | No |
   | `AUTH_SETUP_FAILED` | Test token expired or env var missing | No |
   | `POLICY_BLOCKED` | Plan asked for something out of scope | No, plan issue |
   | `DEPENDENCY_FAILED` | Look at the upstream test case first | Depends on upstream |
   | `RUNNER_ERROR` | `run.log` stack trace | No, runner bug |

4. **Look at the evidence:** `runs/<run_id>/evidence/TC-xxx.*`. These files hold the sanitized request, the response, and curl's `--write-out` metadata.
5. **Reproduce:** re-run only the affected cases twice (`qa-runner run "runs/$RUN_ID" --only TC-004 --repeat 2`, planned flag). Promote a finding from `SUSPECTED` to `CONFIRMED` only when it reproduces deterministically (2/2) and the expected behaviour is documented ([QA_STRATEGY.md](./QA_STRATEGY.md)).
6. **Check the deployed version:** does the environment actually run the PR head SHA? Results from a stale deployment are `BLOCKED`, not FAIL.
7. **File it:** confirmed defects go to the target repo as an issue or PR comment. Include the run ID and sanitized evidence, never raw tokens.

Never report a bug based only on an AI explanation.

## 4. Recovering interrupted runs

A run is interrupted when `run.log` exists but `result.json` doesn't, or when an n8n execution is stuck or failed mid-way.

| Situation | Procedure |
|---|---|
| Runner container restarted mid-run | Partial evidence remains under `evidence/`. Start a **new** run ID with the same approved plan (`cp plan.json approval.json` into the new run dir). Don't append to the old run. Mark the old run with `echo interrupted > runs/<old>/INTERRUPTED` |
| n8n crashed or restarted | Open **Executions**, find the failed execution, and check which stage finished (workflow state in [WORKFLOW_DESIGN.md](./WORKFLOW_DESIGN.md)). If execution hadn't started, retry from n8n. If it had, follow the row above |
| Approval pending for days | Approvals expire after 72 h `[Recommendation]`. The workflow marks the run `BLOCKED`. Re-request approval with a fresh validation |
| Duplicate runs for one PR head SHA | Keep the newest complete one. Lock file `runs/.locks/<project>-pr<N>-<sha7>.lock` prevents this from Phase 9 on; remove stale locks older than the level's max runtime + 10 min |
| Test data left behind | Follow [§7](#7-cleaning-up-test-data) |

Re-runs are safe because tests are non-destructive by default and created entities carry the `qa-prqa-` prefix. Destructive plans need a fresh `approval.json` (`destructive_allowed: true`) for every run.

## 5. Rotating credentials

Rotate on a schedule (every 90 days `[Recommendation]`), when someone with access leaves, and immediately after any suspected leak.

| Credential | Where it lives | Rotation steps | Verify |
|---|---|---|---|
| GitHub fine-grained PAT (`GH_TOKEN`) | `compose/.env` | GitHub → Settings → Developer settings → Fine-grained tokens → regenerate (same read-only scopes) → update `.env` → `$DC up -d qa-runner` → revoke the old token | `$DC exec qa-runner gh api user --jq .login` |
| Discord webhook | n8n credential and/or `compose/.env` | Channel settings → Integrations → Webhooks → delete old, create new → update the credential/env → restart | `qa-runner notify <run> --dry-run`, then a test message |
| `QA_RUNNER_SHARED_TOKEN` | `.env` (n8n and runner) | `openssl rand -hex 32` → update `.env` → `$DC up -d` (both services) | n8n health call returns 200; the old token returns 401 |
| Test-account tokens `QA_<PROJECT>_<PROFILE>_TOKEN` | `.env` | Re-issue in the AUT's identity provider (test tenant only) → update `.env` → restart runner | Smoke run: no `AUTH_SETUP_FAILED` |
| `N8N_ENCRYPTION_KEY` | `.env` + offline backup | **Don't rotate casually.** Changing it makes stored credentials unreadable. Export workflows, re-create the credentials under the new key, and follow the n8n documentation `[Verify]` | n8n starts; credentials test OK |

After rotating, search recent artifacts for the old value (`grep -rF "<first-8-chars>" runs/ | head`). A hit means redaction failed: treat it as an incident ([§9](#9-incident-response)).

## 6. Troubleshooting integrations

### 6.1 GitHub

| Symptom | Check |
|---|---|
| 401 | Token expired or revoked: `gh auth status` |
| 403 / 404 on a known repo | Fine-grained token doesn't include that repo, or lacks *Pull requests: read* |
| 403 with `rate limit` | `gh api rate_limit`; poll less often (Phase 9 polling interval) |
| Wrong PR or branch analysed | Compare `pr.head_sha` in the plan with `gh pr view N --json headRefOid`. The runner refuses mismatches |
| OpenAPI file not found | `openapi.path` in `projects.yaml`; file may not exist at head SHA |

### 6.2 n8n

| Symptom | Check |
|---|---|
| HTTP Request to runner fails `ECONNREFUSED` | Runner not running, or not on `qa_internal`: `$DC ps` |
| 401 from runner | `X-Runner-Token` header doesn't match `QA_RUNNER_SHARED_TOKEN` |
| Executions disappear | Execution pruning settings `[Verify]` (`EXECUTIONS_DATA_PRUNE`) |
| Workflow changes lost | Not exported to `n8n/workflows/`; see [§10](#10-backups-and-routine-maintenance) |

### 6.3 Runner and application under test

| Symptom | Check |
|---|---|
| All tests `ERROR`/`TRANSPORT_ERROR` | AUT down; DNS alias; proxy profile; `allowed_hosts` |
| `POLICY_BLOCKED` on every case | Wrong `--project`; path prefixes; method not allowed |
| `AUTH_SETUP_FAILED` | Env var name doesn't match `token_env` in config |
| Timeouts on some cases | AUT slow; `request_timeout_seconds`; don't raise it beyond the level budget |

### 6.4 Discord

See [§8](#8-handling-failed-notifications).

## 7. Cleaning up test data

Rules ([QA_STRATEGY.md](./QA_STRATEGY.md), [SECURITY.md](./SECURITY.md)):

- Tests run only against non-production environments with test accounts.
- Every created entity uses the project's `test_data_prefix` (default `qa-prqa-`).
- The runner records every created resource ID in `result.json` (`created_resources[]`), then deletes them in reverse order when `cleanup.required: true`.

Manual cleanup after an interrupted run or a failed automatic cleanup:

```bash
# 1. List what the run created (sanitized IDs only)
jq -r '.created_resources[]? | [.type,.id,.cleanup_status] | @tsv' "runs/$RUN_ID/result.json"

# 2. Delete leftovers with the runner (planned; uses the same allowlist and auth profile)
qa-runner cleanup "runs/$RUN_ID" --project acme-orders --dry-run
qa-runner cleanup "runs/$RUN_ID" --project acme-orders
```

- Never clean up by running hand-written `DELETE` loops against a shared environment without listing the targets first.
- If the environment supports resets (`strategy: environment_reset`), use the environment owner's documented reset procedure. The runner doesn't do this.
- Write down leftovers you couldn't delete in the report, and tell the environment owner.

## 8. Handling failed notifications

A notification failure **never** changes the test result. The report on disk is the source of truth.

| Symptom | Likely cause | Action |
|---|---|---|
| HTTP 404 from Discord | Webhook deleted or rotated | Rotate ([§5](#5-rotating-credentials)) |
| HTTP 401/403 | Malformed webhook URL | Check that the credential value is intact |
| HTTP 429 | Rate limited | Respect `Retry-After`; the workflow retries up to 3 times with backoff ([WORKFLOW_DESIGN.md](./WORKFLOW_DESIGN.md)) |
| HTTP 400 | Payload too large or invalid (content/embed limits) `[Verify]` Discord limits | The runner truncates the summary; check `qa-runner notify --dry-run` output |
| Timeout | Network or egress issue | Retry later |

Re-send manually:

```bash
qa-runner notify "runs/$RUN_ID" --dry-run   # inspect: no secrets, no full responses
qa-runner notify "runs/$RUN_ID"
```

The runner writes `runs/<run_id>/notification.json` with the delivery status. If delivery is still failing after retries, the n8n error branch logs it and the run stays `notified: false`.

## 9. Incident response

### 9.1 Severity of operational incidents

| Level | Examples |
|---|---|
| SEV-A | Secret leaked (in Discord, a report, a public repo); requests sent to production or an unauthorized host; destructive action on shared data |
| SEV-B | Redaction gap found in local reports only; runner escaped its policy (bug) without external impact |
| SEV-C | Repeated failed runs, notification outage, n8n data corruption with backups available |

### 9.2 Procedure

1. **Contain**
   - Stop automation: deactivate the n8n triggers (polling or webhook workflows), then `$DC stop qa-runner`.
   - For a leaked secret, **revoke it first** ([§5](#5-rotating-credentials)) and investigate afterwards.
   - For a leaked Discord message, delete the message and rotate the webhook.
2. **Preserve evidence:** copy the affected `runs/<run_id>/` directory and the n8n execution export to a restricted location. Don't edit them.
3. **Assess:** what was exposed or affected, where it went, and when. Check `run.log`, the n8n execution history, and the Discord channel.
4. **Notify:** tell the repository or environment owners. For unauthorized-target incidents, tell the owner of that target.
5. **Eradicate:** fix the root cause (allowlist entry, redaction pattern, validator rule). Add a regression test to the runner's own test suite.
6. **Recover:** re-enable automation step by step: manual run → polling → webhooks.
7. **Post-incident review:** blameless write-up within 5 working days. Add an ADR in [DECISIONS.md](./DECISIONS.md) if a design change follows.

### 9.3 Kill switch

- Set `enabled: false` for the project in `config/projects.yaml`. The runner refuses to run it.
- Deactivate all n8n workflows tagged `prqa`.
- `$DC stop qa-runner`. Without the runner nothing can execute requests.

## 10. Backups and routine maintenance

| Task | Frequency | How |
|---|---|---|
| Export n8n workflows to git | After every change | n8n UI → Download, or `$DC exec n8n n8n export:workflow --all --separate --output=/home/node/.n8n/exports/` `[Verify]` CLI flags, then copy into `n8n/workflows/` and commit (review the export for credential data; exports reference credentials by ID) |
| Back up `n8n_data` volume | Weekly | `docker run --rm -v prqa_n8n_data:/data:ro -v "$ROOT/backups":/backup alpine tar czf /backup/n8n_data-$(date +%F).tar.gz -C /data .` (stop n8n first for consistency) |
| Back up `N8N_ENCRYPTION_KEY` | Once + after change | Offline password manager. Without it, a volume backup can't decrypt credentials |
| Prune old runs | Monthly | Retention policy in [REPORTING.md](./REPORTING.md); see [LOCAL_SETUP.md §11](./LOCAL_SETUP.md#11-cleanup) |
| Update pinned images | Monthly | Read release notes, bump the tag, back up, `$DC pull && $DC up -d`, run a smoke test; roll back by restoring the previous tag ([DEPLOYMENT.md](./DEPLOYMENT.md)) |
| Rotate credentials | 90 days | [§5](#5-rotating-credentials) |
| Test restore | Quarterly | Restore the volume into a scratch compose project and check the workflows load |

Restore `n8n_data`:

```bash
$DC stop n8n
docker run --rm -v prqa_n8n_data:/data -v "$ROOT/backups":/backup alpine \
  sh -c 'rm -rf /data/* && tar xzf /backup/n8n_data-YYYY-MM-DD.tar.gz -C /data'
$DC start n8n
```

The volume name is `<compose project>_n8n_data`, which is `prqa_n8n_data` with `name: prqa`. Confirm it with `docker volume ls`.
