#!/usr/bin/env python3
"""Run a gated PRQA prompt with an agent CLI installed on THIS machine (operator choice: executor `local`).

Engine (PRQA_LOCAL_ENGINE in compose/.env, or --engine): `opencode` (default) or `claude` (Claude Code, headless `claude -p`
with the Bash tool only; model PRQA_LOCAL_MODEL, default claude-sonnet-5-5). For `claude`, the sandbox gets a private home
with only the operator's Claude login file bound in (~/.claude/.credentials.json, read-write so a token refresh is kept) and
a minimal ~/.claude.json; the operator's settings, hooks, plugins, MCP servers, projects and history stay out of reach.

Why: the free `opencode/glm-5.3-flash-free` model answers the genuine OpenCode client; the sandbox path (custom `prqa`
provider through the model proxy) gets HTTP 429 without an OpenCode Zen key. This runs the real local client instead.

What it keeps from the sandbox path (worker/sandbox/entrypoint.sh):
  * the same chunked execution and records (worker/sandbox/chunked_run.py + record.py), the same slim bash-only agent;
  * the prompt is re-verified (sha256 of the reviewed qa-prompt.md) and re-gated (85 + hard fails) right before running,
    and the PR must still be eligible at the reviewed head SHA;
  * the result is validated against schemas/qa-result.schema.json (+ count reconciliation) like the worker does.
Identity (operator decision, 2026-10-28): the free tier is tied to the operator's own OpenCode account, so the operator's
OpenCode data/config/cache/state directories are bound in; their snapshot/, repos/ and tool-output/ (copies of other
projects) are hidden. Everything else stays out of reach (bubblewrap, no root needed): no other part of /home, no /media
or /root (no ~/.config/prqa secrets, no ssh keys, not this project), no inherited environment, the repository read-only,
only the evidence directory writable, own PID/IPC/UTS namespaces; the network is shared (opencode.ai + the API relay on
127.0.0.1). A terminal window (scripts/pipeline/prqa_local_watch.py) follows the run when a desktop session is available.
The disposable API keeps no egress: a per-run relay container (label prqa.disposable=<run id>, removed afterwards)
publishes it on 127.0.0.1 only.

    python3 scripts/pipeline/prqa_local_opencode.py --run-dir runs/prqa-local-pr-runs/<run>      # writes <run>/local/status.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("prqa_run_single_pr", ROOT / "scripts" / "pipeline" / "prqa_run_single_pr.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)  # type: ignore[union-attr]
from prqa_worker.validation import SchemaStore, validate_result  # noqa: E402  (worker/ is on sys.path via R)

ENGINES = ("opencode", "claude")
DEFAULT_MODELS = {"opencode": "opencode/glm-5.3-flash-free", "claude": "claude-sonnet-5-5"}
ENGINE_NAMES = {"opencode": "OpenCode", "claude": "Claude Code"}
CLAUDE_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
CLAUDE_ACCOUNT_KEYS = ("userID", "oauthAccount", "hasCompletedOnboarding", "lastOnboardingVersion", "firstStartTime")
CLAUDE_ENV = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_ERROR_REPORTING": "1"}
SANDBOX = ROOT / "worker" / "sandbox"
RELAY_IMAGE = "prqa/worker:0.1.0"
API_PORT = 8000
USER_OPENCODE_DIRS = (".local/share/opencode", ".config/opencode", ".cache/opencode", ".local/state/opencode")
HIDDEN_OPENCODE_SUBDIRS = (".local/share/opencode/snapshot", ".local/share/opencode/repos", ".local/share/opencode/tool-output")
OPENCODE_ENV = {"OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1", "OPENCODE_DISABLE_CLAUDE_CODE": "1", "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1"}


class LocalError(Exception):
    pass


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    if check and p.returncode != 0:
        raise LocalError(f"docker {args[0]} failed ({p.returncode})")
    return p


def opencode_binary() -> Path:
    found = shutil.which("opencode") or str(Path.home() / ".opencode" / "bin" / "opencode")
    path = Path(found).resolve()
    if not path.is_file():
        raise LocalError("the OpenCode CLI is not installed on this machine")
    return path


def claude_binary() -> Path:
    found = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    path = Path(found).resolve()
    if not path.is_file():
        raise LocalError("the Claude Code CLI is not installed on this machine")
    return path


def prepare_claude_home(home: Path) -> None:
    """A private home for `claude`: only the account fields of ~/.claude.json (no project history) and a mount point for
    the login file, which bwrap_argv binds in from the operator's home."""
    if not CLAUDE_CREDENTIALS.is_file():
        raise LocalError("the Claude Code CLI is not logged in on this machine (run `claude` and /login)")
    try:
        cfg = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    (home / ".claude").mkdir(mode=0o700, exist_ok=True)
    (home / ".claude" / ".credentials.json").touch(mode=0o600)
    write_json(home / ".claude.json", {k: cfg[k] for k in CLAUDE_ACCOUNT_KEYS if k in cfg})


def resolve_engine(engine: str | None, model: str | None) -> tuple[str, str]:
    env = R.env_file_values(ROOT / "compose" / ".env")
    engine = engine or os.environ.get("PRQA_LOCAL_ENGINE") or env.get("PRQA_LOCAL_ENGINE") or "opencode"
    if engine not in ENGINES:
        raise LocalError(f"unknown PRQA_LOCAL_ENGINE {engine!r} (expected one of {', '.join(ENGINES)})")
    model = (model or os.environ.get("PRQA_LOCAL_MODEL") or env.get("PRQA_LOCAL_MODEL")
             or (os.environ.get("PRQA_LOCAL_OPENCODE_MODEL") if engine == "opencode" else None) or DEFAULT_MODELS[engine])
    return engine, model


def opencode_config(model: str) -> str:
    """The `prqa` agent on OpenCode's built-in provider. Unlike the sandbox agent, OpenCode's standard tools stay enabled:
    the free tier refuses requests whose tool set was trimmed (measured 2026-10-28), and GLM's context does not need the
    slim agent the 8k-TPM Groq path did. bubblewrap bounds the tools: repository read-only, evidence the only writable dir."""
    return json.dumps({"enabled_providers": [model.split("/", 1)[0]], "autoupdate": False, "share": "disabled", "model": model,
                       "permission": {"external_directory": {"*": "deny", "/work/evidence/*": "allow"}},
                       "agent": {"prqa": {"mode": "primary", "description": "PRQA API tester",
                                          "prompt": "You are a senior QA engineer testing an HTTP API with curl through the bash tool. "
                                                    "Follow the user's instructions exactly, keep command output short, and never "
                                                    "modify the repository."}}})


def engine_binds(engine: str, binary: Path) -> list[str]:
    if engine == "claude":
        return ["--bind", str(CLAUDE_CREDENTIALS), "/home/prqa/.claude/.credentials.json",
                "--ro-bind", str(binary), "/opt/claude/bin/claude"]
    return [*[a for d in USER_OPENCODE_DIRS if (Path.home() / d).is_dir() for a in ("--bind", str(Path.home() / d), f"/home/prqa/{d}")],
            *[a for d in HIDDEN_OPENCODE_SUBDIRS if (Path.home() / d).is_dir() for a in ("--tmpfs", f"/home/prqa/{d}")],
            "--ro-bind", str(binary), "/opt/opencode/bin/opencode"]


def copy_evidence(src: Path, dst: Path) -> list[str]:
    """Copy the evidence tree file by file. A file the destination filesystem refuses (e.g. `nul` or `a:b` on NTFS) is
    skipped and reported instead of failing a run whose result is already valid."""
    skipped = []
    for f in sorted(src.rglob("*")):
        rel = f.relative_to(src)
        try:
            if f.is_dir():
                (dst / rel).mkdir(parents=True, exist_ok=True)
            elif f.is_file():
                (dst / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst / rel)
        except OSError:
            skipped.append(str(rel))
    return skipped


def bwrap_argv(*, home: Path, repo: Path, evidence: Path, prompt: Path, binary: Path, env: dict[str, str], inner: list[str],
               engine: str = "opencode") -> list[str]:
    argv = ["bwrap", "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/sbin", "/sbin", "--ro-bind", "/etc", "/etc",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/home",
            "--bind", str(home), "/home/prqa", *engine_binds(engine, binary),
            "--ro-bind", str(SANDBOX / "chunked_run.py"), "/opt/prqa/chunked_run.py",
            "--ro-bind", str(SANDBOX / "record.py"), "/opt/prqa/bin/prqa-record",
            "--ro-bind", str(repo), "/work/repo", "--bind", str(evidence), "/work/evidence", "--ro-bind", str(prompt), "/work/prompt.md",
            "--chdir", "/work/repo", "--unshare-all", "--share-net", "--die-with-parent", "--new-session", "--clearenv"]
    for k, v in env.items():
        argv += ["--setenv", k, v]
    return argv + inner


# ------------------------------------------------------------------------------------------------- the run --
def verify(run_dir: Path, host) -> tuple[dict[str, Any], str]:
    pv = json.loads((run_dir / "preview.json").read_text())
    if pv.get("status") != "previewed" or not pv.get("quality_accepted"):
        raise LocalError(f"preview is not an accepted, unexecuted preview (status {pv.get('status')})")
    text = (run_dir / "qa-prompt.md").read_text(encoding="utf-8")
    if R.sha256(text) != pv["prompt_sha256"]:
        raise LocalError("PROMPT_CHANGED_AFTER_REVIEW: qa-prompt.md no longer matches the gated hash")
    regate = R.gate_all(text, json.loads((run_dir / "context.json").read_text(encoding="utf-8")))
    if not regate["accepted"]:
        raise LocalError(f"QUALITY_GATE_REJECTED on re-gate ({regate['failure']})")
    if not (pv.get("test_identity") or {}).get("ready"):
        raise LocalError("TEST_CREDENTIALS_UNAVAILABLE: no verified synthetic test identity for the disposable API")
    pr = host.gh_get(f"/repos/{R.REPO}/pulls/{pv['pr_number']}")
    if pr["head"]["sha"] != pv["head_sha"]:
        raise LocalError(f"HEAD_CHANGED: PR head is now {pr['head']['sha'][:12]}")
    reasons = R.eligibility(pr, require_label=not pv.get("simulation"))
    if reasons:
        raise LocalError("PR_NO_LONGER_ELIGIBLE: " + "; ".join(reasons))
    return pv, text


def start_relay(run_dir: Path) -> tuple[str, int]:
    st = json.loads((run_dir / "env" / "env-state.json").read_text())
    rid, net = st["run_id"], st["sandbox_network"]
    api = next(r["name"] for r in st["resources"] if r["kind"] == "container" and r["name"] == f"prqa-dx-{rid}-api")
    # the relay starts on the default bridge (needed to publish on 127.0.0.1), where the alias does not resolve: target
    # the API's address on the sandbox network instead
    ip = docker("inspect", "--format", "{{(index .NetworkSettings.Networks \"%s\").IPAddress}}" % net, api).stdout.strip()
    if not ip:
        raise LocalError("the disposable API has no address on the sandbox network")
    name = f"prqa-dx-{rid}-hostrelay"
    docker("rm", "-f", name, check=False)
    docker("run", "-d", "--name", name, "--label", f"prqa.disposable={rid}", "--network", "bridge", "-p", "127.0.0.1::8790",
           "--user", "65534:65534", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
           "-e", f"PRQA_RELAY_TARGET={ip}:{API_PORT}", "-v", f"{ROOT / 'scripts' / 'services' / 'prqa_bridge_relay.py'}:/relay/relay.py:ro",
           "--entrypoint", "python3", RELAY_IMAGE, "-u", "/relay/relay.py")
    docker("network", "connect", net, name)
    port = docker("port", name, "8790/tcp").stdout.strip().splitlines()[0].rsplit(":", 1)[1]
    return name, int(port)


def open_viewer(run_dir: Path, pr: int, engine: str = "opencode") -> None:
    """Best effort: a terminal window that follows the run (read-only; closing it does not stop anything)."""
    if os.environ.get("PRQA_LOCAL_VIEWER", "1") == "0" or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    cmd = [sys.executable, "-u", str(ROOT / "scripts" / "pipeline" / "prqa_local_watch.py"), "--run-dir", str(run_dir)]
    if shutil.which("gnome-terminal"):
        argv = ["gnome-terminal", "--title", f"PRQA · {R.REPO} PR #{pr} · {ENGINE_NAMES[engine]}", "--", *cmd]
    elif shutil.which("xterm"):
        argv = ["xterm", "-T", f"PRQA PR #{pr}", "-e", *cmd]
    else:
        return
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def wait_api(base: str, seconds: int = 90) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/v1/openapi.json", timeout=10) as r:
                if r.status == 200:
                    return
        except OSError:
            pass
        time.sleep(3)
    raise LocalError("the disposable API is not reachable through the host relay")


def run(run_dir: Path, *, model: str, timeout_min: int, host=None, engine: str = "opencode") -> dict[str, Any]:
    host = host or R.Host()
    status_file = run_dir / "local" / "status.json"
    executor = f"local-{engine}"
    status: dict[str, Any] = {"state": "starting", "pid": os.getpid(), "started_at": R.now_iso(), "model": model, "engine": engine}
    write_json(status_file, status)
    relay = None
    job_id = ""
    try:
        pv, text = verify(run_dir, host)
        job_id = f"local-{pv['run_id']}"
        private = Path(pv["private_dir"])
        repo = private / "repo"
        if not repo.is_dir():
            raise LocalError("the reviewed checkout is missing from the private workspace")
        work = private / "local"
        shutil.rmtree(work, ignore_errors=True)
        home, evidence = work / "home", work / "evidence"
        for d in (home, evidence):
            d.mkdir(parents=True, mode=0o700)
        prompt = work / "prompt.md"
        prompt.write_text(text, encoding="utf-8")
        if engine == "claude":
            prepare_claude_home(home)
        binary = claude_binary() if engine == "claude" else opencode_binary()
        relay, port = start_relay(run_dir)
        base = f"http://127.0.0.1:{port}"
        wait_api(base)
        job = {"job_id": job_id, "repository": R.REPO, "pr": {"number": pv["pr_number"], "title": str(pv.get("pr_title") or "")[:300],
                                                             "head_sha": pv["head_sha"]}}
        env = {"HOME": "/home/prqa", "PATH": f"/opt/prqa/bin:/opt/{'claude' if engine == 'claude' else 'opencode'}/bin:/usr/bin:/bin",
               "LANG": "C.UTF-8", "TMPDIR": "/tmp", "PRQA_EVIDENCE_DIR": "/work/evidence", "PRQA_JOB_ID": job_id,
               "PRQA_JOB_JSON": json.dumps(job), "BASE_URL": base,
               **(CLAUDE_ENV if engine == "claude" else {"OPENCODE_CONFIG_CONTENT": opencode_config(model), **OPENCODE_ENV})}
        inner = ["python3", "-I", "/opt/prqa/chunked_run.py", "--prompt", "/work/prompt.md", "--repo-dir", "/work/repo",
                 "--evidence", "/work/evidence", "--model", model, "--timeout-s", str(timeout_min * 60), "--prompt-sha256", pv["prompt_sha256"],
                 "--engine", engine]
        status.update(state="running", job_id=job_id, base_url="http://127.0.0.1:<relay>", evidence=str(evidence), pr=pv["pr_number"],
                      sha=pv["head_sha"])
        write_json(status_file, status)
        open_viewer(run_dir, pv["pr_number"], engine)
        log(f"{ENGINE_NAMES[engine]} ({model}) running locally under bubblewrap for {R.REPO} PR #{pv['pr_number']} @ {pv['head_sha'][:12]}")
        with open(evidence / "runner-stdout.log", "w") as out, open(evidence / "runner-stderr.log", "w") as err:
            try:
                subprocess.run(bwrap_argv(home=home, repo=repo, evidence=evidence, prompt=prompt, binary=binary, env=env,
                                          inner=inner, engine=engine), stdout=out, stderr=err, timeout=timeout_min * 60 + 120, check=False)
            except subprocess.TimeoutExpired:
                log(f"local {ENGINE_NAMES[engine]} run hit the hard timeout")
        result = json.loads((evidence / "qa-result.json").read_text()) if (evidence / "qa-result.json").is_file() else None
        check = validate_result(result, SchemaStore(ROOT / "schemas")) if result is not None else {"valid": False, "errors": ["no qa-result.json"]}
        if result is not None and check["valid"]:
            result["executed_by"] = dict(result.get("executed_by") or {}, prompt_source=pv.get("prompt_source") or "reference_fallback")
            job_status, error = "succeeded", None
        else:
            job_status, error = "failed", {"message": f"local {ENGINE_NAMES[engine]} result invalid: " + "; ".join(map(str, check["errors"]))[:280]}
            result = None
        skipped = copy_evidence(evidence, run_dir / "local" / "evidence")                  # already scrubbed by chunked_run
        if skipped:
            log(f"evidence files not copied (name not allowed on this drive): {', '.join(skipped)[:300]}")
        finished = R.now_iso()
        jobdoc = {"id": job_id, "status": job_status, "launch_state": "local", "executor": executor, "model": model,
                  "started_at": status["started_at"], "finished_at": finished, "result": result, **({"error": error} if error else {})}
        R._write(run_dir / "job.json", jobdoc)
        if result:
            R._write(run_dir / "result.json", result)
        R._write(run_dir / "execution.json", {"job_id": job_id, "status": job_status, "launch_state": "local", "executor": executor,
                                              "model": model, "ledger_before": None, "ledger_after": None, "finished_at": finished,
                                              "engine": engine, "opencode_executed": bool(result) and bool(result.get("test_cases"))})
        pv = json.loads((run_dir / "preview.json").read_text())
        pv.update(status="executed", job_id=job_id, job_status=job_status, executor=executor)
        R._write(run_dir / "preview.json", pv)
        status.update(state="finished", job_status=job_status, finished_at=finished)
    except (LocalError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        status.update(state="failed", error=f"{type(exc).__name__}: {exc}"[:300], finished_at=R.now_iso())
        log(f"local run failed: {status['error']}")
    finally:
        if relay:
            docker("rm", "-f", relay, check=False)
        write_json(status_file, status)
    return status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--repo", default=os.environ.get("PRQA_TARGET_REPO") or R.DEFAULT_REPO, choices=sorted(R.PROJECTS))
    ap.add_argument("--engine", choices=ENGINES, default=None, help="default: PRQA_LOCAL_ENGINE (compose/.env), else opencode")
    ap.add_argument("--model", default=None, help="default: PRQA_LOCAL_MODEL, else the engine's default model")
    ap.add_argument("--timeout-min", type=int, default=85)
    a = ap.parse_args(argv)
    R.select_project(a.repo)
    try:
        engine, model = resolve_engine(a.engine, a.model)
    except LocalError as exc:
        st = {"state": "failed", "pid": os.getpid(), "error": str(exc), "finished_at": R.now_iso()}
        write_json(a.run_dir.resolve() / "local" / "status.json", st)
        print(json.dumps(st))
        return 1
    st = run(a.run_dir.resolve(), model=model, timeout_min=a.timeout_min, engine=engine)
    print(json.dumps(st))
    return 0 if st["state"] == "finished" else 1


if __name__ == "__main__":
    sys.exit(main())
