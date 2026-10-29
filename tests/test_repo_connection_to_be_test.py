"""Per-repository connection + automatic `to be test` GitHub Actions trigger. SYNTHETIC, offline, no real service.

Covers: every label/action case (client policy and receiver), HMAC validation, timestamp expiry, delivery replay,
connection/repository binding, payload validation and size limits, head-SHA idempotency, missing URL/secret/connection id,
HTTPS/private-address validation of the ingress URL, the generated workflow (YAML syntax, no checkout, no
pull_request_target, permissions {}, no ${{ }} in the script, embedded client verbatim), no secrets in logs or artifacts,
the missing GitHub read credential, context-builder integration in synthetic mode, review-only enforcement, the
provider budget guard (no network, no LLM call), the n8n route for the new outcomes and the nine inactive workflows.

Run: cd "<project>" && PYTHONPATH=worker python3 -m unittest tests.test_repo_connection_to_be_test -v
"""
from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "scripts" / "setup"))

from prqa_worker import actions_connections as conns  # noqa: E402
from prqa_worker.actions_intake import sign_actions_request  # noqa: E402
from prqa_worker.registry import Registry  # noqa: E402
from prqa_worker.server import App  # noqa: E402

NOW = 1_800_000_000
SYN_REPO = "synthetic-org/sample-orders-api"
REAL_REPO = "acme-labs/orders-service"
SHA_HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
SHA_HEAD2 = "b2c3d4e5f60718293a4b5c6d7e8f901234567890"
SHA_BASE = "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432"
CID_SYN = "prqa_c_" + "1" * 24
CID_REAL = "prqa_c_" + "2" * 24
CID_OTHER = "prqa_c_" + "3" * 24
SECRET_SYN = "5" * 64
SECRET_REAL = "6" * 64
SECRET_OTHER = "7" * 64
URL = "https://prqa-ingress.acme-labs.io/webhook/prqa/actions"


def load_client():
    spec = importlib.util.spec_from_file_location("prqa_to_be_test_client", ROOT / "scripts" / "clients" / "prqa_to_be_test_client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def load_cli():
    spec = importlib.util.spec_from_file_location("prqa_connect_repository", ROOT / "scripts" / "setup" / "prqa_connect_repository.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


CLIENT = load_client()
CLI = load_cli()


def write_registry_file(directory: Path, connections: dict) -> None:
    directory.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chmod(directory, 0o750)
    (directory / conns.CONNECTIONS_FILE).write_text(json.dumps({"version": 1, "connections": connections}), encoding="utf-8")


def default_connections() -> dict:
    return {CID_SYN: {"repository": SYN_REPO, "secret": SECRET_SYN, "status": "active"},
            CID_REAL: {"repository": REAL_REPO, "secret": SECRET_REAL, "status": "active"},
            CID_OTHER: {"repository": "acme-labs/other-repo", "secret": SECRET_OTHER, "status": "active"}}


def gh_event(action="labeled", *, repo=SYN_REPO, number=101, label="to be test", labels=("to be test",), draft=False,
             state="open", head_sha=SHA_HEAD, fork=False, sender="synthetic-dev") -> dict:
    head_repo = "someone/fork-of-repo" if fork else repo
    ev = {"action": action, "number": number,
          "repository": {"full_name": repo, "id": 4242},
          "sender": {"login": sender},
          "pull_request": {"number": number, "state": state, "draft": draft, "title": "SYNTHETIC title", "body": "ignored",
                           "labels": [{"name": n} for n in labels],
                           "head": {"sha": head_sha, "ref": "feature/x", "repo": {"full_name": head_repo, "fork": fork}},
                           "base": {"sha": SHA_BASE, "ref": "main", "repo": {"full_name": repo}}}}
    if action in ("labeled", "unlabeled"):
        ev["label"] = {"name": label}
    return ev


def payload(*, cid=CID_SYN, repo=SYN_REPO, number=101, action="labeled", label="to be test", head_sha=SHA_HEAD,
            run_id="9001", attempt="1", created_at=NOW, **over) -> dict:
    p = {"schema_version": "2.0", "type": "prqa.to_be_test.dispatch", "connection_id": cid,
         "repository": {"full_name": repo, "id": 4242},
         "pull_request": {"number": number, "state": "open", "draft": False,
                          "head": {"sha": head_sha, "ref": "feature/x", "repo_full_name": repo},
                          "base": {"sha": SHA_BASE, "ref": "main"}},
         "event": {"name": "pull_request", "action": action, "label": label, "sender_login": "synthetic-dev"},
         "delivery": {"id": f"gha-{run_id}-{attempt}", "created_at": created_at},
         "actions": {"run_id": run_id, "run_attempt": attempt, "workflow": "PRQA · to be test", "client_version": "2.0"}}
    for key, value in over.items():
        cur = p
        parts = key.split(".")
        for part in parts[:-1]:
            cur = cur[part]
        cur[parts[-1]] = value
    return p


def signed(p: dict | bytes, secret: str = SECRET_SYN, ts: int = NOW, signature: str | None = None) -> dict:
    raw = p if isinstance(p, bytes) else json.dumps(p, separators=(",", ":"), sort_keys=True).encode()
    return {"body_b64": base64.b64encode(raw).decode(), "timestamp": str(ts),
            "signature": signature if signature is not None else sign_actions_request(secret, ts, raw), "user_agent": "prqa-to-be-test/2.0"}


def real_registry(tmp: Path) -> Path:
    """config/repositories.yaml + a disabled, pending real entry for REAL_REPO, appended by the CLI's own helper."""
    path = tmp / "repositories.yaml"
    shutil.copy(ROOT / "config" / "repositories.yaml", path)
    CLI.register_repository(path, REAL_REPO)
    return path


def make_app(tmp: Path, *, github_token: str = "", connections: dict | None = None, **extra) -> App:
    cdir = tmp / "connections"
    write_registry_file(cdir, default_connections() if connections is None else connections)
    env = {"AUTOMATION_MODE": "review_only", "MOCK_SERVICES_URL": "http://prqa-mocks:8090", "PRQA_WORKER_TOKEN": "test-token",
           "PRQA_REGISTRY_PATH": str(real_registry(tmp)), "PRQA_SCHEMA_DIR": str(ROOT / "schemas"),
           "PRQA_PROMPTS_DIR": str(ROOT / "prompts"), "PRQA_FIXTURES_DIR": str(ROOT / "fixtures"), "PRQA_DATA_DIR": str(tmp / "data"),
           "MOCK_EXECUTION_DELAY_S": "0", "PRQA_WEBHOOK_SECRET": "", "PRQA_ACTIONS_CONNECTIONS_PATH": str(cdir),
           "GITHUB_TOKEN": github_token}
    env.update(extra)
    (tmp / "data").mkdir(exist_ok=True)
    app = App(env)
    app.intake.clock = lambda: NOW
    return app


class _Base(unittest.TestCase):
    github_token = ""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.app = make_app(self.tmp, github_token=self.github_token)
        self.intake = self.app.intake

    def tearDown(self):
        self.app.executor.stop()
        self._tmp.cleanup()

    def h(self, req):
        out = self.intake.handle(req)
        return out.http_status, out.outcome, out.reason_code, out.body


# ------------------------------------------------------------------------------------------------ client policy --
class LabelPolicyTests(unittest.TestCase):
    """The exact label/action matrix, decided by the client from the event JSON."""

    def d(self, ev, name="pull_request"):
        return CLIENT.decide(name, ev)[0]

    def test_opened_with_label_already_present_dispatches(self):
        self.assertTrue(self.d(gh_event("opened")))
        self.assertFalse(self.d(gh_event("opened", labels=())))

    def test_labeled_dispatches_only_for_the_exact_label(self):
        self.assertTrue(self.d(gh_event("labeled")))
        for other in ("To Be Test", "to be tested", "TO BE TEST", "to-be-test", " to be test", "to be test ", "bug", ""):
            self.assertFalse(self.d(gh_event("labeled", label=other, labels=(other,))), other)
        # an unrelated label added while "to be test" is already on the PR does NOT dispatch again
        self.assertFalse(self.d(gh_event("labeled", label="bug", labels=("to be test", "bug"))))

    def test_reopened_synchronize_ready_for_review_need_the_label(self):
        for action in ("reopened", "synchronize", "ready_for_review"):
            self.assertTrue(self.d(gh_event(action)), action)
            self.assertFalse(self.d(gh_event(action, labels=("bug",))), action)
            self.assertFalse(self.d(gh_event(action, labels=("To Be Test",))), action)

    def test_label_removed_and_other_actions_never_dispatch(self):
        for action in ("unlabeled", "edited", "closed", "assigned", "review_requested", "converted_to_draft"):
            self.assertFalse(self.d(gh_event(action)), action)
        self.assertFalse(self.d(gh_event("unlabeled", labels=())))
        self.assertFalse(self.d(gh_event("labeled"), name="pull_request_target"))
        self.assertFalse(self.d(gh_event("labeled"), name="issues"))

    def test_draft_closed_and_fork_pull_requests_are_skipped(self):
        self.assertFalse(self.d(gh_event("labeled", draft=True)))
        self.assertFalse(self.d(gh_event("synchronize", draft=True)))
        self.assertFalse(self.d(gh_event("labeled", state="closed")))
        self.assertFalse(self.d(gh_event("labeled", fork=True)))
        ev = gh_event("labeled"); ev["pull_request"]["head"]["repo"] = None          # deleted fork
        self.assertFalse(self.d(ev))


# --------------------------------------------------------------------------------------------- receiver (worker) --
class ReceiverAuthTests(_Base):
    def test_valid_signed_dispatch_is_accepted_for_a_synthetic_connection(self):
        status, outcome, code, body = self.h(signed(payload()))
        self.assertEqual((status, outcome), (202, "accepted"), code)
        seed = body["job_seed"]
        self.assertEqual(seed["repository"], SYN_REPO)
        self.assertEqual(seed["pr"]["head_sha"], SHA_HEAD)
        self.assertEqual(seed["trigger"]["requested_label"], "to be test")
        self.assertEqual(seed["trigger"]["connection_id"], CID_SYN)
        self.assertEqual(seed["trigger"]["label"], "qa-test:standard")
        self.assertEqual(body["context_preflight"]["source"], "synthetic_fixture")

    def test_missing_malformed_and_invalid_signatures_are_rejected(self):
        p = payload()
        self.assertEqual(self.h(signed(p, signature=""))[:3], (401, "unauthorized", "SIGNATURE_MISSING"))
        self.assertEqual(self.h(signed(p, signature="sha1=abc"))[2], "SIGNATURE_MALFORMED")
        self.assertEqual(self.h(signed(p, signature="sha256=" + "0" * 64))[2], "SIGNATURE_INVALID")
        self.assertEqual(self.h(signed(p, secret=SECRET_REAL))[2], "SIGNATURE_INVALID")          # another connection's secret
        tampered = signed(p)
        raw = base64.b64decode(tampered["body_b64"]).replace(b'"number":101', b'"number":102')
        tampered["body_b64"] = base64.b64encode(raw).decode()
        self.assertEqual(self.h(tampered)[2], "SIGNATURE_INVALID")

    def test_expired_and_future_timestamps_are_rejected(self):
        self.assertEqual(self.h(signed(payload(created_at=NOW - 301), ts=NOW - 301))[2], "TIMESTAMP_EXPIRED")
        self.assertEqual(self.h(signed(payload(created_at=NOW + 301), ts=NOW + 301))[2], "TIMESTAMP_EXPIRED")
        self.assertEqual(self.h(signed(payload(), ts=NOW))[0], 202)                              # inside the window
        # created_at must agree with the signed header timestamp
        self.assertEqual(self.h(signed(payload(run_id="9002", created_at=NOW - 1000), ts=NOW))[2], "DELIVERY_TIMESTAMP_MISMATCH")

    def test_replayed_delivery_id_is_rejected(self):
        req = signed(payload())
        self.assertEqual(self.h(req)[0], 202)
        self.assertEqual(self.h(req)[:3], (409, "replayed", "DELIVERY_REPLAYED"))
        # a re-signed copy with a fresh timestamp is still the same delivery
        self.assertEqual(self.h(signed(payload(created_at=NOW + 5), ts=NOW + 5))[2], "DELIVERY_REPLAYED")

    def test_unknown_malformed_and_revoked_connections_are_rejected(self):
        self.assertEqual(self.h(signed(payload(cid="prqa_c_" + "9" * 24)))[:3], (401, "unauthorized", "CONNECTION_UNKNOWN"))
        self.assertEqual(self.h(signed(payload(cid="not-a-connection")))[2], "CONNECTION_ID_MALFORMED")
        conn = default_connections(); conn[CID_SYN]["status"] = "revoked"
        write_registry_file(self.tmp / "connections", conn)
        os.utime(self.tmp / "connections" / conns.CONNECTIONS_FILE, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.h(signed(payload()))[2], "CONNECTION_UNKNOWN")

    def test_connection_id_alone_is_not_authentication(self):
        # knowing a valid connection id but not its secret gets nothing
        for secret in ("", "x" * 64, SECRET_OTHER):
            self.assertEqual(self.h(signed(payload(), secret=secret or "y"))[0], 401)

    def test_repository_must_match_the_connection_exactly(self):
        p = payload(cid=CID_OTHER, repo=SYN_REPO)                      # valid signature of CID_OTHER, wrong repository
        self.assertEqual(self.h(signed(p, secret=SECRET_OTHER))[:3], (403, "forbidden", "REPOSITORY_MISMATCH"))
        p = payload(repo="Synthetic-org/sample-orders-api")            # case differs: still a mismatch
        self.assertEqual(self.h(signed(p))[2], "REPOSITORY_MISMATCH")

    def test_unexpected_actions_and_labels_are_rejected_even_when_signed(self):
        for action in ("unlabeled", "closed", "edited"):
            self.assertEqual(self.h(signed(payload(action=action, run_id="1" + str(len(action)))))[2], "ACTION_NOT_ELIGIBLE", action)
        for label in ("To Be Test", "to be tested", "", "bug"):
            self.assertEqual(self.h(signed(payload(label=label, run_id="77")))[2], "LABEL_NOT_ELIGIBLE", label)

    def test_payload_validation_and_malformed_pr_identifiers(self):
        cases = {"pull_request.number": "101", "pull_request.head.sha": "XYZ", "pull_request.base.sha": "a" * 39,
                 "schema_version": "1.0", "delivery.id": "../../etc", "actions.run_id": "12a"}
        for i, (key, value) in enumerate(cases.items()):
            status, outcome, code, _ = self.h(signed(payload(run_id=str(500 + i), **{key: value})))
            self.assertEqual((status, outcome), (422, "rejected"), key)
            self.assertIn(code, ("PAYLOAD_INVALID", "PAYLOAD_INCONSISTENT"), key)
        self.assertEqual(self.h(signed(payload(number=0, run_id="600")))[2], "PAYLOAD_INVALID")
        self.assertEqual(self.h(signed(payload(number=-1, run_id="601")))[2], "PAYLOAD_INVALID")
        extra = payload(run_id="602"); extra["github_token"] = "ghp_synthetic"     # no extra fields (and no credentials) accepted
        self.assertEqual(self.h(signed(extra))[2], "PAYLOAD_INVALID")
        mism = payload(run_id="603"); mism["delivery"]["id"] = "gha-604-1"
        self.assertEqual(self.h(signed(mism))[2], "PAYLOAD_INCONSISTENT")
        same = payload(run_id="605", head_sha=SHA_BASE)
        self.assertEqual(self.h(signed(same))[2], "PAYLOAD_INCONSISTENT")
        self.assertEqual(self.h(signed(b"not json"))[0], 401)                  # no connection id → original path, no secret → 401

    def test_oversized_payload_is_rejected_before_any_other_work(self):
        big = payload(); big["actions"]["workflow"] = "x" * (17 * 1024)
        self.assertEqual(self.h(signed(big))[:3], (413, "too_large", "BODY_TOO_LARGE"))

    def test_draft_and_closed_pull_requests_are_rejected(self):
        self.assertEqual(self.h(signed(payload(run_id="700", **{"pull_request.draft": True})))[2], "PR_DRAFT")
        self.assertEqual(self.h(signed(payload(run_id="701", **{"pull_request.state": "closed"})))[2], "PR_NOT_OPEN")

    def test_auth_failures_are_rate_limited_and_logged_without_secrets(self):
        with self.assertLogs("prqa.actions", level="WARNING") as logs:
            for _ in range(31):
                last = self.h(signed(payload(), signature="sha256=" + "0" * 64))
        self.assertEqual(last[:3], (429, "rate_limited", "AUTH_FAILURE_RATE_LIMIT"))
        text = "\n".join(logs.output)
        for secret in (SECRET_SYN, SECRET_REAL, SECRET_OTHER):
            self.assertNotIn(secret, text)
        self.assertNotIn("sha256=", text)


class ReceiverIdempotencyAndPolicyTests(_Base):
    def test_same_head_sha_is_one_run_and_a_new_head_sha_is_a_new_run(self):
        self.assertEqual(self.h(signed(payload(action="opened", run_id="1")))[0], 202)
        # labeled + synchronize retries of the same head SHA in other runs: duplicate, no second job seed
        status, outcome, code, body = self.h(signed(payload(action="labeled", run_id="2")))
        self.assertEqual((status, outcome, code), (200, "duplicate", "DUPLICATE_REQUEST"))
        self.assertNotIn("job_seed", body)
        # a new commit while labelled: new head SHA → a new run
        status, outcome, _, body = self.h(signed(payload(action="synchronize", run_id="3", head_sha=SHA_HEAD2)))
        self.assertEqual((status, outcome), (202, "accepted"))
        self.assertEqual(body["job_seed"]["pr"]["head_sha"], SHA_HEAD2)
        # the delivery ids of the duplicate are remembered too (replay of run 2 → 409)
        self.assertEqual(self.h(signed(payload(action="labeled", run_id="2")))[2], "DELIVERY_REPLAYED")

    def test_accepted_dispatch_creates_exactly_one_job_through_the_existing_pipeline(self):
        _, _, _, body = self.h(signed(payload()))
        seed = body["job_seed"]
        job_req = {"synthetic": seed["synthetic"], "repository": seed["repository"], "pr": seed["pr"], "test_level": seed["test_level"],
                   "trigger": {"source": "github_actions", "delivery_id": body["idempotency_key"], "label": seed["trigger"]["label"],
                               "requested_label": seed["trigger"]["requested_label"]},
                   "prompt": {"text": "x" * 300, "source": "fixture", "sha256": "a" * 64, "template_version": "v2"}}
        hdr = {"x-worker-token": "test-token"}
        status, created = self.app.route("POST", "/jobs", {}, job_req, hdr)
        self.assertEqual(status, 201, created)
        self.assertEqual(self.app.route("POST", "/jobs", {}, job_req, hdr)[0], 409)
        self.assertEqual(self.app.store.stats().get("queued"), 1)

    def test_unregistered_repository_is_forbidden(self):
        conn = default_connections(); conn[CID_OTHER]["repository"] = "acme-labs/not-registered"
        write_registry_file(self.tmp / "connections", conn)
        os.utime(self.tmp / "connections" / conns.CONNECTIONS_FILE, (time.time() + 5, time.time() + 5))
        p = payload(cid=CID_OTHER, repo="acme-labs/not-registered")
        self.assertEqual(self.h(signed(p, secret=SECRET_OTHER))[:3], (403, "forbidden", "REPOSITORY_NOT_REGISTERED"))

    def test_missing_github_read_credential_fails_closed_for_real_repositories(self):
        p = payload(cid=CID_REAL, repo=REAL_REPO)
        status, outcome, code, body = self.h(signed(p, secret=SECRET_REAL))
        self.assertEqual((status, outcome, code), (503, "unavailable", "GITHUB_READ_CREDENTIAL_MISSING"))
        self.assertNotIn("job_seed", body)
        self.assertFalse(body["context_preflight"]["github_read_credential_configured"])
        # nothing was claimed: after provisioning the same delivery is processed normally
        self.assertFalse(self.app.store.delivery_seen(conns.delivery_key(CID_REAL, "gha-9001-1")))

    def test_review_only_returns_blocked_and_never_hands_over_a_job(self):
        self.intake.github_read_configured = True
        p = payload(cid=CID_REAL, repo=REAL_REPO)
        status, outcome, code, body = self.h(signed(p, secret=SECRET_REAL))
        self.assertEqual((status, outcome, code), (200, "blocked", "REVIEW_ONLY_MODE"))
        self.assertNotIn("job_seed", body)
        self.assertTrue(body["policy_preview"]["review_only"])
        self.assertFalse(body["policy_preview"]["expected_decision"]["allowed"])
        self.assertEqual(body["automation_mode"], "review_only")
        self.assertEqual(self.app.store.stats().get("queued"), None)
        # the blocked dispatch is recorded once; a retry is a duplicate, not a new decision
        self.assertEqual(self.h(signed(payload(cid=CID_REAL, repo=REAL_REPO, run_id="9002"), secret=SECRET_REAL))[1], "duplicate")
        self.assertTrue(self.app.policy.review_only)
        self.assertFalse(self.app.policy.real_execution_possible)
        self.app.policy.self_check()

    def test_context_builder_integration_in_synthetic_mode(self):
        _, _, _, body = self.h(signed(payload()))
        seed = body["job_seed"]
        fx = ROOT / "fixtures" / "synthetic"
        pr = {"number": seed["pr"]["number"], "title": "SYNTHETIC", "body": "Adds a status filter (SYNTHETIC).",
              "head": {"sha": seed["pr"]["head_sha"], "ref": seed["pr"]["head_ref"]},
              "base": {"sha": seed["pr"]["base_sha"], "ref": seed["pr"]["base_ref"]}}
        status, out = self.app.route("POST", "/context/build", {}, {
            "pr": pr, "files": json.loads((fx / "pr-files.synthetic.json").read_text()), "diff": (fx / "pr-diff.synthetic.diff").read_text(),
            "openapi": json.loads((fx / "openapi.synthetic.json").read_text()), "repository": seed["repository"], "synthetic": True},
            {"x-worker-token": "test-token"})
        self.assertEqual(status, 200)
        ctx = out["context"]
        blob = json.dumps(ctx)
        self.assertIn(SHA_HEAD, blob)
        self.assertIn(SYN_REPO, blob)
        self.assertTrue(ctx.get("changed_files"))

    def test_config_reports_connections_without_secrets(self):
        status, cfg = self.app.route("GET", "/config", {}, None, {"x-worker-token": "test-token"})
        self.assertEqual(status, 200)
        summary = cfg["actions_intake"]["connections"]
        self.assertEqual(summary["active_connections"], 3)
        dump = json.dumps(cfg)
        for secret in (SECRET_SYN, SECRET_REAL, SECRET_OTHER):
            self.assertNotIn(secret, dump)


class ReceiverWithCredentialTests(_Base):
    github_token = "synthetic-read-only-token-not-real"

    def test_credential_present_review_only_still_blocks_real_execution(self):
        status, outcome, code, body = self.h(signed(payload(cid=CID_REAL, repo=REAL_REPO), secret=SECRET_REAL))
        self.assertEqual((status, outcome, code), (200, "blocked", "REVIEW_ONLY_MODE"))
        self.assertTrue(body["context_preflight"]["github_read_credential_configured"])
        self.assertEqual(body["context_preflight"]["github_api_mode"], "mock")      # review-only: never the real GitHub API
        self.assertNotIn("synthetic-read-only-token-not-real", json.dumps(body))


# ------------------------------------------------------------------------------------------------ registry --
class ConnectionRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "c"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_or_malformed_registry_fails_closed(self):
        self.assertIsNone(conns.ConnectionRegistry(None).get(CID_SYN))
        self.assertIsNone(conns.ConnectionRegistry(str(self.dir)).get(CID_SYN))
        write_registry_file(self.dir, default_connections())
        (self.dir / conns.CONNECTIONS_FILE).write_text("{not json", encoding="utf-8")
        reg = conns.ConnectionRegistry(str(self.dir))
        self.assertIsNone(reg.get(CID_SYN))
        self.assertIn("unusable", reg.summary()["error"])
        for bad in ({"version": 2, "connections": {}}, {"version": 1, "connections": {"bad": {}}},
                    {"version": 1, "connections": {CID_SYN: {"repository": SYN_REPO, "secret": "short"}}},
                    {"version": 1, "connections": {CID_SYN: {"repository": "no-slash", "secret": SECRET_SYN}}},
                    {"version": 1, "connections": {CID_SYN: {"repository": SYN_REPO, "secret": SECRET_SYN},
                                                   CID_REAL: {"repository": SYN_REPO, "secret": SECRET_REAL}}}):
            with self.assertRaises(conns.ConnectionError_):
                conns.parse_registry(bad)

    def test_registry_reloads_on_change(self):
        write_registry_file(self.dir, {})
        reg = conns.ConnectionRegistry(str(self.dir))
        self.assertIsNone(reg.get(CID_SYN))
        write_registry_file(self.dir, default_connections())
        os.utime(self.dir / conns.CONNECTIONS_FILE, (time.time() + 5, time.time() + 5))
        self.assertEqual(reg.get(CID_SYN).repository, SYN_REPO)

    def test_store_creates_reuses_rotates_and_revokes_with_private_modes(self):
        store = conns.ConnectionStore(self.dir)
        c1, a1 = store.connect(REAL_REPO)
        self.assertEqual(a1, "created")
        self.assertRegex(c1.connection_id, conns.CONNECTION_ID_RE)
        self.assertRegex(c1.secret, r"^[0-9a-f]{64}$")
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o750)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / conns.CONNECTIONS_FILE).st_mode), 0o640)
        c2, a2 = store.connect(REAL_REPO)
        self.assertEqual((a2, c2), ("reused", c1))
        c3, a3 = store.connect(REAL_REPO, rotate_secret=True)
        self.assertEqual((a3, c3.connection_id), ("rotated", c1.connection_id))
        self.assertNotEqual(c3.secret, c1.secret)
        other, _ = store.connect("acme-labs/second")
        self.assertNotEqual(other.connection_id, c1.connection_id)
        self.assertNotEqual(other.secret, c3.secret)
        self.assertEqual(store.revoke(REAL_REPO), c1.connection_id)
        self.assertIsNone(conns.ConnectionRegistry(str(self.dir)).get(c1.connection_id))
        with self.assertRaises(conns.ConnectionError_):
            store.connect("not a repo")
        self.assertNotIn(c3.secret, json.dumps(c3.public()))

    def test_store_refuses_a_group_writable_or_world_readable_directory(self):
        self.dir.mkdir(mode=0o755)
        os.chmod(self.dir, 0o777)
        store = conns.ConnectionStore(self.dir)
        # ensure_dir tightens it to 750; a directory it cannot tighten is refused
        store.ensure_dir()
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o750)
        os.chmod(self.dir, 0o777)
        with mock.patch.object(conns.os, "chmod"):                     # simulate a directory whose mode cannot be tightened
            with self.assertRaises(conns.ConnectionError_):
                store.ensure_dir()


# ----------------------------------------------------------------------------------------- endpoint validation --
class IngressUrlTests(unittest.TestCase):
    def test_public_https_ingress_is_accepted(self):
        for url in (URL, "https://qa.acme-labs.io:8443/webhook/prqa/actions", "https://8.8.8.8/webhook/prqa/actions"):
            self.assertTrue(conns.validate_ingress_url(url)[0], url)

    def test_http_private_local_internal_and_dummy_endpoints_are_rejected(self):
        bad = ["", "not a url", "http://prqa.acme-labs.io/webhook/prqa/actions", "ftp://prqa.acme-labs.io/webhook/prqa/actions",
               "https://localhost/webhook/prqa/actions", "https://127.0.0.1/webhook/prqa/actions", "https://[::1]/webhook/prqa/actions",
               "https://10.1.2.3/webhook/prqa/actions", "https://192.168.1.10/webhook/prqa/actions", "https://172.16.0.4/webhook/prqa/actions",
               "https://169.254.169.254/webhook/prqa/actions", "https://100.64.0.1/webhook/prqa/actions", "https://[fe80::1]/webhook/prqa/actions",
               "https://[fd00::1]/webhook/prqa/actions", "https://0.0.0.0/webhook/prqa/actions",
               "https://n8n/webhook/prqa/actions", "https://prqa-worker/webhook/prqa/actions", "https://prqa-model-proxy:8089/webhook/prqa/actions",
               "https://host.docker.internal/webhook/prqa/actions", "https://box.local/webhook/prqa/actions", "https://svc.internal/webhook/prqa/actions",
               "https://prqa.example.com/webhook/prqa/actions", "https://example.org/webhook/prqa/actions", "https://prqa.example.test/webhook/prqa/actions",
               "https://x.invalid/webhook/prqa/actions", "https://dev.localhost/webhook/prqa/actions",
               "https://prqa.acme-labs.io/", "https://prqa.acme-labs.io/rest/workflows", "https://prqa.acme-labs.io/webhook-test/prqa/actions",
               "https://prqa.acme-labs.io/webhook/prqa/github", "https://prqa.acme-labs.io:5678/webhook/prqa/actions",
               "https://user:pw@prqa.acme-labs.io/webhook/prqa/actions", "https://prqa.acme-labs.io/webhook/prqa/actions?token=x",
               "https://prqa.acme-labs.io/webhook/prqa/actions#x", "https://bad_host.acme-labs.io/webhook/prqa/actions"]
        for url in bad:
            self.assertFalse(conns.validate_ingress_url(url)[0], url)

    def test_http_only_in_explicit_local_test_mode(self):
        local = "http://127.0.0.1:5678/webhook/prqa/actions"
        self.assertFalse(conns.validate_ingress_url(local)[0])
        self.assertTrue(conns.validate_ingress_url(local, local_test=True)[0])
        self.assertFalse(conns.validate_ingress_url("http://10.0.0.5/webhook/prqa/actions", local_test=True)[0])


# --------------------------------------------------------------------------------------------- client end-to-end --
class _Resp:
    def __init__(self, status, body):
        self.status, self._body, self.headers = status, body, {}

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ClientTests(_Base):
    def run_client(self, ev, *, env_over=None, opener=None, event_name="pull_request", secret=SECRET_SYN, cid=CID_SYN, repo=SYN_REPO):
        evf = self.tmp / "event.json"
        evf.write_text(json.dumps(ev), encoding="utf-8")
        out, summ = self.tmp / "out.txt", self.tmp / "summary.md"
        for f in (out, summ):
            f.write_text("", encoding="utf-8")
        env = {"GITHUB_EVENT_PATH": str(evf), "GITHUB_EVENT_NAME": event_name, "GITHUB_REPOSITORY": repo,
               "PRQA_EXPECTED_REPOSITORY": repo, "PRQA_WEBHOOK_URL": URL, "PRQA_WEBHOOK_SECRET": secret, "PRQA_CONNECTION_ID": cid,
               "GITHUB_RUN_ID": "9001", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_WORKFLOW": "PRQA · to be test",
               "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summ)}
        env.update(env_over or {})
        sent = []

        def to_worker(req, timeout):
            self.assertLessEqual(timeout, 60)
            headers = {k.lower(): v for k, v in req.header_items()}
            sent.append((req.full_url, headers, req.data))
            o = self.intake.handle({"body_b64": base64.b64encode(req.data).decode(), "timestamp": headers.get("x-prqa-timestamp"),
                                    "signature": headers.get("x-prqa-signature"), "user_agent": headers.get("user-agent")})
            return _Resp(o.http_status, json.dumps(o.to_response()[1]).encode())

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = CLIENT.main(env=env, opener=opener or to_worker, sleep=lambda s: None, clock=lambda: NOW)
        return rc, buf.getvalue() + out.read_text() + summ.read_text(), sent

    def test_end_to_end_labeled_dispatch_is_accepted_by_the_worker(self):
        rc, text, sent = self.run_client(gh_event("labeled"))
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(sent), 1)
        self.assertIn("outcome=accepted", text)
        body = json.loads(sent[0][2])
        self.assertEqual(set(body), {"schema_version", "type", "connection_id", "repository", "pull_request", "event", "delivery", "actions"})
        self.assertNotIn("title", json.dumps(body["pull_request"]))
        self.assertEqual(body["event"]["label"], "to be test")
        self.assertEqual(body["delivery"]["id"], "gha-9001-1")
        self.assertNotIn("authorization", sent[0][1])

    def test_every_dispatching_case_reaches_the_worker_and_non_dispatching_cases_send_nothing(self):
        dispatching = [gh_event("opened"), gh_event("labeled"), gh_event("reopened"), gh_event("synchronize"), gh_event("ready_for_review")]
        silent = [gh_event("unlabeled", labels=()), gh_event("labeled", label="bug", labels=("bug",)),
                  gh_event("labeled", label="To Be Test", labels=("To Be Test",)), gh_event("opened", labels=()),
                  gh_event("labeled", draft=True), gh_event("labeled", fork=True), gh_event("edited")]
        for i, ev in enumerate(dispatching):
            rc, text, sent = self.run_client(ev, env_over={"GITHUB_RUN_ID": str(100 + i)})
            self.assertEqual((rc, len(sent)), (0, 1), ev["action"])
            self.assertRegex(text, r"outcome=(accepted|duplicate)")
        for ev in silent:
            rc, text, sent = self.run_client(ev, env_over={"PRQA_WEBHOOK_URL": "", "PRQA_WEBHOOK_SECRET": ""})
            self.assertEqual((rc, sent), (0, []), ev["action"])
            self.assertIn("dispatch=false", text)

    def test_missing_configuration_fails_visibly_and_sends_nothing(self):
        for missing in ("PRQA_WEBHOOK_URL", "PRQA_WEBHOOK_SECRET", "PRQA_CONNECTION_ID"):
            rc, text, sent = self.run_client(gh_event("labeled"), env_over={missing: ""})
            self.assertEqual((rc, sent), (1, []), missing)
            self.assertIn("::error::", text)
            self.assertIn(missing, text)
            self.assertIn("outcome=client_error", text)
        rc, text, sent = self.run_client(gh_event("labeled"), env_over={"PRQA_WEBHOOK_URL": "http://prqa.acme-labs.io/webhook/prqa/actions"})
        self.assertEqual((rc, sent), (1, []))
        rc, text, sent = self.run_client(gh_event("labeled"), env_over={"PRQA_CONNECTION_ID": "abc"})
        self.assertEqual((rc, sent), (1, []))
        rc, text, sent = self.run_client(gh_event("labeled"), env_over={"PRQA_EXPECTED_REPOSITORY": "acme-labs/other"})
        self.assertEqual((rc, sent), (1, []))
        rc, text, sent = self.run_client(gh_event("labeled"), env_over={"GITHUB_EVENT_PATH": str(self.tmp / "missing.json")})
        self.assertEqual((rc, sent), (1, []))

    def test_wrong_secret_or_review_only_are_reported_without_leaking_anything(self):
        rc, text, _ = self.run_client(gh_event("labeled"), secret="f" * 64)
        self.assertEqual(rc, 1)
        self.assertIn("SIGNATURE_INVALID", text)
        self.intake.github_read_configured = True
        rc, text, sent = self.run_client(gh_event("labeled", repo=REAL_REPO), secret=SECRET_REAL, cid=CID_REAL, repo=REAL_REPO,
                                         env_over={"GITHUB_RUN_ID": "8001"})
        self.assertEqual(rc, 0)
        self.assertIn("outcome=blocked", text)
        self.assertIn("REVIEW_ONLY_MODE", text)
        for secret in (SECRET_SYN, SECRET_REAL, "f" * 64):
            self.assertNotIn(secret, text)
        self.assertNotIn("sha256=", text)
        self.assertNotIn(sent[0][1]["x-prqa-signature"], text)

    def test_missing_github_credential_fails_the_job_without_retrying(self):
        rc, text, sent = self.run_client(gh_event("labeled", repo=REAL_REPO), secret=SECRET_REAL, cid=CID_REAL, repo=REAL_REPO)
        self.assertEqual((rc, len(sent)), (1, 1))
        self.assertIn("GITHUB_READ_CREDENTIAL_MISSING", text)

    def test_bounded_retries_and_replay_after_a_lost_response(self):
        calls = []

        def flaky(req, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("connection reset")
            return _Resp(409, json.dumps({"outcome": "replayed", "reason_code": "DELIVERY_REPLAYED"}).encode())
        rc, text, _ = self.run_client(gh_event("labeled"), opener=flaky)
        self.assertEqual((rc, len(calls)), (0, 2))
        self.assertIn("outcome=delivered_earlier", text)
        calls.clear()

        def down(req, timeout):
            calls.append(1)
            raise OSError("unreachable")
        rc, text, _ = self.run_client(gh_event("labeled"), opener=down)
        self.assertEqual((rc, len(calls)), (1, 3))
        self.assertIn("prqa-ingress.acme-labs.io", text)                    # host only, never the full URL path/secret
        # a first-attempt 409 is a genuine replay → failure
        rc, _, _ = self.run_client(gh_event("labeled"), opener=lambda r, timeout: _Resp(409, b'{"outcome":"replayed","reason_code":"DELIVERY_REPLAYED"}'))
        self.assertEqual(rc, 1)


# -------------------------------------------------------------------------------------- CLI + generated package --
class SetupCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cdir = self.tmp / "conns"
        self.secrets_env = self.tmp / "secrets.env"
        self.secrets_env.write_text("EXISTING_LINE=keep-me\n", encoding="utf-8")
        os.chmod(self.secrets_env, 0o600)

    def tearDown(self):
        self._tmp.cleanup()

    def connect(self, *extra, repo=REAL_REPO):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = CLI.main(["--connections-dir", str(self.cdir), "connect", repo, "--out", str(self.tmp / "out"),
                           "--secrets-env", str(self.secrets_env), *extra])
        return rc, buf.getvalue(), err.getvalue()

    def package_files(self, repo=REAL_REPO):
        return sorted(p for p in (self.tmp / "out" / repo.replace("/", "__")).rglob("*") if p.is_file())

    def test_connect_generates_a_secret_free_package_with_unconfigured_endpoint(self):
        with mock.patch.dict(os.environ, {"PRQA_PUBLIC_INGRESS_URL": ""}):
            rc, out, err = self.connect()
        self.assertEqual(rc, 0, err)
        info = json.loads(out)
        self.assertEqual((info["connection"], info["endpoint_status"]), ("created", "unconfigured"))
        secret = conns.ConnectionStore(self.cdir).find_by_repository(REAL_REPO).secret
        self.assertNotIn(secret, out + err)
        files = {p.name: p for p in self.package_files()}
        self.assertEqual(set(files), {"prqa-to-be-test.yml", "INSTALL.md", "connection.json"})
        self.assertTrue(str(files["prqa-to-be-test.yml"]).endswith(".github/workflows/prqa-to-be-test.yml"))
        for p in files.values():
            self.assertNotIn(secret, p.read_text(encoding="utf-8"), p.name)
        meta = json.loads(files["connection.json"].read_text())
        self.assertEqual(meta["endpoint"]["status"], "unconfigured")
        self.assertFalse(meta["secret_included"])
        install = files["INSTALL.md"].read_text()
        for needle in ("UNCONFIGURED", "Never commit the secret", "PRQA_WEBHOOK_SECRET", "PRQA_WEBHOOK_URL", "PRQA_CONNECTION_ID",
                       "| **SECRET** |", "not secret", "Rollback", "Verification", "Server provisioning", "Event policy",
                       "permissions: {}", "pull_request_target", "GITHUB_READ_CREDENTIAL_MISSING"):
            self.assertIn(needle, install, needle)
        env_text = self.secrets_env.read_text()
        self.assertIn("EXISTING_LINE=keep-me", env_text)
        self.assertIn(f"PRQA_ACTIONS_CONNECTIONS_DIR={self.cdir}", env_text)
        self.assertIn("PRQA_ACTIONS_CONNECTIONS_GID=", env_text)
        self.assertEqual(stat.S_IMODE(os.stat(self.secrets_env).st_mode), 0o600)

    def test_reuse_rotate_and_configured_endpoint(self):
        _, out1, _ = self.connect()
        _, out2, _ = self.connect("--webhook-url", URL)
        self.assertEqual(json.loads(out1)["connection_id"], json.loads(out2)["connection_id"])
        self.assertEqual(json.loads(out2)["connection"], "reused")
        self.assertEqual(json.loads(out2)["endpoint_status"], "configured")
        before = conns.ConnectionStore(self.cdir).find_by_repository(REAL_REPO).secret
        _, out3, _ = self.connect("--rotate-secret")
        self.assertEqual(json.loads(out3)["connection"], "rotated")
        self.assertNotEqual(before, conns.ConnectionStore(self.cdir).find_by_repository(REAL_REPO).secret)

    def test_invalid_repository_and_endpoints_are_refused_before_anything_is_written(self):
        for repo in ("acme", "acme/", "/repo", "acme/re po", "a" * 40 + "/r", "acme/repo;rm -rf /", "acme/$(id)"):
            rc, _, err = self.connect(repo=repo)
            self.assertEqual(rc, 2, repo)
        for url in ("http://prqa.acme-labs.io/webhook/prqa/actions", "https://192.168.0.2/webhook/prqa/actions",
                    "https://prqa.example.com/webhook/prqa/actions", "https://n8n/webhook/prqa/actions"):
            rc, _, err = self.connect("--webhook-url", url)
            self.assertEqual(rc, 2, url)
            self.assertIn("rejected", err)
        self.assertFalse((self.cdir / conns.CONNECTIONS_FILE).exists())
        rc, out, _ = self.connect("--webhook-url", "http://127.0.0.1:5678/webhook/prqa/actions", "--local-test")
        self.assertEqual((rc, json.loads(out)["endpoint_status"]), (0, "local_test"))

    def test_register_repository_adds_only_a_disabled_pending_entry(self):
        reg = self.tmp / "repositories.yaml"
        shutil.copy(ROOT / "config" / "repositories.yaml", reg)
        rc, out, err = self.connect("--register-repository", "--registry", str(reg))
        self.assertEqual(rc, 0, err)
        from prqa_worker.policy import Policy
        policy = Policy.from_env({"AUTOMATION_MODE": "review_only"})
        entry = Registry.load(reg, policy).get(REAL_REPO)
        self.assertEqual((entry.kind, entry.enabled, entry.approval_status), ("real", False, "pending_approval"))
        self.assertIn("already present", CLI.register_repository(reg, REAL_REPO))
        with self.assertRaises(CLI.SetupError):
            CLI.register_repository(reg, "synthetic-org/new-one")

    def test_export_secret_refuses_a_terminal_and_writes_only_to_a_pipe(self):
        self.connect()
        tty_out = io.StringIO()
        tty_out.isatty = lambda: True  # type: ignore[method-assign]
        err = io.StringIO()
        with contextlib.redirect_stdout(tty_out), contextlib.redirect_stderr(err):
            rc = CLI.main(["--connections-dir", str(self.cdir), "export-secret", REAL_REPO])
        self.assertEqual((rc, tty_out.getvalue()), (2, ""))
        self.assertIn("refusing", err.getvalue())
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "setup" / "prqa_connect_repository.py"), "--connections-dir", str(self.cdir),
                               "export-secret", REAL_REPO], capture_output=True, text=True, timeout=60)
        secret = conns.ConnectionStore(self.cdir).find_by_repository(REAL_REPO).secret
        self.assertEqual((proc.returncode, proc.stdout == secret, proc.stderr), (0, True, ""))
        rc_missing = subprocess.run([sys.executable, str(ROOT / "scripts" / "setup" / "prqa_connect_repository.py"), "--connections-dir", str(self.cdir),
                                     "export-secret", "acme-labs/unknown"], capture_output=True, text=True, timeout=60)
        self.assertEqual((rc_missing.returncode, rc_missing.stdout), (2, ""))

    def test_revoke_and_status(self):
        self.connect()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(CLI.main(["--connections-dir", str(self.cdir), "status", REAL_REPO]), 0)
            self.assertEqual(CLI.main(["--connections-dir", str(self.cdir), "revoke", REAL_REPO]), 0)
            self.assertEqual(CLI.main(["--connections-dir", str(self.cdir), "status", REAL_REPO]), 1)
        self.assertIsNone(re.search(r"\b[0-9a-f]{64}\b", buf.getvalue()))


class GeneratedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = CLI.render_workflow(REAL_REPO, CID_REAL)
        self.doc = yaml.safe_load(self.text)

    def test_yaml_syntax_and_triggers(self):
        on = self.doc.get("on", self.doc.get(True))                    # PyYAML parses the bare key `on` as True
        self.assertEqual(list(on), ["pull_request"])
        self.assertEqual(on["pull_request"]["types"], ["opened", "reopened", "labeled", "synchronize", "ready_for_review"])
        self.assertNotIn("unlabeled", on["pull_request"]["types"])
        self.assertNotIn("pull_request_target", self.text.split("name: PRQA")[1].split("jobs:")[0])
        self.assertNotIn("pull_request_target:", self.text)
        self.assertEqual(self.doc["permissions"], {})
        job = self.doc["jobs"]["dispatch"]
        self.assertEqual(job["timeout-minutes"], 5)
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertIn(f"github.repository == '{REAL_REPO}'", job["if"])
        self.assertEqual(self.doc["concurrency"]["cancel-in-progress"], False)

    def test_no_checkout_no_third_party_actions_no_expression_in_the_script(self):
        steps = self.doc["jobs"]["dispatch"]["steps"]
        self.assertEqual(len(steps), 1)
        self.assertNotIn("uses", steps[0])
        self.assertNotIn("uses:", self.text)
        self.assertNotIn("actions/checkout", self.text)
        self.assertNotIn("${{", steps[0]["run"])
        self.assertNotIn("GITHUB_TOKEN", json.dumps(steps[0]["env"]))
        self.assertEqual(steps[0]["env"]["PRQA_WEBHOOK_SECRET"], "${{ secrets.PRQA_WEBHOOK_SECRET }}")
        self.assertEqual(steps[0]["env"]["PRQA_WEBHOOK_URL"], "${{ secrets.PRQA_WEBHOOK_URL }}")
        self.assertEqual(steps[0]["env"]["PRQA_CONNECTION_ID"], "${{ vars.PRQA_CONNECTION_ID }}")
        self.assertEqual(steps[0]["env"]["PRQA_EXPECTED_REPOSITORY"], REAL_REPO)

    def test_no_secret_url_or_hardcoded_endpoint_in_the_workflow(self):
        self.assertIsNone(re.search(r"\b[0-9a-f]{64}\b", self.text))
        self.assertIsNone(re.search(r"https?://(?!github\.com)[a-z0-9.-]+\.[a-z]{2,}/webhook", self.text))
        self.assertNotIn(URL, self.text)

    def test_client_is_embedded_verbatim_and_the_step_runs(self):
        run = self.doc["jobs"]["dispatch"]["steps"][0]["run"]
        client = (ROOT / "scripts" / "clients" / "prqa_to_be_test_client.py").read_text(encoding="utf-8").rstrip("\n")
        self.assertIn(client, run)
        with tempfile.TemporaryDirectory() as tmp:
            ev = Path(tmp) / "event.json"
            ev.write_text(json.dumps(gh_event("labeled", repo=REAL_REPO)), encoding="utf-8")
            env = {"PATH": os.environ.get("PATH", ""), "GITHUB_EVENT_PATH": str(ev), "GITHUB_EVENT_NAME": "pull_request",
                   "GITHUB_REPOSITORY": REAL_REPO, "PRQA_EXPECTED_REPOSITORY": REAL_REPO, "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1"}
            proc = subprocess.run(["bash", "-c", run], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("::error::PRQA client: PRQA is not configured", proc.stdout)

    def test_template_rejects_injection_through_repository_or_connection_id(self):
        for repo, cid in (("acme/x'||true", CID_REAL), ("acme/x\nrun: id", CID_REAL), (REAL_REPO, "prqa_c_${{ x }}")):
            with self.assertRaises(CLI.SetupError):
                CLI.render_workflow(repo, cid)


# ------------------------------------------------------------------------------------------- safety invariants --
class SafetyInvariantTests(unittest.TestCase):
    def test_n8n_route_handles_new_outcomes_without_starting_the_pipeline(self):
        wf = json.loads((ROOT / "n8n" / "workflows" / "01-prqa-github-webhook-receiver.json").read_text())
        nodes = {n["name"]: n for n in wf["nodes"]}
        edges = {(src, i, t["node"]) for src, c in wf["connections"].items() for i, outs in enumerate(c.get("main", []))
                 for t in outs or []}
        for idx, name, code in ((7, "Respond 200 Blocked by Policy (Actions)", 200), (8, "Respond 409 Delivery Replayed (Actions)", 409),
                                (9, "Respond 413 Payload Too Large (Actions)", 413), (10, "Respond 503 Context Credential Missing (Actions)", 503),
                                (11, "Respond 503 Worker Unavailable (Actions)", 503)):
            self.assertIn(("Route Intake Outcome", idx, name), edges)
            self.assertEqual(nodes[name]["parameters"]["options"]["responseCode"], code)
            self.assertFalse(any(src == name for src, _, _ in edges), name)          # terminal: never starts the pipeline
        keys = [r["outputKey"] for r in nodes["Route Intake Outcome"]["parameters"]["rules"]["values"]]
        self.assertEqual(keys[7:], ["blocked", "replayed", "too_large", "unavailable"])
        self.assertNotIn("$env.PRQA_WEBHOOK_SECRET", json.dumps([n for n in wf["nodes"] if n["type"] != "n8n-nodes-base.stickyNote"]))

    def test_nine_workflows_remain_inactive(self):
        files = sorted((ROOT / "n8n" / "workflows").glob("*.json"))
        self.assertEqual(len(files), 9)
        for f in files:
            self.assertFalse(json.loads(f.read_text()).get("active"), f.name)

    def test_review_only_configuration_and_budget_caps_are_unchanged(self):
        wanted = {"AUTOMATION_MODE": "review_only", "PROMPT_LLM_LIVE_IN_REVIEW_ONLY": "false", "PROMPT_LLM_MAX_TOKENS": "3000",
                  "PRQA_BUDGET_JOB_TOKENS": "24000", "PRQA_BUDGET_TOKENS_PER_MINUTE": "8000", "PRQA_BUDGET_MAX_IN_FLIGHT": "1"}
        found = {}
        for line in (ROOT / "compose" / ".env.example").read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0].strip()
            if key in wanted:
                found[key] = line.split("=", 1)[1].split("#")[0].strip()
        for key, value in wanted.items():
            if key in found:
                self.assertEqual(found[key], value, key)
        compose = (ROOT / "compose" / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("AUTOMATION_MODE: ${AUTOMATION_MODE:-review_only}", compose)
        self.assertIn("PRQA_ACTIONS_CONNECTIONS_PATH: /run/prqa-connections", compose)
        self.assertNotIn("docker.sock:/var/run/docker.sock", compose.split("prqa-worker:")[1].split("prqa-mocks:")[0])

    def test_dispatch_path_makes_no_network_or_provider_call(self):
        """Budget guard: the intake never contacts GitHub, Groq or the proxy; nothing reserves provider budget."""
        src = (ROOT / "worker" / "prqa_worker" / "actions_intake.py").read_text() + (ROOT / "worker" / "prqa_worker" / "actions_connections.py").read_text()
        for forbidden in (r"^\s*(import|from)\s+(urllib\.request|urllib\.error|http|requests|socket|ssl)\b", r"provider_budget", r"(?i)groq"):
            self.assertIsNone(re.search(forbidden, src, re.M), forbidden)
        with tempfile.TemporaryDirectory() as tmp:
            app = make_app(Path(tmp))
            try:
                def boom(*a, **k):
                    raise AssertionError("network access attempted")
                with mock.patch.object(socket.socket, "connect", boom), mock.patch.object(socket, "create_connection", boom):
                    out = app.intake.handle(signed(payload()))
                self.assertEqual(out.http_status, 202)
                self.assertNotIn("prompt", out.body["job_seed"])
                self.assertFalse(app.policy.prompt_llm_real)
            finally:
                app.executor.stop()

    def test_no_secret_in_generated_artifacts_or_tracked_placeholder(self):
        placeholder = ROOT / "compose" / "actions-connections.unprovisioned"
        self.assertFalse((placeholder / conns.CONNECTIONS_FILE).exists())
        for p in placeholder.rglob("*"):
            if p.is_file():
                self.assertIsNone(re.search(r"\b[0-9a-f]{64}\b", p.read_text(errors="replace")))
        schema = json.loads((ROOT / "schemas" / "actions-connection-request.schema.json").read_text())
        self.assertFalse(schema["additionalProperties"])
        names: set[str] = set()

        def walk(node):
            if isinstance(node, dict):
                names.update(k.lower() for k in (node.get("properties") or {}))
                for v in node.values():
                    walk(v)
        walk(schema)
        self.assertIn("connection_id", names)
        for name in names:                                             # no field can carry a credential
            self.assertIsNone(re.search(r"token|secret|password|authorization|api_?key|credential", name), name)


if __name__ == "__main__":
    unittest.main()
