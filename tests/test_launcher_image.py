"""The BUILT launcher image (prqa/launcher:0.1.0) against the real policy proxy (opt-in: PRQA_LAUNCHER_IMAGE=1).

tests/test_launcher_integration.py runs the launcher code from the host interpreter; this file runs the image that
`docker compose --profile phase2 build prqa-launcher` produces, hardened as compose runs it (uid 10002, read-only root,
no capabilities, no-new-privileges) and with no network at all: its only channel is the proxy socket. Checks:

  * the image carries the docker CLI and no daemon, no credential-shaped environment, nothing but /app code;
  * a permitted, read-only command (`docker image inspect` of the pinned sandbox image, `prqa_launcher.py --check`)
    succeeds through the proxy, and the CLI reports failures in its defined form (exit 1 / exit 2);
  * prohibited requests — reading another container's configuration or environment (n8n holds the provider key),
    exec, privileged or host-bind containers (the host secrets directory), unpinned images, pulls, builds — are
    refused by the proxy, with a deny decision logged; list requests only ever return prqa-labelled resources;
  * the launcher process cannot see the host secrets file or any provider credential.

Every probe is read-only or uses --rm; nothing destructive is attempted, even as a probe. The daemon's containers,
volumes, networks and images are compared before and after. No model provider, GitHub, Discord or OpenCode run.

    PRQA_LAUNCHER_IMAGE=1 PYTHONPATH=worker python3 -m unittest tests.test_launcher_image -v
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
REAL_SOCK = "/var/run/docker.sock"
IMAGE = "prqa/launcher:0.1.0"
SANDBOX_TAG = "prqa/opencode-sandbox:local"
SANDBOX_ID = "sha256:d0efff568953e15bf0d57758dfd30d435803fc05b39c98ec2cc46f0199d967e5"   # as test_launcher_integration
SECRETS_DIR = Path.home() / ".config" / "prqa"
CREDENTIAL_NAMES = ("GROQ_API_KEY", "PROMPT_LLM_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN", "DISCORD_WEBHOOK_URL")
ENABLED = os.environ.get("PRQA_LAUNCHER_IMAGE") == "1" and shutil.which("docker") is not None


def real(argv, *, timeout=120):
    p = subprocess.run(["docker", "-H", "unix://" + REAL_SOCK, *argv], capture_output=True, text=True, timeout=timeout, check=False)
    return p.returncode, p.stdout, p.stderr


def snapshot():
    out = {}
    for kind, argv in (("containers", ["ps", "-aq"]), ("volumes", ["volume", "ls", "-q"]),
                       ("networks", ["network", "ls", "-q"]), ("images", ["image", "ls", "-aq"])):
        out[kind] = set(real(argv)[1].split())
    return out


@unittest.skipUnless(ENABLED, "set PRQA_LAUNCHER_IMAGE=1 with a local Docker daemon and the built launcher image")
class LauncherImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if real(["image", "inspect", IMAGE])[0] != 0:
            raise unittest.SkipTest(f"{IMAGE} is not built")
        if real(["image", "inspect", SANDBOX_TAG, "--format", "{{.Id}}"])[1].strip() != SANDBOX_ID:
            raise unittest.SkipTest("pinned sandbox image missing or different")
        cls.baseline = snapshot()
        cls.tmp = Path(tempfile.mkdtemp(prefix="prqa-limg-"))       # short: unix socket paths are length-limited
        os.chmod(cls.tmp, 0o711)                                     # uid 10002 must traverse to the socket only
        cls.sock = str(cls.tmp / "docker-policy.sock")
        cls.log = cls.tmp / "proxy.log"
        cls.gid = os.getgid()
        # the real proxy, as compose runs it: socket group = the launcher's group, mode 0660
        cls.proxy = subprocess.Popen([sys.executable, "-I", str(ROOT / "proxy" / "docker_policy_proxy.py"), "--listen", cls.sock,
                                      "--upstream", REAL_SOCK, "--image-id", SANDBOX_ID, "--image-tag", SANDBOX_TAG,
                                      "--socket-gid", str(cls.gid), "--log-file", str(cls.log)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if os.path.exists(cls.sock):
                break
            time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proxy.terminate()
        try:
            cls.proxy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proxy.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tearDown(self):
        after = snapshot()
        for kind in ("containers", "volumes", "networks", "images"):
            self.assertEqual(after[kind], self.baseline[kind], f"daemon {kind} changed")

    # ----------------------------------------------------------------- helpers --
    def in_launcher(self, script, env=None, timeout=120):
        """Run `sh -c script` inside the launcher image, hardened as compose runs it; its only channel is the proxy."""
        argv = ["run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--tmpfs", "/tmp:size=16m", "--user", "10002:10002", "--group-add", str(self.gid), "--pids-limit", "128",
                "--memory", "512m", "-v", f"{self.tmp}:/run/prqa", "-e", "DOCKER_HOST=unix:///run/prqa/docker-policy.sock"]
        for k, v in (env or {}).items():
            argv += ["-e", f"{k}={v}"]
        return real(argv + ["--entrypoint", "sh", IMAGE, "-c", script], timeout=timeout)

    def rc_only(self, docker_args):
        """Exit status of a docker command inside the launcher; its output is discarded, never captured."""
        rc, out, _ = self.in_launcher(f"docker {docker_args} >/dev/null 2>&1; echo $?")
        return int(out.strip().splitlines()[-1])

    def decisions(self, since):
        text = self.log.read_text()[since:] if self.log.exists() else ""
        return [l.split(" decision ", 1)[1] for l in text.splitlines() if " decision " in l]

    def offset(self):
        return len(self.log.read_text()) if self.log.exists() else 0

    # ------------------------------------------------------------------- tests --
    def test_1_image_configuration(self):
        rc, out, _ = real(["image", "inspect", IMAGE, "--format", "{{json .Config}}"])
        cfg = json.loads(out)
        self.assertEqual(cfg["User"], "10002:10002")
        self.assertEqual(cfg["Cmd"], ["python3", "/app/prqa_launcher.py", "--interval", "15"])
        names = {e.split("=", 1)[0] for e in cfg.get("Env") or []}
        self.assertFalse(names & set(CREDENTIAL_NAMES), names)
        self.assertFalse([n for n in names if any(w in n for w in ("KEY", "TOKEN", "SECRET", "PASSWORD")) and n != "GPG_KEY"])
        rc, out, _ = self.in_launcher("docker --version >/dev/null && echo cli; for b in dockerd containerd runc; do command -v $b; done; "
                                      "ls /app; id -u")
        lines = out.split()
        self.assertEqual(lines[0], "cli")                       # the launcher's only tool
        self.assertEqual(sorted(lines[1:3]), ["prqa_launcher.py", "prqa_worker"])  # no daemon binaries, only /app code
        self.assertEqual(lines[-1], "10002")

    def test_2_permitted_read_only_commands_succeed_through_the_proxy(self):
        since = self.offset()
        rc, out, err = self.in_launcher(f"docker image inspect {SANDBOX_TAG} --format '{{{{.Id}}}}'")
        self.assertEqual((rc, out.strip()), (0, SANDBOX_ID), err[-300:])
        env = {"PRQA_LAUNCHER_TOKEN": "synthetic-launcher-token-0000000000000000", "PRQA_SANDBOX_IMAGE": SANDBOX_TAG,
               "PRQA_SANDBOX_IMAGE_ID": SANDBOX_ID, "PRQA_SANDBOX_NETWORK": "none", "PRQA_WORKER_URL": "http://127.0.0.1:9"}
        rc, out, err = self.in_launcher("python3 /app/prqa_launcher.py --check", env=env)
        self.assertEqual(rc, 0, err[-400:])
        report = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((report["image_id"], report["network"]), (SANDBOX_ID, "none"))
        self.assertTrue(self.decisions(since))
        self.assertTrue(all(d.startswith("allow ") for d in self.decisions(since)), self.decisions(since))

    def test_3_failures_come_back_in_the_defined_form(self):
        base = {"PRQA_SANDBOX_IMAGE": SANDBOX_TAG, "PRQA_SANDBOX_NETWORK": "none", "PRQA_WORKER_URL": "http://127.0.0.1:9"}
        rc, _, err = self.in_launcher("python3 /app/prqa_launcher.py --check",
                                      env={**base, "PRQA_LAUNCHER_TOKEN": "synthetic-launcher-token-1111111111111111", "PRQA_SANDBOX_IMAGE_ID": "sha256:" + "0" * 64})
        self.assertEqual(rc, 1)
        self.assertIn("check failed: sandbox image ID mismatch", err)
        rc, _, err = self.in_launcher("python3 /app/prqa_launcher.py --check", env={**base, "PRQA_SANDBOX_IMAGE_ID": SANDBOX_ID})
        self.assertEqual(rc, 2)
        self.assertIn("launcher configuration error", err)

    def test_4_prohibited_requests_are_refused_by_the_proxy(self):
        secrets_bind = f"-v {SECRETS_DIR}:/s:ro"
        probes = {
            # another container's configuration and environment: n8n holds GROQ_API_KEY
            "inspect n8n": "inspect prqa-n8n-1",
            "container inspect n8n": "container inspect prqa-n8n-1",
            "exec into n8n": "exec prqa-n8n-1 true",
            "logs of n8n": "logs prqa-n8n-1",
            # host access and the protected secrets directory
            "bind host secrets dir": f"run --rm {secrets_bind} {SANDBOX_TAG} true",
            "bind host root": f"run --rm -v /:/host {SANDBOX_TAG} true",
            "privileged": f"run --rm --privileged {SANDBOX_TAG} true",
            "host network": f"run --rm --network host {SANDBOX_TAG} true",
            "unpinned image": "run --rm python:3.13-slim true",
            "pull": "pull alpine:3.20",
            "build": "build -t prqa/x - </dev/null",
            "system info": "info",
            "image list": "image ls",
        }
        for name, args in probes.items():
            with self.subTest(probe=name):
                since = self.offset()
                self.assertNotEqual(self.rc_only(args), 0, f"{name} must be refused")
                denied = [d for d in self.decisions(since) if d.startswith("deny")]
                self.assertTrue(denied, f"{name}: no deny decision logged (rc != 0 must come from the policy)")

    def test_5_lists_only_ever_show_prqa_resources(self):
        rc, out, _ = self.in_launcher("docker ps -a --format '{{.Names}}'; echo ---; docker volume ls --format '{{.Name}}'")
        self.assertEqual(rc, 0)
        containers, volumes = (part.split() for part in out.split("---"))
        self.assertFalse([c for c in containers if not c.startswith(("prqa-sandbox-", "prqa-populate-"))], containers)
        self.assertNotIn("prqa-n8n-1", containers)
        self.assertFalse([v for v in volumes if not v.startswith("prqa-checkout-")], volumes)

    def test_6_no_credential_or_host_secret_inside_the_launcher(self):
        names = " ".join(CREDENTIAL_NAMES)
        rc, out, _ = self.in_launcher(
            f"for n in {names}; do printenv $n >/dev/null && echo LEAK:$n; done; "
            f"grep -c -a -E 'GROQ|OPENROUTER|PROMPT_LLM_API_KEY' /proc/1/environ || true; "
            f"test -e '{SECRETS_DIR}' && echo HOST_SECRETS_VISIBLE; test -e /run/secrets && echo RUN_SECRETS; "
            f"mount | grep -c ' /run/prqa ' ; touch /app/x 2>/dev/null && echo APP_WRITABLE; echo done")
        self.assertEqual(rc, 0)
        self.assertNotIn("LEAK:", out)
        self.assertNotIn("HOST_SECRETS_VISIBLE", out)
        self.assertNotIn("RUN_SECRETS", out)
        self.assertNotIn("APP_WRITABLE", out)
        self.assertEqual(out.split()[0], "0")                    # no provider variable in the process environment
        self.assertTrue(out.strip().endswith("done"))


if __name__ == "__main__":
    unittest.main()
