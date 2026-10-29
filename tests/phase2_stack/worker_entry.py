"""Worker entry point for the DISPOSABLE Phase-2 container test (tests/test_phase2_containers.py) — never used elsewhere.

Runs the production worker code (mounted read-only from worker/) inside the prqa/worker image with the test-only
live configuration of tests/test_launch_lifecycle.live_env (placeholder repository approved, synthetic tokens) and the
git checkout replaced by the synthetic tree of _FakeCheckout, so a live job reaches launch_state=pending without any
network access. The disposable compose project puts this container on internal networks only; it holds no real
credential, and the Docker-access startup guard (PRQA_ENFORCE_NO_DOCKER) stays on.
"""
from __future__ import annotations

import logging
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "worker"), str(ROOT)]

import prqa_worker.executor as ex  # noqa: E402
from prqa_worker.server import App, Handler  # noqa: E402
from tests.test_launch_lifecycle import _FakeCheckout, live_env  # noqa: E402


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    ex.run_checkout = _FakeCheckout()
    app = App(live_env("/data", PRQA_ENFORCE_NO_DOCKER="true"))
    Handler.app = app
    app.executor.start()
    httpd = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    logging.getLogger("prqa.p2it").info("disposable phase-2 worker: AUTOMATION_MODE=%s (test-only live_env)", app.policy.automation_mode)
    try:
        httpd.serve_forever()
    finally:
        app.executor.stop()


if __name__ == "__main__":
    main()
