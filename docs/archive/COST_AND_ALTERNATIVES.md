# Cost and Alternatives

Compares the AI and orchestration options for PR QA Pilot by cost model, access type, limitations, security and maintenance, so we can start for free and know exactly what would later turn into a paid dependency.

> Status: Planned — documentation only (v0.1, 2026-10-18)

## Contents

1. [How to read this document](#1-how-to-read-this-document)
2. [Key distinctions](#2-key-distinctions)
3. [Summary matrix](#3-summary-matrix)
4. [AI planning options](#4-ai-planning-options)
5. [Orchestration options](#5-orchestration-options)
6. [Recommended path](#6-recommended-path)
7. [Cost controls](#7-cost-controls)
8. [Items requiring verification](#8-items-requiring-verification)

Related: [DECISIONS.md](./DECISIONS.md) · [AI_TEST_PLAN_SPEC.md](./AI_TEST_PLAN_SPEC.md) · [ARCHITECTURE.md](./ARCHITECTURE.md) · [DEPLOYMENT.md](./DEPLOYMENT.md) · [SECURITY.md](./SECURITY.md)

---

## 1. How to read this document

- **This document lists no prices on purpose.** Prices, free tiers, quotas and plan entitlements change often. Every cost item links to the vendor's official page and is tagged `[Verify]`. Check that page on the day you decide.
- Tags follow the project convention: `[Verified]` (checked on this machine), `[Recommendation]`, `[Assumption]`, `[Needs approval]`, `[Paid]`, `[Verify]`.
- "Free" always says *which kind* of free (see [§2](#2-key-distinctions)).

## 2. Key distinctions

| Distinction | Meaning | Why it matters here |
|---|---|---|
| **Free software** vs **free service** | Free software (open-source or source-available) costs nothing to license but you pay for the hardware it runs on. A free service is someone else's hosted offering with limits they set and can change | n8n self-hosted and local LLMs are software. ChatGPT Free is a service |
| **Free account** vs **API access** | A free *web account* lets a person use a chat UI. *API access* is a separate product with its own keys and billing | **ChatGPT Free (web) does not provide an OpenAI API key or API credits.** A paid ChatGPT subscription doesn't include API usage either `[Verify]` (https://help.openai.com). The same separation exists between Claude consumer plans and the Anthropic API `[Verify]` |
| **Fixed** vs **usage-based** cost | Fixed: subscription or server rent per month. Usage-based: per token, per request, per CI minute | Usage-based costs grow with PR volume and test level. They need budgets and request caps ([§7](#7-cost-controls)) |
| **Licence terms** vs **price** | Something can be free to use and still restricted (fair-code licences, terms of service) | n8n's licence restricts some commercial uses `[Verify]`. Automating consumer web UIs may break terms of service |

### 2.1 Why we don't automate the ChatGPT web UI

Driving the ChatGPT web interface with browser automation, or calling its undocumented internal endpoints, is **rejected as the default integration** ([ADR-004](./DECISIONS.md)):

- **Terms of service.** Consumer terms generally forbid automated or programmatic access to the service other than through the API. Read the current OpenAI Terms of Use before considering it `[Verify]` (https://openai.com/policies/terms-of-use).
- **Reliability.** UI and internal-endpoint changes break automation without warning, and CAPTCHAs and rate limits apply.
- **Security.** It needs a logged-in browser session (credentials) inside the automation environment.
- **No structured-output guarantees.** There are no JSON-mode or schema features to rely on.

The manual copy/paste workflow keeps a person in the loop, which the Phase 3 human review needs anyway.

## 3. Summary matrix

| Option | Category | Software cost | Service cost model | Access type | Automatable? | Data leaves the machine? | Phase |
|---|---|---|---|---|---|---|---|
| ChatGPT Free, manual | AI planning | — | Free account with usage limits `[Verify]` | Web UI only, **no API** | No (by design) | Yes, to OpenAI (sanitized context only) | 3 (default) |
| OpenAI API | AI planning | — | **Usage-based** per token `[Paid]` `[Verify]` | API key, separate billing | Yes | Yes, to OpenAI | 8 (optional) |
| Claude Code (CLI) | AI planning | CLI install | **Paid**: Claude subscription plan *or* usage-based API billing `[Paid]` `[Verify]` | CLI auth (subscription login or API key) | Yes, headless mode `[Verify]` | Yes, to Anthropic (or the cloud provider) | 8 (optional) |
| Local LLM (e.g. Ollama) | AI planning | Free software (model licences vary) | **Fixed**: your hardware and electricity | Local HTTP API | Yes | No | 8 (optional) |
| n8n self-hosted | Orchestration | Free to self-host under its licence `[Verify]` | **Fixed**: host resources. Paid enterprise features exist `[Verify]` | Local UI/API | Yes | Only what workflows send | 1+ (default) |
| GitHub Actions | Trigger / CI | — | Free minutes on some plans/repos, then **usage-based** `[Verify]` | GitHub-hosted or self-hosted runners | Yes | Runs on GitHub (hosted) or your runner | 9 (alternative) |
| Python-only orchestration | Orchestration | Free (stdlib + cron/systemd) | **Fixed**: host only | CLI / cron | Yes | Only what scripts send | Fallback |

## 4. AI planning options

All providers implement the same `PlanProvider` interface. Their output goes through the same schema validation, policy checks and approval gate ([ADR-006](./DECISIONS.md), [ADR-008](./DECISIONS.md), [AI_TEST_PLAN_SPEC.md](./AI_TEST_PLAN_SPEC.md)). Switching provider never changes the runner.

### 4.1 ChatGPT Free, manual copy/paste (`manual_chatgpt`)

| Aspect | Detail |
|---|---|
| What it is | A person pastes `prompts/test-plan-prompt.v1.md` plus the sanitized context package into the ChatGPT web UI and saves the returned JSON as `plan.json` |
| Cost | No direct cost. Free-tier model access and message limits are set by OpenAI and change `[Verify]` (https://openai.com/chatgpt/pricing/) |
| Pros | Zero spend; human review built in; no API keys to protect; fits Phase 3 |
| Cons | Manual (minutes per PR); doesn't scale to many PRs; output format may drift (the validator catches it); limits on context size and message quota `[Verify]` |
| Security | Only the **sanitized** context package may be pasted. No secrets, tokens, personal data or proprietary code beyond what policy allows. Check the data-usage/training settings of the account `[Verify]` (https://openai.com/policies/privacy-policy) |
| Maintenance | Keep the prompt versioned (`prompt_version` in each plan) |

### 4.2 OpenAI API (`openai_api`)

| Aspect | Detail |
|---|---|
| What it is | Programmatic calls from a runner-side provider module using an API key |
| Cost | `[Paid]` Usage-based (input/output tokens; varies by model) `[Verify]` https://openai.com/api/pricing/. Requires an API account with billing set up. **Separate from any ChatGPT plan** |
| Pros | Fully automatable; structured-output/JSON features can reduce malformed plans `[Verify]` (https://platform.openai.com/docs); predictable integration |
| Cons | Ongoing spend; key management; vendor dependency |
| Security | Key stored only in runner env or a secrets store; spending limits set in the provider console `[Verify]`; context still sanitized; review the API data-usage policy `[Verify]` |
| Maintenance | Model deprecations need occasional updates |

### 4.3 Claude Code CLI (`claude_code_cli`)

| Aspect | Detail |
|---|---|
| What it is | Anthropic's agentic coding CLI. It has a non-interactive "print"/headless mode (`claude -p "<prompt>"`) that can return JSON output `[Verify]` (https://docs.anthropic.com/en/docs/claude-code) |
| Cost | `[Paid]` **Not free.** Needs either a paid Claude subscription plan that includes Claude Code, or usage-based Anthropic API billing (API key from the Anthropic Console), or access through a supported cloud provider `[Verify]` (https://www.anthropic.com/pricing, https://docs.anthropic.com/en/docs/claude-code) |
| Auth for automation | Don't assume a person's interactive subscription login can be reused by a service account or container. For unattended automation, use an **API key** set as an environment variable, or cloud-provider credentials, and check the current authentication docs and terms `[Verify]` |
| Pros | Strong reasoning on diffs and specs; can produce structured output; can run headless in a container |
| Cons | Paid; it's an *agent* with tool-use abilities, so it must be restricted (no file writes, no shell, no network beyond the API). The plan still goes through the validator |
| Security | Run it in its own container with the repository context **read-only** and tools disabled or allowlisted `[Verify]` settings/permission flags; never give it the runner's test credentials; the output is untrusted input like any other provider's ([SECURITY.md](./SECURITY.md)) |
| Maintenance | CLI version pinning; flags may change between releases |

### 4.4 Local LLM (`local_llm`)

| Aspect | Detail |
|---|---|
| What it is | A model served locally, e.g. Ollama or llama.cpp exposing an OpenAI-compatible HTTP endpoint `[Verify]` (https://github.com/ollama/ollama) |
| Cost | Free software; **fixed** hardware cost. Usable quality on code/spec reasoning usually needs a capable GPU or a lot of RAM `[Assumption]` |
| Licence | Each model has its own licence (some restrict commercial use) `[Verify]` per model |
| Pros | No data leaves the machine; no per-token cost; works offline |
| Cons | Lower plan quality than frontier models for many tasks `[Assumption]`; slower on CPU; more malformed JSON (the validator rejects it); you operate the model server yourself |
| Security | Bind to `127.0.0.1` or an internal docker network only; model files come from trusted sources |

### 4.5 Comparison for the planning step

| Criterion | ChatGPT Free (manual) | OpenAI API | Claude Code | Local LLM |
|---|---|---|---|---|
| Direct cost | None | Usage-based `[Paid]` | Subscription or usage `[Paid]` | Hardware (fixed) |
| Automation | None | Full | Full | Full |
| Plan quality | High, varies by available model `[Assumption]` | High `[Assumption]` | High `[Assumption]` | Medium, model-dependent `[Assumption]` |
| Data residency | OpenAI | OpenAI | Anthropic / cloud provider | Local |
| Setup effort | Lowest | Low | Medium | Medium–high |
| Main risk | Human time, manual errors | Spend, key leak | Spend, agent permissions | Quality, ops burden |

## 5. Orchestration options

### 5.1 n8n self-hosted (default, [ADR-001](./DECISIONS.md))

| Aspect | Detail |
|---|---|
| Cost | Free to self-host. **Fixed** cost of the machine. n8n's "Sustainable Use License" (fair-code) restricts some commercial uses such as offering n8n as a service; some features need a paid licence `[Verify]` (https://docs.n8n.io/sustainable-use-license/, https://n8n.io/pricing/) |
| Pros | Visual workflows, manual approvals (Wait/Form nodes), retries, credential store, schedule and webhook triggers, workflows exportable as JSON |
| Cons | Another service to run and update; shell-execution nodes must be disabled ([SECURITY.md](./SECURITY.md)); exported JSON gives noisy git diffs |
| Use for | Triggers, branching, approvals, notifications, run state. **Not** for executing requests ([ADR-002](./DECISIONS.md)) |

### 5.2 GitHub Actions (future trigger alternative, [ADR-009](./DECISIONS.md))

| Aspect | Detail |
|---|---|
| Cost | Public repos and some plans get free minutes on GitHub-hosted runners; beyond that, **usage-based** per minute. Self-hosted runners have no GitHub minute charge but you run the machine `[Verify]` (https://docs.github.com/en/billing/managing-billing-for-your-products/managing-billing-for-github-actions/about-billing-for-github-actions) |
| Pros | Native `labeled` events; close to the PR; no inbound webhook endpoint needed |
| Cons | Staging credentials would live in GitHub secrets. Workflows triggered by forks/`pull_request_target` are a well-known injection risk. GitHub-hosted runners can't reach a local-only test target |
| Safe use | Only to **notify** the runner (e.g. `repository_dispatch` / a signed webhook to n8n) with no secrets and no PR code checkout; never `pull_request_target` running PR code ([ADR-010](./DECISIONS.md)) |

### 5.3 Python-only orchestration (fallback)

| Aspect | Detail |
|---|---|
| Cost | Free. Fixed host cost only |
| Pros | Fewest moving parts; everything in git; easy to test |
| Cons | You build approvals, retries, scheduling UI, credential storage and execution history yourself |
| When to choose | If n8n becomes an operational burden, or for headless server deployments. Possible because the runner CLI is orchestration-agnostic ([ADR-002](./DECISIONS.md)) |

### 5.4 Orchestration comparison

| Criterion | n8n self-hosted | GitHub Actions | Python-only |
|---|---|---|---|
| Software cost | Free (licence terms apply) `[Verify]` | n/a (service) | Free |
| Running cost | Fixed (host) | Free quota, then usage `[Verify]` | Fixed (host) |
| Manual approval UI | Built in | Environments/reviewers `[Verify]` | Build it |
| Reaches local test targets | Yes | Only with a self-hosted runner | Yes |
| Secret exposure surface | Local | GitHub secrets | Local |
| Effort | Low–medium | Medium | Medium–high |

## 6. Recommended path

| Stage | AI planning | Orchestration | Trigger | Spend |
|---|---|---|---|---|
| v1 (Phases 0–7) | `manual_chatgpt` | n8n self-hosted + runner | Manual → polling | **None** besides your machine |
| v2 (Phase 8) | Choose *one* of `local_llm` (no spend) or `openai_api` / `claude_code_cli` (paid) based on plan-quality measurements from v1 `[Needs approval]` | Same | Polling | Fixed or usage-based, capped |
| v3 (Phase 9–10) | Same | Same, server-hosted ([DEPLOYMENT.md](./DEPLOYMENT.md)) | Signed webhook; optional Actions `repository_dispatch` | Host + optional AI usage |

Decision criteria for Phase 8 (measure them during Phase 3):

1. Share of AI plans that pass validation on the first attempt.
2. Share of plan test cases a reviewer keeps unchanged.
3. Reviewer minutes per PR.
4. PR volume per week × level, which drives usage-based spend.

## 7. Cost controls

Apply these before any paid provider is enabled `[Recommendation]`:

- **Hard caps in the provider console:** monthly spending limit or budget alerts `[Verify]` the provider's billing settings.
- **Per-run caps in the runner:** maximum context size (truncate diffs, include only the relevant OpenAPI fragments), one generation attempt plus at most one repair attempt per plan, and level-based request caps (`max_requests`, see [QA_STRATEGY.md](./QA_STRATEGY.md)).
- **Only labelled PRs** trigger planning; nothing runs on every push.
- **Duplicate-run prevention** (Phase 9 lock per project/PR/head SHA) stops paying twice for the same plan.
- **Cache plans** per head SHA. A re-run of the same commit reuses the approved plan.
- **Log usage:** record `generated_by.provider`, model, and token counts (if the provider returns them) in `result.json` metadata.
- **GitHub Actions:** prefer self-hosted runners or the free quota; set a spending limit `[Verify]`.

## 8. Items requiring verification

Check these against official documentation before relying on them:

| # | Item | Official source |
|---|---|---|
| 1 | ChatGPT Free model access, limits, data-usage settings | https://openai.com/chatgpt/pricing/ · https://help.openai.com |
| 2 | That ChatGPT plans don't include API credits | https://help.openai.com |
| 3 | OpenAI API per-token pricing and spending limits | https://openai.com/api/pricing/ · https://platform.openai.com/docs |
| 4 | OpenAI Terms of Use on automated access | https://openai.com/policies/terms-of-use |
| 5 | Claude Code plans, API billing, headless flags, auth methods for unattended use | https://www.anthropic.com/pricing · https://docs.anthropic.com/en/docs/claude-code |
| 6 | n8n licence terms and paid features | https://docs.n8n.io/sustainable-use-license/ · https://n8n.io/pricing/ |
| 7 | GitHub Actions free minutes, billing, spending limits | https://docs.github.com/en/billing |
| 8 | Ollama / model licences and hardware requirements | https://github.com/ollama/ollama · each model's licence |
| 9 | Discord webhook rate limits and payload limits | https://discord.com/developers/docs/resources/webhook |

No prices, quotas or entitlements in this document have been verified as of 2026-10-18.
