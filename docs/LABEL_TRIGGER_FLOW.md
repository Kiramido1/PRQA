# Automatic PR-label-triggered testing (`to be test`)

Status (2026-10-22): implemented and verified **locally with mocks and the synthetic fixture only**. The stack stays
`AUTOMATION_MODE=review_only`; all n8n workflows are inactive; no real repository, Discord channel, LLM or public URL is
involved. Each transition below is marked with how it was verified. Nothing is "live" until a separately approved
end-to-end test confirms it.

## 1. End-to-end event flow

| # | Transition | Where | Verified by |
|---|---|---|---|
| 1 | GitHub sends `pull_request` with `action: labeled`, label = `PRQA_TRIGGER_LABEL` (default `to be test`; `qa-test:*` still accepted) | GitHub → receiver | synthetic signed payloads (`scripts/eval/synthetic_pipeline_test.py`), unit tests |
| 2 | `PRQA 01 · GitHub Webhook Receiver` verifies `X-Hub-Signature-256` over the raw body (`GITHUB_WEBHOOK_SECRET`, constant-time), dedupes `X-GitHub-Delivery` on the worker | n8n Code node + worker `/deliveries/claim` | existing tests + synthetic e2e |
| 3 | `Validate Event & Extract PR` maps the label to a test level, checks open / non-draft / SHA format / repository format, extracts repo, PR number, base + head branch, exact head SHA | n8n Code node, mirrored by `validation.evaluate_event` | `tests/test_label_trigger.py::LabelEventTests` (worker rule), structural test of the n8n JSON |
| 4 | `PRQA 00 · Main Pipeline` → policy gate: registered + approved repository, fork rule, review-only (real repositories are **blocked** until Phase 2; the requester gets a BLOCKED notice on Discord) | worker `/config` + Code node | existing synthetic e2e (blocked branch) |
| 5 | Stage 02 fetches PR metadata, changed files and diff from the GitHub API (mock in review-only) and the worker builds the sanitized context (`/context/build`); Stage 03 builds the prompt | existing | existing tests + synthetic e2e |
| 6 | Stage 04 `POST /jobs` (second dedupe: one active job per `repo#pr@sha:level`); the worker **creates an isolated workspace** `<data>/workspaces/<job_id>` and the pinned checkout plan; live jobs may run the checkout (§3) | worker `executor.prepare_workspace` | `ExecutorWorkspaceTests`, `WorkspaceTests` (incl. real local git) |
| 7 | Stage 04 polls `GET /jobs/<id>` every 10 s until `succeeded` / `failed` / `cancelled` (bounded, 360 polls) | existing + new `cancelled` route | `ReceiverStructureTests.test_dispatcher_treats_cancelled_as_terminal` |
| 8 | Stage 05 validates (schema + count reconciliation); invalid → INCONCLUSIVE | existing | existing tests |
| 9 | Stage 06 hands the validated result to the worker → Discord (mock sink by default) | existing | `tests/test_discord_reporting.py` |

Final states: **PASSED** (`PASS`, `PASS_WITH_KNOWN_ISSUES`), **FAILED**, **INCONCLUSIVE**, **BLOCKED** (policy), plus
**cancelled** (never reported). HTTP 202 from the receiver only means *accepted*.

## 2. Trigger behaviour

| Event | Behaviour |
|---|---|
| `labeled` with the opt-in label on an open, non-draft PR | request accepted → pipeline; level = `PRQA_DEFAULT_TEST_LEVEL` (`standard`) |
| `labeled` with `qa-test:<level>` | same, explicit level |
| same event redelivered by GitHub | `X-GitHub-Delivery` already claimed → 200 duplicate, nothing runs |
| label applied again while a job for that SHA is active | `POST /jobs` → 409 `DUPLICATE_JOB`, visible *Stop* in Stage 04, nothing new runs |
| label applied again after the job finished | a new run (this is the intended "re-test" gesture) |
| `unlabeled` (opt-in or qa-test label removed) | **cancel** every queued/running job of that PR (`POST /jobs/cancel`, reason `LABEL_REMOVED`); queued → `cancelled` at once, running → outcome discarded when the attempt ends; nothing is reported |
| `closed` | cancel active jobs (`PR_CLOSED`) |
| `synchronize` (new commits) | cancel active jobs whose head SHA is **older** than the new one (`SUPERSEDED`); the new SHA is **not** tested automatically — remove and re-add the label (or use the Actions `workflow_dispatch`) |
| draft PR, closed PR, other actions, non-PRQA labels | acknowledged with 200 and ignored |

Cancelled jobs are terminal: the dispatcher stops with *Job Cancelled*, and `POST /notifications/discord` answers 409
`JOB_NOT_COMPLETED` (never reported as PASS). Configure the label with `PRQA_TRIGGER_LABEL` (read by both n8n and the
worker from `compose/.env`); both must agree, which `compose/docker-compose.yml` guarantees.

## 3. Isolated workspace and pinned checkout

`worker/prqa_worker/workspace.py`:

- One directory per job under `PRQA_WORKSPACE_ROOT` (`/data/workspaces` in the worker volume), mode 0700, named by
  the validated job id only. Path traversal, symlinks and unmarked directories are refused; two jobs never share a
  directory. Every workspace carries `.prqa-workspace.json`.
- `build_checkout_plan` derives the remote URL from the **validated repository name** (`https://github.com/<owner>/<repo>.git`,
  never a caller-supplied URL), pins `fetch --depth 1 origin <40-hex sha>` + `checkout --detach FETCH_HEAD`, disables hooks
  (`core.hooksPath` → empty dir, `init.templateDir`), submodules, credential helpers, prompts, system/user config and every
  protocol except https. The plan is written next to the marker (`checkout-plan.json`) and contains no secret.
- `run_checkout` executes the plan step by step without a shell, with one bounded deadline (`PRQA_CHECKOUT_TIMEOUT_S`, 300 s),
  verifies `HEAD == sha`, and redacts the token from errors. The token travels only through `GIT_CONFIG_*` environment
  variables (never argv, never on disk). Repository code is **not executed**: no hooks, no submodule init, no install, no build.
- Review-only / mock jobs only **record** the plan (state `planned`). A real checkout runs only for `live` jobs with
  `PRQA_WORKER_CHECKOUT_ENABLED=true`; the tree is then packed into a per-job artifact (`checkout.tar`, no `.git`, with a
  manifest naming job, repository and SHA) that the launcher streams into the sandbox with `docker cp -` before the
  container starts. The sandbox never clones and never receives a GitHub token; it verifies the artifact fail-closed
  (`--prechecked`). Transport, credentials and limits: [SANDBOX_TRUST_BOUNDARY.md](./SANDBOX_TRUST_BOUNDARY.md).
- Retention: after each job the marker is set to `released`; a sweep removes **marked** workspaces older than
  `PRQA_WORKSPACE_RETENTION_HOURS` (24). `GET /workspaces` lists them, `POST /workspaces/cleanup` forces a sweep. Unmarked
  directories and symlinks are never touched.
- Executing anything from the repository (tests, builds, package hooks) remains the sandbox's separately approved policy
  (`worker/sandbox/`), untouched by this change.

## 4. Reaching the local receiver from GitHub

The receiver listens on `127.0.0.1:5678` only. Two supported options; neither was performed by this change.

**A. Inbound HTTPS tunnel / reverse proxy (push).** Expose exactly `POST /webhook/prqa/github` (and
`/webhook/prqa/actions`) over HTTPS, keep the editor, worker `:8080` and mocks `:8090` private, set `N8N_WEBHOOK_URL` /
`N8N_PROTOCOL=https` in the compose file, publish the receiver chain (`scripts/setup/n8n_api.py publish-chain PrqaWebhookRecvr`),
then create the repository webhook on GitHub (content type JSON, secret = `GITHUB_WEBHOOK_SECRET`, event *Pull requests*).
Options and trade-offs are in `docs/GITHUB_ACTIONS_ONBOARDING.md` §4. Managed by you; PRQA does not start tunnels.

**B. Local polling (pull, no inbound exposure).** `scripts/pipeline/prqa_label_poller.py` reconciles the label state of open
PRs with a small local state file and submits only *changes* to the local receiver through the authenticated Actions
contract (`source: local_poller`, idempotency key `poll:<repo>#<pr>@<sha>:<level>`), so signature, replay protection,
repository allowlist and rate limits are reused unchanged. Exact repeats are answered as `duplicate` by the worker
*before* consuming rate-limit capacity.

```bash
set -a; source compose/.env; set +a            # PRQA_WEBHOOK_SECRET, PRQA_TRIGGER_LABEL, poller policy variables
export GITHUB_TOKEN=<read-only PAT: Pull requests, Metadata, Issues (read)>  PRQA_POLL_REPOSITORIES=owner/repo
python3 scripts/pipeline/prqa_label_poller.py --dry-run   # shows the reconciliation plan; sends nothing, writes no state
python3 scripts/pipeline/prqa_label_poller.py             # one pass; schedule it yourself (cron / systemd timer)
```

| Observation | Poller action |
|---|---|
| label present, not yet submitted for this SHA | submit (`label`) |
| label present, same SHA already submitted | nothing (no request) |
| label gone or PR closed | one signed `unlabel` request → worker cancels active jobs of the PR |
| label removed then re-added on the same SHA | `PRQA_POLL_RERUN_ON_RELABEL=true` (default): submit with `force_rerun` (new run; the worker's one-active-job rule still applies); `false`: ignore |
| label kept, new commits pushed | `PRQA_POLL_RETEST_ON_NEW_SHA=false` (default, webhook semantics): one `unlabel` to cancel older-SHA jobs, then wait for a re-label; `true`: also submit the new SHA |
| label added by someone outside `PRQA_TRIGGER_ALLOWED_ACTORS` | skipped (actor resolved from the GitHub issue-events API, forwarded as `labelled_by`) |
| GitHub API 5xx / 429 / secondary limit / network error | bounded retries (4 attempts) with exponential backoff + full jitter, `Retry-After` / rate-limit reset honoured; the repository is skipped for this pass on final failure |
| process restart, lost state file | nothing is resubmitted: the worker answers `duplicate` for keys it already claimed |
| submission or cancel fails (receiver 5xx, unreachable) | the PR stays `pending_rerun` / label-present in the state; the next pass retries under the same policy. A PR counts as submitted only when the receiver answered `accepted` or `duplicate` (cancel: `cancelled`) |

State file (`PRQA_POLL_STATE_FILE`, default `runs/poller-state.json`, atomic replace): PR numbers, SHAs, outcomes and
timestamps only; never a token or secret. Pagination: 100 per page, up to 10 pages. Trade-offs: latency equals the
polling interval; the token must be able to list PRs and issue events; the receiver workflow must be published for the
loopback URL to exist. The poller never runs in the background on its own.

**C. GitHub Actions** (`.github/workflows/prqa.yml`) remains available as the manual-dispatch path and as a push
alternative that also needs option A.

## 5. Registering a repository

```yaml
  - full_name: owner/repo
    kind: real
    enabled: false                   # Phase 2: true, with approval_status: approved, approved_by, approved_at
    approval_status: pending_approval
    api: { environment: staging, base_url: "https://staging.example.test", openapi_path: "/openapi.json", auth_profiles: {...} }
```

Restart the worker (`scripts/start.sh --no-build`). Unregistered repositories are blocked by the policy gate (GitHub
path) or refused with 403 at the receiver (Actions/poller path). While review-only, a registered real repository is
accepted and then **blocked**, with a BLOCKED notice on the mock Discord sink.

## 6. Verification and what is still manual

Verified offline: 203 unit tests (`tests/`), 9/9 workflow files valid, generated Actions workflow in sync, and the
worker/mock synthetic checks (`scripts/run_synthetic_tests.sh`, without `--webhook`). Not verified: a real GitHub webhook
delivery, a real checkout from github.com (only the local file:// bare-repository test), live OpenCode execution, real Discord.

Manual steps remaining: choose option A or B above; register the repository; set `GITHUB_TOKEN` (read-only) for the
poller or for a live checkout; configure Discord (`docs/GITHUB_ACTIONS_ONBOARDING.md` §6); and, only after approval,
Phase 2 flags (`AUTOMATION_MODE=live`, `REAL_*`, `PRQA_WORKER_CHECKOUT_ENABLED`).
