#!/usr/bin/env bash
# PR QA Pilot sandbox entrypoint — PHASE 2 ONLY.
# Usage (plan built by worker/prqa_worker/opencode_runner.py):
#   prqa-sandbox-entrypoint --repo owner/name --sha <40-hex> --model provider/model --timeout-minutes N \
#       --tree-sha256 <64-hex> --prompt-sha256 <64-hex> --prechecked
#   … --tree-sha256 … --prompt-sha256 … --verify-checkout-only     (verification step alone; tests)
#
# The sandbox NEVER clones and NEVER receives a GitHub token. The worker checks the repository out (pinned SHA, hooks
# disabled) outside the sandbox; a helper container extracts the per-job artifact into the job's named volume, which
# this container mounts READ-ONLY at /checkout:
#   /checkout/repo/…            repository tree without .git (untrusted)
#   /checkout/prqa/manifest.json  trusted: job id, repository, SHA, file count, tree hash, prompt hash
#   /checkout/prqa/prompt.md      trusted: the PRQA-generated prompt
# The read-only mount (and --read-only rootfs) make all of it immutable at run time. Verification (prqa-verify-checkout, shipped in this IMAGE, never in
# the artifact) is fail-closed. Only TOKEN (the staging target's test credential) may arrive through --env-file, when the
# registry opts in, and PRQA_MODEL_TOKEN (the per-job model-proxy token) when the launcher issued one.
#
# Model access: the only provider OpenCode is given is "prqa" — the PRQA model proxy (PRQA_MODEL_BASE_URL, an
# OpenAI-compatible endpoint), authenticated with the per-job token as `Authorization: Bearer` through OpenCode's
# `{env:PRQA_MODEL_TOKEN}` substitution; the token itself is never written into the config. The model must be
# prqa/<upstream model>. OpenCode runs under `env -i` with an explicit allowlist (OPENCODE_SANDBOX_ENV below), so
# nothing else in this container's environment (PRQA_MODEL_BASE_URL included) reaches it.
# See docs/SANDBOX_TRUST_BOUNDARY.md.
set -euo pipefail

REPO=""; SHA=""; MODEL=""; TIMEOUT_MIN="30"; PRECHECKED="0"; VERIFY_ONLY="0"; TREE_SHA=""; PROMPT_SHA=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2;;
    --sha) SHA="$2"; shift 2;;
    --model) MODEL="$2"; shift 2;;
    --timeout-minutes) TIMEOUT_MIN="$2"; shift 2;;
    --tree-sha256) TREE_SHA="$2"; shift 2;;       # worker-held expected hashes: the artifact's own manifest is not enough
    --prompt-sha256) PROMPT_SHA="$2"; shift 2;;
    --prechecked) PRECHECKED="1"; shift 1;;
    --verify-checkout-only) VERIFY_ONLY="1"; PRECHECKED="1"; shift 1;;
    *) echo "unknown argument: $1" >&2; exit 64;;
  esac
done
[[ "$REPO" =~ ^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$ ]] || { echo "invalid repo" >&2; exit 65; }
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid sha" >&2; exit 65; }
[[ "$TIMEOUT_MIN" =~ ^[0-9]{1,4}$ ]] || { echo "invalid timeout" >&2; exit 65; }
[[ "$PRECHECKED" == "1" ]] || { echo "refusing to run without a pre-checked artifact (sandbox never clones)" >&2; exit 66; }
[[ "$VERIFY_ONLY" == "1" || -n "$MODEL" ]] || { echo "model required" >&2; exit 65; }
[[ -n "${PRQA_JOB_ID:-}" ]] || { echo "PRQA_JOB_ID missing" >&2; exit 66; }
[[ "$TREE_SHA" =~ ^[0-9a-f]{64}$ ]] || { echo "--tree-sha256 missing or malformed" >&2; exit 66; }
[[ "$PROMPT_SHA" =~ ^[0-9a-f]{64}$ ]] || { echo "--prompt-sha256 missing or malformed" >&2; exit 66; }

CHECKOUT_DIR="${PRQA_CHECKOUT_DIR:-/checkout}"
VERIFIER="${PRQA_VERIFIER:-/usr/local/bin/prqa-verify-checkout}"
REPO_DIR="$CHECKOUT_DIR/repo"
PROMPT_FILE="$CHECKOUT_DIR/prqa/prompt.md"

# ---- fail-closed verification of the artifact the worker streamed in ----
[[ -f "$VERIFIER" ]] || { echo "verifier $VERIFIER missing from the image" >&2; exit 66; }
[[ -d "$REPO_DIR" && -d "$CHECKOUT_DIR/prqa" ]] || { echo "checkout layout missing ($CHECKOUT_DIR/repo, $CHECKOUT_DIR/prqa)" >&2; exit 66; }
# 1. must be read-only for this user (the container runs with --read-only; outside Docker the same probe applies)
for probe_dir in "$REPO_DIR" "$CHECKOUT_DIR/prqa"; do
  if ( : > "$probe_dir/.prqa-write-probe" ) 2>/dev/null; then
    rm -f "$probe_dir/.prqa-write-probe" 2>/dev/null || true
    echo "$probe_dir is writable; refusing to run" >&2; exit 66
  fi
done
# 2. manifest, identity (repo / SHA / job id), tree hash, prompt hash, no unexpected trusted files
python3 -I "$VERIFIER" "$CHECKOUT_DIR" "$REPO" "$SHA" "$PRQA_JOB_ID" "$TREE_SHA" "$PROMPT_SHA"
[[ "$VERIFY_ONLY" == "1" ]] && exit 0

export PRQA_EVIDENCE_DIR="${PRQA_EVIDENCE_DIR:-/work/evidence}"
mkdir -p "$PRQA_EVIDENCE_DIR"

# ---- model provider: the PRQA model proxy, and nothing else ----
OPENCODE_SANDBOX_ENV=(HOME=/home/sandbox PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 TMPDIR=/tmp
  OPENCODE_DISABLE_PROJECT_CONFIG=1 OPENCODE_PURE=1 OPENCODE_DISABLE_MODELS_FETCH=1 OPENCODE_DISABLE_AUTOUPDATE=1
  OPENCODE_DISABLE_LSP_DOWNLOAD=1 OPENCODE_DISABLE_DEFAULT_PLUGINS=1 OPENCODE_DISABLE_CLAUDE_CODE=1
  OPENCODE_DISABLE_EXTERNAL_SKILLS=1
  "PRQA_JOB_ID=$PRQA_JOB_ID" "PRQA_EVIDENCE_DIR=$PRQA_EVIDENCE_DIR")
for name in PRQA_JOB_JSON PRQA_TEST_LEVEL BASE_URL TOKEN; do
  if [[ -n "${!name:-}" ]]; then OPENCODE_SANDBOX_ENV+=("$name=${!name}"); fi
done
if [[ -n "${PRQA_MODEL_TOKEN:-}" ]]; then
  [[ "$PRQA_MODEL_TOKEN" =~ ^prqa_mt_[A-Za-z0-9_-]{20,128}$ ]] || { echo "PRQA_MODEL_TOKEN is malformed" >&2; exit 67; }
  [[ "${PRQA_MODEL_BASE_URL:-}" =~ ^http://[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?(:[0-9]{1,5})?/v1$ ]] \
    || { echo "PRQA_MODEL_BASE_URL must be http://<proxy host>[:port]/v1" >&2; exit 67; }
  [[ "$MODEL" =~ ^prqa/[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$ ]] || { echo "model must be prqa/<upstream model> (the PRQA model proxy)" >&2; exit 67; }
  [[ "$PRQA_EVIDENCE_DIR" =~ ^/work/[A-Za-z0-9._-]+$ ]] || { echo "PRQA_EVIDENCE_DIR must be a directory directly under /work" >&2; exit 67; }
  # OpenCode works in the read-only repository; the job's evidence directory (sandbox tmpfs) is the one place outside it
  # where it may write (qa-result.json, evidence files). Every other external directory is denied (`opencode run`
  # would auto-reject an "ask" anyway; deny states it).
  OPENCODE_CONFIG_CONTENT="$(python3 -I - "${MODEL#prqa/}" "$PRQA_MODEL_BASE_URL" "$PRQA_EVIDENCE_DIR" <<'PY'
import json, sys
model, base, evidence = sys.argv[1:4]
# Slim agent: the provider allows 8,000 tokens per minute, which bounds every request (input + completion allowance).
# The default OpenCode agent alone is ~8k tokens (long system prompt + 10 tool schemas), so the `prqa` agent has a short
# system prompt and only the bash tool, and the model's context limit makes OpenCode compact a long session before a
# request can outgrow the provider limit. Execution is split per scenario group by prqa-chunked-run.
off = ["edit", "glob", "grep", "read", "skill", "task", "todowrite", "todoread", "webfetch", "write", "list", "patch"]
print(json.dumps({"enabled_providers": ["prqa"], "autoupdate": False, "share": "disabled", "model": "prqa/" + model,
                  "permission": {"external_directory": {"*": "deny", evidence + "/*": "allow"}},
                  "tools": {t: False for t in off},
                  "agent": {"prqa": {"mode": "primary", "description": "PRQA API tester",
                                     "prompt": "You are a senior QA engineer testing an HTTP API with curl through the bash tool. "
                                               "Your ONLY tool is `bash` (arguments: command, description); there is no "
                                               "read, write, edit or browser tool: read and write files with shell commands. "
                                               "Follow the user's instructions exactly, keep command output short, and never "
                                               "modify the repository.",
                                     "tools": {"bash": True}}},
                  "provider": {"prqa": {"npm": "@ai-sdk/openai-compatible", "name": "PRQA model proxy",
                                        "options": {"baseURL": base, "apiKey": "{env:PRQA_MODEL_TOKEN}"},
                                        "models": {model: {"name": model, "tool_call": True, "limit": {"context": 6500, "output": 1500}}}}}}))
PY
)"
  OPENCODE_SANDBOX_ENV+=("OPENCODE_CONFIG_CONTENT=$OPENCODE_CONFIG_CONTENT" "PRQA_MODEL_TOKEN=$PRQA_MODEL_TOKEN")
elif [[ "$MODEL" == prqa/* ]]; then
  echo "model $MODEL needs the PRQA model proxy, but no per-job model token was provided" >&2; exit 67
fi

# Run OpenCode non-interactively with the TRUSTED prompt (never a file from repo/). --pure disables external plugins.
# NOTE: OpenCode can still execute commands that repository content talks it into; the container limits (read-only
# root, tmpfs, cap-drop, pids/memory, network per launcher) bound what that can reach. See docs/SANDBOX_TRUST_BOUNDARY.md.
# One OpenCode session per scenario group (prqa-chunked-run), then a deterministic qa-result.json from the per-scenario
# records; see /usr/local/bin/prqa-chunked-run for why.
timeout --signal=TERM --kill-after=60 "$((TIMEOUT_MIN * 60))" \
  env -i "${OPENCODE_SANDBOX_ENV[@]}" python3 -I /usr/local/bin/prqa-chunked-run --prompt "$PROMPT_FILE" --repo-dir "$REPO_DIR" \
  --evidence "$PRQA_EVIDENCE_DIR" --model "$MODEL" --timeout-s "$((TIMEOUT_MIN * 60))" --prompt-sha256 "$PROMPT_SHA" \
  > "$PRQA_EVIDENCE_DIR/opencode-events.jsonl" 2> "$PRQA_EVIDENCE_DIR/opencode-stderr.log" || true

# The worker reads the last stdout line as the result document.
if [[ -f "$PRQA_EVIDENCE_DIR/qa-result.json" ]]; then
  python3 -I -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))))' "$PRQA_EVIDENCE_DIR/qa-result.json"
else
  python3 -I - "$PRQA_JOB_ID" "$REPO" "$SHA" "$PRQA_EVIDENCE_DIR" <<'PY'
import json, os, re, sys
job_id, repo, sha, evidence = sys.argv[1:5]
try:
    pr = json.loads(os.environ.get("PRQA_JOB_JSON") or "{}").get("pr") or {}
except ValueError:
    pr = {}
number = pr.get("number") if isinstance(pr.get("number"), int) and pr.get("number") > 0 else 0
blockers = ["qa-result.json missing after OpenCode run; see opencode-stderr.log"]
# Name the cause when it is a model-side refusal (fixed phrases only; no log content is copied into the result).
logs = ""
for name in ("opencode-events.jsonl", "opencode-stderr.log"):
    try:
        with open(os.path.join(evidence, name), encoding="utf-8", errors="replace") as f:
            logs += f.read()[-200000:]
    except OSError:
        pass
causes = [(r"provider budget[^\n]*tokens-per-minute", "model proxy refused every model request: provider budget (one request exceeds the tokens-per-minute limit)"),
          (r"provider budget", "model proxy refused model requests: provider budget"),
          (r"\b429\b|rate.?limit", "model requests were rate limited (HTTP 429)"),
          (r"\b413\b|request too large", "model request too large for the provider (HTTP 413)"),
          (r"\b401\b|\b403\b|unauthori[sz]ed|forbidden", "model proxy rejected the per-job model token")]
for rx, text in causes:
    if re.search(rx, logs, re.I):
        blockers.append(text)
        break
print(json.dumps({
  "result_version": "1.0", "synthetic": False, "job_id": job_id, "repository": repo,
  "pr": {"number": number, "title": str(pr.get("title") or "")[:300], "head_sha": sha},
  "environment": {"name": "staging", "base_url_redacted": "unknown"},
  "executed_by": {"engine": "opencode", "mode": "live"},
  "summary": {"overall_status": "INCONCLUSIVE", "total": 0, "passed": 0, "failed": 0, "blocked": 0, "not_applicable": 0,
              "executive_summary": "OpenCode did not produce qa-result.json; no test case was executed. " + blockers[-1]},
  "coverage_matrix": [], "test_cases": [], "defects": [],
  "blockers": blockers,
  "recommendation": {"verdict": "INCONCLUSIVE", "rationale": "no machine-readable result"},
}))
PY
fi
