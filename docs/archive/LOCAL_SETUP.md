# Local Setup (Debian 13)

How to prepare a Debian 13 workstation to develop and run PR QA Pilot locally: prerequisites, Docker Compose layout, environment variables, startup/shutdown, troubleshooting and cleanup.

> Status: Planned — documentation only (v0.1, 2026-10-18)

## Contents

1. [Scope and assumptions](#1-scope-and-assumptions)
2. [Prerequisites](#2-prerequisites)
3. [Installation and verification](#3-installation-and-verification)
4. [Directory layout](#4-directory-layout)
5. [Docker Compose configuration](#5-docker-compose-configuration)
6. [Environment variables](#6-environment-variables)
7. [Startup](#7-startup)
8. [Shutdown](#8-shutdown)
9. [Verification checklist](#9-verification-checklist)
10. [Troubleshooting](#10-troubleshooting)
11. [Cleanup](#11-cleanup)

Related: [ARCHITECTURE.md](./ARCHITECTURE.md) · [SECURITY.md](./SECURITY.md) · [OPERATIONS_RUNBOOK.md](./OPERATIONS_RUNBOOK.md) · [IMPLEMENTATION_ROADMAP.md](./IMPLEMENTATION_ROADMAP.md) · [DEPLOYMENT.md](./DEPLOYMENT.md)

---

## 1. Scope and assumptions

- Single developer workstation, local-first, no public exposure. `[Recommendation]`
- All service ports bind to `127.0.0.1` only. `[Recommendation]`
- The application under test (AUT) is already deployed somewhere reachable (a local container or a staging host). PR QA Pilot never builds or runs the AUT's source code — see [ADR-010](./DECISIONS.md). `[Recommendation]`
- `compose/docker-compose.yml`, `compose/.env.example` and the `runner/` package are **planned**. The contents below are the target design and are not in the repository yet. `[Assumption]`
- No paid AI API is needed. The AI step is a manual copy/paste in ChatGPT Free ([ADR-004](./DECISIONS.md)).

## 2. Prerequisites

### 2.1 Already present on the reference machine

Checked on 2026-10-18 on the author's workstation:

| Tool | Version found | Status | Used for |
|---|---|---|---|
| Debian | 13 (trixie) | `[Verified]` | Host OS |
| Python | 3.13.11 | `[Verified]` | qa-runner (needs ≥ 3.11) |
| curl | 8.17.0 | `[Verified]` | Request transport ([ADR-003](./DECISIONS.md)) |
| jq | 1.7 | `[Verified]` | Inspecting JSON by hand (not used by the runner) |
| gh | 2.46.0 | `[Verified]` | GitHub CLI: PR metadata, diffs |
| git | 2.47.3 | `[Verified]` | Source control |
| Docker Engine | 27.5.1 (Debian package `docker.io`) | `[Verified]` | Containers |
| Docker Compose | 2.26.1 (plugin) | `[Verified]` | Multi-container orchestration |
| Node.js | 24.13.0 | `[Verified]` | Optional; not needed by the runtime |

Another machine may differ. Do not assume every tool is installed; run the checks in [§3.2](#32-verify).

### 2.2 Required versus optional

| Requirement | Required? | Notes |
|---|---|---|
| Docker Engine + Compose v2 | Yes | `docker compose` (the plugin), not the legacy `docker-compose` v1 |
| Python ≥ 3.11 with `venv` | Yes | For running the runner outside a container during development |
| curl ≥ 7.70 | Yes | `--write-out '%{json}'` needs curl ≥ 7.70 `[Verify]` against the curl changelog |
| gh | Yes, from Phase 6 | Phase 3 can use it by hand |
| jq | Recommended | Debugging and report inspection |
| GitHub fine-grained PAT (read-only) | From Phase 6 | See [SECURITY.md](./SECURITY.md) |
| Discord server with a webhook | From Phase 5 | A private test channel is recommended |
| ChatGPT Free account | Phase 3 | Web interface only; no API access ([COST_AND_ALTERNATIVES.md](./COST_AND_ALTERNATIVES.md)) |
| Hardware | ~2 vCPU, 4 GB RAM, 5 GB disk free | `[Assumption]`; n8n plus a small Python container |

## 3. Installation and verification

Install only what is missing. These commands change the system; run them yourself after reviewing them.

### 3.1 Install (Debian 13 packages)

```bash
sudo apt update
sudo apt install -y ca-certificates curl jq git gh \
  python3 python3-venv python3-pip \
  docker.io docker-compose
```

- On Debian 13, `docker-compose` provides Compose v2. Confirm that `docker compose version` works. If it doesn't, install `docker-compose-plugin` from Docker's official apt repository instead `[Verify]` (https://docs.docker.com/engine/install/debian/).
- Docker's official `docker-ce` packages also work. Do not mix `docker.io` and `docker-ce` on one host.

Let your user run Docker without `sudo`:

```bash
sudo usermod -aG docker "$USER"
newgrp docker            # or log out and log back in
```

> Security note: membership in the `docker` group is effectively root on the host. Accept this only on a single-user development machine. Rootless Docker is an alternative `[Verify]` (https://docs.docker.com/engine/security/rootless/).

### 3.2 Verify

```bash
cat /etc/os-release | grep -E '^(PRETTY_NAME|VERSION_ID)='
python3 --version                 # >= 3.11
python3 -c "import venv; print('venv ok')"
curl --version | head -1
curl -s -o /dev/null -w '%{json}' https://example.com | jq '.http_code'   # --write-out json works
jq --version
git --version
gh --version | head -1
docker version --format '{{.Server.Version}}'
docker compose version
docker run --rm hello-world       # daemon + permissions
```

Expected: every command exits 0 and prints a version. `hello-world` prints "Hello from Docker!".

### 3.3 Python development environment (runner, planned)

```bash
cd "<project dir>"
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install PyYAML jsonschema          # the only third-party dependencies (ADR-003)
# once runner/ exists:
# pip install -e runner/
# qa-runner --version
```

### 3.4 GitHub CLI authentication (Phase 6)

```bash
# Prefer a fine-grained PAT, read-only: Metadata, Contents, Pull requests
# Supply it via the environment so it is never written into gh's config:
export GH_TOKEN='<paste-fine-grained-read-only-token>'
gh auth status
gh api repos/example-org/orders-service --jq '.full_name'   # replace with an allowlisted repo
```

`gh auth login` (interactive) is fine for manual work by a person. The runner container receives only `GH_TOKEN`.

## 4. Directory layout

Planned layout. See also [ARCHITECTURE.md](./ARCHITECTURE.md).

```
automation testing PRs/
├── compose/
│   ├── docker-compose.yml        # planned (see §5)
│   ├── .env.example              # planned (see §6), committed
│   ├── .env                      # real secrets, chmod 600, gitignored
│   └── egress-proxy/squid.conf   # optional allowlist proxy
├── config/projects.yaml          # copied from docs/examples/projects.example.yaml, gitignored
├── runner/                       # qa_runner package (planned)
├── prompts/test-plan-prompt.v1.md
├── n8n/workflows/*.json          # exported workflows
└── runs/<run_id>/                # file-based run storage (ADR-007)
```

The path contains spaces. Always quote it in shell commands.

## 5. Docker Compose configuration

### 5.1 Requirements

| Requirement | How it is met |
|---|---|
| n8n keeps its configuration across restarts | Named volume `n8n_data` mounted at `/home/node/.n8n` |
| Stable credential encryption | A fixed `N8N_ENCRYPTION_KEY` in `.env`. Losing it makes stored credentials unreadable |
| No public exposure | Ports bound to `127.0.0.1` |
| n8n cannot run shell commands | `NODES_EXCLUDE` blocks the Execute Command node ([SECURITY.md](./SECURITY.md)) |
| Runner isolation ([ADR-005](./DECISIONS.md)) | Non-root, read-only root filesystem, `cap_drop: [ALL]`, `no-new-privileges`, no Docker socket, no host mounts except `runs/` and read-only config |
| Runner egress limited to approved targets | Runner attaches only to `qa_internal` (`internal: true`, so no internet route). External staging hosts are reached only through the optional allowlist proxy |
| Reproducibility | Image tags pinned. `<PINNED_VERSION>` must be a concrete n8n release `[Verify]` (https://docs.n8n.io/hosting/installation/docker/) |

### 5.2 Planned `compose/docker-compose.yml`

```yaml
# PR QA Pilot — local development stack (PLANNED, not yet created)
name: prqa

services:
  n8n:
    image: docker.n8n.io/n8nio/n8n:<PINNED_VERSION>   # [Verify] choose a current stable tag
    restart: unless-stopped
    ports:
      - "127.0.0.1:5678:5678"
    environment:
      N8N_HOST: ${N8N_HOST:-localhost}
      N8N_PORT: "5678"
      N8N_PROTOCOL: http
      WEBHOOK_URL: http://localhost:5678/
      GENERIC_TIMEZONE: ${GENERIC_TIMEZONE:-UTC}
      TZ: ${GENERIC_TIMEZONE:-UTC}
      N8N_ENCRYPTION_KEY: ${N8N_ENCRYPTION_KEY:?set in .env}
      N8N_DIAGNOSTICS_ENABLED: "false"
      N8N_PERSONALIZATION_ENABLED: "false"
      # Block shell/file-system nodes. Node type names: [Verify] against the n8n docs for your version.
      NODES_EXCLUDE: '["n8n-nodes-base.executeCommand","n8n-nodes-base.localFileTrigger"]'
      N8N_BLOCK_ENV_ACCESS_IN_NODE: "true"          # [Verify] variable name for your version
      QA_RUNNER_URL: http://qa-runner:8080
      QA_RUNNER_SHARED_TOKEN: ${QA_RUNNER_SHARED_TOKEN:?set in .env}
    volumes:
      - n8n_data:/home/node/.n8n
    networks:
      - qa_internal        # talks to qa-runner
      - qa_egress          # GitHub API, Discord webhook
    security_opt:
      - no-new-privileges:true

  qa-runner:
    build:
      context: ../runner          # planned package with its own Dockerfile (python:3.13-slim + curl)
    image: prqa/qa-runner:0.1.0
    restart: unless-stopped
    command: ["qa-runner", "serve", "--bind", "0.0.0.0:8080"]
    user: "10001:10001"
    read_only: true
    tmpfs:
      - /tmp:size=64m,mode=1777
    cap_drop: [ALL]
    security_opt:
      - no-new-privileges:true
    pids_limit: 128
    mem_limit: 512m
    cpus: "1.0"
    environment:
      QA_RUNNER_SHARED_TOKEN: ${QA_RUNNER_SHARED_TOKEN:?set in .env}
      QA_CONFIG_PATH: /config/projects.yaml
      QA_RUNS_DIR: /runs
      GH_TOKEN: ${GH_TOKEN:-}
      QA_ACME_ORDERS_TENANT_A_TOKEN: ${QA_ACME_ORDERS_TENANT_A_TOKEN:-}
      QA_ACME_ORDERS_TENANT_B_TOKEN: ${QA_ACME_ORDERS_TENANT_B_TOKEN:-}
      # Used only when a project targets an external staging host:
      QA_EGRESS_PROXY: ${QA_EGRESS_PROXY:-}          # e.g. http://egress-proxy:3128
    volumes:
      - ../config/projects.yaml:/config/projects.yaml:ro
      - ../runs:/runs:rw
      # NEVER mount /var/run/docker.sock or the host home directory.
    networks:
      - qa_internal
      - qa_targets         # optional: a local application under test on the same host
    # no "ports:" section: reachable only from inside the compose networks

  egress-proxy:            # optional, enabled with: --profile egress
    profiles: ["egress"]
    image: ubuntu/squid:<PINNED_VERSION>              # [Verify] image and tag
    restart: unless-stopped
    volumes:
      - ./egress-proxy/squid.conf:/etc/squid/squid.conf:ro
    networks:
      - qa_internal
      - qa_egress
    security_opt:
      - no-new-privileges:true

volumes:
  n8n_data:

networks:
  qa_internal:
    internal: true         # no route to the internet or the host LAN
  qa_egress:
    driver: bridge
  qa_targets:
    external: true         # create once: docker network create qa_targets
    name: qa_targets
```

Notes:

- **Using GitHub from the runner.** `qa-runner context build` needs `api.github.com`. Either let n8n fetch PR data (n8n is on `qa_egress`) and pass it to the runner, or add `api.github.com` to the proxy allowlist. Choosing between them is a decision `[Needs approval]`. The default in [WORKFLOW_DESIGN.md](./WORKFLOW_DESIGN.md) is to route GitHub reads through the runner via the proxy.
- **Local application under test.** Attach the AUT container to `qa_targets` with an alias such as `orders-api.qa.internal`. That matches the `local` environment in [projects.example.yaml](./examples/projects.example.yaml).
- **Example proxy allowlist** (`compose/egress-proxy/squid.conf`):

```
acl allowed_dst dstdomain .staging.example.test api.github.com
acl SSL_ports port 443
acl CONNECT method CONNECT
http_access deny CONNECT !SSL_ports
http_access allow allowed_dst
http_access deny all
http_port 3128
```

- **Defence in depth.** The proxy does not replace the runner's own host allowlist, private-IP and metadata-address blocking ([SECURITY.md](./SECURITY.md)). Both layers are required.

## 6. Environment variables

> **Implemented configuration:** the real template is `compose/.env.example`. Provider keys never go into `compose/.env` (this drive does not enforce file modes): put `GROQ_API_KEY` (the prompt LLM, direct Groq `openai/gpt-oss-120b`) in `~/.config/prqa/secrets.env` with `chmod 600` (directory `chmod 700`); `scripts/start.sh` passes it to Docker Compose as a second `--env-file`. See [GROQ_MIGRATION.md](../GROQ_MIGRATION.md).

### 6.1 Planned `compose/.env.example`

```dotenv
# PR QA Pilot — copy to compose/.env, then: chmod 600 compose/.env
# Never commit compose/.env.

# --- n8n ---
N8N_HOST=localhost
GENERIC_TIMEZONE=UTC
# Generate once: openssl rand -hex 32   (store a backup; losing it breaks stored n8n credentials)
N8N_ENCRYPTION_KEY=<generate-64-hex-chars>

# --- n8n <-> runner shared secret (header X-Runner-Token) ---
# Generate: openssl rand -hex 32
QA_RUNNER_SHARED_TOKEN=<generate-64-hex-chars>

# --- GitHub (runner only; fine-grained, read-only: Metadata, Contents, Pull requests) ---
GH_TOKEN=<fine-grained-read-only-token>

# --- Discord (summary notifications only) ---
DISCORD_WEBHOOK_URL=<https://discord.com/api/webhooks/ID/TOKEN>

# --- Test identities for the application under test (non-production accounts only) ---
QA_ACME_ORDERS_TENANT_A_TOKEN=<tenant-a-test-user-token>
QA_ACME_ORDERS_TENANT_B_TOKEN=<tenant-b-test-user-token>

# --- Optional egress proxy (set only when using --profile egress) ---
QA_EGRESS_PROXY=
```

### 6.2 Variable reference

| Variable | Consumer | Secret | Purpose |
|---|---|---|---|
| `N8N_ENCRYPTION_KEY` | n8n | Yes | Encrypts n8n credentials at rest |
| `N8N_HOST`, `GENERIC_TIMEZONE` | n8n | No | Host name, timezone for schedules |
| `QA_RUNNER_SHARED_TOKEN` | n8n, runner | Yes | Authenticates n8n → runner HTTP calls |
| `GH_TOKEN` | runner | Yes | Read-only GitHub access |
| `DISCORD_WEBHOOK_URL` | n8n only (the runner builds the payload, n8n posts it) | Yes | Anyone holding this URL can post to the channel |
| `QA_<PROJECT>_<PROFILE>_TOKEN` | runner | Yes | Test-account credentials per `auth_profile` |
| `QA_EGRESS_PROXY` | runner | No | Proxy URL for external staging targets |
| `QA_CONFIG_PATH`, `QA_RUNS_DIR` | runner | No | Paths inside the container |

```bash
cp compose/.env.example compose/.env
chmod 600 compose/.env
sed -i "s|<generate-64-hex-chars>|$(openssl rand -hex 32)|" compose/.env   # replaces the first occurrence only; run twice, or edit by hand
```

Prefer storing the Discord webhook as an n8n credential when n8n sends notifications, and keep only one copy. See [SECURITY.md](./SECURITY.md).

## 7. Startup

```bash
cd "<project dir>"
docker network create qa_targets 2>/dev/null || true      # once
mkdir -p runs && cp -n docs/examples/projects.example.yaml config/projects.yaml

# Phase 1 (n8n only):
docker compose -f compose/docker-compose.yml --env-file compose/.env up -d n8n

# Phase 4+ (n8n + runner):
docker compose -f compose/docker-compose.yml --env-file compose/.env up -d --build

# With the optional egress proxy:
docker compose -f compose/docker-compose.yml --env-file compose/.env --profile egress up -d

docker compose -f compose/docker-compose.yml ps
docker compose -f compose/docker-compose.yml logs -f n8n
```

Open http://localhost:5678 and create the n8n owner account on first start.

Tip: `alias prqa-compose='docker compose -f "<project dir>/compose/docker-compose.yml" --env-file "<project dir>/compose/.env"'`

## 8. Shutdown

```bash
docker compose -f compose/docker-compose.yml stop          # stop, keep containers
docker compose -f compose/docker-compose.yml down          # remove containers + networks; KEEPS volumes
```

Do **not** use `down -v` unless you mean to delete n8n's data ([§11](#11-cleanup)). Before stopping, make sure no run is in progress (look for `runs/*/run.log` without a `result.json`). See [OPERATIONS_RUNBOOK.md](./OPERATIONS_RUNBOOK.md).

## 9. Verification checklist

| # | Check | Command / action | Expected |
|---|---|---|---|
| 1 | n8n healthy | `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5678/healthz` | `200` `[Verify]` endpoint for your n8n version |
| 2 | Not exposed on the LAN | `ss -ltnp \| grep 5678` | Bound to `127.0.0.1:5678` only |
| 3 | Persistence | Create a test workflow → `down` → `up -d` | Workflow still there |
| 4 | Execute Command disabled | Search the node panel for "Execute Command" | Not available |
| 5 | Runner has no internet route | `docker compose exec qa-runner curl -sS -m 5 https://example.com` | Fails (no route / DNS failure) |
| 6 | Runner not root | `docker compose exec qa-runner id -u` | `10001` |
| 7 | Read-only root filesystem | `docker compose exec qa-runner touch /x` | `Read-only file system` |
| 8 | No Docker socket | `docker compose exec qa-runner ls /var/run/docker.sock` | `No such file` |
| 9 | Runner reachable from n8n only | HTTP Request node → `http://qa-runner:8080/health` with `X-Runner-Token` | `200` |
| 10 | Secrets file permissions | `stat -c '%a' compose/.env` | `600` |

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `permission denied ... /var/run/docker.sock` | User not in `docker` group, or the session wasn't refreshed | `sudo usermod -aG docker $USER`, then log in again |
| `docker: 'compose' is not a docker command` | Compose v2 plugin missing | Install `docker-compose` (Debian 13) or `docker-compose-plugin` ([§3.1](#31-install-debian-13-packages)) |
| `N8N_ENCRYPTION_KEY ... set in .env` | `.env` missing or not passed | Use `--env-file compose/.env`; check the file exists |
| n8n: "Mismatching encryption keys" | Key changed after first start | Restore the original key from backup. Otherwise credentials must be re-created |
| n8n UI unreachable | Container crashed or port conflict | `docker compose logs n8n`; `ss -ltnp \| grep 5678` |
| Workflows lost after restart | Volume not mounted or `down -v` was used | Check `docker volume ls \| grep n8n_data`; restore from backup ([OPERATIONS_RUNBOOK.md](./OPERATIONS_RUNBOOK.md)) |
| `network qa_targets declared as external, but could not be found` | Network not created | `docker network create qa_targets` |
| Runner: `ENVIRONMENT_UNAVAILABLE` for local AUT | AUT not attached to `qa_targets`, or wrong alias | `docker network connect --alias orders-api.qa.internal qa_targets <aut-container>` |
| Runner: `POLICY_BLOCKED` for a staging host | Host not in `allowed_hosts`, or resolves to a private IP | Fix `config/projects.yaml`; private targets need `allow_private_network: true` |
| Staging host unreachable from runner | Proxy profile not started or domain not allowlisted | `--profile egress`; add the domain to `squid.conf`; set `QA_EGRESS_PROXY` |
| `runs/` write errors | Host directory owned by another UID | `sudo chown -R 10001:10001 runs` (or a matching group) |
| Paths with spaces break scripts | Unquoted path | Quote every path; prefer `"$PWD"` |
| `gh: HTTP 403/404` | Token lacks access to that repo, or repo not allowlisted | Check the fine-grained token's repository selection and permissions |
| Clock or timezone confusion in reports | `GENERIC_TIMEZONE` differs from host | Reports use UTC (`Z`); set `GENERIC_TIMEZONE=UTC` |

## 11. Cleanup

From least to most destructive. Look at what you are deleting before each step.

```bash
cd "<project dir>"

# 1. Stop and remove containers/networks (keeps n8n data and runs/)
docker compose -f compose/docker-compose.yml down

# 2. Remove old run artifacts (review the list first)
find runs -mindepth 1 -maxdepth 1 -type d -mtime +30 -print
find runs -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +

# 3. Remove the locally built runner image
docker image rm prqa/qa-runner:0.1.0

# 4. DESTRUCTIVE: delete n8n data (workflows, credentials, execution history)
#    Export workflows first (see OPERATIONS_RUNBOOK.md)
docker volume ls | grep n8n_data
docker compose -f compose/docker-compose.yml down -v

# 5. Remove the shared target network
docker network rm qa_targets

# 6. Python virtualenv
rm -rf .venv
```

Also revoke or rotate any tokens you no longer need: the GitHub PAT, Discord webhook and test-account tokens. See [OPERATIONS_RUNBOOK.md](./OPERATIONS_RUNBOOK.md#5-rotating-credentials). Test data created in the AUT is cleaned up separately, as described in the [runbook](./OPERATIONS_RUNBOOK.md#7-cleaning-up-test-data).
