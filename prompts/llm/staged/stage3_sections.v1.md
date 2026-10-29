# Stage 3 of 3 — PR-specific execution, defect and reporting guidance (PRQA staged prompt generator v1)

The scope and the test cases of ONE pull request are already validated (input below). The mandatory execution rules, evidence format, defect template, report format and the machine-readable `qa-result.json` contract are fixed text added by the assembler; do not restate them. Write only the PR-specific guidance that goes under them. Output a single JSON object and nothing else.

## Input (user message, JSON)
- `scope`: summary, requirements (IDs, headers), risks, assumptions.
- `test_cases`: ID, label, operation and title of every case (their bodies are already final).
- `uncovered`, `blocked`: requirements and cases that cannot be executed and why.
- `operations`: the only operations that may be named.

## Rules
- Every item refers to these cases, requirements or operations by ID or `METHOD /path`; nothing generic, nothing new.
- No new endpoints, fields, status codes, limits, roles or business rules. A severity is a suggestion for the reporter, with the impact it depends on.
- Plain sentences, no Markdown headings or tables, no `[FACT]`/`[STATED]` tags, no credentials, no host names.
- Never write a numeric HTTP status code (items with one are discarded): say "a 4xx", "a 5xx", "the documented success status" or "the documented validation error". Name operations exactly as listed in `operations`.

## Output (JSON object, these keys only; each list at most 6 items of <= 220 chars)
```
{"execution_notes": ["order and fixture notes for running the cases with curl only"],
 "evidence_notes": ["what to capture for which case (status, body field, follow-up read)"],
 "severity_guidance": [{"ref": "CALC-002", "severity": "P0|P1|P2|P3", "rationale": "impact if it fails"}],
 "regression_analysis": ["how to tell a regression by this PR from a pre-existing issue for these operations"],
 "blockers": ["what may block execution and how to record it as BLOCKED"],
 "report_notes": ["what the final report must state for this PR"]}
```
