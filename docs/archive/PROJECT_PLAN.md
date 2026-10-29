# Project Plan

What PR QA Pilot is, why it exists, what it must and must not do, and how delivery is staged.

> Status: Planned — documentation only (v0.1, 2026-10-18)

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Objectives](#2-objectives)
3. [Non-Goals](#3-non-goals)
4. [Requirements](#4-requirements)
5. [Constraints](#5-constraints)
6. [Assumptions](#6-assumptions)
7. [Success Criteria](#7-success-criteria)
8. [Scope Boundaries](#8-scope-boundaries)
9. [Project Phases](#9-project-phases)
10. [Milestones and Deliverables](#10-milestones-and-deliverables)
11. [Decisions Requiring User Approval](#11-decisions-requiring-user-approval)

## 1. Problem Statement

API changes in Pull Requests are often merged after only light manual testing. Writing PR-specific API tests by hand is slow and inconsistent across repositories. AI models can draft useful test ideas quickly, but their output is non-deterministic, can be wrong, and can be manipulated through prompt injection in PR content. Executing AI-generated commands directly is unsafe.

The project needs a repeatable way to turn a labeled PR into a reviewed, bounded and deterministic API test run. That run must produce trustworthy evidence. It must start at zero AI API cost and be able to grow into full automation later.

## 2. Objectives

| ID | Objective |
|---|---|
| O1 | Test the API surface changed by a PR, scoped by the diff and the OpenAPI contract |
| O2 | Use AI only to *propose* test plans; execution and assertions stay deterministic |
| O3 | Run entirely locally and free in v1, with manual ChatGPT Free planning |
| O4 | Make every run traceable: inputs, plan hash, approval, evidence, results |
| O5 | Protect credentials, repositories and environments by design |
| O6 | Onboard new repositories through configuration, not by copying workflows |
| O7 | Move from manual AI to an automated provider without changing the runner |

## 3. Non-Goals

- UI, browser, end-to-end, load, performance or chaos testing.
- Building, installing or executing PR or repository code ([ADR-010](./DECISIONS.md#adr-010-never-execute-prrepository-code)).
- Testing production environments.
- Replacing unit or integration tests owned by application teams.
- Automating the ChatGPT web UI or using undocumented endpoints ([ADR-004](./DECISIONS.md#adr-004-chatgpt-free-used-manually-in-v1)).
- Kubernetes, microservices, databases or paid infrastructure in v1.
- Auto-merging, approving or blocking PRs. v1 is advisory only.

## 4. Requirements

### 4.1 Functional

| ID | Requirement | Phase |
|---|---|---|
| FR-01 | Detect a PR carrying a `qa-test:*` label (manual input first, then polling, then webhook) | 4, 6, 9 |
| FR-02 | Resolve repository, PR number, base/head refs and SHAs, and changed files | 6 |
| FR-03 | Retrieve the PR description, diff and OpenAPI spec at `head_sha` | 6 |
| FR-04 | Produce a sanitized AI context package | 3, 6 |
| FR-05 | Accept a JSON test plan from any `PlanProvider` | 3, 8 |
| FR-06 | Validate the plan against the JSON Schema and the project policy (hosts, methods, paths, limits) | 2 |
| FR-07 | Require approval tied to the plan's SHA-256 before execution | 2, 3 |
| FR-08 | Execute requests with `curl` through argv, with timeouts and bounded transport retries | 2 |
| FR-09 | Assert status, JSON schema, body, headers and duration deterministically | 2 |
| FR-10 | Classify results as PASS, FAIL, ERROR, BLOCKED or SKIPPED, with a `failure_class` | 2 |
| FR-11 | Redact secrets and store sanitized evidence | 2 |
| FR-12 | Generate `result.json` and `report.md` | 2 |
| FR-13 | Send a sanitized Discord summary | 5 |
| FR-14 | Support multiple projects with per-project policies | 7 |
| FR-15 | Prevent duplicate runs for the same PR, head SHA and level | 9 |

### 4.2 Non-Functional

| ID | Requirement |
|---|---|
| NFR-01 | Security: least privilege, no shell execution of untrusted input, egress allowlist ([SECURITY.md](./SECURITY.md)) |
| NFR-02 | Reproducibility: the same plan, environment and data produce the same assertions; versions are pinned |
| NFR-03 | Cost: no paid service required through Phase 7 |
| NFR-04 | Operability: documented start, stop, troubleshooting and cleanup ([OPERATIONS_RUNBOOK.md](./OPERATIONS_RUNBOOK.md)) |
| NFR-05 | Simplicity: the runner depends only on the Python stdlib, `PyYAML` and `jsonschema` |
| NFR-06 | Bounded load: per-level request caps and per-request timeouts ([QA_STRATEGY.md](./QA_STRATEGY.md)) |

## 5. Constraints

- Debian 13, local-first, Docker Compose. [Verified]
- Self-hosted n8n as the orchestrator ([ADR-001](./DECISIONS.md#adr-001-self-hosted-n8n-as-orchestrator)).
- Python test runner, with `curl` as the request tool and `jq` for humans only ([ADR-003](./DECISIONS.md#adr-003-curl-via-subprocess-argv-as-transport)).
- No pytest as a required framework.
- ChatGPT Free has web access only and **no API credentials or credits**. [Verify] at <https://help.openai.com>.
- Claude Code is neither assumed free nor assumed reusable by automation. [Verify] at <https://docs.anthropic.com/en/docs/claude-code>.
- The application under test must not be modified.

## 6. Assumptions

- [Assumption] Each project has a reachable non-production environment and dedicated test accounts.
- [Assumption] An OpenAPI/Swagger spec exists for most projects. When it is missing, planning falls back to the diff and description, and schema assertions are reduced.
- [Assumption] A single developer machine is enough for v1 volume, which is a handful of PRs per day.
- [Assumption] Test data created by tests can be cleaned up through the API or a documented reset.

## 7. Success Criteria

The MVP is complete when **every** criterion from brief section 12 holds. Each one is mapped to a phase and an acceptance test in [IMPLEMENTATION_ROADMAP.md](./IMPLEMENTATION_ROADMAP.md#mvp-acceptance-matrix).

1. n8n runs locally and keeps its configuration across restarts.
2. A manual test run can be started reproducibly.
3. The runner validates an input test plan.
4. The runner executes an approved request against a safe target.
5. Expected and actual results are compared deterministically.
6. A failed assertion is reported as FAIL.
7. Transport and unexpected execution failures are classified separately (ERROR).
8. Sensitive values are redacted.
9. Markdown and JSON reports are generated.
10. Discord notifications can be tested independently.
11. The system runs without a paid AI API.
12. The AI planning step can be completed manually with ChatGPT Free.
13. No application source code is modified.
14. No tests run against unauthorized targets.
15. All components have documented startup, shutdown, troubleshooting and cleanup.

Longer-term success measures, tracked after the MVP:

- [Recommendation] A new repository can be onboarded in under 30 minutes using `projects.yaml` only.
- [Recommendation] At least 90% of AI-proposed plans pass validation after at most one revision.

## 8. Scope Boundaries

| In scope | Out of scope |
|---|---|
| REST/JSON APIs described by OpenAPI or docs | GraphQL, gRPC, WebSocket (future evaluation) |
| Staging and local environments listed in `projects.yaml` | Production, or any host not allowlisted |
| Safe negative security tests | Exploitation, fuzzing at scale, DoS |
| Non-destructive tests by default | Destructive tests without explicit approval and isolation |
| Read-only GitHub access | Writing to repositories, commenting, merging (possible future opt-in) |

## 9. Project Phases

| Phase | Name | Goal |
|---|---|---|
| 0 | Discovery and Requirements | Verify the environment, record assumptions, choose the architecture |
| 1 | Local Infrastructure | Docker Compose, n8n with persistent storage, runner container skeleton |
| 2 | Deterministic Test Runner | Validation, `curl` execution, assertions, evidence and reports, without AI |
| 3 | Manual AI Integration | ChatGPT Free copy/paste workflow, prompt template, approval |
| 4 | n8n Integration | n8n calls the runner over the internal HTTP wrapper |
| 5 | Discord Notifications | Sanitized summary and notification failure handling |
| 6 | GitHub PR Integration | PR metadata, diff and OpenAPI retrieval; manual or polling trigger |
| 7 | Multiple Repositories | `projects.yaml`, per-project policies and credentials |
| 8 | Automated AI Providers | `PlanProvider` implementations for an API or local model |
| 9 | Automatic Label Triggers | Polling, then webhooks, with duplicate-run prevention |
| 10 | Deployment Readiness | Security and reliability testing, backups, monitoring, operational review |

Sequencing note: Phase 6 can start in parallel with Phase 4 because it only needs the runner CLI. In Phase 3 the context package is assembled from manually run `gh` output. Details are in [IMPLEMENTATION_ROADMAP.md](./IMPLEMENTATION_ROADMAP.md).

## 10. Milestones and Deliverables

| Milestone | Phases | Deliverables |
|---|---|---|
| M0 Blueprint approved | 0 | This documentation set, signed-off decisions ([DECISIONS.md](./DECISIONS.md)) |
| M1 Infrastructure up | 1 | `compose/docker-compose.yml`, `compose/.env.example`, persistent n8n |
| M2 Deterministic runner | 2 | `runner/qa_runner` CLI, `test-plan.schema.json`, reports, unit self-checks |
| **M3 MVP** | 3–5 | Manual AI flow, n8n workflow export, Discord summary; all 15 MVP criteria pass |
| M4 GitHub-aware | 6–7 | Context builder, `config/projects.yaml`, two projects onboarded |
| M5 Automated | 8–9 | Automated provider, label triggers, deduplication |
| M6 Production-ready ops | 10 | Security review, backup and restore drill, monitoring, readiness checklist ([DEPLOYMENT.md](./DEPLOYMENT.md)) |

## 11. Decisions Requiring User Approval

| ID | Decision | Default proposal |
|---|---|---|
| D1 | Which safe API target to use for Phases 2–5 | A local mock API container on `qa_internal` [Needs approval] |
| D2 | Which Discord channel and webhook to use for notifications | A dedicated private `#qa-bot` test channel [Needs approval] |
| D3 | Which automated AI provider, if any, for Phase 8 | Decide after Phase 7 using [COST_AND_ALTERNATIVES.md](./COST_AND_ALTERNATIVES.md) [Needs approval] [Paid] for API options |
| D4 | Whether `qa-test:smoke` may auto-run without human plan review once Phase 9 is reached | Yes, for staging only [Needs approval] |
| D5 | Whether the bot may later post PR comments (needs write scope) | No in v1 [Needs approval] |
