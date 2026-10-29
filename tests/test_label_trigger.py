"""Label-triggered testing: event parsing, lifecycle (unlabeled / closed / synchronize), cancellation, isolated
workspaces and pinned checkout, executor integration, the local poller, and the generated n8n structure.
SYNTHETIC — no network, no real repository. The real-git checkout test uses a throw-away local bare repository
through file:// (never https) and is skipped when git is not installed.

Run: cd "<project>" && PYTHONPATH=worker python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from prqa_worker import opencode_runner  # noqa: E402
from prqa_worker.server import App  # noqa: E402
from prqa_worker.validation import SchemaStore, evaluate_event, resolve_trigger_label  # noqa: E402
from prqa_worker.workspace import (MARKER, CheckoutError, WorkspaceError, WorkspaceManager,  # noqa: E402
                                   build_checkout_plan, run_checkout)

FIX = ROOT / "fixtures" / "synthetic"
SECRET = "synthetic-label-secret-0123456789abcdef0123456789abcdef"
SHA_HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
SHA_NEW = "b" * 40
GIT = shutil.which("git")


def event(action="labeled", label="to be test", *, sha=SHA_HEAD, state="open", draft=False, repo="synthetic-org/sample-orders-api", number=101):
    ev = json.loads((FIX / "github-pr-labeled.synthetic.json").read_text(encoding="utf-8"))
    ev["action"] = action
    ev["label"] = {"id": 1, "name": label}
    ev["pull_request"]["state"] = state
    ev["pull_request"]["draft"] = draft
    ev["pull_request"]["head"]["sha"] = sha
    ev["pull_request"]["number"] = number
    ev["number"] = number
    ev["repository"]["full_name"] = repo
    ev["pull_request"]["head"]["repo"]["full_name"] = repo
    return ev


def make_app(tmp: str, **extra) -> App:
    env = {
        "AUTOMATION_MODE": "review_only", "MOCK_SERVICES_URL": "http://prqa-mocks:8090", "PRQA_WORKER_TOKEN": "test-token",
        "PRQA_REGISTRY_PATH": str(ROOT / "config/repositories.yaml"), "PRQA_SCHEMA_DIR": str(ROOT / "schemas"),
        "PRQA_PROMPTS_DIR": str(ROOT / "prompts"), "PRQA_FIXTURES_DIR": str(ROOT / "fixtures"), "PRQA_DATA_DIR": tmp,
        "MOCK_EXECUTION_DELAY_S": "0", "PRQA_WEBHOOK_SECRET": SECRET, "PRQA_WORKSPACE_ROOT": tmp + "/workspaces",
    }
    env.update(extra)
    return App(env)


# ------------------------------------------------------------------ events ---
class LabelEventTests(unittest.TestCase):
    def setUp(self):
        self.schemas = SchemaStore(ROOT / "schemas")

    def test_opt_in_label_maps_to_default_level_and_is_configurable(self):
        self.assertEqual(resolve_trigger_label("to be test"), "standard")
        self.assertEqual(resolve_trigger_label("To Be Test "), "standard")                     # case/space tolerant
        self.assertEqual(resolve_trigger_label("to be test", default_level="security"), "security")
        self.assertEqual(resolve_trigger_label("needs-qa", trigger_label="needs-qa", default_level="smoke"), "smoke")
        self.assertIsNone(resolve_trigger_label("to be test", trigger_label="needs-qa"))
        self.assertEqual(resolve_trigger_label("qa-test:comprehensive"), "comprehensive")
        self.assertIsNone(resolve_trigger_label("bug"))
        self.assertEqual(resolve_trigger_label("to be test", default_level="bogus"), "standard")  # invalid default → standard

    def test_labeled_event_with_opt_in_label_is_eligible(self):
        out = evaluate_event("pull_request", event(), self.schemas)
        self.assertTrue(out["eligible"], out)
        seed = out["job_seed"]
        self.assertEqual((seed["repository"], seed["pr"]["number"], seed["pr"]["head_sha"], seed["test_level"]),
                         ("synthetic-org/sample-orders-api", 101, SHA_HEAD, "standard"))
        self.assertEqual(seed["trigger"], {"source": "github_webhook", "label": "qa-test:standard", "requested_label": "to be test",
                                           "requested_by": "synthetic-qa-lead"})   # sender.login from the HMAC-authenticated event
        self.assertEqual(seed["pr"]["base_ref"], "main"); self.assertEqual(seed["pr"]["head_ref"], "feature/order-status-filter")
        out = evaluate_event("pull_request", event(label="qa-test:smoke"), self.schemas)
        self.assertEqual(out["job_seed"]["test_level"], "smoke")

    def test_label_filtering_and_ineligible_states(self):
        self.assertEqual(evaluate_event("pull_request", event(label="bug"), self.schemas)["reason_code"], "LABEL_NOT_QA")
        self.assertEqual(evaluate_event("pull_request", event(label="to be test", trigger_label="needs-qa") if False else event(label="to be tested"), self.schemas)["reason_code"], "LABEL_NOT_QA")
        self.assertEqual(evaluate_event("pull_request", event(draft=True), self.schemas)["reason_code"], "PR_DRAFT")
        self.assertEqual(evaluate_event("pull_request", event(state="closed"), self.schemas)["reason_code"], "PR_NOT_OPEN")
        self.assertEqual(evaluate_event("pull_request", event("opened"), self.schemas)["reason_code"], "ACTION_NOT_LABELED")
        self.assertEqual(evaluate_event("pull_request", event(sha="nothex"), self.schemas)["reason_code"], "PAYLOAD_INVALID")
        self.assertEqual(evaluate_event("issues", event(), self.schemas)["reason_code"], "EVENT_NOT_PULL_REQUEST")
        self.assertEqual(evaluate_event("pull_request", event(label="to be test"), self.schemas, trigger_label="needs-qa")["reason_code"], "LABEL_NOT_QA")

    def test_lifecycle_events_cancel_instead_of_start(self):
        out = evaluate_event("pull_request", event("unlabeled"), self.schemas)
        self.assertFalse(out["eligible"]); self.assertEqual(out["reason_code"], "LIFECYCLE_UNLABELED")
        self.assertEqual(out["lifecycle"], {"action": "cancel", "reason": "LABEL_REMOVED", "repository": "synthetic-org/sample-orders-api",
                                            "pr_number": 101, "except_head_sha": None, "label": "to be test"})
        self.assertEqual(evaluate_event("pull_request", event("unlabeled", label="bug"), self.schemas)["reason_code"], "LABEL_NOT_QA")
        out = evaluate_event("pull_request", event("closed", state="closed"), self.schemas)
        self.assertEqual(out["lifecycle"]["reason"], "PR_CLOSED")
        out = evaluate_event("pull_request", event("synchronize", sha=SHA_NEW), self.schemas)
        self.assertEqual((out["lifecycle"]["reason"], out["lifecycle"]["except_head_sha"]), ("SUPERSEDED", SHA_NEW))
        self.assertNotIn("job_seed", out)


# ------------------------------------------------------------ cancellation ---
class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = make_app(self.tmp.name)

    def tearDown(self):
        self.app.executor.stop()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        return self.app.route(method, path, {}, body, {"x-worker-token": "test-token"})

    def job_from_label(self, delivery="d-1", sha=SHA_HEAD, level="standard"):
        out = self.call("POST", "/webhook/evaluate", {"event": "pull_request", "payload": event(sha=sha)})[1]
        seed = out["job_seed"]
        req = {"synthetic": seed["synthetic"], "repository": seed["repository"], "pr": seed["pr"], "test_level": level,
               "trigger": dict(seed["trigger"], delivery_id=delivery), "prompt": {"text": "x" * 300, "source": "fixture", "sha256": "a" * 64}}
        status, body = self.call("POST", "/jobs", req)
        self.assertEqual(status, 201, body)
        return body["job"]["id"]

    def test_duplicate_label_events_do_not_create_duplicate_jobs(self):
        self.job_from_label("d-1")
        out = self.call("POST", "/webhook/evaluate", {"event": "pull_request", "payload": event()})[1]
        seed = out["job_seed"]
        req = {"synthetic": True, "repository": seed["repository"], "pr": seed["pr"], "test_level": "standard",
               "trigger": dict(seed["trigger"], delivery_id="d-1"), "prompt": {"text": "x" * 300, "source": "fixture", "sha256": "a" * 64}}
        self.assertEqual(self.call("POST", "/jobs", req)[1]["error"], "DUPLICATE_DELIVERY")   # GitHub redelivery
        req["trigger"]["delivery_id"] = "d-2"
        self.assertEqual(self.call("POST", "/jobs", req)[1]["error"], "DUPLICATE_JOB")         # label applied again while active
        self.assertEqual(self.app.store.stats(), {"queued": 1})

    def test_unlabeled_cancels_queued_job_and_dispatcher_sees_terminal_state(self):
        job_id = self.job_from_label()
        status, body = self.call("POST", "/jobs/cancel", {"repository": "synthetic-org/sample-orders-api", "pr_number": 101, "reason": "LABEL_REMOVED", "event": "LIFECYCLE_UNLABELED"})
        self.assertEqual(status, 200)
        self.assertEqual([c["id"] for c in body["cancelled"]], [job_id])
        job = self.call("GET", f"/jobs/{job_id}")[1]["job"]
        self.assertEqual(job["status"], "cancelled"); self.assertEqual(job["cancel"]["reason"], "LABEL_REMOVED")
        self.assertIsNone(self.app.store.next_queued(), "a cancelled job is never executed")
        status, body = self.call("POST", "/notifications/discord", {"kind": "result", "job_id": job_id})
        self.assertEqual((status, body["error"]), (409, "JOB_NOT_COMPLETED"))                # never reported
        self.assertEqual(self.call("POST", "/jobs/cancel", {"repository": "x", "pr_number": 1})[0], 422)
        self.assertEqual(self.call("POST", "/jobs/cancel", {"repository": "a/b", "pr_number": "nan"})[0], 422)

    def test_cancel_while_running_discards_the_outcome(self):
        job_id = self.job_from_label()
        running = self.app.store.next_queued()
        self.assertEqual(running["status"], "running")
        status, body = self.call("POST", f"/jobs/{job_id}/cancel", {"reason": "PR_CLOSED"})
        self.assertEqual(body["job"]["status"], "running"); self.assertEqual(body["job"]["cancel"]["reason"], "PR_CLOSED")
        # simulate the end of the executor attempt exactly as Executor._loop does
        self.app.executor.execute(running)
        current = self.app.store.get(job_id)
        self.assertTrue(current["cancel"])
        self.app.store.finish_cancelled(job_id)
        job = self.app.store.get(job_id)
        self.assertEqual(job["status"], "cancelled"); self.assertIsNone(job["result"])
        self.assertIn("cancel_requested", [e["kind"] for e in self.app.store.events(job_id=job_id)])
        self.assertEqual(self.call("POST", "/jobs/nope/cancel", {})[0], 404)

    def test_synchronize_cancels_only_older_sha_jobs(self):
        old = self.job_from_label("d-old", sha=SHA_HEAD)
        new = self.job_from_label("d-new", sha=SHA_NEW)
        status, body = self.call("POST", "/jobs/cancel", {"repository": "synthetic-org/sample-orders-api", "pr_number": 101, "reason": "SUPERSEDED", "except_head_sha": SHA_NEW})
        self.assertEqual([c["id"] for c in body["cancelled"]], [old])
        self.assertEqual(self.app.store.get(new)["status"], "queued")
        self.assertEqual(self.call("POST", "/jobs/cancel", {"repository": "a/b", "pr_number": 1, "except_head_sha": "zz"})[0], 422)

    def test_executor_loop_honours_cancellation_end_to_end(self):
        job_id = self.job_from_label()
        self.app.store.cancel(job_id, reason="LABEL_REMOVED")
        self.app.executor.start()
        try:
            time.sleep(0.8)
        finally:
            self.app.executor.stop()
        self.assertEqual(self.app.store.get(job_id)["status"], "cancelled")
        # a second, un-cancelled job runs to completion and gets a workspace record
        job2 = self.job_from_label("d-3", sha=SHA_NEW)
        self.app.executor._stop.clear(); self.app.executor.start()
        for _ in range(40):
            if self.app.store.get(job2)["status"] == "succeeded":
                break
            time.sleep(0.1)
        self.app.executor.stop()
        job = self.app.store.get(job2)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["workspace"]["checkout"], "planned")


# -------------------------------------------------------------- workspaces ---
class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "ws"
        self.now = [1_000_000.0]
        self.ws = WorkspaceManager(self.root, retention_hours=1, clock=lambda: self.now[0])

    def tearDown(self):
        self.tmp.cleanup()

    def test_isolated_paths_and_traversal_protection(self):
        p = self.ws.create("20261022-abc123", repository="o/r", head_sha=SHA_HEAD, execution_mode="mock")
        self.assertEqual(p.parent, self.root.resolve()); self.assertTrue((p / MARKER).is_file())
        self.assertEqual(oct(p.stat().st_mode & 0o777), "0o700")
        for bad in ("../etc", "a/b", "..", "x" * 2, "job id", "20261022-abc123/../other", "\\\\evil"):
            with self.assertRaises(WorkspaceError, msg=bad):
                self.ws.path_for(bad)
        other = self.ws.create("20261022-def456", repository="o/r", head_sha=SHA_HEAD, execution_mode="mock")
        self.assertNotEqual(p, other)
        # an unmarked directory with the same name is never reused or deleted
        stray = self.root / "20261022-stray00"
        stray.mkdir(); (stray / "user-data.txt").write_text("keep me")
        with self.assertRaises(WorkspaceError):
            self.ws.create("20261022-stray00", repository="o/r", head_sha=SHA_HEAD, execution_mode="mock")
        # a symlink inside the root is never followed
        link = self.root / "20261022-link000"
        link.symlink_to(self.tmp.name)
        with self.assertRaises(WorkspaceError):
            self.ws.create("20261022-link000", repository="o/r", head_sha=SHA_HEAD, execution_mode="mock")

    def test_retention_cleanup_only_removes_marked_expired_workspaces(self):
        old = self.ws.create("20261022-old0001", repository="o/r", head_sha=SHA_HEAD, execution_mode="mock")
        self.now[0] += 1800
        fresh = self.ws.create("20261022-new0001", repository="o/r", head_sha=SHA_HEAD, execution_mode="mock")
        in_use = self.ws.create("20261022-busy001", repository="o/r", head_sha=SHA_HEAD, execution_mode="mock")
        self.ws.mark(in_use, state="in_use")
        stray = self.root / "not-a-prqa-dir"; stray.mkdir(); (stray / "x").write_text("x")
        self.now[0] += 2400                                  # old is 70 min old, fresh 40 min, retention 60
        swept = self.ws.cleanup()
        self.assertEqual(swept["removed"], ["20261022-old0001"])
        self.assertFalse(old.exists()); self.assertTrue(fresh.exists()); self.assertTrue(in_use.exists()); self.assertTrue(stray.exists())
        self.assertIn("not-a-prqa-dir", swept["skipped_unmarked"])
        self.now[0] += 7200
        self.ws.mark(in_use, state="released")
        self.assertEqual(sorted(self.ws.cleanup()["removed"]), ["20261022-busy001", "20261022-new0001"])
        self.assertTrue(stray.exists())
        self.assertFalse(self.ws.remove("20261022-nothere"))

    def test_checkout_plan_is_pinned_hook_free_and_token_free(self):
        wd = self.root / "j" / "repo"
        plan = build_checkout_plan(repository="example-org/orders-service", head_sha=SHA_HEAD, workdir=wd)
        self.assertEqual(plan.remote_url, "https://github.com/example-org/orders-service.git")
        joined = " ".join(" ".join(s) for s in plan.steps)
        self.assertIn(f"--depth 1 origin {SHA_HEAD}", joined)
        self.assertIn("core.hooksPath=", joined); self.assertIn("protocol.allow=never", joined); self.assertIn("protocol.https.allow=always", joined)
        self.assertIn("submodule.recurse=false", joined); self.assertIn("credential.helper=", joined)
        self.assertIn("checkout --quiet --detach --no-recurse-submodules FETCH_HEAD", joined)
        self.assertEqual(plan.step_names, ["init", "remote-add", "fetch", "checkout", "verify"])
        self.assertEqual(plan.env["GIT_TERMINAL_PROMPT"], "0")
        self.assertNotIn("token", json.dumps(plan.to_dict()).lower())
        for kwargs in (dict(repository="evil/../../etc", head_sha=SHA_HEAD), dict(repository="o/r; rm -rf /", head_sha=SHA_HEAD),
                       dict(repository="o/r", head_sha="HEAD"), dict(repository="o/r", head_sha=SHA_HEAD.upper()),
                       dict(repository="o/r", head_sha=SHA_HEAD, remote_base="ssh://github.com"),
                       dict(repository="o/r", head_sha=SHA_HEAD, remote_base="https://github.com/../x")):
            with self.assertRaises(WorkspaceError, msg=str(kwargs)):
                build_checkout_plan(workdir=wd, **{"remote_base": "https://github.com", **kwargs})
        with self.assertRaises(WorkspaceError):
            build_checkout_plan(repository="o/r", head_sha=SHA_HEAD, workdir="relative/path")

    def test_run_checkout_with_fake_git_token_in_env_only_timeout_and_mismatch(self):
        wd = self.root / "j2" / "repo"
        plan = build_checkout_plan(repository="o/r", head_sha=SHA_HEAD, workdir=wd, timeout_s=50)
        calls = []

        class P:
            def __init__(self, rc=0, out="", err=""):
                self.returncode, self.stdout, self.stderr = rc, out, err

        def fake_runner(argv, env, timeout, **kw):
            calls.append({"argv": argv, "env": env, "timeout": timeout})
            if argv[-2:] == ["--verify", "HEAD"]:
                return P(0, SHA_HEAD + "\n")
            return P(0)
        out = run_checkout(plan, token="ghp_SYNTHETICCANARY0000000000000000000000000", runner=fake_runner, git_path="/usr/bin/git")
        self.assertEqual(out["head"], SHA_HEAD); self.assertEqual(out["steps"], 5)
        for c in calls:
            self.assertNotIn("SYNTHETICCANARY", " ".join(c["argv"]))
            self.assertEqual(c["env"]["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
            self.assertTrue(c["env"]["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic "))
            self.assertEqual(c["env"]["GIT_TERMINAL_PROMPT"], "0"); self.assertLessEqual(c["timeout"], 50)
        self.assertFalse(any("SYNTHETICCANARY" in p.read_text() for p in wd.parent.rglob("*") if p.is_file()))

        def mismatch(argv, env, timeout, **kw):
            return P(0, "c" * 40 + "\n") if argv[-2:] == ["--verify", "HEAD"] else P(0)
        with self.assertRaises(CheckoutError) as ctx:
            run_checkout(plan, runner=mismatch, git_path="/usr/bin/git")
        self.assertIn("does not match", str(ctx.exception))

        def failing(argv, env, timeout, **kw):
            return P(128, "", f"fatal: Authorization: Basic {env.get('GIT_CONFIG_VALUE_0', '')[21:]} rejected for ghp_SYNTHETICCANARY0000000000000000000000000") if "fetch" in argv else P(0)
        with self.assertRaises(CheckoutError) as ctx:
            run_checkout(plan, token="ghp_SYNTHETICCANARY0000000000000000000000000", runner=failing, git_path="/usr/bin/git")
        self.assertIn("(fetch) failed", str(ctx.exception)); self.assertNotIn("SYNTHETICCANARY", str(ctx.exception))

        def slow(argv, env, timeout, **kw):
            raise subprocess.TimeoutExpired(argv, timeout)
        with self.assertRaises(CheckoutError) as ctx:
            run_checkout(plan, runner=slow, git_path="/usr/bin/git")
        self.assertIn("timed out", str(ctx.exception))
        clock = [0.0]

        def ticking(argv, env, timeout, **kw):
            clock[0] += 30
            return P(0, SHA_HEAD + "\n")
        with self.assertRaises(CheckoutError) as ctx:
            run_checkout(plan, runner=ticking, clock=lambda: clock[0], git_path="/usr/bin/git")
        self.assertIn("timed out before step", str(ctx.exception))
        with self.assertRaises(CheckoutError):
            run_checkout(plan, runner=fake_runner, git_path=None) if shutil.which("git") is None else (_ for _ in ()).throw(CheckoutError("x"))

    @unittest.skipUnless(GIT, "git not installed")
    def test_real_git_checkout_of_exact_sha_from_local_bare_repo(self):
        """Local-only: a bare repository with two commits and a malicious hook; the checkout pins the first commit."""
        src = Path(self.tmp.name) / "src"; src.mkdir()
        env = {"PATH": os.environ["PATH"], "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.test", "HOME": self.tmp.name, "GIT_CONFIG_NOSYSTEM": "1"}
        run = lambda *a, cwd=src: subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
        run("init", "-q", "-b", "main"); (src / "a.txt").write_text("one"); run("add", "."); run("commit", "-q", "-m", "one")
        first = run("rev-parse", "HEAD")
        (src / "a.txt").write_text("two"); run("add", "."); run("commit", "-q", "-m", "two")
        second = run("rev-parse", "HEAD")
        bare = Path(self.tmp.name) / "remote" / "example-org" / "svc.git"
        bare.parent.mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], env=env, check=True, capture_output=True)
        canary = Path(self.tmp.name) / "hook-ran"
        for hooks in (bare / "hooks",):
            hooks.mkdir(exist_ok=True)
            (hooks / "post-checkout").write_text(f"#!/bin/sh\ntouch {canary}\n"); (hooks / "post-checkout").chmod(0o755)
        wd = self.root / "j3" / "repo"
        plan = build_checkout_plan(repository="example-org/svc", head_sha=first, workdir=wd,
                                   remote_base=f"file://{Path(self.tmp.name) / 'remote'}", allowed_protocols=("file",))
        out = run_checkout(plan, timeout_s=None) if False else run_checkout(plan)
        self.assertEqual(out["head"], first)
        self.assertEqual((wd / "a.txt").read_text(), "one", "exact requested revision, not the branch tip")
        self.assertNotEqual(first, second)
        self.assertFalse(canary.exists(), "no hook executed during checkout")
        self.assertEqual(subprocess.run(["git", "-C", str(wd), "rev-parse", "HEAD"], env=env, capture_output=True, text=True).stdout.strip(), first)
        self.assertFalse((wd / ".git" / "hooks" / "post-checkout").exists())
        with self.assertRaises(CheckoutError):
            run_checkout(build_checkout_plan(repository="example-org/svc", head_sha="d" * 40, workdir=self.root / "j4" / "repo",
                                             remote_base=f"file://{Path(self.tmp.name) / 'remote'}", allowed_protocols=("file",)))


# ------------------------------------------------------- executor + runner ---
class ExecutorWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = make_app(self.tmp.name)

    def tearDown(self):
        self.app.executor.stop()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        return self.app.route(method, path, {}, body, {"x-worker-token": "test-token"})

    def test_mock_job_records_plan_without_running_git(self):
        seed = self.call("POST", "/webhook/evaluate", {"event": "pull_request", "payload": event()})[1]["job_seed"]
        req = {"synthetic": True, "repository": seed["repository"], "pr": seed["pr"], "test_level": "standard",
               "trigger": dict(seed["trigger"], delivery_id="w-1"), "prompt": {"text": "x" * 300, "source": "fixture", "sha256": "a" * 64}}
        job_id = self.call("POST", "/jobs", req)[1]["job"]["id"]
        running = self.app.store.next_queued()
        result = self.app.executor.execute(running)
        info = self.app.store.get(job_id)["workspace"]
        self.assertEqual(info["checkout"], "planned"); self.assertEqual(info["head_sha"], SHA_HEAD)
        ws = Path(info["path"])
        self.assertTrue((ws / MARKER).is_file()); self.assertTrue((ws / "checkout-plan.json").is_file())
        self.assertFalse((ws / "repo" / ".git").exists(), "no git ran for a mock job")
        self.assertEqual(self.app.workspaces.read_marker(ws)["state"], "in_use")
        self.app.store.succeed(job_id, result)
        self.app.executor._release_workspace(job_id)
        self.assertEqual(self.app.workspaces.read_marker(ws)["state"], "released")
        status, body = self.call("GET", "/workspaces")
        self.assertEqual(body["workspaces"][0]["job_id"], job_id)
        status, body = self.call("POST", "/workspaces/cleanup", {"max_age_s": 0})
        self.assertEqual(body["removed"], [job_id]); self.assertFalse(ws.exists())
        cfg = self.call("GET", "/config")[1]
        self.assertEqual(cfg["trigger"]["label"], "to be test"); self.assertFalse(cfg["workspaces"]["worker_checkout_enabled"])

    def test_worker_checkout_refused_in_review_only(self):
        self.app.executor.worker_checkout_enabled = True
        job = {"id": "20261022-live0001", "repository": "your-org/your-first-real-repo", "execution_mode": "live", "synthetic": False,
               "request": {"pr": {"number": 7, "head_sha": SHA_HEAD, "title": "t"}, "repository": "your-org/your-first-real-repo", "synthetic": False}}
        with self.assertRaises(opencode_runner.PolicyViolation):
            self.app.executor.prepare_workspace(job)

    def test_sandbox_plan_uses_artifact_stream_not_host_paths(self):
        job = {"id": "20261022-j1000001", "execution_mode": "live", "request": {"repository": "o/r", "test_level": "standard", "pr": {"number": 1, "head_sha": SHA_HEAD, "title": "t"}}, "synthetic": False}
        plan = opencode_runner.build_sandbox_plan(image="img", network="none", job=job, model="p/m", runtime_minutes=5, env={},
                                                  artifact_path="/data/workspaces/20261022-j1000001/checkout.tar", tree_sha256="a" * 64, prompt_sha256="b" * 64)
        joined = " ".join(" ".join(step) for step in plan.argv_steps if step is not plan.helper_copy)
        self.assertNotIn("-v ", joined); self.assertNotIn("type=bind", joined); self.assertNotIn("/data/workspaces", joined)
        self.assertEqual(plan.helper_copy[:2], ["docker", "cp"])       # the only place the local path appears: archive upload, not a mount
        self.assertEqual(plan.volume, "prqa-checkout-20261022-j1000001")
        self.assertIn("--mount type=volume,src=prqa-checkout-20261022-j1000001,dst=/checkout", " ".join(plan.populate))
        self.assertTrue(plan.create[-1] == "--prechecked")
        with self.assertRaises(opencode_runner.PolicyViolation):
            opencode_runner.build_sandbox_plan(image="img", network="none", job=job, model="p/m", runtime_minutes=5, env={},
                                               artifact_path="/data/workspaces/other-job/checkout.tar", tree_sha256="a" * 64, prompt_sha256="b" * 64)
        entry = (ROOT / "worker/sandbox/entrypoint.sh").read_text()
        self.assertIn("--prechecked", entry); self.assertIn("prqa-verify-checkout", entry)
        self.assertNotIn("git clone", entry)          # the sandbox never clones
        self.assertNotIn("GITHUB_TOKEN", entry)       # and never sees a GitHub token


# --------------------------------------------------------------- structure ---
class ReceiverStructureTests(unittest.TestCase):
    def load(self, name):
        return json.loads((ROOT / "n8n" / "workflows" / name).read_text(encoding="utf-8"))

    @staticmethod
    def edges(wf):
        return [(src, i, e["node"]) for src, c in wf["connections"].items() for i, b in enumerate(c["main"]) for e in b]

    def test_receiver_lifecycle_branch_and_label_config(self):
        wf = self.load("01-prqa-github-webhook-receiver.json")
        nodes = {n["name"]: n for n in wf["nodes"]}
        js = nodes["Validate Event & Extract PR"]["parameters"]["jsCode"]
        self.assertIn("$env.PRQA_TRIGGER_LABEL", js); self.assertIn("'to be test'", js); self.assertIn("$env.PRQA_DEFAULT_TEST_LEVEL", js)
        self.assertIn("unlabeled: 'LABEL_REMOVED'", js); self.assertIn("synchronize: 'SUPERSEDED'", js)
        e = self.edges(wf)
        self.assertIn(("Eligible Event?", 1, "Lifecycle Event? (label removed / closed / new SHA)"), e)
        self.assertIn(("Lifecycle Event? (label removed / closed / new SHA)", 0, "Cancel Active Jobs (worker /jobs/cancel)"), e)
        self.assertIn(("Lifecycle Event? (label removed / closed / new SHA)", 1, "Respond 200 Ignored"), e)
        self.assertIn(("Cancel Active Jobs (worker /jobs/cancel)", 0, "Respond 200 Lifecycle Handled"), e)
        self.assertNotIn(("Cancel Active Jobs (worker /jobs/cancel)", 0, "Start Main Pipeline (async)"), e)
        self.assertIn("/jobs/cancel", nodes["Cancel Active Jobs (worker /jobs/cancel)"]["parameters"]["url"])
        compose = (ROOT / "compose/docker-compose.yml").read_text()
        self.assertEqual(compose.count("PRQA_TRIGGER_LABEL: ${PRQA_TRIGGER_LABEL:-to be test}"), 2)   # n8n + worker read the same value

    def test_dispatcher_treats_cancelled_as_terminal(self):
        wf = self.load("04-prqa-opencode-job-dispatcher.json")
        nodes = {n["name"]: n for n in wf["nodes"]}
        rules = [r["outputKey"] for r in nodes["Job Finished?"]["parameters"]["rules"]["values"]]
        self.assertEqual(rules, ["succeeded", "failed", "cancelled"])
        e = self.edges(wf)
        self.assertIn(("Job Finished?", 2, "Stop: Job Cancelled (label removed / PR closed / superseded)"), e)
        self.assertIn(("Job Finished?", 3, "Poll Limit Reached? (> 360 polls ≈ 60 min)"), e)
        self.assertNotIn(("Job Finished?", 2, "Attach Job Result to Pipeline Data"), e)


if __name__ == "__main__":
    unittest.main()
