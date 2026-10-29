# PRQA — Prompt Provider: Direct Groq (`openai/gpt-oss-120b`)

**Status (2026-10-26):** Groq is the sole active prompt-generation provider. Implemented and verified offline
(unit, integration and synthetic end-to-end tests, with outbound network blocked). Live use needs a **new**
`GROQ_API_KEY` in `~/.config/prqa/secrets.env`; the earlier key was exposed and must not be reused.
**Safety:** `AUTOMATION_MODE=review_only`, `PROMPT_LLM_LIVE_IN_REVIEW_ONLY=false`, all 9 n8n workflows inactive.

---

## 1. Active configuration

| Setting | Value | Where |
|---|---|---|
| Base URL | `https://api.groq.com/openai/v1` (`/chat/completions`) | `PROMPT_LLM_API_BASE` (compose default + `compose/.env`) |
| Model | `openai/gpt-oss-120b` | `PROMPT_LLM_MODEL` |
| `temperature` | `0.2` | fixed in `build_llm_request` |
| `max_completion_tokens` | `3000` | `PROMPT_LLM_MAX_TOKENS` (1..24000, default 3000); Groq deprecates `max_tokens`, so the builder renames it; the user message states the same limit |
| `reasoning_effort` | `low` | `PROMPT_LLM_REASONING_EFFORT` (low / medium / high; GPT-OSS on Groq only) |
| `include_reasoning` | `false` | always sent with `reasoning_effort`: hidden reasoning is never returned |
| `stream` | `false` | fixed |
| Key | `GROQ_API_KEY` only | `~/.config/prqa/secrets.env` → n8n service only |

The exact request (`/prompts/build` → `llm_request`, passed unchanged by workflow 03):

```json
{"model": "openai/gpt-oss-120b", "messages": [...], "temperature": 0.2, "max_completion_tokens": 3000,
 "stream": false, "reasoning_effort": "low", "include_reasoning": false}
```

No OpenRouter `reasoning` field and no `reasoning_format` (unsupported for GPT-OSS and mutually exclusive with
`include_reasoning`). In review-only mode the same request goes to the mock (`{MOCK_SERVICES_URL}/llm/v1`, model
`mock-llm`) so the production shape is exercised offline.

## 2. Provider rules

1. Groq is the only active provider. There is **no OpenRouter fallback**, retry provider or hidden recovery path.
2. `GROQ_API_KEY` is used for the Groq base only; `PROMPT_LLM_API_KEY` is never a substitute (it is a legacy slot
   for non-Groq endpoints). The worker policy (`_resolve_prompt_llm_key`), workflow 03's Authorization header and the
   evaluator all apply the same per-provider rule; an OpenRouter-shaped key (`sk-or-…`) is refused for Groq.
3. If Groq fails or its output fails the quality gate, the worker records a sanitized failure and uses the
   deterministic reference fallback. Providers are never switched.
4. `scripts/eval/evaluate_openrouter_live.py` and `runs/openrouter-live/` remain only for historical comparison; nothing
   in the active path selects them.

## 3. Credential setup (manual, by the account owner)

1. Revoke the exposed key at <https://console.groq.com/keys> and create a new one. (This cannot be done or
   confirmed from this project.)
2. Open `~/.config/prqa/secrets.env` **in an editor** (not with `echo`, which leaves the key in shell history) and
   add one line: `GROQ_API_KEY=<new key>`.
3. Keep the file owner-only: `chmod 700 ~/.config/prqa && chmod 600 ~/.config/prqa/secrets.env`. That directory is
   on the home filesystem (ext4), where modes are enforced; the project drive is FUSE/NTFS and is not.
4. Leave `GROQ_API_KEY=` and `PROMPT_LLM_API_KEY=` empty in `compose/.env` and `compose/.env.example`. The evaluator
   refuses to run if a key appears in `compose/.env`.
5. `scripts/start.sh` passes the secrets file to `docker compose` as a second `--env-file`
   (`PRQA_SECRETS_FILE` overrides the path). Compose hands `GROQ_API_KEY` to the **n8n** service only, because n8n
   sends the generation request. The worker builds the request but never receives the key, and the OpenCode sandbox
   environment strips it (`opencode_runner.SECRET_ENV_NAMES`).

## 4. Failure handling (pipeline)

Workflow 03 checks for the provider key before calling, extracts the response without copying provider text, and
reports to `/prompts/finalize`. The worker records `generation.failure` (a fixed identifier) and `generation.outcome`.
Every failure uses the reference fallback:

| Situation | `generation.failure` | `generation.outcome` |
|---|---|---|
| Real mode without `GROQ_API_KEY` (no request sent) | `missing_credential` | `provider_error` |
| Non-2xx response | `http_error` | `http_error` |
| Timeout / connection failure | `transport_error` | `transport_error` |
| HTTP 200 body with a provider `error` object | `provider_error_response` | `provider_error` |
| HTTP 200 body that is not JSON | `malformed_json` | `malformed` |
| JSON without `choices` and without an error object | `missing_choices` | `malformed` |
| No content | `empty_content` | `empty` |
| `finish_reason=length` (partial output never used) | `truncated` | `truncated` |
| Complete output rejected by the deterministic validator | `gate_rejected` | `gate_rejected` |
| Validator passed, a rubric hard-fail rule fired | `rubric_hard_fail` | `gate_rejected` |
| Validator passed, rubric score below 85 (or not a finite number) | `rubric_threshold` | `gate_rejected` |
| Rubric scoring raised or returned a malformed result (fail closed) | `rubric_unavailable` | `gate_rejected` |
| Complete output accepted (validator valid, no hard fail, score ≥ 85) | `null` | `usable` (source `llm`) |

The worker's quality gate (`/prompts/finalize`) has two parts, and a complete candidate must pass both: the deterministic validator (`validate_generated_prompt`, any error selects the fallback) and the 85-point rubric (`worker/prqa_worker/qa_rubric.py`, with `HF-SCOPE-VIOLATION` and the other hard fails). The decision is `qa_rubric.accept_result`: gate valid with no errors, no hard fail, and a finite score ≥ `THRESHOLD` (85.0, defined once). The evaluators and the benchmark import the same module (`tests/qa_rubric.py` is only an alias). The candidate is scored once; the fallback is never scored. The response carries `quality` = `{accepted, failure, score, threshold, hard_fails (ids only), gate_valid}` with no prompt text. Until 2026-10-26 the worker enforced only the validator, so run 4 (gate-valid, 78.0) would have been used; it now falls back with `rubric_threshold`. Since 2026-10-26 the validator also requires a BLOCKED item (a `(Blocked)` scenario or `BLOCKED` in section 4, 5 or 11), since a legend such as `Pass/Fail/Blocked` alone used to satisfy the phrase check. A gate
pass is not proof that a prompt has no defects.

## 5. Bounded live evaluation (`scripts/eval/evaluate_groq_live.py`)

The evaluator drives the production request (`Policy.from_env(compose/.env).generation_settings()` +
`build_llm_request`) against the synthetic benchmark cases only.

```bash
python3 scripts/eval/evaluate_groq_live.py --only C01-clear-behaviour --dry-run   # payload shape; no key, no network
python3 scripts/eval/evaluate_groq_live.py --check-config                       # local checks only; nothing sent or written
python3 scripts/eval/evaluate_groq_live.py --only C01-clear-behaviour --limit 1  # one live request
```

- Preconditions (all local, before any network operation): review-only flags, base exactly Groq, model with
  pinned published limits (`GROQ_MODEL_LIMITS`: 131,072 context, 65,536 completion), valid generation settings,
  `GROQ_API_KEY` from the owner-only secrets file, and the estimated prompt + completion budget within the limits.
- No model-list call: the first and only network operation per case is the generation request.
- One attempt per case, no retries; any failed request stops the run. Artifacts in `runs/groq-live/` are redacted,
  carry source hashes, never contain the key, a key fingerprint, headers or reasoning text.

## 6. History

- 2026-10-23: Groq support added (then `llama-3.3-70b-versatile`, `GROQ_API_KEY` with a `PROMPT_LLM_API_KEY`
  fallback). OpenRouter `dots-studio/dots-3-note-preview:free` had produced 0/14 gate-valid outputs.
- 2026-10-26: one live C01 request to `openai/gpt-oss-120b`: HTTP 200 in 9.6 s, `finish_reason=stop`,
  gate-rejected (score 72.0, `HF-SCOPE-VIOLATION`: invented `GET /v1/orders/{id}`, unestablished 401/404 triggers,
  description-only `cancellation_reason` and 500-character limit stated as facts). The gate and prompt were
  hardened against each defect (`tests/test_groq_c01_regressions.py`). The key used was found in a tracked
  template and is treated as compromised.
- 2026-10-26: Groq locked as the only provider: `max_completion_tokens`, `reasoning_effort=low`,
  `include_reasoning=false`, `GROQ_API_KEY` only, no OpenRouter fallback, credential check in workflow 03,
  distinct failure categories, synthetic end-to-end test (`tests/test_e2e_groq_pipeline.py`).

- 2026-10-26 (live, 2 requests): request 1 HTTP 200 in 6.1 s, `stop`, 7,587 + 2,439 tokens, gate-rejected (70.0,
  `HF-SCOPE-VIOLATION`: description-only `cancellation_reason`, 500 limit and "reason required" stated as untagged
  facts with the `[STATED]` tag on a separate bullet, 404/401 triggers asserted, no BLOCKED item; no invented
  endpoint this time). Correction: a mechanically derived claim ledger in the user message (`claim_ledger()`).
  Request 2 HTTP 413 in 0.4 s: `rate_limit_exceeded`, tokens per minute **limit 8000**, requested 10,430. No third
  request. The ledger's effect on output quality is therefore unverified.

- 2026-10-26 (live, run 3, 1 request): HTTP 200 in 5.1 s, `stop`, 4,125 + 2,091 tokens, gate-rejected (70.0, `HF-FABRICATED-FACT`): `reason` requiredness stated as a diff fact, `jq` reads of files no request writes. Offline remediation: requiredness and self-contained-request rules in the prompt; the gate no longer reads a tester directive ("Validate that …", "gaps that must be flagged") as a validation claim. Offline re-score of that output: still rejected (72.0, `HF-FABRICATED-FACT`).

- 2026-10-26 (live, run 4, 1 request): HTTP 200 in 4.7 s, `stop`, 4,210 + 1,790 tokens; deterministic gate **valid** (0 errors, 0 hard fails), rubric **78.0** (< 85). The worker of that day would have used it (`source: llm`).
  Since the rubric enforcement (same day, offline), `/prompts/finalize` returns the reference fallback for it (`rubric_threshold`).

- 2026-10-26 (offline, 0 requests): prompt-quality pass from the 18 saved C01 outputs (re-scores in
  `runs/offline-rescore/c01-all-saved-outputs-rescore-20261026.json`). The title now carries `PR #<n>`; requirement IDs are the
  gate's `S1…` (given in the claim ledger); an evidence-bounded coverage mix (success, negatives, boundary pair built in the
  shell, one Regression per changed operation, AUTH for a documented 401); Blocked scenarios keep Request/Expected;
  description-only details carry `[STATED: PR description]` on their own line in every section. The model's context copy
  drops test-irrelevant metadata (`llm_context_view`) and only the selected test level is explained; the request stays
  16,930 chars. Rubric, threshold, gate and hard fails unchanged; the effect on Groq output is untested.

## 7. Request size and the account limit

The 413 of live request 2 carried the provider error `rate_limit_exceeded` / `tokens` and a sanitized message naming
a tokens-per-minute limit of 8,000 and 10,430 requested. No response headers were kept, so how Groq counted the
10,430 is not known. The evaluator classifies any non-2xx by its HTTP status (`http_error`). Since 2026-10-26 it also
records the provider's `provider_error_code` / `provider_error_type` beside the status, without inferring a cause.

Offline remediation (2026-10-26) reduced the constructed C01 request from 33,833 to about 16,600 message characters.
That is about 7,985 → 3,930 prompt tokens, an **estimate** at 4.237 chars/token calibrated on request 1, or about
4,750 at a pessimistic 3.5. It also set the completion budget to 3,000. Changes:
- compact system rules
- no reference excerpt in the request (its constraints and the 12 defect-record fields are in the system rules)
- compact context JSON
- a shorter claim ledger

`tests/test_prompt_size_and_defect_matrix.py` pins both values. Whether a live request now fits the limit has not been
tested.

## 8. Going live (not done; requires explicit approval)

Live prompt generation inside review-only mode needs, in order: the new key in the secrets file; a passing bounded
evaluation; then `PROMPT_LLM_LIVE_IN_REVIEW_ONLY=true` in `compose/.env`, a restart via `scripts/start.sh`, and
manual activation of the n8n workflows after visual review. None of these steps is automated.
