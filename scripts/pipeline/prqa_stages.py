#!/usr/bin/env python3
"""The label-triggered PRQA pipeline as separate stages, so n8n (workflow `PRQA 09 · Label-Triggered Pipeline`) owns the
orchestration: which stage runs next, the wait/poll loop, and the blocked / finished branches.

    discover  → open PRs labelled `to be test` not yet processed for (PR, SHA, policy); in-flight and stale claims
    claim     {pr}  → re-checks eligibility and claims the run key (never twice for the same PR head SHA)
    prepare   {key} → disposable env at the head SHA + OpenAPI + identity probe + context → the first Groq request of the
              staged generator (worker/prqa_worker/prompt_stages.py: scope → coverage → sections, each <= the per-stage
              token target, released only when the shared ledger has capacity: `wait_s`) — n8n then calls Groq through
              the model proxy with the inference-only prompt credential
    promptstep {key, generation} → validates the stage result (schema + provenance), returns the next stage's request
              (`next: call`, with `wait_s`), a bounded retry, or `next: gate` once the prompt is assembled or generation
              stopped (PRQA_PROMPT_GENERATION=single: the old one-request path, passed straight to the gate)
    gate      {key, generation?} → worker finalizes the assembled staged prompt (or the single-request generation)
              (validator + 85 rubric on the complete prompt, fallback on failure/rejection), final-text gate (85, hard
              fails); generation arrives on stdin (--stdin), never in argv
    submit    {key} → executor `sandbox`: ONE single-use grant bound to the prompt hash → worker job → launcher → OpenCode
              container; executor `local` (PRQA_OPENCODE_EXECUTOR): scripts/pipeline/prqa_local_opencode.py in the background
              (the OpenCode CLI of this machine, free GLM, bubblewrap-isolated; re-verifies + re-gates the prompt itself)
    poll      {key} → job status (n8n waits between polls); times out after the runner's wait limit
    finalize  {key} → report + checklist, cleanup, Discord, notification
    fail      {key, reason} → terminal blocked state with the same cleanup / Discord / notification
    recover        → stale claims (n8n execution lost): resume the job if one exists, else mark interrupted and clean up

Every stage reuses the tested functions of scripts/pipeline/prqa_auto_runner.py and scripts/pipeline/prqa_run_single_pr.py unchanged and
holds the runner's exclusive state lock while it runs, so the CLI runner and n8n can never process the same key twice.
A stage that blocks (gate rejected, env failed, head moved, grant refused) finishes the run itself — cleanup and
Discord never depend on n8n reaching a later node. Logs go to stderr; stdout is exactly one JSON object.

    python3 scripts/pipeline/prqa_stages.py <stage> [--pr N] [--key KEY] [--reason TEXT]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("prqa_auto_runner", ROOT / "scripts" / "pipeline" / "prqa_auto_runner.py")
A = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(A)  # type: ignore[union-attr]
R = A.R
run_grants = A.run_grants
from prqa_worker import prompt_stages as PS  # noqa: E402  (worker/ is on sys.path via prqa_run_single_pr)

WAIT_LIMIT_S = int(os.environ.get("PRQA_STAGE_WAIT_S") or 5400)      # same budget as the CLI runner's --wait-s
STALE_AFTER_S = 45 * 60                                                # no stage touched an in-flight claim for this long
KEY_RE = re.compile(r"^[A-Za-z0-9._/-]+#\d+@[0-9a-f]{40}:[a-z0-9.-]+$")


class StageError(Exception):
    pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _age_s(iso: str | None, now: float) -> float | None:
    """Ages are wall-clock: every stamp they compare against is written by R.now_iso()."""
    if not iso:
        return None
    try:
        return now - datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


class Ctx:
    """One stage = one locked read-modify-write of the runner state."""

    def __init__(self, host, state_dir: Path = A.STATE_DIR, auto_dir: Path = A.AUTO_DIR) -> None:
        self.host, self.state_dir, self.auto_dir = host, state_dir, auto_dir
        self.state: dict[str, Any] = {}

    def entry(self, key: str) -> dict[str, Any]:
        if not KEY_RE.match(key or "") or key.endswith("-sim"):
            raise StageError("invalid run key")
        e = self.state["keys"].get(key)
        if not e:
            raise StageError("unknown run key")
        return e

    def touch(self, entry: dict[str, Any], stage: str) -> None:
        entry.update(stage=stage, stage_at=R.now_iso(), driver="n8n")
        A.save_state(self.state_dir, self.state)

    def finish(self, entry: dict[str, Any], status: str, reason: str | None = None) -> dict[str, Any]:
        A.finish_entry(self.host, entry, state=self.state, state_dir=self.state_dir, auto_dir=self.auto_dir, out=log, status=status, reason=reason)
        return {"finished": True, "status": entry["status"], "verdict": entry.get("verdict") or entry["status"].upper(), "reason": entry.get("reason"),
                "report": R.rel(ROOT / entry["run_dir"] / "report.md"), "cleanup": entry.get("cleanup"), "discord": (entry.get("discord") or {}).get("status")}


def in_flight(state: dict[str, Any], now: float) -> list[dict[str, Any]]:
    out = []
    for k, e in state["keys"].items():
        if e.get("status") in A.IN_FLIGHT and not e.get("simulation"):
            age = _age_s(e.get("stage_at") or e.get("claimed_at"), now)
            out.append({"key": k, "pr": e["pr"], "status": e["status"], "stage": e.get("stage"), "idle_s": int(age or 0),
                        "stale": age is None or age > STALE_AFTER_S})
    return out


# -------------------------------------------------------------------------------------------------------- stages --
def discover(c: Ctx, **_: Any) -> dict[str, Any]:
    flying = in_flight(c.state, time.time())
    eligible, disc = A.eligible_prs(c.host)
    heads = {t["number"]: t["head_sha"] for t in disc["open_prs"]}
    pending = [{"pr": n, "sha": heads[n]} for n in eligible if A.run_key(n, heads[n]) not in c.state["keys"]]
    res: dict[str, Any] = {"eligible": eligible, "pending": pending, "in_flight": flying, "next": None, "blockers": [],
                           "stale": [f["key"] for f in flying if f["stale"]]}
    if flying:
        res["action"] = "recover" if res["stale"] else "wait"          # concurrency 1: never a second pipeline
    elif not pending:
        res["action"] = "idle"
    else:
        res["blockers"] = A.prerequisite_blockers(c.host, c.auto_dir)
        res["action"] = "blocked_prerequisites" if res["blockers"] else "run"
        res["next"] = None if res["blockers"] else pending[0]
    return res


def claim(c: Ctx, *, pr: int | None = None, **_: Any) -> dict[str, Any]:
    if not isinstance(pr, int) or pr <= 0:
        raise StageError("pr must be a positive integer")
    if in_flight(c.state, time.time()):
        return {"claimed": False, "reason": "another run is in flight (concurrency 1)"}
    p = c.host.gh_get(f"/repos/{R.REPO}/pulls/{pr}")
    reasons = R.eligibility(p, require_label=True)
    if reasons:
        return {"claimed": False, "reason": "; ".join(reasons)}
    sha = p["head"]["sha"]
    key = A.run_key(pr, sha)
    if key in c.state["keys"]:
        return {"claimed": False, "reason": f"already processed ({c.state['keys'][key]['status']})"}
    run_dir = R.RUNS_DIR / f"{R.SLUG}-pr-{pr}-{sha}"
    entry = {"key": key, "pr": pr, "sha": sha, "status": "claimed", "claimed_at": R.now_iso(), "claim_id": os.urandom(4).hex(), "run_dir": R.rel(run_dir)}
    c.state["keys"][key] = entry
    c.touch(entry, "claim")                                            # claimed BEFORE any side effect
    return {"claimed": True, "key": key, "pr": pr, "sha": sha, "title": str(p.get("title") or "")[:200], "url": p.get("html_url"),
            "run_dir": entry["run_dir"]}


def prepare(c: Ctx, *, key: str = "", **_: Any) -> dict[str, Any]:
    """Disposable env at the head SHA + OpenAPI + identity probe + context; returns the chat-completion request that n8n
    sends to Groq through the model proxy (n8n holds the inference-only prompt credential; the host never sees it)."""
    e = c.entry(key)
    if e["status"] != "claimed":
        raise StageError(f"prepare needs a claimed key (status {e['status']})")
    c.touch(e, "prepare")
    run_dir = ROOT / e["run_dir"]
    try:
        R.preview(c.host, pinned=e["pr"], with_env=True, out=log, context_only=True)
    except R.Blocked as b:
        return {"ok": False, **c.finish(e, "blocked", f"{b.code}: {b.detail}")}
    pv = json.loads((run_dir / "preview.json").read_text())
    if pv["head_sha"] != e["sha"]:
        return {"ok": False, **c.finish(e, "blocked", "HEAD_MOVED_DURING_PREVIEW")}
    if pv.get("status") != "context_ready":
        return {"ok": False, **c.finish(e, "blocked", f"PREVIEW_STOPPED: status {pv.get('status')}")}
    context = json.loads((run_dir / "context.json").read_text())
    if generation_mode() == "staged":
        try:
            act = _pipeline(e, run_dir, context).start()
        except (PS.StageConfigError, OSError, ValueError, RuntimeError) as exc:
            log(f"staged prompt generation unavailable ({type(exc).__name__}: {str(exc)[:200]}); reference fallback")
            return {"ok": True, "llm_request": None, "prompt_job_id": None, "next": "gate", "staged": {"action": "unavailable"}}
        return {"ok": True, **_staged_out(act, e), "test_identity_ready": bool((pv.get("test_identity") or {}).get("ready"))}
    st, built = c.host.worker("POST", "/prompts/build", {"repository": R.REPO, "pr_number": e["pr"], "test_level": R.LEVEL,
                                                         "synthetic": False, "context": context})
    if st != 200 or not isinstance((built or {}).get("llm_request"), dict):
        return {"ok": True, "llm_request": None, "prompt_job_id": None}   # n8n skips the model call → reference fallback
    request = dict(built["llm_request"], model=prompt_model())       # the worker renders the request; the proxy pins the model
    return {"ok": True, "llm_request": request, "prompt_job_id": re.sub(r"[^A-Za-z0-9_.-]", "-", f"{R.REPO}-{e['pr']}")[:100],
            "test_identity_ready": bool((pv.get("test_identity") or {}).get("ready"))}


def prompt_model() -> str:
    return R.env_file_values(ROOT / "compose" / ".env").get("PROMPT_LLM_MODEL") or "openai/gpt-oss-120b"


def _compose_env() -> dict[str, str]:
    """Non-secret settings: compose/.env, overridden by this process's environment for the PRQA_PROMPT_* keys."""
    env = R.env_file_values(ROOT / "compose" / ".env")
    env.update({k: v for k, v in os.environ.items() if k.startswith(("PRQA_PROMPT_", "PRQA_BUDGET_"))})
    return env


def generation_mode() -> str:
    """staged (default): the three-/two-stage generator; single: the previous one-request path."""
    return "single" if (_compose_env().get("PRQA_PROMPT_GENERATION") or "staged").strip() == "single" else "staged"


def _budget_dir() -> str | None:
    """The model proxy's shared ledger directory on this host (PRQA_PROVIDER_BUDGET_DIR; read from the secrets file
    without loading any other value from it)."""
    if "PRQA_PROVIDER_BUDGET_DIR" in os.environ:                     # set (even empty: no ledger) → the file is not read
        return os.environ["PRQA_PROVIDER_BUDGET_DIR"] or None
    sf = Path.home() / ".config" / "prqa" / "secrets.env"
    try:
        for line in sf.read_text(encoding="utf-8").splitlines():
            if line.startswith("PRQA_PROVIDER_BUDGET_DIR="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


def _capacity(job_id: str):
    """Read-only check of the proxy's shared ledger (Ledger.capacity: rolling window, provider-reported remaining
    tokens/reset, daily and per-job limits). None when no ledger is configured: the proxy alone then decides."""
    d = _budget_dir()
    if not d:
        return None
    if str(ROOT / "proxy") not in sys.path:
        sys.path.insert(0, str(ROOT / "proxy"))
    import provider_budget  # noqa: E402
    ledger = provider_budget.Ledger(provider_budget.BudgetConfig.from_env(_compose_env(), directory=d))
    return lambda tokens: ledger.capacity(job_id=job_id, tokens=tokens)


def _prompt_job_ref(e: dict[str, Any]) -> str:
    """X-PRQA-Job-Id of the prompt requests: the proxy's ledger job is prompt-<this>, one per PR head."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", f"{R.REPO}-{e['pr']}-{e['sha'][:12]}")[:100]


def _pipeline(e: dict[str, Any], run_dir: Path, context: dict[str, Any] | None = None) -> PS.Pipeline:
    env = _compose_env()
    cfg = PS.StageConfig.from_env(env)
    tpm = env.get("PRQA_BUDGET_TOKENS_PER_MINUTE", "")
    cfg.check(int(tpm) if tpm.isdigit() else None)
    if context is None:
        context = json.loads((run_dir / "context.json").read_text())
    pv = json.loads((run_dir / "preview.json").read_text())
    spec = run_dir / "env" / "openapi.json"
    openapi = json.loads(spec.read_text()) if spec.is_file() and pv.get("openapi_present") and pv.get("head_sha") == e["sha"] else None
    base = env.get("PROMPT_LLM_API_BASE") or ""
    return PS.Pipeline(run_dir, prompts_dir=ROOT / "prompts", context=context, openapi=openapi, repository=R.REPO,
                       pr_number=e["pr"], head_sha=e["sha"], test_level=R.LEVEL, model=prompt_model(),
                       provider="groq" if "groq.com" in base else "other", cfg=cfg,
                       capacity=_capacity("prompt-" + _prompt_job_ref(e)))


def _staged_out(act: dict[str, Any], e: dict[str, Any]) -> dict[str, Any]:
    if act.get("action") == "call":
        return {"llm_request": act["llm_request"], "prompt_job_id": _prompt_job_ref(e), "next": "call",
                "wait_s": max(1, int(-(-float(act.get("wait_s") or 0) // 1))), "prompt_stage": act["stage"],
                "attempt": act["attempt"], "estimate": act.get("estimate"), **({"retry_of": act["retry_of"]} if act.get("retry_of") else {})}
    return {"llm_request": None, "prompt_job_id": None, "next": "gate",
            "staged": {"action": act.get("action"), "reason": act.get("reason"), "sha256": act.get("sha256")}}


def promptstep(c: Ctx, *, key: str = "", generation: dict[str, Any] | None = None, usage: dict[str, Any] | None = None,
               **_: Any) -> dict[str, Any]:
    """One Groq result of the staged generator in, the next request (or `next: gate`) out. Never runs OpenCode, never
    creates a job: generation only."""
    e = c.entry(key)
    if e["status"] != "claimed":
        raise StageError(f"promptstep needs a claimed key (status {e['status']})")
    run_dir = ROOT / e["run_dir"]
    pv = json.loads((run_dir / "preview.json").read_text()) if (run_dir / "preview.json").is_file() else {}
    if pv.get("status") != "context_ready":
        raise StageError("promptstep needs a prepared context (run prepare first)")
    c.touch(e, "promptstep")
    if generation_mode() == "single":
        return {"ok": True, "next": "gate", "generation": generation}
    try:
        act = _pipeline(e, run_dir).record(generation, usage)
    except (PS.StageConfigError, OSError, ValueError, RuntimeError) as exc:
        log(f"staged prompt generation stopped ({type(exc).__name__}: {str(exc)[:200]})")
        return {"ok": True, "llm_request": None, "next": "gate", "staged": {"action": "unavailable"}}
    return {"ok": True, **_staged_out(act, e)}


def gate(c: Ctx, *, key: str = "", generation: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    """Finalize Groq's prompt in the worker (validator + 85-point rubric; a failed/rejected generation → reference
    fallback), re-gate the complete final text, and stop the run here unless it is accepted."""
    e = c.entry(key)
    if e["status"] != "claimed":
        raise StageError(f"gate needs a claimed key (status {e['status']})")
    run_dir = ROOT / e["run_dir"]
    pv = json.loads((run_dir / "preview.json").read_text()) if (run_dir / "preview.json").is_file() else {}
    if pv.get("status") != "context_ready":
        raise StageError("gate needs a prepared context (run prepare first)")
    c.touch(e, "gate")
    staged = None
    if generation is None and generation_mode() == "staged" and (run_dir / PS.METADATA).is_file():
        try:
            pipe = _pipeline(e, run_dir)
            staged = pipe.meta()
            if staged.get("head_sha") != e["sha"]:
                staged = None                                          # another head's generation is never used
            elif (text := pipe.assembled_text()) is not None:
                generation = {"generated_text": text, "finish_reason": "stop"}
            else:
                if staged.get("status") == "running":                  # n8n left the loop early: stop, never resume silently
                    staged.update(status="failed", failure="generation interrupted before assembly")
                    pipe._save_meta(staged)
                generation = {"llm_error": "staged prompt generation failed", "http_status": staged.get("failure_http_status")}
        except (PS.StageConfigError, OSError, ValueError, RuntimeError) as exc:
            log(f"staged metadata unreadable ({type(exc).__name__}); reference fallback")
            staged, generation = None, None
    try:
        rc = R.preview_finish(c.host, run_dir=run_dir, generation=generation, out=log, staged=staged)
    except R.Blocked as b:
        return {"ok": False, **c.finish(e, "blocked", f"{b.code}: {b.detail}")}
    pv = json.loads((run_dir / "preview.json").read_text())
    q = json.loads((run_dir / "quality.json").read_text()) if (run_dir / "quality.json").is_file() else {}
    g = pv.get("prompt_generation") or {}
    result = {"score": q.get("score"), "threshold": q.get("threshold"), "hard_fails": q.get("hard_fails"), "prompt_source": pv.get("prompt_source"),
              "prompt_sha256": pv.get("prompt_sha256"), "groq_called": g.get("model_called"), "groq_outcome": g.get("outcome"),
              "groq_failure": g.get("failure"), "groq_worker_gate": g.get("worker_gate"),
              **({"staged": {"mode": staged.get("mode"), "status": staged.get("status"), "failure": staged.get("failure"),
                             "usage": {s: v.get("usage") for s, v in (staged.get("stages") or {}).items()}}} if staged else {})}
    if rc != 0 or not pv.get("quality_accepted"):
        return {"ok": False, "gate": result, **c.finish(e, "blocked", f"QUALITY_GATE_REJECTED: score {q.get('score')}, failure {q.get('failure')}, "
                                                                     f"hard fails {q.get('hard_fails')}, gate errors {q.get('gate_errors')}, "
                                                                     f"final-text {(q.get('final_text') or {}).get('failure')}")}
    return {"ok": True, "gate": result}


def submit(c: Ctx, *, key: str = "", **_: Any) -> dict[str, Any]:
    e = c.entry(key)
    if e["status"] != "claimed":
        raise StageError(f"submit needs a prepared, claimed key (status {e['status']})")
    run_dir = ROOT / e["run_dir"]
    pv = json.loads((run_dir / "preview.json").read_text()) if (run_dir / "preview.json").is_file() else {}
    if pv.get("status") != "previewed" or not pv.get("quality_accepted"):
        raise StageError("submit needs an accepted preview (run prepare and gate first)")
    c.touch(e, "submit")
    if executor() == "local":
        return submit_local(c, e, run_dir, pv)
    try:
        token, claims = run_grants.mint(c.host.grant_key(), repository=R.REPO, pr_number=e["pr"], head_sha=e["sha"], now=int(c.host.clock()),
                                        ttl_s=A.GRANT_TTL_S, test_level=R.LEVEL, prompt_sha256=pv["prompt_sha256"], purpose=run_grants.AUTO_PURPOSE)
    except run_grants.GrantError as exc:
        return {"ok": False, **c.finish(e, "blocked", f"{exc.code}: {exc.reason}")}
    grant_dir = Path(pv["private_dir"])
    grant_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    grant_file = grant_dir / f"grant-{claims['grant_id']}.token"
    fd = os.open(grant_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    del token
    e.update(status="submitting", grant_id=claims["grant_id"], grant_expires_at=claims["expires_at"])
    A.save_state(c.state_dir, c.state)
    try:                                                               # wait_s=0: submit only; n8n owns the wait loop
        R.execute(c.host, run_dir=run_dir, confirm_repo=R.REPO, confirm_pr=e["pr"], confirm_sha=e["sha"], grant_file=grant_file,
                  approved=True, wait_s=0, out=log)
    except R.Blocked as b:
        return {"ok": False, **c.finish(e, "blocked", f"{b.code}: {b.detail}")}
    finally:
        grant_file.unlink(missing_ok=True)
    pv = json.loads((run_dir / "preview.json").read_text())
    e.update(status="submitted", job_id=pv.get("job_id"), submitted_at=pv.get("submitted_at") or R.now_iso())
    c.touch(e, "submit")
    return {"ok": True, "job_id": e["job_id"], "grant_id": e["grant_id"]}


# ------------------------------------------------------------------------------------------- local executor --
LOCAL_START_WAIT_S = 20


def executor() -> str:
    """`sandbox` (worker → launcher → container, default) or `local` (the OpenCode CLI on this machine under bubblewrap,
    scripts/pipeline/prqa_local_opencode.py). Operator setting PRQA_OPENCODE_EXECUTOR in compose/.env."""
    v = (os.environ.get("PRQA_OPENCODE_EXECUTOR") or R.env_file_values(ROOT / "compose" / ".env").get("PRQA_OPENCODE_EXECUTOR") or "sandbox")
    return v if v in ("sandbox", "local") else "sandbox"


def _local_status(run_dir: Path) -> dict[str, Any]:
    try:
        return json.loads((run_dir / "local" / "status.json").read_text())
    except (OSError, ValueError):
        return {}


def _alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _stop_local(e: dict[str, Any], run_dir: Path) -> None:
    """Stop a local run (its whole session: python → bwrap → opencode) and remove its API relay container."""
    if _alive(e.get("local_pid")):
        try:
            os.killpg(int(e["local_pid"]), signal.SIGTERM)
        except OSError:
            pass
        for _ in range(20):
            if not _alive(e.get("local_pid")):
                break
            time.sleep(0.5)
    try:
        rid = json.loads((run_dir / "env" / "env-state.json").read_text())["run_id"]
        subprocess.run(["docker", "rm", "-f", f"prqa-dx-{rid}-hostrelay"], capture_output=True, timeout=60)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass


def _engine_label(e: dict[str, Any], st: dict[str, Any]) -> tuple[str, str]:
    """(executor label, human name) of the engine a local run used: OpenCode, or Claude Code after a switch."""
    engine = st.get("engine") or e.get("engine") or "opencode"
    return f"local-{engine}", {"claude": "Claude Code"}.get(engine, "OpenCode")


def _local_failed_job(run_dir: Path, e: dict[str, Any], message: str, st: dict[str, Any] | None = None) -> None:
    label, _ = _engine_label(e, st or {})
    R._write(run_dir / "job.json", {"id": e.get("job_id"), "status": "failed", "launch_state": "local", "executor": label,
                                    "result": None, "error": {"message": message[:300]}})
    pv = json.loads((run_dir / "preview.json").read_text())
    pv.update(status="executed", job_status="failed", executor=label)
    R._write(run_dir / "preview.json", pv)


def _salvage_local_result(run_dir: Path, e: dict[str, Any], st: dict[str, Any]) -> bool:
    """The agent finished and wrote a schema-valid qa-result.json, but the runner died afterwards (e.g. copying evidence).
    Record that result as the job's outcome so the report and Discord carry the real test results instead of NO_RESULT."""
    from prqa_worker.validation import SchemaStore, validate_result
    candidates = [Path(st["evidence"]) / "qa-result.json"] if st.get("evidence") else []
    candidates.append(run_dir / "local" / "evidence" / "qa-result.json")
    for f in candidates:
        try:
            result = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(result, dict) or not validate_result(result, SchemaStore(ROOT / "schemas"))["valid"]:
            continue
        pv = json.loads((run_dir / "preview.json").read_text())
        label, name = _engine_label(e, st)
        result["executed_by"] = dict(result.get("executed_by") or {}, prompt_source=pv.get("prompt_source") or "reference_fallback")
        finished = st.get("finished_at") or R.now_iso()
        note = f"{name} result recovered after the runner failed: {st.get('error') or 'runner ended'}"[:300]
        R._write(run_dir / "job.json", {"id": e.get("job_id"), "status": "succeeded", "launch_state": "local", "executor": label,
                                        "model": (result.get("executed_by") or {}).get("model"), "finished_at": finished,
                                        "result": result, "note": note})
        R._write(run_dir / "result.json", result)
        R._write(run_dir / "execution.json", {"job_id": e.get("job_id"), "status": "succeeded", "launch_state": "local", "executor": label,
                                              "finished_at": finished, "engine": label.removeprefix("local-"), "note": note,
                                              "opencode_executed": bool(result.get("test_cases"))})
        pv.update(status="executed", job_id=e.get("job_id"), job_status="succeeded", executor=label)
        R._write(run_dir / "preview.json", pv)
        log(note)
        return True
    return False


def submit_local(c: Ctx, e: dict[str, Any], run_dir: Path, pv: dict[str, Any]) -> dict[str, Any]:
    (run_dir / "local").mkdir(parents=True, exist_ok=True)
    (run_dir / "local" / "status.json").unlink(missing_ok=True)
    logf = open(run_dir / "local" / "runner.log", "w")
    p = subprocess.Popen(["systemd-inhibit", "--what=sleep:idle", "--who=prqa", "--why=PRQA local agent run", "--mode=block",
                          sys.executable, "-u", str(ROOT / "scripts" / "pipeline" / "prqa_local_opencode.py"), "--run-dir", str(run_dir), "--repo", R.REPO],
                         cwd=ROOT, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True)
    logf.close()
    e.update(status="submitted", job_id=f"local-{pv['run_id']}", executor="local", local_pid=p.pid, submitted_at=R.now_iso())
    c.touch(e, "submit")
    deadline = time.time() + LOCAL_START_WAIT_S                      # catch an immediate refusal (re-gate, head moved, …)
    while time.time() < deadline and _local_status(run_dir).get("state") in (None, "starting"):
        if p.poll() is not None:
            break
        time.sleep(1)
    st = _local_status(run_dir)
    if st.get("state") == "failed" or (p.poll() is not None and st.get("state") != "finished"):
        _stop_local(e, run_dir)
        return {"ok": False, **c.finish(e, "blocked", f"LOCAL_OPENCODE_NOT_STARTED: {st.get('error') or 'runner exited'}")}
    return {"ok": True, "job_id": e["job_id"], "executor": "local", "state": st.get("state")}


def poll_local(c: Ctx, e: dict[str, Any]) -> dict[str, Any]:
    run_dir = ROOT / e["run_dir"]
    st = _local_status(run_dir)
    if st.get("state") == "finished":
        return {"terminal": True, "timed_out": False, "status": st.get("job_status"), "launch_state": "local"}
    if st.get("state") == "failed" or not _alive(e.get("local_pid")):
        _stop_local(e, run_dir)
        if _salvage_local_result(run_dir, e, st):
            return {"terminal": True, "timed_out": False, "status": "succeeded", "launch_state": "local", "recovered": True}
        _, name = _engine_label(e, st)
        _local_failed_job(run_dir, e, f"local {name} run failed: {st.get('error') or 'runner process ended without a result'}", st)
        return {"terminal": True, "timed_out": False, "status": "failed", "launch_state": "local"}
    waited = _age_s(e.get("submitted_at"), time.time()) or 0
    return {"terminal": False, "timed_out": waited > WAIT_LIMIT_S, "status": st.get("state") or "starting", "launch_state": "local",
            "waited_s": int(waited)}


def poll(c: Ctx, *, key: str = "", **_: Any) -> dict[str, Any]:
    e = c.entry(key)
    if e["status"] != "submitted" or not e.get("job_id"):
        raise StageError(f"poll needs a submitted key (status {e['status']})")
    c.touch(e, "poll")
    if e.get("executor") == "local":
        return poll_local(c, e)
    st, job = c.host.worker("GET", f"/jobs/{e['job_id']}")
    job = (job or {}).get("job", job) if isinstance(job, dict) else {}
    if st != 200:
        return {"terminal": False, "timed_out": False, "status": f"worker HTTP {st}"}
    if job.get("status") in R.TERMINAL:
        run_dir = ROOT / e["run_dir"]
        R._write(run_dir / "job.json", job)
        if job.get("result"):
            R._write(run_dir / "result.json", job["result"])
        ex = json.loads((run_dir / "execution.json").read_text()) if (run_dir / "execution.json").is_file() else {}
        ex.update(job_id=e["job_id"], status=job.get("status"), launch_state=job.get("launch_state"), ledger_after=c.host.ledger(), finished_at=R.now_iso(),
                  opencode_executed=bool(job.get("result")) and job.get("status") == "succeeded")
        R._write(run_dir / "execution.json", ex)
        pv = json.loads((run_dir / "preview.json").read_text())
        pv.update(status="executed", job_status=job.get("status"))
        R._write(run_dir / "preview.json", pv)
        return {"terminal": True, "timed_out": False, "status": job.get("status"), "launch_state": job.get("launch_state")}
    waited = _age_s(e.get("submitted_at"), time.time()) or 0
    return {"terminal": False, "timed_out": waited > WAIT_LIMIT_S, "status": job.get("status"), "launch_state": job.get("launch_state"),
            "waited_s": int(waited)}


def finalize(c: Ctx, *, key: str = "", **_: Any) -> dict[str, Any]:
    e = c.entry(key)
    run_dir = ROOT / e["run_dir"]
    if e["status"] != "submitted" or not (run_dir / "job.json").is_file():
        raise StageError(f"finalize needs a submitted key whose job finished (status {e['status']})")
    job = json.loads((run_dir / "job.json").read_text())
    if job.get("status") not in R.TERMINAL:
        raise StageError("job is not finished yet")
    c.touch(e, "finalize")
    info = A.write_reports(run_dir, entry=e, out=log)
    e.update(verdict=info["verdict"], opencode_executed=info["executed"])
    reason = None
    if job.get("status") != "succeeded":
        err = str((job.get("error") or {}).get("message") or "")[:300]
        reason = f"job finished with status {job.get('status')}" + (f": {err}" if err else "")
    return {"ok": True, "opencode_executed": info["executed"], **c.finish(e, "executed", reason)}


def fail(c: Ctx, *, key: str = "", reason: str = "", **_: Any) -> dict[str, Any]:
    e = c.entry(key)
    if e["status"] not in A.IN_FLIGHT:
        return {"finished": False, "reason": f"already {e['status']}"}
    reason = re.sub(r"[^\w .:;,()/#@=-]", "", str(reason or "stopped by n8n"))[:300]
    if e.get("executor") == "local":                                   # never leave OpenCode running for a run we gave up on
        _stop_local(e, ROOT / e["run_dir"])
    elif e.get("job_id"):
        c.host.worker("POST", f"/jobs/{e['job_id']}/cancel", {"reason": f"n8n: {reason}"[:200]})
    return c.finish(e, "blocked", reason)


def recover(c: Ctx, **_: Any) -> dict[str, Any]:
    stale = {f["key"] for f in in_flight(c.state, time.time()) if f["stale"]}
    done_local = []
    for k in sorted(stale):                                            # local runs: no worker job to adopt
        e = c.state["keys"][k]
        if e.get("executor") == "local":
            _stop_local(e, ROOT / e["run_dir"])
            c.finish(e, "interrupted", "n8n lost the run; local agent stopped, not re-run automatically")
            done_local.append(k)
    stale -= set(done_local)
    if not stale:
        return {"recovered": done_local}
    fresh = {k: v for k, v in c.state["keys"].items() if k not in stale and v.get("status") in A.IN_FLIGHT}
    for v in fresh.values():                                           # A.recover walks every in-flight key: hide the live ones
        v["_hold"], v["status"] = v["status"], "held"
    try:
        done = A.recover(c.host, state=c.state, state_dir=c.state_dir, auto_dir=c.auto_dir, wait_s=60, out=log)
    finally:
        for v in fresh.values():
            v["status"] = v.pop("_hold")
        A.save_state(c.state_dir, c.state)
    return {"recovered": done_local + done, "still_running": sorted(stale - set(done))}


STAGES = {f.__name__: f for f in (discover, claim, prepare, promptstep, gate, submit, poll, finalize, fail, recover)}


def run(stage: str, args: dict[str, Any], host=None, state_dir: Path = A.STATE_DIR, auto_dir: Path = A.AUTO_DIR) -> dict[str, Any]:
    if stage not in STAGES:
        raise StageError(f"unknown stage {stage!r}")
    c = Ctx(host or R.Host(), state_dir, auto_dir)
    with A.locked(state_dir):
        c.state = A.load_state(state_dir)
        return {"stage": stage, **STAGES[stage](c, **args)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=sorted(STAGES))
    ap.add_argument("--repo", default=os.environ.get("PRQA_TARGET_REPO") or R.DEFAULT_REPO, choices=sorted(R.PROJECTS))
    ap.add_argument("--pr", type=int)
    ap.add_argument("--key", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--stdin", action="store_true", help="read the stage's JSON body (e.g. gate's generation) from stdin")
    a = ap.parse_args(argv)
    R.select_project(a.repo)
    args: dict[str, Any] = {k: v for k, v in (("pr", a.pr), ("key", a.key), ("reason", a.reason)) if v}
    if a.stdin:
        body = json.loads(sys.stdin.read() or "{}")
        gen = body.get("generation") if isinstance(body, dict) else None
        args["generation"] = {k: gen.get(k) for k in R.GENERATION_FIELDS} if isinstance(gen, dict) else None
        if a.stage == "promptstep" and isinstance(gen, dict) and isinstance(gen.get("usage"), dict):
            args["usage"] = gen["usage"]
    sys.stdout.flush()
    real_stdout = os.dup(1)
    os.dup2(2, 1)                                                      # anything the pipeline prints (incl. child processes) → stderr
    try:
        res = run(a.stage, args)
        rc = 0
    except A.Busy as b:
        res, rc = {"stage": a.stage, "busy": True, "detail": str(b)}, 3
    except StageError as exc:
        res, rc = {"stage": a.stage, "error": str(exc)}, 2
    finally:
        sys.stdout.flush()
        os.dup2(real_stdout, 1)
        os.close(real_stdout)
    print(json.dumps(res, sort_keys=True, default=str), flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
