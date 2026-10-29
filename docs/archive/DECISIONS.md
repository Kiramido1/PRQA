# Architecture Decision Records

The log of major architecture decisions for PR QA Pilot, in lightweight ADR format.

> Status: Planned — documentation only (v0.1, 2026-10-18)

## Table of Contents

- [ADR Format](#adr-format)
- [Index](#index)
- [ADR-001 Self-hosted n8n as orchestrator](#adr-001-self-hosted-n8n-as-orchestrator)
- [ADR-002 Python runner separated from orchestration](#adr-002-python-runner-separated-from-orchestration)
- [ADR-003 curl via subprocess argv as transport](#adr-003-curl-via-subprocess-argv-as-transport)
- [ADR-004 ChatGPT Free used manually in v1](#adr-004-chatgpt-free-used-manually-in-v1)
- [ADR-005 Isolated runner container](#adr-005-isolated-runner-container)
- [ADR-006 Pluggable PlanProvider abstraction](#adr-006-pluggable-planprovider-abstraction)
- [ADR-007 File-based run storage](#adr-007-file-based-run-storage)
- [ADR-008 Strict validation and human approval for every plan](#adr-008-strict-validation-and-human-approval-for-every-plan)
- [ADR-009 Polling before webhooks](#adr-009-polling-before-webhooks)
- [ADR-010 Never execute PR/repository code](#adr-010-never-execute-prrepository-code)
- [ADR-011 Config-driven multi-repository support](#adr-011-config-driven-multi-repository-support)

## ADR Format

Each record states its **Status** (Proposed, Accepted, Superseded, Deprecated), **Context**, **Decision**, **Alternatives considered** and **Consequences**. A new decision gets the next number. Superseded records stay in place with a link to their replacement.

All records below are **Proposed** until the project owner approves them in Phase 0. After that they become **Accepted**.

## Index

| ADR | Title | Status |
|---|---|---|
| 001 | Self-hosted n8n as orchestrator | Proposed |
| 002 | Python runner separated from orchestration | Proposed |
| 003 | curl via subprocess argv as transport | Proposed |
| 004 | ChatGPT Free used manually in v1 | Proposed |
| 005 | Isolated runner container | Proposed |
| 006 | Pluggable PlanProvider abstraction | Proposed |
| 007 | File-based run storage | Proposed |
| 008 | Strict validation and human approval for every plan | Proposed |
| 009 | Polling before webhooks | Proposed |
| 010 | Never execute PR/repository code | Proposed |
| 011 | Config-driven multi-repository support | Proposed |

---

<a id="adr-001"></a>

## ADR-001 Self-hosted n8n as orchestrator

**Status:** Proposed

**Context.** The workflow combines triggers (manual, schedule, webhook), approvals, branching on outcomes and notifications. The user prefers a visual, self-hosted orchestrator. The workflow must not depend on paid SaaS.

**Decision.** Use self-hosted n8n in Docker (`docker.n8n.io/n8nio/n8n:<PINNED_VERSION>`) for **orchestration only**: triggers, input validation, run state, approval waits, error branches and Discord notifications. n8n does not execute curl or shell commands. Command-execution nodes are disabled (`NODES_EXCLUDE`). [Verify] the node identifiers, and whether recent versions disable them by default, at <https://docs.n8n.io>.

**Alternatives considered.**

| Option | Why not chosen |
|---|---|
| Python-only orchestration (cron + scripts) | No visual approval or monitoring; more custom code. It remains the fallback |
| GitHub Actions as orchestrator | Runs on GitHub infrastructure, which complicates reaching local or staging targets and the manual AI step; risky with untrusted PR events |
| Temporal, Airflow, Prefect | Heavier than v1 needs |

**Consequences.**

- (+) Visual workflows, built-in credential store, Wait/approval nodes, execution history.
- (+) Workflows are exportable as JSON and can be version controlled.
- (−) An extra service to secure and back up (`n8n_data`, `N8N_ENCRYPTION_KEY`).
- (−) n8n licensing terms must be respected. [Verify] the "Sustainable Use License" for the intended use.

---

<a id="adr-002"></a>

## ADR-002 Python runner separated from orchestration

**Status:** Proposed

**Context.** Plan validation, policy enforcement, assertion logic and redaction are security-critical and need unit-level self-checks, versioning and reproducibility. Writing that logic in n8n Code nodes is hard to test and review.

**Decision.** Implement a standalone Python 3.11+ package `qa_runner` (deps: stdlib, `PyYAML`, `jsonschema`) with a CLI (`qa-runner`) and a thin internal HTTP wrapper (`qa-runner serve`) on the `qa_internal` network, authenticated with `X-Runner-Token`. n8n calls it through the HTTP Request node.

**Alternatives considered.**

| Option | Why not chosen |
|---|---|
| Logic inside n8n Code nodes | Hard to test, review and reuse outside n8n |
| n8n Execute Command calling scripts | Gives the orchestrator shell access, which widens the attack surface |
| FastAPI or Flask service | Extra dependencies; the stdlib `http.server` is enough for a few internal endpoints [Recommendation] |

**Consequences.**

- (+) The runner works without n8n (CLI). That covers testing, fallback and CI.
- (+) Clear security boundary and single place for policy.
- (−) Two components to deploy; an internal API contract to maintain.

---

<a id="adr-003"></a>

## ADR-003 curl via subprocess argv as transport

**Status:** Proposed

**Context.** The user prefers API testing with `curl` and no pytest. A Python HTTP client would be simpler to code but departs from that preference. Building curl command strings from AI output risks shell injection.

**Decision.** `curl` is the request transport, invoked with `subprocess.run([...], shell=False)` and a fixed option set: `--max-time`, `--connect-timeout`, `--max-redirs 0`, `--proto =http,https`, `--write-out '%{json}'`, bodies through `--data-binary @file`, headers as separate `-H` argv items. Secrets are injected from environment variables at execution time. Python performs all assertions; `jq` is for human debugging only. Every executed request is also logged as a redacted, copy-pasteable curl command for reproduction.

**Alternatives considered.**

| Option | Why not chosen |
|---|---|
| `requests`/`httpx`/`urllib` | Departs from the user's curl preference; it remains possible as a later `transport` plug-in |
| Shell scripts with curl + jq | Quoting and injection risk; weak assertion logic |
| pytest + requests | Explicitly not a required framework |

**Consequences.**

- (+) Evidence is directly reproducible by humans with curl.
- (+) No shell is involved, so metacharacters in plan values are inert.
- (−) Process spawn per request (acceptable at v1 volumes); curl version differences matter, so pin it in the runner image.

---

<a id="adr-004"></a>

## ADR-004 ChatGPT Free used manually in v1

**Status:** Proposed

**Context.** The project must start without paid AI APIs. ChatGPT Free is a web interface. It does **not** provide API credentials or credits, and a ChatGPT subscription is separate from API billing. [Verify] at <https://help.openai.com>. Automating the web UI or using undocumented endpoints is brittle, likely violates the terms of service, and has security implications.

**Decision.** In v1, a human copies the sanitized context package and the versioned prompt into ChatGPT Free, then saves the JSON answer as `plan.json`. The plan is validated and approved locally. There is no browser automation and no undocumented endpoints.

**Alternatives considered.**

| Option | Why not chosen |
|---|---|
| OpenAI API from day one | Usage-based cost [Paid]; deferred to Phase 8 |
| Browser automation of ChatGPT | Brittle, against the terms of service, unsafe |
| Local LLM from day one | Hardware and quality uncertainty; evaluated in Phase 8 |
| No AI (hand-written plans) | Always supported (`provider: human`) but slower |

**Consequences.**

- (+) Zero AI cost; a human reviews every plan.
- (−) A manual step and throughput limits; Phase 9 auto-runs are limited until Phase 8.

---

<a id="adr-005"></a>

## ADR-005 Isolated runner container

**Status:** Proposed

**Context.** The runner processes untrusted input (PR content, AI plans) and makes network requests. It must not be usable to reach internal services, cloud metadata or the host.

**Decision.** Run the runner in its own container: non-root user, read-only root filesystem (writable `runs/` and `tmpfs` only), all Linux capabilities dropped, `no-new-privileges`, **no Docker socket**, no host filesystem mounts beyond `runs/` and read-only `config/`. Network: `qa_internal` for n8n ↔ runner, plus a controlled egress path to allowlisted targets only. Application-level host/IP allowlist checks run before every request (block `169.254.169.254`, loopback, RFC 1918 unless explicitly allowlisted for a local target). See [SECURITY.md](./SECURITY.md).

**Alternatives considered.**

| Option | Why not chosen |
|---|---|
| Run the runner on the host directly | No isolation from host services and files |
| Run inside the n8n container | Couples privileges; n8n would gain network and command reach |
| Firecracker/gVisor sandbox | Stronger but more complex; revisit in Phase 10 if needed |

**Consequences.**

- (+) Contains damage from a malicious plan or a runner bug.
- (−) Egress allowlisting at the network level needs extra setup (firewall/proxy); the application-level check is mandatory regardless.

---

<a id="adr-006"></a>

## ADR-006 Pluggable PlanProvider abstraction

**Status:** Proposed

**Context.** AI providers will change: manual ChatGPT Free now, and possibly the OpenAI API, Claude Code CLI or a local LLM later. The runner must not be rewritten for each.

**Decision.** Define `PlanProvider.generate(context_package) -> plan_json` with implementations `manual` (default), `openai_api`, `claude_code_cli` (headless `claude -p`; needs its own authentication and billing [Verify] at <https://docs.anthropic.com/en/docs/claude-code>) and `local_llm` (for example an OpenAI-compatible local endpoint such as Ollama [Verify]). The provider is selected in config. Every provider's output passes the same validation and approval ([ADR-008](#adr-008-strict-validation-and-human-approval-for-every-plan)).

**Alternatives considered.**

| Option | Why not chosen |
|---|---|
| Hard-code one provider | Rewrites needed later |
| A framework such as LangChain | Unnecessary dependency for a single prompt-to-JSON call |

**Consequences.**

- (+) Providers are swappable by config; fallback to manual is always available.
- (−) A least-common-denominator interface; provider-specific features (structured outputs) must stay optional.

---

<a id="adr-007"></a>

## ADR-007 File-based run storage

**Status:** Proposed

**Context.** v1 volume is small. A database adds operations work (backups, migrations) that brings no value yet.

**Decision.** Store every run under `runs/<run_id>/` (`context/`, `plan.json`, `approval.json`, `evidence/`, `result.json`, `report.md`, `run.log`). Run ID format: `YYYYMMDDTHHMMSSZ-<project_id>-pr<number>-<head_sha7>`. Append-only `runs/index.jsonl` supports history and deduplication.

**Alternatives considered.**

| Option | Why not chosen |
|---|---|
| SQLite/PostgreSQL | Not needed at v1 scale; revisit if querying history becomes painful |
| n8n execution data only | Not durable enough, mixes concerns, and is pruned by n8n |

**Consequences.**

- (+) Simple, inspectable, easy to back up and diff.
- (−) Concurrency on `index.jsonl` needs file locking; retention must be enforced ([REPORTING.md](./REPORTING.md)).

---

<a id="adr-008"></a>

## ADR-008 Strict validation and human approval for every plan

**Status:** Proposed

**Context.** AI output is untrusted and can be malformed or manipulated through prompt injection in PR content. Executing unvalidated plans could hit unauthorized hosts, leak data or destroy test data.

**Decision.** Every plan, whatever its provider, must pass, in order:

1. JSON Schema validation ([test-plan.schema.json](./examples/test-plan.schema.json)).
2. Project policy: hosts, path prefixes, methods, forbidden headers, body size, request cap per level, destructive flags.
3. Approval recorded in `approval.json` and bound to the plan's SHA-256.

Any edit invalidates the approval. Destructive tests additionally need `destructive_allowed: true`. Auto-approval is allowed only for `qa-test:smoke` once Phase 9 is reached, if decision D4 is accepted.

**Alternatives considered.**

| Option | Why not chosen |
|---|---|
| Trust provider output | Unsafe |
| Schema only, no policy | A schema-valid plan can still target a forbidden host |
| Human review only | Error-prone; machines should enforce invariants |

**Consequences.**

- (+) Defence in depth; a full audit trail.
- (−) Review friction; re-prompting loops for invalid plans.

---

<a id="adr-009"></a>

## ADR-009 Polling before webhooks

**Status:** Proposed

**Context.** Webhooks need a publicly reachable endpoint, signature verification and duplicate handling, because GitHub may redeliver events. A local machine is usually not publicly reachable.

**Decision.** Use the manual trigger first (Phase 4), then n8n Schedule polling of labeled PRs (Phase 6/9). Enable webhooks only after duplicate-run prevention (key `(project_id, pr_number, head_sha, level)`) and `X-Hub-Signature-256` verification exist. GitHub Actions may later send a `repository_dispatch`/HTTP notification, but must never run untrusted PR code with secrets.

**Alternatives considered.**

| Option | Why not chosen now |
|---|---|
| Webhooks from day one | Needs public exposure (tunnel or server) and dedup first |
| GitHub Actions as trigger | Viable later; the `pull_request_target` risks must be avoided |

**Consequences.**

- (+) No inbound exposure needed for v1.
- (−) Up to one polling interval of latency (5 min); uses GitHub API rate limit budget. [Verify] the limits at <https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api>.

---

<a id="adr-010"></a>

## ADR-010 Never execute PR/repository code

**Status:** Proposed

**Context.** Building or running code from a PR (including install scripts and tests) is untrusted code execution. It could steal secrets or attack the host. The platform tests *deployed* APIs and does not need to build anything.

**Decision.** The platform only *reads* PR content (metadata, diff, OpenAPI files) as data and sends HTTP requests to already-deployed non-production environments listed in `projects.yaml`. It never checks out code to execute it, never runs `npm install`/`pip install`/`make` from a PR, and never uses privileged `pull_request_target` workflows that execute PR code.

**Alternatives considered.**

| Option | Why not chosen |
|---|---|
| Build PR into ephemeral preview environments | Valuable, but belongs to the application's CI/CD with its own isolation; out of scope |

**Consequences.**

- (+) Eliminates a whole class of supply-chain and secret-exfiltration risks.
- (−) Testing depends on the PR being deployed to a test environment; the runner cannot verify which build is deployed unless the API exposes a version endpoint. [Recommendation] Check a `/version` or health endpoint against `head_sha` when available.

---

<a id="adr-011"></a>

## ADR-011 Config-driven multi-repository support

**Status:** Proposed

**Context.** Copying an n8n workflow per repository causes drift and duplicated secrets.

**Decision.** One workflow and one runner serve all projects. Per-project settings live in `config/projects.yaml` (schema per [projects.example.yaml](./examples/projects.example.yaml)): repository, enabled flag, base URL, OpenAPI location, allowed environment, allowed path prefixes and methods, auth profiles (env variable names only), supported levels, timeouts, request caps, cleanup and notifications. Credentials follow `QA_<PROJECT>_<PROFILE>_TOKEN`.

**Alternatives considered.**

| Option | Why not chosen |
|---|---|
| One workflow per repository | Duplication and drift |
| Config inside n8n static data | Not reviewable in git and not usable by the runner CLI |

**Consequences.**

- (+) Onboarding is a config change plus secrets.
- (−) The config file becomes security-critical: it must be validated at startup and reviewed in pull requests.
