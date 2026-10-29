#!/usr/bin/env python3
"""Launcher-side reconciliation of orphaned PRQA sandbox resources (containers/volumes labelled prqa.job + prqa.kind).

    python3 scripts/pipeline/prqa_sandbox_reconcile.py --dry-run        # list what would be removed (default is also dry-run)
    python3 scripts/pipeline/prqa_sandbox_reconcile.py --apply          # remove resources of terminal / stale-unknown jobs
    python3 scripts/pipeline/prqa_sandbox_reconcile.py --cancel <job>   # terminate one job's sandbox, helper and volume now

Runs on a host with Docker access (the worker container has none by design). Job status comes from the worker API on
loopback (`GET /jobs/<id>` with X-Worker-Token from compose/.env); an unreachable worker makes every job "unknown",
which only ever removes resources older than --grace seconds. Nothing here starts a background process.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "scripts" / "setup"))

from prqa_worker import launcher  # noqa: E402

WORKER = "http://127.0.0.1:8080"


def worker_token() -> str:
    try:
        from n8n_api import env  # type: ignore
        return env().get("PRQA_WORKER_TOKEN", "")
    except Exception:  # noqa: BLE001
        return ""


def job_status_via_api(token: str):
    def lookup(job_id: str) -> str | None:
        if not token:
            return None
        req = urllib.request.Request(f"{WORKER}/jobs/{urllib.request.quote(job_id)}", headers={"X-Worker-Token": token})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))["job"]["status"]
        except urllib.error.HTTPError as err:
            return None if err.code == 404 else None
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            return None
    return lookup


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="actually remove (default: dry run)")
    ap.add_argument("--dry-run", action="store_true", help="explicit dry run")
    ap.add_argument("--grace", type=float, default=launcher.DEFAULT_GRACE_S, help="seconds before an unknown job's resources may be removed")
    ap.add_argument("--cancel", metavar="JOB_ID", help="terminate this job's sandbox/helper/volume immediately")
    args = ap.parse_args(argv)
    if args.cancel:
        print(json.dumps(launcher.cancel_sandbox(args.cancel), indent=1))
        return 0
    report = launcher.reconcile(job_status=job_status_via_api(worker_token()), grace_s=args.grace, dry_run=not args.apply or args.dry_run)
    print(launcher.describe(report))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
