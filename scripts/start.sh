#!/usr/bin/env bash
# Start the PR QA Pilot review stack (n8n + worker + mocks) in REVIEW-ONLY mode.
#   scripts/start.sh            # create compose/.env if missing, build, start, wait for health
#   scripts/start.sh --no-build
#   scripts/start.sh --phase2   # additionally start the Phase-2 services (Docker policy proxy, model proxy, launcher) —
#                               # still review-only: refuses unless AUTOMATION_MODE=review_only, the credentials pass
#                               # scripts/setup/provision_model_proxy_secrets.py --check and the pinned sandbox image matches
#   scripts/start.sh --recreate # recreate every container (needed after a reboot: the drive may mount at a new path)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/compose/.env"
SECRETS_FILE="${PRQA_SECRETS_FILE:-$HOME/.config/prqa/secrets.env}"   # optional: real API keys, kept off the FUSE drive (chmod 600)
COMPOSE=(docker compose -f "$ROOT/compose/docker-compose.yml" --env-file "$ENV_FILE")
[[ -f "$SECRETS_FILE" ]] && COMPOSE+=(--env-file "$SECRETS_FILE") && echo "using secrets from $SECRETS_FILE"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ROOT/compose/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  gen() { openssl rand -hex 32; }
  sed -i "s|^N8N_ENCRYPTION_KEY=.*|N8N_ENCRYPTION_KEY=$(gen)|" "$ENV_FILE"
  sed -i "s|^PRQA_WORKER_TOKEN=.*|PRQA_WORKER_TOKEN=$(gen)|" "$ENV_FILE"
  sed -i "s|^GITHUB_WEBHOOK_SECRET=.*|GITHUB_WEBHOOK_SECRET=$(gen)|" "$ENV_FILE"
  sed -i "s|^PRQA_WEBHOOK_SECRET=.*|PRQA_WEBHOOK_SECRET=$(gen)|" "$ENV_FILE"
  sed -i "s|^N8N_OWNER_PASSWORD=.*|N8N_OWNER_PASSWORD=Prqa-$(openssl rand -hex 6)-Review1|" "$ENV_FILE"
  echo "created compose/.env (chmod 600) with locally generated secrets; real integration keys are empty"
fi

# Older .env files predate the GitHub Actions entry: add a locally generated PRQA_WEBHOOK_SECRET once (never printed).
if ! grep -q '^PRQA_WEBHOOK_SECRET=' "$ENV_FILE"; then
  printf '\n# --- GitHub Actions entry secret (added by scripts/start.sh; copy into the GitHub secret PRQA_WEBHOOK_SECRET) ---\nPRQA_WEBHOOK_SECRET=%s\nPRQA_WEBHOOK_TOLERANCE_S=300\n' "$(openssl rand -hex 32)" >> "$ENV_FILE"
  echo "added PRQA_WEBHOOK_SECRET to compose/.env"
fi

# Pin the n8n image tag to the version that was actually pulled (reproducible restarts).
if grep -q '^N8N_IMAGE_TAG=latest' "$ENV_FILE"; then
  TAG="$(docker run --rm docker.n8n.io/n8nio/n8n:latest n8n --version 2>/dev/null | tail -1 | tr -d '[:space:]' || true)"
  if [[ "$TAG" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    docker tag docker.n8n.io/n8nio/n8n:latest "docker.n8n.io/n8nio/n8n:$TAG"
    sed -i "s|^N8N_IMAGE_TAG=.*|N8N_IMAGE_TAG=$TAG|" "$ENV_FILE"
    echo "pinned N8N_IMAGE_TAG=$TAG"
  fi
fi

# Phase-1 guard: refuse to start outside review-only mode unless explicitly overridden (Phase 2).
MODE="$(grep -E '^AUTOMATION_MODE=' "$ENV_FILE" | cut -d= -f2- | tr -d '[:space:]')"
if [[ "${MODE:-review_only}" != "review_only" && "${PRQA_ALLOW_LIVE:-}" != "1" ]]; then
  echo "REFUSING to start: AUTOMATION_MODE=$MODE. Phase 2 requires explicit approval; set PRQA_ALLOW_LIVE=1 to override." >&2
  exit 2
fi

BUILD=(--build); PHASE2=0; RECREATE=()
for arg in "$@"; do
  case "$arg" in
    --no-build) BUILD=();;
    --phase2) PHASE2=1;;
    --recreate) RECREATE=(--force-recreate);;
    *) echo "unknown argument: $arg" >&2; exit 64;;
  esac
done
if [[ "$PHASE2" == "1" ]]; then
  # Fail closed BEFORE anything is (re)started: review-only only, credentials provisioned and in sync, image pinned.
  [[ "${MODE:-review_only}" == "review_only" ]] || { echo "REFUSING --phase2: AUTOMATION_MODE=$MODE (Phase 2 services start in review_only only)" >&2; exit 2; }
  [[ -f "$SECRETS_FILE" ]] || { echo "REFUSING --phase2: $SECRETS_FILE is missing" >&2; exit 2; }
  PRQA_SECRETS_FILE="$SECRETS_FILE" python3 -I "$ROOT/scripts/setup/provision_model_proxy_secrets.py" --check \
    || { echo "REFUSING --phase2: credentials not provisioned or out of sync; run scripts/setup/provision_model_proxy_secrets.py" >&2; exit 2; }
  PINNED="$(grep -E '^PRQA_SANDBOX_IMAGE_ID=' "$ENV_FILE" | cut -d= -f2- | tr -d '[:space:]')"
  ACTUAL="$(docker image inspect prqa/opencode-sandbox:local --format '{{.Id}}' 2>/dev/null || true)"
  [[ "$PINNED" =~ ^sha256:[0-9a-f]{64}$ && "$PINNED" == "$ACTUAL" ]] \
    || { echo "REFUSING --phase2: PRQA_SANDBOX_IMAGE_ID does not match prqa/opencode-sandbox:local" >&2; exit 2; }
fi
"${COMPOSE[@]}" up -d "${BUILD[@]}" "${RECREATE[@]}"

echo "waiting for services…"
for i in $(seq 1 60); do
  n8n_ok=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5678/rest/settings || true)
  worker_ok=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/healthz || true)
  mocks_ok=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8090/healthz || true)
  if [[ "$n8n_ok" == "200" && "$worker_ok" == "200" && "$mocks_ok" == "200" ]]; then
    echo "n8n:    http://127.0.0.1:5678  (healthz $n8n_ok)"
    echo "worker: http://127.0.0.1:8080  (healthz $worker_ok, AUTOMATION_MODE=$(curl -s http://127.0.0.1:8080/healthz | python3 -c 'import json,sys; print(json.load(sys.stdin)["automation_mode"])'))"
    echo "mocks:  http://127.0.0.1:8090  (healthz $mocks_ok)"
    if [[ "$PHASE2" == "1" ]]; then
      P2=(prqa-docker-proxy prqa-model-proxy prqa-model-proxy-opencode prqa-launcher)
      [[ ${#BUILD[@]} -gt 0 ]] && "${COMPOSE[@]}" --profile phase2 build prqa-launcher
      "${COMPOSE[@]}" --profile phase2 up -d --no-deps "${RECREATE[@]}" "${P2[@]}"
      for j in $(seq 1 30); do
        running=$("${COMPOSE[@]}" --profile phase2 ps --status running --format '{{.Service}}' "${P2[@]}" | sort | tr '\n' ' ')
        [[ "$running" == "prqa-docker-proxy prqa-launcher prqa-model-proxy prqa-model-proxy-opencode " ]] && break
        sleep 2
      done
      if [[ "$running" != "prqa-docker-proxy prqa-launcher prqa-model-proxy prqa-model-proxy-opencode " ]]; then
        echo "Phase-2 services did not all reach running (running: ${running:-none}):" >&2
        "${COMPOSE[@]}" --profile phase2 logs --tail 30 "${P2[@]}" >&2
        exit 1
      fi
      echo "phase2: ${running}running (review-only; no job is executed while AUTOMATION_MODE=review_only)"
    fi
    "${COMPOSE[@]}" ps
    exit 0
  fi
  sleep 2
done
echo "services did not become healthy in time:" >&2
"${COMPOSE[@]}" ps >&2
"${COMPOSE[@]}" logs --tail 40 >&2
exit 1
