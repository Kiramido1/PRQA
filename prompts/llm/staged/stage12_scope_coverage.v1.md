# Stage 1 of 2 — PR scope, verified facts and test coverage (PRQA staged prompt generator v1, two-stage mode)

You extract a compact, evidence-grounded scope package for ONE pull request AND write its API test cases. A QA agent runs the cases later with `curl` against a non-production API. Output a single JSON object and nothing else.

The input is the union of the two inputs below; follow both rule sets and return both outputs in one object:
`{"scope": <Stage 1 output>, "coverage": <Stage 2 output>}`.

## Stage 1 rules and output (key `scope`)
{{STAGE1}}

## Stage 2 rules and output (key `coverage`)
{{STAGE2}}
