"""Simulation mode: the real pipeline on an explicit PR without the trigger label, with the worker checking out the
pinned head from a local bare mirror (file protocol, /data/mirrors only) instead of GitHub — no GitHub token anywhere.

Offline: the fake Host of tests/test_run_single_pr.py and the real in-process worker App (review-only, grant key, NO
GITHUB_TOKEN, PRQA_CHECKOUT_REMOTE_BASE=file:///data/mirrors). The mirror checkout test runs real git on a temp repo.

Run: cd "<project>" && PYTHONPATH=worker python3 -m unittest tests.test_simulation_mode -v
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))

from prqa_worker.executor import validate_local_mirror_base  # noqa: E402
from prqa_worker.server import App  # noqa: E402
from prqa_worker.workspace import build_checkout_plan, run_checkout  # noqa: E402
from tests.test_auto_runner import REJECT, A, AutoBase  # noqa: E402
from tests.test_launch_lifecycle import live_env  # noqa: E402
from tests.test_run_single_pr import KEY, R, SHA, pr_json  # noqa: E402

MIRROR = "file:///data/mirrors"


class SimBase(AutoBase):
    def setUp(self):
        super().setUp()
        self.app.executor.stop()
        t = Path(self.tmp.name)
        env = live_env(str(t / "data"), AUTOMATION_MODE="review_only", REAL_PROJECT_TESTING_ENABLED="false", AUTO_EXECUTION_ENABLED="false",
                       PRQA_WORKER_CHECKOUT_ENABLED="false", PRQA_REGISTRY_PATH=str(t / "registry.yaml"), PRQA_RUN_GRANT_KEY=KEY,
                       PRQA_SANDBOX_NETWORK=R.SANDBOX_NETWORK, PRQA_CHECKOUT_REMOTE_BASE=MIRROR)
        env.pop("GITHUB_TOKEN", None)
        self.app = App(env)
        self.app.clock = lambda: 1_900_000_000

    def host(self, prs=None, **kw):
        kw.setdefault("secrets", ("PRQA_RUN_GRANT_KEY",))
        return super().host(prs or {7: pr_json(7, labels=("needs-review",))}, **kw)

    def simulate(self, host, n=7):
        buf = io.StringIO()
        with redirect_stdout(buf):
            res = A.simulate(host, n, state_dir=self.state_dir, auto_dir=self.auto_dir, wait_s=0, out=print)
        self.out = buf.getvalue()
        return res


class SimulationPipelineTests(SimBase):
    def test_simulation_runs_without_label_or_github_token_and_is_marked(self):
        host = self.host()
        res = self.simulate(host)
        entry = res["processed"]
        self.assertTrue(entry["simulation"])
        self.assertTrue(entry["key"].endswith(":auto-v1-sim"))
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]["request"]["trigger"]["delivery_id"].startswith("simulation:"))
        self.assertEqual(jobs[0]["request"]["trigger"]["requested_by"], "operator-simulation")
        run_dir = R.RUNS_DIR / f"acme-pr-7-{SHA}-sim"
        self.assertTrue(json.loads((run_dir / "preview.json").read_text())["simulation"])
        self.assertTrue(json.loads((run_dir / "metadata.json").read_text())["simulation"])
        consumed = [a for a in self.app.store.grant_audit_log() if a["action"] == "consumed"]
        self.assertEqual(len(consumed), 1)
        st, cfg = host.worker("GET", "/config")
        self.assertEqual((st, cfg["workspaces"]["checkout_remote"], cfg["workspaces"]["github_token_configured"]), (200, MIRROR, False))

    def test_discord_and_notification_are_labelled_simulation(self):
        host = self.host()
        with mock.patch.object(R, "score_prompt", return_value=REJECT):     # finishes (blocked) → report, cleanup, Discord
            self.simulate(host)
        self.assertEqual(len(self.discord.posts), 1)
        first = self.discord.posts[0]["body"]
        self.assertIn("SIMULATION", first.get("content", ""))
        self.assertIn("SIMULATION", json.dumps(first["embeds"][0].get("title", "")))
        note = json.loads((self.auto_dir / "notifications.jsonl").read_text().splitlines()[-1])
        self.assertTrue(note["simulation"])
        self.assertIn("SIMULATION", (R.RUNS_DIR / f"acme-pr-7-{SHA}-sim" / "report.md").read_text())

    def test_result_messages_carry_the_simulation_banner_only_when_simulated(self):
        result = json.loads((ROOT / "fixtures" / "synthetic" / "opencode-result.synthetic.json").read_text())
        pv = {"repository": R.REPO, "pr_number": 7, "head_sha": SHA, "prompt_sha256": "0" * 64}
        entry = {"run_dir": "runs/x", "cleanup": "ok"}
        sim = A.DD.build_messages(result=result, pv=pv, entry=entry, prompt={}, simulation=True)
        real = A.DD.build_messages(result=result, pv=pv, entry=entry, prompt={})
        self.assertIn("SIMULATION", sim[0]["content"])
        self.assertNotIn("SIMULATION", json.dumps(real))

    def test_simulation_does_not_consume_the_real_runs_key(self):
        host = self.host()
        self.simulate(host)
        keys = list(self.state()["keys"])
        self.assertEqual(keys, [A.run_key(7, SHA, True)])
        self.assertNotIn(A.run_key(7, SHA), keys)

    def test_second_simulation_of_the_same_head_is_skipped(self):
        host = self.host()
        self.simulate(host)
        res = self.simulate(host)
        self.assertIn("already processed", res["processed"]["reason"])
        self.assertEqual(len(self.jobs()), 1)

    def test_rerun_archives_the_finished_simulation_and_never_touches_a_real_key(self):
        host = self.host()
        with mock.patch.object(R, "score_prompt", return_value=REJECT):
            self.simulate(host)
        st = self.state()
        st["keys"]["real-key"] = {"pr": 7, "status": "executed", "run_dir": "x"}
        A.save_state(self.state_dir, st)
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(R, "score_prompt", return_value=REJECT):
            res = A.simulate(host, 7, state_dir=self.state_dir, auto_dir=self.auto_dir, wait_s=0, out=print, rerun=True)
        self.assertEqual(res["archived"], [A.run_key(7, SHA, True)])
        self.assertIn("real-key", self.state()["keys"])
        self.assertEqual(len(list(R.RUNS_DIR.glob(f"acme-pr-7-{SHA}-sim-*"))), 1)
        self.assertEqual(len(self.discord.posts), 2)

    def test_a_stuck_in_flight_key_is_recovered_before_a_rerun(self):
        host = self.host()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        st = self.state()
        key = A.run_key(7, SHA, True)
        run_dir = R.RUNS_DIR / f"acme-pr-7-{SHA}-sim"
        run_dir.mkdir(parents=True, exist_ok=True)
        st["keys"][key] = {"key": key, "pr": 7, "sha": SHA, "status": "submitted", "simulation": True, "claim_id": "dead0001",
                           "run_dir": R.rel(run_dir)}
        A.save_state(self.state_dir, st)
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(R, "score_prompt", return_value=REJECT):
            res = A.simulate(host, 7, state_dir=self.state_dir, auto_dir=self.auto_dir, wait_s=0, out=print, rerun=True)
        self.assertEqual(res["recovered"], [key], "the runner that died left it in flight; it is finished first")
        self.assertEqual(res["archived"], [key], "then --rerun archives it instead of skipping as 'already processed'")
        self.assertNotIn("skipped", res["processed"])

    def test_simulation_requires_the_worker_mirror(self):
        self.app.executor.stop()                              # rebuilt: default GitHub remote, no token
        t = Path(self.tmp.name)
        (t / "data2").mkdir()
        env = live_env(str(t / "data2"), AUTOMATION_MODE="review_only", PRQA_REGISTRY_PATH=str(t / "registry.yaml"), PRQA_RUN_GRANT_KEY=KEY,
                       PRQA_SANDBOX_NETWORK=R.SANDBOX_NETWORK)
        env.pop("GITHUB_TOKEN", None)
        self.app = App(env)
        host = self.host()
        res = self.simulate(host)
        self.assertIn("prerequisites missing", res["result"])
        self.assertTrue(any("local mirror" in b for b in res["blockers"]))
        self.assert_nothing_started(host)

    def test_non_label_rules_still_apply(self):
        for kw in ({"draft": True}, {"fork": True}, {"state": "closed"}, {"base": "release"}):
            host = self.host({7: pr_json(7, labels=(), **kw)})
            res = self.simulate(host)
            self.assertIn("skipped", res["processed"], kw)
            self.assert_nothing_started(host)

    def test_real_execute_path_still_requires_the_label_and_token(self):
        # a normal (non-simulation) poll pass on the same host ignores the unlabeled PR entirely
        host = self.host()
        res = self.once(host)
        self.assertEqual(res["eligible"], [])
        self.assert_nothing_started(host)


class FailedJobReportTests(unittest.TestCase):
    def test_failed_job_without_result_reports_no_result_not_the_runner_state(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "preview.json").write_text(json.dumps({"pr_number": 7, "head_sha": SHA}))
            (d / "execution.json").write_text(json.dumps({"job_id": "j1", "status": "failed", "opencode_executed": False}))
            (d / "job.json").write_text(json.dumps({"status": "failed", "error": {"message": "launcher result failed validation"}}))
            entry = {"key": "k", "status": "submitting", "simulation": True, "reason": "job finished with status failed: launcher result failed validation"}
            with redirect_stdout(io.StringIO()):
                info = A.write_reports(d, entry=entry, out=print)
            self.assertEqual(info, {"executed": False, "verdict": "NO_RESULT (job failed)"})
            report = (d / "report.md").read_text()
            self.assertIn("SIMULATION", report)
            self.assertIn("launcher result failed validation", report)
            self.assertIn("Agent executed: **no**", report)


class SandboxFallbackResultTests(unittest.TestCase):
    """worker/sandbox/entrypoint.sh: when OpenCode leaves no qa-result.json the fallback must validate (real PR
    number) and name a model-side refusal, so the job ends INCONCLUSIVE with a cause instead of `pr/number: 0`."""

    def fallback(self, logs: str, job_json: str):
        text = (ROOT / "worker" / "sandbox" / "entrypoint.sh").read_text()
        code = text.split("<<'PY'\n", 2)[-1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "opencode-stderr.log").write_text(logs)
            env = {"PATH": "/usr/bin:/bin", "PRQA_JOB_JSON": job_json}
            p = subprocess.run([sys.executable, "-I", "-", "20261027-abcdef", R.REPO, SHA, d], input=code, capture_output=True, text=True, env=env, check=True)
        return json.loads(p.stdout)

    def test_budget_refusal_is_named_and_the_result_validates(self):
        from prqa_worker.validation import SchemaStore, validate_result
        res = self.fallback("deny: provider budget: request reservation 17135 exceeds the tokens-per-minute limit 8000",
                            json.dumps({"job_id": "j1", "repository": R.REPO, "pr": {"number": 7, "title": "t", "head_sha": SHA}}))
        self.assertEqual(res["pr"]["number"], 7)
        self.assertIn("tokens-per-minute", " ".join(res["blockers"]))
        self.assertEqual(res["test_cases"], [])
        self.assertTrue(validate_result(res, SchemaStore(ROOT / "schemas"))["valid"])

    def test_no_job_json_keeps_the_invalid_marker_and_no_log_text_is_copied(self):
        res = self.fallback("secret-ish line sk-abc 429", "")
        self.assertEqual(res["pr"]["number"], 0)
        self.assertNotIn("sk-abc", json.dumps(res))


class MirrorCheckoutTests(unittest.TestCase):
    def test_remote_base_is_restricted_to_the_worker_mirror_directory(self):
        self.assertEqual(validate_local_mirror_base(""), "")
        self.assertEqual(validate_local_mirror_base("file:///data/mirrors/"), MIRROR)
        for bad in ("https://github.com", "file:///etc", "file:///data/mirrors/../x", "file:///data/mirrorsX", "ssh://h/x",
                    "file:///data/mirrors/a b", "http://127.0.0.1"):
            with self.assertRaises(ValueError, msg=bad):
                validate_local_mirror_base(bad)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_pinned_checkout_from_a_bare_mirror_by_sha(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "src"
            subprocess.run(["git", "init", "-q", str(src)], check=True)
            (src / "a.txt").write_text("one\n")
            g = ["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t"]
            subprocess.run(g + ["add", "."], check=True)
            subprocess.run(g + ["commit", "-qm", "one"], check=True)
            sha = subprocess.run(g + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            (src / "a.txt").write_text("two\n")
            subprocess.run(g + ["commit", "-qam", "two"], check=True)
            mirror = d / "mirrors" / "org" / "repo.git"
            mirror.parent.mkdir(parents=True)
            subprocess.run(["git", "clone", "-q", "--bare", str(src), str(mirror)], check=True)
            plan = build_checkout_plan(repository="org/repo", head_sha=sha, workdir=d / "ws", remote_base=f"file://{d / 'mirrors'}",
                                       allowed_protocols=("file",))
            self.assertIn("protocol.file.allow=always", " ".join(plan.steps[1]))
            self.assertNotIn("protocol.https.allow=always", " ".join(plan.steps[1]))
            out = run_checkout(plan, token=None)
            self.assertEqual(out["head"], sha)
            self.assertEqual((d / "ws" / "a.txt").read_text(), "one\n")


if __name__ == "__main__":
    unittest.main()


class ProjectSelectionTests(unittest.TestCase):
    def tearDown(self):
        R.select_project("acme/private-api")

    def test_amenly_is_the_default_and_only_connected_project(self):
        self.assertEqual(R.DEFAULT_REPO, "Kiramido1/Amenly-Product")
        self.assertEqual(sorted(k for k in R.PROJECTS if k != "acme/private-api"), ["Kiramido1/Amenly-Product"])

    def test_amenly_is_public_with_its_own_profile_and_bases(self):
        R.select_project("Kiramido1/Amenly-Product")
        self.assertEqual((R.SLUG, R.ENV_PROFILE, R.PRIVATE_REPO, R.ALLOWED_BASES), ("amenly", "amenly", False, ("main",)))
        self.assertIn("anonymously from GitHub", A.simulation_note())

    def test_unknown_project_is_refused(self):
        with self.assertRaises(SystemExit):
            R.select_project("someone/else")

    def test_registry_lists_amenly_disabled_and_pending(self):
        from prqa_worker.policy import Policy
        from prqa_worker.registry import Registry
        e = Registry.load(ROOT / "config" / "repositories.yaml", Policy.from_env({})).get("Kiramido1/Amenly-Product")
        self.assertEqual((e.kind, e.enabled, e.approval_status, e.api["environment"]), ("real", False, "pending_approval", "staging"))
        self.assertIsNone(Registry.load(ROOT / "config" / "repositories.yaml", Policy.from_env({})).get("zedny-labs/Fluxera"))


class PublicSimulationTests(SimBase):
    def setUp(self):
        super().setUp()
        self.app.executor.stop()
        t = Path(self.tmp.name)
        env = live_env(str(t / "data"), AUTOMATION_MODE="review_only", REAL_PROJECT_TESTING_ENABLED="false", AUTO_EXECUTION_ENABLED="false",
                       PRQA_WORKER_CHECKOUT_ENABLED="false", PRQA_REGISTRY_PATH=str(t / "registry.yaml"), PRQA_RUN_GRANT_KEY=KEY,
                       PRQA_SANDBOX_NETWORK=R.SANDBOX_NETWORK)
        env.pop("GITHUB_TOKEN", None)
        self.app = App(env)
        self.app.clock = lambda: 1_900_000_000
        self.addCleanup(setattr, R, "PRIVATE_REPO", R.PRIVATE_REPO)

    def test_public_repo_needs_no_mirror_and_no_token(self):
        R.PRIVATE_REPO = False
        self.assertFalse([b for b in A.simulation_blockers(self.host(), self.auto_dir) if "mirror" in b or "GITHUB" in b or "GitHub" in b])

    def test_private_repo_without_mirror_is_blocked(self):
        R.PRIVATE_REPO = True
        self.assertTrue(any("local mirror" in b for b in A.simulation_blockers(self.host(), self.auto_dir)))


class EnvScriptErrorTests(unittest.TestCase):
    """Host._dx turns the disposable-env script's exit into ONE readable reason (not a JSON fragment)."""

    def run_dx(self, rc, stdout, stderr=""):
        done = subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)
        with mock.patch.object(R.subprocess, "run", return_value=done):
            return R.Host()._dx("up")

    def test_unhealthy_api_with_trailing_output(self):
        out = json.dumps({"healthy": False, "probe": ["health 000", "openapi 000"]}, indent=1) + "\n" + json.dumps({"teardown": 1})
        rc, data = self.run_dx(1, out)
        self.assertEqual(rc, 1); self.assertFalse(data["healthy"])
        self.assertEqual(data["error"], "API not healthy from the sandbox network (probe: health 000, openapi 000; see env/api.log)")

    def test_system_exit_message_is_used(self):
        rc, data = self.run_dx(1, "", "Traceback...\nalembic upgrade failed (see migrate.log)\n")
        self.assertEqual(data["error"], "alembic upgrade failed (see migrate.log)")

    def test_success_keeps_the_document(self):
        self.assertEqual(self.run_dx(0, '{"healthy": true}')[1], {"healthy": True})
