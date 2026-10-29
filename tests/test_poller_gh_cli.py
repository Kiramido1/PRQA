"""Local poller discovery through the authenticated `gh` CLI: exact label, base-branch allowlist, fork/draft/closed
skips, deterministic single selection, discovery-only (never submits), and no credential in argv/output. Offline:
the `gh` subprocess is replaced by a fake runner.

Run: cd "<project>" && python3 -m unittest tests.test_poller_gh_cli -v
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "zedny-labs/Fluxera"


def load_poller():
    spec = importlib.util.spec_from_file_location("prqa_label_poller_ghcli", ROOT / "scripts" / "pipeline" / "prqa_label_poller.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


P = load_poller()


def pr(n, *, base="main", draft=False, state="open", fork=False, sha=None):
    return {"number": n, "title": f"PR {n}", "draft": draft, "state": state,
            "head": {"sha": sha or (f"{n:02d}" * 20)[:40], "repo": {"full_name": "someone/fork" if fork else REPO}},
            "base": {"ref": base, "repo": {"full_name": REPO}}}


class FakeGh:
    def __init__(self, issues, prs, fail=None):
        self.issues, self.prs, self.fail, self.argv = issues, prs, fail, []

    def __call__(self, argv, capture_output, text, timeout):
        self.argv.append(argv)
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, "", self.fail)
        path = argv[-1]
        if "/issues?" in path:
            page = int(path.split("page=")[-1])
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.issues if page == 1 else []), "")
        n = int(path.rsplit("/", 1)[1])
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.prs[n]), "")


def issue(n, *labels):
    return {"number": n, "pull_request": {"url": "x"}, "labels": [{"name": l} for l in labels]}


class GhCliDiscoveryTests(unittest.TestCase):
    def run_poll(self, fake, **env):
        with tempfile.TemporaryDirectory() as tmp:
            e = {"PRQA_POLL_REPOSITORIES": REPO, "PRQA_POLL_GITHUB_BACKEND": "gh-cli", "PRQA_POLL_BASE_BRANCHES": "main,Dev"}
            e.update(env)
            lines: list[str] = []
            summary = P.run_once(e, dry_run=True, out=lines.append, state_path=Path(tmp) / "s.json", gh_reader=P.GhCliReader(runner=fake))
            self.assertFalse((Path(tmp) / "s.json").exists())                 # dry run writes no state
        return summary, lines

    def test_selects_exactly_one_eligible_pr_deterministically(self):
        fake = FakeGh([issue(9, "to be test"), issue(4, "to be test"), issue(6, "to be test")],
                      {9: pr(9), 4: pr(4, base="Dev"), 6: pr(6)})
        summary, _ = self.run_poll(fake)
        self.assertEqual([c["number"] for c in summary["candidates"]], [4, 6, 9])
        sel = summary["selected"]
        self.assertEqual((sel["number"], sel["base"], sel["url"]), (4, "Dev", f"https://github.com/{REPO}/pull/4"))
        self.assertRegex(sel["head_sha"], r"^[0-9a-f]{40}$")
        self.assertEqual(summary["submitted"], 0)

    def test_ineligible_prs_are_skipped_with_reasons(self):
        fake = FakeGh([issue(1, "to be test"), issue(2, "to be test"), issue(3, "to be test"), issue(4, "to be test"),
                       issue(5, "To Be Test"), issue(6, "needs-review"), issue(7, "to be test", "bug")],
                      {1: pr(1, draft=True), 2: pr(2, fork=True), 3: pr(3, base="release"), 4: pr(4, state="closed"),
                       5: pr(5), 6: pr(6), 7: pr(7)})
        summary, _ = self.run_poll(fake)
        acts = summary["repositories"][REPO]["actions"]
        self.assertEqual(acts, {"1": "skipped_draft", "2": "skipped_fork", "3": "skipped_base_branch", "4": "skipped_not_open", "7": "dry_run"})
        self.assertEqual(summary["selected"]["number"], 7)              # #5 (wrong case) and #6 (other label) never considered

    def test_no_eligible_pr_selects_nothing(self):
        summary, _ = self.run_poll(FakeGh([issue(7, "needs-review")], {7: pr(7)}))
        self.assertEqual((summary["candidates"], summary["selected"]), ([], None))

    def test_gh_cli_is_discovery_only_and_never_submits(self):
        with self.assertRaises(SystemExit):
            P.run_once({"PRQA_POLL_REPOSITORIES": REPO, "PRQA_POLL_GITHUB_BACKEND": "gh-cli", "PRQA_WEBHOOK_SECRET": "x" * 40},
                       dry_run=False, gh_reader=P.GhCliReader(runner=FakeGh([], {})))

    def test_argv_carries_no_credential_and_only_get_paths(self):
        fake = FakeGh([issue(4, "to be test")], {4: pr(4)})
        self.run_poll(fake)
        for argv in fake.argv:
            self.assertEqual(argv[:4], ["gh", "api", "--method", "GET"])
            self.assertTrue(argv[-1].startswith("/repos/zedny-labs/Fluxera/"))
            self.assertFalse(any("token" in a.lower() or "authorization" in a.lower() for a in argv))
        with self.assertRaises(P.GitHubError):
            P.GhCliReader(runner=fake).get("/user")
        with self.assertRaises(P.GitHubError):
            P.GhCliReader(runner=fake).get("/repos/x/y --method DELETE")

    def test_gh_errors_are_reported_without_secrets(self):
        summary, lines = self.run_poll(FakeGh([], {}, fail="HTTP 401: Bad credentials ghp_abcdefghijklmnopqrstuvwxyz0123"))
        self.assertEqual(summary["errors"], 1)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz0123", " ".join(lines) + json.dumps(summary))


class GhRetryTests(unittest.TestCase):
    """A slow/unreachable GitHub is retried a few times, then reported clearly; auth errors are not retried."""

    def script(self, outcomes):
        calls = []
        def runner(argv, capture_output, text, timeout):
            calls.append(argv)
            o = outcomes[min(len(calls), len(outcomes)) - 1]
            if o == "timeout":
                raise subprocess.TimeoutExpired(argv, timeout)
            rc, out, err = o
            return subprocess.CompletedProcess(argv, rc, out, err)
        return runner, calls

    def test_timeout_then_success_is_retried(self):
        runner, calls = self.script(["timeout", (0, "[]", "")])
        waits = []
        data, _ = P.GhCliReader(runner=runner, sleep=waits.append).get("/repos/x/y/pulls")
        self.assertEqual(data, [])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(waits), 1)

    def test_persistent_timeout_gives_clear_error(self):
        runner, calls = self.script(["timeout"])
        with self.assertRaises(P.GitHubError) as ctx:
            P.GhCliReader(runner=runner, sleep=lambda s: None, max_attempts=3).get("/repos/x/y/pulls?page=1")
        self.assertEqual(len(calls), 3)
        self.assertIn("did not answer", str(ctx.exception))
        self.assertNotIn("page=1", str(ctx.exception))

    def test_network_error_retried_but_auth_error_not(self):
        runner, calls = self.script([(1, "", "error connecting to api.github.com"), (0, "{}", "")])
        P.GhCliReader(runner=runner, sleep=lambda s: None).get("/repos/x/y")
        self.assertEqual(len(calls), 2)
        runner, calls = self.script([(1, "", "HTTP 401: Bad credentials")])
        with self.assertRaises(P.GitHubError):
            P.GhCliReader(runner=runner, sleep=lambda s: None).get("/repos/x/y")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
