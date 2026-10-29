#!/usr/bin/env python3
"""One real, API-only PRQA run against ONE PR of the connected project (Kiramido1/Amenly-Product), in four explicit, separately invoked stages.
PRQA stays review-only; nothing global changes; the only execution path is a consumed single-run grant.

    python3 scripts/pipeline/prqa_run_single_pr.py preflight
        Read-only checks (gh login, worker/launcher/model proxy/sandbox network/images, registry entry, secret NAMES,
        grant key presence, global invariants). Prints which blockers stop `preview` and which stop `execute`.

    python3 scripts/pipeline/prqa_run_single_pr.py preview [--pr N] [--no-env]
        Discovery (the poller's gh-cli dry run + every open PR with its eligibility reasons) → exactly one eligible PR
        (lowest number; --pr pins one but it must still be eligible) → exact head commit fetched into a private
        workspace (host-side, through the gh credential helper; never passed on) → disposable project API + Postgres
        (scripts/pipeline/prqa_disposable_env.py, synthetic seed, verified from the sandbox network) → context from the worker
        (POST /context/build, with the OpenAPI document of the running head) → prompt from the worker
        (POST /prompts/finalize: the deterministic reference fallback; this runner never calls a model) → the shipped
        quality gate (validator + 85-point rubric + hard fails) → saves and PRINTS the complete prompt.
        Creates no job, consumes no grant, calls no provider.

    python3 scripts/pipeline/prqa_run_single_pr.py preview --pr N --preview-only [--openapi-file F --openapi-head-sha SHA]
        Read-only prompt preview of ONE explicitly selected PR that need not carry the trigger label (it must still be
        open, non-draft, same-repository, base in the allowed list). No checkout, no disposable environment, no poller, no job,
        no grant, no provider. The OpenAPI document is used only when --openapi-head-sha equals the PR's current head
        (the caller vouches that the file was generated at that commit; its sha256 is recorded). The head is re-read
        after the prompt is built; a changed/closed/draft PR stops the preview. Output goes to
        runs/prompt-previews/<slug>-pr-<n>-<sha>/ with `preview_only: true`, `execution_eligible: false`, and
        `execute` refuses such a directory (PREVIEW_ONLY_NOT_EXECUTABLE).

    python3 scripts/pipeline/prqa_run_single_pr.py execute --run-dir DIR --execute \\
            --confirm-repo Kiramido1/Amenly-Product --confirm-pr N --confirm-sha <40 hex> --grant-file FILE
        Separate, explicit approval of the reviewed prompt. Refuses unless: the confirmation equals the preview; the
        preview is still `previewed`; the prompt file hash equals the previewed hash; the gate accepted the prompt;
        GitHub still shows the same open, non-draft, same-repo PR on an allowed base with the exact label `to be test` and
        the same head (otherwise the preview is invalidated); the worker is review-only with GITHUB_TOKEN and grants
        configured; the registry base_url is the disposable API; the API answers from the sandbox network; the grant
        (scripts/pipeline/prqa_authorize_run.py) is bound to exactly this repo/PR/SHA/level and unexpired. Then ONE
        POST /jobs carrying the prompt and the grant: the worker consumes the grant atomically and the existing
        worker → launcher → OpenCode sandbox → model proxy path runs it. Waits, saves job/result/events and the ledger.

    python3 scripts/pipeline/prqa_run_single_pr.py cleanup --run-dir DIR
        Tears down only this run's disposable environment (label-checked) and private workspace.

Evidence: runs/prqa-local-pr-runs/<slug>-pr-<number>-<sha>/ (qa-prompt.md, metadata.json, quality.json,
context.json, discovery.json, preview.json, env/, and after execute: job.json, result.json, execution.json).
No credential value is ever printed, written or put in argv by this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "worker"))
from prqa_worker import run_grants  # noqa: E402

# Connected projects. `private` repositories need the worker's read-only GITHUB_TOKEN for the pinned checkout (or, in a
# simulation, a local mirror); public ones are checked out anonymously over https. `env_profile` is the recipe in
# scripts/pipeline/prqa_disposable_env.py. select_project() switches the module to one of them (default: DEFAULT_REPO).
PROJECTS: dict[str, dict[str, Any]] = {
    "Kiramido1/Amenly-Product": {"slug": "amenly", "env_profile": "amenly", "bases": ("main",), "private": False},
}
DEFAULT_REPO = "Kiramido1/Amenly-Product"
REPO = DEFAULT_REPO
SLUG = "amenly"
ENV_PROFILE = "amenly"
PRIVATE_REPO = False
TRIGGER_LABEL = "to be test"
ALLOWED_BASES: tuple[str, ...] = ("main",)
LEVEL = "standard"
JOB_LABEL = "qa-test:standard"
API_ALIAS = "prqa-target-api"
EXPECTED_BASE_URL = f"http://{API_ALIAS}:8000"
SANDBOX_NETWORK = "prqa_prqa_sandbox"
SANDBOX_IMAGE = "prqa/opencode-sandbox:local"
RUNS_DIR = ROOT / "runs" / "prqa-local-pr-runs"
PREVIEWS_DIR = ROOT / "runs" / "prompt-previews"
PRIVATE_ROOT = Path.home() / ".cache" / "prqa" / "single-pr-runs"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TERMINAL = ("succeeded", "failed", "cancelled")


def select_project(repo: str) -> None:
    global REPO, SLUG, ENV_PROFILE, PRIVATE_REPO, ALLOWED_BASES
    if repo not in PROJECTS:
        raise SystemExit(f"unknown project {repo!r}; connected: {', '.join(PROJECTS)}")
    p = PROJECTS[repo]
    REPO, SLUG, ENV_PROFILE, PRIVATE_REPO, ALLOWED_BASES = repo, p["slug"], p["env_profile"], p["private"], tuple(p["bases"])


class Blocked(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def rel(path: Path) -> str:
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def env_file_values(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.strip())
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


# --------------------------------------------------------------------------------------------------------- host --
class Host:
    """Every side effect of the runner. Tests replace it; nothing else touches GitHub, Docker or the worker."""

    def __init__(self) -> None:
        self.secrets_file = Path(os.environ.get("PRQA_SECRETS_FILE") or Path.home() / ".config/prqa/secrets.env")
        self.worker_url = os.environ.get("PRQA_WORKER_URL_HOST", "http://127.0.0.1:8080")

    # GitHub (host-side, read-only, through the authenticated gh CLI; its token is never read)
    def _poller(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("prqa_label_poller_runner", ROOT / "scripts" / "pipeline" / "prqa_label_poller.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    def gh_get(self, path: str) -> Any:
        return self._poller().GhCliReader().get(path)[0]

    def gh_paginate(self, path: str) -> list[Any]:
        return self._poller().GhCliReader().paginate(path)

    def gh_diff(self, number: int) -> str:
        p = subprocess.run(["gh", "api", "--method", "GET", "-H", "Accept: application/vnd.github.diff", f"/repos/{REPO}/pulls/{number}"],
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise Blocked("GITHUB_DIFF_UNAVAILABLE", f"gh api exit {p.returncode}")
        return p.stdout

    def gh_auth_ok(self) -> bool:
        return subprocess.run(["gh", "auth", "status", "-h", "github.com"], capture_output=True, text=True, timeout=30).returncode == 0

    def poll_select(self) -> dict[str, Any]:
        mod = self._poller()
        env = {"PRQA_POLL_REPOSITORIES": REPO, "PRQA_POLL_GITHUB_BACKEND": "gh-cli", "PRQA_POLL_BASE_BRANCHES": ",".join(ALLOWED_BASES),
               "PRQA_TRIGGER_LABEL": TRIGGER_LABEL}
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            return mod.run_once(env, dry_run=True, out=lambda s: None, state_path=Path(tmp) / "state.json", gh_reader=mod.GhCliReader())

    def fetch_commit(self, workdir: Path, sha: str) -> str:
        """Exact commit into a fresh private repo; the gh credential helper is used host-side by git only."""
        workdir.mkdir(parents=True, exist_ok=True)
        git = ["git", "-C", str(workdir)]
        subprocess.run([*git, "init", "-q"], check=True, timeout=30)
        f = subprocess.run([*git, "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", "-c", "core.hooksPath=/dev/null",
                            "fetch", "-q", "--no-tags", "--depth", "1", f"https://github.com/{REPO}.git", sha], capture_output=True, text=True, timeout=600)
        if f.returncode != 0:
            raise Blocked("FETCH_FAILED", (f.stderr or "")[-300:])
        got = subprocess.run([*git, "rev-parse", "FETCH_HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        return got

    # secrets: names only (values never leave this object, except the grant key used for local verification)
    def secret_names(self) -> set[str]:
        return {k for k, v in env_file_values(self.secrets_file).items() if v}

    def discord_url(self) -> str:
        return env_file_values(self.secrets_file).get("DISCORD_WEBHOOK_URL", "")

    def grant_key(self) -> str:
        return env_file_values(self.secrets_file).get("PRQA_RUN_GRANT_KEY", "")

    # worker (loopback; worker token read from compose/.env, never printed)
    def worker(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        token = env_file_values(ROOT / "compose" / ".env").get("PRQA_WORKER_TOKEN", "")
        req = urllib.request.Request(self.worker_url + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"content-type": "application/json", "x-worker-token": token})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"null")
            except ValueError:
                return e.code, None
        except (urllib.error.URLError, OSError) as e:
            return 0, {"error": "WORKER_UNREACHABLE", "detail": type(e).__name__}

    # docker (inspection only, plus the disposable-env script)
    def docker_lines(self, *args: str) -> tuple[int, str]:
        p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout.strip()

    def _dx(self, *args: str, timeout: int = 2400) -> tuple[int, Any]:
        p = subprocess.run([sys.executable, str(ROOT / "scripts" / "pipeline" / "prqa_disposable_env.py"), *args], capture_output=True, text=True, timeout=timeout)
        out = p.stdout.strip()
        try:   # the first JSON document on stdout (the script may print more after it, e.g. a teardown summary)
            data = json.JSONDecoder().raw_decode(out[out.index("{"):])[0] if "{" in out else {}
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        if p.returncode != 0:
            err = [l for l in (p.stderr or "").strip().splitlines() if l.strip()]
            if err:                                    # SystemExit message of the env script: one readable line
                data.setdefault("error", err[-1][-500:])
            elif data.get("healthy") is False:
                data.setdefault("error", f"API not healthy from the sandbox network (probe: {', '.join(data.get('probe') or []) or 'no answer'}; "
                                         "see env/api.log)")
            else:
                data.setdefault("error", f"disposable environment script exited {p.returncode}")
        return p.returncode, data

    def env_up(self, *, run_id: str, git_dir: Path, sha: str, state_dir: Path, private_dir: Path) -> tuple[int, Any]:
        return self._dx("up", "--run-id", run_id, "--git-dir", str(git_dir), "--sha", sha, "--state-dir", str(state_dir),
                        "--private-dir", str(private_dir), "--seed", "--sandbox-network", SANDBOX_NETWORK, "--api-alias", API_ALIAS,
                        "--profile", ENV_PROFILE)

    def env_probe(self, state_dir: Path) -> tuple[int, Any]:
        return self._dx("probe", "--state-dir", str(state_dir), timeout=180)

    def env_auth_probe(self, state_dir: Path, register_path: str, me_path: str) -> tuple[int, Any]:
        return self._dx("auth-probe", "--state-dir", str(state_dir), "--register-path", register_path, "--me-path", me_path, timeout=180)

    def env_down(self, state_dir: Path) -> tuple[int, Any]:
        return self._dx("down", "--state-dir", str(state_dir), timeout=300)

    def ledger(self) -> dict[str, Any] | None:
        try:
            sys.path.insert(0, str(ROOT / "proxy"))
            import provider_budget as pb  # type: ignore
            # the ledger of the proxy OpenCode jobs use (prqa-model-proxy-opencode when configured, else the shared one)
            cvals, svals = env_file_values(ROOT / "compose" / ".env"), env_file_values(self.secrets_file)
            env = {k: v for k, v in cvals.items() if k.startswith("PRQA_BUDGET_")}
            if svals.get("PRQA_OPENCODE_BUDGET_DIR"):
                env["PRQA_PROVIDER_BUDGET_DIR"] = svals["PRQA_OPENCODE_BUDGET_DIR"]
                env["PRQA_BUDGET_TOKENS_PER_MINUTE"] = cvals.get("PRQA_OPENCODE_BUDGET_TOKENS_PER_MINUTE") or env.get("PRQA_BUDGET_TOKENS_PER_MINUTE", "")
            else:
                env["PRQA_PROVIDER_BUDGET_DIR"] = svals.get("PRQA_PROVIDER_BUDGET_DIR") or str(Path.home() / ".config/prqa/budget")
            s = pb.Ledger(pb.BudgetConfig.from_env(env)).snapshot()
            return {k: s.get(k) for k in ("requests_today", "tokens_today", "in_flight")}
        except Exception as exc:  # noqa: BLE001
            return {"error": type(exc).__name__}

    def registry_entry(self) -> dict[str, Any] | None:
        from prqa_worker.policy import Policy
        from prqa_worker.registry import Registry
        e = Registry.load(ROOT / "config" / "repositories.yaml", Policy.from_env({})).get(REPO)
        if e is None:
            return None
        return {"kind": e.kind, "enabled": e.enabled, "approval_status": e.approval_status, "allowed_labels": list(e.allowed_labels),
                "environment": (e.api or {}).get("environment"), "base_url": (e.api or {}).get("base_url")}

    def clock(self) -> float:
        return time.time()

    def sleep(self, s: float) -> None:
        time.sleep(s)


# ------------------------------------------------------------------------------------------------- eligibility --
def eligibility(pr: dict[str, Any], *, require_label: bool = True) -> list[str]:
    """Reasons a PR is NOT eligible (empty = eligible). Same rules as the poller and prqa_authorize_run --verify-github.
    require_label=False is used ONLY by the preview-only path, which can never execute."""
    reasons = []
    if pr.get("state") != "open":
        reasons.append("not open")
    if pr.get("draft"):
        reasons.append("draft")
    if ((pr.get("head") or {}).get("repo") or {}).get("full_name") != REPO:
        reasons.append("head is a fork / another repository")
    if (pr.get("base") or {}).get("ref") not in ALLOWED_BASES:
        reasons.append(f"base {(pr.get('base') or {}).get('ref')!r} not in {'/'.join(ALLOWED_BASES)}")
    labels = [l.get("name") for l in pr.get("labels") or []]
    if require_label and TRIGGER_LABEL not in labels:
        reasons.append(f"label {TRIGGER_LABEL!r} missing (labels: {', '.join(map(str, labels)) or 'none'})")
    if not SHA_RE.match(str((pr.get("head") or {}).get("sha") or "")):
        reasons.append("head sha malformed")
    return reasons


def discover(host: Host, pinned: int | None) -> dict[str, Any]:
    prs = host.gh_paginate(f"/repos/{REPO}/pulls?state=open")
    table = [{"number": p["number"], "title": p.get("title", "")[:120], "base": (p.get("base") or {}).get("ref"),
              "draft": bool(p.get("draft")), "head_sha": (p.get("head") or {}).get("sha"),
              "labels": [l.get("name") for l in p.get("labels") or []], "reasons": eligibility(p)} for p in prs]
    eligible = sorted((t for t in table if not t["reasons"]), key=lambda t: t["number"])
    poll = host.poll_select()
    poll_sel = (poll.get("selected") or {}).get("number")
    out = {"at": now_iso(), "open_prs": table, "eligible": [t["number"] for t in eligible], "poller_selected": poll_sel,
           "poller_candidates": [c.get("number") for c in poll.get("candidates") or []], "selected": None}
    if pinned is not None:
        hit = [t for t in eligible if t["number"] == pinned]
        if not hit:
            why = next((t["reasons"] for t in table if t["number"] == pinned), ["not an open PR"])
            out["blocker"] = f"PR #{pinned} is not eligible: {'; '.join(why)}"
            return out
        out["selected"] = pinned
    elif eligible:
        out["selected"] = eligible[0]["number"]
    else:
        out["blocker"] = f"no eligible PR (open, non-draft, same repository, base {'/'.join(ALLOWED_BASES)}, exact label '{TRIGGER_LABEL}')"
        return out
    if out["selected"] not in out["poller_candidates"]:
        out["blocker"] = f"poller disagrees: PR #{out['selected']} is not among its candidates {out['poller_candidates']}"
        out["selected"] = None
    return out


SIM_MIRROR_PREFIX = "file:///data/mirrors"


def discover_simulation(host: Host, number: int) -> dict[str, Any]:
    """SIMULATION ONLY: an explicit operator-chosen PR, no trigger label and no poller agreement. Every other
    eligibility rule (open, non-draft, same repository, allowed base, valid head) still applies."""
    pr = host.gh_get(f"/repos/{REPO}/pulls/{number}")
    reasons = eligibility(pr, require_label=False)
    out = {"at": now_iso(), "simulation": True, "open_prs": [], "eligible": [] if reasons else [number],
           "poller_selected": None, "poller_candidates": [], "selected": None if reasons else number,
           "trigger": "manual simulation (label not required)"}
    if reasons:
        out["blocker"] = f"PR #{number} is not eligible for a simulation: {'; '.join(reasons)}"
    return out


# --------------------------------------------------------------------------------------------------- preflight --
def preflight(host: Host) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, stage: str, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "blocks": None if ok else stage, "detail": detail})

    check("gh CLI authenticated (host-side discovery only)", host.gh_auth_ok(), "preview")
    st, health = host.worker("GET", "/healthz")
    check("worker reachable", st == 200, "preview", f"HTTP {st}")
    check("AUTOMATION_MODE=review_only", st == 200 and (health or {}).get("automation_mode") == "review_only", "preview")
    st, cfg = host.worker("GET", "/config")
    cfg = cfg if st == 200 and isinstance(cfg, dict) else {}
    eps = cfg.get("endpoints") or {}
    check("PROMPT_LLM_LIVE_IN_REVIEW_ONLY=false (prompt LLM stays mock)", eps.get("prompt_llm_live_in_review_only") is False, "preview")
    flags = ((cfg.get("policy") or {}).get("flags") or {})
    real_on = [k for k, v in flags.items() if k.lower().startswith("real_") and v]
    check("all REAL_* flags false", not real_on, "preview", ",".join(real_on))
    ws, launch = cfg.get("workspaces") or {}, cfg.get("launch") or {}
    if PRIVATE_REPO:
        check("worker has the read-only GITHUB_TOKEN (pinned checkout of the private repo)", bool(ws.get("github_token_configured")), "execute")
    else:
        check("worker checks out from GitHub (public repository: anonymous https)", (ws.get("checkout_remote") or "https://github.com") == "https://github.com",
              "execute", str(ws.get("checkout_remote")))
    check("worker has single-run grants enabled (PRQA_RUN_GRANT_KEY)", bool(launch.get("single_run_grants_enabled")), "execute")
    check("worker sandbox network", ws.get("sandbox_network") == SANDBOX_NETWORK, "execute", str(ws.get("sandbox_network")))
    names = host.secret_names()
    if PRIVATE_REPO:
        check("GITHUB_TOKEN present in the secrets file (name only)", "GITHUB_TOKEN" in names, "execute")
    check("PRQA_RUN_GRANT_KEY present in the secrets file (name only)", run_grants.KEY_RE.match(host.grant_key() or "") is not None, "execute",
          "run: python3 scripts/pipeline/prqa_authorize_run.py --init-key, then restart the worker")
    for name in ("prqa-prqa-launcher-1", "prqa-prqa-model-proxy-1", "prqa-prqa-model-proxy-opencode-1"):
        rc, out = host.docker_lines("inspect", "--format", "{{.State.Running}}", name)
        check(f"{name} running", rc == 0 and out == "true", "execute")
    rc, out = host.docker_lines("network", "inspect", "--format", '{{.Internal}} {{index .Labels "prqa.net"}}', SANDBOX_NETWORK)
    check("sandbox network internal + labelled", rc == 0 and out == "true sandbox", "preview", out)
    for image in (SANDBOX_IMAGE, "postgres:16-alpine"):
        rc, _ = host.docker_lines("image", "inspect", "--format", "{{.Id}}", image)
        check(f"image {image} present", rc == 0, "preview")
    entry = host.registry_entry()
    check("registry entry exists (kind real)", bool(entry and entry["kind"] == "real"), "preview")
    if entry:
        check("registry environment is not production", entry["environment"] != "production", "preview", str(entry["environment"]))
        check(f"registry api.base_url = {EXPECTED_BASE_URL} (the disposable API)", entry["base_url"] == EXPECTED_BASE_URL, "execute",
              f"currently {entry['base_url']!r}; the launcher passes this to the sandbox as BASE_URL")
        check(f"registry allows label {JOB_LABEL}", JOB_LABEL in entry["allowed_labels"], "execute")
        check(f"{REPO} stays disabled + pending_approval (grant path only)", not entry["enabled"] and entry["approval_status"] == "pending_approval",
              "execute", f"enabled={entry['enabled']} approval={entry['approval_status']}")
    ledger = host.ledger()
    check("provider budget ledger readable", bool(ledger) and "error" not in ledger, "execute", json.dumps(ledger))
    return {"at": now_iso(), "checks": checks, "ledger": ledger,
            "blocks_preview": [c["check"] for c in checks if c["blocks"] == "preview"],
            "blocks_execute": [c["check"] for c in checks if c["blocks"] in ("preview", "execute")]}


# ----------------------------------------------------------------------------------------------------- preview --
def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")


def score_prompt(text: str, context: dict[str, Any]) -> dict[str, Any]:
    """The shipped gate on the prompt body (what scripts/eval/evaluate_prompt_quality.py Track A scores for fallbacks)."""
    from prqa_worker import qa_rubric
    from prqa_worker.prompt_builder import Templates, reference_fallback_prompt, validate_generated_prompt
    t = Templates.load(ROOT / "prompts")
    pr = context.get("pr") or {}
    body = reference_fallback_prompt(t, repository=REPO, pr_number=int(pr.get("number") or 0), test_level=LEVEL, context=context)
    consistent = text.startswith(body.rstrip()[:400])
    gate = validate_generated_prompt(body, pr_title=str(pr.get("title") or ""), context=context)
    rub = qa_rubric.score_prompt(body, pr_title=str(pr.get("title") or ""), context=context, gate=gate)
    acc = qa_rubric.accept_result(rub, gate)
    if not consistent:
        acc = dict(acc, accepted=False, failure="worker_local_builder_mismatch")
    return {"accepted": bool(acc["accepted"]), "failure": acc.get("failure"), "score": rub.quality_score, "threshold": qa_rubric.THRESHOLD,
            "hard_fails": rub.hard_fails, "gate_errors": gate.get("errors"), "gate_warnings": gate.get("warnings"),
            "human_flags": rub.human_flags, "dimensions": {k: d.score for k, d in rub.dimensions.items()},
            "worker_and_local_builder_consistent": consistent}


def final_gate(text: str, context: dict[str, Any]) -> dict[str, Any]:
    """The shipped gate on the COMPLETE text OpenCode receives (body + identity section + result contract)."""
    from prqa_worker import qa_rubric
    from prqa_worker.prompt_builder import validate_generated_prompt
    title = str((context.get("pr") or {}).get("title") or "")
    gate = validate_generated_prompt(text, pr_title=title, context=context)
    rub = qa_rubric.score_prompt(text, pr_title=title, context=context, gate=gate)
    acc = qa_rubric.accept_result(rub, gate)
    return {"accepted": bool(acc["accepted"]), "failure": acc.get("failure"), "score": rub.quality_score, "hard_fails": rub.hard_fails,
            "gate_errors": gate.get("errors"), "gate_warnings": gate.get("warnings")}


def gate_all(text: str, context: dict[str, Any]) -> dict[str, Any]:
    """Body gate (score_prompt) AND the gate on the complete final text. Accepted only when both accept."""
    q = score_prompt(text, context)
    f = final_gate(text, context)
    return dict(q, accepted=bool(q["accepted"] and f["accepted"]), failure=q.get("failure") or (None if f["accepted"] else f.get("failure") or "final_text_rejected"),
                final_text=f)


def auth_paths(openapi: dict[str, Any] | None) -> dict[str, str] | None:
    """Self-registration + current-user operations, ONLY when the OpenAPI document of the running head documents them
    (POST …/auth/register with email+password in its body, GET …/auth/me secured by a bearer scheme)."""
    if not isinstance(openapi, dict):
        return None
    paths = openapi.get("paths") or {}
    schemas = (openapi.get("components") or {}).get("schemas") or {}
    reg = me = None
    for path, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        if path.endswith("/auth/register") and isinstance(ops.get("post"), dict):
            media = ((ops["post"].get("requestBody") or {}).get("content") or {}).get("application/json") or {}
            ref = str((media.get("schema") or {}).get("$ref") or "").rsplit("/", 1)[-1]
            props = set((schemas.get(ref) or {}).get("properties") or {})
            if {"email", "password"} <= props:
                reg = path
        if path.endswith("/auth/me") and isinstance(ops.get("get"), dict) and ops["get"].get("security"):
            me = path
    return {"register": reg, "me": me} if reg and me else None


CONTRACT_HEADING = "## 15. Machine-Readable Output"


def identity_section(paths: dict[str, str]) -> str:
    return (f"## 14. Test Identities (disposable environment — appended by PR QA Pilot)\n\n"
            f"`$BASE_URL` is a disposable environment built for this run from the PR head, holding synthetic seed data only; it is "
            f"destroyed after the run. No credentials are handed to you and `$TOKEN` is not set. Create the identities you need yourself "
            f"with curl through the documented self-registration operation `POST {paths['register']}` (synthetic names, emails under "
            f"`@prqa-synthetic.dev`, passwords generated locally and never written to evidence), and use the returned bearer token as "
            f"`$TOKEN`. Before this run PR QA Pilot verified, from the sandbox network, that registration answered 201 and that the "
            f"returned token was accepted by `GET {paths['me']}` (401 without it). Create a second identity when a scenario needs another "
            f"user or role; if a role cannot be obtained through documented operations, mark that scenario BLOCKED. Redact every token "
            f"and password from evidence.\n\n---\n\n")


def add_identity_section(text: str, paths: dict[str, str]) -> str:
    i = text.find(CONTRACT_HEADING)
    if i < 0:
        raise Blocked("PROMPT_CONTRACT_MISSING", "the result contract section is missing from the generated prompt")
    return text[:i] + identity_section(paths) + text[i:]      # the contract file opens with its own `---` separator


def preview(host: Host, *, pinned: int | None = None, with_env: bool = True, out=print, simulation: bool = False,
            context_only: bool = False) -> int:
    """context_only: stop once the context is built (status `context_ready`); the prompt is generated outside (n8n → Groq
    via the model proxy) and gated by preview_finish(). Otherwise the deterministic reference prompt is gated here."""
    if simulation and pinned is None:
        raise Blocked("PR_REQUIRED", "a simulation needs an explicit PR number")
    disc = discover_simulation(host, pinned) if simulation else discover(host, pinned)
    if not disc.get("selected"):
        path = RUNS_DIR / f"{SLUG}-preview-blocked-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
        _write(path, disc)
        out(f"PREVIEW STOPPED: {disc.get('blocker')}")
        for t in disc["open_prs"]:
            out(f"  #{t['number']:<4} base={t['base']:<5} draft={t['draft']!s:<5} labels={t['labels']} → "
                f"{'ELIGIBLE' if not t['reasons'] else '; '.join(t['reasons'])}")
        out(f"discovery saved: {rel(path)}")
        return 3
    n = int(disc["selected"])
    pr = host.gh_get(f"/repos/{REPO}/pulls/{n}")
    if eligibility(pr, require_label=not simulation):
        raise Blocked("PR_CHANGED_DURING_PREVIEW", "; ".join(eligibility(pr, require_label=not simulation)))
    sha, base_ref, base_sha = pr["head"]["sha"], pr["base"]["ref"], pr["base"]["sha"]
    run_dir = RUNS_DIR / f"{SLUG}-pr-{n}-{sha}{'-sim' if simulation else ''}"
    old = json.loads((run_dir / "preview.json").read_text()) if (run_dir / "preview.json").is_file() else None
    if old and old.get("status") in ("previewed", "submitted") and old.get("env_created") and not old.get("cleaned_at"):
        raise Blocked("PREVIEW_EXISTS", f"{rel(run_dir)} has a live preview; run `cleanup --run-dir` first")
    run_id = f"{SLUG[:2]}{n}-{sha[:8]}-{secrets.token_hex(2)}"
    private = PRIVATE_ROOT / run_id
    state: dict[str, Any] = {"run_id": run_id, "repository": REPO, "pr_number": n, "head_sha": sha, "base_ref": base_ref, "base_sha": base_sha,
                             "pr_url": pr.get("html_url"), "pr_title": pr.get("title"), "test_level": LEVEL, "status": "preparing",
                             "created_at": now_iso(), "private_dir": str(private), "env_state_dir": str(run_dir / "env"), "env_created": False}
    if simulation:
        state["simulation"] = True
    _write(run_dir / "discovery.json", disc)
    _write(run_dir / "preview.json", state)
    files = host.gh_paginate(f"/repos/{REPO}/pulls/{n}/files")
    diff = host.gh_diff(n)
    workspace = private / "repo"
    got = host.fetch_commit(workspace, sha)
    if got != sha:
        raise Blocked("FETCH_SHA_MISMATCH", f"fetched {got[:12]} != {sha[:12]}")
    state["workspace_head"] = got
    openapi, contract_source = None, "none (environment not started)"
    if with_env:
        state["env_created"] = True
        _write(run_dir / "preview.json", state)
        rc, env_out = host.env_up(run_id=run_id, git_dir=workspace, sha=sha, state_dir=run_dir / "env", private_dir=private / "env")
        state["env_up"] = {k: env_out.get(k) for k in ("healthy", "base_url", "probe", "api_egress", "error")}
        if rc != 0 or not env_out.get("healthy"):
            drc, down = host.env_down(run_dir / "env")
            state.update(status="blocked", blocker="ENV_START_OR_HEALTH_FAILED", env_down={"rc": drc, **(down or {})})
            _write(run_dir / "preview.json", state)
            raise Blocked("ENV_START_OR_HEALTH_FAILED", str(env_out.get("error") or env_out.get("probe"))[:300])
        spec_path = run_dir / "env" / "openapi.json"
        if spec_path.is_file():
            openapi = json.loads(spec_path.read_text())
            contract_source = f"OpenAPI of the running head ({EXPECTED_BASE_URL}/api/v1/openapi.json via the sandbox network)"
        ap = auth_paths(openapi)
        identity: dict[str, Any] = {"mode": "self-registration", "paths": ap, "ready": False}
        if ap:
            arc, aout = host.env_auth_probe(run_dir / "env", ap["register"], ap["me"])
            identity.update(ready=arc == 0 and bool((aout or {}).get("ok")), probe=(aout or {}).get("probe"))
        else:
            identity["reason"] = "the OpenAPI document of this head documents no self-registration + current-user operation"
        state["test_identity"] = identity
    st, ctx = host.worker("POST", "/context/build", {"pr": pr, "files": files, "diff": diff, "openapi": openapi, "repository": REPO, "synthetic": False})
    if st != 200:
        raise Blocked("CONTEXT_BUILD_FAILED", f"HTTP {st}")
    context = ctx["context"]
    state.update(openapi_present=bool(openapi), contract_source=contract_source)
    if context_only:
        _write(run_dir / "context.json", context)
        state.update(status="context_ready", context_ready_at=now_iso())
        _write(run_dir / "preview.json", state)
        out(f"context ready: {rel(run_dir / 'context.json')} (prompt generation happens outside, then preview_finish gates it)")
        return 0
    return _finish_preview(host, run_dir=run_dir, state=state, pr=pr, context=context, generation=None, out=out)


def preview_finish(host: Host, *, run_dir: Path, generation: dict[str, Any] | None, out=print,
                   staged: dict[str, Any] | None = None) -> int:
    """Second half of a context_only preview: finalize the externally generated prompt (or the reference fallback when
    `generation` is None / failed), gate it, write the reviewed artifacts. The PR must still be eligible at the same SHA."""
    state = _load_state(run_dir)
    if state.get("status") != "context_ready":
        raise Blocked("CONTEXT_NOT_READY", f"status is {state.get('status')!r}")
    pr = host.gh_get(f"/repos/{REPO}/pulls/{state['pr_number']}")
    if pr["head"]["sha"] != state["head_sha"]:
        raise Blocked("HEAD_CHANGED", f"PR head is now {pr['head']['sha'][:12]}")
    reasons = eligibility(pr, require_label=not state.get("simulation"))
    if reasons:
        raise Blocked("PR_NO_LONGER_ELIGIBLE", "; ".join(reasons))
    context = json.loads((run_dir / "context.json").read_text(encoding="utf-8"))
    return _finish_preview(host, run_dir=run_dir, state=state, pr=pr, context=context, generation=generation, out=out, staged=staged)


GENERATION_FIELDS = ("generated_text", "finish_reason", "llm_error", "http_status", "response_shape")


def _finish_preview(host: Host, *, run_dir: Path, state: dict[str, Any], pr: dict[str, Any], context: dict[str, Any],
                    generation: dict[str, Any] | None, out=print, staged: dict[str, Any] | None = None) -> int:
    """staged: the staged generator's metadata (worker/prqa_worker/prompt_stages.py) when `generation` is its assembled
    prompt or its failure; the gate's verdict and rejection reasons are then also written to generation-metadata.json."""
    n, sha, base_ref, base_sha, run_id = state["pr_number"], state["head_sha"], state["base_ref"], state["base_sha"], state["run_id"]
    simulation = bool(state.get("simulation"))
    openapi, contract_source = state.get("openapi_present"), state.get("contract_source")
    gen = {k: (generation or {}).get(k) for k in GENERATION_FIELDS}
    st, fin = host.worker("POST", "/prompts/finalize", {**gen, "repository": REPO, "pr_number": n, "test_level": LEVEL,
                                                         "context": context, "synthetic": False})
    if st != 200:
        raise Blocked("PROMPT_FINALIZE_FAILED", f"HTTP {st}")
    prompt = fin["prompt"]
    allowed_sources = ("reference_fallback", "llm") if generation is not None else ("reference_fallback",)
    if prompt["source"] not in allowed_sources or sha256(prompt["text"]) != prompt["sha256"]:
        raise Blocked("PROMPT_PROVENANCE_INVALID", "unexpected source or hash")
    generation_record = {"model_called": generation is not None, **(fin.get("generation") or {}),
                         "worker_gate": {k: (fin.get("quality") or {}).get(k) for k in ("accepted", "score", "failure")} if fin.get("quality") else None}
    if (state.get("test_identity") or {}).get("ready"):
        prompt = dict(prompt, worker_sha256=prompt["sha256"], text=add_identity_section(prompt["text"], state["test_identity"]["paths"]))
        prompt["sha256"] = sha256(prompt["text"])
    quality = gate_all(prompt["text"], context)
    _write(run_dir / "qa-prompt.md", prompt["text"])
    _write(run_dir / "context.json", context)
    _write(run_dir / "quality.json", quality)
    meta = {"repository": REPO, "pr_number": n, "pr_url": pr.get("html_url"), "pr_title": pr.get("title"), "head_sha": sha,
            **({"simulation": True, "simulation_note": "manual trigger without the label; worker checkout from a local mirror (no GitHub token)"}
               if simulation else {}),
            "base_branch": base_ref, "base_sha": base_sha, "run_id": run_id, "generated_at": now_iso(),
            "prompt": {"file": "qa-prompt.md", "sha256": prompt["sha256"], "chars": len(prompt["text"]), "source": prompt["source"],
                       "template_version": prompt.get("template_version"), "reference_sha256": prompt.get("reference_sha256"),
                       "generation_path": (f"n8n → Groq via the model proxy in {len(staged.get('stage_order') or [])} validated stages "
                                          f"({' → '.join(staged.get('stage_order') or [])}; prompt credential, shared budget) → local "
                                          "deterministic assembly → worker POST /prompts/finalize (validator + 85-point rubric on the complete "
                                          "assembled prompt; a failed or rejected generation falls back to the reference prompt)"
                                          if staged and generation is not None else
                                          "n8n → Groq via the model proxy (prompt credential, shared budget) → worker POST /prompts/finalize "
                                          "(validator + 85-point rubric; a failed or rejected generation falls back to the reference prompt)"
                                          if generation is not None else
                                          "worker POST /prompts/finalize with no model output → deterministic reference fallback"),
                       "generation": generation_record,
                       **({"staged_generation": {"file": "generation-metadata.json", "mode": staged.get("mode"), "status": staged.get("status"),
                                                 "assembled_sha256": staged.get("assembled_sha256"), "failure": staged.get("failure")}}
                          if staged else {})},
            "quality_gate": quality, "contract_sources": [contract_source, "PR description, changed files and diff (GitHub, untrusted)"],
            "context_sha256": context.get("context_sha256"),
            "test_identity": state.get("test_identity"),
            "unresolved": ([] if openapi else ["no OpenAPI document: contract not verified"]) +
                          ([] if (state.get("test_identity") or {}).get("ready") else ["no verified synthetic test identity: execute will refuse"]) +
                          ([f"gate rejected the prompt ({quality['failure']}): execute will refuse it"] if not quality["accepted"] else []) +
                          ([] if context["api_spec"]["changed_operations"] else ["no changed API operation detected from the OpenAPI diff"])}
    _write(run_dir / "metadata.json", meta)
    if staged:
        gm_path = run_dir / "generation-metadata.json"
        gm = json.loads(gm_path.read_text(encoding="utf-8")) if gm_path.is_file() else {}
        gm["gate"] = {"candidate": "staged assembled prompt" if staged.get("status") == "assembled" else "none (staged generation failed)",
                      "worker_validation": {k: (fin.get("validation") or {}).get(k) for k in ("valid", "errors", "warnings")},
                      "worker_quality": {k: (fin.get("quality") or {}).get(k) for k in ("accepted", "score", "threshold", "failure", "hard_fails")}
                      if fin.get("quality") else None,
                      "final_prompt_source": prompt["source"], "final_prompt_sha256": prompt["sha256"],
                      "final_gate": {k: quality.get(k) for k in ("accepted", "score", "threshold", "failure", "hard_fails")},
                      "at": now_iso()}
        gm["result"] = ("accepted: the staged prompt is the run's prompt" if prompt["source"] == "llm" and quality["accepted"] else
                        "rejected: the staged prompt was not used (see gate); the existing fallback/rejection policy decided")
        _write(gm_path, gm)
    state.update(status="previewed", prompt_sha256=prompt["sha256"], prompt_source=prompt["source"], quality_accepted=quality["accepted"],
                 quality_score=quality["score"], previewed_at=now_iso(), prompt_generation=generation_record)
    _write(run_dir / "preview.json", state)
    out("=" * 100)
    out(prompt["text"])
    out("=" * 100)
    out(f"PR #{n} {pr.get('title')} | {pr.get('html_url')} | base {base_ref} | head {sha}")
    out(f"prompt: {prompt['source']} sha256 {prompt['sha256']} | gate: {'ACCEPTED' if quality['accepted'] else 'REJECTED'} "
        f"score {quality['score']} (threshold {quality['threshold']}) hard_fails={quality['hard_fails']}")
    out(f"saved: {rel(run_dir / 'qa-prompt.md')}  metadata: {rel(run_dir / 'metadata.json')}")
    out("No job was created, no grant consumed, no provider called. Review the prompt; execution is a separate `execute` invocation.")
    return 0 if quality["accepted"] else 4


def preview_only(host: Host, *, number: int, openapi_file: Path | None = None, openapi_head_sha: str | None = None,
                 out=print) -> int:
    """Prompt preview of an explicitly selected PR, without the trigger label. Never executable: no checkout, no
    environment, no job, no grant, no provider; the only worker calls are /context/build and /prompts/finalize."""
    pr = host.gh_get(f"/repos/{REPO}/pulls/{number}")
    reasons = eligibility(pr, require_label=False)
    if reasons:
        out(f"PREVIEW STOPPED: PR #{number} cannot be previewed: {'; '.join(reasons)}")
        return 3
    sha, base_ref, base_sha = pr["head"]["sha"], pr["base"]["ref"], pr["base"]["sha"]
    labels = [l.get("name") for l in pr.get("labels") or []]
    openapi, contract = None, {"source": "none", "verified_for_head": False}
    if openapi_file is not None:
        if openapi_head_sha != sha:
            raise Blocked("OPENAPI_HEAD_MISMATCH", f"--openapi-head-sha {str(openapi_head_sha)[:12]} != PR head {sha[:12]}")
        raw = openapi_file.read_text(encoding="utf-8")
        openapi = json.loads(raw)
        contract = {"source": f"OpenAPI document supplied by the operator for head {sha} ({openapi_file.name})", "sha256": sha256(raw),
                    "paths": len(openapi.get("paths") or {}), "verified_for_head": True,
                    "note": "generated at this commit by the operator; not fetched from a running API in this preview"}
    files = host.gh_paginate(f"/repos/{REPO}/pulls/{number}/files")
    diff = host.gh_diff(number)
    st, ctx = host.worker("POST", "/context/build", {"pr": pr, "files": files, "diff": diff, "openapi": openapi, "repository": REPO, "synthetic": False})
    if st != 200:
        raise Blocked("CONTEXT_BUILD_FAILED", f"HTTP {st}")
    context = ctx["context"]
    st, fin = host.worker("POST", "/prompts/finalize", {"generated_text": None, "repository": REPO, "pr_number": number, "test_level": LEVEL,
                                                         "context": context, "synthetic": False})
    if st != 200:
        raise Blocked("PROMPT_FINALIZE_FAILED", f"HTTP {st}")
    prompt = fin["prompt"]
    if prompt["source"] != "reference_fallback" or sha256(prompt["text"]) != prompt["sha256"]:
        raise Blocked("PROMPT_PROVENANCE_INVALID", "unexpected source or hash")
    again = host.gh_get(f"/repos/{REPO}/pulls/{number}")
    if eligibility(again, require_label=False) or again["head"]["sha"] != sha:
        raise Blocked("PR_CHANGED_DURING_PREVIEW", f"head {str(again['head'].get('sha'))[:12]}; "
                      f"{'; '.join(eligibility(again, require_label=False)) or 'head moved'} — nothing saved")
    quality = score_prompt(prompt["text"], context)
    run_dir = PREVIEWS_DIR / f"{SLUG}-pr-{number}-{sha}"
    unresolved = ([] if openapi else ["no OpenAPI document for this head: contract not verified"]) + \
                 ([f"gate rejected the prompt ({quality['failure']})"] if not quality["accepted"] else []) + \
                 ([] if context["api_spec"]["changed_operations"] else ["no changed API operation detected"]) + \
                 ([] if TRIGGER_LABEL in labels else [f"PR does not carry the label {TRIGGER_LABEL!r}: not eligible for execution"]) + \
                 ["description claims tagged [STATED] are intent only; the diff does not prove them implemented",
                  "API credentials for the target are not provided to the sandbox (BASE_URL only)"]
    meta = {"preview_only": True, "execution_eligible": False, "repository": REPO, "pr_number": number, "pr_url": pr.get("html_url"),
            "pr_title": pr.get("title"), "pr_state": pr.get("state"), "draft": bool(pr.get("draft")), "labels": labels,
            "head_sha": sha, "head_ref": pr["head"].get("ref"), "base_branch": base_ref, "base_sha": base_sha, "generated_at": now_iso(),
            "prompt": {"file": "qa-prompt.md", "sha256": prompt["sha256"], "chars": len(prompt["text"]), "source": prompt["source"],
                       "template_version": prompt.get("template_version"), "reference_sha256": prompt.get("reference_sha256"),
                       "generation_path": "worker POST /prompts/finalize with no model output → deterministic reference fallback "
                                          "(review-only: prompt LLM is mock; no provider call)"},
            "quality_gate": {k: quality[k] for k in ("accepted", "failure", "score", "threshold", "hard_fails", "gate_errors", "gate_warnings")},
            "contract": contract, "context_sha256": context.get("context_sha256"), "unresolved_assumptions": unresolved,
            "not_done": ["no checkout", "no disposable environment", "no job", "no grant", "no OpenCode", "no label change"]}
    _write(run_dir / "qa-prompt.md", prompt["text"])
    _write(run_dir / "context.json", context)
    _write(run_dir / "quality.json", quality)
    _write(run_dir / "metadata.json", meta)
    _write(run_dir / "preview.json", {"status": "preview_only", "preview_only": True, "execution_eligible": False, "repository": REPO,
                                      "pr_number": number, "head_sha": sha, "prompt_sha256": prompt["sha256"],
                                      "quality_accepted": quality["accepted"], "previewed_at": now_iso()})
    out("=" * 100)
    out(prompt["text"])
    out("=" * 100)
    out(f"PREVIEW ONLY — PR #{number} {pr.get('title')} | {pr.get('html_url')} | base {base_ref} | head {sha} | labels {labels}")
    out(f"prompt: {prompt['source']} sha256 {prompt['sha256']} | gate: {'ACCEPTED' if quality['accepted'] else 'REJECTED'} "
        f"score {quality['score']} (threshold {quality['threshold']}) hard_fails={quality['hard_fails']}")
    out(f"saved: {rel(run_dir / 'qa-prompt.md')}  metadata: {rel(run_dir / 'metadata.json')}")
    out("execution_eligible=false: no job, no grant, no provider call; this directory can never be executed.")
    return 0 if quality["accepted"] else 4


# ----------------------------------------------------------------------------------------------------- execute --
def _load_state(run_dir: Path) -> dict[str, Any]:
    p = run_dir / "preview.json"
    if not p.is_file():
        raise Blocked("NO_PREVIEW", f"{run_dir} has no preview.json")
    return json.loads(p.read_text())


def execute(host: Host, *, run_dir: Path, confirm_repo: str, confirm_pr: int, confirm_sha: str, grant_file: Path | None,
            approved: bool, wait_s: int = 3600, out=print) -> int:
    if not approved:
        raise Blocked("EXECUTE_FLAG_REQUIRED", "pass --execute to approve the reviewed prompt explicitly")
    state = _load_state(run_dir)

    def invalidate(code: str, detail: str) -> Blocked:
        state.update(status="invalidated", invalidated_at=now_iso(), invalidated_reason=f"{code}: {detail}")
        _write(run_dir / "preview.json", state)
        return Blocked(code, detail + " — preview invalidated: run cleanup, then preview again and review the new prompt")

    if state.get("preview_only") or state.get("execution_eligible") is False:
        raise Blocked("PREVIEW_ONLY_NOT_EXECUTABLE", "this is a preview-only run; a label-eligible `preview` is required for execution")
    if (confirm_repo, int(confirm_pr), confirm_sha) != (state["repository"], state["pr_number"], state["head_sha"]):
        raise Blocked("CONFIRMATION_MISMATCH", "confirmed repo/PR/SHA differ from the reviewed preview")
    if state.get("status") != "previewed":
        raise Blocked("PREVIEW_NOT_EXECUTABLE", f"status is {state.get('status')!r}")
    text = (run_dir / "qa-prompt.md").read_text(encoding="utf-8")
    if sha256(text) != state["prompt_sha256"]:
        raise Blocked("PROMPT_CHANGED_AFTER_REVIEW", "qa-prompt.md no longer matches the previewed hash")
    if not state.get("quality_accepted"):
        raise Blocked("QUALITY_GATE_REJECTED", "the previewed prompt did not pass the quality gate (threshold 85, hard fails)")
    context = json.loads((run_dir / "context.json").read_text(encoding="utf-8"))
    regate = gate_all(text, context)
    if not regate["accepted"]:
        raise Blocked("QUALITY_GATE_REJECTED", f"re-gating the prompt before submission failed ({regate['failure']})")
    if not (state.get("test_identity") or {}).get("ready"):
        raise Blocked("TEST_CREDENTIALS_UNAVAILABLE", "no verified synthetic test identity for the disposable API (self-registration probe did not pass)")
    pr = host.gh_get(f"/repos/{REPO}/pulls/{state['pr_number']}")
    if pr["head"]["sha"] != state["head_sha"]:
        raise invalidate("HEAD_CHANGED", f"PR head is now {pr['head']['sha'][:12]}")
    sim = bool(state.get("simulation"))
    reasons = eligibility(pr, require_label=not sim)
    if reasons:
        raise invalidate("PR_NO_LONGER_ELIGIBLE", "; ".join(reasons))
    st, health = host.worker("GET", "/healthz")
    if st != 200 or (health or {}).get("automation_mode") != "review_only":
        raise Blocked("WORKER_NOT_REVIEW_ONLY", f"HTTP {st}")
    st, cfg = host.worker("GET", "/config")
    cfg = cfg if isinstance(cfg, dict) else {}
    if not PRIVATE_REPO:
        if (cfg.get("workspaces") or {}).get("checkout_remote", "https://github.com") != "https://github.com":
            raise Blocked("CHECKOUT_REMOTE_NOT_GITHUB", f"public {REPO} is checked out from GitHub, but the worker uses "
                                                       f"{(cfg.get('workspaces') or {}).get('checkout_remote')!r}")
    elif sim:
        if not str((cfg.get("workspaces") or {}).get("checkout_remote") or "").startswith(SIM_MIRROR_PREFIX):
            raise Blocked("SIMULATION_MIRROR_NOT_CONFIGURED", f"the worker checks out from {(cfg.get('workspaces') or {}).get('checkout_remote')!r}, "
                                                              f"not a local mirror under {SIM_MIRROR_PREFIX}")
    elif not (cfg.get("workspaces") or {}).get("github_token_configured"):
        raise Blocked("GITHUB_READ_CREDENTIAL_MISSING", f"add the fine-grained read-only GITHUB_TOKEN for {REPO} to the secrets file and restart the worker")
    if not (cfg.get("launch") or {}).get("single_run_grants_enabled"):
        raise Blocked("GRANT_KEY_NOT_CONFIGURED", "run `python3 scripts/pipeline/prqa_authorize_run.py --init-key` and restart the worker (never done automatically)")
    entry = host.registry_entry() or {}
    if entry.get("base_url") != EXPECTED_BASE_URL:
        raise Blocked("REGISTRY_BASE_URL_NOT_DISPOSABLE", f"api.base_url is {entry.get('base_url')!r}; set it to {EXPECTED_BASE_URL} (entry stays disabled/pending)")
    rc, probe = host.env_probe(Path(state["env_state_dir"]))
    if rc != 0 or not (probe or {}).get("reachable"):
        raise Blocked("SANDBOX_CANNOT_REACH_API", json.dumps(probe)[:300])
    if grant_file is None or not Path(grant_file).is_file():
        raise Blocked("GRANT_MISSING", "mint one with scripts/pipeline/prqa_authorize_run.py --repo ... --pr ... --sha ... --verify-github")
    token = Path(grant_file).read_text(encoding="utf-8").strip()
    try:
        claims = run_grants.verify(token, host.grant_key(), now=int(host.clock()))
        request = {"synthetic": False, "repository": REPO, "test_level": LEVEL,
                   "pr": {"number": state["pr_number"], "head_sha": state["head_sha"]},
                   "prompt": {"text": text, "sha256": state["prompt_sha256"]}}
        run_grants.check_binding(claims, request)
    except run_grants.GrantError as exc:
        raise Blocked(exc.code, exc.reason)
    ledger_before = host.ledger()
    body = {"synthetic": False, "repository": REPO, "test_level": LEVEL,
            "pr": {"number": state["pr_number"], "head_sha": state["head_sha"], "base_sha": state["base_sha"], "title": str(state["pr_title"])[:300],
                   "base_ref": state["base_ref"], "head_ref": str(pr["head"].get("ref") or "")[:255], "is_fork": False, "url": state["pr_url"]},
            "trigger": {"source": "manual", "delivery_id": f"{'simulation' if sim else 'single-run'}:{state['run_id']}", "label": JOB_LABEL,
                        "requested_label": TRIGGER_LABEL, "requested_by": "operator-simulation" if sim else "operator"},
            "prompt": {"text": text, "source": state["prompt_source"], "sha256": state["prompt_sha256"]},
            "run_grant": token}
    st, resp = host.worker("POST", "/jobs", body)
    del token, body
    if st != 201:
        state.update(last_execute_refusal={"at": now_iso(), "http": st, "error": (resp or {}).get("error")})
        _write(run_dir / "preview.json", state)
        raise Blocked(str((resp or {}).get("error") or f"HTTP_{st}"), str((resp or {}).get("detail") or "")[:300])
    job_id = resp["job"]["id"]
    state.update(status="submitted", job_id=job_id, grant_id=resp.get("grant_id"), submitted_at=now_iso())
    _write(run_dir / "preview.json", state)
    out(f"job {job_id} created (grant {resp.get('grant_id')} consumed); waiting for the launcher/OpenCode result…")
    deadline = host.clock() + wait_s
    job: dict[str, Any] = resp["job"]
    while host.clock() < deadline:
        st, job = host.worker("GET", f"/jobs/{job_id}")
        job = (job or {}).get("job", job) if isinstance(job, dict) else {}
        if job.get("status") in TERMINAL:
            break
        host.sleep(15)
    _write(run_dir / "job.json", job)
    if job.get("result"):
        _write(run_dir / "result.json", job["result"])
    ledger_after = host.ledger()
    execution = {"job_id": job_id, "status": job.get("status"), "launch_state": job.get("launch_state"), "ledger_before": ledger_before,
                 "ledger_after": ledger_after, "finished_at": now_iso(), "opencode_executed": bool(job.get("result")) and job.get("status") == "succeeded"}
    _write(run_dir / "execution.json", execution)
    state.update(status="executed" if job.get("status") in TERMINAL else "submitted", job_status=job.get("status"))
    _write(run_dir / "preview.json", state)
    out(json.dumps(execution, indent=1))
    return 0 if job.get("status") == "succeeded" else 5


# ----------------------------------------------------------------------------------------------------- cleanup --
def cleanup(host: Host, *, run_dir: Path, out=print) -> int:
    state = _load_state(run_dir)
    result: dict[str, Any] = {"at": now_iso()}
    ok = True
    if state.get("env_created") and not state.get("env_torn_down_at"):
        rc, down = host.env_down(Path(state["env_state_dir"]))
        result["env_down"] = {"rc": rc, **(down or {})}
        if rc == 0:
            state["env_torn_down_at"] = now_iso()
        else:
            ok = False
    private = Path(state.get("private_dir") or "")
    if state.get("private_dir") and private.resolve().is_relative_to(PRIVATE_ROOT.resolve()) and private.is_dir():
        shutil.rmtree(private)
        result["private_workspace_removed"] = str(private)
    state.setdefault("cleanups", []).append(result)
    if ok:
        state["cleaned_at"] = now_iso()
        if state.get("status") in ("previewed", "preparing", "blocked", "invalidated"):
            state["status"] = "cleaned"
    _write(run_dir / "preview.json", state)
    out(json.dumps(result, indent=1))
    if not ok:
        out("CLEANUP INCOMPLETE: environment teardown failed; state kept so cleanup can be retried.")
    return 0 if ok else 6


# -------------------------------------------------------------------------------------------------------- main --
def main(argv: list[str] | None = None, host: Host | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=os.environ.get("PRQA_TARGET_REPO") or DEFAULT_REPO, choices=sorted(PROJECTS))
    sub = ap.add_subparsers(dest="stage", required=True)
    sub.add_parser("preflight")
    p = sub.add_parser("preview")
    p.add_argument("--pr", type=int)
    p.add_argument("--no-env", action="store_true", help="skip the disposable API (prompt then has no verified contract)")
    p.add_argument("--preview-only", action="store_true", help="explicit PR (--pr) without the trigger label; never executable")
    p.add_argument("--openapi-file", help="preview-only: OpenAPI JSON generated at the PR head")
    p.add_argument("--openapi-head-sha", help="preview-only: the head SHA the --openapi-file was generated at")
    e = sub.add_parser("execute")
    e.add_argument("--run-dir", required=True)
    e.add_argument("--execute", action="store_true")
    e.add_argument("--confirm-repo", required=True)
    e.add_argument("--confirm-pr", type=int, required=True)
    e.add_argument("--confirm-sha", required=True)
    e.add_argument("--grant-file")
    e.add_argument("--wait-s", type=int, default=3600)
    c = sub.add_parser("cleanup")
    c.add_argument("--run-dir", required=True)
    args = ap.parse_args(argv)
    select_project(args.repo)
    host = host or Host()
    try:
        if args.stage == "preflight":
            res = preflight(host)
            print(json.dumps(res, indent=1))
            return 0 if not res["blocks_preview"] else 2
        if args.stage == "preview" and args.preview_only:
            if args.pr is None:
                raise Blocked("PR_REQUIRED", "--preview-only needs an explicit --pr N")
            return preview_only(host, number=args.pr, openapi_file=Path(args.openapi_file) if args.openapi_file else None,
                                openapi_head_sha=args.openapi_head_sha)
        if args.stage == "preview":
            return preview(host, pinned=args.pr, with_env=not args.no_env)
        if args.stage == "execute":
            return execute(host, run_dir=Path(args.run_dir), confirm_repo=args.confirm_repo, confirm_pr=args.confirm_pr,
                           confirm_sha=args.confirm_sha, grant_file=Path(args.grant_file) if args.grant_file else None,
                           approved=args.execute, wait_s=args.wait_s)
        return cleanup(host, run_dir=Path(args.run_dir))
    except Blocked as b:
        print(json.dumps({"blocked": b.code, "detail": b.detail}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
