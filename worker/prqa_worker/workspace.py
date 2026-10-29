"""Isolated per-job workspaces and a pinned, hook-free checkout of the validated PR head SHA.

Design
------
* One directory per job under ``PRQA_WORKSPACE_ROOT`` (``<data>/workspaces`` by default), named by the job id only.
  Job ids are validated against ``JOB_ID_RE`` and the resolved path must stay directly under the root
  (no traversal, no symlinked escapes, no cross-job collisions).
* Every workspace carries a ``.prqa-workspace.json`` marker. Cleanup only ever removes directories that carry
  the marker and are older than the retention window; anything else under the root is left alone.
* ``build_checkout_plan`` is a pure function: it derives the remote URL from the *validated* repository
  name (never from a caller-supplied URL), pins the fetch to the 40-hex head SHA, disables hooks,
  submodules, prompts, system/user git config and every protocol except https. The token is injected at
  run time through ``GIT_CONFIG_*`` environment variables, never through argv and never written to disk.
* ``run_checkout`` executes the plan step by step with a bounded timeout, verifies ``HEAD`` equals the
  requested SHA, and redacts the token from any error output. Nothing inside the repository is executed:
  no hooks, no submodule init, no build. Executing repository code remains a separately approved sandbox
  policy (see worker/sandbox/).

Review-only mode never runs a real checkout: the mock executor only records the plan next to the marker.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from collections.abc import Callable

from .redaction import redact_text

MARKER = ".prqa-workspace.json"
ARTIFACT_MANIFEST = ".prqa-checkout.json"
ARTIFACT_NAME = "checkout.tar"
DEFAULT_MAX_CHECKOUT_MB = 500
DEFAULT_MIN_FREE_MB = 1024
DEFAULT_MAX_WORKSPACES = 50
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$")
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_REMOTE_BASE = "https://github.com"
DEFAULT_TIMEOUT_S = 300
MAX_STDERR = 2000


class WorkspaceError(RuntimeError):
    pass


class CheckoutError(WorkspaceError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------- plan ----
@dataclass(frozen=True)
class CheckoutPlan:
    repository: str
    head_sha: str
    remote_url: str
    workdir: str
    steps: list[list[str]] = field(default_factory=list)
    step_names: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)      # public environment only; the token is added at run time
    timeout_s: int = DEFAULT_TIMEOUT_S

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_checkout_plan(*, repository: str, head_sha: str, workdir: str | Path, timeout_s: int = DEFAULT_TIMEOUT_S,
                        remote_base: str = DEFAULT_REMOTE_BASE, allowed_protocols: tuple[str, ...] = ("https",)) -> CheckoutPlan:
    """Deterministic git argv for a pinned, shallow, hook-free checkout. Raises WorkspaceError on bad input."""
    if not REPO_RE.match(repository or ""):
        raise WorkspaceError("invalid repository name")
    if not SHA_RE.match(head_sha or ""):
        raise WorkspaceError("head_sha must be 40 lowercase hex characters")
    if ".." in repository.split("/") or repository.endswith(".git"):
        raise WorkspaceError("invalid repository name")
    wd = Path(workdir)
    if not wd.is_absolute():
        raise WorkspaceError("workdir must be absolute")
    base = remote_base.rstrip("/")
    if not re.match(r"^(https://[A-Za-z0-9.-]+(:\d+)?|file:///[^\s]*)$", base):
        raise WorkspaceError("remote_base must be https://host[:port] (or file:///… for tests)")
    remote_url = f"{base}/{repository}.git"
    no_hooks = str(wd / ".prqa-no-hooks")
    common = ["git", "-C", str(wd),
              "-c", f"core.hooksPath={no_hooks}",
              "-c", "protocol.allow=never"]
    for proto in allowed_protocols:
        if not re.match(r"^[a-z]+$", proto):
            raise WorkspaceError("invalid protocol name")
        common += ["-c", f"protocol.{proto}.allow=always"]
    common += ["-c", "submodule.recurse=false", "-c", "core.fsmonitor=false", "-c", "gc.auto=0",
               "-c", "fetch.recurseSubmodules=false", "-c", "credential.helper="]
    steps = [
        ["git", "-c", f"init.templateDir={no_hooks}", "init", "--quiet", str(wd)],
        common + ["remote", "add", "origin", remote_url],
        common + ["fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--depth", "1", "origin", head_sha],
        common + ["checkout", "--quiet", "--detach", "--no-recurse-submodules", "FETCH_HEAD"],
        common + ["rev-parse", "--verify", "HEAD"],
    ]
    env = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_ASKPASS": "/bin/false", "LC_ALL": "C", "HOME": str(wd), "GIT_PROTOCOL_FROM_USER": "0"}
    return CheckoutPlan(repository=repository, head_sha=head_sha, remote_url=remote_url, workdir=str(wd),
                        steps=steps, step_names=["init", "remote-add", "fetch", "checkout", "verify"], env=env, timeout_s=int(timeout_s))


def _token_env(plan: CheckoutPlan, token: str | None) -> dict[str, str]:
    """Token travels as an http.extraheader via GIT_CONFIG_* (env only: not argv, not on disk)."""
    if not token:
        return {}
    host = re.match(r"^(https://[^/]+)", plan.remote_url)
    if not host:
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{host.group(1)}/.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}


def tree_size_bytes(path: str | Path) -> int:
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for f in files:
            fp = os.path.join(root, f)
            if not os.path.islink(fp):
                try:
                    total += os.stat(fp).st_size
                except OSError:
                    pass
    return total


def run_checkout(plan: CheckoutPlan, *, token: str | None = None, runner: Callable[..., Any] = subprocess.run,
                 clock: Callable[[], float] = time.monotonic, git_path: str | None = None,
                 max_checkout_mb: float = DEFAULT_MAX_CHECKOUT_MB) -> dict[str, Any]:
    """Execute the plan. Each step is a bounded subprocess (no shell). Verifies HEAD == head_sha and the size bound."""
    git = git_path or shutil.which("git")
    if not git:
        raise CheckoutError("git is not installed on the worker")
    wd = Path(plan.workdir)
    wd.mkdir(parents=True, exist_ok=True)
    (wd / ".prqa-no-hooks").mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    env.update(plan.env)
    env.update(_token_env(plan, token))
    secrets = {"github_token": token} if token else {}
    if env.get("GIT_CONFIG_VALUE_0"):
        secrets["basic_auth"] = env["GIT_CONFIG_VALUE_0"]
    started = clock()
    deadline = started + plan.timeout_s
    head = ""
    for i, step in enumerate(plan.steps, 1):
        name = plan.step_names[i - 1] if i <= len(plan.step_names) else f"step{i}"
        remaining = deadline - clock()
        if remaining <= 0:
            raise CheckoutError(f"checkout timed out before step {i}/{len(plan.steps)} ({name})")
        argv = [git] + step[1:]
        try:
            proc = runner(argv, env=env, capture_output=True, text=True, shell=False, timeout=max(1, int(remaining)), check=False)
        except subprocess.TimeoutExpired:
            raise CheckoutError(f"checkout step {i}/{len(plan.steps)} ({name}) timed out")
        if proc.returncode != 0:
            err = redact_text(str(proc.stderr or "")[-MAX_STDERR:], secrets)
            raise CheckoutError(f"checkout step {i}/{len(plan.steps)} ({name}) failed: {err}")
        if name == "verify":
            head = str(proc.stdout or "").strip()
    if head != plan.head_sha:
        raise CheckoutError(f"checked-out HEAD {head[:12] or '?'} does not match the requested SHA")
    size = tree_size_bytes(wd)
    if size > max_checkout_mb * 1024 * 1024:
        shutil.rmtree(wd, ignore_errors=True)
        raise CheckoutError(f"checkout exceeds PRQA_CHECKOUT_MAX_MB ({size // (1024 * 1024)} MiB > {int(max_checkout_mb)} MiB); workspace discarded")
    return {"head": head, "steps": len(plan.steps), "duration_ms": int((clock() - started) * 1000), "remote_url": plan.remote_url,
            "size_bytes": size}


# ----------------------------------------------------------- artifact ----
# Artifact layout (tar, extracted by `docker cp -` into /checkout):
#   repo/<relative path>        the checked-out working tree WITHOUT .git (repository-controlled, untrusted)
#   prqa/manifest.json          trusted: job id, repository, head SHA, file count, tree hash, prompt hash
#   prqa/prompt.md              trusted: the PRQA-generated prompt (never taken from the repository)
# The sandbox verifier (worker/sandbox/verify_checkout.py) recomputes the tree hash with TREE_HASH_VERSION below.
TREE_HASH_VERSION = "prqa-tree-v1"
TRUSTED_DIR = "prqa"
MANIFEST_MEMBER = f"{TRUSTED_DIR}/manifest.json"
PROMPT_MEMBER = f"{TRUSTED_DIR}/prompt.md"


def _walk_regular_files(root: Path):
    """Sorted (relative path, absolute path) of regular files; .git dirs and symlinks are skipped, never followed."""
    out = []
    for cur, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != ".git" and not os.path.islink(os.path.join(cur, d)))
        rel_root = os.path.relpath(cur, root)
        for name in names:
            full = os.path.join(cur, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = name if rel_root == "." else os.path.join(rel_root, name)
            if rel == ".git" or rel.startswith(".git/") or ".." in rel.split(os.sep):
                continue
            out.append((rel.replace(os.sep, "/"), full))
    return sorted(out)


def tree_digest(root: str | Path) -> tuple[str, int]:
    """Deterministic tree hash: for every regular file in sorted relative-path order,
    sha256 over  version \0 path \0 mode(x|r) \0 size \0 sha256(content) \0 . Returns (hex digest, file count)."""
    h = hashlib.sha256()
    h.update(TREE_HASH_VERSION.encode("utf-8") + b"\0")
    n = 0
    for rel, full in _walk_regular_files(Path(root)):
        st = os.stat(full)
        mode = b"x" if st.st_mode & 0o100 else b"r"
        with open(full, "rb") as fh:
            content = hashlib.sha256(fh.read()).hexdigest().encode("ascii")
        h.update(rel.encode("utf-8") + b"\0" + mode + b"\0" + str(st.st_size).encode("ascii") + b"\0" + content + b"\0")
        n += 1
    return h.hexdigest(), n


def pack_checkout_artifact(*, repo_dir: str | Path, out_path: str | Path, job_id: str, repository: str, head_sha: str,
                           prompt_text: str, max_mb: float = DEFAULT_MAX_CHECKOUT_MB,
                           known_secrets: dict[str, str] | None = None) -> dict[str, Any]:
    """Per-job checkout artifact for the sandbox (see layout above). Symlinks skipped, every member owner-read-only,
    uid/gid normalised, no .git, trusted files outside repo/. Refuses secrets inside the prompt."""
    if not JOB_ID_RE.match(job_id) or not REPO_RE.match(repository) or not SHA_RE.match(head_sha):
        raise WorkspaceError("artifact identity fields are invalid")
    if not isinstance(prompt_text, str) or len(prompt_text.strip()) < 200:
        raise WorkspaceError("prompt_text is missing or too short to be a PRQA prompt")
    for name, value in (known_secrets or {}).items():
        if value and len(value) >= 6 and value in prompt_text:
            raise WorkspaceError(f"prompt contains a secret value ({name}); refusing to pack")
    if redact_text(prompt_text) != prompt_text:
        raise WorkspaceError("prompt contains secret-looking content; refusing to pack")
    repo_dir = Path(repo_dir)
    out_path = Path(out_path)
    if not repo_dir.is_dir():
        raise WorkspaceError("repo_dir does not exist")
    limit = int(max_mb * 1024 * 1024)
    total = 0
    tmp = out_path.with_suffix(".tmp")
    tree_hash, files = tree_digest(repo_dir)
    try:
        with tarfile.open(tmp, "w") as tar:
            for rel, full in _walk_regular_files(repo_dir):
                st = os.stat(full)
                total += st.st_size
                if total > limit:
                    raise WorkspaceError(f"artifact exceeds PRQA_CHECKOUT_MAX_MB ({int(max_mb)} MiB)")
                info = tar.gettarinfo(full, arcname=f"repo/{rel}")
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = 0o555 if st.st_mode & 0o100 else 0o444
                with open(full, "rb") as fh:
                    tar.addfile(info, fh)
            prompt_blob = prompt_text.encode("utf-8")
            manifest = {"artifact_version": "2.0", "tree_hash_version": TREE_HASH_VERSION, "job_id": job_id, "repository": repository,
                        "head_sha": head_sha, "files": files, "tree_sha256": tree_hash, "prompt_sha256": hashlib.sha256(prompt_blob).hexdigest(),
                        "prompt_chars": len(prompt_text), "packed_at": _now(), "git_dir_included": False}
            for member, blob in ((PROMPT_MEMBER, prompt_blob), (MANIFEST_MEMBER, json.dumps(manifest, indent=1).encode("utf-8"))):
                info = tarfile.TarInfo(member)
                info.size = len(blob); info.mode = 0o444; info.uid = info.gid = 0; info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(blob))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, out_path)
    manifest["path"] = str(out_path)
    manifest["size_bytes"] = out_path.stat().st_size
    return manifest


def read_artifact_manifest(path: str | Path) -> dict[str, Any]:
    with tarfile.open(path, "r") as tar:
        member = tar.extractfile(MANIFEST_MEMBER)
        if member is None:
            raise WorkspaceError("artifact has no manifest")
        return json.loads(member.read().decode("utf-8"))


# ---------------------------------------------------------- workspaces ----
class WorkspaceManager:
    def __init__(self, root: str | Path, *, retention_hours: float = 24.0, clock: Callable[[], float] = time.time,
                 min_free_mb: float = DEFAULT_MIN_FREE_MB, max_workspaces: int = DEFAULT_MAX_WORKSPACES) -> None:
        self.root = Path(root).resolve()
        self.retention_s = float(retention_hours) * 3600.0
        self.clock = clock
        self.min_free_mb = float(min_free_mb)
        self.max_workspaces = int(max_workspaces)
        self.root.mkdir(parents=True, exist_ok=True)

    def free_mb(self) -> float:
        return shutil.disk_usage(self.root).free / (1024 * 1024)

    def _check_capacity(self) -> None:
        if self.free_mb() < self.min_free_mb:
            raise WorkspaceError(f"insufficient free space under the workspace root (< PRQA_WORKSPACE_MIN_FREE_MB={int(self.min_free_mb)})")
        marked = [e for e in self.root.iterdir() if e.is_dir() and not e.is_symlink() and self.read_marker(e)]
        if len(marked) >= self.max_workspaces:
            swept = self.cleanup(max_age_s=0)      # released workspaces go first, regardless of age
            if len(marked) - len(swept["removed"]) >= self.max_workspaces:
                raise WorkspaceError(f"workspace count limit reached (PRQA_WORKSPACE_MAX_COUNT={self.max_workspaces}); all are in use")

    def path_for(self, job_id: str) -> Path:
        if not JOB_ID_RE.match(job_id or "") or "/" in job_id or "\\" in job_id or ".." in job_id:
            raise WorkspaceError("invalid job id for a workspace")
        candidate = (self.root / job_id)
        resolved = candidate.resolve() if candidate.exists() else candidate.parent.resolve() / job_id
        if resolved.parent != self.root or resolved.name != job_id:
            raise WorkspaceError("workspace path escapes the workspace root")
        return resolved

    def create(self, job_id: str, *, repository: str, head_sha: str, execution_mode: str) -> Path:
        self._check_capacity()
        path = self.path_for(job_id)
        if path.is_symlink():
            raise WorkspaceError("workspace path is a symlink")
        if path.exists():
            if not (path / MARKER).is_file():
                raise WorkspaceError("workspace path exists without a PRQA marker; refusing to reuse it")
            shutil.rmtree(path)        # a retry of the same job starts from a clean directory
        path.mkdir(mode=0o700)
        self._write_marker(path, {"job_id": job_id, "repository": repository, "head_sha": head_sha, "execution_mode": execution_mode,
                                  "created_at": _now(), "created_ts": self.clock(), "state": "created"})
        return path

    def mark(self, path: Path, **fields: Any) -> dict[str, Any]:
        info = self.read_marker(path) or {}
        info.update(fields)
        info["updated_at"] = _now()
        self._write_marker(path, info)
        return info

    @staticmethod
    def _write_marker(path: Path, info: dict[str, Any]) -> None:
        (path / MARKER).write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")

    @staticmethod
    def read_marker(path: Path) -> dict[str, Any] | None:
        try:
            return json.loads((path / MARKER).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _owned(self, entry: Path) -> bool:
        """True only for a real directory directly under the root that carries a PRQA marker naming itself."""
        if entry.is_symlink() or not entry.is_dir():
            return False
        try:
            resolved = entry.resolve(strict=True)
        except OSError:
            return False
        if resolved.parent != self.root or not JOB_ID_RE.match(entry.name):
            return False
        marker = self.read_marker(entry)
        return bool(marker) and marker.get("job_id") == entry.name

    def purge_sources(self, job_id: str, *, keep: tuple[str, ...] = (MARKER, "checkout-plan.json", "diagnostics.json")) -> dict[str, Any]:
        """Delete the extracted tree and the artifact of ONE owned workspace as soon as the sandbox no longer needs
        them; marker, plan and diagnostics stay for troubleshooting. Never touches other directories."""
        path = self.path_for(job_id)
        if not self._owned(path):
            return {"purged": [], "skipped": "not an owned workspace"}
        purged = []
        for entry in sorted(path.iterdir()):
            if entry.name in keep:
                continue
            if entry.is_symlink() or entry.is_file():
                entry.unlink(); purged.append(entry.name)
            elif entry.is_dir():
                shutil.rmtree(entry); purged.append(entry.name + "/")
        self.mark(path, sources_purged_at=_now())
        return {"purged": purged}

    def remove(self, job_id: str) -> bool:
        path = self.path_for(job_id)
        if not self._owned(path):
            return False
        shutil.rmtree(path)
        return True

    def cleanup(self, *, max_age_s: float | None = None) -> dict[str, Any]:
        """Remove PRQA-marked workspaces older than the retention window. Never touches unmarked entries."""
        max_age = self.retention_s if max_age_s is None else float(max_age_s)
        now = self.clock()
        removed: list[str] = []
        skipped: list[str] = []
        for entry in sorted(self.root.iterdir()):
            if not self._owned(entry):
                skipped.append(entry.name)
                continue
            marker = self.read_marker(entry) or {}
            if marker.get("state") in ("in_use",):
                continue
            created = float(marker.get("created_ts") or 0)
            if now - created >= max_age:
                shutil.rmtree(entry)
                removed.append(entry.name)
        return {"removed": removed, "skipped_unmarked": skipped, "retention_s": max_age}
