#!/usr/bin/env python3
"""PRQA chunked OpenCode execution (runs INSIDE the sandbox, as the sandbox user, with the sandbox environment).

Why: the provider allows 8,000 tokens per minute, and that bounds every single model request (input + completion
allowance). OpenCode's default agent alone sends ~8k tokens (system prompt + 10 tool schemas), and the gated QA prompt
is ~7.7k tokens, so one OpenCode session over the whole prompt can never run. Instead:

  * OpenCode is configured by the entrypoint with a slim primary agent `prqa` (short system prompt, bash tool only) and
    a small context limit, so it compacts long sessions instead of growing past the provider limit;
  * the gated prompt (verified by hash before this runs) is split deterministically: a shared header (role, environment
    and API discovery, strict constraints, test identities) + ONE group of scenarios per OpenCode session, taken
    verbatim from the prompt; nothing is rewritten or invented;
  * each session writes one small JSON record per scenario (`tc/<ID>.json`); this script then builds `qa-result.json`
    deterministically (counts, coverage matrix, defects, verdict) so the result always validates and every planned
    scenario appears exactly once (missing ones are BLOCKED with the reason).

A synthetic identity is created up front through the documented self-registration operation (when section 14 names
one) so each session does not spend model requests on it; its token is exported as $TOKEN to OpenCode only, and is
scrubbed from every record and evidence file before the result is written.

Engines: `opencode` (default; the sandbox image and the local OpenCode CLI) or `claude` (the Claude Code CLI on this
machine, `claude -p` with the Bash tool only; scripts/pipeline/prqa_local_opencode.py --engine claude).

Usage (entrypoint): chunked_run.py --prompt FILE --repo-dir DIR --evidence DIR --model prqa/<m> --timeout-s N [--engine E]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SCENARIO_RE = re.compile(r"^\*\*([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{3})\s*\((Required|Regression|Conditional|Blocked)\)", re.I | re.M)
HEADING_RE = re.compile(r"^(#{1,3}) ", re.M)
HEADER_SECTIONS = (r"^# ", r"^## 1\.", r"^## 4\.", r"^### 8\.1", r"^## 14\.")
CONTEXT_SECTION = r"^## 2\."
CONTEXT_GROUP_PREFIXES = ("STATED", "SREG", "REG")
MAX_GROUP_SCENARIOS = 3
STATUSES = ("PASS", "FAIL", "BLOCKED", "NOT_APPLICABLE")
SEVERITIES = ("P0", "P1", "P2", "P3")
ORIGINS = ("introduced_by_pr", "pre_existing", "environment", "test_data", "ambiguous_contract", "unknown")
REGISTER_RE = re.compile(r"self-registration operation `POST (/[A-Za-z0-9_./{}-]+)`")
FIELD_MAX = 1000                      # qa-result schema: expected/actual/impact/… maxLength
ENGINES = {"opencode": "opencode", "claude": "claude_code"}     # --engine → qa-result executed_by.engine
ENGINE_NAMES = {"opencode": "OpenCode", "claude": "Claude Code"}
CLAUDE_SYSTEM = ("You are a senior QA engineer testing an HTTP API with curl through the Bash tool. Follow the user's "
                 "instructions exactly, keep command output short, and never modify the repository.")
# a refusal that every later session would hit too: stop instead of spending the remaining runtime on it
FATAL_CAUSES = ("provider quota exhausted", "agent not authenticated")


# ------------------------------------------------------------------------------------------------- splitting --
def blocks(text: str) -> list[dict[str, Any]]:
    """Heading-delimited blocks (#, ##, ###), each with its heading line and body."""
    starts = [m.start() for m in HEADING_RE.finditer(text)]
    if not starts or starts[0] != 0:
        starts = [0] + starts
    out = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(text)
        chunk = text[s:e]
        out.append({"heading": chunk.splitlines()[0] if chunk else "", "text": chunk.rstrip() + "\n",
                    "ids": [m.group(1) for m in SCENARIO_RE.finditer(chunk)]})
    return out


def split_scenarios(block: dict[str, Any]) -> list[dict[str, Any]]:
    """A block with more than MAX_GROUP_SCENARIOS scenarios is cut at scenario starts (heading kept on each part)."""
    if len(block["ids"]) <= MAX_GROUP_SCENARIOS:
        return [block]
    text = block["text"]
    marks = [m.start() for m in SCENARIO_RE.finditer(text)]
    lead = text[:marks[0]]
    parts = []
    for i in range(0, len(marks), MAX_GROUP_SCENARIOS):
        s = marks[i]
        e = marks[i + MAX_GROUP_SCENARIOS] if i + MAX_GROUP_SCENARIOS < len(marks) else len(text)
        body = lead + text[s:e]
        parts.append({"heading": block["heading"], "text": body, "ids": [m.group(1) for m in SCENARIO_RE.finditer(body)]})
    return parts


def split_prompt(text: str) -> dict[str, Any]:
    bl = blocks(text)
    header = "".join(b["text"] for b in bl if any(re.match(p, b["heading"]) for p in HEADER_SECTIONS))
    context = "".join(b["text"] for b in bl if re.match(CONTEXT_SECTION, b["heading"]))
    groups = []
    for b in bl:
        if b["ids"]:
            groups.extend(split_scenarios(b))
    for i, g in enumerate(groups, 1):
        g["name"] = f"g{i:02d}"
        g["area"] = area_of(g["heading"])
    return {"header": header, "context": context, "groups": groups, "planned": [i for g in groups for i in g["ids"]]}


def area_of(heading: str) -> str:
    h = re.sub(r"^#+\s*(\d+\.\s*)?", "", heading).strip()
    return re.sub(r"\s*—.*$", "", h)[:80] or "General"


def register_path(text: str) -> str | None:
    m = REGISTER_RE.search(text)
    return m.group(1) if m else None


CHUNK_CONTRACT = """
## Your task in this session

Execute ONLY these scenarios: {ids}. Other scenarios of this PR run in separate sessions; do not test them.
{identity}
Keep every command output short so the session stays small: use `curl -s -o "$PRQA_EVIDENCE_DIR/<file>" -w '%{{http_code}}\\n' …`
and then `head -c 800 "$PRQA_EVIDENCE_DIR/<file>"`; never print a whole OpenAPI document (use `jq` to extract only the
operation you need, piped through `head -c 1500`).

For EACH scenario ID above record the outcome with the `prqa-record` command (it writes the record file for you;
never write JSON by hand), for example:
`prqa-record --id <ID> --status PASS --http 200 --endpoint "GET /path" --scenario "short title" --expected "…" --actual "what you observed" --evidence <file>`
Status is PASS, FAIL, BLOCKED (reason in --actual) or NOT_APPLICABLE. For FAIL also pass `--defect-title "…" --severity P0|P1|P2|P3
--origin introduced_by_pr|pre_existing|environment|test_data|ambiguous_contract|unknown --impact "…" --repro 'curl … $TOKEN …'`.
Record each scenario as soon as it is done. Never put tokens or passwords in any argument or file.
Do not write qa-result.json and do not modify the repository.
When every scenario is recorded, reply with the single word DONE.
"""


def chunk_prompt(split: dict[str, Any], group: dict[str, Any], *, token_ready: bool) -> str:
    identity = ("A synthetic user was already registered for you through the documented self-registration operation; "
                "its bearer token is in the environment variable `$TOKEN` (use `-H \"Authorization: Bearer $TOKEN\"`). "
                "Register another synthetic identity only when a scenario needs a second user.\n"
                if token_ready else "")
    ctx = split["context"] if group["ids"] and group["ids"][0].split("-")[0] in CONTEXT_GROUP_PREFIXES else ""
    return (split["header"] + ("\n" + ctx if ctx else "") + "\n## Scenarios for this session\n\n" + group["text"] +
            CHUNK_CONTRACT.format(ids=", ".join(group["ids"]), identity=identity))


# ---------------------------------------------------------------------------------------------- aggregation --
def _s(v: Any, n: int = FIELD_MAX) -> str:
    return ("" if v is None else str(v))[:n]


def scrub(text: str, secrets_: list[str]) -> str:
    for s in secrets_:
        if s:
            text = text.replace(s, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)(?!\$|\[REDACTED|<)[A-Za-z0-9._~+/=-]{16,}", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(\"(?:password|access_token|refresh_token|token)\"\s*:\s*\")[^\"]*\"", r'\1[REDACTED]"', text)
    return text


def load_record(path: Path) -> dict[str, Any] | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def aggregate(*, split: dict[str, Any], tc_dir: Path, job: dict[str, Any], outcomes: dict[str, str],
              secrets_: list[str], model: str, prompt_sha256: str, started: str, finished: str, duration_ms: int,
              blockers: list[str], engine: str = "opencode") -> dict[str, Any]:
    cases, defects = [], []
    area_by_id = {i: g["area"] for g in split["groups"] for i in g["ids"]}
    group_by_id = {i: g["name"] for g in split["groups"] for i in g["ids"]}
    for sid in split["planned"]:
        rec = load_record(tc_dir / f"{sid}.json")
        status = str((rec or {}).get("status") or "").upper().replace(" ", "_")
        if rec is None or status not in STATUSES:
            why = outcomes.get(group_by_id[sid], "no record written")
            rec = {"scenario": "", "endpoint": "", "expected": "", "actual": f"not reported by {ENGINE_NAMES[engine]} ({why})", "http_status": None}
            status = "BLOCKED"
        http = rec.get("http_status")
        case = {"id": sid, "area": area_by_id[sid][:80], "scenario": _s(rec.get("scenario"), 300) or sid,
                "endpoint": _s(rec.get("endpoint"), 300) or "n/a", "expected": _s(rec.get("expected")) or "n/a",
                "actual": _s(rec.get("actual")) or "n/a", "status": status,
                "http_status": http if isinstance(http, int) and not isinstance(http, bool) else None,
                "evidence": _s(rec.get("evidence"), 300), "defect_ref": None}
        d = rec.get("defect") if status == "FAIL" else None
        if isinstance(d, dict):
            did = f"DEF-{len(defects) + 1:03d}"
            sev = str(d.get("severity") or "").upper()
            origin = str(d.get("origin") or "")
            defects.append({"id": did, "title": _s(d.get("title"), 200) or f"{sid} failed", "severity": sev if sev in SEVERITIES else "P2",
                            "severity_rationale": _s(d.get("severity_rationale")), "endpoint": case["endpoint"],
                            "preconditions": _s(d.get("preconditions")), "repro_curl": _s(d.get("repro_curl"), 2000),
                            "expected": _s(d.get("expected")) or case["expected"], "actual": _s(d.get("actual")) or case["actual"],
                            "http_status": case["http_status"], "impact": _s(d.get("impact")),
                            "origin": origin if origin in ORIGINS else "unknown", "test_case_ids": [sid]})
            case["defect_ref"] = did
        cases.append(case)
    counts = {s: sum(1 for c in cases if c["status"] == s) for s in STATUSES}
    areas: dict[str, dict[str, Any]] = {}
    for c in cases:
        a = areas.setdefault(c["area"], {"area": c["area"][:80], "planned": 0, "passed": 0, "failed": 0, "blocked": 0, "not_applicable": 0})
        a["planned"] += 1
        a[{"PASS": "passed", "FAIL": "failed", "BLOCKED": "blocked", "NOT_APPLICABLE": "not_applicable"}[c["status"]]] += 1
    executed = counts["PASS"] + counts["FAIL"]
    if counts["FAIL"]:
        verdict = "FAIL"
    elif executed == 0 or counts["BLOCKED"] * 2 >= len(cases):
        verdict = "INCONCLUSIVE"
    elif counts["BLOCKED"]:
        verdict = "PASS_WITH_KNOWN_ISSUES"
    else:
        verdict = "PASS"
    failed_groups = [f"{g}: {o}" for g, o in outcomes.items() if o != "ok"]
    blockers = blockers + [f"session {f}" for f in failed_groups]
    summary_txt = (f"{len(cases)} planned scenarios executed by {ENGINE_NAMES[engine]} in {len(split['groups'])} sessions: "
                   f"{counts['PASS']} passed, {counts['FAIL']} failed, {counts['BLOCKED']} blocked, "
                   f"{counts['NOT_APPLICABLE']} not applicable; {len(defects)} defect(s).")
    rationale = {"FAIL": "at least one scenario failed; see defects",
                 "INCONCLUSIVE": "too many scenarios could not run to support a verdict",
                 "PASS_WITH_KNOWN_ISSUES": "all executed scenarios passed, but some were blocked",
                 "PASS": "all planned scenarios passed or were not applicable"}[verdict]
    pr = job.get("pr") or {}
    result = {
        "result_version": "1.0", "synthetic": False, "job_id": _s(job.get("job_id"), 128), "repository": _s(job.get("repository"), 200),
        "pr": {"number": pr.get("number") if isinstance(pr.get("number"), int) else 0, "title": _s(pr.get("title"), 300),
               "head_sha": _s(pr.get("head_sha"), 40)},
        "environment": {"name": "staging", "base_url_redacted": re.sub(r"[?#].*$", "", os.environ.get("BASE_URL", "unknown"))[:200] or "unknown"},
        "executed_by": {"engine": ENGINES[engine], "mode": "live", "model": model[:120], "prompt_sha256": prompt_sha256,
                        "started_at": started, "finished_at": finished, "duration_ms": int(duration_ms)},
        "summary": {"overall_status": verdict, "total": len(cases), "passed": counts["PASS"], "failed": counts["FAIL"],
                    "blocked": counts["BLOCKED"], "not_applicable": counts["NOT_APPLICABLE"], "executive_summary": summary_txt},
        "coverage_matrix": list(areas.values()), "test_cases": cases, "defects": defects,
        "blockers": [_s(b, 500) for b in blockers][:50],
        "recommendation": {"verdict": verdict, "rationale": rationale},
    }
    return json.loads(scrub(json.dumps(result), secrets_))


# ---------------------------------------------------------------------------------------------------- run --
def find_token(d: Any) -> str:
    """The first `access_token` string anywhere in a JSON response (top level, `token.…`, `data.…`)."""
    if isinstance(d, dict):
        if isinstance(d.get("access_token"), str):
            return d["access_token"]
        for v in d.values():
            t = find_token(v)
            if t:
                return t
    elif isinstance(d, list):
        for v in d:
            t = find_token(v)
            if t:
                return t
    return ""


def register_body(spec: dict[str, Any], path: str, *, tag: str, password: str) -> dict[str, Any]:
    """Required fields of the register operation's JSON body, from the API's own OpenAPI document: an email field gets a
    synthetic address, a password field the generated password, every other required string a synthetic label."""
    op = ((spec.get("paths") or {}).get(path) or {}).get("post") or {}
    schema = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema") or {}
    for _ in range(5):
        ref = schema.get("$ref") if isinstance(schema, dict) else None
        if not ref:
            break
        node: Any = spec
        for part in ref.lstrip("#/").split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        schema = node
    body: dict[str, Any] = {}
    for f in (schema.get("required") if isinstance(schema, dict) else None) or ["email", "password"]:
        lf = str(f).lower()
        body[f] = (f"prqa-{tag}@prqa-synthetic.dev" if "email" in lf else password if "password" in lf else f"PRQA Synthetic {tag}")
    return body


def preregister(path: str | None, *, attempts: int = 3, pause_s: float = 5.0) -> tuple[str, list[str], str]:
    """Synthetic identity via the documented self-registration operation. Returns (token, secrets to scrub, why it
    failed): the reason is an HTTP status or an exception class name only, never a response body."""
    base = os.environ.get("BASE_URL", "").rstrip("/")
    if not path or not base:
        return "", [], "no documented self-registration operation" if base else "BASE_URL not set"
    password = "Prqa-" + secrets.token_urlsafe(18)
    why = ""
    for attempt in range(attempts):
        if attempt:
            time.sleep(pause_s)
        try:
            with urllib.request.urlopen(base + "/api/v1/openapi.json", timeout=30) as r:
                spec = json.loads(r.read() or b"{}")
        except (urllib.error.URLError, ValueError, OSError):
            spec = {}
        body = json.dumps(register_body(spec, path, tag=secrets.token_hex(6), password=password)).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, data=body, headers={"Content-Type": "application/json"}), timeout=30) as r:
                data = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            why = f"register answered HTTP {e.code}"
            continue
        except (urllib.error.URLError, ValueError, OSError) as e:
            why = f"register failed: {type(e).__name__}"
            continue
        tok = find_token(data)
        if tok:
            return tok, [password, tok], ""
        why = "register response had no access_token"
    return "", [password], why


def cause(log: str) -> str:
    for rx, text in ((r"provider_quota_exhausted|free usage limit|usage limit reached|out of extra usage|credit balance is too low",
                      "provider quota exhausted (free-tier or daily limit; retry after the provider's reset)"),
                     (r"authentication_error|invalid api key|not logged in|please run /login|oauth token (has )?expired",
                      "agent not authenticated (log in to the CLI on this machine)"),
                     (r"provider budget[^\n]*tokens-per-minute", "provider budget (tokens per minute)"),
                     (r"provider budget", "provider budget"), (r"\b429\b|rate.?limit", "rate limited (429)"),
                     (r"\b413\b|request too large", "request too large (413)")):
        if re.search(rx, log, re.I):
            return text
    return ""


def agent_argv(engine: str, *, model: str, repo_dir: str, name: str, text: str) -> list[str]:
    if engine == "claude":
        # headless Claude Code: Bash only (no file tools, no MCP, no slash commands), every Bash call allowed; bubblewrap
        # bounds it (repository read-only, the evidence directory the only writable path)
        return ["claude", "-p", text, "--model", model, "--output-format", "stream-json", "--verbose", "--tools", "Bash",
                "--allowedTools", "Bash", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
                "--append-system-prompt", CLAUDE_SYSTEM]
    return ["opencode", "run", "--pure", "--agent", "prqa", "--format", "json", "--dir", repo_dir,
            "-m", model, "--title", f"prqa {name}", text]


def run(args: argparse.Namespace) -> int:
    prompt = Path(args.prompt).read_text(encoding="utf-8")
    ev = Path(args.evidence)
    tc = ev / "tc"
    tc.mkdir(parents=True, exist_ok=True)
    split = split_prompt(prompt)
    job = json.loads(os.environ.get("PRQA_JOB_JSON") or "{}")
    started_t = time.time()
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_t))
    token, scrub_list, why = preregister(register_path(prompt))
    blockers = [] if token else [f"synthetic identity was not pre-registered ({why or 'unknown reason'}); sessions register their own"]
    env = dict(os.environ)
    if token:
        env["TOKEN"] = token
    outcomes: dict[str, str] = {}
    deadline = started_t + args.timeout_s - 90
    fatal = ""
    for i, g in enumerate(split["groups"]):
        if fatal:
            outcomes[g["name"]] = f"not started ({fatal})"
            continue
        left = deadline - time.time()
        if left < 120:
            outcomes[g["name"]] = "not started (runtime exhausted)"
            continue
        per = max(120, int(left / (len(split["groups"]) - i)) * 2)
        per = int(min(per, left))
        text = chunk_prompt(split, g, token_ready=bool(token))
        (ev / f"session-{g['name']}-prompt.md").write_text(text, encoding="utf-8")
        events, stderr = ev / f"{args.engine}-events-{g['name']}.jsonl", ev / f"{args.engine}-stderr-{g['name']}.log"
        with open(events, "w") as out, open(stderr, "w") as err:
            try:
                p = subprocess.run(agent_argv(args.engine, model=args.model, repo_dir=args.repo_dir, name=g["name"], text=text),
                                   stdout=out, stderr=err, env=env, cwd=args.repo_dir, timeout=per, check=False)
                rc = p.returncode
            except subprocess.TimeoutExpired:
                rc = "timeout"
        missing = [s for s in g["ids"] if not (tc / f"{s}.json").is_file()]
        if not missing:
            outcomes[g["name"]] = "ok"
        else:
            logs = events.read_text(errors="replace")[-100000:] + stderr.read_text(errors="replace")[-100000:]
            c = cause(logs)
            outcomes[g["name"]] = f"exit {rc}; no record for {', '.join(missing)}" + (f"; {c}" if c else "")
            if c.startswith(FATAL_CAUSES):
                fatal = c
    finished_t = time.time()
    result = aggregate(split=split, tc_dir=tc, job=job, outcomes=outcomes, secrets_=scrub_list, model=args.model,
                       prompt_sha256=args.prompt_sha256, started=started,
                       finished=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(finished_t)),
                       duration_ms=int((finished_t - started_t) * 1000), blockers=blockers, engine=args.engine)
    for f in ev.rglob("*"):
        if f.is_file() and f.stat().st_size < 5_000_000:
            try:
                t = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            s = scrub(t, scrub_list)
            if s != t:
                f.write_text(s, encoding="utf-8")
    (ev / "qa-result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--repo-dir", required=True)
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--timeout-s", type=int, required=True)
    ap.add_argument("--prompt-sha256", default="")
    ap.add_argument("--engine", choices=sorted(ENGINES), default="opencode")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
