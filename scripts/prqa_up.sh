#!/usr/bin/env bash
# Bring the whole PRQA label pipeline up from the CURRENT project path (what `make run` calls).
#
# Why recreate: the project lives on an NTFS drive that is mounted at login, after dockerd has started. Its mount
# point can change between boots (/media/<user>/<label>, <label>1, <label>2, ...), so containers created on an earlier
# boot point at a path that no longer exists. This script always recreates them from the path it runs from.
#
#   1. checks that the drive is mounted and the secrets file exists
#   2. scripts/start.sh --phase2 --recreate   (n8n, worker, mocks, bridge relay, docker proxy, model proxies, launcher)
#   3. installs the n8n bridge user service with this path and (re)starts it
#   4. makes sure n8n workflow 09 (label trigger, every minute) is active
#   5. prints the auto-runner prerequisites (read-only)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
SECRETS_FILE="${PRQA_SECRETS_FILE:-$HOME/.config/prqa/secrets.env}"
UNIT_DIR="$HOME/.config/systemd/user"
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "1/5 checks"
mnt="$(findmnt -n -o TARGET -T "$ROOT" || true)"
if [[ -z "$mnt" || "$mnt" == "/" || "$mnt" == "/home" ]]; then
  echo "project is not on a mounted drive ($ROOT -> ${mnt:-?}); mount the drive first" >&2; exit 2
fi
[[ -f "$SECRETS_FILE" ]] || { echo "missing $SECRETS_FILE (see 'First-time setup' in README.md)" >&2; exit 2; }
docker info >/dev/null 2>&1 || { echo "docker is not reachable" >&2; exit 2; }
echo "project: $ROOT (drive mounted at $mnt)"

say "2/5 containers (recreated from this path)"
scripts/start.sh --phase2 --recreate

say "3/5 n8n bridge service"
mkdir -p "$UNIT_DIR" "$HOME/.local/state/prqa/bridge-sock"
chmod 700 "$HOME/.local/state/prqa/bridge-sock"
for unit in deploy/systemd/*.service; do
  dest="$UNIT_DIR/$(basename "$unit")"
  # only the bridge is installed; the auto-runner unit is refreshed only if the operator installed it earlier
  [[ "$(basename "$unit")" == "prqa-n8n-bridge.service" || -f "$dest" ]] || continue
  sed "s|@PRQA_ROOT@|$ROOT|g" "$unit" > "$dest.tmp" && mv "$dest.tmp" "$dest"
  echo "installed $dest"
done
systemctl --user daemon-reload
systemctl --user enable prqa-n8n-bridge.service >/dev/null 2>&1 || true
systemctl --user restart prqa-n8n-bridge.service
for _ in $(seq 1 15); do
  [[ -S "$HOME/.local/state/prqa/bridge-sock/bridge.sock" ]] && systemctl --user is-active --quiet prqa-n8n-bridge.service && break
  sleep 1
done
systemctl --user is-active --quiet prqa-n8n-bridge.service \
  || { echo "bridge did not start:" >&2; journalctl --user -u prqa-n8n-bridge -n 30 --no-pager >&2; exit 1; }
echo "bridge: active"

say "4/5 n8n workflow 09 (label trigger)"
if python3 scripts/setup/n8n_api.py list | grep -E 'PrqaLabelTrigger' | grep -qiE 'true|active'; then
  echo "PrqaLabelTrigger: already active"
else
  python3 scripts/setup/n8n_api.py activate PrqaLabelTrigger true
fi
python3 scripts/setup/n8n_api.py list | grep -E 'PrqaLabelTrigger' || true

say "5/5 auto-runner prerequisites"
timeout 240 python3 scripts/pipeline/prqa_auto_runner.py status | python3 -c '
import json, sys
try:
    s = json.load(sys.stdin)
except ValueError:
    print("blockers:\n  - status check gave no result (timed out or crashed); run: make status")
    sys.exit(1)
b = s.get("prerequisite_blockers") or []
print("blockers:", "none" if not b else "")
for x in b: print("  -", x)
for p in s.get("open_prs", []):
    why = "; ".join(p.get("reasons") or ["eligible"])
    print("  PR #%s labels=%s -> %s" % (p["number"], p["labels"], why))
sys.exit(1 if b else 0)
' && printf '\n\033[32mready: add the label "to be test" to a PR of the connected repository; n8n checks every minute.\033[0m\n' \
  || { printf '\n\033[33mstarted, but the blockers above stop a run.\033[0m\n'; exit 1; }
