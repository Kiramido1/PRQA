"""scripts/pipeline/prqa_disposable_env.py teardown safety: `down` removes only resources recorded in the state file AND labelled
prqa.disposable=<run id>; anything relabelled / foreign is left untouched. Offline: docker is replaced by a fake.

Run: cd "<project>" && python3 -m unittest tests.test_disposable_env -v
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prqa_disposable_env", ROOT / "scripts" / "pipeline" / "prqa_disposable_env.py")
DX = importlib.util.module_from_spec(spec)
spec.loader.exec_module(DX)  # type: ignore[union-attr]


class FakeDocker:
    def __init__(self, labels):
        self.labels, self.calls = labels, []

    def __call__(self, *args, check=True, timeout=900, capture=True):
        self.calls.append(args)
        if "inspect" in args:
            name = args[-1]
            if name not in self.labels:
                return subprocess.CompletedProcess(args, 1, "", "No such object")
            return subprocess.CompletedProcess(args, 0, self.labels[name] + "\n", "")
        if args[0] == "ps":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")


class TeardownTests(unittest.TestCase):
    def test_down_removes_only_labelled_recorded_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir, private = Path(tmp) / "state", Path(tmp) / "private"
            state_dir.mkdir(); private.mkdir()
            (private / "app.env").write_text("SECRET_KEY=x\n")
            res = [{"kind": "image", "name": "prqa/disposable-api:r1"}, {"kind": "network", "name": "prqa-dx-r1-db"},
                   {"kind": "container", "name": "prqa-dx-r1-db"}, {"kind": "container", "name": "users-own-db"},
                   {"kind": "container", "name": "prqa-dx-r1-gone"}]
            (state_dir / "env-state.json").write_text(json.dumps({"run_id": "r1", "private_dir": str(private), "resources": res}))
            fake = FakeDocker({"prqa/disposable-api:r1": "r1", "prqa-dx-r1-db": "r1", "users-own-db": ""})
            orig, DX.docker = DX.docker, fake
            try:
                rc = DX.main(["down", "--state-dir", str(state_dir)])
            finally:
                DX.docker = orig
            self.assertEqual(rc, 0)
            removals = [a for a in fake.calls if a[0] in ("rm", "image", "network") and "inspect" not in a]
            removed_names = {a[-1] for a in removals}
            self.assertEqual(removed_names, {"prqa/disposable-api:r1", "prqa-dx-r1-db"})
            self.assertNotIn("users-own-db", removed_names)
            state = json.loads((state_dir / "env-state.json").read_text())
            skipped = {s["name"]: s["why"] for s in state["steps"][-1]["skipped"]}
            self.assertIn("label mismatch", skipped["users-own-db"])
            self.assertEqual(skipped["prqa-dx-r1-gone"], "already gone")
            self.assertFalse(private.exists(), "generated env files removed")

    def test_up_refuses_bad_inputs_before_touching_docker(self):
        fake = FakeDocker({})
        orig, DX.docker = DX.docker, fake
        try:
            with tempfile.TemporaryDirectory() as tmp:
                for argv in (["up", "--run-id", "r1", "--sha", "a" * 40, "--state-dir", tmp, "--git-dir", tmp],      # no private dir
                             ["up", "--run-id", "R;rm", "--sha", "a" * 40, "--state-dir", tmp, "--git-dir", tmp, "--private-dir", tmp],
                             ["up", "--run-id", "r1", "--sha", "main", "--state-dir", tmp, "--git-dir", tmp, "--private-dir", tmp]):
                    with self.assertRaises(SystemExit):
                        DX.main(argv)
        finally:
            DX.docker = orig
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
