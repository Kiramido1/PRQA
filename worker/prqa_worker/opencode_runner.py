"""Isolated OpenCode execution (LIVE MODE ONLY — never reached while review-only).

Trust boundary (docs/SANDBOX_TRUST_BOUNDARY.md): the worker performs every credentialed operation (the pinned
checkout with GITHUB_TOKEN, workspace.py) OUTSIDE the sandbox and hands the sandbox a per-job **checkout artifact**
(tar of the working tree without .git, bound to job/repository/SHA by a manifest). The sandbox receives:

  * the artifact, extracted by a short-lived helper container (network none, read-only rootfs, uid 10001, safe
    extractor shipped in the image) into a per-job named volume that the sandbox mounts READ-ONLY at /checkout —
    no host path, no other job's workspace; the sandbox rootfs stays --read-only
  * ALLOWED_ENV only (job id, test level, job JSON, evidence dir, BASE_URL of the target API)
  * TOKEN (the staging target's test credential) through ``--env-file /dev/stdin`` only when the registry entry
    opts in; never GITHUB_TOKEN, never a model API key, never a PRQA service secret
  * network ``none`` unless PRQA_SANDBOX_NETWORK names a dedicated network that reaches only the test target
    and the PRQA model proxy, which injects the provider key; the sandbox holds only a per-job PRQA_MODEL_TOKEN
    (env-file) and PRQA_MODEL_BASE_URL (docs/SANDBOX_TRUST_BOUNDARY.md §5d)

Hooks and a read-only filesystem do NOT prevent OpenCode from executing repository-controlled instructions inside
the sandbox; they only bound what such execution can reach. This module builds the plan deterministically so it can
be unit-tested without Docker. The worker container itself has no Docker CLI or socket: executing the plan is a
Phase 2 operator decision (fail closed here).
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Callable, Mapping

from .policy import Policy
from .redaction import redact_text

ALLOWED_ENV = ("BASE_URL", "PRQA_JOB_ID", "PRQA_JOB_JSON", "PRQA_EVIDENCE_DIR", "PRQA_TEST_LEVEL", "PRQA_MODEL_BASE_URL")
# Fixed hardening env (names verified as strings in the OpenCode 1.19.3 binary): never honour a repository opencode.json.
FIXED_SANDBOX_ENV = {"OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_PURE": "1"}
HELPER_INPUT = "/work/checkout.tar"        # docker cp destination inside the helper (the image has /work)
SECRET_ENV_FILE_NAMES = ("TOKEN", "PRQA_MODEL_TOKEN")
SECRET_ENV_NAMES = ("GITHUB_TOKEN", "OPENROUTER_API_KEY", "GROQ_API_KEY", "PROMPT_LLM_API_KEY", "PRQA_WORKER_TOKEN", "PRQA_WEBHOOK_SECRET",
                    "GITHUB_WEBHOOK_SECRET", "DISCORD_WEBHOOK_URL", "N8N_ENCRYPTION_KEY", "PRQA_LAUNCHER_TOKEN", "PRQA_MODEL_PROXY_ADMIN_TOKEN",
                    "PRQA_MODEL_TOKEN", "TOKEN")
SANDBOX_CHECKOUT_ROOT = "/checkout"
SANDBOX_CHECKOUT_DIR = "/checkout/repo"
EVIDENCE_DIR = "/work/evidence"
DEFAULT_NETWORK = "none"
JOB_ID_SAFE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$")


class PolicyViolation(RuntimeError):
    pass


class RunnerUnavailable(RuntimeError):
    pass


class SandboxCancelled(RuntimeError):
    """Cancellation reached the launcher: the sandbox was terminated and no result is returned."""


HEX64 = __import__("re").compile(r"^[0-9a-f]{64}$")


VOLUME_SAFE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,120}$")   # Docker volume/container name subset
HELPER_ENTRYPOINT = "/usr/local/bin/prqa-populate-checkout"


@dataclass(frozen=True)
class SandboxPlan:
    """Per-job transport compatible with a read-only sandbox AND a body-validating Docker socket proxy (no hijacked
    attach streams): volume_create → helper_create → helper_copy (artifact via the archive API into /work) →
    helper_start → helper_wait → helper_logs → create (sandbox, volume mounted READ-ONLY) → start → poll inspect →
    logs → cleanup (sandbox, helper, volume; names and labels bound to the job id)."""
    container: str
    helper: str
    volume: str
    volume_create: list[str]
    helper_create: list[str]
    helper_copy: list[str]
    helper_start: list[str]
    helper_wait: list[str]
    helper_logs: list[str]
    create: list[str]
    start: list[str]
    inspect_state: list[str]
    logs: list[str]
    cleanup: list[list[str]]
    artifact_path: str
    env_file_names: tuple[str, ...]
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def argv_steps(self) -> list[list[str]]:
        return [self.volume_create, self.helper_create, self.helper_copy, self.helper_start, self.helper_wait, self.create, self.start]

    @property
    def populate(self) -> list[str]:          # compatibility alias for tests/docs: the helper create step
        return self.helper_create

    @property
    def remove(self) -> list[str]:
        return self.cleanup[0]


def _sandbox_env(job: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, str]:
    pr = job["request"]["pr"]
    safe_env = {k: env[k] for k in ALLOWED_ENV if k in env}
    safe_env["PRQA_JOB_ID"] = job["id"]
    safe_env["PRQA_EVIDENCE_DIR"] = EVIDENCE_DIR
    safe_env["PRQA_TEST_LEVEL"] = job["request"]["test_level"]
    safe_env["PRQA_JOB_JSON"] = json.dumps({
        "job_id": job["id"], "repository": job["request"]["repository"],
        "pr": {"number": pr["number"], "title": pr.get("title", ""), "head_sha": pr["head_sha"]},
    })
    for name in SECRET_ENV_NAMES:
        safe_env.pop(name, None)
    safe_env.update(FIXED_SANDBOX_ENV)
    return safe_env


def _names(job_id: str) -> tuple[str, str, str]:
    if not VOLUME_SAFE.match(str(job_id)):
        raise PolicyViolation("job id is not safe for container/volume names")
    return f"prqa-sandbox-{job_id}", f"prqa-populate-{job_id}", f"prqa-checkout-{job_id}"


HARDENING = ["--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--read-only", "--user", "10001:10001"]


def build_docker_argv(*, image: str, network: str, job: Mapping[str, Any], model: str, runtime_minutes: int,
                      env: Mapping[str, str], tree_sha256: str, prompt_sha256: str, pass_target_token: bool = False,
                      docker_cli: str = "docker", env_file_names: tuple[str, ...] = ()) -> list[str]:
    """The `docker create` step. Pure; no secret value and no host path ever appears in argv. The image declares the
    ENTRYPOINT, so the tokens after the image are exactly the entrypoint's arguments (no command name). The expected
    hashes are the WORKER-held values from packing time: the sandbox must match them, not only its own manifest."""
    sandbox, _, volume = _names(job["id"])
    if not HEX64.match(str(tree_sha256)) or not HEX64.match(str(prompt_sha256)):
        raise PolicyViolation("expected tree/prompt hashes must be 64 lowercase hex characters")
    pr = job["request"]["pr"]
    argv = [
        docker_cli, "create",
        "--name", sandbox,
        "--label", f"prqa.job={job['id']}", "--label", "prqa.kind=sandbox",
        "--network", network or DEFAULT_NETWORK,
        *HARDENING,
        "--tmpfs", "/tmp:size=256m,mode=1777",
        "--tmpfs", "/work:size=1g,mode=1777,uid=10001,gid=10001",
        "--tmpfs", "/home/sandbox:size=64m,uid=10001,gid=10001",
        "--mount", f"type=volume,src={volume},dst={SANDBOX_CHECKOUT_ROOT},readonly",
        "--memory", "2g", "--cpus", "2", "--pids-limit", "256",
        "--stop-timeout", "30",
    ]
    if pass_target_token or env_file_names:
        argv += ["--env-file", "/dev/stdin"]      # per-job secrets only (TOKEN opt-in, PRQA_MODEL_TOKEN), streamed at create time; never argv
    for key, value in sorted(_sandbox_env(job, env).items()):
        argv += ["-e", f"{key}={value}"]
    argv += [image, "--repo", job["request"]["repository"], "--sha", pr["head_sha"], "--model", model,
             "--timeout-minutes", str(runtime_minutes), "--tree-sha256", str(tree_sha256), "--prompt-sha256", str(prompt_sha256),
             "--prechecked"]
    return argv


def build_sandbox_plan(*, image: str, network: str, job: Mapping[str, Any], model: str, runtime_minutes: int,
                       env: Mapping[str, str], artifact_path: str, tree_sha256: str, prompt_sha256: str,
                       pass_target_token: bool = False, docker_cli: str = "docker", helper_image: str | None = None,
                       model_token: bool = False) -> SandboxPlan:
    ap = Path(artifact_path)
    if not ap.is_absolute() or ap.name != "checkout.tar" or any(part == ".." for part in ap.parts):
        raise PolicyViolation("artifact_path must be an absolute path to the job's checkout.tar")
    if ap.parent.name != str(job["id"]):
        raise PolicyViolation("artifact does not belong to this job (parent directory must be the job id)")
    sandbox, helper, volume = _names(job["id"])
    env_file_names = tuple(n for n, on in (("TOKEN", pass_target_token), ("PRQA_MODEL_TOKEN", model_token)) if on)
    create = build_docker_argv(image=image, network=network, job=job, model=model, runtime_minutes=runtime_minutes, env=env,
                               tree_sha256=tree_sha256, prompt_sha256=prompt_sha256, pass_target_token=pass_target_token,
                               docker_cli=docker_cli, env_file_names=env_file_names)
    himg = helper_image or image
    helper_create = [docker_cli, "create", "--name", helper, "--label", f"prqa.job={job['id']}", "--label", "prqa.kind=helper",
                     "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", "10001:10001",
                     "--tmpfs", "/tmp:size=64m,mode=1777", "--memory", "512m", "--cpus", "1", "--pids-limit", "64",
                     "--mount", f"type=volume,src={volume},dst={SANDBOX_CHECKOUT_ROOT}",
                     "--entrypoint", HELPER_ENTRYPOINT, himg, SANDBOX_CHECKOUT_ROOT, job["id"], HELPER_INPUT]
    return SandboxPlan(
        container=sandbox, helper=helper, volume=volume,
        volume_create=[docker_cli, "volume", "create", "--label", f"prqa.job={job['id']}", "--label", "prqa.kind=checkout", volume],
        helper_create=helper_create,
        helper_copy=[docker_cli, "cp", str(ap), f"{helper}:{HELPER_INPUT}"],
        helper_start=[docker_cli, "start", helper],
        helper_wait=[docker_cli, "wait", helper],
        helper_logs=[docker_cli, "logs", helper],
        create=create, start=[docker_cli, "start", sandbox],
        inspect_state=[docker_cli, "container", "inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", sandbox],
        logs=[docker_cli, "logs", sandbox],
        cleanup=[[docker_cli, "rm", "-f", sandbox], [docker_cli, "rm", "-f", helper], [docker_cli, "volume", "rm", "-f", volume]],
        artifact_path=str(ap), env_file_names=env_file_names,
        meta={"network": network or DEFAULT_NETWORK, "checkout_dir": SANDBOX_CHECKOUT_DIR, "volume": volume, "job_id": str(job["id"]),
              "docker_cli": docker_cli, "tree_sha256": str(tree_sha256), "prompt_sha256": str(prompt_sha256), "image": image},
    )


def run_live(job: Mapping[str, Any], entry: Any, policy: Policy, *, network: str | None = None,
             image: str | None = None, env: Mapping[str, str] | None = None,
             dry_run: bool = False, artifact_path: str | None = None, expected_hashes: Mapping[str, str] | None = None,
             runner: Any = subprocess.run, docker_cli: str | None = None,
             cancel_check: Callable[[], bool] | None = None, report: Callable[[str, dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Execute a live job in the sandbox. Raises PolicyViolation unless every gate is open, RunnerUnavailable when the
    worker has no way to launch containers (expected: the worker image carries no Docker CLI and no socket)."""
    if policy.review_only or not policy.real_execution_possible:
        raise PolicyViolation("live execution requested while policy forbids it (review_only or flags off)")
    if job["request"].get("synthetic") or job["execution_mode"] != "live":
        raise PolicyViolation("live runner called for a non-live job")
    if entry is None or entry.kind != "real" or not entry.effective_enabled(policy):
        raise PolicyViolation("repository is not effectively enabled")
    env = dict(env or os.environ)
    model = (entry.opencode or {}).get("model") or policy.opencode_model
    if not model:
        raise RunnerUnavailable("no OpenCode model configured (registry opencode.model or OPENCODE_MODEL)")
    if not artifact_path:
        raise RunnerUnavailable("no checkout artifact for this job: the worker-side checkout (PRQA_WORKER_CHECKOUT_ENABLED) is required for live execution")
    expected = dict(expected_hashes or {})
    if not HEX64.match(str(expected.get("tree_sha256", ""))) or not HEX64.match(str(expected.get("prompt_sha256", ""))):
        raise RunnerUnavailable("worker-held expected hashes (tree_sha256, prompt_sha256) are missing or malformed; refusing to launch")
    image = image or env.get("OPENCODE_SANDBOX_IMAGE", "prqa/opencode-sandbox:local")
    network = network if network is not None else env.get("PRQA_SANDBOX_NETWORK", DEFAULT_NETWORK)
    token_env_name = entry.sandbox_target_token_env()          # validated at registry load; None by default
    pass_token = bool(token_env_name and env.get(token_env_name))
    if token_env_name and not env.get(token_env_name):
        raise RunnerUnavailable(f"registry opts into pass_target_token but {token_env_name} is not set on the worker")
    sandbox_env = dict(env)
    if pass_token:
        sandbox_env["TOKEN"] = env[token_env_name]           # the only credential that may cross, under its generic name
    cli = docker_cli or env.get("PRQA_DOCKER_CLI", "docker")
    plan = build_sandbox_plan(image=image, network=network, job=job, model=model, runtime_minutes=entry.max_runtime_minutes,
                              env=sandbox_env, artifact_path=artifact_path, tree_sha256=expected["tree_sha256"],
                              prompt_sha256=expected["prompt_sha256"], pass_target_token=pass_token, docker_cli=cli)
    if dry_run:
        return {"plan": {"volume_create": plan.volume_create, "populate": plan.populate, "create": plan.create, "start": plan.start,
                         "cleanup": plan.cleanup, "env_file_names": list(plan.env_file_names), "meta": plan.meta}, "dry_run": True}
    # The worker process never launches containers: live jobs are handed to the separate launcher (launcher.py)
    # through the job store's launch lifecycle. Anything else here is a design violation, not a missing dependency.
    raise RunnerUnavailable("in-process launch is disabled: live jobs are executed by the separate prqa-launcher (launch_state hand-off)")


def _decode(x: Any) -> str:
    return x if isinstance(x, str) else (x or b"").decode("utf-8", "replace")


def _run_step(runner: Any, argv: list[str], *, stdin_data: Any = None, timeout: int = 60) -> Any:
    kwargs: dict[str, Any] = {"capture_output": True, "shell": False, "timeout": timeout, "check": False}
    if isinstance(stdin_data, (bytes, bytearray)):
        kwargs["input"] = bytes(stdin_data)
    else:
        kwargs["input"] = stdin_data
        kwargs["text"] = True
    try:
        return runner(argv, **kwargs)
    except FileNotFoundError:
        raise RunnerUnavailable(f"container launcher '{argv[0]}' not found")


def _step(runner: Any, argv: list[str], *, stdin_data: Any = None, timeout: int = 60, secrets: Mapping[str, str], label: str) -> Any:
    try:
        proc = _run_step(runner, argv, stdin_data=stdin_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"sandbox step '{label}' timed out after {timeout}s; resources removed")
    if proc.returncode != 0:
        raise RuntimeError(f"sandbox step '{label}' exited {proc.returncode}: {redact_text(_decode(proc.stderr)[-2000:], dict(secrets))}")
    return proc


def execute_plan(plan: SandboxPlan, *, env: Mapping[str, str], runner: Any = subprocess.run, timeout_s: int,
                 cancel_check: Callable[[], bool] | None = None, report: Callable[[str, dict[str, Any]], None] | None = None,
                 poll_s: float = 2.0, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Run the plan without any attached/hijacked stream: helper via create+cp+start+wait+logs, sandbox via
    create+start+inspect-polling+logs. `cancel_check()` is consulted before every step and every `poll_s` while the
    sandbox runs; a positive answer terminates the sandbox (`rm -f`) and raises SandboxCancelled. The helper's and the
    sandbox's exit codes are propagated as failures. Cleanup of THIS job's resources always runs, is verified and is
    reported through `report("sandbox_cleanup", …)` even when execution failed (the original error is re-raised)."""
    secrets = {k: env[k] for k in SECRET_ENV_NAMES if env.get(k)}
    if not Path(plan.artifact_path).is_file():
        raise RuntimeError("checkout artifact is missing; refusing to launch")
    env_file = "".join(f"{k}={env[k]}\n" for k in plan.env_file_names if env.get(k))
    cancelled = lambda: bool(cancel_check and cancel_check())  # noqa: E731
    cli = plan.meta.get("docker_cli", plan.create[0])

    def guard(label: str) -> None:
        if cancelled():
            raise SandboxCancelled(f"cancelled before step '{label}'")
    try:
        guard("volume create"); _step(runner, plan.volume_create, timeout=60, secrets=secrets, label="volume create")
        guard("helper create"); _step(runner, plan.helper_create, timeout=60, secrets=secrets, label="helper create")
        guard("helper copy");   _step(runner, plan.helper_copy, timeout=300, secrets=secrets, label="helper copy")
        guard("helper start");  _step(runner, plan.helper_start, timeout=60, secrets=secrets, label="helper start")
        wait = _step(runner, plan.helper_wait, timeout=300, secrets=secrets, label="helper wait")
        try:
            helper_rc = int(_decode(wait.stdout).strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise RuntimeError("helper wait returned no exit code")
        if helper_rc != 0:
            logs = _run_step(runner, plan.helper_logs, timeout=60)
            raise RuntimeError(f"sandbox step 'populate' exited {helper_rc}: {redact_text((_decode(logs.stderr) or _decode(logs.stdout))[-2000:], secrets)}")
        guard("create");        _step(runner, plan.create, stdin_data=env_file if plan.env_file_names else None, timeout=60, secrets=secrets, label="create")
        guard("start");         _step(runner, plan.start, timeout=60, secrets=secrets, label="start")
        deadline = clock() + float(timeout_s)
        exit_code: int | None = None
        while True:
            insp = _run_step(runner, plan.inspect_state, timeout=30)
            text = _decode(insp.stdout).strip()
            status, _, code = text.partition(" ")
            if insp.returncode == 0 and status in ("exited", "dead"):
                try:
                    exit_code = int(code)
                except ValueError:
                    exit_code = -1
                break
            if cancelled():
                _run_step(runner, [cli, "rm", "-f", plan.container], timeout=60)
                raise SandboxCancelled("cancelled while the sandbox was running; sandbox terminated")
            if clock() >= deadline:
                _run_step(runner, [cli, "rm", "-f", plan.container], timeout=60)
                raise RuntimeError(f"sandbox step 'run' timed out after {timeout_s}s; resources removed")
            sleep(poll_s)
        logs = _run_step(runner, plan.logs, timeout=120)
        if exit_code != 0:
            raise RuntimeError(f"sandbox step 'run' exited {exit_code}: {redact_text(_decode(logs.stderr)[-2000:], secrets)}")
        out = _decode(logs.stdout)
        last = out.strip().splitlines()[-1] if out.strip() else "{}"
        return json.loads(last)
    finally:
        outcome = cleanup_plan(plan, runner=runner)
        if report is not None:
            try:
                report("sandbox_cleanup", outcome)
            except Exception:  # noqa: BLE001 — reporting must never mask the original failure
                pass


def _resource_exists(runner: Any, cli: str, kind: str, name: str) -> bool | None:
    """True/False, or None when Docker itself could not answer (treated as 'unknown', never as 'absent')."""
    try:
        proc = runner([cli, kind, "inspect", "--format", "{{.Name}}", name], capture_output=True, shell=False, timeout=30, check=False, text=True)
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode == 0:
        return True
    err = _decode(proc.stderr).lower()
    if "no such" in err or "not found" in err:
        return False
    return None


def cleanup_plan(plan: SandboxPlan, *, runner: Any = subprocess.run) -> dict[str, Any]:
    """Ownership-scoped removal of this job's sandbox container, helper container and volume, VERIFIED afterwards.
    An already-absent resource counts as cleaned; a resource that still exists (or whose state cannot be determined)
    after removal is reported as a failure. Never raises."""
    cli = plan.meta.get("docker_cli", plan.create[0])
    resources = []
    for kind, name, argv in (("container", plan.container, plan.cleanup[0]), ("container", plan.helper, plan.cleanup[1]),
                             ("volume", plan.volume, plan.cleanup[2])):
        rec: dict[str, Any] = {"kind": kind, "name": name, "job_id": plan.meta.get("job_id")}
        before = _resource_exists(runner, cli, kind, name)
        rec["existed_before"] = before
        if before is False:
            rec.update({"action": "already_absent", "verified_absent": True})
            resources.append(rec)
            continue
        try:
            proc = runner(argv, capture_output=True, shell=False, timeout=60, check=False, text=True)
            rec["rm_rc"] = int(getattr(proc, "returncode", -1))
            err = _decode(getattr(proc, "stderr", ""))
            if rec["rm_rc"] != 0:
                rec["error"] = redact_text(err.strip()[-300:])
        except Exception as exc:  # noqa: BLE001
            rec["rm_rc"] = -1
            rec["error"] = f"{type(exc).__name__}: {redact_text(str(exc))[:200]}"
        after = _resource_exists(runner, cli, kind, name)
        rec["verified_absent"] = after is False
        rec["action"] = "removed" if after is False else ("still_present" if after else "unknown")
        resources.append(rec)
    failed = [r for r in resources if not r.get("verified_absent")]
    return {"ok": not failed, "job_id": plan.meta.get("job_id"), "resources": resources,
            "failed": [{"kind": r["kind"], "name": r["name"], "action": r["action"], "error": r.get("error")} for r in failed]}


def describe_argv(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)
