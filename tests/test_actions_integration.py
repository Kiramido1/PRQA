"""GitHub Actions → PRQA webhook → pipeline integration tests. SYNTHETIC, no network, no real service.

Covers: signed/unsigned/expired/replayed requests, field validation and propagation, idempotency, rate limiting,
safe logging, the embedded Actions client (timeouts, HTTP errors, no secret leakage), the generated workflow file,
and the pipeline semantics (accepted ≠ passed, wait for the job, no duplicate jobs).

Run: cd "<project>" && PYTHONPATH=worker python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from prqa_worker.actions_intake import RateLimiter, derive_idempotency_key, sign_actions_request  # noqa: E402
from prqa_worker.server import App  # noqa: E402
from prqa_worker.validation import SchemaStore  # noqa: E402

FIX = ROOT / "fixtures" / "synthetic"
SECRET = "synthetic-actions-secret-0123456789abcdef0123456789abcdef"
NOW = 1_800_000_000
SHA_HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
SHA_BASE = "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432"


def actions_payload(repository: str = "synthetic-org/sample-orders-api", number: int = 101, *, level: str = "standard",
                    force: bool = False, **overrides) -> dict:
    p = {
        "schema_version": "1.0",
        "source": "github_actions",
        "repository": {"full_name": repository, "html_url": f"https://github.com/{repository}"},
        "pull_request": {
            "number": number, "title": "Add status filter (SYNTHETIC) @everyone **bold**", "body": "desc", "state": "open", "draft": False,
            "html_url": f"https://github.com/{repository}/pull/{number}", "user": {"login": "synthetic-dev"},
            "labels": [{"name": "to be test"}],
            "head": {"ref": "feature/x", "sha": SHA_HEAD, "repo": {"full_name": repository, "fork": False}},
            "base": {"ref": "main", "sha": SHA_BASE, "repo": {"full_name": repository}},
        },
        "request": {"test_level": level, "trigger": "label", "label": "to be test", "requested_by": "synthetic-qa-lead", "force_rerun": force},
        "actions": {"run_id": "123456789", "run_attempt": "1", "run_url": f"https://github.com/{repository}/actions/runs/123456789",
                    "workflow": "PRQA · PR QA Pilot", "event_name": "pull_request", "client_version": "1.0"},
    }
    for key, value in overrides.items():
        cur = p
        parts = key.split(".")
        for part in parts[:-1]:
            cur = cur[part]
        cur[parts[-1]] = value
    return p


def signed(payload: dict, *, secret: str = SECRET, ts: int = NOW, signature: str | None = None, key: str | None = None) -> dict:
    import base64
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return {"body_b64": base64.b64encode(raw).decode(), "signature": signature if signature is not None else sign_actions_request(secret, ts, raw),
            "timestamp": str(ts), "idempotency_key": key or "", "user_agent": "prqa-actions-client/1.0"}


def make_app(tmp: str, **extra_env) -> App:
    env = {
        "AUTOMATION_MODE": "review_only", "MOCK_SERVICES_URL": "http://prqa-mocks:8090",
        "PRQA_WORKER_TOKEN": "test-token", "PRQA_REGISTRY_PATH": str(ROOT / "config/repositories.yaml"),
        "PRQA_SCHEMA_DIR": str(ROOT / "schemas"), "PRQA_PROMPTS_DIR": str(ROOT / "prompts"), "PRQA_FIXTURES_DIR": str(ROOT / "fixtures"),
        "PRQA_DATA_DIR": tmp, "MOCK_EXECUTION_DELAY_S": "0", "PRQA_WEBHOOK_SECRET": SECRET,
    }
    env.update(extra_env)
    app = App(env)
    app.intake.clock = lambda: NOW
    return app


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = make_app(self.tmp.name)
        self.intake = self.app.intake

    def tearDown(self):
        self.app.executor.stop()
        self.tmp.cleanup()

    def test_valid_signed_request_accepted(self):
        out = self.intake.handle(signed(actions_payload()))
        self.assertEqual((out.http_status, out.outcome), (202, "accepted"), out.reason)
        seed = out.body["job_seed"]
        self.assertEqual(seed["repository"], "synthetic-org/sample-orders-api")
        self.assertEqual(seed["pr"]["number"], 101)
        self.assertEqual(seed["pr"]["head_sha"], SHA_HEAD)
        self.assertEqual(seed["pr"]["url"], "https://github.com/synthetic-org/sample-orders-api/pull/101")
        self.assertEqual(seed["trigger"]["source"], "github_actions")
        self.assertEqual(seed["trigger"]["label"], "qa-test:standard")
        self.assertEqual(seed["trigger"]["requested_label"], "to be test")
        self.assertEqual(seed["trigger"]["run_url"], "https://github.com/synthetic-org/sample-orders-api/actions/runs/123456789")
        self.assertEqual(out.body["idempotency_key"], derive_idempotency_key("synthetic-org/sample-orders-api", 101, SHA_HEAD, "standard"))
        self.assertTrue(out.body["policy_preview"]["expected_decision"]["allowed"])  # synthetic → mock execution
        self.assertNotIn("result", out.body)  # accepted ≠ tested

    def test_missing_signature_rejected(self):
        for field in ("signature", "timestamp"):
            req = signed(actions_payload()); req[field] = ""
            out = self.intake.handle(req)
            self.assertEqual((out.http_status, out.outcome, out.reason_code), (401, "unauthorized", "SIGNATURE_MISSING"))

    def test_malformed_and_invalid_signature_rejected(self):
        out = self.intake.handle(signed(actions_payload(), signature="sha256=nothex"))
        self.assertEqual((out.http_status, out.reason_code), (401, "SIGNATURE_MALFORMED"))
        out = self.intake.handle(signed(actions_payload(), signature="sha1=" + "0" * 40))
        self.assertEqual(out.reason_code, "SIGNATURE_MALFORMED")
        out = self.intake.handle(signed(actions_payload(), secret="wrong-secret-wrong-secret-wrong-secret-0000"))
        self.assertEqual((out.http_status, out.reason_code), (401, "SIGNATURE_INVALID"))
        # a signature for a different body (tampering) is invalid too
        good = signed(actions_payload())
        tampered = signed(actions_payload(number=102)); tampered["signature"] = good["signature"]
        self.assertEqual(self.intake.handle(tampered).reason_code, "SIGNATURE_INVALID")
        # signature does not cover a different timestamp
        req = signed(actions_payload()); req["timestamp"] = str(NOW + 1)
        self.assertEqual(self.intake.handle(req).reason_code, "SIGNATURE_INVALID")

    def test_expired_and_future_timestamp_rejected(self):
        for ts in (NOW - 301, NOW + 301):
            out = self.intake.handle(signed(actions_payload(), ts=ts))
            self.assertEqual((out.http_status, out.reason_code), (401, "TIMESTAMP_EXPIRED"), ts)
        out = self.intake.handle(signed(actions_payload(), ts=NOW - 299))
        self.assertEqual(out.http_status, 202)
        req = signed(actions_payload()); req["timestamp"] = "yesterday"
        self.assertEqual(self.intake.handle(req).reason_code, "TIMESTAMP_MALFORMED")

    def test_replay_of_accepted_request_is_duplicate_not_new_job(self):
        req = signed(actions_payload())
        first = self.intake.handle(req)
        self.assertEqual(first.http_status, 202)
        replay = self.intake.handle(req)
        self.assertEqual((replay.http_status, replay.outcome, replay.reason_code), (200, "duplicate", "DUPLICATE_REQUEST"))
        self.assertEqual(replay.body["idempotency_key"], first.body["idempotency_key"])
        self.assertNotIn("job_seed", replay.body)
        # and the same request replayed after the window is simply expired
        self.intake.clock = lambda: NOW + 1000
        self.assertEqual(self.intake.handle(req).reason_code, "TIMESTAMP_EXPIRED")

    def test_secret_not_configured_fails_closed(self):
        self.app.executor.stop()
        app = make_app(self.tmp.name + "/x", PRQA_WEBHOOK_SECRET="")
        try:
            out = app.intake.handle(signed(actions_payload(), secret=""))
            self.assertEqual((out.http_status, out.reason_code), (401, "WEBHOOK_SECRET_NOT_CONFIGURED"))
        finally:
            app.executor.stop()

    def test_constant_time_comparison_used(self):
        import inspect
        from prqa_worker import actions_intake
        src = inspect.getsource(actions_intake.ActionsIntake.verify)
        self.assertIn("hmac.compare_digest", src)
        self.assertNotIn("expected ==", src)


class PayloadValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = make_app(self.tmp.name)
        self.intake = self.app.intake

    def tearDown(self):
        self.app.executor.stop()
        self.tmp.cleanup()

    def reject(self, payload, code, status=422):
        out = self.intake.handle(signed(payload))
        self.assertEqual((out.http_status, out.reason_code), (status, code), (out.reason, out.body))
        return out

    def test_schema_violations(self):
        self.reject(actions_payload(**{"pull_request.head.sha": "ZZZ"}), "PAYLOAD_INVALID")
        self.reject(actions_payload(**{"request.test_level": "extreme"}), "PAYLOAD_INVALID")
        self.reject(actions_payload(**{"repository.full_name": "../../etc"}), "PAYLOAD_INVALID")
        self.reject(actions_payload(**{"pull_request.number": -1}), "PAYLOAD_INVALID")
        bad = actions_payload(); bad["extra"] = {"$ref": "x"}
        self.reject(bad, "PAYLOAD_INVALID")
        import base64
        req = signed(actions_payload()); req["body_b64"] = base64.b64encode(b"not json").decode()
        req["signature"] = sign_actions_request(SECRET, NOW, b"not json")
        out = self.intake.handle(req)
        self.assertEqual(out.reason_code, "PAYLOAD_INVALID")

    def test_consistency_pr_url_repository_number(self):
        self.reject(actions_payload(**{"pull_request.html_url": "https://github.com/evil/repo/pull/101"}), "PAYLOAD_INCONSISTENT")
        self.reject(actions_payload(**{"pull_request.html_url": "https://github.com/synthetic-org/sample-orders-api/pull/7"}), "PAYLOAD_INCONSISTENT")
        self.reject(actions_payload(**{"repository.html_url": "https://github.com/other/repo"}), "PAYLOAD_INCONSISTENT")
        self.reject(actions_payload(**{"pull_request.base.repo.full_name": "other/repo"}), "PAYLOAD_INCONSISTENT")
        self.reject(actions_payload(**{"pull_request.base.sha": SHA_HEAD}), "PAYLOAD_INCONSISTENT")

    def test_ineligible_pr_states(self):
        self.reject(actions_payload(**{"pull_request.state": "closed"}), "PR_NOT_OPEN")
        self.reject(actions_payload(**{"pull_request.draft": True}), "PR_DRAFT")

    def test_unregistered_repository_forbidden(self):
        out = self.reject(actions_payload("someone/unknown-repo"), "REPOSITORY_NOT_REGISTERED", 403)
        self.assertEqual(out.outcome, "forbidden")

    def test_registered_real_repo_accepted_with_blocked_preview_in_review_only(self):
        out = self.intake.handle(signed(actions_payload("your-org/your-first-real-repo", 7)))
        self.assertEqual(out.http_status, 202)
        self.assertFalse(out.body["job_seed"]["synthetic"])
        preview = out.body["policy_preview"]
        self.assertTrue(preview["review_only"])
        self.assertFalse(preview["expected_decision"]["allowed"])
        self.assertEqual(preview["expected_decision"]["reason_code"], "REVIEW_ONLY_MODE")

    def test_fork_detection_and_untrusted_title_redaction(self):
        p = actions_payload(**{"pull_request.head.repo": {"full_name": "forker/sample-orders-api", "fork": True}})
        p["pull_request"]["title"] = "token ghp_SYNTHETICCANARY0000000000000000000000000 inside"
        out = self.intake.handle(signed(p))
        self.assertEqual(out.http_status, 202)
        self.assertTrue(out.body["job_seed"]["pr"]["is_fork"])
        self.assertNotIn("ghp_SYNTHETICCANARY", out.body["job_seed"]["pr"]["title"])

    def test_idempotency_key_header_must_match_derived_key(self):
        self.reject(actions_payload(), "IDEMPOTENCY_KEY_MISMATCH") if False else None
        req = signed(actions_payload(), key="gha:attacker-chosen-key-0001")
        out = self.intake.handle(req)
        self.assertEqual((out.http_status, out.reason_code), (422, "IDEMPOTENCY_KEY_MISMATCH"))
        self.assertEqual(out.body["expected_key"], derive_idempotency_key("synthetic-org/sample-orders-api", 101, SHA_HEAD, "standard"))
        ok = self.intake.handle(signed(actions_payload(), key=out.body["expected_key"]))
        self.assertEqual(ok.http_status, 202)

    def test_duplicate_key_same_sha_different_run_and_force_rerun(self):
        self.assertEqual(self.intake.handle(signed(actions_payload())).http_status, 202)
        again = actions_payload(); again["actions"]["run_id"] = "999"
        out = self.intake.handle(signed(again))
        self.assertEqual(out.outcome, "duplicate")                      # same repo#pr@sha:level → no new processing
        forced = self.intake.handle(signed(actions_payload(force=True)))
        self.assertEqual(forced.http_status, 202)                        # explicit re-run gets a distinct key
        self.assertTrue(forced.body["idempotency_key"].endswith(":run-123456789"))
        other_level = self.intake.handle(signed(actions_payload(level="smoke")))
        self.assertEqual(other_level.http_status, 202)
        self.assertEqual(other_level.body["job_seed"]["trigger"]["label"], "qa-test:smoke")

    def test_rate_limit(self):
        lim = RateLimiter(2, 60, clock=lambda: 100.0)
        self.assertTrue(lim.allow("r")[0]); self.assertTrue(lim.allow("r")[0])
        ok, retry = lim.allow("r")
        self.assertFalse(ok); self.assertGreater(retry, 0)
        self.assertTrue(lim.allow("other")[0])
        self.app.executor.stop()
        app = make_app(self.tmp.name + "/rl", PRQA_ACTIONS_RATE_LIMIT_PER_REPO="2")
        try:
            for i in range(2):
                p = actions_payload(**{"pull_request.head.sha": f"{i:040x}"})
                self.assertEqual(app.intake.handle(signed(p)).http_status, 202)
            out = app.intake.handle(signed(actions_payload(**{"pull_request.head.sha": "c" * 40})))
            self.assertEqual((out.http_status, out.outcome, out.reason_code), (429, "rate_limited", "RATE_LIMITED"))
            self.assertIn("retry_after_s", out.body)
        finally:
            app.executor.stop()

    def test_auth_failures_are_rate_limited_and_never_logged_with_secrets(self):
        with self.assertLogs("prqa", level="INFO") as cm:
            good = signed(actions_payload())
            self.assertEqual(self.intake.handle(good).http_status, 202)
            bad = signed(actions_payload(), secret="x" * 40)
            self.assertEqual(self.intake.handle(bad).http_status, 401)
        joined = "\n".join(cm.output)
        self.assertNotIn(SECRET, joined)
        self.assertNotIn(good["signature"], joined)
        self.assertNotIn(bad["signature"], joined)
        self.assertNotIn(good["body_b64"], joined)
        self.assertIn("SIGNATURE_INVALID", joined)
        events = self.app.store.events(limit=20)
        self.assertNotIn(SECRET, json.dumps(events))


class WorkerEndpointTests(unittest.TestCase):
    """Through App.route: the exact contract n8n uses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = make_app(self.tmp.name)

    def tearDown(self):
        self.app.executor.stop()
        self.tmp.cleanup()

    def call(self, method, path, body=None, token="test-token"):
        return self.app.route(method, path, {}, body, {"x-worker-token": token})

    def test_intake_requires_worker_token_and_returns_contract(self):
        self.assertEqual(self.call("POST", "/webhook/actions/intake", signed(actions_payload()), token="nope")[0], 401)
        status, body = self.call("POST", "/webhook/actions/intake", signed(actions_payload()))
        self.assertEqual(status, 202)
        self.assertEqual(body["outcome"], "accepted")
        for key in ("idempotency_key", "job_seed", "received_at", "automation_mode", "policy_preview"):
            self.assertIn(key, body)
        status, body = self.call("POST", "/webhook/actions/intake", signed(actions_payload()))
        self.assertEqual((status, body["outcome"]), (200, "duplicate"))
        status, body = self.call("POST", "/webhook/actions/intake", signed(actions_payload(), ts=NOW - 9999))
        self.assertEqual((status, body["outcome"]), (401, "unauthorized"))
        status, body = self.call("POST", "/webhook/actions/intake", signed(actions_payload("nobody/nowhere")))
        self.assertEqual((status, body["outcome"]), (403, "forbidden"))
        status, body = self.call("POST", "/webhook/actions/intake", signed(actions_payload(**{"pull_request.draft": True, "pull_request.head.sha": "d" * 40})))
        self.assertEqual((status, body["outcome"]), (422, "rejected"))
        # an exact repeat of an already-claimed key answers 'duplicate' before any further validation or rate-limit use
        status, body = self.call("POST", "/webhook/actions/intake", signed(actions_payload(**{"pull_request.draft": True})))
        self.assertEqual((status, body["outcome"]), (200, "duplicate"))

    def test_config_reports_intake_and_discord_state_without_secrets(self):
        status, cfg = self.call("GET", "/config")
        self.assertTrue(cfg["actions_intake"]["webhook_secret_configured"])
        self.assertEqual(cfg["actions_intake"]["timestamp_tolerance_s"], 300)
        self.assertEqual(cfg["discord"]["mode"], "mock")
        self.assertFalse(cfg["discord"]["real_delivery_possible"])
        self.assertNotIn(SECRET, json.dumps(cfg))

    def test_accepted_request_flows_into_a_single_job_and_job_request_schema(self):
        status, body = self.call("POST", "/webhook/actions/intake", signed(actions_payload()))
        seed = body["job_seed"]
        job_req = {
            "synthetic": seed["synthetic"], "repository": seed["repository"], "pr": seed["pr"], "test_level": seed["test_level"],
            "trigger": {"source": seed["trigger"]["source"], "delivery_id": body["idempotency_key"], "label": seed["trigger"]["label"],
                        "requested_label": seed["trigger"]["requested_label"], "requested_by": seed["trigger"]["requested_by"], "run_url": seed["trigger"]["run_url"]},
            "prompt": {"text": "x" * 300, "source": "fixture", "sha256": "a" * 64, "template_version": "v2"},
        }
        status, created = self.call("POST", "/jobs", job_req)
        self.assertEqual(status, 201, created)
        job = created["job"]
        self.assertEqual(job["request"]["trigger"]["source"], "github_actions")
        self.assertEqual(job["request"]["pr"]["head_sha"], SHA_HEAD)
        self.assertEqual(job["request"]["pr"]["url"], "https://github.com/synthetic-org/sample-orders-api/pull/101")
        self.assertEqual(job["request"]["trigger"]["run_url"], seed["trigger"]["run_url"])
        # the same idempotency key (delivery id) cannot create a second job; neither can the same repo#pr@sha:level
        status, dup = self.call("POST", "/jobs", job_req)
        self.assertEqual((status, dup["error"]), (409, "DUPLICATE_DELIVERY"))
        other = json.loads(json.dumps(job_req)); other["trigger"]["delivery_id"] = "gha:other-key-000000"
        status, dup2 = self.call("POST", "/jobs", other)
        self.assertEqual((status, dup2["error"]), (409, "DUPLICATE_JOB"))
        self.assertEqual(self.app.store.stats().get("queued"), 1)

    def test_accepted_webhook_does_not_imply_success_and_report_waits_for_job(self):
        status, body = self.call("POST", "/webhook/actions/intake", signed(actions_payload()))
        self.assertEqual(status, 202)
        self.assertNotIn("overall_status", json.dumps(body))
        seed = body["job_seed"]
        job_req = {"synthetic": True, "repository": seed["repository"], "pr": seed["pr"], "test_level": "standard",
                   "trigger": {"source": "github_actions", "delivery_id": body["idempotency_key"], "label": "qa-test:standard"},
                   "prompt": {"text": "x" * 300, "source": "fixture", "sha256": "a" * 64}}
        status, created = self.call("POST", "/jobs", job_req)
        job_id = created["job"]["id"]
        # job created ≠ job completed: polling shows queued, and the reporter refuses to report
        status, got = self.call("GET", f"/jobs/{job_id}")
        self.assertEqual(got["job"]["status"], "queued")
        self.assertIsNone(got["job"]["result"])
        status, resp = self.call("POST", "/notifications/discord", {"kind": "result", "job_id": job_id})
        self.assertEqual((status, resp["error"], resp["job_status"]), (409, "JOB_NOT_COMPLETED", "queued"))
        running = self.app.store.next_queued()
        status, resp = self.call("POST", "/notifications/discord", {"kind": "result", "job_id": job_id})
        self.assertEqual((status, resp["job_status"]), (409, "running"))
        # only once the executor has finished does the result exist and can be reported
        result = self.app.executor.execute(running)
        self.app.store.succeed(job_id, result)
        status, got = self.call("GET", f"/jobs/{job_id}")
        self.assertEqual(got["job"]["status"], "succeeded")
        self.assertEqual(got["job"]["result"]["pr"]["url"], "https://github.com/synthetic-org/sample-orders-api/pull/101")


# --------------------------------------------------------------------------------------- client ---
def load_client():
    spec = importlib.util.spec_from_file_location("prqa_actions_client", ROOT / "scripts" / "clients" / "prqa_actions_client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class _Resp:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def gh_pr(repo="example-org/orders-service", number=42, **over):
    pr = {"number": number, "title": "Add filter", "body": "x" * 9000, "state": "open", "draft": False,
          "html_url": f"https://github.com/{repo}/pull/{number}", "user": {"login": "dev"}, "labels": [{"name": "to be test"}],
          "head": {"ref": "feat", "sha": SHA_HEAD, "repo": {"full_name": repo, "fork": False}},
          "base": {"ref": "main", "sha": SHA_BASE, "repo": {"full_name": repo}}}
    pr.update(over)
    return pr


class ActionsClientTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out"
        self.summary = Path(self.tmp.name) / "summary"
        self.env = {
            "PRQA_WEBHOOK_URL": "https://prqa.example.org/webhook/prqa/actions", "PRQA_WEBHOOK_SECRET": SECRET,
            "GITHUB_TOKEN": "ghs_SYNTHETICRUNNERTOKEN00000000000000000000", "GITHUB_REPOSITORY": "example-org/orders-service",
            "GITHUB_API_URL": "https://api.github.com", "GITHUB_SERVER_URL": "https://github.com", "GITHUB_RUN_ID": "77",
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_WORKFLOW": "PRQA", "GITHUB_EVENT_NAME": "pull_request", "GITHUB_ACTOR": "dev",
            "PRQA_PR_NUMBER": "42", "PRQA_TEST_LEVEL": "standard", "PRQA_TRIGGER_LABEL": "to be test",
            "PRQA_HTTP_TIMEOUT_SECONDS": "7", "GITHUB_OUTPUT": str(self.out), "GITHUB_STEP_SUMMARY": str(self.summary),
        }
        self.calls = []
        self.sleeps = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_client(self, responder, env=None):
        def opener(req, timeout=None):
            self.calls.append({"url": req.full_url, "method": req.get_method(), "headers": dict(req.header_items()), "data": req.data, "timeout": timeout})
            return responder(req, len(self.calls))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.mod.main(env or self.env, opener=opener, sleep=self.sleeps.append, clock=lambda: NOW)
        return rc, buf.getvalue()

    def outputs(self):
        return dict(line.split("=", 1) for line in self.out.read_text().splitlines()) if self.out.exists() else {}

    def responder(self, prqa_status=202, prqa_body=None, pr=None):
        def respond(req, n):
            if "api.github.com" in req.full_url:
                return _Resp(200, json.dumps(pr or gh_pr()).encode())
            body = prqa_body if prqa_body is not None else {"outcome": "accepted", "receiver_execution_id": "55", "head_sha": SHA_HEAD, "automation_mode": "review_only",
                                                            "policy_preview": {"expected_decision": {"allowed": False, "reason_code": "REVIEW_ONLY_MODE", "reason": "review only"}}}
            return _Resp(prqa_status, json.dumps(body).encode())
        return respond

    def test_valid_request_signed_and_propagated(self):
        rc, log = self.run_client(self.responder())
        self.assertEqual(rc, 0, log)
        api_call, post = self.calls
        self.assertEqual(api_call["url"], "https://api.github.com/repos/example-org/orders-service/pulls/42")
        self.assertEqual(api_call["timeout"], 7)
        self.assertEqual(post["url"], "https://prqa.example.org/webhook/prqa/actions")
        self.assertEqual(post["headers"]["X-prqa-timestamp"], str(NOW))
        self.assertEqual(post["headers"]["X-prqa-idempotency-key"], f"gha:example-org/orders-service#42@{SHA_HEAD}:standard")
        body = json.loads(post["data"])
        self.assertEqual(body["pull_request"]["head"]["sha"], SHA_HEAD)
        self.assertEqual(body["pull_request"]["html_url"], "https://github.com/example-org/orders-service/pull/42")
        self.assertEqual(body["repository"]["full_name"], "example-org/orders-service")
        self.assertEqual(body["request"]["test_level"], "standard")
        self.assertEqual(body["request"]["trigger"], "label")
        self.assertEqual(body["actions"]["run_url"], "https://github.com/example-org/orders-service/actions/runs/77")
        self.assertEqual(len(body["pull_request"]["body"]), 8000)  # bounded description
        # the signature verifies with the worker's reference implementation over the exact bytes sent
        self.assertEqual(post["headers"]["X-prqa-signature"], sign_actions_request(SECRET, NOW, post["data"]))
        # the payload itself passes the worker's schema
        self.assertEqual(SchemaStore(ROOT / "schemas").errors(SchemaStore(ROOT / "schemas").actions_request, body), [])
        self.assertEqual(self.outputs()["outcome"], "accepted")
        self.assertEqual(self.outputs()["receiver_execution_id"], "55")
        self.assertIn("will be BLOCKED", log)          # review-only preview surfaced as a warning
        self.assertIn("does not mean the test passed", self.summary.read_text() + log)

    def test_workflow_dispatch_force_rerun_and_level(self):
        env = dict(self.env, GITHUB_EVENT_NAME="workflow_dispatch", PRQA_FORCE_RERUN="true", PRQA_TEST_LEVEL="security")
        rc, log = self.run_client(self.responder(), env)
        self.assertEqual(rc, 0, log)
        post = self.calls[1]
        self.assertTrue(post["headers"]["X-prqa-idempotency-key"].endswith(":security:run-77"))
        self.assertEqual(json.loads(post["data"])["request"]["trigger"], "workflow_dispatch")

    def test_end_to_end_against_worker_intake(self):
        """The client's bytes verified by the real worker intake (no n8n in between)."""
        tmp = tempfile.TemporaryDirectory()
        app = make_app(tmp.name)
        try:
            env = dict(self.env, GITHUB_REPOSITORY="synthetic-org/sample-orders-api", PRQA_PR_NUMBER="101")
            captured = {}

            def respond(req, n):
                if "api.github.com" in req.full_url:
                    return _Resp(200, json.dumps(gh_pr("synthetic-org/sample-orders-api", 101)).encode())
                import base64
                status, body = app.route("POST", "/webhook/actions/intake", {}, {
                    "body_b64": base64.b64encode(req.data).decode(), "signature": req.get_header("X-prqa-signature"),
                    "timestamp": req.get_header("X-prqa-timestamp"), "idempotency_key": req.get_header("X-prqa-idempotency-key")},
                    {"x-worker-token": "test-token"})
                captured["status"], captured["body"] = status, body
                return _Resp(status, json.dumps(body).encode())
            rc, log = self.run_client(respond, env)
            self.assertEqual(rc, 0, log)
            self.assertEqual(captured["status"], 202)
            self.assertEqual(captured["body"]["job_seed"]["pr"]["number"], 101)
            rc, log = self.run_client(respond, env)       # second run → duplicate, still exit 0 with a warning
            self.assertEqual(rc, 0)
            self.assertEqual(captured["status"], 200)
            self.assertIn("::warning::", log)
        finally:
            app.executor.stop(); tmp.cleanup()

    def test_invalid_config_and_pr_validation(self):
        for env, needle in (
            (dict(self.env, PRQA_WEBHOOK_URL=""), "PRQA_WEBHOOK_URL is not configured"),
            (dict(self.env, PRQA_WEBHOOK_URL="http://prqa.example.org/x"), "must be an https"),
            (dict(self.env, PRQA_WEBHOOK_SECRET=""), "PRQA_WEBHOOK_SECRET is not configured"),
            (dict(self.env, PRQA_PR_NUMBER="abc"), "positive integer"),
            (dict(self.env, PRQA_PR_NUMBER="0"), "positive integer"),
            (dict(self.env, PRQA_TEST_LEVEL="extreme"), "PRQA_TEST_LEVEL"),
            (dict(self.env, GITHUB_REPOSITORY="bad repo"), "GITHUB_REPOSITORY"),
        ):
            self.calls.clear()
            rc, log = self.run_client(self.responder(), env)
            self.assertEqual(rc, 1, env)
            self.assertIn(needle, log)
            self.assertEqual(self.calls, [], "no request may be sent on invalid config")
        for pr, needle in (
            (gh_pr(number=43), "expected #42"),
            (gh_pr(base={"ref": "main", "sha": SHA_BASE, "repo": {"full_name": "other/repo"}}), "belongs to"),
            (gh_pr(state="closed"), "only open"),
            (gh_pr(draft=True), "draft"),
            (gh_pr(head={"ref": "f", "sha": "nope", "repo": None}), "malformed"),
        ):
            self.calls.clear()
            rc, log = self.run_client(self.responder(pr=pr))
            self.assertEqual(rc, 1, needle)
            self.assertIn(needle, log)
            self.assertEqual(len(self.calls), 1, "PRQA must not be called for an invalid PR")

    def test_pr_not_found_and_github_api_error(self):
        def respond(req, n):
            return _Resp(404, b'{"message":"Not Found"}') if n == 1 else _Resp(202, b"{}")
        rc, log = self.run_client(respond)
        self.assertEqual(rc, 1); self.assertIn("does not exist", log)

        def respond_err(req, n):
            raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b""))
        self.calls.clear()
        rc, log = self.run_client(respond_err)
        self.assertEqual(rc, 1); self.assertIn("HTTP 500", log)

    def test_timeout_connection_failure_and_bounded_retries(self):

        def timeout(req, n):
            if "api.github.com" in req.full_url:
                return _Resp(200, json.dumps(gh_pr()).encode())
            raise TimeoutError("timed out")
        rc, log = self.run_client(timeout)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.calls), 1 + 3, "exactly max_attempts POSTs, no unlimited retries")
        self.assertEqual(len(self.sleeps), 2)
        self.assertIn("unreachable after 3 attempts", log)
        self.assertIn("prqa.example.org", log)
        self.assertNotIn("/webhook/prqa/actions", log.replace("prqa.example.org", ""))  # host only
        self.assertEqual(self.outputs()["outcome"], "client_error")

        def refused(req, n):
            if "api.github.com" in req.full_url:
                return _Resp(200, json.dumps(gh_pr()).encode())
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        self.calls.clear(); self.sleeps.clear()
        rc, log = self.run_client(refused)
        self.assertEqual(rc, 1)
        self.assertIn("URLError", log)

    def test_http_errors_and_retryable_statuses(self):
        def flaky(req, n):
            if "api.github.com" in req.full_url:
                return _Resp(200, json.dumps(gh_pr()).encode())
            if n == 2:
                return _Resp(503, b'{"outcome":"error"}')
            if n == 3:
                raise urllib.error.HTTPError(req.full_url, 429, "rate", {"Retry-After": "2"}, io.BytesIO(b'{"outcome":"rate_limited","retry_after_s":2}'))
            return _Resp(202, json.dumps({"outcome": "accepted", "receiver_execution_id": "9"}).encode())
        rc, log = self.run_client(flaky)
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.sleeps, [5.0, 2.0])       # 503 → backoff; 429 → Retry-After honoured
        for status, body, needle in (
            (401, {"outcome": "unauthorized", "reason_code": "SIGNATURE_INVALID", "reason": "signature mismatch"}, "PRQA_WEBHOOK_SECRET equals"),
            (403, {"outcome": "forbidden", "reason_code": "REPOSITORY_NOT_REGISTERED", "reason": "not registered"}, "register example-org/orders-service"),
            (422, {"outcome": "rejected", "reason_code": "PR_DRAFT", "reason": "draft", "errors": ["x"]}, "PR_DRAFT"),
            (500, {"error": "INTERNAL"}, "HTTP 500"),
        ):
            self.calls.clear(); self.sleeps.clear()
            rc, log = self.run_client(self.responder(status, body))
            self.assertEqual(rc, 1, (status, log))
            self.assertIn("::error::", log)
            self.assertIn(needle, log)
            self.assertEqual(self.outputs().get("outcome"), body.get("outcome", f"http_{status}"))

    def test_no_secret_or_signature_leaks_in_logs_outputs_or_summary(self):
        rc, log = self.run_client(self.responder())
        everything = log + self.out.read_text() + self.summary.read_text()
        self.assertNotIn(SECRET, everything)
        self.assertNotIn(self.env["GITHUB_TOKEN"], everything)
        self.assertNotIn(self.calls[1]["headers"]["X-prqa-signature"], everything)
        self.assertNotIn("Authorization", everything)
        self.assertNotIn("Bearer", everything)
        # and the same holds on every failure path
        for responder in (self.responder(401, {"outcome": "unauthorized", "reason": SECRET[:10]}), self.responder(pr=gh_pr(draft=True))):
            self.calls.clear()
            rc, log = self.run_client(responder)
            self.assertNotIn(SECRET, log + self.out.read_text() + self.summary.read_text())


class WorkflowFileTests(unittest.TestCase):
    def setUp(self):
        import yaml
        self.text = (ROOT / ".github" / "workflows" / "prqa.yml").read_text(encoding="utf-8")
        self.wf = yaml.safe_load(self.text)

    def test_generated_file_matches_generator_and_embeds_client_verbatim(self):
        proc = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "build" / "build_actions_workflow.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        client = (ROOT / "scripts" / "clients" / "prqa_actions_client.py").read_text(encoding="utf-8").rstrip("\n")
        run = self.wf["jobs"]["request-qa"]["steps"][1]["run"]
        embedded = run.split("<<'PRQA_CLIENT'\n", 1)[1].rsplit("\nPRQA_CLIENT", 1)[0]
        self.assertEqual(embedded.rstrip("\n"), client)

    def test_triggers_permissions_and_safety_properties(self):
        on = self.wf.get("on") or self.wf.get(True)
        self.assertIn("workflow_dispatch", on)
        self.assertEqual(on["workflow_dispatch"]["inputs"]["pr_number"]["required"], True)
        self.assertEqual(on["pull_request"]["types"], ["labeled"])
        self.assertEqual(self.wf["permissions"], {"contents": "read", "pull-requests": "read"})
        job = self.wf["jobs"]["request-qa"]
        self.assertIn("to be test", job["if"])
        self.assertIn("vars.PRQA_TRIGGER_LABEL", job["if"])
        self.assertEqual(job["timeout-minutes"], 10)
        self.assertEqual(self.wf["concurrency"]["cancel-in-progress"], False)
        self.assertNotIn("actions/checkout", self.text)            # PR code is never checked out or executed
        self.assertNotIn("pull_request_target", self.text)
        self.assertNotIn("curl -v", self.text)
        self.assertNotIn("set -x", self.text)
        env = job["steps"][1]["env"]
        self.assertEqual(env["PRQA_WEBHOOK_SECRET"], "${{ secrets.PRQA_WEBHOOK_SECRET }}")
        self.assertNotIn("echo $PRQA_WEBHOOK_SECRET", self.text)
        import re
        code_only = "\n".join(line for line in self.text.splitlines() if not line.lstrip().startswith("#"))
        self.assertIsNone(re.search(r"https://[a-z0-9.-]+\.(ngrok|trycloudflare|example)\.(io|com|org|app)/", code_only),
                          "no hard-coded receiver URL outside comments")
        self.assertEqual(env["PRQA_WEBHOOK_URL"], "${{ secrets.PRQA_WEBHOOK_URL || vars.PRQA_WEBHOOK_URL }}")

    def test_embedded_step_runs_and_fails_closed_without_configuration(self):
        run = self.wf["jobs"]["request-qa"]["steps"][1]["run"]
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PATH": os.environ.get("PATH", ""), "GITHUB_OUTPUT": tmp + "/out", "GITHUB_STEP_SUMMARY": tmp + "/sum", "HOME": tmp}
            proc = subprocess.run(["bash", "-e", "-c", run], env=env, capture_output=True, text=True, cwd=tmp, timeout=60)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("::error::PRQA client: PRQA_WEBHOOK_URL is not configured", proc.stdout)
            self.assertEqual(proc.stderr, "")


class PipelineStructureTests(unittest.TestCase):
    """The generated n8n workflows keep the required execution order and waiting semantics."""

    def load(self, name):
        return json.loads((ROOT / "n8n" / "workflows" / name).read_text(encoding="utf-8"))

    @staticmethod
    def edges(wf):
        out = []
        for src, conns in wf["connections"].items():
            for idx, branch in enumerate(conns["main"]):
                for e in branch:
                    out.append((src, idx, e["node"]))
        return out

    def test_receiver_has_actions_entry_wired_to_worker_intake_and_main_pipeline(self):
        wf = self.load("01-prqa-github-webhook-receiver.json")
        nodes = {n["name"]: n for n in wf["nodes"]}
        hook = nodes["GitHub Actions Webhook (POST /prqa/actions)"]
        self.assertEqual(hook["parameters"]["path"], "prqa/actions")
        self.assertTrue(hook["parameters"]["options"]["rawBody"])
        intake = nodes["Verify, Validate & Claim (worker /webhook/actions/intake)"]
        self.assertIn("/webhook/actions/intake", intake["parameters"]["url"])
        self.assertTrue(intake["parameters"]["options"]["response"]["response"]["neverError"])
        self.assertNotIn("retryOnFail", intake)                  # a retried intake could turn an accepted request into a duplicate
        e = self.edges(wf)
        self.assertIn(("Route Intake Outcome", 0, "Build Pipeline Input (Actions)"), e)
        self.assertIn(("Build Pipeline Input (Actions)", 0, "Respond 202 Accepted (Actions)"), e)
        self.assertIn(("Respond 202 Accepted (Actions)", 0, "Start Main Pipeline (async)"), e)
        for out_idx, name in ((1, "Respond 200 Duplicate (Actions)"), (2, "Respond 401 Unauthorized (Actions)"),
                              (3, "Respond 403 Repository Not Registered (Actions)"), (4, "Respond 422 Invalid Request (Actions)"),
                              (5, "Respond 429 Rate Limited (Actions)"), (6, "Respond 200 Lifecycle Cancelled (Actions)"),
                              (7, "Respond 200 Blocked by Policy (Actions)"), (8, "Respond 409 Delivery Replayed (Actions)"),
                              (9, "Respond 413 Payload Too Large (Actions)"), (10, "Respond 503 Context Credential Missing (Actions)"),
                              (11, "Respond 503 Worker Unavailable (Actions)")):
            self.assertIn(("Route Intake Outcome", out_idx, name), e)
            self.assertNotIn((name, 0, "Start Main Pipeline (async)"), e)   # only the 202 path starts the pipeline
        # the existing GitHub path is untouched
        self.assertIn(("Respond 202 Accepted", 0, "Start Main Pipeline (async)"), e)
        self.assertIn(("Signature Valid?", 1, "Respond 401 Invalid Signature"), e)
        self.assertFalse(nodes["Start Main Pipeline (async)"]["parameters"]["options"]["waitForSubWorkflow"])
        real_nodes = [n for n in wf["nodes"] if n["type"] != "n8n-nodes-base.stickyNote"]
        self.assertNotIn("$env.PRQA_WEBHOOK_SECRET", json.dumps(real_nodes))   # the Actions secret is never read by n8n (comments may name it)

    def test_main_pipeline_order_job_then_validate_then_report(self):
        wf = self.load("00-prqa-main-pipeline.json")
        e = self.edges(wf)
        self.assertIn(("Stage 02 · Collect PR Context", 0, "Stage 03 · Build QA Prompt (LLM)"), e)
        self.assertIn(("Stage 03 · Build QA Prompt (LLM)", 0, "Stage 04 · Dispatch OpenCode Job"), e)
        self.assertIn(("Stage 04 · Dispatch OpenCode Job", 0, "Stage 05 · Validate Result & Render Report"), e)
        self.assertIn(("Stage 05 · Validate Result & Render Report", 0, "Stage 06 · Discord Report"), e)
        self.assertNotIn(("Stage 04 · Dispatch OpenCode Job", 0, "Stage 06 · Discord Report"), e)   # never report before validation
        self.assertIn(("Record Blocked Decision (worker /events)", 0, "Notify Blocked Decision (worker → Discord)"), e)
        for stage in ("Stage 04 · Dispatch OpenCode Job", "Stage 05 · Validate Result & Render Report", "Stage 06 · Discord Report"):
            self.assertIn((stage, 1, "Stage Failed → Sanitize"), e)        # error outputs captured
        self.assertEqual(wf["settings"]["errorWorkflow"], "PrqaErrorHandler")

    def test_dispatcher_waits_for_terminal_job_status(self):
        wf = self.load("04-prqa-opencode-job-dispatcher.json")
        nodes = {n["name"]: n for n in wf["nodes"]}
        e = self.edges(wf)
        self.assertIn(("Extract Job ID", 0, "Wait 10 s"), e)
        self.assertIn(("Wait 10 s", 0, "Poll Job Status (worker)"), e)
        self.assertIn(("Poll Job Status (worker)", 0, "Job Finished?"), e)
        self.assertIn(("Job Finished?", 0, "Attach Job Result to Pipeline Data"), e)      # succeeded
        self.assertIn(("Job Finished?", 3, "Poll Limit Reached? (> 360 polls ≈ 60 min)"), e)  # queued/running → keep polling
        self.assertIn(("Poll Limit Reached? (> 360 polls ≈ 60 min)", 1, "Wait 10 s"), e)
        self.assertIn(("Poll Limit Reached? (> 360 polls ≈ 60 min)", 0, "Stop: Poll Timeout"), e)  # bounded
        rules = nodes["Job Finished?"]["parameters"]["rules"]["values"]
        self.assertEqual([r["outputKey"] for r in rules], ["succeeded", "failed", "cancelled"])
        self.assertNotIn(("Submit Job to Worker (policy gate + dedupe)", 0, "Attach Job Result to Pipeline Data"), e)  # 201 ≠ done

    def test_discord_reporter_delegates_to_worker_and_never_sends_from_n8n(self):
        wf = self.load("06-prqa-discord-reporter.json")
        nodes = {n["name"]: n for n in wf["nodes"]}
        deliver = nodes["Deliver Discord Report (worker /notifications/discord)"]
        self.assertIn("/notifications/discord", deliver["parameters"]["url"])
        self.assertIn("kind: 'result'", deliver["parameters"]["jsonBody"])
        self.assertIn("result_validation", deliver["parameters"]["jsonBody"])
        real_nodes = [n for n in wf["nodes"] if n["type"] != "n8n-nodes-base.stickyNote"]
        self.assertNotIn("DISCORD_WEBHOOK_URL", json.dumps(real_nodes))   # no n8n node posts to Discord any more
        self.assertNotIn("discord.com", json.dumps(real_nodes))
        e = self.edges(wf)
        self.assertIn(("Delivered?", 0, "Discord: delivered ✓"), e)
        self.assertIn(("Delivered?", 1, "Discord: failed / skipped (result unchanged)"), e)


if __name__ == "__main__":
    unittest.main()
