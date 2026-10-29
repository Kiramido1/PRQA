# Repository connection setup — automatic `to be test` trigger

Connect a GitHub repository **once**; afterwards every eligible pull request carrying the exact label `to be test`
is dispatched to PRQA automatically by a generated GitHub Actions workflow. No new frontend: one CLI on the PRQA host.

> Status (2026-10-27): implemented and verified **offline only** (unit/integration tests, generated-workflow checks,
> worker rebuilt). No public HTTPS endpoint, GitHub read credential or real repository is configured. The stack runs in
> `AUTOMATION_MODE=review_only`: a real repository's dispatch is authenticated, recorded and answered
> `200 blocked / REVIEW_ONLY_MODE`; nothing executes.

## 1. Architecture

```
PR event (opened/labeled/reopened/synchronize/ready_for_review)
  └─ .github/workflows/prqa-to-be-test.yml  (pull_request, permissions: {}, no checkout, 5 min)
       client reads $GITHUB_EVENT_PATH → exact label rule → schema-2.0 payload → HMAC-SHA256(connection secret)
  └─ HTTPS ingress (operator-provided reverse proxy, ONLY POST /webhook/prqa/actions)
  └─ n8n PRQA 01 "GitHub Actions Webhook" → worker POST /webhook/actions/intake  (raw bytes + X-PRQA-* headers)
       worker: size → connection → HMAC (±300 s, constant time) → action/label → schema → repository binding
               → delivery replay → head-SHA idempotency → rate limit → open/not draft → registry
               → GitHub read credential → policy (review-only = blocked) → claim → 202 job_seed
  └─ PRQA 00 Main Pipeline (unchanged): policy gate, context collector (read-only GitHub), prompt (Groq via
     model proxy, budget ledger, 3000 tokens, gate + rubric 85, deterministic fallback), job, validation, report
```

| Component | File |
|---|---|
| Connection registry, ingress-URL validation | `worker/prqa_worker/actions_connections.py` |
| Receiver (schema 2.0 path) | `worker/prqa_worker/actions_intake.py` (`_handle_connection`) |
| Payload schema | `schemas/actions-connection-request.schema.json` |
| Workflow client (embedded verbatim) | `scripts/clients/prqa_to_be_test_client.py` |
| Setup CLI / package generator | `scripts/setup/prqa_connect_repository.py` |
| n8n route for the new outcomes | `scripts/build/build_workflows.py` → `n8n/workflows/01-prqa-github-webhook-receiver.json` |
| Tests | `tests/test_repo_connection_to_be_test.py` |

## 2. Connect a repository (PRQA host)

```bash
python3 scripts/setup/prqa_connect_repository.py connect OWNER/REPO \
    [--webhook-url https://YOUR-INGRESS/webhook/prqa/actions] [--register-repository]
```

* Creates or reuses the connection: a non-secret id `prqa_c_<24 hex>` and a 256-bit CSPRNG secret, stored in
  `~/.config/prqa/actions-connections/connections.json` (dir 750, file 640, outside the project drive).
* Records `PRQA_ACTIONS_CONNECTIONS_DIR` / `PRQA_ACTIONS_CONNECTIONS_GID` in `~/.config/prqa/secrets.env`; compose mounts
  the directory read-only into `prqa-worker` at `/run/prqa-connections` (worker re-reads it on change).
  Without provisioning, an empty tracked placeholder is mounted and every dispatch is rejected `CONNECTION_UNKNOWN`.
* Validates the endpoint: `https://` only, a public DNS name or public IP, path exactly `/webhook/prqa/actions`, no
  credentials/query/fragment, no internal service ports. Rejected: http (except `--local-test` against
  127.0.0.1/localhost), localhost, loopback/private/link-local/CGNAT addresses, `.local/.internal/.test/.example/.invalid`,
  `example.com/org/net`, compose service names (`n8n`, `prqa-*`), the n8n editor/REST/test-webhook paths.
  Without a URL the package is generated with endpoint status **unconfigured**.
* `--register-repository` appends a **disabled, pending_approval** entry to `config/repositories.yaml`.
* Writes the package to `runs/repo-connection-packages/OWNER__REPO/`: the workflow, `INSTALL.md`, `connection.json`.
  **No package file contains the secret.**
* Never: creates a tunnel, changes network isolation, contacts GitHub, pushes code, activates n8n workflows.

Other commands: `export-secret OWNER/REPO` (stdout only when piped; refuses a terminal), `status`, `revoke`,
`connect --rotate-secret`.

## 3. Install in the repository

| Name | Kind | Secret? |
|---|---|---|
| `PRQA_WEBHOOK_SECRET` | Actions **secret** | **SECRET** — `export-secret OWNER/REPO \| gh secret set PRQA_WEBHOOK_SECRET --repo OWNER/REPO` |
| `PRQA_WEBHOOK_URL` | Actions **secret** | **SECRET** (keeps the ingress host private) — `gh secret set PRQA_WEBHOOK_URL --repo OWNER/REPO` |
| `PRQA_CONNECTION_ID` | Actions **variable** | not secret — `gh variable set PRQA_CONNECTION_ID --body prqa_c_…` |
| `.github/workflows/prqa-to-be-test.yml` | committed file | not secret |

**Never commit the secret.** Then create the label `to be test` (exact spelling).

## 4. Event policy

| Event | Dispatch? |
|---|---|
| `opened` with `to be test` already on the PR | yes |
| `labeled` with exactly `to be test` (case-sensitive) | yes; `To Be Test`, `to-be-test`, other labels: no |
| `reopened` / `synchronize` / `ready_for_review` while labelled | yes |
| `unlabeled`, `edited`, `closed`, any other action | no (`unlabeled` is not even subscribed) |
| draft, closed, fork PR | no (fork PRs get no secrets) |

Idempotency: one run per connection + PR + head SHA (`conn:<id>#<pr>@<sha>`). `opened` + `labeled` on the same commit is
one job (`200 duplicate`); a new commit (`synchronize`) is a new run. Every delivery id (`gha-<run id>-<attempt>`) is
recorded: a replay is `409 DELIVERY_REPLAYED`. Removing the label dispatches nothing new.

## 5. Signed payload (schema 2.0)

Headers `X-PRQA-Timestamp` (unix s) and `X-PRQA-Signature: sha256=HMAC-SHA256(secret, "<timestamp>.<raw body>")`.
Body fields: `schema_version`, `type`, `connection_id`, `repository{full_name,id}`, `pull_request{number,state,draft,
head{sha,ref,repo_full_name},base{sha,ref}}`, `event{name,action,label,sender_login (untrusted)}`, `delivery{id,
created_at}`, `actions{run_id,run_attempt,workflow,client_version}`. No title, body, files or credentials; extra fields
are rejected. Max 16 KiB. The connection id only selects the secret — it authenticates nothing on its own.

## 6. Receiver responses

| HTTP | outcome / reason_code | Meaning |
|---|---|---|
| 202 | accepted / ACCEPTED | job_seed handed to the Main Pipeline (accepted ≠ passed) |
| 200 | duplicate / DUPLICATE_REQUEST | same PR head SHA already dispatched |
| 200 | blocked / REVIEW_ONLY_MODE (or another policy code) | recorded, never executed |
| 401 | unauthorized / CONNECTION_UNKNOWN, CONNECTION_ID_MALFORMED, SIGNATURE_*, TIMESTAMP_* | authentication failed |
| 403 | forbidden / REPOSITORY_MISMATCH, REPOSITORY_NOT_REGISTERED | wrong repository for the connection / not onboarded |
| 409 | replayed / DELIVERY_REPLAYED | delivery id already received |
| 413 | too_large / BODY_TOO_LARGE | body > 16 KiB |
| 422 | rejected / ACTION_NOT_ELIGIBLE, LABEL_NOT_ELIGIBLE, PAYLOAD_INVALID, PAYLOAD_INCONSISTENT, PR_DRAFT, PR_NOT_OPEN, DELIVERY_TIMESTAMP_MISMATCH | invalid dispatch |
| 429 | rate_limited | per-repository / global / auth-failure limits |
| 503 | unavailable / GITHUB_READ_CREDENTIAL_MISSING | real repository but no read-only `GITHUB_TOKEN`: fail closed, nothing claimed |

## 7. Reachability (operator decision)

GitHub-hosted runners need a public HTTPS endpoint. Expose **only** `POST /webhook/prqa/actions` through a reverse proxy
with TLS. Never expose the n8n editor or REST API, the worker port (8080), the mocks, the model proxy or its admin
socket, or the Docker socket. This project never creates a tunnel automatically. The n8n receiver workflow must be
activated in a separately approved Phase-2 step for the public path to work.

## 8. Verification and rollback

Verify: label a test PR → job "PRQA dispatch accepted" (or "recorded, not executed" while review-only); an unrelated
label → "nothing dispatched". Rollback: delete the workflow file, delete the two secrets and the variable, then
`prqa_connect_repository.py revoke OWNER/REPO` (immediately `401 CONNECTION_UNKNOWN`).
