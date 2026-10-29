"""scripts/pipeline/prqa_run_single_pr.py — preflight / preview / execute / cleanup for ONE PR (synthetic private project).

Offline: GitHub, Docker and the disposable environment are a fake Host; the worker is the real in-process App in
review-only mode with a single-run grant key (so grant consumption, policy and the job store are the shipped code).

Run: cd "<project>" && PYTHONPATH=worker python3 -m unittest tests.test_run_single_pr -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))

from prqa_worker import run_grants  # noqa: E402
from prqa_worker.server import App  # noqa: E402
from tests.test_launch_lifecycle import WT, live_env  # noqa: E402

spec = importlib.util.spec_from_file_location("prqa_run_single_pr", ROOT / "scripts" / "pipeline" / "prqa_run_single_pr.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)  # type: ignore[union-attr]

# A synthetic PRIVATE project (no real repository): keeps the GITHUB_TOKEN / local-mirror paths and a two-branch base
# list covered now that the only connected project (Kiramido1/Amenly-Product) is public with base main only.
TEST_PROJECT = "acme/private-api"
R.PROJECTS[TEST_PROJECT] = {"slug": "acme", "env_profile": "amenly", "bases": ("main", "Dev"), "private": True}
R.select_project(TEST_PROJECT)

KEY = "3c" * 32
REPO = R.REPO
SHA = "fe17f7d3a2cd86af61bf52e3c7e73a1364607411"
SHA2 = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "55c2295006f6a675c6404b40e72cb36577978dfb"
DIFF = """diff --git a/backend/app/schemas/org.py b/backend/app/schemas/org.py
--- a/backend/app/schemas/org.py
+++ b/backend/app/schemas/org.py
@@ -94,6 +94,8 @@ class OrganizationCreate(BaseModel):
     team_type: str | None = Field(default=None, max_length=40)
+    # The days this organization works; omitted = the default week.
+    working_days: list[str] | None = Field(default=None, max_length=7)
"""
OPENAPI = {"openapi": "3.1.0", "info": {"title": "Acme API", "version": "0.1.0"}, "paths": {
    "/api/v1/organizations": {"post": {"responses": {"201": {"description": "Successful Response"}, "422": {"description": "Validation Error"}}},
                              "get": {"responses": {"200": {"description": "Successful Response"}}}},
    "/api/v1/auth/register": {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/UserRegister"}}}},
                                       "responses": {"201": {"description": "Successful Response"}, "422": {"description": "Validation Error"}}}},
    "/api/v1/auth/me": {"get": {"security": [{"bearerAuth": []}], "responses": {"200": {"description": "Successful Response"}}}}},
    "components": {"schemas": {"UserRegister": {"properties": {"name": {}, "email": {}, "password": {}}}}}}


def pr_json(n=7, *, sha=SHA, draft=False, state="open", base="Dev", labels=("to be test",), fork=False):
    return {"number": n, "title": "feat(org): choose the organization's working days when creating it", "state": state, "draft": draft,
            "body": "`POST /organizations` accepts `working_days`; omitted = the default week.", "html_url": f"https://github.com/{REPO}/pull/{n}",
            "user": {"login": "dev"}, "labels": [{"name": l} for l in labels],
            "head": {"sha": sha, "ref": "feat/x", "repo": {"full_name": "someone/fork" if fork else REPO, "fork": fork}},
            "base": {"ref": base, "sha": BASE_SHA, "repo": {"full_name": REPO}}}


class FakeHost:
    def __init__(self, app, prs, *, secrets=("GITHUB_TOKEN", "PRQA_RUN_GRANT_KEY"), key=KEY, env_up_rc=0, env_healthy=True,
                 probe_ok=True, down_rc=0, base_url=None, auth_ok=True):
        self.app, self.prs, self.names, self.key = app, dict(prs), set(secrets), key
        self.env_up_rc, self.env_healthy, self.probe_ok, self.down_rc = env_up_rc, env_healthy, probe_ok, down_rc
        self.base_url = base_url or R.EXPECTED_BASE_URL
        self.auth_ok = auth_ok
        self.calls: list[tuple] = []
        self.now = 1_900_000_000.0

    def gh_get(self, path):
        self.calls.append(("gh_get", path))
        return json.loads(json.dumps(self.prs[int(path.rsplit("/", 1)[1])]))

    def gh_paginate(self, path):
        self.calls.append(("gh_paginate", path))
        if path.endswith("/files"):
            return [{"filename": "backend/app/schemas/org.py", "status": "modified", "additions": 2, "deletions": 0}]
        return [json.loads(json.dumps(p)) for p in self.prs.values() if p["state"] == "open"]

    def gh_diff(self, n):
        self.calls.append(("gh_diff", n))
        return DIFF

    def gh_auth_ok(self):
        return True

    def poll_select(self):
        cands = [{"number": p["number"]} for p in self.prs.values() if not R.eligibility(p)]
        return {"candidates": cands, "selected": min(cands, key=lambda c: c["number"]) if cands else None}

    def fetch_commit(self, workdir, sha):
        self.calls.append(("fetch_commit", sha))
        workdir.mkdir(parents=True, exist_ok=True)
        return sha

    def secret_names(self):
        return set(self.names)

    def grant_key(self):
        return self.key if "PRQA_RUN_GRANT_KEY" in self.names else ""

    def worker(self, method, path, body=None):
        self.calls.append(("worker", method, path))
        return self.app.route(method, path, {}, body, {"x-worker-token": WT})

    def docker_lines(self, *args):
        if args[0] == "network":
            return 0, "true sandbox"
        return 0, "true"

    def env_up(self, *, run_id, git_dir, sha, state_dir, private_dir):
        self.calls.append(("env_up", run_id, sha))
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "env-state.json").write_text(json.dumps({"run_id": run_id, "base_url": R.EXPECTED_BASE_URL}))
        if self.env_up_rc:
            return self.env_up_rc, {"error": "alembic upgrade failed"}
        (state_dir / "openapi.json").write_text(json.dumps(OPENAPI))
        return 0, {"healthy": self.env_healthy, "base_url": R.EXPECTED_BASE_URL, "probe": ["health 200" if self.env_healthy else "health 502"]}

    def env_probe(self, state_dir):
        self.calls.append(("env_probe", str(state_dir)))
        return (0, {"reachable": True}) if self.probe_ok else (1, {"reachable": False, "probe": ["health 000"]})

    def env_auth_probe(self, state_dir, register_path, me_path):
        self.calls.append(("env_auth_probe", register_path, me_path))
        lines = ["register 201", "token present", "me_anonymous 401", "me_token 200"] if self.auth_ok else ["register 422", "token absent"]
        return (0 if self.auth_ok else 1), {"ok": self.auth_ok, "probe": lines}

    def env_down(self, state_dir):
        self.calls.append(("env_down", str(state_dir)))
        return self.down_rc, ({"removed": ["container:x"]} if not self.down_rc else {"error": "docker rm failed"})

    def ledger(self):
        return {"requests_today": 6, "tokens_today": 40636, "in_flight": 0}

    def registry_entry(self):
        return {"kind": "real", "enabled": False, "approval_status": "pending_approval", "allowed_labels": ["qa-test:standard"],
                "environment": "staging", "base_url": self.base_url}

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s


ACCEPT = {"accepted": True, "failure": None, "score": 90.0, "threshold": 85.0, "hard_fails": [], "gate_errors": [], "gate_warnings": [],
          "human_flags": [], "dimensions": {}, "worker_and_local_builder_consistent": True}


class RunnerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        reg = (ROOT / "config/repositories.yaml").read_text().replace('base_url: "https://replace-me.staging.example.test"',
                                                                       f'base_url: "{R.EXPECTED_BASE_URL}"')
        reg = reg.rstrip("\n") + f"""

  - full_name: {TEST_PROJECT}
    kind: real
    enabled: false
    approval_status: pending_approval
    display_name: "synthetic private test project"
    default_branch: main
    allowed_events: [labeled]
    api:
      environment: staging
      base_url: "{R.EXPECTED_BASE_URL}"
      openapi_path: "/api/v1/openapi.json"
      auth_profiles:
        anonymous: {{ type: none }}
    max_runtime_minutes: 30
    opencode:
      model: ""
    approved_by: null
    approved_at: null
"""
        (t / "registry.yaml").write_text(reg)
        (t / "data").mkdir()
        env = live_env(str(t / "data"), AUTOMATION_MODE="review_only", REAL_PROJECT_TESTING_ENABLED="false", AUTO_EXECUTION_ENABLED="false",
                       PRQA_WORKER_CHECKOUT_ENABLED="false", PRQA_REGISTRY_PATH=str(t / "registry.yaml"), PRQA_RUN_GRANT_KEY=KEY,
                       GITHUB_TOKEN="github_pat_SYNTHETIC_" + "x" * 30, PRQA_SANDBOX_NETWORK=R.SANDBOX_NETWORK)
        self.app = App(env)
        self.app.clock = lambda: 1_900_000_000
        self.patches = [mock.patch.object(R, "RUNS_DIR", t / "runs"), mock.patch.object(R, "PRIVATE_ROOT", t / "private"),
                        mock.patch.object(R, "PREVIEWS_DIR", t / "previews")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.app.executor.stop()
        self.tmp.cleanup()

    def host(self, prs=None, **kw):
        return FakeHost(self.app, prs or {7: pr_json(7), 5: pr_json(5, base="main", labels=())}, **kw)

    def run_preview(self, host, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = R.preview(host, **kw)
        return rc, buf.getvalue()

    def previewed(self, host, accept=True):
        with mock.patch.object(R, "score_prompt", return_value=dict(ACCEPT, accepted=accept)):
            rc, out = self.run_preview(host)
        self.assertEqual(rc, 0 if accept else 4, out)
        return R.RUNS_DIR / f"acme-pr-7-{SHA}"

    def grant(self, *, sha=SHA, pr=7, now=1_900_000_000, ttl=900):
        token, _ = run_grants.mint(KEY, repository=REPO, pr_number=pr, head_sha=sha, now=now, ttl_s=ttl)
        f = Path(self.tmp.name) / f"g-{len(list(Path(self.tmp.name).glob('g-*')))}.grant"
        f.write_text(token)
        return f

    def execute(self, host, run_dir, grant_file=None, **kw):
        args = dict(run_dir=run_dir, confirm_repo=REPO, confirm_pr=7, confirm_sha=SHA, grant_file=grant_file, approved=True, wait_s=30)
        args.update(kw)
        with redirect_stdout(io.StringIO()):
            return R.execute(host, **args)

    def jobs(self):
        return self.app.store.list()


class DiscoveryTests(RunnerBase):
    def test_eligibility_reasons(self):
        self.assertEqual(R.eligibility(pr_json()), [])
        cases = {"draft": pr_json(draft=True), "not open": pr_json(state="closed"), "fork": pr_json(fork=True),
                 "base": pr_json(base="release"), "label": pr_json(labels=("To Be Test",)), "label ": pr_json(labels=("needs-review",))}
        for word, pr in cases.items():
            self.assertTrue(any(word.strip() in r for r in R.eligibility(pr)), (word, R.eligibility(pr)))

    def test_no_eligible_pr_stops_without_side_effects(self):
        host = self.host({7: pr_json(7, labels=("needs-review",)), 8: pr_json(8, draft=True), 5: pr_json(5, labels=())})
        rc, out = self.run_preview(host)
        self.assertEqual(rc, 3)
        self.assertIn("no eligible PR", out)
        self.assertIn("#7", out); self.assertIn("label 'to be test' missing", out)
        kinds = {c[0] for c in host.calls}
        self.assertNotIn("env_up", kinds); self.assertNotIn("fetch_commit", kinds); self.assertNotIn("worker", kinds)
        self.assertEqual(len(list(R.RUNS_DIR.glob("acme-preview-blocked-*.json"))), 1)

    def test_pinned_ineligible_pr_is_refused(self):
        host = self.host({7: pr_json(7), 9: pr_json(9, fork=True)})
        rc, out = self.run_preview(host, pinned=9)
        self.assertEqual(rc, 3)
        self.assertIn("fork", out)

    def test_deterministic_lowest_number(self):
        host = self.host({12: pr_json(12, sha=SHA2), 7: pr_json(7)})
        self.assertEqual(R.discover(host, None)["selected"], 7)


class PreflightTests(RunnerBase):
    def test_missing_github_token_and_grant_key_block_execute_not_preview(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            env = live_env(tmp.name, AUTOMATION_MODE="review_only", REAL_PROJECT_TESTING_ENABLED="false", AUTO_EXECUTION_ENABLED="false",
                           PRQA_WORKER_CHECKOUT_ENABLED="false", PRQA_REGISTRY_PATH=str(ROOT / "config/repositories.yaml"),
                           PRQA_SANDBOX_NETWORK=R.SANDBOX_NETWORK)
            app = App(env)
            host = FakeHost(app, {7: pr_json()}, secrets=(), base_url="https://replace-me.staging.example.test")
            res = R.preflight(host)
            app.executor.stop()
        finally:
            tmp.cleanup()
        self.assertEqual(res["blocks_preview"], [])
        joined = " | ".join(res["blocks_execute"])
        for needle in ("GITHUB_TOKEN", "PRQA_RUN_GRANT_KEY", "api.base_url"):
            self.assertIn(needle, joined)


class PreviewTests(RunnerBase):
    def test_preview_saves_prints_and_has_no_execution_side_effects(self):
        host = self.host()
        rc, out = self.run_preview(host)
        self.assertIn(rc, (0, 4))
        run_dir = R.RUNS_DIR / f"acme-pr-7-{SHA}"
        text = (run_dir / "qa-prompt.md").read_text()
        self.assertIn(text.strip()[:500], out)                                   # the complete prompt is printed
        self.assertIn("PR #7", text)
        state = json.loads((run_dir / "preview.json").read_text())
        meta = json.loads((run_dir / "metadata.json").read_text())
        self.assertEqual((state["status"], state["head_sha"], state["base_ref"]), ("previewed", SHA, "Dev"))
        self.assertEqual(meta["prompt"]["sha256"], R.sha256(text))
        self.assertEqual(meta["prompt"]["source"], "reference_fallback")
        self.assertEqual(meta["quality_gate"]["threshold"], 85.0)
        self.assertEqual(state["quality_accepted"], meta["quality_gate"]["accepted"])
        worker_paths = [c[2] for c in host.calls if c[0] == "worker"]
        self.assertEqual(worker_paths, ["/context/build", "/prompts/finalize"])     # no /jobs, nothing else
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.app.store.grant_audit_log(), [])
        self.assertEqual([c for c in host.calls if c[0] == "env_up"][0][2], SHA)

    def test_env_start_failure_is_a_blocker_and_partial_resources_are_removed(self):
        for kw in ({"env_up_rc": 1}, {"env_healthy": False}):
            host = self.host(**kw)
            with self.assertRaises(R.Blocked) as ctx:
                self.run_preview(host)
            self.assertEqual(ctx.exception.code, "ENV_START_OR_HEALTH_FAILED")
            self.assertIn("env_down", [c[0] for c in host.calls])
            state = json.loads((R.RUNS_DIR / f"acme-pr-7-{SHA}" / "preview.json").read_text())
            self.assertEqual(state["status"], "blocked")
            self.assertEqual(self.jobs(), [])

    def test_live_preview_must_be_cleaned_before_a_new_one(self):
        host = self.host()
        self.previewed(host)
        with self.assertRaises(R.Blocked) as ctx:
            self.run_preview(host)
        self.assertEqual(ctx.exception.code, "PREVIEW_EXISTS")


class ExecuteTests(RunnerBase):
    def test_happy_path_consumes_grant_and_creates_exactly_one_job(self):
        host = self.host()
        run_dir = self.previewed(host)
        rc = self.execute(host, run_dir, self.grant())
        self.assertEqual(rc, 5)                                                 # job queued; nothing executes it offline
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual((job["execution_mode"], job["head_sha"], job["request"]["prompt"]["sha256"]),
                         ("live", SHA, R.sha256((run_dir / "qa-prompt.md").read_text())))
        self.assertNotIn("run_grant", job["request"])
        state = json.loads((run_dir / "preview.json").read_text())
        self.assertEqual(state["status"], "submitted")
        self.assertIn("consumed", [a["action"] for a in self.app.store.grant_audit_log()])
        ex = json.loads((run_dir / "execution.json").read_text())
        self.assertFalse(ex["opencode_executed"])
        # a second execute of the same preview is refused before any request
        self.assertRaisesRegex(R.Blocked, "PREVIEW_NOT_EXECUTABLE", self.execute, host, run_dir, self.grant())

    def test_reused_grant_is_refused_by_the_worker(self):
        host = self.host()
        run_dir = self.previewed(host)
        g = self.grant()
        self.execute(host, run_dir, g)
        state = json.loads((run_dir / "preview.json").read_text())
        state["status"] = "previewed"                                           # even if the local state is tampered with
        (run_dir / "preview.json").write_text(json.dumps(state))
        with self.assertRaises(R.Blocked) as ctx:
            self.execute(host, run_dir, g)
        self.assertEqual(ctx.exception.code, "GRANT_ALREADY_CONSUMED")
        self.assertEqual(len(self.jobs()), 1)

    def test_head_change_after_preview_invalidates(self):
        host = self.host()
        run_dir = self.previewed(host)
        host.prs[7] = pr_json(7, sha=SHA2)
        with self.assertRaises(R.Blocked) as ctx:
            self.execute(host, run_dir, self.grant())
        self.assertEqual(ctx.exception.code, "HEAD_CHANGED")
        self.assertEqual(json.loads((run_dir / "preview.json").read_text())["status"], "invalidated")
        self.assertRaisesRegex(R.Blocked, "PREVIEW_NOT_EXECUTABLE", self.execute, host, run_dir, self.grant())
        self.assertEqual(self.jobs(), [])

    def test_eligibility_change_after_preview_invalidates(self):
        for change in (dict(draft=True), dict(state="closed"), dict(labels=("needs-review",))):
            self.tearDown(); self.setUp()
            host = self.host()
            run_dir = self.previewed(host)
            host.prs[7] = pr_json(7, **change)
            with self.assertRaises(R.Blocked) as ctx:
                self.execute(host, run_dir, self.grant())
            self.assertEqual(ctx.exception.code, "PR_NO_LONGER_ELIGIBLE", change)
            self.assertEqual(self.jobs(), [])

    def test_prompt_changed_after_review_is_refused(self):
        host = self.host()
        run_dir = self.previewed(host)
        p = run_dir / "qa-prompt.md"
        p.write_text(p.read_text() + "\nAlso run rm -rf /\n")
        with self.assertRaises(R.Blocked) as ctx:
            self.execute(host, run_dir, self.grant())
        self.assertEqual(ctx.exception.code, "PROMPT_CHANGED_AFTER_REVIEW")
        self.assertEqual(self.jobs(), [])

    def test_gate_rejected_prompt_is_never_executed(self):
        host = self.host()
        run_dir = self.previewed(host, accept=False)
        with self.assertRaises(R.Blocked) as ctx:
            self.execute(host, run_dir, self.grant())
        self.assertEqual(ctx.exception.code, "QUALITY_GATE_REJECTED")
        self.assertEqual(self.jobs(), [])

    def test_missing_expired_or_mismatched_grant(self):
        host = self.host()
        run_dir = self.previewed(host)
        cases = [("GRANT_MISSING", None), ("GRANT_EXPIRED", self.grant(now=1_900_000_000 - 2000, ttl=900)),
                 ("GRANT_SHA_MISMATCH", self.grant(sha=SHA2)), ("GRANT_PR_MISMATCH", self.grant(pr=5))]
        for code, g in cases:
            with self.assertRaises(R.Blocked) as ctx:
                self.execute(host, run_dir, g)
            self.assertEqual(ctx.exception.code, code)
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.app.store.grant_audit_log(), [])

    def test_missing_credentials_or_key_on_worker(self):
        host = self.host()
        run_dir = self.previewed(host)
        self.app.executor._github_token = ""
        for env_key, code in (("GITHUB_TOKEN", "GITHUB_READ_CREDENTIAL_MISSING"),):
            saved = self.app.env.pop(env_key)
            with self.assertRaises(R.Blocked) as ctx:
                self.execute(host, run_dir, self.grant())
            self.assertEqual(ctx.exception.code, code)
            self.app.env[env_key] = saved
        self.app.run_grant_key = ""
        with self.assertRaises(R.Blocked) as ctx:
            self.execute(host, run_dir, self.grant())
        self.assertEqual(ctx.exception.code, "GRANT_KEY_NOT_CONFIGURED")
        self.assertEqual(self.jobs(), [])

    def test_sandbox_cannot_reach_api(self):
        host = self.host(probe_ok=False)
        run_dir = self.previewed(host)
        with self.assertRaises(R.Blocked) as ctx:
            self.execute(host, run_dir, self.grant())
        self.assertEqual(ctx.exception.code, "SANDBOX_CANNOT_REACH_API")
        self.assertEqual(self.jobs(), [])

    def test_registry_base_url_must_be_the_disposable_api(self):
        host = self.host(base_url="https://replace-me.staging.example.test")
        run_dir = self.previewed(host)
        self.assertRaisesRegex(R.Blocked, "REGISTRY_BASE_URL_NOT_DISPOSABLE", self.execute, host, run_dir, self.grant())

    def test_explicit_flag_and_exact_confirmation_required(self):
        host = self.host()
        run_dir = self.previewed(host)
        self.assertRaisesRegex(R.Blocked, "EXECUTE_FLAG_REQUIRED", self.execute, host, run_dir, self.grant(), approved=False)
        for kw in ({"confirm_sha": SHA2}, {"confirm_pr": 5}, {"confirm_repo": "zedny-labs/Other"}):
            self.assertRaisesRegex(R.Blocked, "CONFIRMATION_MISMATCH", self.execute, host, run_dir, self.grant(), **kw)
        self.assertEqual(self.jobs(), [])


class PreviewOnlyTests(RunnerBase):
    """`preview --pr N --preview-only`: an explicitly selected PR without the trigger label; never executable."""

    def unlabelled(self, **kw):
        return self.host({7: pr_json(7, labels=("needs-review",), **kw)})

    def run_po(self, host, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = R.preview_only(host, number=7, **kw)
        return rc, buf.getvalue()

    def test_unlabelled_pr_is_previewed_without_any_execution_side_effect(self):
        host = self.unlabelled()
        before = json.dumps(host.prs)
        rc, out = self.run_po(host)
        self.assertIn(rc, (0, 4))
        run_dir = R.PREVIEWS_DIR / f"acme-pr-7-{SHA}"
        text = (run_dir / "qa-prompt.md").read_text()
        self.assertIn(text.strip()[:500], out)
        meta = json.loads((run_dir / "metadata.json").read_text())
        state = json.loads((run_dir / "preview.json").read_text())
        self.assertEqual((meta["preview_only"], meta["execution_eligible"]), (True, False))
        self.assertEqual((state["status"], state["preview_only"], state["execution_eligible"]), ("preview_only", True, False))
        self.assertEqual((meta["head_sha"], meta["base_branch"], meta["labels"]), (SHA, "Dev", ["needs-review"]))
        self.assertEqual(meta["prompt"]["sha256"], R.sha256(text))
        self.assertEqual(meta["prompt"]["source"], "reference_fallback")
        self.assertTrue(any("label" in u for u in meta["unresolved_assumptions"]))
        self.assertEqual([c[2] for c in host.calls if c[0] == "worker"], ["/context/build", "/prompts/finalize"])
        self.assertFalse([c for c in host.calls if c[0] in ("fetch_commit", "env_up", "env_down", "poll_select")])
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.app.store.grant_audit_log(), [])
        self.assertEqual(json.dumps(host.prs), before)                            # nothing on the PR changed
        self.assertFalse(R.RUNS_DIR.exists() and any(R.RUNS_DIR.iterdir()))       # nothing in the execution area

    def test_preview_only_still_requires_open_non_draft_same_repo_supported_base(self):
        for kw in ({"draft": True}, {"state": "closed"}, {"fork": True}, {"base": "feature"}):
            host = self.unlabelled(**kw)
            rc, out = self.run_po(host)
            self.assertEqual(rc, 3, kw)
            self.assertIn("PREVIEW STOPPED", out)
            self.assertFalse([c for c in host.calls if c[0] == "worker"])
        self.assertFalse(R.PREVIEWS_DIR.exists())

    def test_pr_change_during_preview_stops_and_saves_nothing(self):
        host = self.unlabelled()
        real = host.gh_get
        seen = []

        def moving(path):
            pr = real(path)
            seen.append(1)
            if len(seen) > 1:
                pr["head"]["sha"] = SHA2
            return pr
        host.gh_get = moving
        with self.assertRaises(R.Blocked) as ctx:
            self.run_po(host)
        self.assertEqual(ctx.exception.code, "PR_CHANGED_DURING_PREVIEW")
        self.assertFalse(R.PREVIEWS_DIR.exists())

    def test_openapi_file_must_be_bound_to_the_current_head(self):
        spec = Path(self.tmp.name) / "openapi.json"
        spec.write_text(json.dumps({"openapi": "3.1.0", "paths": {}}))
        with self.assertRaises(R.Blocked) as ctx:
            self.run_po(self.unlabelled(), openapi_file=spec, openapi_head_sha=SHA2)
        self.assertEqual(ctx.exception.code, "OPENAPI_HEAD_MISMATCH")
        rc, _ = self.run_po(self.unlabelled(), openapi_file=spec, openapi_head_sha=SHA)
        meta = json.loads((R.PREVIEWS_DIR / f"acme-pr-7-{SHA}" / "metadata.json").read_text())
        self.assertTrue(meta["contract"]["verified_for_head"])
        self.assertEqual(meta["contract"]["sha256"], R.sha256(spec.read_text()))

    def test_execute_refuses_a_preview_only_run_even_with_a_valid_grant(self):
        host = self.unlabelled()
        with mock.patch.object(R, "score_prompt", return_value=dict(ACCEPT, accepted=True)):
            self.run_po(host)
        with self.assertRaises(R.Blocked) as ctx:
            self.execute(host, R.PREVIEWS_DIR / f"acme-pr-7-{SHA}", self.grant())
        self.assertEqual(ctx.exception.code, "PREVIEW_ONLY_NOT_EXECUTABLE")
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.app.store.grant_audit_log(), [])

    def test_label_rules_of_the_execution_path_are_unchanged(self):
        self.assertIn("label 'to be test' missing (labels: needs-review)", R.eligibility(pr_json(7, labels=("needs-review",))))
        host = self.unlabelled()
        rc, out = self.run_preview(host, pinned=7)
        self.assertEqual(rc, 3)
        self.assertIn("not eligible", out)
        self.assertFalse([c for c in host.calls if c[0] in ("worker", "fetch_commit", "env_up")])

    def test_cli_requires_an_explicit_pr(self):
        with redirect_stdout(io.StringIO()) as buf:
            rc = R.main(["--repo", TEST_PROJECT, "preview", "--preview-only"], host=self.unlabelled())
        self.assertEqual(rc, 2)
        self.assertIn("PR_REQUIRED", buf.getvalue())


class CleanupTests(RunnerBase):
    def test_cleanup_failure_is_reported_and_retryable(self):
        host = self.host(down_rc=1)
        run_dir = self.previewed(host)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(R.cleanup(host, run_dir=run_dir), 6)
        state = json.loads((run_dir / "preview.json").read_text())
        self.assertNotIn("cleaned_at", state)
        host.down_rc = 0
        with redirect_stdout(io.StringIO()):
            self.assertEqual(R.cleanup(host, run_dir=run_dir), 0)
        state = json.loads((run_dir / "preview.json").read_text())
        self.assertEqual(state["status"], "cleaned")
        self.assertFalse(Path(state["private_dir"]).exists())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(R.cleanup(host, run_dir=run_dir), 0)          # idempotent: no second teardown
        self.assertEqual(sum(1 for c in host.calls if c[0] == "env_down"), 2)

    def test_cleanup_never_removes_paths_outside_the_private_root(self):
        host = self.host()
        run_dir = self.previewed(host)
        outside = Path(self.tmp.name) / "users-files"
        outside.mkdir()
        state = json.loads((run_dir / "preview.json").read_text())
        state["private_dir"] = str(outside)
        (run_dir / "preview.json").write_text(json.dumps(state))
        with redirect_stdout(io.StringIO()):
            R.cleanup(host, run_dir=run_dir)
        self.assertTrue(outside.exists())


if __name__ == "__main__":
    unittest.main()
