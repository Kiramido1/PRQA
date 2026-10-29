#!/usr/bin/env python3
"""PRQA — authorized Groq live-model prompt quality evaluation (bounded, review-only).

Drives the PRODUCTION prompt-builder request path for the active provider (direct Groq, openai/gpt-oss-120b):
    Policy.from_env(compose/.env).generation_settings()
    build_llm_request(templates, model=PROMPT_LLM_MODEL, provider="groq", max_tokens=..., reasoning_effort=...)
    -> POST https://api.groq.com/openai/v1/chat/completions
directly (outside the n8n pipeline), for the 15 synthetic benchmark cases only. The request is byte-for-byte the
shape the worker returns from /prompts/build: max_completion_tokens (Groq deprecates max_tokens), reasoning_effort and
include_reasoning=false for GPT-OSS, stream=false, no OpenRouter `reasoning` field. Nothing in the pipeline state
changes: AUTOMATION_MODE stays review_only, PROMPT_LLM_LIVE_IN_REVIEW_ONLY stays false.

Hard safety invariants (checked before any network operation; the run refuses otherwise):
- PROMPT_LLM_API_BASE == https://api.groq.com/openai/v1 and PROMPT_LLM_MODEL is a model with pinned, documented
  Groq limits (GROQ_MODEL_LIMITS); the estimated prompt plus the completion budget must fit them.
- AUTOMATION_MODE == review_only and PROMPT_LLM_LIVE_IN_REVIEW_ONLY == false in compose/.env.
- The key is GROQ_API_KEY from ~/.config/prqa/secrets.env only (owner-only file). PROMPT_LLM_API_KEY is never a
  substitute, a key in compose/.env (FUSE drive, no file modes) is refused, and an OpenRouter-shaped key is refused.
  The key goes only into the Authorization header of the Groq request and never into any output or artifact.
- There is no OpenRouter fallback and no model-list preflight: the first network operation is the generation request.

Provider budget (proxy/provider_budget.py): every live request is first reserved in the SAME ledger the model proxy
uses (PRQA_PROVIDER_BUDGET_DIR from the secrets file, PRQA_BUDGET_* limits from compose/.env): daily requests and
tokens, per-job tokens, the tokens-per-minute window (waits up to PRQA_BUDGET_MAX_WAIT_S), one request in flight. The
reservation is the calibrated prompt estimate (PRQA_BUDGET_PROMPT_CHARS_PER_TOKEN) plus max_completion_tokens and is
settled with the reported usage. A missing or invalid budget configuration blocks the run before any request.

Bounded: max 15 requests (1 per case), one attempt per request (no automatic retries), stop on any failed request,
no model substitution, no fallback substitution. Outcomes, truncation handling, redaction, archiving and version
labels are shared with evaluate_openrouter_live.py: an empty, malformed, truncated or provider-error response is a
failed request with a case-<cid>.diagnostic.json (never gated) and stops the run; artifacts carry a `generation` block.

Exit 0 = all attempts completed (success of the MODEL deltas is in the artifacts, not the exit code).

Usage:
  python3 scripts/eval/evaluate_groq_live.py --check-config             # local preconditions only: no network, no files
  python3 scripts/eval/evaluate_groq_live.py --only C01-clear-behaviour --dry-run   # payload shape, no network, no key
  python3 scripts/eval/evaluate_groq_live.py --only C01-clear-behaviour --limit 1   # one live request
  python3 scripts/eval/evaluate_groq_live.py --merge                    # re-score saved artifacts offline
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "tests"))

from prqa_worker.policy import Policy  # noqa: E402
from prqa_worker.prompt_builder import (  # noqa: E402
    GenerationConfigError, GENERATION_GATE_REJECTED, GENERATION_TIMEOUT_S, GENERATION_TRUNCATED, GENERATION_USABLE,
    TEMPLATE_VERSION, Templates, _provider_from_base, build_llm_request, is_truncated, validate_generated_prompt,
)
from prqa_worker import qa_rubric  # noqa: E402  (the production rubric the worker enforces)
from qa_benchmark import CASES, context_for  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts" / "eval"))
import evaluate_openrouter_live as shared  # noqa: E402  (pure helpers: parsing, outcomes, redaction, archiving)
sys.path.insert(0, str(ROOT / "proxy"))
import provider_budget as pb  # noqa: E402  (the ledger shared with the model proxy)

EXPECTED_BASE = "https://api.groq.com/openai/v1"
EXPECTED_PROVIDER = "groq"
OUT_DIR = ROOT / "runs" / "groq-live"
BACKUP_DIR = ROOT / "runs" / "baseline-backups"
HASHED_SOURCES = tuple(f for f in shared.HASHED_SOURCES if f != "scripts/eval/evaluate_openrouter_live.py") + (
    "scripts/eval/evaluate_openrouter_live.py", "scripts/eval/evaluate_groq_live.py")
MAX_REQUESTS = 15
SECRETS_FILE = Path.home() / ".config" / "prqa" / "secrets.env"

# The exact request fields the production builder emits for Groq; anything else is refused before sending.
GROQ_PAYLOAD_KEYS = {"model", "messages", "temperature", "max_completion_tokens", "stream",
                     "reasoning_effort", "include_reasoning"}
# Published limits (https://console.groq.com/docs/models, checked 2026-10-26), pinned so no authenticated model-list
# call is needed before a generation request. A model without an entry here is never sent.
GROQ_MODEL_LIMITS = {
    "openai/gpt-oss-120b": {"context_window": 131072, "max_completion_tokens": 65536},
}
CHARS_PER_TOKEN_FLOOR = 3          # conservative prompt-token estimate (no local tokenizer) for the context-fit check


def source_hashes() -> dict[str, str]:
    out = {}
    for rel in HASHED_SOURCES:
        f = ROOT / rel
        out[rel] = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else "missing"
    return out


def artifact_cid(p: Path) -> str:
    return p.name.removeprefix("case-").removesuffix(".response.json")


def load_env_records() -> dict[str, str]:
    """compose/.env: the non-secret configuration (base, model, flags, generation settings)."""
    p = ROOT / "compose" / ".env"
    return _parse_env(p.read_text(encoding="utf-8").splitlines()) if p.is_file() else {}


def load_secret_records() -> dict[str, str]:
    """~/.config/prqa/secrets.env alone: the only source the Groq key is taken from. compose/.env lives on a drive that
    does not enforce file modes, so a key there is readable by every local user. Refused unless the file is readable
    by its owner only."""
    if not SECRETS_FILE.is_file():
        return {}
    if SECRETS_FILE.stat().st_mode & 0o077:
        raise SystemExit(f"BLOCKED — {SECRETS_FILE} is readable by other users; run chmod 600 on it")
    return _parse_env(SECRETS_FILE.read_text(encoding="utf-8").splitlines())


def _parse_env(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def is_placeholder(key: str) -> bool:
    return bool(re.fullmatch(r"(?i)(<.*>|generate.*|your.*|xxx.*)", key))


def foreign_key_shape(key: str) -> str | None:
    """Name the provider whose key format this is when it is clearly not a Groq key (the value is never echoed)."""
    return "openrouter" if key.startswith("sk-or-") else None


def verify_preconditions(env: dict[str, str], secrets: dict[str, str], *, require_key: bool = True) -> dict:
    """Local checks only (no network). Returns {base, slug, key, settings}; raises SystemExit naming every problem."""
    problems = []
    mode = env.get("AUTOMATION_MODE", "").strip().lower()
    live_flag = env.get("PROMPT_LLM_LIVE_IN_REVIEW_ONLY", "").strip().lower()
    if mode != "review_only":
        problems.append(f"AUTOMATION_MODE={mode!r} (must be review_only)")
    if live_flag not in ("false", ""):
        problems.append(f"PROMPT_LLM_LIVE_IN_REVIEW_ONLY={live_flag!r} (must be false)")
    base = env.get("PROMPT_LLM_API_BASE", "").strip().rstrip("/")
    slug = env.get("PROMPT_LLM_MODEL", "").strip()
    if base != EXPECTED_BASE or _provider_from_base(base) != EXPECTED_PROVIDER:
        problems.append(f"PROMPT_LLM_API_BASE={base!r} (must be exactly {EXPECTED_BASE}; Groq is the only active "
                        "provider and there is no OpenRouter fallback)")
    if slug not in GROQ_MODEL_LIMITS:
        problems.append(f"PROMPT_LLM_MODEL={slug!r} has no pinned Groq limits (expected one of "
                        f"{sorted(GROQ_MODEL_LIMITS)})")
    settings = None
    try:
        settings = Policy.from_env({**env, "PROMPT_LLM_API_BASE": EXPECTED_BASE}).generation_settings()
    except GenerationConfigError as e:
        problems.append(f"generation settings: {e}")
    if settings and settings["reasoning"]:
        problems.append("the OpenRouter `reasoning` field would be sent; Groq rejects it")
    key = (secrets.get("GROQ_API_KEY") or "").strip()
    if (env.get("GROQ_API_KEY") or "").strip():
        problems.append("GROQ_API_KEY is set in compose/.env, whose drive does not enforce file permissions; move it "
                        f"to {SECRETS_FILE} (chmod 600) and clear it in compose/.env")
    elif key and foreign_key_shape(key):
        problems.append(f"GROQ_API_KEY has the {foreign_key_shape(key)} key format; it is never sent to Groq")
    elif require_key and not key and (secrets.get("PROMPT_LLM_API_KEY") or "").strip().startswith("gsk_"):
        problems.append(f"a Groq-format key is stored under PROMPT_LLM_API_KEY in {SECRETS_FILE}; it is never used as "
                        "a substitute. Rename that variable to GROQ_API_KEY (edit the file; keep it chmod 600)")
    elif require_key and (not key or is_placeholder(key)):
        problems.append(f"GROQ_API_KEY missing/placeholder in {SECRETS_FILE}. Create a new key at "
                        "https://console.groq.com/keys and add it there with an editor (chmod 600). "
                        "PROMPT_LLM_API_KEY is never used for Groq.")
    if problems:
        raise SystemExit("BLOCKED — Groq safety preconditions failed:\n  - " + "\n  - ".join(problems))
    return {"base": base, "slug": slug, "key": key, "settings": settings}


def open_budget(env: dict[str, str], secrets: dict[str, str]) -> tuple[pb.Ledger, float, float]:
    """The shared provider-budget ledger, or SystemExit (BLOCKED) before any request when it is not configured."""
    try:
        cfg = pb.BudgetConfig.from_env({**{k: v for k, v in env.items() if k.startswith("PRQA_BUDGET_")},
                                        "PRQA_PROVIDER_BUDGET_DIR": secrets.get("PRQA_PROVIDER_BUDGET_DIR", "")})
        ratio = float(env.get("PRQA_BUDGET_PROMPT_CHARS_PER_TOKEN") or "nan")
        pb.estimate_tokens(1, ratio)
        wait = float(env.get("PRQA_BUDGET_MAX_WAIT_S") or "nan")
        if not 0 <= wait <= 300:
            raise pb.BudgetError("PRQA_BUDGET_MAX_WAIT_S must be within 0..300")
        return pb.Ledger(cfg), ratio, wait
    except (pb.BudgetError, ValueError) as exc:
        raise SystemExit(f"BLOCKED — provider budget not configured: {exc}") from None


def budget_need(payload: dict, chars_per_token: float) -> int:
    chars = sum(len(m.get("content") or "") for m in payload.get("messages", []))
    return pb.estimate_tokens(chars, chars_per_token) + int(payload.get("max_completion_tokens") or 0)


def http_json(url: str, *, method: str = "GET", payload: dict | None = None,
              headers: list[tuple[str, str]] | None = None, timeout: int = 120) -> tuple[int, dict | str]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for name, value in (headers or []):
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.getcode(), json.loads(body)
            except json.JSONDecodeError:
                return resp.getcode(), body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.getcode(), json.loads(body)
        except json.JSONDecodeError:
            return e.getcode(), body


def short_error(provider_msg: str, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", provider_msg).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def estimated_prompt_tokens(payload: dict) -> int:
    chars = sum(len(m.get("content") or "") for m in payload.get("messages", []))
    return -(-chars // CHARS_PER_TOKEN_FLOOR)


def context_fit(payload: dict, meta: dict) -> tuple[bool, str]:
    """True when the conservatively estimated prompt plus the completion budget fits the model's published limits."""
    budget = payload.get("max_completion_tokens", payload.get("max_tokens")) or 0
    window, cap = meta.get("context_window"), meta.get("max_completion_tokens")
    if not isinstance(window, int):
        return False, "context_window not published by Groq /models; fit cannot be verified"
    if isinstance(cap, int) and budget > cap:
        return False, f"completion budget {budget} exceeds the model's max_completion_tokens {cap}"
    need = estimated_prompt_tokens(payload) + budget
    return need <= window, f"estimated prompt {estimated_prompt_tokens(payload)} + completion {budget} = {need} " \
                           f"tokens vs context_window {window}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check-config", action="store_true",
                    help="local preconditions only: no network, no key output, no files written")
    ap.add_argument("--limit", type=int, default=MAX_REQUESTS, help=f"safety cap, default {MAX_REQUESTS}")
    ap.add_argument("--only", type=str, default="",
                    help="comma-separated case IDs to attempt (subset of the budget)")
    ap.add_argument("--merge", action="store_true",
                    help="offline stage: rebuild results from saved case-*.response.json "
                         "artifacts (no network traffic), then emit results.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="build and check the production payload and print its shape; no network, no key needed")
    args = ap.parse_args()
    if args.limit < 1 or args.limit > MAX_REQUESTS:
        ap.error(f"--limit must be between 1 and {MAX_REQUESTS}")
    if sum((args.check_config, args.merge, args.dry_run)) > 1:
        ap.error("--check-config, --merge and --dry-run are mutually exclusive")

    env = load_env_records()
    offline = args.dry_run or args.merge
    secrets = {} if offline else load_secret_records()
    info = verify_preconditions(env, secrets, require_key=not offline)
    base, slug, key, settings = info["base"], info["slug"], info["key"], info["settings"]
    provider = EXPECTED_PROVIDER
    limits = GROQ_MODEL_LIMITS[slug]

    templates = Templates.load(ROOT / "prompts")
    wanted = ([s.strip() for s in args.only.split(",") if s.strip()]
              if args.only else [c.cid for c in CASES[: args.limit]])
    unknown = [w for w in wanted if w not in {c.cid for c in CASES}]
    if unknown:
        raise SystemExit(f"BLOCKED — unknown case ids: {unknown}")
    selected = [c for c in CASES if c.cid in wanted][: args.limit]

    def request_for(case) -> dict:
        payload = build_llm_request(
            templates, model=slug, provider=provider,
            repository="git@synthetic-org/bench-repo", pr_number=case.pr_number,
            test_level="standard", synthetic=True, context=context_for(case), max_tokens=settings["max_tokens"],
            reasoning_effort=settings["reasoning_effort"])
        # Hard safety assertions: the production Groq shape only, no "reasoning" field, no key in the body.
        assert "reasoning" not in payload, f"SAFETY: 'reasoning' field must not be sent to Groq; got keys={set(payload)}"
        assert not set(payload) - GROQ_PAYLOAD_KEYS, f"unexpected payload fields for Groq: {set(payload) - GROQ_PAYLOAD_KEYS}"
        assert not key or key not in json.dumps(payload), "SAFETY: API key found inside request payload"
        return payload

    if args.check_config:
        for case in selected:
            fits, detail = context_fit(request_for(case), limits)
            if not fits:
                raise SystemExit(f"BLOCKED — {case.cid}: {detail}")
        _, cpt, _ = open_budget(env, secrets)
        print(f"[check-config] provider budget configured; reservations "
              f"{[budget_need(request_for(c), cpt) for c in selected]} tokens")
        print(f"[check-config] provider={provider} base={base} model={slug} key=GROQ_API_KEY (secrets file) present; "
              f"max_completion_tokens={settings['max_tokens']} reasoning_effort={settings['reasoning_effort']}; "
              f"{len(selected)} case(s) fit the pinned limits. Local checks only: nothing was sent or written.")
        return 0

    if args.dry_run:
        for case in selected:
            payload = request_for(case)
            shape = {k: v for k, v in payload.items() if k != "messages"}
            print(f"[dry-run] {case.cid} payload={json.dumps(shape, sort_keys=True)} "
                  f"messages={len(payload['messages'])} prompt_chars="
                  f"{sum(len(m['content']) for m in payload['messages'])} "
                  f"est_prompt_tokens<={estimated_prompt_tokens(payload)}")
        print(f"[dry-run] {len(selected)} request(s) would be sent to {base}/chat/completions; nothing was sent")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.merge:
        rows: list[dict] = []
        budget_used = 0
        current = source_hashes()
        failed_cids = set()
        for diag_file in sorted(OUT_DIR.glob("case-*.diagnostic.json")):
            # the latest attempt for that case failed: its older response (if any) is never re-scored
            d = json.loads(diag_file.read_text(encoding="utf-8"))
            budget_used += 1
            failed_cids.add(d.get("cid"))
            status, changed = shared.version_status(d.get("source_sha256"), current)
            rows.append({"cid": d.get("cid"), "provider": provider, "model_slug": slug, "request": "failed",
                         "failure_reason": d.get("failure_reason"), "error": d.get("detail"),
                         "outcome": d.get("outcome") or shared.failure_outcome(d.get("failure_reason")),
                         "would_source": "would_fallback", "usage": (d.get("diagnostics") or {}).get("usage", {}),
                         "row_source": "rebuilt_from_diagnostic", "generation": d.get("generation"),
                         "version_status": status, "sources_changed_since_generation": changed})
        for artifact in sorted(OUT_DIR.glob("case-*.response.json")):
            data = json.loads(artifact.read_text(encoding="utf-8"))
            if data.get("cid") in failed_cids:
                continue
            budget_used += 1
            case = next((c for c in CASES if c.cid == data["cid"]), None)
            if case is None:
                continue
            ctx = context_for(case)
            status, changed = shared.version_status(data.get("source_sha256"), current)
            if is_truncated(data.get("finish_reason")):
                rows.append({"cid": case.cid, "provider": provider, "model_slug": slug,
                             "http_status": data.get("http_status"), "seconds": data.get("seconds"), "request": "failed",
                             "failure_reason": shared.TRUNCATED_MODEL_OUTPUT, "outcome": GENERATION_TRUNCATED,
                             "would_source": "would_fallback", "finish_reason": data.get("finish_reason"),
                             "usage": data.get("usage", {}), "row_source": "rebuilt_from_artifact",
                             "generation": data.get("generation"), "version_status": status,
                             "sources_changed_since_generation": changed})
                continue
            gate = validate_generated_prompt(data["content"], pr_title=case.title, context=ctx)
            rub = qa_rubric.score_prompt(data["content"], pr_title=case.title, context=ctx, gate=gate)
            dec = qa_rubric.accept_result(rub, gate)   # the worker's acceptance rule
            rows.append({
                "cid": case.cid, "provider": provider, "model_slug": slug,
                "http_status": data["http_status"], "seconds": data["seconds"], "request": "ok",
                "model_returned": data.get("model_returned"), "finish_reason": data.get("finish_reason"),
                "usage": data.get("usage", {}),
                "gate_valid": gate["valid"], "would_source": "llm" if dec["accepted"] else "would_fallback",
                "quality_failure": dec["failure"],
                "gate_errors": gate["errors"][:8], "gate_warnings": gate.get("warnings", [])[:6],
                "quality_score": round(rub.quality_score, 1),
                "dimensions": {k: rub.score(k) for k in qa_rubric.DIMENSIONS},
                "hard_fails": rub.hard_fails, "human_flags": rub.human_flags,
                "outcome": GENERATION_USABLE if dec["accepted"] else GENERATION_GATE_REJECTED,
                "row_source": "rebuilt_from_artifact", "generation": data.get("generation"),
                "version_status": status, "sources_changed_since_generation": changed,
            })
        ok_rows = [r for r in rows if r["request"] == "ok"]
        summary = {
            "evaluation_mode": "offline_rescore",
            "provider": provider, "endpoint_host": base, "model_slug": slug,
            "n8n_retry_policy_note": shared.GENERATION_POLICY_NOTE,
            "stopped_reasons": [],
            "usage_totals": {
                "request_budget": MAX_REQUESTS,
                "requests_spent": budget_used,
                "successful": len(ok_rows),
                "failed": len(rows) - len(ok_rows),
                "prompt_tokens": sum((r.get("usage") or {}).get("prompt_tokens") or 0 for r in ok_rows),
                "completion_tokens": sum((r.get("usage") or {}).get("completion_tokens") or 0 for r in ok_rows),
                "total_tokens": sum((r.get("usage") or {}).get("total_tokens") or 0 for r in ok_rows),
            },
            "results": rows,
        }
        (OUT_DIR / "results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"[merge] {budget_used} artifacts rebuilt; budget spent {budget_used}/{MAX_REQUESTS}; "
              f"no network traffic")
        return 0

    headers = [
        ("Authorization", f"Bearer {key}"),
        ("Content-Type", "application/json"),
        ("User-Agent", "prqa-groq-client/1.0"),
    ]
    print(f"[config] provider={provider} base={base} model={slug} key_source=GROQ_API_KEY (secrets file) "
          f"max_completion_tokens={settings['max_tokens']} reasoning_effort={settings['reasoning_effort']}")
    # Pinned published limits, checked locally before any request (no model-list call).
    for case in selected:
        fits, detail = context_fit(request_for(case), limits)
        print(f"[context] {case.cid}: {detail}")
        if not fits:
            raise SystemExit(f"BLOCKED — {case.cid}: {detail}")
    ledger, chars_per_token, max_wait = open_budget(env, secrets)
    snap = ledger.snapshot()
    needs = [budget_need(request_for(c), chars_per_token) for c in selected]
    print(f"[budget] today {snap['requests_today']}/{snap['limits']['daily_requests']} requests, "
          f"{snap['tokens_today']}/{snap['limits']['daily_tokens']} tokens; this run reserves {needs} "
          f"(tokens/minute {snap['limits']['tokens_per_minute']}, per job {snap['limits']['job_tokens']})")
    if snap["requests_today"] + len(needs) > snap["limits"]["daily_requests"] \
            or snap["tokens_today"] + sum(needs) > snap["limits"]["daily_tokens"] \
            or any(n > min(snap["limits"]["tokens_per_minute"], snap["limits"]["job_tokens"]) for n in needs):
        raise SystemExit("BLOCKED — the remaining provider budget cannot accommodate this run; nothing was sent")

    archived = shared.archive_before_attempt(
        [c.cid for c in selected] if args.only else [], label="groq-live", remove_stale=bool(args.only),
        out_dir=OUT_DIR, backup_dir=BACKUP_DIR)
    if archived:
        print(f"[archive] previous artifacts copied to {archived.name}")
    hashes = source_hashes()
    reasoning_meta = {"effort": settings["reasoning_effort"],
                      "include_reasoning": False if settings["reasoning_effort"] else None}
    (OUT_DIR / "run_meta.json").write_text(json.dumps({
        "design_note": "no timestamps in run_meta (hash-comparable); each case artifact records its own "
                       "generation.generated_at; model limits pinned from Groq docs, no model-list call",
        "provider": provider,
        "endpoint_host": urlsplit(base).hostname,
        "base": base,
        "model_slug": slug,
        "model_limits": limits,
        "key_source": "GROQ_API_KEY (secrets file)",
        "request_budget": MAX_REQUESTS,
        "pipeline_state": {"AUTOMATION_MODE": "review_only",
                           "PROMPT_LLM_LIVE_IN_REVIEW_ONLY": "false"},
        "groq_specific": {
            "reasoning_field_sent": False,
            "allowed_payload_keys": sorted(GROQ_PAYLOAD_KEYS),
            "token_parameter": "max_completion_tokens",
            **reasoning_meta,
            "openrouter_fallback": False,
        },
        "requested_max_tokens": settings["max_tokens"],
        "generation_timeout_s": GENERATION_TIMEOUT_S,
        "template_version": TEMPLATE_VERSION,
        "source_sha256": hashes,
    }, indent=2), encoding="utf-8")

    already = {artifact_cid(p) for p in OUT_DIR.glob("case-*.response.json")} if not args.only else set()
    if already:
        print(f"[resume] skipping already-attempted cases: {len(already)} artifacts on disk")
        wanted = [c for c in wanted if c not in already]
        if not wanted:
            print("[resume] nothing left to attempt under the budget")

    n8n_policy = shared.GENERATION_POLICY_NOTE

    def generation_meta(outcome: str) -> dict:
        return {"model_slug": slug, "provider": provider, "endpoint_host": urlsplit(base).hostname,
                "template_version": TEMPLATE_VERSION, "reference_sha256": templates.reference_sha256,
                "source_sha256": hashes, "requested_max_tokens": settings["max_tokens"],
                "reasoning": reasoning_meta,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "request_count": 1, "retries": 0, "outcome": outcome}
    results: list[dict] = []
    stopped: list[str] = []
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                    "requests": 0, "successful": 0, "failed": 0}

    for case in [c for c in selected if c.cid in wanted]:
        ctx = context_for(case)
        request_payload = request_for(case)

        job_id = f"evaluator-{case.cid}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        deadline = time.monotonic() + max_wait
        rid = None
        while rid is None:
            try:
                rid = ledger.reserve(source="evaluator", job_id=job_id, tokens=budget_need(request_payload, chars_per_token),
                                     completion_allowance=int(request_payload.get("max_completion_tokens") or 0))
            except pb.BudgetDenied as exc:
                if exc.retry_after_s is None or time.monotonic() + exc.retry_after_s > deadline:
                    stopped.append(f"{case.cid}: provider budget — {exc.reason}; nothing sent (no retry)")
                    break
                print(f"[budget] waiting {exc.retry_after_s:.1f}s: {exc.reason}")
                time.sleep(exc.retry_after_s)
        if rid is None:
            break

        t0 = time.monotonic()
        try:
            code, body = http_json(
                f"{base.rstrip('/')}/chat/completions", method="POST",
                payload=request_payload, headers=headers,
                timeout=GENERATION_TIMEOUT_S)
            transport = None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            timed_out = isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError)
            transport = shared.TRANSPORT_TIMEOUT if timed_out else shared.TRANSPORT_ERROR
            code, body = 0, {"error": {"message": f"transport failure: {type(e).__name__}: {e}"}}
        elapsed = round(time.monotonic() - t0, 1)
        usage_total = ((body.get("usage") or {}).get("total_tokens") if code == 200 and isinstance(body, dict) else None)
        ledger.settle(rid, total_tokens=usage_total if isinstance(usage_total, int) else None,
                      outcome="ok" if code == 200 else (transport or f"http_{code}"))

        row: dict = {"cid": case.cid, "provider": provider, "model_slug": slug,
                     "http_status": code, "seconds": elapsed}
        echo_source = "\n".join(m.get("content", "") for m in request_payload["messages"])
        if code != 200:
            failure = transport or f"HTTP_{code}"
            outcome = shared.failure_outcome(failure)
            row.update({"failure_reason": failure, "outcome": outcome, "would_source": "would_fallback",
                        "version_status": "current"})
            err_msg, perr = shared.describe_http_failure(body, key=key, echo_source=echo_source)
            http_diag: dict = {"http_status": code}
            if perr is not None:
                http_diag["provider_error"] = perr
            # The outcome comes from the HTTP status alone; the provider's own classification is recorded beside it,
            # never derived from or merged into the status (a 413 is not assumed to be a body-size or a rate limit).
            row.update({"request": "failed", "error": err_msg,
                        "provider_error_code": (perr or {}).get("code"), "provider_error_type": (perr or {}).get("type")})
            results.append(row)
            shared.write_json_atomic(OUT_DIR / f"case-{case.cid}.diagnostic.json", {
                "cid": case.cid, "request": "failed", "failure_reason": failure, "outcome": outcome,
                "detail": err_msg, "seconds": elapsed, "content_present": False,
                "diagnostics": http_diag, "source_sha256": hashes, "generation": generation_meta(outcome)})
            usage_totals["requests"] += 1
            usage_totals["failed"] += 1
            stopped.append(f"{case.cid}: {failure} — stopping remaining cases (no retry, no other provider)")
            break

        parsed = shared.parse_completion(code, body, key=key, echo_source=echo_source)
        diag = parsed["diagnostics"]
        if parsed["failure_reason"]:
            # empty, malformed or truncated: failed, diagnostics kept, never gated, no retry; the run stops
            outcome = shared.failure_outcome(parsed["failure_reason"])
            row.update({"request": "failed", "failure_reason": parsed["failure_reason"], "outcome": outcome,
                        "would_source": "would_fallback", "version_status": "current",
                        "error": f"{parsed['failure_reason']}: {parsed['detail']}", "diagnostics": diag})
            results.append(row)
            record = {"cid": case.cid, "request": "failed", "failure_reason": parsed["failure_reason"],
                      "outcome": outcome, "detail": parsed["detail"], "seconds": elapsed, "content_present": False,
                      "diagnostics": diag, "source_sha256": hashes, "generation": generation_meta(outcome)}
            if parsed.get("partial_content"):
                partial, red = shared.redact_for_artifact(parsed["partial_content"], key)
                record.update({"content_present": True, "partial_content_chars": len(parsed["partial_content"]),
                               "partial_content_redaction": red, "partial_content": partial})
            shared.write_json_atomic(OUT_DIR / f"case-{case.cid}.diagnostic.json", record)
            usage_totals["requests"] += 1
            usage_totals["failed"] += 1
            stopped.append(f"{case.cid}: {parsed['failure_reason']} — stopping remaining cases")
            break

        content = parsed["content"]
        usage = body.get("usage") or {}
        raw_model = body.get("model")
        finish_reason = body["choices"][0].get("finish_reason")

        gate = validate_generated_prompt(content, pr_title=case.title, context=ctx)
        rub = qa_rubric.score_prompt(content, pr_title=case.title, context=ctx, gate=gate)
        dec = qa_rubric.accept_result(rub, gate)   # the worker's acceptance rule (gate + no hard fail + >= 85)
        would_source = "llm" if dec["accepted"] else "would_fallback"
        outcome = GENERATION_USABLE if dec["accepted"] else GENERATION_GATE_REJECTED
        stored, redaction = shared.redact_for_artifact(content, key)

        shared.write_json_atomic(OUT_DIR / f"case-{case.cid}.response.json", {
            "cid": case.cid, "provider": provider, "http_status": code, "seconds": elapsed,
            "model_returned": raw_model, "finish_reason": finish_reason,
            "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
            "diagnostics": diag, "redaction": redaction, "source_sha256": hashes,
            "outcome": outcome, "generation": generation_meta(outcome),
            "content": stored,
        })

        row.update({
            "request": "ok",
            "model_returned": raw_model,
            "finish_reason": finish_reason,
            "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
            "gate_valid": gate["valid"],
            "would_source": would_source,
            "quality_failure": dec["failure"],
            "outcome": outcome,
            "version_status": "current",
            "gate_errors": gate["errors"][:8],
            "gate_warnings": gate.get("warnings", [])[:6],
            "quality_score": round(rub.quality_score, 1),
            "dimensions": {k: rub.score(k) for k in qa_rubric.DIMENSIONS},
            "hard_fails": rub.hard_fails,
            "human_flags": rub.human_flags,
        })
        results.append(row)
        usage_totals["requests"] += 1
        usage_totals["successful"] += 1
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            usage_totals[k] += usage.get(k) or 0
        print(f"[{row['cid']}] HTTP {code} {elapsed}s tokens={row['usage']['total_tokens']} "
              f"gate={'valid' if gate['valid'] else 'INVALID'} score={row['quality_score']} "
              f"hf={len(rub.hard_fails)}")

    summary = {
        "provider": provider,
        "endpoint_host": base,
        "model_slug": slug,
        "n8n_retry_policy_note": n8n_policy,
        "stopped_reasons": stopped,
        "usage_totals": usage_totals,
        "results": results,
    }
    (OUT_DIR / "results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n[done] provider={provider} requests={usage_totals['requests']} "
          f"successful={usage_totals['successful']} "
          f"failed={usage_totals['failed']} total_tokens={usage_totals['total_tokens']}")
    if stopped:
        print("[stopped] " + " | ".join(stopped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
