#!/usr/bin/env python3
"""Local polling alternative to an inbound tunnel: reconcile the `to be test` label state of open PRs with what PRQA
already knows, and submit ONLY changes to the LOCAL receiver through the authenticated GitHub Actions contract
(signature, timestamp window, replay protection, repository allowlist and rate limits are reused unchanged).

    python3 scripts/pipeline/prqa_label_poller.py --dry-run          # show what would change; nothing is sent
    python3 scripts/pipeline/prqa_label_poller.py                    # one reconciliation pass (schedule it yourself: cron / systemd timer)
    python3 scripts/pipeline/prqa_label_poller.py --interval 120     # foreground loop until Ctrl-C (never daemonises)
    python3 scripts/pipeline/prqa_label_poller.py --gh-cli --dry-run # discovery only, read through the authenticated `gh` CLI:
                                                            # gh keeps its own token (never read, exported or forwarded)

Environment (e.g. `set -a; source compose/.env; set +a` plus a READ-ONLY GitHub token exported in the shell):
    PRQA_POLL_REPOSITORIES     comma-separated owner/name list (each must be registered in config/repositories.yaml)
    GITHUB_TOKEN               fine-grained PAT: Pull requests (read), Metadata (read), Issues (read, for who labelled). Never written.
    PRQA_WEBHOOK_SECRET        the worker's secret (signs each request)
    PRQA_RECEIVER_URL          default http://127.0.0.1:5678/webhook/prqa/actions (loopback http or https only)
    PRQA_TRIGGER_LABEL         default "to be test"            PRQA_DEFAULT_TEST_LEVEL   default standard
    PRQA_POLL_STATE_FILE       default runs/poller-state.json  (minimal state, no secrets, written atomically)
    PRQA_POLL_RERUN_ON_RELABEL default true   — label removed then re-added on the same SHA → new run (force_rerun)
    PRQA_POLL_RETEST_ON_NEW_SHA default false — label kept while commits are pushed → cancel older-SHA jobs only;
                                               true → also submit the new SHA (documented rerun policy)
    PRQA_POLL_BASE_BRANCHES    optional comma-separated base branches that are eligible (e.g. "main,Dev"); empty = any
    PRQA_TRIGGER_ALLOWED_ACTORS optional comma-separated logins; the poller resolves WHO added the label from the
                               GitHub issue-events API and skips labels added by anyone else

Semantics per pass (per PR):  new label on SHA → submit · same label/SHA already submitted → nothing (no request at all)
· label gone → signed `unlabel` request (cancels active jobs) · label re-added → rerun policy · SHA changed → see above
· PR closed → `unlabel` once. State survives restarts; GitHub API calls use pagination and bounded retries with
exponential backoff + jitter. Outbound only. No background process is started by this script.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[2]
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PER_PAGE = 100
MAX_PAGES = 10
STATE_VERSION = "1.0"
USER_AGENT = "prqa-label-poller/1.1"


def load_client():
    spec = importlib.util.spec_from_file_location("prqa_actions_client", ROOT / "scripts" / "clients" / "prqa_actions_client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class GitHubError(RuntimeError):
    pass


# ------------------------------------------------------------- GitHub API --
class GitHub:
    """Minimal read-only client: pagination + bounded retries (exponential backoff with jitter, Retry-After / reset honoured)."""

    def __init__(self, api_url: str, token: str, *, opener=urllib.request.urlopen, sleep: Callable[[float], None] = time.sleep,
                 rand: Callable[[], float] = random.random, clock: Callable[[], float] = time.time,
                 timeout_s: int = 30, max_attempts: int = 4) -> None:
        self.api_url = api_url.rstrip("/")
        self._token = token
        self.opener = opener
        self.sleep = sleep
        self.rand = rand
        self.clock = clock
        self.timeout_s = timeout_s
        self.max_attempts = max(1, max_attempts)
        self.calls = 0

    def get(self, path: str) -> tuple[Any, dict[str, str]]:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token      # header only: never argv, never logged, never persisted
        last = "no attempt"
        for attempt in range(1, self.max_attempts + 1):
            self.calls += 1
            req = urllib.request.Request(url, headers=headers)
            try:
                with self.opener(req, timeout=self.timeout_s) as resp:
                    body = resp.read()
                    return json.loads(body.decode("utf-8")) if body else None, {k.lower(): v for k, v in resp.headers.items()}
            except urllib.error.HTTPError as err:
                status = int(err.code)
                hdrs = {k.lower(): v for k, v in (err.headers.items() if err.headers else [])}
                last = f"HTTP {status}"
                retryable = status in (429, 500, 502, 503, 504) or (status == 403 and ("retry-after" in hdrs or hdrs.get("x-ratelimit-remaining") == "0"))
                if not retryable or attempt == self.max_attempts:
                    raise GitHubError(f"GitHub API {last} for {self._safe(url)}")
                wait = self._retry_wait(hdrs, attempt)
            except (urllib.error.URLError, OSError) as err:
                last = type(err).__name__
                if attempt == self.max_attempts:
                    raise GitHubError(f"GitHub API unreachable ({last}) for {self._safe(url)}")
                wait = self._retry_wait({}, attempt)
            self.sleep(wait)
        raise GitHubError(last)

    def _retry_wait(self, hdrs: dict[str, str], attempt: int) -> float:
        if hdrs.get("retry-after"):
            try:
                return min(120.0, max(1.0, float(hdrs["retry-after"])))
            except ValueError:
                pass
        if hdrs.get("x-ratelimit-remaining") == "0" and hdrs.get("x-ratelimit-reset"):
            try:
                return min(300.0, max(1.0, float(hdrs["x-ratelimit-reset"]) - self.clock()))
            except ValueError:
                pass
        base = min(60.0, 2.0 ** attempt)
        return base + self.rand() * base          # exponential backoff with full jitter

    def paginate(self, path: str, *, max_pages: int = MAX_PAGES) -> list[Any]:
        items: list[Any] = []
        sep = "&" if "?" in path else "?"
        for page in range(1, max_pages + 1):
            chunk, _ = self.get(f"{path}{sep}per_page={PER_PAGE}&page={page}")
            if not isinstance(chunk, list):
                break
            items.extend(chunk)
            if len(chunk) < PER_PAGE:
                break
        return items

    @staticmethod
    def _safe(url: str) -> str:
        return re.sub(r"\?.*$", "", url)


class GhCliReader:
    """Read-only GitHub access through the installed, already-authenticated `gh` CLI (discovery/dry-run only).
    Same interface as GitHub.get/paginate. The CLI keeps its own credential: this class never reads, prints or forwards it,
    and it is never used for anything that submits to PRQA (the submit path needs the dedicated read-only GITHUB_TOKEN)."""

    api_url = "https://api.github.com"

    # gh stderr that means "network/GitHub hiccup", worth another try (auth or 404 errors are not retried)
    _TRANSIENT = re.compile(r"timed? ?out|timeout|connect|connection|TLS|EOF|HTTP 5\d\d|HTTP 429|temporar", re.I)

    def __init__(self, *, runner=subprocess.run, timeout_s: int = 30, max_attempts: int = 3,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.runner = runner
        self.timeout_s = timeout_s
        self.max_attempts = max(1, max_attempts)
        self.sleep = sleep
        self.calls = 0

    def get(self, path: str) -> tuple[Any, dict[str, str]]:
        if not path.startswith("/repos/") or any(c in path for c in " \n\r\t"):
            raise GitHubError("gh-cli reader only accepts /repos/... GET paths")
        safe = GitHub._safe(path)
        for attempt in range(1, self.max_attempts + 1):
            self.calls += 1
            last = attempt == self.max_attempts
            try:
                proc = self.runner(["gh", "api", "--method", "GET", "-H", "Accept: application/vnd.github+json", path],
                                   capture_output=True, text=True, timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                if last:
                    raise GitHubError(f"GitHub did not answer within {self.timeout_s}s ({self.max_attempts} tries) for {safe}"
                                      " - check the internet connection and run again")
                self.sleep(2.0 * attempt)
                continue
            except OSError as err:
                raise GitHubError(f"gh api failed ({type(err).__name__}) for {safe}")
            if proc.returncode != 0:
                first = (proc.stderr or "").strip().splitlines()[:1]
                detail = re.sub(r"gh[pousr]_[A-Za-z0-9_]+", "<redacted>", first[0])[:160] if first else f"exit {proc.returncode}"
                if not last and first and self._TRANSIENT.search(first[0]):
                    self.sleep(2.0 * attempt)
                    continue
                raise GitHubError(f"gh api error for {safe}: {detail}")
            return (json.loads(proc.stdout) if proc.stdout.strip() else None), {}
        raise GitHubError(f"gh api failed for {safe}")  # unreachable

    def paginate(self, path: str, *, max_pages: int = MAX_PAGES) -> list[Any]:
        return GitHub.paginate(self, path, max_pages=max_pages)  # type: ignore[arg-type]


# ----------------------------------------------------------------- state --
def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") == STATE_VERSION and isinstance(data.get("repositories"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": STATE_VERSION, "repositories": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic replace; the state holds PR numbers, SHAs, keys and outcomes — never a token or a secret."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".poller-state-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ------------------------------------------------------------- reconcile --
def _flag(env: dict, name: str, default: bool) -> bool:
    raw = str(env.get(name, "") or "").strip().lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


def who_labelled(gh: GitHub, repository: str, number: int, label: str) -> str | None:
    """Login that most recently added `label` (GitHub issue-events API: actor is set by GitHub, not by the PR author)."""
    try:
        events = gh.paginate(f"/repos/{repository}/issues/{number}/events", max_pages=3)
    except GitHubError:
        return None
    actor = None
    for ev in events:
        if isinstance(ev, dict) and ev.get("event") == "labeled" and ((ev.get("label") or {}).get("name") or "").casefold() == label.casefold():
            actor = ((ev.get("actor") or {}).get("login")) or actor
    return actor


def run_once(env: dict, *, opener=urllib.request.urlopen, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
             rand: Callable[[], float] = random.random, dry_run: bool = False, out: Callable[[str], None] = print,
             state_path: Path | None = None, gh_reader: Any = None) -> dict[str, Any]:
    client = load_client()
    repos = [r.strip() for r in (env.get("PRQA_POLL_REPOSITORIES") or "").split(",") if r.strip()]
    if not repos:
        raise SystemExit("PRQA_POLL_REPOSITORIES is empty")
    bad = [r for r in repos if not REPO_RE.match(r)]
    if bad:
        raise SystemExit(f"invalid repository names: {bad}")
    receiver = (env.get("PRQA_RECEIVER_URL") or "http://127.0.0.1:5678/webhook/prqa/actions").strip()
    if not (receiver.startswith("https://") or re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?/", receiver)):
        raise SystemExit("PRQA_RECEIVER_URL must be https:// or a loopback http:// URL")
    if not dry_run and not env.get("PRQA_WEBHOOK_SECRET"):
        raise SystemExit("PRQA_WEBHOOK_SECRET is required to sign requests (set -a; source compose/.env)")
    label = env.get("PRQA_TRIGGER_LABEL") or "to be test"
    level = env.get("PRQA_DEFAULT_TEST_LEVEL") or "standard"
    rerun_on_relabel = _flag(env, "PRQA_POLL_RERUN_ON_RELABEL", True)
    retest_on_new_sha = _flag(env, "PRQA_POLL_RETEST_ON_NEW_SHA", False)
    allowed_actors = {a.strip().casefold() for a in (env.get("PRQA_TRIGGER_ALLOWED_ACTORS") or "").split(",") if a.strip()}
    state_path = state_path or Path(env.get("PRQA_POLL_STATE_FILE") or (ROOT / "runs" / "poller-state.json"))
    state = load_state(state_path)
    bases = {b.strip() for b in (env.get("PRQA_POLL_BASE_BRANCHES") or "").split(",") if b.strip()}
    if env.get("PRQA_POLL_GITHUB_BACKEND") == "gh-cli":
        if not dry_run:
            raise SystemExit("--gh-cli is discovery-only: use it with --dry-run (submitting requires the dedicated read-only GITHUB_TOKEN)")
        gh = gh_reader or GhCliReader()
    else:
        gh = GitHub((env.get("GITHUB_API_URL") or "https://api.github.com"), env.get("GITHUB_TOKEN") or "", opener=opener, sleep=sleep, rand=rand, clock=clock)
    now = clock()
    summary: dict[str, Any] = {"dry_run": dry_run, "submitted": 0, "cancelled": 0, "unchanged": 0, "skipped": 0, "errors": 0, "repositories": {},
                               "candidates": [], "selected": None}

    def send(repo: str, number: int, trigger: str, *, force: bool, labelled_by: str | None) -> str:
        """Returns the receiver outcome per the response contract: accepted | duplicate | cancelled | <error>."""
        if dry_run:
            return "dry_run"
        fd, out_path = tempfile.mkstemp(prefix=".prqa-client-out-", suffix=".txt")
        os.close(fd)
        cenv = {
            "PRQA_WEBHOOK_URL": receiver, "PRQA_WEBHOOK_SECRET": env.get("PRQA_WEBHOOK_SECRET", ""), "PRQA_ALLOW_INSECURE_URL": "1",
            "GITHUB_TOKEN": env.get("GITHUB_TOKEN") or "", "GITHUB_REPOSITORY": repo, "GITHUB_API_URL": gh.api_url, "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": str(int(now)), "GITHUB_RUN_ATTEMPT": "1", "GITHUB_WORKFLOW": "prqa-label-poller", "GITHUB_EVENT_NAME": "label_poll",
            "GITHUB_ACTOR": labelled_by or "prqa-label-poller", "PRQA_PR_NUMBER": str(number), "PRQA_TEST_LEVEL": level, "PRQA_TRIGGER_LABEL": label,
            "PRQA_HTTP_TIMEOUT_SECONDS": env.get("PRQA_HTTP_TIMEOUT_SECONDS") or "30", "PRQA_SOURCE": "local_poller",
            "PRQA_TRIGGER_OVERRIDE": trigger, "PRQA_FORCE_RERUN": "true" if force else "false", "PRQA_LABELLED_BY": labelled_by or "",
            "GITHUB_OUTPUT": out_path,
        }
        try:
            rc = client.main(cenv, opener=opener, sleep=sleep, clock=clock)
            outcome = ""
            try:
                for line in Path(out_path).read_text(encoding="utf-8").splitlines():
                    if line.startswith("outcome="):
                        outcome = line.split("=", 1)[1].strip()
            except OSError:
                pass
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
        if rc == 0 and outcome in ("accepted", "duplicate", "cancelled"):
            return outcome
        return outcome or "error"

    for repo in repos:
        rstate = state["repositories"].setdefault(repo, {})
        rsum: dict[str, Any] = {"labelled": [], "actions": {}}
        summary["repositories"][repo] = rsum
        try:
            issues = gh.paginate(f"/repos/{repo}/issues?state=open&labels={urllib.request.quote(label)}")
            # exact, case-sensitive label match (the API's label filter is not case-sensitive)
            labelled = sorted({int(i["number"]) for i in issues if isinstance(i, dict) and i.get("pull_request") and isinstance(i.get("number"), int)
                               and label in [str((x or {}).get("name")) for x in (i.get("labels") or []) if isinstance(x, dict)]})
            heads: dict[int, dict[str, Any]] = {}
            for n in labelled:
                pr, _ = gh.get(f"/repos/{repo}/pulls/{n}")
                sha = str(((pr or {}).get("head") or {}).get("sha") or "").lower()
                if not SHA_RE.match(sha):
                    raise GitHubError(f"PR #{n}: malformed head sha")
                head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
                heads[n] = {"sha": sha, "draft": bool(pr.get("draft")), "state": pr.get("state"),
                            "base": str(((pr.get("base") or {}).get("ref")) or ""), "fork": head_repo != repo,
                            "title": str(pr.get("title") or "")[:200], "url": f"https://github.com/{repo}/pull/{n}"}
        except GitHubError as err:
            out(f"[{repo}] skipped this pass: {err}")
            summary["errors"] += 1
            rsum["error"] = str(err)
            continue
        rsum["labelled"] = labelled

        # 1. labels present now
        for n in labelled:
            key = str(n)
            prev = rstate.get(key) or {}
            sha = heads[n]["sha"]
            if heads[n]["draft"]:
                rsum["actions"][key] = "skipped_draft"; summary["skipped"] += 1
                continue
            if heads[n]["state"] != "open":
                rsum["actions"][key] = "skipped_not_open"; summary["skipped"] += 1
                continue
            if heads[n]["fork"]:
                rsum["actions"][key] = "skipped_fork"; summary["skipped"] += 1
                continue
            if bases and heads[n]["base"] not in bases:
                rsum["actions"][key] = "skipped_base_branch"; summary["skipped"] += 1
                continue
            if prev.get("label_present") and prev.get("submitted_sha") == sha and prev.get("submitted_level") == level and not prev.get("pending_rerun"):
                rsum["actions"][key] = "unchanged"; summary["unchanged"] += 1
                continue
            if prev.get("label_present") and prev.get("submitted_sha") and prev["submitted_sha"] != sha and not retest_on_new_sha:
                # commits pushed while labelled: webhook semantics — cancel older-SHA jobs, do not auto-test the new SHA
                outcome = send(repo, n, "unlabel", force=False, labelled_by=None) if not prev.get("superseded_sha") == sha else "cancelled"
                ok = outcome == "cancelled"
                rstate[key] = {**prev, "head_sha": sha, "observed_at": now, **({"superseded_sha": sha} if ok else {})}
                rsum["actions"][key] = "superseded_cancel" if ok else "error"; summary["cancelled" if ok else "errors"] += 1
                out(f"[{repo}] PR #{n}: new head {sha[:12]} while labelled → older jobs cancelled; re-add the label to test it")
                continue
            actor = who_labelled(gh, repo, n, label) if (allowed_actors or not dry_run) else None
            if allowed_actors and (not actor or actor.casefold() not in allowed_actors):
                rsum["actions"][key] = "skipped_actor_not_allowed"; summary["skipped"] += 1
                out(f"[{repo}] PR #{n}: label added by {actor or 'unknown'} (not in PRQA_TRIGGER_ALLOWED_ACTORS); skipped")
                rstate[key] = {**prev, "head_sha": sha, "label_present": True, "observed_at": now, "skipped_actor": actor}
                continue
            relabel = bool(prev.get("submitted_sha") == sha and (not prev.get("label_present") or prev.get("pending_rerun")))
            force = relabel and rerun_on_relabel
            if relabel and not rerun_on_relabel:
                rstate[key] = {**prev, "label_present": True, "observed_at": now}
                rsum["actions"][key] = "relabel_ignored"; summary["unchanged"] += 1
                continue
            if dry_run:
                out(f"[{repo}] would submit PR #{n} @ {sha[:12]} ({'rerun' if force else 'new'})")
                rsum["actions"][key] = "dry_run"
                summary["candidates"].append({"repository": repo, "number": n, "title": heads[n]["title"], "base": heads[n]["base"],
                                              "head_sha": sha, "url": heads[n]["url"]})
                continue
            outcome = send(repo, n, "label", force=force, labelled_by=actor)
            accepted = outcome in ("accepted", "duplicate")       # only the receiver's answer counts as submitted
            rstate[key] = {"head_sha": sha, "label_present": True,
                           "submitted_sha": sha if accepted else prev.get("submitted_sha"),
                           "submitted_level": level if accepted else prev.get("submitted_level"),
                           "submitted_at": now if accepted else prev.get("submitted_at"),
                           "pending_rerun": (not accepted) and relabel,          # retried next pass under the same policy
                           "last_outcome": outcome, "observed_at": now}
            rsum["actions"][key] = "submitted" if accepted else "error"; summary["submitted" if accepted else "errors"] += 1

        # 2. labels gone (removed or PR closed) for PRs we knew as labelled
        for key, prev in list(rstate.items()):
            if prev.get("label_present") and int(key) not in labelled:
                if dry_run:
                    out(f"[{repo}] would cancel PR #{key} (label no longer present)")
                    rsum["actions"][key] = "dry_run_cancel"
                    continue
                outcome = send(repo, int(key), "unlabel", force=False, labelled_by=None)
                ok = outcome == "cancelled"
                # on failure the label stays recorded as present so the cancel is retried next pass
                rstate[key] = {**prev, "label_present": not ok, "observed_at": now, "last_outcome": "cancelled" if ok else "cancel_error"}
                rsum["actions"][key] = "cancelled" if ok else "error"; summary["cancelled" if ok else "errors"] += 1
        # forget PRs that have been unlabelled for a long time (30 days) to keep the state small
        for key in [k for k, v in rstate.items() if not v.get("label_present") and now - float(v.get("observed_at") or now) > 30 * 86400]:
            rstate.pop(key, None)

    # deterministic single choice for a controlled first run: the oldest eligible PR (lowest number), then repository name
    if summary["candidates"]:
        summary["selected"] = sorted(summary["candidates"], key=lambda c: (c["number"], c["repository"]))[0]
    if not dry_run:
        save_state(state_path, state)
    summary["github_api_calls"] = gh.calls
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="show the reconciliation plan; send nothing, write no state")
    ap.add_argument("--interval", type=int, default=0, help="seconds between passes (0 = one pass, default)")
    ap.add_argument("--gh-cli", action="store_true", help="read GitHub through the authenticated gh CLI (requires --dry-run)")
    args = ap.parse_args(argv)
    env = dict(os.environ)
    if args.gh_cli:
        env["PRQA_POLL_GITHUB_BACKEND"] = "gh-cli"
    while True:
        summary = run_once(env, dry_run=args.dry_run)
        print(json.dumps({k: v for k, v in summary.items() if k != "repositories"}))
        if args.interval <= 0:
            return 1 if summary["errors"] else 0
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    sys.exit(main())
