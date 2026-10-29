#!/usr/bin/env python3
"""Automatic, label-triggered PRQA execution for the connected project (Kiramido1/Amenly-Product) — no manual prompt review; the quality gate is the
mandatory automated prompt approval. Every gate of scripts/pipeline/prqa_run_single_pr.py is reused unchanged:

    poll (gh CLI, read-only) → eligible PR (open, non-draft, same repo, allowed base, EXACT label `to be test`, verified
    head SHA) not yet processed for (PR, SHA, POLICY_VERSION) → preview: exact-commit fetch, disposable API + Postgres at
    that SHA, OpenAPI of the running head, synthetic self-registration probe, context + deterministic prompt from the
    worker, gate on the body AND on the complete final text (threshold 85, hard fails) → ONLY if accepted: one automatic
    single-use grant (purpose auto_pr_run) bound to repo/PR/SHA/level AND the prompt hash, 15 min → execute: every
    precondition re-validated (label, head, base, prompt hash, re-gate, GITHUB_TOKEN for a private repo, grant key, registry base_url,
    sandbox → API reachability, synthetic identity), grant consumed atomically with exactly one job by the worker →
    launcher → real OpenCode sandbox (curl only) → model proxy + shared budget ledger → report.md + qa-result.json +
    checklist → cleanup of this run's resources → local notification.

    python3 scripts/pipeline/prqa_auto_runner.py status            # prerequisites, eligible PRs, processed keys (read-only)
    python3 scripts/pipeline/prqa_auto_runner.py once              # one pass (at most one PR, concurrency 1)
    python3 scripts/pipeline/prqa_auto_runner.py serve --interval 60
    python3 scripts/pipeline/prqa_auto_runner.py activate [--apply]    # scoped project activation; refuses while a prerequisite is missing
    python3 scripts/pipeline/prqa_auto_runner.py retry --key KEY   # re-allow one blocked/interrupted key (operator decision)
    python3 scripts/pipeline/prqa_auto_runner.py simulate --pr N   # SIMULATION: same real pipeline on an explicit PR without the
                                                          # label (labels are never changed); a private repo is checked out
                                                          # from a local mirror; report + Discord are marked SIMULATION

State (dedupe + recovery): $PRQA_AUTO_STATE_DIR or ~/.local/state/prqa/auto/state.json (local disk: flock works there),
written atomically under an exclusive lock, so repeated polls, restarts and concurrent runners never start a second
pipeline for the same key. Recovery never re-runs: an in-flight key is resumed when its job exists, otherwise marked
`interrupted` and its environment cleaned. The worker's own dedupe key (repo#pr@sha:level) and the single-use grant are
the second and third layers. No credential value is printed, logged, written to artifacts or passed in argv.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("prqa_run_single_pr", ROOT / "scripts" / "pipeline" / "prqa_run_single_pr.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)  # type: ignore[union-attr]
from prqa_worker import report as report_mod, run_grants  # noqa: E402  (worker/ is on sys.path via the runner)
from prqa_worker.prompt_builder import parse_scenarios  # noqa: E402
_dspec = importlib.util.spec_from_file_location("prqa_discord_delivery", ROOT / "scripts" / "pipeline" / "prqa_discord_delivery.py")
DD = importlib.util.module_from_spec(_dspec)
_dspec.loader.exec_module(DD)  # type: ignore[union-attr]

POLICY_VERSION = "auto-v1"          # bump when the gate / prompt / execution policy changes: every SHA becomes a new key
GRANT_TTL_S = 900
IN_FLIGHT = ("claimed", "submitting", "submitted")
TERMINAL = ("executed", "blocked", "interrupted")
STATE_DIR = Path(os.environ.get("PRQA_AUTO_STATE_DIR") or Path.home() / ".local" / "state" / "prqa" / "auto")
AUTO_DIR = ROOT / "runs" / "prqa-auto"
Out = Callable[[str], None]


class Busy(Exception):
    pass


# ------------------------------------------------------------------------------------------------ state + lock --
@contextlib.contextmanager
def locked(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(state_dir / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("another PRQA auto runner holds the lock")
        yield
    finally:
        os.close(fd)


def load_state(state_dir: Path) -> dict[str, Any]:
    p = state_dir / "state.json"
    if not p.is_file():
        return {"policy_version": POLICY_VERSION, "keys": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def save_state(state_dir: Path, state: dict[str, Any]) -> None:
    p = state_dir / "state.json"
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def run_key(pr: int, sha: str, simulation: bool = False) -> str:
    return f"{R.REPO}#{pr}@{sha}:{POLICY_VERSION}{'-sim' if simulation else ''}"


# ------------------------------------------------------------------------------------------------ notification --
def notify(event: dict[str, Any], *, auto_dir: Path, out: Out) -> None:
    """Local only: an append-only JSONL file and, when available, a desktop notification. Failures never change a
    verdict and never trigger anything."""
    event = dict(event, at=R.now_iso())
    try:
        auto_dir.mkdir(parents=True, exist_ok=True)
        with open(auto_dir / "notifications.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")
    except OSError as exc:
        out(f"notification file not written: {type(exc).__name__}")
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "PRQA", f"PR #{event.get('pr')} {str(event.get('sha') or '')[:12]}: {event.get('verdict')} — {event.get('report')}"],
                           capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
    out(f"NOTIFY {json.dumps(event, sort_keys=True)}")


# ---------------------------------------------------------------------------------------------- prerequisites --
def discord_url(host) -> str:
    """Host-side only; never logged, returned to callers' output, or passed to the worker/sandbox."""
    if hasattr(host, "discord_url"):
        return host.discord_url()
    return R.env_file_values(host.secrets_file).get("DISCORD_WEBHOOK_URL", "")


def discord_blockers(host, auto_dir: Path) -> list[str]:
    url = discord_url(host)
    if not url:
        return ["DISCORD_WEBHOOK_URL present in the secrets file — run: python3 scripts/setup/prqa_configure_secret.py discord --send-test"]
    if not DD.valid_webhook(url):
        return ["DISCORD_WEBHOOK_URL is a valid https Discord incoming-webhook URL"]
    rec = auto_dir / "discord-setup-test.json"
    ok = rec.is_file() and json.loads(rec.read_text()).get("ok") is True
    return [] if ok else ["Discord setup test succeeded — run: python3 scripts/setup/prqa_configure_secret.py discord --test-only"]


def prerequisite_blockers(host, auto_dir: Path = AUTO_DIR) -> list[str]:
    pf = R.preflight(host)
    return [f"{c['check']}{(' — ' + c['detail']) if c.get('detail') else ''}" for c in pf["checks"] if not c["ok"]] + discord_blockers(host, auto_dir)


GITHUB_TOKEN_CHECKS = ("worker has the read-only GITHUB_TOKEN", "GITHUB_TOKEN present in the secrets file")


def simulation_blockers(host, auto_dir: Path = AUTO_DIR) -> list[str]:
    """Every prerequisite except the GitHub read token, which the simulation replaces by a worker-local mirror."""
    pf = R.preflight(host)
    out = [f"{c['check']}{(' — ' + c['detail']) if c.get('detail') else ''}" for c in pf["checks"]
           if not c["ok"] and not c["check"].startswith(GITHUB_TOKEN_CHECKS)]
    st, cfg = host.worker("GET", "/config")
    remote = str((((cfg or {}) if st == 200 else {}).get("workspaces") or {}).get("checkout_remote") or "")
    if not R.PRIVATE_REPO:
        if remote not in ("", "https://github.com"):
            out.append(f"worker checks out the public {R.REPO} from GitHub (currently {remote!r})")
    elif not remote.startswith(R.SIM_MIRROR_PREFIX):
        out.append(f"worker checkout remote is a local mirror under {R.SIM_MIRROR_PREFIX} (currently {remote!r})")
    return out + discord_blockers(host, auto_dir)


def deliver_discord(host, run_dir: Path, entry: dict[str, Any], state: dict[str, Any], out: Out) -> None:
    """Trusted host-side delivery after sanitization; once per (run key, job). Never changes the QA verdict."""
    try:
        pv = json.loads((run_dir / "preview.json").read_text()) if (run_dir / "preview.json").is_file() else \
            {"repository": R.REPO, "pr_number": entry["pr"], "head_sha": entry["sha"]}
        job = json.loads((run_dir / "job.json").read_text()) if (run_dir / "job.json").is_file() else {}
        result = job.get("result") if isinstance(job.get("result"), dict) and entry.get("status") == "executed" else None
        meta = json.loads((run_dir / "metadata.json").read_text()) if (run_dir / "metadata.json").is_file() else {}
        q = meta.get("quality_gate") or {}
        prompt = {"source": pv.get("prompt_source"), "score": q.get("score"), "accepted": q.get("accepted")}
        msgs = DD.build_messages(result=result, pv=pv, entry=entry, prompt=prompt, simulation=bool(entry.get("simulation")))
        # one delivery per claim: a retried / re-run key is a new claim and gets its own message
        did = f"{entry['key']}:{entry.get('claim_id') or '-'}:{entry.get('job_id') or 'blocked'}"
        rec = DD.deliver(url=discord_url(host), delivery_id=did, ledger=state.setdefault("discord", {}), messages=msgs,
                         opener=getattr(host, "discord_opener", None), sleep=getattr(host, "sleep", None))
    except Exception as exc:  # noqa: BLE001 — delivery problems are recorded, never raised into the QA verdict
        rec = {"delivered": False, "status": f"error {type(exc).__name__}"}
    entry["discord"] = {k: rec.get(k) for k in ("delivered", "status", "sent", "messages", "at", "skipped")}
    R._write(run_dir / "discord-delivery.json", entry["discord"]) if run_dir.is_dir() else None
    out(f"discord: {entry['discord']['status']}")


def eligible_prs(host) -> tuple[list[int], dict[str, Any]]:
    disc = R.discover(host, None)
    agreed = [n for n in disc["eligible"] if n in disc["poller_candidates"]]
    return sorted(agreed), disc


# ------------------------------------------------------------------------------------------------- reporting --
def write_reports(run_dir: Path, *, entry: dict[str, Any], out: Out) -> dict[str, Any]:
    pv = json.loads((run_dir / "preview.json").read_text())
    meta = json.loads((run_dir / "metadata.json").read_text()) if (run_dir / "metadata.json").is_file() else {}
    quality = meta.get("quality_gate") or {}
    job = json.loads((run_dir / "job.json").read_text()) if (run_dir / "job.json").is_file() else {}
    execution = json.loads((run_dir / "execution.json").read_text()) if (run_dir / "execution.json").is_file() else {}
    result = job.get("result") if isinstance(job.get("result"), dict) else None
    prompt = (run_dir / "qa-prompt.md").read_text(encoding="utf-8") if (run_dir / "qa-prompt.md").is_file() else ""
    planned = [b["id"] for b in parse_scenarios(prompt)] if prompt else []
    checklist = "# Planned test cases\n\n" + "\n".join(f"- [ ] {i}" for i in planned) + "\n"
    if result:
        status = {t.get("id"): t.get("status") for t in result.get("test_cases") or []}
        checklist = "# Planned test cases\n\n" + "\n".join(f"- [{'x' if status.get(i) else ' '}] {i} — {status.get(i) or 'not reported'}" for i in planned) + "\n"
        R._write(run_dir / "qa-result.json", result)
    R._write(run_dir / "test-checklist.md", checklist)
    executed = bool(result) and bool(execution.get("opencode_executed")) and (result.get("executed_by") or {}).get("engine") in ("opencode", "claude_code") \
        and bool(result.get("test_cases"))                        # the no-result fallback (zero test cases) is not an executed run
    s = (result or {}).get("summary") or {}
    sim = bool(entry.get("simulation"))
    lines = [f"# PRQA {'SIMULATION' if sim else 'automatic'} run — {R.REPO} PR #{pv['pr_number']}", "",
             *([f"> 🧪 **SIMULATION** — {simulation_note()}", ""] if sim else []),
             f"- Title: {pv.get('pr_title')}", f"- URL: {pv.get('pr_url')}", f"- Base: {pv.get('base_ref')} · tested SHA `{pv['head_sha']}`",
             f"- Run key: `{entry.get('key')}`", f"- Prompt: {pv.get('prompt_source')} · sha256 `{pv.get('prompt_sha256')}` · gate "
             f"{'ACCEPTED' if quality.get('accepted') else 'REJECTED'} {quality.get('score')} (threshold {quality.get('threshold')}; "
             f"final text {((quality.get('final_text') or {}).get('score'))}) · hard fails {quality.get('hard_fails')}",
             f"- Authorization: automatic single-use grant `{entry.get('grant_id') or '—'}` (purpose {run_grants.AUTO_PURPOSE}); audit: worker table run_grant_audit",
             f"- Job: `{execution.get('job_id') or '—'}` · status {execution.get('status') or entry.get('status')} · launch {execution.get('launch_state')}",
             f"- Agent executed: **{'yes' if executed else 'no'}**" + (f" (engine {(result.get('executed_by') or {}).get('engine')}, mode {(result.get('executed_by') or {}).get('mode')})" if result else ""),
             f"- Budget ledger: before {execution.get('ledger_before')} · after {execution.get('ledger_after')}",
             f"- Counts: planned {len(planned)} · total {s.get('total')} · passed {s.get('passed')} · failed {s.get('failed')} · blocked {s.get('blocked')} · N/A {s.get('not_applicable')}",
             f"- Reason: {entry.get('reason')}" if entry.get('reason') else "", ""]
    if result:
        lines += ["---", "", report_mod.render_markdown(result, job)]
    else:
        lines += ["No QA result: the agent did not run or produced no valid result. Nothing below is a test outcome.", ""]
    R._write(run_dir / "report.md", "\n".join(l for l in lines if l is not None) + "\n")
    out(f"report: {R.rel(run_dir / 'report.md')}")
    verdict = ((result or {}).get("summary") or {}).get("overall_status")
    if not verdict:
        verdict = f"NO_RESULT (job {execution.get('status') or job.get('status') or 'not created'})" if execution or job else str(entry.get("status") or "").upper()
    return {"executed": executed, "verdict": verdict}


# -------------------------------------------------------------------------------------------------- pipeline --
def _blocked_dir_report(run_dir: Path, entry: dict[str, Any], out: Out) -> None:
    if (run_dir / "preview.json").is_file():
        try:
            write_reports(run_dir, entry=entry, out=out)
        except (OSError, ValueError, KeyError) as exc:
            out(f"report not written: {type(exc).__name__}")


def _cleanup(host, run_dir: Path, entry: dict[str, Any], out: Out) -> None:
    if not (run_dir / "preview.json").is_file():
        entry["cleanup"] = "nothing created"
        return
    try:
        rc = R.cleanup(host, run_dir=run_dir, out=out)
        entry["cleanup"] = "ok" if rc == 0 else f"FAILED rc={rc} (state kept; rerun `prqa_run_single_pr.py cleanup --run-dir {R.rel(run_dir)}`)"
    except R.Blocked as b:
        entry["cleanup"] = f"FAILED {b.code}: {b.detail}"


def finish_entry(host, entry: dict[str, Any], *, state: dict[str, Any], state_dir: Path, auto_dir: Path, out: Out, status: str,
                 reason: str | None = None) -> dict[str, Any]:
    """Terminal step of one claim: report (blocked runs), cleanup, Discord once, local notification."""
    run_dir = ROOT / entry["run_dir"]
    entry.update(status=status, finished_at=R.now_iso(), **({"reason": reason} if reason else {}))
    _blocked_dir_report(run_dir, entry, out) if status != "executed" else None
    _cleanup(host, run_dir, entry, out)
    save_state(state_dir, state)
    deliver_discord(host, run_dir, entry, state, out)
    save_state(state_dir, state)
    notify({"pr": entry["pr"], "sha": entry["sha"], "verdict": entry.get("verdict") or status.upper(), "reason": reason,
            "simulation": bool(entry.get("simulation")), "report": R.rel(run_dir / "report.md"), "cleanup": entry.get("cleanup"),
            "discord": entry["discord"]["status"]}, auto_dir=auto_dir, out=out)
    return entry


def process_pr(host, number: int, *, state: dict[str, Any], state_dir: Path, auto_dir: Path, wait_s: int, out: Out,
               simulation: bool = False) -> dict[str, Any]:
    pr = host.gh_get(f"/repos/{R.REPO}/pulls/{number}")
    reasons = R.eligibility(pr, require_label=not simulation)
    if reasons:
        return {"skipped": number, "reason": "; ".join(reasons)}
    sha = pr["head"]["sha"]
    key = run_key(number, sha, simulation)
    if key in state["keys"]:
        return {"skipped": number, "reason": f"already processed ({state['keys'][key]['status']})"}
    run_dir = R.RUNS_DIR / f"{R.SLUG}-pr-{number}-{sha}{'-sim' if simulation else ''}"
    entry: dict[str, Any] = {"key": key, "pr": number, "sha": sha, "status": "claimed", "claimed_at": R.now_iso(), "claim_id": os.urandom(4).hex(),
                             "run_dir": R.rel(run_dir),
                             **({"simulation": True} if simulation else {})}
    state["keys"][key] = entry
    save_state(state_dir, state)                                   # claimed BEFORE any side effect: never twice

    def finish(status: str, reason: str | None = None) -> dict[str, Any]:
        return finish_entry(host, entry, state=state, state_dir=state_dir, auto_dir=auto_dir, out=out, status=status, reason=reason)

    try:
        rc = R.preview(host, pinned=number, with_env=True, out=out, simulation=simulation)
    except R.Blocked as b:
        return finish("blocked", f"{b.code}: {b.detail}")
    pv = json.loads((run_dir / "preview.json").read_text())
    if pv["head_sha"] != sha:
        return finish("blocked", "HEAD_MOVED_DURING_PREVIEW")
    if rc != 0 or not pv.get("quality_accepted"):
        q = json.loads((run_dir / "quality.json").read_text()) if (run_dir / "quality.json").is_file() else {}
        return finish("blocked", f"QUALITY_GATE_REJECTED: score {q.get('score')}, failure {q.get('failure')}, hard fails {q.get('hard_fails')}, "
                                 f"gate errors {q.get('gate_errors')}, final-text {(q.get('final_text') or {}).get('failure')}")
    # the gate accepted the exact prompt: ONE automatic grant bound to repo/PR/SHA/level/prompt hash
    try:
        token, claims = run_grants.mint(host.grant_key(), repository=R.REPO, pr_number=number, head_sha=sha, now=int(host.clock()),
                                        ttl_s=GRANT_TTL_S, test_level=R.LEVEL, prompt_sha256=pv["prompt_sha256"], purpose=run_grants.AUTO_PURPOSE)
    except run_grants.GrantError as exc:
        return finish("blocked", f"{exc.code}: {exc.reason}")
    grant_dir = Path(pv["private_dir"])
    grant_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    grant_file = grant_dir / f"grant-{claims['grant_id']}.token"
    fd = os.open(grant_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    del token
    entry.update(status="submitting", grant_id=claims["grant_id"], grant_expires_at=claims["expires_at"])
    save_state(state_dir, state)
    try:
        erc = R.execute(host, run_dir=run_dir, confirm_repo=R.REPO, confirm_pr=number, confirm_sha=sha, grant_file=grant_file,
                        approved=True, wait_s=wait_s, out=out)
    except R.Blocked as b:
        return finish("blocked", f"{b.code}: {b.detail}")
    finally:
        grant_file.unlink(missing_ok=True)
    pv = json.loads((run_dir / "preview.json").read_text())
    entry.update(job_id=pv.get("job_id"), job_status=pv.get("job_status"))
    if pv.get("status") != "executed":
        entry["status"] = "submitted"                              # job still running: recovery resumes it
        save_state(state_dir, state)
        return entry
    info = write_reports(run_dir, entry=entry, out=out)
    entry["verdict"] = info["verdict"]
    entry["opencode_executed"] = info["executed"]
    job_error = ""
    if erc != 0 and (run_dir / "job.json").is_file():
        job_error = str((json.loads((run_dir / "job.json").read_text()).get("error") or {}).get("message") or "")[:300]
    return finish("executed", None if erc == 0 else f"job finished with status {pv.get('job_status')}" + (f": {job_error}" if job_error else ""))


def recover(host, *, state: dict[str, Any], state_dir: Path, auto_dir: Path, wait_s: int, out: Out) -> list[str]:
    """Resume in-flight keys after a crash/restart without ever starting a second pipeline for them."""
    done = []
    for key, entry in list(state["keys"].items()):
        if entry.get("status") not in IN_FLIGHT:
            continue
        run_dir = ROOT / entry["run_dir"]
        pv = json.loads((run_dir / "preview.json").read_text()) if (run_dir / "preview.json").is_file() else {}
        job_id = pv.get("job_id") or entry.get("job_id")
        if not job_id:
            st, data = host.worker("GET", "/jobs?limit=200")
            dedupe = f"{R.REPO}#{entry['pr']}@{entry['sha']}:{R.LEVEL}"
            hit = [j for j in (data or {}).get("jobs", []) if j.get("dedupe_key") == dedupe] if st == 200 else []
            job_id = hit[0]["id"] if hit else None
        if not job_id:
            entry.update(status="interrupted", finished_at=R.now_iso(), reason="runner stopped before a job existed; not re-run automatically")
            _cleanup(host, run_dir, entry, out)
            save_state(state_dir, state)
            notify({"pr": entry["pr"], "sha": entry["sha"], "verdict": "INTERRUPTED", "report": entry["run_dir"], "cleanup": entry.get("cleanup")},
                   auto_dir=auto_dir, out=out)
            done.append(key)
            continue
        deadline = host.clock() + wait_s
        job: dict[str, Any] = {}
        while host.clock() < deadline:
            st, job = host.worker("GET", f"/jobs/{job_id}")
            job = (job or {}).get("job", job) if isinstance(job, dict) else {}
            if job.get("status") in R.TERMINAL:
                break
            host.sleep(15)
        if job.get("status") not in R.TERMINAL:
            entry.update(status="submitted", job_id=job_id)
            save_state(state_dir, state)
            continue
        R._write(run_dir / "job.json", job)
        ex = json.loads((run_dir / "execution.json").read_text()) if (run_dir / "execution.json").is_file() else {}
        ex.update(job_id=job_id, status=job.get("status"), launch_state=job.get("launch_state"), ledger_after=host.ledger(), recovered=True,
                  opencode_executed=bool(job.get("result")) and job.get("status") == "succeeded")
        R._write(run_dir / "execution.json", ex)
        info = write_reports(run_dir, entry=entry, out=out)
        entry.update(status="executed", job_id=job_id, verdict=info["verdict"], opencode_executed=info["executed"], finished_at=R.now_iso(), recovered=True)
        _cleanup(host, run_dir, entry, out)
        save_state(state_dir, state)
        deliver_discord(host, run_dir, entry, state, out)
        save_state(state_dir, state)
        notify({"pr": entry["pr"], "sha": entry["sha"], "verdict": info["verdict"], "report": R.rel(run_dir / "report.md"), "cleanup": entry.get("cleanup")},
               auto_dir=auto_dir, out=out)
        done.append(key)
    return done


def once(host, *, state_dir: Path = STATE_DIR, auto_dir: Path = AUTO_DIR, wait_s: int = 3600, out: Out = print) -> dict[str, Any]:
    with locked(state_dir):
        state = load_state(state_dir)
        summary: dict[str, Any] = {"at": R.now_iso(), "policy_version": POLICY_VERSION}
        summary["recovered"] = recover(host, state=state, state_dir=state_dir, auto_dir=auto_dir, wait_s=wait_s, out=out)
        eligible, disc = eligible_prs(host)
        summary["eligible"] = eligible
        heads = {t["number"]: t["head_sha"] for t in disc["open_prs"]}
        pending = [n for n in eligible if run_key(n, heads[n]) not in state["keys"]]
        summary["pending"] = pending
        if not pending:
            summary["result"] = "idle: no eligible, unprocessed PR — nothing triggered"
        else:
            blockers = prerequisite_blockers(host, auto_dir)
            if blockers:
                summary.update(result="waiting: execution prerequisites missing — no PR claimed, no environment started", blockers=blockers)
            else:
                summary["processed"] = process_pr(host, pending[0], state=state, state_dir=state_dir, auto_dir=auto_dir, wait_s=wait_s, out=out)
                summary["result"] = "processed one PR (concurrency 1)"
        R._write(auto_dir / "last-pass.json", summary)
        out(json.dumps({k: v for k, v in summary.items() if k != "processed"}, indent=1))
        return summary


def simulation_note() -> str:
    return SIMULATION_NOTE if R.PRIVATE_REPO else SIMULATION_NOTE_PUBLIC


SIMULATION_NOTE = ("manually triggered on an explicit PR without the `to be test` label; the worker checked out the exact "
                   "head SHA from a local mirror instead of GitHub (no GitHub token). Everything else is the real pipeline: "
                   "disposable API, quality gate, single-use grant, launcher, OpenCode sandbox, model proxy and budget ledger.")
SIMULATION_NOTE_PUBLIC = ("manually triggered on an explicit PR without the `to be test` label (labels are never changed); the "
                          "public repository was checked out anonymously from GitHub at the exact head SHA. Everything else is the "
                          "real pipeline: disposable API, quality gate, single-use grant, launcher, OpenCode sandbox, model proxy "
                          "and budget ledger.")


def archive_simulation(state: dict[str, Any], number: int) -> list[str]:
    """--rerun: move this PR's finished simulation keys to `retried` and rename their run directories (kept, not deleted).
    Only simulation keys; a real run's key is never touched."""
    moved = []
    for key, e in list(state["keys"].items()):
        if e.get("simulation") and e.get("pr") == number and e.get("status") not in IN_FLIGHT:
            old = ROOT / e["run_dir"]
            if old.is_dir():
                new = old.with_name(f"{old.name}-{time.strftime('%Y%m%dT%H%M%S')}")
                old.rename(new)
                e["archived_run_dir"] = R.rel(new)
            state.setdefault("retried", []).append(dict(e, retried_at=R.now_iso()))
            del state["keys"][key]
            moved.append(key)
    return moved


def simulate(host, number: int, *, state_dir: Path = STATE_DIR, auto_dir: Path = AUTO_DIR, wait_s: int = 3600, out: Out = print,
             rerun: bool = False) -> dict[str, Any]:
    with locked(state_dir):
        state = load_state(state_dir)
        summary: dict[str, Any] = {"at": R.now_iso(), "policy_version": POLICY_VERSION, "simulation": True, "pr": number}
        # finish whatever an interrupted runner left in flight (report, Discord, cleanup) before deciding anything
        summary["recovered"] = recover(host, state=state, state_dir=state_dir, auto_dir=auto_dir, wait_s=wait_s, out=out)
        if rerun:
            summary["archived"] = archive_simulation(state, number)
            save_state(state_dir, state)
        blockers = simulation_blockers(host, auto_dir)
        if blockers:
            summary.update(result="simulation not started: prerequisites missing", blockers=blockers)
        else:
            summary["processed"] = process_pr(host, number, state=state, state_dir=state_dir, auto_dir=auto_dir, wait_s=wait_s,
                                              out=out, simulation=True)
            summary["result"] = "simulation processed"
        R._write(auto_dir / "last-simulation.json", summary)
        out(json.dumps(summary, indent=1, default=str))
        return summary


# -------------------------------------------------------------------------------------------------- activation --
def verify_github_read_token(token: str) -> dict[str, Any]:
    """Safe, non-mutating reads with the dedicated token (header only; never argv/logs). A classic token (it reports
    OAuth scopes) is refused: only a fine-grained, repository-scoped token is acceptable."""
    res: dict[str, Any] = {}
    for name, path in (("repository", f"/repos/{R.REPO}"), ("pulls", f"/repos/{R.REPO}/pulls?per_page=1"),
                       ("contents", f"/repos/{R.REPO}/commits?per_page=1")):
        req = urllib.request.Request("https://api.github.com" + path, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                                                               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "prqa-auto"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                res[name] = r.status
                if r.headers.get("X-OAuth-Scopes"):
                    res["classic_token"] = True
        except urllib.error.HTTPError as e:
            res[name] = e.code
        except (urllib.error.URLError, OSError) as e:
            res[name] = type(e).__name__
    res["ok"] = all(res.get(k) == 200 for k in ("repository", "pulls", "contents")) and not res.get("classic_token")
    return res


def activate(host, *, apply: bool, out: Out = print) -> int:
    """Scoped activation of the selected project. Changes (each printed, applied only with --apply; for a private repo only
    when GITHUB_TOKEN verifies):
    1. PRQA_RUN_GRANT_KEY generated into the secrets file if missing (scripts/pipeline/prqa_authorize_run.py --init-key);
    2. config/repositories.yaml: the project's api.base_url → the disposable alias (entry stays disabled / pending_approval);
    3. prqa-worker recreated to load the key (and GITHUB_TOKEN). Nothing global changes: review_only, REAL_* false,
       n8n workflows inactive; only grant-bound jobs of this project can run."""
    vals = R.env_file_values(host.secrets_file)
    plan = []
    if R.PRIVATE_REPO and not vals.get("GITHUB_TOKEN"):
        out(json.dumps({"activated": False, "blocker": "GITHUB_TOKEN_MISSING",
                        "detail": f"add a fine-grained token (only {R.REPO}; Contents: Read-only, Pull requests: Read-only, Metadata: Read-only) "
                                  "with masked input: python3 scripts/setup/prqa_configure_secret.py github — then run activate again"}, indent=1))
        return 2
    dblk = discord_blockers(host, AUTO_DIR)
    if dblk:
        out(json.dumps({"activated": False, "blocker": "DISCORD_NOT_READY", "detail": dblk}, indent=1))
        return 2
    check = verify_github_read_token(vals["GITHUB_TOKEN"]) if R.PRIVATE_REPO else {"ok": True, "public_repository": True}
    if not check["ok"]:
        out(json.dumps({"activated": False, "blocker": "GITHUB_TOKEN_NOT_VERIFIED", "reads": check}, indent=1))
        return 2
    if not run_grants.KEY_RE.match(vals.get("PRQA_RUN_GRANT_KEY", "")):
        plan.append("init grant key (secrets file, mode 600, never printed)")
    entry = host.registry_entry() or {}
    if entry.get("base_url") != R.EXPECTED_BASE_URL:
        plan.append(f"registry: {R.REPO} api.base_url {entry.get('base_url')!r} → {R.EXPECTED_BASE_URL!r} (stays disabled / pending_approval)")
    plan.append("recreate prqa-worker (loads GITHUB_TOKEN and the grant key; settings otherwise unchanged)")
    out(json.dumps({"github_token_reads": check, "planned_changes": plan, "apply": apply}, indent=1))
    if not apply:
        return 0
    if any(p.startswith("init grant key") for p in plan):
        subprocess.run([sys.executable, str(ROOT / "scripts" / "pipeline" / "prqa_authorize_run.py"), "--init-key"], check=True, capture_output=True)
    if any(p.startswith("registry:") for p in plan):
        reg = ROOT / "config" / "repositories.yaml"
        text = reg.read_text(encoding="utf-8")
        start = text.index(f"- full_name: {R.REPO}")
        end = text.find("\n  - full_name:", start + 1)
        end = len(text) if end < 0 else end
        block = text[start:end]
        new = block.replace('base_url: "https://replace-me.staging.example.test"', f'base_url: "{R.EXPECTED_BASE_URL}"', 1)
        if new == block:
            raise SystemExit(f"registry: {R.REPO} base_url line not found in the expected form; not changed")
        shutil.copy2(reg, reg.with_suffix(f".yaml.bak-{time.strftime('%Y%m%d%H%M%S')}"))
        reg.write_text(text[:start] + new + text[end:], encoding="utf-8")
    compose = ["docker", "compose", "-f", str(ROOT / "compose/docker-compose.yml"), "--env-file", str(ROOT / "compose/.env"),
               "--env-file", str(host.secrets_file), "--profile", "phase2", "up", "-d", "--no-deps", "--force-recreate", "prqa-worker"]
    p = subprocess.run(compose, capture_output=True, text=True, timeout=300)
    out(json.dumps({"activated": p.returncode == 0, "worker_recreate_rc": p.returncode, "remaining_blockers_after": None}, indent=1))
    return 0 if p.returncode == 0 else 1


def status(host, *, state_dir: Path = STATE_DIR, out: Out = print) -> dict[str, Any]:
    st = load_state(state_dir)
    eligible, disc = eligible_prs(host)
    res = {"policy_version": POLICY_VERSION, "prerequisite_blockers": prerequisite_blockers(host), "eligible": eligible,
           "discord_deliveries": {k: v.get("status") for k, v in st.get("discord", {}).items()},
           "open_prs": [{k: t[k] for k in ("number", "base", "draft", "labels", "reasons")} for t in disc["open_prs"]],
           "keys": {k: {f: v.get(f) for f in ("status", "verdict", "reason", "cleanup", "job_id")} for k, v in st["keys"].items()}}
    out(json.dumps(res, indent=1))
    return res


def main(argv: list[str] | None = None, host=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=os.environ.get("PRQA_TARGET_REPO") or R.DEFAULT_REPO, choices=sorted(R.PROJECTS))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    o = sub.add_parser("once")
    o.add_argument("--wait-s", type=int, default=3600)
    s = sub.add_parser("serve")
    s.add_argument("--interval", type=int, default=60)
    s.add_argument("--wait-s", type=int, default=3600)
    a = sub.add_parser("activate")
    a.add_argument("--apply", action="store_true")
    r = sub.add_parser("retry")
    r.add_argument("--key", required=True)
    m = sub.add_parser("simulate")
    m.add_argument("--pr", type=int, required=True)
    m.add_argument("--wait-s", type=int, default=3600)
    m.add_argument("--rerun", action="store_true", help="archive this PR's finished simulation (run dir kept, renamed) and run again")
    args = ap.parse_args(argv)
    R.select_project(args.repo)
    host = host or R.Host()
    if args.cmd == "status":
        try:
            status(host)
        except RuntimeError as exc:  # GitHubError (GitHub unreachable/slow): report it as a blocker, keep stdout valid JSON
            print(json.dumps({"policy_version": POLICY_VERSION, "error": type(exc).__name__,
                              "prerequisite_blockers": [f"GitHub check failed: {str(exc)[:300]}"],
                              "eligible": [], "open_prs": []}, indent=1))
            return 1
        return 0
    if args.cmd == "simulate":
        try:
            res = simulate(host, args.pr, wait_s=args.wait_s, rerun=args.rerun)
        except Busy as b:
            print(json.dumps({"busy": str(b)}))
            return 3
        return 0 if (res.get("processed") or {}).get("status") == "executed" else 2
    if args.cmd == "activate":
        return activate(host, apply=args.apply)
    if args.cmd == "retry":
        with locked(STATE_DIR):
            st = load_state(STATE_DIR)
            e = st["keys"].get(args.key)
            if not e or e.get("status") not in ("blocked", "interrupted"):
                print(json.dumps({"retry": False, "reason": "key unknown or not blocked/interrupted"}))
                return 2
            st.setdefault("retried", []).append(dict(e, retried_at=R.now_iso()))
            del st["keys"][args.key]
            save_state(STATE_DIR, st)
        print(json.dumps({"retry": True, "key": args.key}))
        return 0
    if args.cmd == "once":
        try:
            once(host, wait_s=args.wait_s)
        except Busy as b:
            print(json.dumps({"busy": str(b)}))
            return 3
        return 0
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    while not stop["flag"]:
        try:
            once(host, wait_s=args.wait_s)
        except Busy as b:
            print(json.dumps({"busy": str(b)}))
        except Exception as exc:  # noqa: BLE001 — keep the service alive; the pass is recorded, nothing is retried blindly
            print(json.dumps({"pass_error": type(exc).__name__, "detail": str(exc)[:300]}))
        for _ in range(max(30, args.interval)):
            if stop["flag"]:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
