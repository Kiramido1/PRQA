"""Launcher-side sandbox lifecycle helpers: prompt cancellation and reconciliation of orphaned job-owned resources.

Architecture note (docs/SANDBOX_TRUST_BOUNDARY.md §4): the worker container has no Docker CLI and no socket. These
functions are pure orchestration over an injectable `runner` (subprocess.run-compatible) and an injectable job-status
lookup, so they can be executed by the separate Phase 2 launcher process (or by the opt-in local Docker test) with
whatever job-state interface that process has: the worker HTTP API (`GET /jobs/{id}`) or the job store directly.

Every sandbox resource PRQA creates carries the labels `prqa.job=<job id>` and `prqa.kind=sandbox|helper|checkout`
(opencode_runner.build_sandbox_plan). Reconciliation only ever touches resources that carry BOTH labels, and removes
them only when the owning job is terminal, or when the job is unknown to the status source AND the resource is older
than a grace period. Resources of active jobs are never removed. The pass is idempotent and safe to repeat.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any
from collections.abc import Callable

from .jobs import ACTIVE_STATES, TERMINAL_STATES
from .opencode_runner import VOLUME_SAFE, _names, _resource_exists
from .redaction import redact_text

DEFAULT_GRACE_S = 6 * 3600
LABEL_JOB = "prqa.job"
LABEL_KIND = "prqa.kind"
KINDS = ("sandbox", "helper", "checkout")


@dataclass
class ReconcileReport:
    removed: list[dict[str, Any]] = field(default_factory=list)
    kept: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    scanned: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"scanned": self.scanned, "removed": self.removed, "kept": self.kept, "failed": self.failed, "ok": not self.failed}


def _run(runner: Any, argv: list[str], timeout: int = 60) -> tuple[int, str, str]:
    try:
        proc = runner(argv, capture_output=True, shell=False, timeout=timeout, check=False, text=True)
    except Exception as exc:  # noqa: BLE001
        return -1, "", f"{type(exc).__name__}: {exc}"
    out = proc.stdout if isinstance(proc.stdout, str) else (proc.stdout or b"").decode("utf-8", "replace")
    err = proc.stderr if isinstance(proc.stderr, str) else (proc.stderr or b"").decode("utf-8", "replace")
    return int(proc.returncode), out, err


# ------------------------------------------------------------- cancel --
def cancel_sandbox(job_id: str, *, runner: Any = subprocess.run, docker_cli: str = "docker") -> dict[str, Any]:
    """Terminate the job's sandbox promptly (rm -f), then remove helper and volume. Verified, never raises."""
    if not VOLUME_SAFE.match(str(job_id)):
        return {"ok": False, "job_id": job_id, "error": "unsafe job id"}
    sandbox, helper, volume = _names(job_id)
    resources = []
    for kind, name, argv in (("container", sandbox, [docker_cli, "rm", "-f", sandbox]), ("container", helper, [docker_cli, "rm", "-f", helper]),
                             ("volume", volume, [docker_cli, "volume", "rm", "-f", volume])):
        before = _resource_exists(runner, docker_cli, kind, name)
        rec: dict[str, Any] = {"kind": kind, "name": name, "existed_before": before}
        if before is False:
            rec.update({"action": "already_absent", "verified_absent": True}); resources.append(rec); continue
        rc, _, err = _run(runner, argv)
        rec["rm_rc"] = rc
        if rc != 0:
            rec["error"] = redact_text(err.strip()[-300:])
        after = _resource_exists(runner, docker_cli, kind, name)
        rec["verified_absent"] = after is False
        rec["action"] = "removed" if after is False else ("still_present" if after else "unknown")
        resources.append(rec)
    return {"ok": all(r["verified_absent"] for r in resources), "job_id": job_id, "resources": resources, "at": _now()}


# ---------------------------------------------------------- reconcile --
def list_labelled_resources(*, runner: Any = subprocess.run, docker_cli: str = "docker") -> list[dict[str, Any]]:
    """Containers (any state) and volumes that carry prqa.job + prqa.kind labels, with creation time."""
    out: list[dict[str, Any]] = []
    rc, text, _ = _run(runner, [docker_cli, "ps", "-a", "--filter", f"label={LABEL_JOB}", "--filter", f"label={LABEL_KIND}",
                                "--format", '{{.Names}}\t{{.Label "prqa.job"}}\t{{.Label "prqa.kind"}}\t{{.CreatedAt}}\t{{.State}}'])
    if rc == 0:
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) >= 5 and parts[2] in ("sandbox", "helper"):
                out.append({"type": "container", "name": parts[0], "job_id": parts[1], "kind": parts[2], "created": parts[3], "state": parts[4]})
    rc, text, _ = _run(runner, [docker_cli, "volume", "ls", "--filter", f"label={LABEL_JOB}", "--filter", f"label={LABEL_KIND}=checkout",
                                "--format", '{{.Name}}\t{{.Label "prqa.job"}}'])
    if rc == 0:
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0]:
                created = None
                rc2, insp, _ = _run(runner, [docker_cli, "volume", "inspect", "--format", "{{.CreatedAt}}", parts[0]])
                if rc2 == 0:
                    created = insp.strip()
                out.append({"type": "volume", "name": parts[0], "job_id": parts[1], "kind": "checkout", "created": created, "state": None})
    return out


def _age_s(created: str | None, now: float) -> float | None:
    if not created:
        return None
    text = created.strip()
    # docker ps prints "2026-10-22 13:24:37 +0300 EEST"; volume inspect prints RFC3339 "2026-10-22T10:24:37Z"
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            cleaned = " ".join(text.split(" ")[:3]) if " " in text else text.replace("Z", "+0000")
            return now - datetime.strptime(cleaned, fmt).timestamp()
        except ValueError:
            continue
    try:
        return now - datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def reconcile(*, job_status: Callable[[str], str | None], runner: Any = subprocess.run, docker_cli: str = "docker",
              grace_s: float = DEFAULT_GRACE_S, dry_run: bool = False, clock: Callable[[], float] = time.time) -> ReconcileReport:
    """Remove PRQA-labelled sandbox resources whose owning job is terminal, or unknown and older than `grace_s`.
    `job_status(job_id)` returns the job's status string or None when the job is unknown. Resources whose job is
    active (queued/running), whose age cannot be determined while the job is unknown, or whose labels do not pass the
    name/ownership check are kept. Idempotent."""
    report = ReconcileReport()
    now = clock()
    for res in list_labelled_resources(runner=runner, docker_cli=docker_cli):
        report.scanned += 1
        job_id = res["job_id"]
        rec = dict(res)
        expected_name = None
        if VOLUME_SAFE.match(str(job_id)):
            s, h, v = _names(job_id)
            expected_name = {"sandbox": s, "helper": h, "checkout": v}[res["kind"]]
        if expected_name is None or res["name"] != expected_name:
            rec["reason"] = "name does not match the labelled job; left alone"
            report.kept.append(rec); continue
        status = job_status(job_id)
        rec["job_status"] = status
        if status in ACTIVE_STATES:
            rec["reason"] = "job active"; report.kept.append(rec); continue
        if status not in TERMINAL_STATES:
            age = _age_s(res.get("created"), now)
            if status is None and age is not None and age >= grace_s:
                rec["reason"] = f"job unknown and resource older than grace ({int(age)}s)"
            else:
                rec["reason"] = "job unknown; within grace or age undetermined" if status is None else f"status {status!r} not terminal"
                report.kept.append(rec); continue
        else:
            rec["reason"] = f"job terminal ({status})"
        if dry_run:
            rec["action"] = "would_remove"; report.removed.append(rec); continue
        argv = [docker_cli, "rm", "-f", res["name"]] if res["type"] == "container" else [docker_cli, "volume", "rm", "-f", res["name"]]
        rc, _, err = _run(runner, argv)
        after = _resource_exists(runner, docker_cli, "container" if res["type"] == "container" else "volume", res["name"])
        rec["rm_rc"] = rc
        rec["verified_absent"] = after is False
        if after is False:
            rec["action"] = "removed"; report.removed.append(rec)
        else:
            rec["action"] = "still_present" if after else "unknown"; rec["error"] = redact_text(err.strip()[-200:]); report.failed.append(rec)
    return report


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def describe(report: ReconcileReport) -> str:
    return json.dumps(report.to_dict(), indent=1)
