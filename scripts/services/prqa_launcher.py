#!/usr/bin/env python3
"""prqa-launcher entry point (Phase 2; never started automatically).

    python3 scripts/services/prqa_launcher.py --once            # one pass: claim + execute pending live jobs, then reconcile
    python3 scripts/services/prqa_launcher.py --interval 15     # foreground loop
    python3 scripts/services/prqa_launcher.py --reconcile-only  # orphan recovery only (dry-run unless --apply)
    python3 scripts/services/prqa_launcher.py --check           # verify image pin + sandbox network, launch nothing

Environment: PRQA_WORKER_URL, PRQA_LAUNCHER_TOKEN, PRQA_LAUNCHER_ID, PRQA_SANDBOX_IMAGE, PRQA_SANDBOX_IMAGE_ID (pinned),
PRQA_SANDBOX_NETWORK, DOCKER_HOST (the policy proxy socket), optional PRQA_MODEL_* (see launcher_service.py).
The process holds no GitHub, Discord, OpenRouter or target-API credential.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# In the repository the worker package is at <root>/worker; in the launcher image it sits next to this file (/app).
_here = Path(__file__).resolve()
if len(_here.parents) > 2:
    sys.path.insert(0, str(_here.parents[2] / "worker"))

from prqa_worker import launcher as lifecycle  # noqa: E402
from prqa_worker.launcher_service import LauncherConfig, LauncherError, LauncherService  # noqa: E402

log = logging.getLogger("prqa.launcher.cli")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=0.0)
    ap.add_argument("--reconcile-only", action="store_true")
    ap.add_argument("--apply", action="store_true", help="with --reconcile-only: actually remove orphans")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    try:
        cfg = LauncherConfig.from_env(dict(os.environ))
        svc = LauncherService(cfg)
    except LauncherError as exc:
        print(f"launcher configuration error: {exc}", file=sys.stderr)
        return 2
    if args.check:
        try:
            print(json.dumps({"image_id": svc.verify_image(), "network": svc.verify_network(), "launcher_id": cfg.launcher_id}))
            return 0
        except LauncherError as exc:
            print(f"check failed: {exc}", file=sys.stderr)
            return 1
    if args.reconcile_only:
        report = svc.reconcile(dry_run=not args.apply)
        print(lifecycle.describe(report))
        return 1 if report.failed else 0
    while True:
        try:
            outcomes = svc.run_once()
            for o in outcomes:
                log.info("job %s: %s %s", o.job_id, o.outcome, json.dumps(o.detail)[:300])
            rep = svc.reconcile(dry_run=False)
            if rep.removed or rep.failed:
                log.info("reconcile: removed=%d failed=%d", len(rep.removed), len(rep.failed))
        except LauncherError as exc:
            log.error("launcher pass aborted: %s", exc)
        if args.once or args.interval <= 0:
            return 0
        time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    sys.exit(main())
