#!/usr/bin/env python3
"""Operator CLI: mint ONE single-run grant (worker/prqa_worker/run_grants.py) for one exact PR head while PRQA stays
review-only. Nothing global changes; the grant authorizes at most one job and expires within minutes.

    python3 scripts/pipeline/prqa_authorize_run.py --init-key
        ensure PRQA_RUN_GRANT_KEY (64 hex, CSPRNG) exists in ~/.config/prqa/secrets.env (or PRQA_SECRETS_FILE);
        the file is rewritten atomically with mode 600, other lines verbatim. Restart the worker to load it.

    python3 scripts/pipeline/prqa_authorize_run.py --repo Kiramido1/Amenly-Product --pr 12 --sha <40 hex> [--level standard]
                                          [--ttl 900] [--verify-github] [--out PATH]
        --verify-github: read-only `gh api GET /repos/<repo>/pulls/<n>` must show an open, non-draft, same-repository PR
        whose head is exactly --sha and that carries the exact label `to be test`; otherwise nothing is minted.
        The token is written to --out (default <secrets dir>/grants/<grant_id>.grant), O_EXCL, mode 600, and never
        printed; stdout carries only the public claims and the path. The key never appears in argv, stdout or logs.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "worker"))
from prqa_worker import run_grants  # noqa: E402

KEY_NAME = "PRQA_RUN_GRANT_KEY"
TRIGGER_LABEL = "to be test"
LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def secrets_file(env=os.environ) -> Path:
    return Path(env.get("PRQA_SECRETS_FILE") or Path.home() / ".config/prqa/secrets.env")


def read_key(path: Path) -> str:
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        m = LINE_RE.match(line.strip())
        if m and m.group(1) == KEY_NAME:
            return m.group(2).strip().strip('"').strip("'")
    return ""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)


def init_key(path: Path) -> str:
    """Returns "present" or "generated"; never the value."""
    if run_grants.KEY_RE.match(read_key(path)):
        return "present"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    lines = [l for l in lines if not (LINE_RE.match(l.strip()) and LINE_RE.match(l.strip()).group(1) == KEY_NAME)]
    lines += ["# single-run grant HMAC key (scripts/pipeline/prqa_authorize_run.py; worker only)", f"{KEY_NAME}={secrets.token_hex(32)}"]
    _atomic_write(path, "\n".join(lines) + "\n")
    return "generated"


def verify_on_github(repo: str, pr: int, sha: str, runner=subprocess.run) -> dict:
    """Read-only check through the authenticated gh CLI (GET only; the gh token is never read or forwarded)."""
    proc = runner(["gh", "api", "--method", "GET", f"/repos/{repo}/pulls/{pr}"], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise SystemExit(f"refused: gh api failed (rc {proc.returncode})")
    data = json.loads(proc.stdout)
    labels = [l.get("name") for l in data.get("labels") or []]
    facts = {"state": data.get("state"), "draft": bool(data.get("draft")), "head_sha": (data.get("head") or {}).get("sha"),
             "head_repo": ((data.get("head") or {}).get("repo") or {}).get("full_name"), "base": (data.get("base") or {}).get("ref"),
             "labelled": TRIGGER_LABEL in labels, "title": data.get("title"), "url": data.get("html_url")}
    problems = [msg for bad, msg in (
        (facts["state"] != "open", "PR is not open"), (facts["draft"], "PR is a draft"),
        (facts["head_repo"] != repo, "PR head is a fork / another repository"),
        (facts["head_sha"] != sha, "PR head moved: --sha is stale"), (not facts["labelled"], f"label '{TRIGGER_LABEL}' missing"))
        if bad]
    if problems:
        raise SystemExit("refused: " + "; ".join(problems))
    return facts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init-key", action="store_true")
    ap.add_argument("--repo")
    ap.add_argument("--pr", type=int)
    ap.add_argument("--sha")
    ap.add_argument("--level", default="standard", choices=run_grants.LEVELS)
    ap.add_argument("--ttl", type=int, default=900)
    ap.add_argument("--verify-github", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    path = secrets_file()
    if args.init_key:
        print(json.dumps({"secrets_file": str(path), KEY_NAME: init_key(path)}))
        return 0
    if not (args.repo and args.pr and args.sha):
        ap.error("--repo, --pr and --sha are required")
    key = read_key(path)
    if not run_grants.KEY_RE.match(key):
        raise SystemExit(f"refused: {KEY_NAME} missing or malformed in {path} (run --init-key, then restart the worker)")
    facts = verify_on_github(args.repo, args.pr, args.sha) if args.verify_github else None
    token, claims = run_grants.mint(key, repository=args.repo, pr_number=args.pr, head_sha=args.sha, now=int(time.time()),
                                    ttl_s=args.ttl, test_level=args.level)
    out = Path(args.out) if args.out else path.parent / "grants" / f"{claims['grant_id']}.grant"
    out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    print(json.dumps({"grant": run_grants.public_claims(claims), "token_file": str(out), "github": facts}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
