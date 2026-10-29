# GitHub Actions → PRQA onboarding guide

Add PRQA to any GitHub repository with **one workflow file and two secrets**. The workflow sends a signed test
request to the PRQA receiver; PRQA runs its pipeline (policy gate → PR context → prompt → QA job → result validation)
and posts the **validated** result to Discord (and optionally as a PR comment).

> Status (2026-10-22): implemented and verified **locally with mocks only** (worker unit tests, generated-workflow
> checks, synthetic end-to-end run through n8n). No real repository, GitHub secret, Discord channel or public URL has
> been configured. The stack still runs in `AUTOMATION_MODE=review_only`, so a real repository's request is accepted,
> recorded and then **blocked** by the policy gate until Phase 2 is approved. Nothing here is "live" until a separately
> approved end-to-end test confirms it.

> **Per-repository `to be test` trigger (recommended for new repositories):** `scripts/setup/prqa_connect_repository.py`
> connects one repository with its own signing secret and generates `.github/workflows/prqa-to-be-test.yml`, which
> dispatches automatically on the exact label `to be test` (opened / labeled / reopened / synchronize / ready_for_review).
> See [REPOSITORY_CONNECTION_SETUP.md](REPOSITORY_CONNECTION_SETUP.md). The generic `prqa.yml` below stays supported.

## 1. What happens, step by step

| Step | Where | Meaning of success |
|---|---|---|
| 1. Label a PR `to be test` (or *Run workflow* with a PR number) | GitHub | the Actions job starts; no checkout, no PR code runs |
| 2. The job resolves the PR via the GitHub API, builds and **signs** the request, POSTs it | GitHub runner | HTTP **202 = request accepted** (a green job never means "passed") |
| 3. `PRQA 01 · GitHub Webhook Receiver`, path `/webhook/prqa/actions` → worker `/webhook/actions/intake` | PRQA (n8n + worker) | signature, timestamp window, schema, repository registration, idempotency key verified; key claimed |
| 4. `PRQA 00 · Main Pipeline` → policy gate | PRQA | `allowed` / `synthetic` → stages 02–07; `blocked` → BLOCKED notice on Discord, no test |
| 5. Stage 04 creates the **QA job** and polls the worker until `succeeded` / `failed` | PRQA | job created ≠ job completed; the pipeline waits |
| 6. Stage 05 validates the result (schema + count reconciliation) | PRQA | invalid results become an honest **INCONCLUSIVE** |
| 7. Stage 06 hands the validated result to the worker → Discord | PRQA | `delivered` / `failed` recorded on the job; a Discord failure never changes the result |
| 8. Stage 07 optional GitHub PR comment | PRQA | needs `GITHUB_TOKEN` with *Issues: write* on the PRQA side, not in the workflow |

Six separate states are tracked and never conflated: webhook **accepted** → job **created** → job **running** →
job **completed** → validation **completed** → Discord delivery **completed**.

## 2. Copy the workflow file

Copy [`.github/workflows/prqa.yml`](../.github/workflows/prqa.yml) **unchanged** into the target repository at the same
path. It is self-contained (the client is embedded; the canonical source is `scripts/clients/prqa_actions_client.py`, and
`python3 scripts/build/build_actions_workflow.py --check` proves the two are identical).

Properties of the workflow:

- Triggers: `workflow_dispatch` (inputs `pr_number` **required**, `test_level`, `force_rerun`) and
  `pull_request: [labeled]` filtered to the opt-in label.
- Permissions: `contents: read`, `pull-requests: read` only. PRQA posts the optional PR comment with **its own** token,
  so the workflow never needs write scope.
- No `actions/checkout`, no `pull_request_target`: PR code is never fetched or executed.
- `concurrency` serialises overlapping label + manual triggers per PR; the second one becomes a harmless duplicate.
- Bounded HTTP: 30 s timeout per request (configurable 5–120 s), at most 3 attempts, retries only on timeouts,
  connection errors, 429 and 5xx. Secrets, signatures and `Authorization` headers are never printed.
- Fork PRs: the label trigger skips them with a notice (fork PRs do not receive repository secrets). A maintainer can
  use *Run workflow* instead; PRQA still applies its own `fork_prs_eligible` policy.

## 3. Configure the target repository

Settings → Secrets and variables → Actions.

| Kind | Name | Value | Required |
|---|---|---|---|
| Secret | `PRQA_WEBHOOK_URL` | `https://<your-prqa-host>/webhook/prqa/actions` (see §4) | yes (a repository *variable* of the same name also works) |
| Secret | `PRQA_WEBHOOK_SECRET` | the **same** value as `PRQA_WEBHOOK_SECRET` in the PRQA worker environment (`compose/.env`) | yes |
| Variable | `PRQA_TRIGGER_LABEL` | opt-in label, default `to be test` | no |
| Variable | `PRQA_TEST_LEVEL` | `smoke` \| `standard` \| `comprehensive` \| `security`, default `standard` | no |
| Variable | `PRQA_HTTP_TIMEOUT_SECONDS` | per-request timeout, default `30` | no |

Create the label `to be test` in the repository (Issues → Labels) if it does not exist.

Nothing else in the target repository changes.

## 4. Obtain the HTTPS PRQA webhook URL

The receiver runs in n8n on `127.0.0.1:5678`. **GitHub-hosted runners cannot reach `localhost`** or a private network,
and the workflow refuses non-`https://` URLs. The operator must expose the receiver over public HTTPS. Options, from
least to most effort:

| Option | How | Notes |
|---|---|---|
| Reverse proxy on a public host | Caddy / nginx / Traefik with Let's Encrypt in front of n8n, path-restricted to `/webhook/prqa/` | recommended for anything lasting; set `N8N_WEBHOOK_URL=https://<host>/` and `N8N_PROTOCOL=https` in `compose/docker-compose.yml` so n8n generates the right URLs |
| Managed tunnel | Cloudflare Tunnel / ngrok (paid plans give stable hostnames) | fine for a pilot; restrict the tunnel to the webhook path, keep the editor private |
| Self-hosted runners inside the PRQA network | run the Actions job on a runner that can reach n8n directly | the URL may then be an internal name, but it still must be `https://`; the workflow only accepts `http://127.0.0.1` / `localhost` with `PRQA_ALLOW_INSECURE_URL=1` for **local testing** |

Only the two paths `POST /webhook/prqa/github` and `POST /webhook/prqa/actions` need to be reachable from the internet;
expose nothing else (the n8n editor, the worker on `:8080` and the mocks on `:8090` stay private). Both paths are
authenticated; the receiver workflow must be **published** (`scripts/setup/n8n_api.py publish-chain PrqaWebhookRecvr`) for the
production URL to exist, which `scripts/run_synthetic_tests.sh --webhook` does only temporarily.

**This task did not expose anything.** Exposing the receiver is a separate, approved operator step.

## 5. Configure the PRQA side

In `compose/.env` (never committed; `scripts/start.sh` creates it and adds a locally generated
`PRQA_WEBHOOK_SECRET` if missing):

```
PRQA_WEBHOOK_SECRET=<openssl rand -hex 32>      # copy into the GitHub secret of the same name
PRQA_WEBHOOK_TOLERANCE_S=300                     # accepted clock skew / replay window
DISCORD_WEBHOOK_URL=                             # Discord incoming webhook of a TEST channel (see §6)
```

Then register the repository in `config/repositories.yaml` (manual, deliberate step):

```yaml
  - full_name: your-org/your-repo
    kind: real
    enabled: false                # true only in Phase 2, with approval_status: approved
    approval_status: pending_approval
    api: { environment: staging, base_url: "https://staging.example.test", openapi_path: "/openapi.json", auth_profiles: {...} }
```

An unregistered repository is refused at the receiver with **403 `REPOSITORY_NOT_REGISTERED`** (the Actions log says
so). A registered but not yet enabled/approved repository, or any real repository while `AUTOMATION_MODE=review_only`,
is accepted (202) and then **blocked** by the policy gate; the Actions log prints the policy preview
(`REVIEW_ONLY_MODE`, `REPOSITORY_PENDING_APPROVAL`, …) as a warning and Discord receives a BLOCKED notice.

Restart the stack after editing `.env` or the registry: `scripts/start.sh` (worker and n8n read them at start).

## 6. Discord

1. Discord → channel settings → Integrations → Webhooks → *New Webhook* → copy the URL. Use a **test channel** first.
2. Put the URL in `compose/.env` → `DISCORD_WEBHOOK_URL`. It is read by the worker (reports) and n8n (error-handler
   alerts). It is redacted everywhere it could be printed (`[REDACTED:discord_webhook]`).
3. Real delivery is **off by default**. The worker posts to the mock sink (`prqa-mocks`, inspect at
   `http://127.0.0.1:8090/mock/received`) until **both** hold: `AUTOMATION_MODE=live` with
   `REAL_DISCORD_REPORTING_ENABLED=true` (Phase 2 flags; forced off while review-only) **and** `DISCORD_WEBHOOK_URL`
   is set. `GET /config` on the worker shows `discord.mode` and `discord.real_delivery_possible`.

What a report contains (rendered by `worker/prqa_worker/discord.py` from the validated `qa-result`):
repository and PR number, PR title and link, tested head SHA (+ branches), overall status
(**passed / passed with known issues / failed / inconclusive**, or **blocked** for policy refusals), passed / failed /
blocked / skipped counts, bugs with severity, origin, expected vs. actual, evidence test-case ids and the sanitized
reproduction command (follow-up messages, one embed per defect), blockers, validation warnings, PRQA job id, n8n
execution id, Actions run link, and whether the prompt came from a **live model** or the **deterministic reference
fallback**. Untrusted text is sanitized (mass/user/role mentions neutralised, Markdown escaped, secrets redacted) and
`allowed_mentions` is always empty. Messages respect Discord's limits (2000 / 256 / 4096 / 1024 / 25 fields / 10 embeds
/ 6000 chars); long reports are split into at most 6 messages and the remainder is summarised.

## 7. Enable manual dispatch

The workflow already declares `workflow_dispatch`. In the target repository: Actions → *PRQA · PR QA Pilot* →
*Run workflow* → enter the PR number, choose the level, optionally tick *force_rerun* (needed to re-test a head SHA that
was already submitted). The PR must belong to that repository, be open and not a draft, otherwise the job fails before
anything is sent.

## 8. Use the `to be test` label

Add the label to an open, non-draft PR. Removing and re-adding it after new commits requests a test of the **new**
head SHA (new idempotency key). Re-adding it for the same SHA is reported as *duplicate* (job succeeds with a warning,
nothing new runs). Change the label name with the `PRQA_TRIGGER_LABEL` variable; the PRQA test level is independent of
the label (`PRQA_TEST_LEVEL`).

## 9. Test the integration safely

In order, each step without touching a real repository:

1. `scripts/run_synthetic_tests.sh` — unit tests (158, all mocked), workflow JSON validation, generated-workflow check,
   worker + mock checks including the Actions intake contract and Discord delivery to the mock sink.
2. `scripts/run_synthetic_tests.sh --webhook` — additionally publishes the chain temporarily and POSTs signed synthetic
   requests to **both** receiver paths on `127.0.0.1:5678`, waits for the pipeline executions, then unpublishes everything.
3. Dry-run the client locally against the stack (nothing leaves the machine; the synthetic fixture PR is served by the
   mocks, so point the client at the n8n receiver and the mock GitHub API):
   ```bash
   set -a; source compose/.env; set +a
   PRQA_ALLOW_INSECURE_URL=1 PRQA_WEBHOOK_URL=http://127.0.0.1:5678/webhook/prqa/actions \
   GITHUB_API_URL=http://127.0.0.1:8090/github GITHUB_REPOSITORY=synthetic-org/sample-orders-api PRQA_PR_NUMBER=101 \
   GITHUB_RUN_ID=1 GITHUB_EVENT_NAME=workflow_dispatch GITHUB_OUTPUT=/tmp/prqa-out GITHUB_STEP_SUMMARY=/tmp/prqa-summary \
   python3 scripts/clients/prqa_actions_client.py
   ```
   (requires the receiver to be published, e.g. during step 2, and the synthetic fixture PR).
4. First real repository (Phase 2, approved): configure the secrets, keep PRQA in review-only, label a PR → expect a
   green Actions job with the warning *will be BLOCKED (REVIEW_ONLY_MODE)* and a BLOCKED notice in the mock sink. Only
   then switch the flags.

## 10. Diagnose

| Symptom (Actions log) | Cause | Fix |
|---|---|---|
| `PRQA_WEBHOOK_URL is not configured` / `must be an https:// URL` | secret missing or plain http | §3, §4 |
| `HTTP 401 unauthorized SIGNATURE_INVALID` | secrets differ between GitHub and the worker | re-copy `PRQA_WEBHOOK_SECRET`; rotate (§11) |
| `HTTP 401 … TIMESTAMP_EXPIRED` | runner/worker clocks differ by more than 300 s, or the request was replayed late | fix NTP; raise `PRQA_WEBHOOK_TOLERANCE_S` only as a last resort |
| `HTTP 401 … WEBHOOK_SECRET_NOT_CONFIGURED` | worker has no `PRQA_WEBHOOK_SECRET` | §5, restart |
| `HTTP 403 forbidden REPOSITORY_NOT_REGISTERED` | repository not in the registry | §5 |
| `HTTP 422 rejected PR_DRAFT` / `PR_NOT_OPEN` / `PAYLOAD_INCONSISTENT` | PR ineligible or payload tampered/mismatched | fix the PR; a mismatch means the request was not produced by the unmodified workflow |
| `HTTP 429 rate_limited` | more than 20 requests / 10 min for the repository (or 60 global, or 30 failed auths) | wait; `PRQA_ACTIONS_RATE_LIMIT_*` in `compose/.env` |
| `HTTP 503` / `unreachable after 3 attempts` | n8n down, receiver not published, tunnel/proxy down, worker down | `scripts/start.sh`, `scripts/setup/n8n_api.py list`, check the proxy; the client already retried |
| `::warning:: already has a request … duplicate` | same repo/PR/SHA/level already submitted | expected; `force_rerun=true` for a new run |
| `::warning:: will be BLOCKED (REVIEW_ONLY_MODE …)` | PRQA policy | expected until Phase 2 flags are set |
| Job green but nothing on Discord | request blocked, pipeline failed, Discord in mock mode, or delivery failed | worker `GET /jobs/<id>` → `notifications.discord`, `GET /events`; n8n executions of PRQA 00; `http://127.0.0.1:8090/mock/received` in mock mode |
| Discord `mock_failed` / `real_failed` / `real_partial` | webhook URL invalid (404), payload rejected (400), rate-limited beyond 3 attempts | per-message `status_code`s are recorded on the job; the result is unchanged; re-run Stage 06 or call `POST /notifications/discord` again (a failed delivery can be retried, a delivered one is skipped) |
| Duplicate Discord messages | should not happen: the worker claims `notifications.discord` before sending | if the worker was restarted mid-send, a `sending` claim expires after 15 min |

The worker never logs the secret, signatures, request bodies or the Discord URL; it logs reason codes, repository, PR,
level and the idempotency key.

## 11. Rotate secrets without exposing them

- `PRQA_WEBHOOK_SECRET`: generate a new value (`openssl rand -hex 32`), put it in `compose/.env`, restart the worker
  (`scripts/start.sh --no-build`), then update the GitHub secret. Requests signed with the old value fail with 401
  during the switch (seconds). Never paste the value into an issue, a log or the workflow file.
- `DISCORD_WEBHOOK_URL`: create a new webhook in Discord, update `.env`, restart, delete the old webhook in Discord.
- `PRQA_WORKER_TOKEN`, `GITHUB_WEBHOOK_SECRET`, `N8N_ENCRYPTION_KEY`: unchanged procedure (see `docs/archive/OPERATIONS_RUNBOOK.md`).

Secrets live only in `compose/.env` (chmod 600, git-ignored) or `~/.config/prqa/secrets.env`, and in GitHub's encrypted
secrets. The generated n8n JSON, the workflow YAML and the repository contain none.

## 12. Request contract (for reference)

`POST /webhook/prqa/actions`, `Content-Type: application/json`

| Header | Value |
|---|---|
| `X-PRQA-Timestamp` | unix seconds when the request was signed |
| `X-PRQA-Signature` | `sha256=` + hex HMAC-SHA256(`PRQA_WEBHOOK_SECRET`, `"<timestamp>.<raw body bytes>"`) |
| `X-PRQA-Idempotency-Key` | optional; must equal `gha:<owner/repo>#<pr>@<head_sha>:<level>` (+ `:run-<run_id>` with `force_rerun`), which the worker derives itself |

Body: [`schemas/actions-request.schema.json`](../schemas/actions-request.schema.json) — `repository.full_name`,
`pull_request{number,title,body,state,draft,html_url,head{ref,sha,repo},base{ref,sha,repo}}`,
`request{test_level,trigger,label,requested_by,force_rerun}`, `actions{run_id,run_attempt,run_url,…}`. The PR sub-object
mirrors the GitHub webhook subset the receiver already accepted. The worker re-derives the PR URL and rejects any
disagreement between `repository.full_name`, `pull_request.html_url`, `base.repo.full_name` and the numbers.

Responses: `202 accepted` (`job_seed`, `idempotency_key`, `policy_preview`, `receiver_execution_id`), `200 duplicate`,
`401 unauthorized`, `403 forbidden`, `422 rejected`, `429 rate_limited`, `503` worker unavailable — all JSON with
`outcome`, `reason_code`, `reason`.

## 13. Known limitations

- **Asynchronous completion.** The Actions job ends at "accepted". There is no callback or status endpoint for GitHub
  yet; the result is delivered to Discord / the PR comment. Tracing: `receiver_execution_id` (n8n) and the idempotency
  key (worker events, `/jobs`).
- **Public HTTPS reachability** is an operator decision and was not performed (§4).
- **Review-only mode** blocks every real repository by design; the integration can only be exercised end-to-end with
  the synthetic fixture until Phase 2 is approved.
- One shared `PRQA_WEBHOOK_SECRET` for all repositories (per-repository secrets are a possible extension).
- Rate limits are in-process (reset on worker restart) and keyed by repository, not by source IP (n8n does not forward
  it reliably).
- Discord attachments are not used; long reports are split into messages and the full Markdown report stays on the
  worker (`/jobs/<id>`) and in the GitHub comment.
