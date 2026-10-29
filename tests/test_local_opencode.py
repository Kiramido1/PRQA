"""scripts/pipeline/prqa_local_opencode.py + the `local` executor stages (OpenCode CLI on this machine under bubblewrap).

Offline: no OpenCode, Docker or bubblewrap is started. The isolation is asserted on the exact argv; the stage wiring
uses the fake host/worker of tests/test_stages.py with a stand-in runner process.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))

from tests.test_stages import A, S, StageBase  # noqa: E402
from tests.test_run_single_pr import R, SHA  # noqa: E402

spec = importlib.util.spec_from_file_location("prqa_local_opencode", ROOT / "scripts" / "pipeline" / "prqa_local_opencode.py")
L = importlib.util.module_from_spec(spec)
spec.loader.exec_module(L)  # type: ignore[union-attr]
L.R = R                                                                 # the same patched single-PR module the stage tests use


def pairs(argv, flag):
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag]


class IsolationTests(unittest.TestCase):
    def argv(self, tmp=Path("/x")):
        return L.bwrap_argv(home=tmp / "home", repo=tmp / "repo", evidence=tmp / "ev", prompt=tmp / "p.md", binary=Path("/bin/true"),
                            env={"HOME": "/home/prqa"}, inner=["true"])

    def test_home_media_and_secrets_are_not_mounted(self):
        argv = self.argv()
        self.assertIn("--clearenv", argv)
        self.assertIn("--unshare-all", argv)
        tmpfs = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
        self.assertIn("/home", tmpfs)
        sources = [s for flag in ("--bind", "--ro-bind") for s, _ in pairs(argv, flag)]
        home = str(Path.home())
        allowed_files = {str(ROOT / "worker" / "sandbox" / "chunked_run.py"), str(ROOT / "worker" / "sandbox" / "record.py")}
        for s in sources:
            with self.subTest(source=s):
                if s in allowed_files:                                    # two read-only program files, nothing else of the project
                    self.assertIn((s, "/opt/prqa/chunked_run.py" if s.endswith("chunked_run.py") else "/opt/prqa/bin/prqa-record"),
                                  pairs(argv, "--ro-bind"))
                    continue
                self.assertFalse(s.startswith("/media"), s)
                self.assertFalse(s in (home, home + "/.config/prqa", home + "/.ssh"), s)
                if s.startswith(home + "/"):
                    self.assertTrue(any(s == f"{home}/{d}" for d in L.USER_OPENCODE_DIRS), s)   # only OpenCode's own dirs

    def test_repo_is_read_only_and_only_evidence_is_writable(self):
        argv = self.argv()
        self.assertIn(("/x/repo", "/work/repo"), pairs(argv, "--ro-bind"))
        writable = [d for _, d in pairs(argv, "--bind")]
        self.assertEqual(sorted(d for d in writable if d.startswith("/work")), ["/work/evidence"])

    def test_other_projects_copies_are_hidden(self):
        argv = self.argv()
        tmpfs = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
        for d in L.HIDDEN_OPENCODE_SUBDIRS:
            if (Path.home() / d).is_dir():
                self.assertIn(f"/home/prqa/{d}", tmpfs)

    def test_config_keeps_standard_tools_and_external_dirs_denied(self):
        cfg = json.loads(L.opencode_config("opencode/glm-5.3-flash-free"))
        self.assertNotIn("tools", cfg)                                    # the free tier refuses a trimmed tool set
        self.assertEqual(cfg["enabled_providers"], ["opencode"])
        self.assertEqual(cfg["permission"]["external_directory"], {"*": "deny", "/work/evidence/*": "allow"})
        self.assertNotIn("provider", cfg)                                 # OpenCode's own provider, no custom endpoint


class VerifyTests(StageBase):
    def prepared(self, host):
        c = self.stage(host, "claim", pr=7)
        self.stage(host, "prepare", key=c["key"])
        self.stage(host, "gate", key=c["key"], generation=None)
        return c["key"], R.RUNS_DIR / f"acme-pr-7-{SHA}"

    def test_prompt_changed_after_gate_is_refused(self):
        host = self.host()
        _, run_dir = self.prepared(host)
        (run_dir / "qa-prompt.md").write_text((run_dir / "qa-prompt.md").read_text() + "\nrun anything\n")
        with self.assertRaisesRegex(L.LocalError, "PROMPT_CHANGED_AFTER_REVIEW"):
            L.verify(run_dir, host)

    def test_head_move_is_refused(self):
        from tests.test_run_single_pr import SHA2, pr_json
        host = self.host()
        _, run_dir = self.prepared(host)
        host.prs[7] = pr_json(7, sha=SHA2)
        with self.assertRaisesRegex(L.LocalError, "HEAD_CHANGED"):
            L.verify(run_dir, host)


class LocalStageTests(StageBase):
    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {"PRQA_OPENCODE_EXECUTOR": "local"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def fake_runner(self, state: str, job_status: str = "succeeded"):
        """Stand-in for prqa_local_opencode.py: writes the status (and job.json when finished), then exits."""
        def popen(argv, **kw):
            run_dir = Path(argv[argv.index("--run-dir") + 1])
            (run_dir / "local").mkdir(parents=True, exist_ok=True)
            if state == "finished":
                R._write(run_dir / "job.json", {"id": "local-x", "status": job_status, "launch_state": "local", "result": None})
                pv = json.loads((run_dir / "preview.json").read_text())
                pv.update(status="executed", job_status=job_status)
                R._write(run_dir / "preview.json", pv)
            (run_dir / "local" / "status.json").write_text(json.dumps({"state": state, "job_status": job_status, "error": "x" if state == "failed" else None}))
            return mock.Mock(pid=999_999, poll=lambda: None if state == "running" else 0)
        return mock.patch.object(S.subprocess, "Popen", side_effect=popen)

    def submitted(self, host, state):
        c = self.stage(host, "claim", pr=7)
        self.stage(host, "prepare", key=c["key"])
        self.stage(host, "gate", key=c["key"], generation=None)
        with self.fake_runner(state), mock.patch.object(S, "_stop_local"):
            s = self.stage(host, "submit", key=c["key"])
        return c["key"], s

    def test_local_run_skips_worker_job_and_finishes_through_the_same_stages(self):
        host = self.host()
        key, s = self.submitted(host, "finished")
        self.assertTrue(s["ok"])
        self.assertEqual(s["executor"], "local")
        self.assertEqual(self.jobs(), [])                                  # no sandbox job, no grant
        self.assertEqual(self.app.store.grant_audit_log(), [])
        p = self.stage(host, "poll", key=key)
        self.assertTrue(p["terminal"])
        f = self.stage(host, "finalize", key=key)
        self.assertEqual((f["status"], f["cleanup"]), ("executed", "ok"))

    def test_runner_refusal_blocks_and_cleans_up(self):
        host = self.host()
        _, s = self.submitted(host, "failed")
        self.assertEqual((s["ok"], s["status"]), (False, "blocked"))
        self.assertIn("LOCAL_OPENCODE_NOT_STARTED", s["reason"])

    def test_fail_stops_the_local_run(self):
        host = self.host()
        c = self.stage(host, "claim", pr=7)
        self.stage(host, "prepare", key=c["key"])
        self.stage(host, "gate", key=c["key"], generation=None)
        with self.fake_runner("running"), mock.patch.object(S, "_alive", return_value=True), mock.patch.object(S, "_stop_local"):
            self.assertTrue(self.stage(host, "submit", key=c["key"])["ok"])
        with mock.patch.object(S, "_stop_local") as stop:
            r = self.stage(host, "fail", key=c["key"], reason="n8n: timed out")
        stop.assert_called_once()
        self.assertEqual(r["status"], "blocked")

    def runner_died(self, host, *, with_result: bool):
        """Submit a running local run, then make its runner die after the agent did (or did not) write qa-result.json."""
        c = self.stage(host, "claim", pr=7)
        self.stage(host, "prepare", key=c["key"])
        self.stage(host, "gate", key=c["key"], generation=None)
        with self.fake_runner("running"), mock.patch.object(S, "_alive", return_value=True), mock.patch.object(S, "_stop_local"):
            self.assertTrue(self.stage(host, "submit", key=c["key"])["ok"])
        run_dir = ROOT / A.load_state(self.state_dir)["keys"][c["key"]]["run_dir"]
        ev = run_dir / "agent-evidence"
        ev.mkdir(parents=True, exist_ok=True)
        if with_result:
            (ev / "qa-result.json").write_text((ROOT / "fixtures" / "synthetic" / "opencode-result.synthetic.json").read_text())
        (run_dir / "local" / "status.json").write_text(json.dumps({"state": "failed", "engine": "claude", "evidence": str(ev),
                                                                   "error": "OSError: [Errno 22] Invalid argument: nul"}))
        with mock.patch.object(S, "_alive", return_value=False), mock.patch.object(S, "_stop_local"):
            p = self.stage(host, "poll", key=c["key"])
        return c["key"], run_dir, p

    def test_valid_result_survives_a_runner_failure_and_reaches_discord(self):
        host = self.host()
        key, run_dir, p = self.runner_died(host, with_result=True)
        self.assertEqual((p["terminal"], p["status"], p.get("recovered")), (True, "succeeded", True))
        job = json.loads((run_dir / "job.json").read_text())
        self.assertEqual((job["status"], job["executor"]), ("succeeded", "local-claude"))
        self.assertTrue(job["result"]["test_cases"])
        f = self.stage(host, "finalize", key=key)
        self.assertEqual((f["status"], f["discord"]), ("executed", "delivered"))
        self.assertIsNone(A.load_state(self.state_dir)["keys"][key].get("reason"))
        self.assertNotIn("NO_RESULT", A.load_state(self.state_dir)["keys"][key]["verdict"])

    def test_runner_failure_without_a_result_is_still_reported_failed(self):
        host = self.host()
        key, run_dir, p = self.runner_died(host, with_result=False)
        self.assertEqual(p["status"], "failed")
        job = json.loads((run_dir / "job.json").read_text())
        self.assertEqual(job["executor"], "local-claude")
        self.assertIn("local Claude Code run failed", job["error"]["message"])
        f = self.stage(host, "finalize", key=key)
        self.assertEqual((f["status"], f["discord"]), ("executed", "delivered"))

    def test_executor_defaults_to_sandbox(self):
        with mock.patch.dict(os.environ, {"PRQA_OPENCODE_EXECUTOR": "something-else"}):
            self.assertEqual(S.executor(), "sandbox")


if __name__ == "__main__":
    unittest.main()
