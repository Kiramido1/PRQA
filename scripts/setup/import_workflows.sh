#!/usr/bin/env bash
# Import n8n/workflows/*.json into the running n8n container. Workflows stay INACTIVE.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE=(docker compose -f "$ROOT/compose/docker-compose.yml" --env-file "$ROOT/compose/.env")
SECRETS_FILE="${PRQA_SECRETS_FILE:-$HOME/.config/prqa/secrets.env}"; [[ -f "$SECRETS_FILE" ]] && COMPOSE+=(--env-file "$SECRETS_FILE")

python3 "$ROOT/scripts/build/validate_workflows.py" >/dev/null || { echo "workflow validation failed; not importing" >&2; exit 1; }
python3 "$ROOT/scripts/setup/n8n_api.py" setup-owner
# Copy the validated files in instead of relying on the /imports bind mount: a container created while the project
# drive was mounted elsewhere keeps an empty, stale mount and n8n then "successfully" imports nothing.
FILES=("$ROOT"/n8n/workflows/*.json)
"${COMPOSE[@]}" exec -T n8n sh -c 'rm -rf /tmp/prqa-import && mkdir -p /tmp/prqa-import'
for f in "${FILES[@]}"; do "${COMPOSE[@]}" cp "$f" n8n:/tmp/prqa-import/ >/dev/null; done
OUT="$("${COMPOSE[@]}" exec -T n8n n8n import:workflow --separate --input=/tmp/prqa-import 2>&1)"
echo "$OUT"
"${COMPOSE[@]}" exec -T n8n rm -rf /tmp/prqa-import
grep -q "Successfully imported ${#FILES[@]} workflows" <<<"$OUT" \
  || { echo "import incomplete: expected ${#FILES[@]} workflows" >&2; exit 1; }
echo "--- workflows in n8n (all must be active=False) ---"
python3 "$ROOT/scripts/setup/n8n_api.py" list
