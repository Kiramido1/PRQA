#!/usr/bin/env bash
# Stop the review stack. Keeps volumes (n8n workflows/credentials, worker job store).
#   scripts/stop.sh          # docker compose down (keeps data)
#   scripts/stop.sh --purge  # ALSO deletes volumes (n8n data, job store) — asks for confirmation
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "$ROOT/compose/docker-compose.yml" --env-file "$ROOT/compose/.env")
SECRETS_FILE="${PRQA_SECRETS_FILE:-$HOME/.config/prqa/secrets.env}"; [[ -f "$SECRETS_FILE" ]] && COMPOSE+=(--env-file "$SECRETS_FILE")
if [[ "${1:-}" == "--purge" ]]; then
  read -r -p "This deletes n8n data and the job store. Type PURGE to confirm: " ans
  [[ "$ans" == "PURGE" ]] || { echo "aborted"; exit 1; }
  "${COMPOSE[@]}" down -v
else
  "${COMPOSE[@]}" down
fi
