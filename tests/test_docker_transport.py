"""REAL local Docker integration test for the sandbox artifact transport (opt-in: PRQA_DOCKER_INTEGRATION=1).

Builds a DISPOSABLE image from the locally cached python:3.13-slim (no pull, --network none) containing the repository's
real entrypoint, verifier and extractor, then executes the real plan: docker volume create → helper `docker run` with
the artifact on stdin → `docker create` (read-only rootfs, network none, uid 10001, volume mounted read-only) →
`docker start -a` → cleanup. No OpenCode, no network, no secrets, no PRQA worker/n8n interaction. Every container,
volume, image and temp file it creates is removed in tearDown, including on failure.

    PRQA_DOCKER_INTEGRATION=1 PYTHONPATH=worker python3 -m unittest tests.test_docker_transport -v
    # against an existing image (F and G skipped: they would reach the OpenCode invocation):
    PRQA_DOCKER_TEST_IMAGE=prqa/opencode-sandbox:local PRQA_DOCKER_INTEGRATION=1 PYTHONPATH=worker python3 -m unittest tests.test_docker_transport -v
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from prqa_worker import opencode_runner  # noqa: E402
from prqa_worker.workspace import pack_checkout_artifact  # noqa: E402

ENABLED = os.environ.get("PRQA_DOCKER_INTEGRATION") == "1" and shutil.which("docker") is not None
BASE_IMAGE = "python:3.13-slim"
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
SHA_WRONG = "b" * 40
REPO = "synthetic-org/sample-orders-api"
PROMPT = "# SYNTHETIC PRQA prompt (transport test)\n" + ("Test the orders API with curl against $BASE_URL using $TOKEN. " * 8)
TAG = f"{os.getpid()}{int(time.time()) % 100000}"
# PRQA_DOCKER_TEST_IMAGE=<ref>: run the transport cases against an existing image (e.g. a sandbox image candidate) instead
# of the disposable stub. Cases that would reach the OpenCode invocation (F, G) are skipped in that mode: this suite
# never executes OpenCode.
EXTERNAL_IMAGE = os.environ.get("PRQA_DOCKER_TEST_IMAGE") or ""
IMAGE = EXTERNAL_IMAGE or f"prqa/transport-test:disposable-{TAG}"
EVIDENCE: list[dict] = []


def sh(argv, *, stdin_bytes=None, timeout=180):
    kw = {"capture_output": True, "timeout": timeout, "check": False}
    if stdin_bytes is not None:
        kw["input"] = stdin_bytes
    p = subprocess.run(argv, **kw)
    return p.returncode, (p.stdout or b"").decode("utf-8", "replace"), (p.stderr or b"").decode("utf-8", "replace")


def make_tree(root: Path, nfiles=3):
    root.mkdir(parents=True)
    if nfiles == 3:
        (root / "src").mkdir(); (root / "src" / "app.py").write_text("print('hi')\n")
        (root / "README.md").write_text("# synthetic\n")
        (root / "run.sh").write_text("#!/bin/sh\necho x\n"); (root / "run.sh").chmod(0o755)
    else:
        for i in range(nfiles):
            sub = root / f"d{i % 7}"; sub.mkdir(exist_ok=True); (sub / f"f{i}.txt").write_text(str(i))
    (root / ".git").mkdir(); (root / ".git" / "config").write_text("[remote]\n\turl = https://x-access-token:FAKE@github.test/o/r\n")
    os.symlink("/etc/passwd", root / "passwd-link")


@unittest.skipUnless(ENABLED, "set PRQA_DOCKER_INTEGRATION=1 with a local Docker daemon to run the real transport test")
class DockerTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if EXTERNAL_IMAGE:
            rc, out, _ = sh(["docker", "image", "inspect", EXTERNAL_IMAGE, "--format", "{{.Id}}"])
            if rc != 0:
                raise unittest.SkipTest(f"{EXTERNAL_IMAGE} is not present locally and pulling is not allowed")
            cls.image_id = out.strip()
            cls.tmp = Path(tempfile.mkdtemp(prefix="prqa-xfer-"))
            EVIDENCE.append({"image": EXTERNAL_IMAGE, "image_id": cls.image_id, "mode": "external image (no stub build)"})
            return
        rc, out, _ = sh(["docker", "image", "inspect", BASE_IMAGE, "--format", "{{.Id}}"])
        if rc != 0:
            raise unittest.SkipTest(f"{BASE_IMAGE} is not cached locally and pulling is not allowed")
        cls.base_id = out.strip()
        cls.ctx = Path(tempfile.mkdtemp(prefix="prqa-xfer-ctx-"))
        for f in ("entrypoint.sh", "verify_checkout.py", "populate_checkout.py"):
            shutil.copy(ROOT / "worker" / "sandbox" / f, cls.ctx / f)
        # same layout as Dockerfile.opencode minus OpenCode/git/npm (no network needed)
        (cls.ctx / "Dockerfile").write_text(
            f"FROM {BASE_IMAGE}\n"
            "RUN groupadd -g 10001 sandbox && useradd -u 10001 -g 10001 -M -d /home/sandbox -s /bin/bash sandbox\n"
            "COPY entrypoint.sh /usr/local/bin/prqa-sandbox-entrypoint\n"
            "COPY verify_checkout.py /usr/local/bin/prqa-verify-checkout\n"
            "COPY populate_checkout.py /usr/local/bin/prqa-populate-checkout\n"
            "RUN chmod 0555 /usr/local/bin/prqa-sandbox-entrypoint /usr/local/bin/prqa-verify-checkout /usr/local/bin/prqa-populate-checkout"
            " && mkdir -p /checkout && chown 10001:10001 /checkout && chmod 0755 /checkout\n"
            "USER 10001:10001\nWORKDIR /work\nENTRYPOINT [\"/usr/local/bin/prqa-sandbox-entrypoint\"]\n")
        rc, out, err = sh(["docker", "build", "--network", "none", "-t", IMAGE, str(cls.ctx)], timeout=600)
        if rc != 0:
            shutil.rmtree(cls.ctx, ignore_errors=True)
            raise unittest.SkipTest(f"disposable image build failed offline: {err[-300:]}")
        cls.tmp = Path(tempfile.mkdtemp(prefix="prqa-xfer-"))

    @classmethod
    def tearDownClass(cls):
        leftovers = []
        for kind, flt in (("container", "name=prqa-sandbox-"), ("container", "name=prqa-populate-")):
            rc, out, _ = sh(["docker", "ps", "-a", "--filter", flt, "--filter", f"label=prqa.test={TAG}", "--format", "{{.Names}}"])
            for n in out.split():
                sh(["docker", "rm", "-f", n]); leftovers.append(n)
        rc, out, _ = sh(["docker", "volume", "ls", "--filter", "label=prqa.kind=checkout", "--format", "{{.Name}}"])
        for v in out.split():
            if TAG in v or "xfer" in v:
                sh(["docker", "volume", "rm", "-f", v]); leftovers.append(v)
        if not EXTERNAL_IMAGE:
            sh(["docker", "rmi", "-f", IMAGE])
        shutil.rmtree(getattr(cls, "ctx", "/nonexistent"), ignore_errors=True)
        shutil.rmtree(getattr(cls, "tmp", "/nonexistent"), ignore_errors=True)
        EVIDENCE.append({"teardown_removed": leftovers, "image_removed": None if EXTERNAL_IMAGE else IMAGE})
        print("\nDOCKER TRANSPORT EVIDENCE\n" + json.dumps(EVIDENCE, indent=1))

    # ------------------------------------------------------------------ helpers --
    def job(self, job_id):
        return {"id": job_id, "execution_mode": "live", "synthetic": False,
                "request": {"repository": REPO, "test_level": "standard", "synthetic": False, "pr": {"number": 101, "head_sha": SHA, "title": "synthetic"}}}

    def artifact(self, job_id, nfiles=3):
        ws = self.tmp / "workspaces" / job_id
        if not (ws / "repo").exists():
            make_tree(ws / "repo", nfiles)
        m = pack_checkout_artifact(repo_dir=ws / "repo", out_path=ws / "checkout.tar", job_id=job_id, repository=REPO, head_sha=SHA, prompt_text=PROMPT)
        self.manifests = getattr(self, "manifests", {}); self.manifests[job_id] = m
        return ws / "checkout.tar", m

    def plan(self, job_id, artifact, *, tree=None, prompt=None, runtime_minutes=1):
        m = self.manifests[job_id]
        return opencode_runner.build_sandbox_plan(image=IMAGE, network="none", job=self.job(job_id), model="p/m", runtime_minutes=runtime_minutes, env={},
                                                  artifact_path=str(artifact), tree_sha256=tree or m["tree_sha256"], prompt_sha256=prompt or m["prompt_sha256"])

    def hash_args(self, job_id):
        m = self.manifests[job_id]
        return ["--tree-sha256", m["tree_sha256"], "--prompt-sha256", m["prompt_sha256"]]

    def run_transport(self, name, job_id, artifact, *, entry_args=None, tar_bytes=None, skip_populate=False, exact_plan=False):
        """Execute the real steps (not execute_plan, so each step's outcome is recorded) and always clean up."""
        plan = self.plan(job_id, artifact)
        create = list(plan.create)
        if not exact_plan:
            idx = create.index(IMAGE)
            create = create[:idx + 1] + (entry_args if entry_args is not None else ["--repo", REPO, "--sha", SHA, *self.hash_args(job_id), "--verify-checkout-only"])
        rec = {"case": name, "job": job_id, "volume": plan.volume, "steps": []}
        src = Path(artifact)
        if tar_bytes is not None:
            src = Path(artifact).with_name("checkout-variant.tar"); src.write_bytes(tar_bytes)
        try:
            rc, out, err = sh(plan.volume_create); rec["steps"].append({"step": "volume create", "rc": rc, "stderr": err.strip()[:160]})
            self.assertEqual(rc, 0, err)
            if not skip_populate:
                rc, out, err = sh(plan.helper_create); rec["steps"].append({"step": "helper create", "rc": rc, "stderr": err.strip()[:160]}); self.assertEqual(rc, 0, err)
                rc, out, err = sh(["docker", "cp", str(src), f"{plan.helper}:/work/checkout.tar"]); rec["steps"].append({"step": "helper cp (archive API)", "rc": rc, "bytes": src.stat().st_size, "stderr": err.strip()[:160]}); self.assertEqual(rc, 0, err)
                rc, out, err = sh(plan.helper_start); self.assertEqual(rc, 0, err)
                rc, out, err = sh(plan.helper_wait, timeout=300); helper_rc = int(out.strip().splitlines()[-1])
                rc, lo, le = sh(plan.helper_logs)
                rec["steps"].append({"step": "populate (helper wait)", "rc": helper_rc, "stdout": lo.strip()[-120:], "stderr": le.strip()[-200:]})
                rec["populate_rc"] = helper_rc
                if helper_rc != 0:
                    rec["outcome"] = "populate refused"
                    return rec
            rc, out, err = sh(create); rec["steps"].append({"step": "create", "rc": rc, "stderr": err.strip()[:160]})
            self.assertEqual(rc, 0, err)
            t0 = time.time()
            rc, out, err = sh(plan.start); self.assertEqual(rc, 0, err)
            rc, out, err = sh(["docker", "wait", plan.container], timeout=300); exit_code = int(out.strip().splitlines()[-1])
            rc, lo, le = sh(plan.logs)
            rec["steps"].append({"step": "run (start+wait+logs)", "rc": exit_code, "seconds": round(time.time() - t0, 2), "stdout": lo.strip()[-240:], "stderr": le.strip()[-240:]})
            rec["last_stdout_line"] = lo.strip().splitlines()[-1] if lo.strip() else ""
            rc_i, insp, _ = sh(["docker", "inspect", "--format",
                                "exit={{.State.ExitCode}} readonly={{.HostConfig.ReadonlyRootfs}} net={{.HostConfig.NetworkMode}} user={{.Config.User}} "
                                "binds={{len .HostConfig.Binds}} mounts={{range .Mounts}}{{.Type}}:{{.Name}}:{{.Destination}}:rw={{.RW}} {{end}}env={{.Config.Env}} image={{.Image}}", plan.container])
            rec["inspect"] = insp.strip()
            rec["exit"] = exit_code
            rec["outcome"] = f"container exit {exit_code}"
            return rec
        finally:
            outcome = opencode_runner.cleanup_plan(plan)
            rc1, c, _ = sh(["docker", "ps", "-a", "--filter", f"name={plan.container}", "--format", "{{.Names}}"])
            rc2, v, _ = sh(["docker", "volume", "ls", "--filter", f"name=^{plan.volume}$", "--format", "{{.Name}}"])
            rec["cleanup"] = {"ok": outcome["ok"], "actions": [r["action"] for r in outcome["resources"]], "container_left": c.strip() != "", "volume_left": v.strip() != ""}
            if tar_bytes is not None:
                src.unlink(missing_ok=True)
            EVIDENCE.append(rec)

    def assert_sandbox_config(self, rec, volume):
        if "image=" in rec.get("inspect", ""):
            self.assertIn(f"image={getattr(self, 'image_id', '')}" if EXTERNAL_IMAGE else "image=sha256:", rec["inspect"])
        insp = rec["inspect"]
        self.assertIn("readonly=true", insp); self.assertIn("net=none", insp); self.assertIn("user=10001:10001", insp); self.assertIn("binds=0", insp)
        self.assertIn(f"volume:{volume}:/checkout:rw=false", insp)
        self.assertNotIn("docker.sock", insp)
        for secret_name in ("GITHUB_TOKEN", "TOKEN=", "OPENROUTER", "PRQA_WEBHOOK_SECRET", "PRQA_WORKER_TOKEN", "DISCORD"):
            self.assertNotIn(secret_name, insp)
        self.assertFalse(rec["cleanup"]["container_left"]); self.assertFalse(rec["cleanup"]["volume_left"]); self.assertTrue(rec["cleanup"]["ok"])

    # -------------------------------------------------------------------- cases --
    def test_A_valid_artifact(self):
        art, m = self.artifact("20261022-xfera00001")
        rec = self.run_transport("A-valid", "20261022-xfera00001", art)
        self.assertEqual(rec.get("populate_rc"), 0, rec)
        self.assertEqual(rec["exit"], 0, rec)
        self.assertIn("checkout verified", rec["steps"][-1]["stdout"]); self.assertIn("3 files", rec["steps"][-1]["stdout"])
        self.assert_sandbox_config(rec, rec["volume"])

    def test_B_wrong_sha(self):
        art, _ = self.artifact("20261022-xferb00001")
        rec = self.run_transport("B-wrong-sha", "20261022-xferb00001", art, entry_args=["--repo", REPO, "--sha", SHA_WRONG, *self.hash_args("20261022-xferb00001"), "--verify-checkout-only"])
        self.assertEqual(rec["exit"], 66, rec); self.assertNotIn("checkout verified", rec["steps"][-1]["stdout"])
        self.assertIn("head_sha", rec["steps"][-1]["stderr"]); self.assert_sandbox_config(rec, rec["volume"])

    def test_C_500_files(self):
        art, m = self.artifact("20261022-xferc00500", nfiles=500)
        self.assertEqual(m["files"], 500)
        rec = self.run_transport("C-500-files", "20261022-xferc00500", art)
        self.assertEqual(rec["exit"], 0, rec); self.assertIn("500 files", rec["steps"][-1]["stdout"])
        self.assertLess(rec["steps"][-1]["seconds"], 60); self.assert_sandbox_config(rec, rec["volume"])

    def test_D_missing_malformed_and_tampered(self):
        art, _ = self.artifact("20261022-xferd00001")
        rec = self.run_transport("D1-missing-artifact", "20261022-xferd00001", art, skip_populate=True)
        self.assertEqual(rec["exit"], 66, rec); self.assertIn("layout missing", rec["steps"][-1]["stderr"])
        self.assert_sandbox_config(rec, rec["volume"])

        def rewrite(mutate):
            buf = io.BytesIO()
            with tarfile.open(art) as src, tarfile.open(fileobj=buf, mode="w") as dst:
                for member in src.getmembers():
                    data = src.extractfile(member).read()
                    data = mutate(member.name, data); member.size = len(data)
                    dst.addfile(member, io.BytesIO(data))
            return buf.getvalue()
        bad_manifest = rewrite(lambda n, d: b"{not json" if n == "prqa/manifest.json" else d)
        rec = self.run_transport("D2-malformed-manifest", "20261022-xferd00001", art, tar_bytes=bad_manifest)
        self.assertEqual(rec.get("populate_rc"), 70, rec)                      # the helper already refuses it
        self.assertIn("manifest is not JSON", rec["steps"][-1]["stderr"]); self.assertFalse(rec["cleanup"]["volume_left"])
        tampered = rewrite(lambda n, d: b"# tampered\n" if n == "repo/README.md" else d)
        rec = self.run_transport("D3-tampered-content", "20261022-xferd00001", art, tar_bytes=tampered)
        self.assertEqual(rec.get("populate_rc"), 0, rec)                       # structurally fine → the sandbox verifier catches it
        self.assertEqual(rec["exit"], 66, rec); self.assertIn("tree hash mismatch", rec["steps"][-1]["stderr"])
        self.assert_sandbox_config(rec, rec["volume"])
        evil = rewrite(lambda n, d: d)
        buf = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(evil)) as src, tarfile.open(fileobj=buf, mode="w") as dst:
            for member in src.getmembers():
                dst.addfile(member, src.extractfile(member))
            info = tarfile.TarInfo("repo/evil-link"); info.type = tarfile.SYMTYPE; info.linkname = "/etc/passwd"; dst.addfile(info)
        rec = self.run_transport("D4-symlink-member", "20261022-xferd00001", art, tar_bytes=buf.getvalue())
        self.assertEqual(rec.get("populate_rc"), 70, rec); self.assertIn("non-regular member", rec["steps"][-1]["stderr"])

    def test_E_cross_job_isolation(self):
        art_a, _ = self.artifact("20261022-xfere0000a")
        art_b, _ = self.artifact("20261022-xfere0000b")
        plan_a, plan_b = self.plan("20261022-xfere0000a", art_a), self.plan("20261022-xfere0000b", art_b)
        try:
            for plan, art in ((plan_a, art_a), (plan_b, art_b)):
                self.assertEqual(sh(plan.volume_create)[0], 0)
                self.assertEqual(sh(plan.helper_create)[0], 0); self.assertEqual(sh(plan.helper_copy)[0], 0); self.assertEqual(sh(plan.helper_start)[0], 0)
                rc, out, err = sh(plan.helper_wait); self.assertEqual(out.strip().splitlines()[-1], "0", err)
            # job A's cleanup removes only A's resources; B's populated volume survives
            codes = opencode_runner.cleanup_plan(plan_a)["ok"]
            rc, v, _ = sh(["docker", "volume", "ls", "--filter", "label=prqa.kind=checkout", "--format", "{{.Name}}"])
            self.assertNotIn(plan_a.volume, v.split()); self.assertIn(plan_b.volume, v.split())
            # job A cannot be given job B's artifact (plan builder refuses before any docker call)
            with self.assertRaises(opencode_runner.PolicyViolation):
                self.plan("20261022-xfere0000a", art_b)
            # a sandbox for job B mounts only B's volume, read-only, and verifies B's content
            rec = self.run_transport("E-job-b-after-a-cleanup", "20261022-xfere0000b", art_b, skip_populate=True)
            self.assertEqual(rec["exit"], 0, rec); self.assertIn(f"volume:{plan_b.volume}:/checkout:rw=false", rec["inspect"])
            self.assertNotIn(plan_a.volume, rec["inspect"])
            EVIDENCE.append({"case": "E-isolation", "a_cleanup_codes": codes, "volumes_after_a_cleanup": v.split()})
        finally:
            opencode_runner.cleanup_plan(plan_a); opencode_runner.cleanup_plan(plan_b)

    @unittest.skipIf(EXTERNAL_IMAGE, "exact plan would reach the OpenCode invocation in a real sandbox image; stub image only")
    def test_F_exact_plan_arguments(self):
        """The exact plan command line: ENTRYPOINT must parse it (no 'unknown argument'). Without OpenCode in the image
        the run step fails harmlessly and the INCONCLUSIVE fallback document is the last stdout line."""
        art, _ = self.artifact("20261022-xferf00001")
        rec = self.run_transport("F-exact-plan-args", "20261022-xferf00001", art, exact_plan=True)
        self.assertNotIn("unknown argument", rec["steps"][-1]["stderr"])
        self.assertEqual(rec["exit"], 0, rec)
        last = json.loads(rec["last_stdout_line"])
        self.assertEqual(last["summary"]["overall_status"], "INCONCLUSIVE"); self.assertEqual(last["job_id"], "20261022-xferf00001")
        self.assert_sandbox_config(rec, rec["volume"])

    # ------------------------------------------------ production path: execute_plan ---
    @unittest.skipIf(EXTERNAL_IMAGE, "execute_plan success path would reach the OpenCode invocation in a real sandbox image; stub image only")
    def test_G_execute_plan_production_path(self):
        """REAL execute_plan: volume → helper create/cp/start/wait → create → start → inspect poll → logs → verified cleanup → result doc."""
        art, m = self.artifact("20261022-xferg00001")
        plan = self.plan("20261022-xferg00001", art)
        events = []
        out = opencode_runner.execute_plan(plan, env={}, timeout_s=120, report=lambda k, d: events.append((k, d)))
        self.assertEqual(out["summary"]["overall_status"], "INCONCLUSIVE")          # no OpenCode in the image → honest fallback
        self.assertEqual(out["job_id"], "20261022-xferg00001"); self.assertEqual(out["pr"]["head_sha"], SHA)
        kind, detail = events[-1]
        self.assertEqual(kind, "sandbox_cleanup"); self.assertTrue(detail["ok"], detail)
        self.assertEqual([r["action"] for r in detail["resources"]], ["removed", "removed", "removed"])   # sandbox, helper (no --rm any more), volume
        self.assertTrue(all(r["verified_absent"] for r in detail["resources"]))
        rc, v, _ = sh(["docker", "volume", "ls", "--filter", f"name=^{plan.volume}$", "--format", "{{.Name}}"]); self.assertEqual(v.strip(), "")
        rc, c, _ = sh(["docker", "ps", "-a", "--filter", f"name={plan.container}", "--format", "{{.Names}}"]); self.assertEqual(c.strip(), "")
        EVIDENCE.append({"case": "G-execute_plan", "result_status": out["summary"]["overall_status"], "cleanup": detail})

    def test_H_execute_plan_helper_and_sandbox_exit_codes_propagate(self):
        art, m = self.artifact("20261022-xferh00001")
        # helper refuses a malformed manifest → RuntimeError carrying exit 70; nothing created afterwards; cleanup verified
        buf = io.BytesIO()
        with tarfile.open(art) as src, tarfile.open(fileobj=buf, mode="w") as dst:
            for member in src.getmembers():
                data = src.extractfile(member).read()
                if member.name == "prqa/manifest.json":
                    data = b"{not json"; member.size = len(data)
                dst.addfile(member, io.BytesIO(data))
        bad = self.tmp / "workspaces" / "20261022-xferh00001" / "checkout.tar"
        good_bytes = bad.read_bytes(); bad.write_bytes(buf.getvalue())
        plan = self.plan("20261022-xferh00001", bad)
        events = []
        with self.assertRaises(RuntimeError) as ctx:
            opencode_runner.execute_plan(plan, env={}, timeout_s=120, report=lambda k, d: events.append((k, d)))
        self.assertIn("'populate' exited 70", str(ctx.exception)); self.assertIn("manifest is not JSON", str(ctx.exception))
        self.assertTrue(events[-1][1]["ok"]); self.assertEqual(events[-1][1]["resources"][0]["action"], "already_absent")   # sandbox never created
        # sandbox refuses a wrong worker-expected tree hash → exit 66 propagates with the verifier's reason
        bad.write_bytes(good_bytes)
        plan = self.plan("20261022-xferh00001", bad, tree="f" * 64)
        events = []
        with self.assertRaises(RuntimeError) as ctx:
            opencode_runner.execute_plan(plan, env={}, timeout_s=120, report=lambda k, d: events.append((k, d)))
        self.assertIn("'run' exited 66", str(ctx.exception)); self.assertIn("worker-expected", str(ctx.exception))
        self.assertTrue(events[-1][1]["ok"])
        EVIDENCE.append({"case": "H-exit-codes", "helper": "70 propagated", "sandbox": "66 propagated", "cleanup_ok": True})

    def test_I_execute_plan_timeout_terminates_and_cleans_up(self):
        art, m = self.artifact("20261022-xferi00001")
        plan = self.plan("20261022-xferi00001", art)
        # same hardened create command, but the container runs `sleep` instead of the entrypoint so the timeout path is real
        create = list(plan.create)
        idx = create.index(IMAGE)
        create = create[:idx] + ["--entrypoint", "/bin/sleep", IMAGE, "300"]
        slow = opencode_runner.SandboxPlan(**{**plan.__dict__, "create": create})
        events = []
        t0 = time.time()
        with self.assertRaises(RuntimeError) as ctx:
            opencode_runner.execute_plan(slow, env={}, timeout_s=3, poll_s=0.5, report=lambda k, d: events.append((k, d)))
        self.assertIn("timed out", str(ctx.exception)); self.assertLess(time.time() - t0, 60)
        self.assertTrue(events[-1][1]["ok"], events[-1][1])
        rc, c, _ = sh(["docker", "ps", "-a", "--filter", f"name={plan.container}", "--format", "{{.Names}}"]); self.assertEqual(c.strip(), "")
        EVIDENCE.append({"case": "I-timeout", "seconds": round(time.time() - t0, 1), "cleanup_ok": events[-1][1]["ok"]})

    def test_J_cancellation_terminates_running_sandbox(self):
        art, m = self.artifact("20261022-xferj00001")
        plan = self.plan("20261022-xferj00001", art)
        create = list(plan.create); idx = create.index(IMAGE)
        slow = opencode_runner.SandboxPlan(**{**plan.__dict__, "create": create[:idx] + ["--entrypoint", "/bin/sleep", IMAGE, "300"]})
        started = time.time()

        def cancel_check():
            return time.time() - started > 4.0           # cancellation "arrives" while the sandbox is running (after populate+create)
        events = []
        with self.assertRaises(opencode_runner.SandboxCancelled) as ctx:
            opencode_runner.execute_plan(slow, env={}, timeout_s=120, poll_s=0.5, cancel_check=cancel_check, report=lambda k, d: events.append((k, d)))
        self.assertIn("terminated", str(ctx.exception)); self.assertLess(time.time() - started, 30)
        self.assertTrue(events[-1][1]["ok"])
        rc, c, _ = sh(["docker", "ps", "-a", "--filter", f"name={plan.container}", "--format", "{{.Names}}"]); self.assertEqual(c.strip(), "")
        EVIDENCE.append({"case": "J-cancel-running", "seconds": round(time.time() - started, 1), "cleanup_ok": events[-1][1]["ok"]})

    def test_K_cleanup_failure_is_observable(self):
        """A second container keeps the job's volume busy → `volume rm` fails → the outcome says so (no false success)."""
        art, m = self.artifact("20261022-xferk00001")
        plan = self.plan("20261022-xferk00001", art)
        blocker = f"prqa-xfer-blocker-{TAG}"
        try:
            self.assertEqual(sh(plan.volume_create)[0], 0)
            rc, out, err = sh(["docker", "create", "--name", blocker, "--network", "none", "--mount", f"type=volume,src={plan.volume},dst=/checkout,readonly", IMAGE, "--repo", "o/r"])
            self.assertEqual(rc, 0, err)
            outcome = opencode_runner.cleanup_plan(plan)
            self.assertFalse(outcome["ok"])
            failed = {f["name"]: f for f in outcome["failed"]}
            self.assertIn(plan.volume, failed); self.assertEqual(failed[plan.volume]["action"], "still_present"); self.assertIn("in use", failed[plan.volume]["error"])
            rc, v, _ = sh(["docker", "volume", "ls", "--filter", f"name=^{plan.volume}$", "--format", "{{.Name}}"]); self.assertEqual(v.strip(), plan.volume)
            EVIDENCE.append({"case": "K-cleanup-failure-observable", "outcome_ok": outcome["ok"], "failed": outcome["failed"]})
        finally:
            sh(["docker", "rm", "-f", blocker])
            final = opencode_runner.cleanup_plan(plan)
            self.assertTrue(final["ok"], final)

    def test_L_reconcile_removes_orphans_of_terminal_jobs_only(self):
        from prqa_worker import launcher
        art_t, _ = self.artifact("20261022-xferl0term")
        art_a, _ = self.artifact("20261022-xferl0actv")
        plan_t, plan_a = self.plan("20261022-xferl0term", art_t), self.plan("20261022-xferl0actv", art_a)
        try:
            for plan in (plan_t, plan_a):                       # simulate a launcher that died after creating resources
                self.assertEqual(sh(plan.volume_create)[0], 0)
                self.assertEqual(sh(plan.create)[0], 0)
            status = {"20261022-xferl0term": "failed", "20261022-xferl0actv": "running"}
            rep = launcher.reconcile(job_status=lambda j: status.get(j), dry_run=True)
            names = {r["name"] for r in rep.removed}
            self.assertEqual(names, {plan_t.container, plan_t.volume})
            rep = launcher.reconcile(job_status=lambda j: status.get(j))
            self.assertTrue(rep.to_dict()["ok"]); self.assertEqual({r["name"] for r in rep.removed}, {plan_t.container, plan_t.volume})
            rc, c, _ = sh(["docker", "ps", "-a", "--filter", f"name={plan_a.container}", "--format", "{{.Names}}"]); self.assertEqual(c.strip(), plan_a.container)
            rc, v, _ = sh(["docker", "volume", "ls", "--filter", f"name=^{plan_t.volume}$", "--format", "{{.Name}}"]); self.assertEqual(v.strip(), "")
            rep2 = launcher.reconcile(job_status=lambda j: status.get(j))
            self.assertEqual(rep2.removed, [], "idempotent")
            out = launcher.cancel_sandbox("20261022-xferl0actv")
            self.assertTrue(out["ok"])
            EVIDENCE.append({"case": "L-reconcile", "removed": sorted(names), "kept_active": plan_a.container, "cancel_ok": out["ok"]})
        finally:
            opencode_runner.cleanup_plan(plan_t); opencode_runner.cleanup_plan(plan_a)


if __name__ == "__main__":
    unittest.main()
