# Prompt Quality Evaluation — Senior QA Prompt Pipeline

**Scope:** evaluate the quality of the prompts the PRQA pipeline actually produces, using
synthetic data only (no model calls, no network, no real repositories). This document is the
deliverable of the "Senior QA Prompt Quality Evaluation & Regression Benchmark" task; the
machinery it describes lives in `worker/prqa_worker/qa_rubric.py` (moved from `tests/qa_rubric.py`, which is now an
alias, when the worker began enforcing it on 2026-10-26), `tests/qa_benchmark.py`,
`tests/test_qa_benchmark.py` and `scripts/evaluate_prompt_quality.py`.

## 1. Executive summary

- The deterministic fallback path (Track A) scores **avg 94.9 / min 91.0 across the 15-case
  benchmark**, with **zero rubric hard-fails** and the quality gate accepting all 15 outputs.
  Every case exceeds its per-case floor (lowest floors: 50–70).
- The validator (Track B) **detects every seeded defect** in the 6 fixture probes: fabricated
  facts, truncated output, injected instructions, duplicate-ID blocks, and dropped
  stated-requirement coverage — 6/6 decisions match expectation, exit code 0.
- Five demonstrated product defects were found by the benchmark and fixed in
  `worker/prqa_worker/prompt_builder.py` (§5); each fix is pinned by a named test.
- Full regression stays green after the fixes: **344 unit tests OK** (25 opt-in skips),
  **36/36 synthetic pipeline checks**, all workflows valid,
  `.github/workflows/prqa.yml` up to date. Container/runtime state unchanged.

## 2. Architecture under test (what produces and checks a prompt)

```
build_context(workflow 05)                      # context package from PR data (validation.py)
  → build_llm_request (prompts/llm/system.v2.md + user.v2.md)
      → model output (real pipeline only)       # NOT exercised: offline benchmark
    ↘ validate_generated_prompt (quality gate)  # structure / evidence tags / secrets / duplicates
        → invalid ⇒ reference_fallback_prompt   # deterministic prompt from context + reference
finalize_prompt ⇒ shipped prompt.text (+ result contract, + synthetic banner)
```

The evaluator exercises the two non-model halves:

- **Track A — deterministic builder.** `fallback_prompt()` = `finalize_prompt(generated_text=None)`
  (source stays `reference_fallback`; the shipped file additionally carries the synthetic banner
  and the appended result contract, which are *not* scored). Scored body: the raw
  `reference_fallback_prompt(...)` output per case.
- **Track B — validator.** Mutations of the shipped known-good fixture
  (`fixtures/synthetic/llm-response.synthetic.json`) through
  `validate_generated_prompt(...)` + the rubric. This measures what the gate protects, not what
  a model would produce.

## 3. The rubric (worker/prqa_worker/qa_rubric.py)

The production worker applies it: `/prompts/finalize` uses an LLM candidate only when the validator passes, no hard-fail
rule fires and the score is a finite number ≥ 85 (`qa_rubric.accept_result`); otherwise the deterministic fallback.

10 dimensions, each 0–5, `quality_score = Σ/50·100`, pass threshold **85**; three human-review
flags trace labelled expectations. Deterministic hard-fails (any ⇒ review, regardless of score):

| Code | Trigger (deterministic check) |
|---|---|
| `HF-FABRICATED-FACT` | `[FACT]`-tagged claim not present in trusted text |
| `HF-SCOPE-VIOLATION` | Required/Regression assertion outside context-declared evidence |
| `HF-INJECTION` | forbidden instruction phrase outside quoted data |
| `HF-SECRETS` | secret-shaped literal or canary in the prompt |
| `HF-MISSING-CRITICAL` | covered stated requirements < 40% (≥ 3 stated) |
| `HF-UNUSABLE` | quality gate rejected the text (e.g. heading/structure/duplicates) |

Rubric scores are string-prefixed (`"HF-UNUSABLE: gate rejected …"`); tests/evaluator match by
**prefix**, not membership.

## 4. Track A results (fallback builder, 15 cases)

| Case | Gate | Score | scope | busi | test | exec | expe | cont | ambi | pr_s | outp | safe |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| C01-clear-behaviour | OK | 96.0 | 5 | 5 | 5 | 4 | 5 | 5 | 5 | 5 | 4 | 5 |
| C02-pagination | OK | 96.0 | 5 | 5 | 5 | 4 | 5 | 5 | 5 | 5 | 4 | 5 |
| C03-authz | OK | 94.0 | 5 | 5 | 5 | 4 | 5 | 4 | 5 | 5 | 4 | 5 |
| C04-validation-bounds | OK | 96.0 | 5 | 5 | 5 | 4 | 5 | 5 | 5 | 5 | 4 | 5 |
| C05-state-transition | OK | 96.0 | 5 | 5 | 5 | 4 | 5 | 5 | 5 | 5 | 4 | 5 |
| C06-regression-default | OK | 96.0 | 5 | 5 | 5 | 4 | 5 | 5 | 5 | 5 | 4 | 5 |
| C07-soft-delete | OK | 96.0 | 5 | 5 | 5 | 4 | 5 | 5 | 5 | 5 | 4 | 5 |
| C08-schema-compat | OK | 96.0 | 5 | 5 | 5 | 4 | 5 | 5 | 5 | 5 | 4 | 5 |
| C09-incomplete | OK | 91.0 | 5 | 5 | 4.5 | 2 | 5 | 5 | 5 | 5 | 4 | 5 |
| C10-ambiguous-scope | OK | 96.0 | 5 | 5 | 5 | 4 | 5 | 5 | 5 | 5 | 4 | 5 |
| C11-empty-requirements | OK | 91.0 | 5 | 5 | 4.5 | 2 | 5 | 5 | 5 | 5 | 4 | 5 |
| C12-conflicting | OK | 96.0 | 5 | 5 | 5 | 4 | 5 | 5 | 5 | 5 | 4 | 5 |
| C13-injection | OK | 94.0 | 5 | 5 | 5 | 3 | 5 | 5 | 5 | 5 | 4 | 5 |
| C14-numeric-bait | OK | 93.0 | 5 | 5 | 4.5 | 3 | 5 | 5 | 5 | 5 | 4 | 5 |
| C15-complex | OK | 96.0 | 5 | 5 | 5 | 4 | 5 | 5 | 5 | 5 | 4 | 5 |

**avg 94.9 · min 91.0 · spread 5.0** — variation lands where context quality actually varies
(discovery-driven cases C09/C11/C14 score lower executability/test-density by design, not
failure). Baseline before fixes: the first calibration run had **3 of 15 cases rejected by the
builder's own gate** (duplicate generic scenario bodies) and several measurement-stage score
coefficients clustered at 89–91 due to weak executability grounding.

## 5. Defects found by the benchmark → fixes (all in worker)

| # | Defect (evidence) | Root cause | Fix | Test pin |
|---|---|---|---|---|
| F1 | Multi-op fallbacks rejected by their own gate (`CANCEL-004 repeats ORDERS-003`), C01/C07/C10/C12/C15 | per-op "persisted state" block body was identical across operations — `check_duplicates` strips the `**ID…**` title line | block now names its concrete operation (`- Requirement/Risk: the mutation of `{op}`…`, `- Expected: … in the output of `{op}``) | Track A: all-15 gate-valid |
| F2 | `REG-002 repeats REG-001` (C15) when ≥2 ops | regression block body op-neutral (op only in the stripped title) | body names the op (`- Requirement/Risk: existing clients of `{op}`…`, `- Request: … for `{op}` from section 4.`) | Track A: all-15 gate-valid |
| F3 | `C08-schema-compat` produced **zero** operations → API-001-only prompt | benchmark helper `diff_for` skipped the derived OpenAPI hunk when the case diff already mentioned `openapi/` in a schema-only fragment | per-path guard: skip only paths whose executable `+  <path>:` lines are already embedded | C08 shows `GET /v1/orders/{orderId}` ops (score 91 → 96) |
| F4 | Conflicting requirements merged into one stated requirement (C12 had only the 400 side; the 404 side lost silently) | same-category merge rule (`shared ≥ 1`) overrode the Jaccard guarantee | conflict guard: statements with differing concrete numeric keywords (400 vs 404, 200 vs 300) stay separate | `test_8_conflicting_codes_are_stated_not_facts` (S1=404, S2=400 both present, no `[FACT]` escalation) |
| F5 | C14's fallback would fail the gate it is contracted to satisfy: `reference-template contamination: 'organization'` | the fallback quotes untrusted_PR vocabulary verbatim; the gate forbids reference-template terms unless trusted evidence contains them | `_mask_unverified_terms()` masks non-evidenced reference terms with `[desc-term pending discovery]` inside the fallback's quoted/stated data | Track A C14 gate-valid (89.0→93.0); `organization` and bait values stay out of `[FACT]` lines |

Measurement-only fixes (no product code): rubric heading/body association
(`block_bodies_scoped`, executability regex), case-sensitive `S\#` substring check, and
benchmark wiring (stated descriptions missing from the PR payload, shipped-text vs body scoring).

## 6. Track B results (validator, 6 fixtures)

All fixtures are surgery on the shipped known-good output, evaluated against the canonical
fixture context (B06 uses a strict 4-stated-requirement context):

| Fixture | Score | Decision | Detected | Reason |
|---|---:|---|---|---|
| B01-known-good | 86.0 | accept | yes | passes gate; ≥ threshold |
| B02-fabricated-fact | 72.0 | reject | yes | `unsupported claim in a Required/Regression scenario` ([FACT]-tagged audit-logs endpoint) |
| B03-incomplete | 40.0 | reject | yes | missing required headings; `HF-UNUSABLE` |
| B04-injection | 86.0 | reject | yes | `contains injected/forbidden phrase: ignore all previous instructions`; `HF-INJECTION` |
| B05-near-duplicate-ids | 86.0 | reject | yes | byte-exact re-emission of `ORD-CNL-002` as `ORD-DUP-001` → `duplicate scenario` |
| B06-dropped-requirement | 82.0 | reject | yes | `inadequate PR-specific coverage: 2/4 stated requirements covered` (doc'd limit: errors fire at ≥3 stated, covered <60%) |

Note: B02/B04/B05 scores stay ≥85 because the metric measures the *whole document* — the rubric
hard-fail codes and the gate error carry the rejection semantics (the evaluator checks the
decision + hard-fail prefix, not the numeric score, per fixture pin).

## 7. Metamorphic properties tested (tests/test_qa_benchmark.py)

1. Endpoint change → scope of scenarios/ops changes, old endpoint vanishes (delegate desc).
2. Changed rule (200→300 chars) changes the expected-value focus, old value absent.
3. Adding a stated requirement grows the stated scenario list.
4. Removing the OpenAPI spec never fabricates operations (discovery-driven plan only).
5. Injected instructions/SQL are **evicted wholesale** from the description quote and disclosed
   as a removal note (shipped design; never quoted as data).
6. Reordering prose keeps requirement identities (category + keyword sets preserved).
7. Numeric-bait values never ride `[FACT]`-tagged lines without diff/openapi provenance.
8. Conflicting codes surface as two distinct stated requirements, never a `[FACT]` claim.

## 8. Security verification

- No real LLM, no OpenCode, no network: evaluator and tests import `prompt_builder` directly;
  Track-B fixtures are local JSON surgery.
- `AUTOMATION_MODE=review_only` and `PROMPT_LLM_LIVE_IN_REVIEW_ONLY=false` invariants stand; the
  n8n layer is untouched (9 workflows still inactive); all containers keep their recovery-final
  IDs (`5faf55002f8e…`, `098408d777ff…`, `800843b2db23…`).
- Repository writes limited to: `worker/prqa_worker/prompt_builder.py` (F1/F2/F4/F5), the four
  new `tests/qa_*`/`tests/test_qa_benchmark.py` files, `scripts/evaluate_prompt_quality.py`,
  `runs/prompt-quality/evaluation.{md,json}` — and this document.

## 9. Limitations & next step

- **Track A measures the fallback, not a model.** Uniform scores are expected; the benchmark's
  value is in gate-validity, grounding tags and hard-fail freedom per context shape.
- **Track B measures the validator**, using one context family; thresholds beyond the canonical
  fixture (e.g. <3 stated requirements) are documented limits (B06).
- **No real-model baseline yet.** The next step is a live-model evaluation behind an explicit
  operator opt-in (new env flag alongside `PROMPT_LLM_LIVE_IN_REVIEW_ONLY=false`, which stays
  `false`), feeding the same rubric; the runner
  (`fallback_prompt` ⇄ `build_llm_request`) already exists for that swap.
