#!/usr/bin/env bash
# Install everything PRQA needs on a Debian/Ubuntu machine (what `make install` calls). Safe to re-run: anything already
# present is skipped. Needs sudo only when an apt package is missing or the user is not yet in the `docker` group.
#
#   scripts/setup/install.sh           # install what is missing
#   scripts/setup/install.sh --check   # report only, change nothing (exit 1 if something is missing)
#
#   1. system packages (apt): Docker + Compose, git, gh, curl, openssl, jq, bubblewrap, pipx, Python venv/yaml/jsonschema
#   2. docker group membership for the current user
#   3. Poetry (pipx) and the Python dev environment (poetry install)
#   4. agent CLI: Claude Code (default engine, official installer) and its login; OpenCode only when
#      PRQA_LOCAL_ENGINE=opencode
#   5. Docker images: n8n, Postgres (disposable API databases), the sandbox image (built locally)
#   6. private config directory ~/.config/prqa (mode 700)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
CHECK=0
case "${1:-}" in
  --check) CHECK=1 ;;
  "") ;;
  *) echo "usage: $0 [--check]" >&2; exit 64 ;;
esac

# The host services run on the system python3, so yaml/jsonschema come from apt; poetry adds the dev tools.
APT_PACKAGES=(docker.io docker-compose git gh curl openssl jq bubblewrap pipx python3 python3-venv python3-yaml python3-jsonschema)
N8N_IMAGE="docker.n8n.io/n8nio/n8n"
N8N_DEFAULT_TAG="2.44.5"
POSTGRES_IMAGE="postgres:16-alpine"
SANDBOX_IMAGE="prqa/opencode-sandbox:local"
MISSING=0

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m       %s\n' "$*"; }
todo() { printf '  \033[33mmissing\033[0m  %s\n' "$*"; MISSING=1; }
run()  { if [[ "$CHECK" == 1 ]]; then return 0; fi; "$@"; }

say "1/6 system packages"
if ! command -v apt-get >/dev/null; then
  echo "  apt-get not found: install these yourself: ${APT_PACKAGES[*]}" >&2
else
  need=()
  for p in "${APT_PACKAGES[@]}"; do
    if dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed"; then ok "$p"; else todo "$p"; need+=("$p"); fi
  done
  if [[ ${#need[@]} -gt 0 && "$CHECK" == 0 ]]; then
    sudo apt-get update && sudo apt-get install -y --no-install-recommends "${need[@]}"
  fi
fi

say "2/6 docker access"
if id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
  ok "$USER is in the docker group"
else
  todo "$USER is not in the docker group"
  run sudo usermod -aG docker "$USER"
  [[ "$CHECK" == 0 ]] && echo "  added: log out and back in (or run: newgrp docker), then re-run make install"
fi
if docker info >/dev/null 2>&1; then ok "docker daemon reachable"; else todo "docker daemon not reachable (sudo systemctl enable --now docker)"; fi

say "3/6 Poetry + Python environment"
export PATH="$HOME/.local/bin:$PATH"
if command -v poetry >/dev/null; then ok "$(poetry --version)"; else todo "poetry"; run pipx install poetry; fi
if [[ "$CHECK" == 0 ]]; then
  poetry install --no-interaction
elif poetry env info -p >/dev/null 2>&1 && poetry run python -c "import yaml, jsonschema" 2>/dev/null; then
  ok "poetry environment ($(poetry env info -p))"
else
  todo "poetry environment (poetry install)"
fi

say "4/6 agent CLI"
ENGINE="claude"
if [[ -f compose/.env ]]; then
  e="$(sed -n 's/^PRQA_LOCAL_ENGINE=//p' compose/.env | tr -d '[:space:]')"
  [[ -n "$e" ]] && ENGINE="$e"
fi
CLAUDE="$(command -v claude || true)"
[[ -z "$CLAUDE" && -x "$HOME/.local/bin/claude" ]] && CLAUDE="$HOME/.local/bin/claude"
if [[ -n "$CLAUDE" ]]; then
  ok "claude $("$CLAUDE" --version 2>/dev/null | head -1) ($CLAUDE)"
else
  todo "claude (Claude Code CLI)"
  if [[ "$CHECK" == 0 ]]; then curl -fsSL https://claude.ai/install.sh | bash; fi
fi
if [[ -s "$HOME/.claude/.credentials.json" ]]; then
  ok "Claude Code login"
else
  todo "Claude Code login: run 'claude' once and sign in (/login)"
fi
if [[ "$ENGINE" == "opencode" ]]; then
  OPENCODE="$(command -v opencode || true)"
  [[ -z "$OPENCODE" && -x "$HOME/.opencode/bin/opencode" ]] && OPENCODE="$HOME/.opencode/bin/opencode"
  if [[ -n "$OPENCODE" ]]; then
    ok "opencode $("$OPENCODE" --version 2>/dev/null | head -1) ($OPENCODE)"
  else
    todo "opencode (PRQA_LOCAL_ENGINE=opencode)"
    if [[ "$CHECK" == 0 ]]; then curl -fsSL https://opencode.ai/install | bash; fi
  fi
fi

say "5/6 Docker images"
N8N_TAG="$N8N_DEFAULT_TAG"
if [[ -f compose/.env ]]; then
  t="$(sed -n 's/^N8N_IMAGE_TAG=//p' compose/.env | tr -d '[:space:]')"
  [[ -n "$t" && "$t" != "latest" ]] && N8N_TAG="$t"
fi
for img in "$N8N_IMAGE:$N8N_TAG" "$POSTGRES_IMAGE"; do
  if docker image inspect "$img" >/dev/null 2>&1; then ok "$img"; else todo "$img"; run docker pull "$img"; fi
done
if docker image inspect "$SANDBOX_IMAGE" >/dev/null 2>&1; then
  ok "$SANDBOX_IMAGE"
else
  todo "$SANDBOX_IMAGE (built from worker/sandbox/Dockerfile.opencode)"
  if [[ "$CHECK" == 0 ]]; then
    docker build -f worker/sandbox/Dockerfile.opencode -t "$SANDBOX_IMAGE" worker/sandbox
    # start.sh --phase2 refuses unless compose/.env pins the exact image id
    if [[ -f compose/.env ]]; then
      id="$(docker image inspect "$SANDBOX_IMAGE" --format '{{.Id}}')"
      sed -i "s|^PRQA_SANDBOX_IMAGE_ID=.*|PRQA_SANDBOX_IMAGE_ID=$id|" compose/.env
      echo "  pinned PRQA_SANDBOX_IMAGE_ID in compose/.env"
    fi
  fi
fi
echo "  (worker, mocks and launcher images are built by make run)"

say "6/6 private config"
CFG="$HOME/.config/prqa"
if [[ -d "$CFG" ]]; then ok "$CFG"; else todo "$CFG"; run mkdir -p "$CFG"; fi
run chmod 700 "$CFG" 2>/dev/null || true
if [[ -f "$CFG/secrets.env" ]]; then
  ok "$CFG/secrets.env"
else
  todo "$CFG/secrets.env — create it (see 'First-time setup' in README.md)"
fi

echo
if [[ "$CHECK" == 1 ]]; then
  [[ "$MISSING" == 0 ]] && printf '\033[32meverything is installed.\033[0m\n' || { printf '\033[33msome items are missing: run make install\033[0m\n'; exit 1; }
else
  printf '\033[32minstall finished.\033[0m Next: secrets (README → First-time setup), then: make run\n'
fi
