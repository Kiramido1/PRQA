#!/usr/bin/env bash
# Run every SAFE, SYNTHETIC verification: unit tests, workflow validation, and (if the stack is up) the end-to-end mock pipeline.
#   scripts/run_synthetic_tests.sh            # unit + workflow validation + worker/mock checks
#   scripts/run_synthetic_tests.sh --webhook  # also exercises the n8n webhook receiver with signed synthetic payloads
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
echo "=== 1. worker unit tests (synthetic fixtures) ==="
PYTHONPATH=worker python3 -m unittest discover -s tests
echo "=== 2. workflow JSON validation ==="
python3 scripts/build/validate_workflows.py
echo "=== 2b. GitHub Actions workflow is generated from scripts/clients/prqa_actions_client.py ==="
python3 scripts/build/build_actions_workflow.py --check
if curl -fs -o /dev/null http://127.0.0.1:8080/healthz; then
  echo "=== 3. end-to-end synthetic pipeline (worker + mocks${1:+ + n8n webhook}) ==="
  python3 scripts/eval/synthetic_pipeline_test.py "${1:-}"
else
  echo "=== 3. skipped: stack not running (scripts/start.sh) ==="
fi
