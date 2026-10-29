"""The `claude` engine of the local executor: Claude Code CLI (`claude -p`, Bash only) instead of OpenCode.

Offline: a fake `claude` executable stands in for the CLI; no network, Docker or bubblewrap is used.

Run: python3 -m unittest tests.test_claude_engine -v
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))

from prqa_worker.validation import SchemaStore, validate_result  # noqa: E402
from tests.test_chunked_run import C, JOB, PROMPT  # noqa: E402
from tests.test_local_opencode import L  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts" / "pipeline"))
import prqa_local_watch as W  # noqa: E402

# A fake `claude`: records its argv, then writes one PASS record per scenario named in the prompt after -p.
FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, re, sys
argv = sys.argv[1:]
text = argv[argv.index("-p") + 1]
open(os.environ["PRQA_EVIDENCE_DIR"] + "/calls.log", "a").write(json.dumps(argv[:1] + argv[2:]) + "\\n")
ids = re.search(r"Execute ONLY these scenarios: ([^.]+)\\.", text).group(1).split(", ")
for i in ids:
    json.dump({"status": "PASS", "scenario": i, "endpoint": "GET /x", "expected": "e", "actual": "a"},
              open(os.environ["PRQA_EVIDENCE_DIR"] + "/tc/" + i + ".json", "w"))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done"}))
"""
# A fake `claude` that is refused by the provider and writes nothing.
REFUSED_CLAUDE = """#!/usr/bin/env python3
import json, os
open(os.environ["PRQA_EVIDENCE_DIR"] + "/calls.log", "a").write("call\\n")
print(json.dumps({"type": "result", "subtype": "error", "is_error": True,
                  "result": "API Error: 401 {\\"type\\":\\"error\\",\\"error\\":{\\"type\\":\\"authentication_error\\"}}"}))
"""


def run_loop(fake_source: str) -> tuple[Path, int, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    d = Path(tmp.name)
    bin_ = d / "bin"
    bin_.mkdir()
    fake = bin_ / "claude"
    fake.write_text(fake_source)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (d / "prompt.md").write_text(PROMPT)
    ev = d / "ev"
    ev.mkdir()
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "PRQA_EVIDENCE_DIR": str(ev), "PRQA_JOB_JSON": json.dumps(JOB), "BASE_URL": ""}
    with mock.patch.dict(os.environ, env, clear=True):
        rc = C.main(["--prompt", str(d / "prompt.md"), "--repo-dir", str(d), "--evidence", str(ev), "--model", "claude-sonnet-5-5",
                     "--timeout-s", "3600", "--prompt-sha256", "b" * 64, "--engine", "claude"])
    return ev, rc, tmp


class ClaudeSessionLoopTests(unittest.TestCase):
    def test_sessions_run_claude_with_bash_only_and_produce_a_valid_result(self):
        ev, rc, tmp = run_loop(FAKE_CLAUDE)
        with tmp:
            self.assertEqual(rc, 0)
            calls = [json.loads(line) for line in (ev / "calls.log").read_text().splitlines()]
            self.assertEqual(len(calls), len(C.split_prompt(PROMPT)["groups"]))
            for argv in calls:
                self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")
                self.assertEqual(argv[argv.index("--tools") + 1], "Bash")
                self.assertEqual(argv[argv.index("--allowedTools") + 1], "Bash")
                for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
                    self.assertIn(flag, argv)
                self.assertNotIn("--dangerously-skip-permissions", argv)
            self.assertTrue(list(ev.glob("claude-events-g*.jsonl")))
            res = json.loads((ev / "qa-result.json").read_text())
            check = validate_result(res, SchemaStore(ROOT / "schemas"))
            self.assertTrue(check["valid"], check["errors"])
            self.assertEqual(res["executed_by"]["engine"], "claude_code")
            self.assertEqual(res["executed_by"]["model"], "claude-sonnet-5-5")
            self.assertIn("executed by Claude Code", res["summary"]["executive_summary"])
            self.assertEqual(res["summary"]["passed"], len(res["test_cases"]))

    def test_a_refusal_stops_the_remaining_sessions(self):
        ev, rc, tmp = run_loop(REFUSED_CLAUDE)
        with tmp:
            self.assertEqual(rc, 0)
            self.assertEqual(len((ev / "calls.log").read_text().splitlines()), 1, "later sessions must not start")
            res = json.loads((ev / "qa-result.json").read_text())
            self.assertEqual(res["summary"]["overall_status"], "INCONCLUSIVE")
            blockers = " ".join(res["blockers"])
            self.assertIn("agent not authenticated", blockers)
            self.assertIn("not started (agent not authenticated", blockers)

    def test_quota_messages_are_recognised(self):
        for log in ("Free usage limit reached", "Claude AI usage limit reached", "Credit balance is too low"):
            self.assertTrue(C.cause(log).startswith("provider quota exhausted"), log)
        self.assertEqual(C.cause("all good"), "")


class ClaudeRunnerTests(unittest.TestCase):
    def test_engine_and_model_come_from_compose_env(self):
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(L.R, "env_file_values", return_value={"PRQA_LOCAL_ENGINE": "claude"}):
            os.environ.pop("PRQA_LOCAL_ENGINE", None)
            os.environ.pop("PRQA_LOCAL_MODEL", None)
            self.assertEqual(L.resolve_engine(None, None), ("claude", "claude-sonnet-5-5"))
        with mock.patch.object(L.R, "env_file_values", return_value={}):
            self.assertEqual(L.resolve_engine("opencode", None)[0], "opencode")
            self.assertEqual(L.resolve_engine("claude", "claude-opus-5-5"), ("claude", "claude-opus-5-5"))
            with self.assertRaises(L.LocalError):
                L.resolve_engine("gpt", None)

    def test_only_the_login_file_and_binary_enter_the_sandbox(self):
        binds = L.engine_binds("claude", Path("/opt/x/claude"))
        self.assertEqual(binds, ["--bind", str(L.CLAUDE_CREDENTIALS), "/home/prqa/.claude/.credentials.json",
                                 "--ro-bind", "/opt/x/claude", "/opt/claude/bin/claude"])
        argv = L.bwrap_argv(home=Path("/h"), repo=Path("/r"), evidence=Path("/e"), prompt=Path("/p.md"), binary=Path("/opt/x/claude"),
                            env={"HOME": "/home/prqa"}, inner=["true"], engine="claude")
        sources = [argv[i + 1] for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")]
        self.assertNotIn(str(Path.home() / ".claude"), sources, "the operator's whole ~/.claude must not be mounted")
        self.assertFalse(any(".opencode" in s or "/opencode" in s for s in sources))
        self.assertIn("--clearenv", argv)

    def test_private_home_keeps_only_account_fields(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            fake_home, home = d / "op", d / "home"
            (fake_home / ".claude").mkdir(parents=True)
            home.mkdir()
            creds = fake_home / ".claude" / ".credentials.json"
            creds.write_text("{}")
            (fake_home / ".claude.json").write_text(json.dumps({"userID": "u", "oauthAccount": {"a": 1}, "projects": {"/secret": {}},
                                                               "mcpServers": {"x": {}}}))
            with mock.patch.object(L.Path, "home", return_value=fake_home), mock.patch.object(L, "CLAUDE_CREDENTIALS", creds):
                L.prepare_claude_home(home)
            cfg = json.loads((home / ".claude.json").read_text())
            self.assertEqual(set(cfg), {"userID", "oauthAccount"})
            self.assertTrue((home / ".claude" / ".credentials.json").is_file())
            self.assertEqual((home / ".claude" / ".credentials.json").read_text(), "", "placeholder only; the real file is bind-mounted")

    def test_missing_login_is_refused(self):
        with mock.patch.object(L, "CLAUDE_CREDENTIALS", Path("/nonexistent/.credentials.json")), tempfile.TemporaryDirectory() as d:
            with self.assertRaises(L.LocalError):
                L.prepare_claude_home(Path(d))


class ViewerTests(unittest.TestCase):
    def test_claude_stream_events_are_shown(self):
        lines = W.event_lines({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Testing calc"}, {"type": "tool_use", "input": {"command": "curl -s $BASE_URL/x"}}]}})
        self.assertEqual([t for t, _ in lines], ["  ▸ Testing calc", "  $ curl -s $BASE_URL/x"])
        out = W.event_lines({"type": "user", "message": {"content": [{"type": "tool_result", "content": [{"type": "text", "text": "200"}]}]}})
        self.assertEqual(out, [("    200", "dim")])
        self.assertEqual(W.event_lines({"type": "result", "is_error": True, "result": "boom"}), [("  ! boom", "r")])
        self.assertEqual(W.event_lines({"type": "system", "subtype": "init"}), [])


class EvidenceCopyTests(unittest.TestCase):
    def test_a_refused_filename_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d) / "src", Path(d) / "dst"
            (src / "tc").mkdir(parents=True)
            (src / "qa-result.json").write_text("{}")
            (src / "tc" / "TC-1.json").write_text("{}")
            (src / "nul").write_text("x")
            real = L.shutil.copy2

            def ntfs_copy(a, b, **kw):
                if Path(b).name == "nul":
                    raise OSError(22, "Invalid argument")
                return real(a, b, **kw)

            with mock.patch.object(L.shutil, "copy2", side_effect=ntfs_copy):
                skipped = L.copy_evidence(src, dst)
            self.assertEqual(skipped, ["nul"])
            self.assertTrue((dst / "qa-result.json").is_file())
            self.assertTrue((dst / "tc" / "TC-1.json").is_file())


if __name__ == "__main__":
    unittest.main()
