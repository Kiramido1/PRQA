"""ISOLATED launcher integration test (opt-in: PRQA_LAUNCHER_INTEGRATION=1). Real pieces: the launcher CLI
(scripts/services/prqa_launcher.py) as a separate process, the Docker policy proxy (proxy/docker_policy_proxy.py) as a separate
process in front of the host daemon socket, the production worker App served over loopback HTTP, the production plan
builder / transport / cleanup / reconcile code, and the docker CLI. Synthetic pieces: a fake git checkout (no network),
a disposable stub image (real entrypoint + verifier + extractor, `opencode` replaced by a stub that writes a synthetic
result), synthetic jobs for the placeholder repository. The promoted sandbox image is used only where its real
entrypoint stops before the OpenCode invocation (hash mismatch → exit 66).

Never executes OpenCode, never contacts a model provider, never touches containers/volumes it did not create.

    PRQA_LAUNCHER_INTEGRATION=1 PYTHONPATH=worker python3 -m unittest tests.test_launcher_integration -v
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "proxy"))
sys.path.insert(0, str(ROOT))

import docker_policy as dp  # noqa: E402
import docker_policy_proxy as dpp  # noqa: E402
from prqa_worker import opencode_runner  # noqa: E402
from prqa_worker.server import App, Handler as WorkerHandler  # noqa: E402
from prqa_worker.workspace import pack_checkout_artifact  # noqa: E402
from tests.test_docker_transport import make_tree  # noqa: E402
from tests.test_launch_lifecycle import LT, PROMPT, SHA, WT, _FakeCheckout, live_env, live_job_request  # noqa: E402

ENABLED = os.environ.get("PRQA_LAUNCHER_INTEGRATION") == "1" and shutil.which("docker") is not None
REAL_SOCK = "/var/run/docker.sock"
PROMOTED_TAG = "prqa/opencode-sandbox:local"
PROMOTED_ID = "sha256:d0efff568953e15bf0d57758dfd30d435803fc05b39c98ec2cc46f0199d967e5"
TAG = f"{os.getpid()}{int(time.time()) % 100000}"
STUB_TAG = f"prqa/launcher-test:stub-{TAG}"
EVIDENCE: dict = {"proxy_decisions": [], "jobs": {}, "launcher_runs": [], "notes": []}


def real(argv, *, timeout=120):
    p = subprocess.run(["docker", "-H", "unix://" + REAL_SOCK, *argv], capture_output=True, text=True, timeout=timeout, check=False)
    return p.returncode, p.stdout, p.stderr


def snapshot():
    """Names of every container, volume and network on the daemon (taken through the real socket)."""
    out = {}
    for kind, argv in (("containers", ["ps", "-a", "--format", "{{.Names}}"]), ("volumes", ["volume", "ls", "--format", "{{.Name}}"]),
                       ("networks", ["network", "ls", "--format", "{{.Name}}"])):
        rc, text, _ = real(argv)
        out[kind] = set(text.split())
    return out


@unittest.skipUnless(ENABLED, "set PRQA_LAUNCHER_INTEGRATION=1 with a local Docker daemon to run the isolated launcher integration test")
class LauncherIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rc, out, err = real(["image", "inspect", PROMOTED_TAG, "--format", "{{.Id}}"])
        if rc != 0 or out.strip() != PROMOTED_ID:
            raise unittest.SkipTest(f"promoted image ID mismatch or missing: {out.strip()!r}")
        cls.tmp = Path(tempfile.mkdtemp(prefix="prqa-li-"))          # short path: unix socket names are length-limited
        cls.baseline = snapshot()
        # --- disposable stub image (offline) ---
        ctx = cls.tmp / "ctx"; ctx.mkdir()
        for f in ("entrypoint.sh", "verify_checkout.py", "populate_checkout.py"):
            shutil.copy(ROOT / "worker" / "sandbox" / f, ctx / f)
        for f in ("Dockerfile", "stub_opencode.py"):
            shutil.copy(ROOT / "tests" / "launcher_stub" / f, ctx / f)
        shutil.copy(ROOT / "fixtures" / "synthetic" / "opencode-result.synthetic.json", ctx / "result-template.json")
        rc, out, err = real(["build", "--network", "none", "--pull=false", "-t", STUB_TAG, str(ctx)], timeout=600)
        if rc != 0:
            raise unittest.SkipTest(f"stub image build failed offline: {err[-300:]}")
        rc, out, _ = real(["image", "inspect", STUB_TAG, "--format", "{{.Id}}"])
        cls.stub_id = out.strip()
        # --- real policy proxy process in front of the host daemon ---
        cls.sock = str(cls.tmp / "policy.sock"); cls.proxy_log = cls.tmp / "proxy.log"
        cls.proxy = subprocess.Popen([sys.executable, str(ROOT / "proxy" / "docker_policy_proxy.py"), "--listen", cls.sock, "--upstream", REAL_SOCK,
                                      "--image-id", cls.stub_id, "--image-id", PROMOTED_ID, "--image-tag", STUB_TAG, "--log-file", str(cls.proxy_log)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if os.path.exists(cls.sock):
                break
            time.sleep(0.1)
        # --- production worker App over loopback HTTP, with the git checkout faked (no network) ---
        import prqa_worker.executor as ex
        cls._orig_checkout = ex.run_checkout
        ex.run_checkout = _FakeCheckout()
        wdir = cls.tmp / "worker"; wdir.mkdir()
        # This suite runs the launcher WITHOUT a model proxy (the stub never calls a model), so the worker hands out a
        # synthetic non-proxy model id; the default prqa/<model> without a configured proxy is refused by the launcher
        # (tests/test_sandbox_network_policy.py), and the proxy path runs in tests/test_phase2_containers.py.
        cls.app = App(live_env(str(wdir), OPENCODE_MODEL="synthetic/stub-no-model"))
        WorkerHandler.app = cls.app
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), WorkerHandler)
        cls.worker_url = f"http://127.0.0.1:{cls.httpd.server_port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.app.executor.start()
        EVIDENCE["topology"] = {"stub_image": STUB_TAG, "stub_image_id": cls.stub_id, "promoted_image_id": PROMOTED_ID, "proxy_socket": cls.sock,
                                "proxy_upstream": REAL_SOCK, "worker_url": cls.worker_url, "sandbox_network": "none"}

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.executor.stop(); cls.httpd.shutdown(); cls.httpd.server_close()
        finally:
            import prqa_worker.executor as ex
            ex.run_checkout = cls._orig_checkout
            cls.proxy.terminate()
            try:
                cls.proxy.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.proxy.kill()
        leftovers = []
        for job_id in EVIDENCE["jobs"]:
            for kind, name in (("container", f"prqa-sandbox-{job_id}"), ("container", f"prqa-populate-{job_id}"), ("volume", f"prqa-checkout-{job_id}")):
                exists = real(["container" if kind == "container" else "volume", "inspect", name])[0] == 0   # rm -f exits 0 for absent ones
                if exists:
                    real(["rm", "-f", name] if kind == "container" else ["volume", "rm", "-f", name]); leftovers.append(name)
        real(["rmi", "-f", STUB_TAG])
        after = snapshot()
        EVIDENCE["cleanup"] = {"leftovers_removed_at_teardown": leftovers, "stub_image_removed": STUB_TAG,
                               "daemon_delta": {k: {"added": sorted(after[k] - cls.baseline[k]), "missing": sorted(cls.baseline[k] - after[k])} for k in after}}
        (cls.tmp / "evidence.json").write_text(json.dumps(EVIDENCE, indent=1, default=str))
        out = os.environ.get("PRQA_LI_EVIDENCE_OUT")
        if out:
            shutil.copy(cls.tmp / "evidence.json", out); shutil.copy(cls.proxy_log, Path(out).with_name("proxy-decisions.log"))
        print("\nLAUNCHER INTEGRATION EVIDENCE\n" + json.dumps(EVIDENCE, indent=1, default=str)[:12000])
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ----------------------------------------------------------------- helpers --
    def proxy_decisions(self, since: int = 0):
        text = self.proxy_log.read_text() if self.proxy_log.exists() else ""
        rows = []
        for line in text[since:].splitlines():
            if " decision " in line:
                parts = line.split(" decision ", 1)[1].split()
                rows.append({"decision": parts[0], "method": parts[1], "path": dp.VERSION_PREFIX.sub("", parts[2]), "rest": " ".join(parts[3:])})
        return rows

    def log_offset(self) -> int:
        return len(self.proxy_log.read_text()) if self.proxy_log.exists() else 0

    def worker(self, method, path, body=None, *, token=WT, extra=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=30)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", "x-worker-token": token}
        headers.update(extra or {})
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse(); raw = resp.read(); conn.close()
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, {"raw": raw[:200]}

    def launcher_api(self, method, path, body=None, *, launcher_id="launcher-A"):
        return self.worker(method, path, body, token="", extra={"x-launcher-token": LT, "x-launcher-id": launcher_id})

    def launcher_env(self, launcher_id: str) -> dict:
        home = self.tmp / f"home-{launcher_id}"; home.mkdir(exist_ok=True)
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LANG": "C.UTF-8",
                "PRQA_WORKER_URL": self.worker_url, "PRQA_LAUNCHER_TOKEN": LT, "PRQA_LAUNCHER_ID": launcher_id,
                "PRQA_SANDBOX_IMAGE": STUB_TAG, "PRQA_SANDBOX_IMAGE_ID": self.stub_id, "PRQA_SANDBOX_NETWORK": "none",
                "DOCKER_HOST": "unix://" + self.sock, "PRQA_LAUNCHER_WORKDIR": str(self.tmp / f"work-{launcher_id}"),
                "PRQA_LAUNCH_HEARTBEAT_S": "1", "PRQA_LAUNCH_POLL_S": "0.5", "PRQA_LAUNCH_LEASE_S": "20"}

    def run_launcher(self, launcher_id="launcher-A", *args, wait=True):
        p = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "services" / "prqa_launcher.py"), *(args or ("--once",))], env=self.launcher_env(launcher_id),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(self.tmp))
        if not wait:
            return p
        out, _ = p.communicate(timeout=600)
        EVIDENCE["launcher_runs"].append({"launcher": launcher_id, "args": list(args or ("--once",)), "rc": p.returncode, "tail": out[-1500:]})
        return p.returncode, out

    _pr_counter = [100]

    def new_pending_job(self, delivery: str, title: str = "Launcher integration stub") -> str:
        req = live_job_request(delivery); req["pr"]["title"] = title
        self._pr_counter[0] += 1; req["pr"]["number"] = self._pr_counter[0]          # distinct PRs: the worker dedupes repo#pr@sha:level
        req["pr"]["url"] = f"https://github.com/your-org/your-first-real-repo/pull/{req['pr']['number']}"
        st, body = self.worker("POST", "/jobs", req)
        self.assertEqual(st, 201, body)
        job_id = body["job"]["id"]
        for _ in range(300):
            job = self.app.store.get(job_id)                          # the HTTP job view is the public one; launch fields live in the store
            if job.get("launch_state") == "pending" or job.get("status") in ("failed", "cancelled"):
                break
            time.sleep(0.1)
        self.assertEqual(job.get("launch_state"), "pending", {"job": job, "events": self.job_events(job_id)})
        EVIDENCE["jobs"][job_id] = {"delivery": delivery, "transitions": ["queued", "running/handoff", "running/pending"]}
        return job_id

    def job_events(self, job_id):
        return [e["kind"] for e in self.app.store.events(job_id=job_id)]

    def record_job(self, job_id, **more):
        job = self.app.store.get(job_id)
        EVIDENCE["jobs"][job_id].update({"final_status": job["status"], "launch_state": job.get("launch_state"), "launch_owner": job.get("launch_owner"),
                                        "launch_attempts": job.get("launch_attempts"), "events": self.job_events(job_id), **more})
        return job

    def job_resources_present(self, job_id):
        rc, c, _ = real(["ps", "-a", "--filter", f"label=prqa.job={job_id}", "--format", "{{.Names}}"])
        rc, v, _ = real(["volume", "ls", "--filter", f"label=prqa.job={job_id}", "--format", "{{.Name}}"])
        return sorted(c.split() + v.split())

    def raw(self, method, path, body=None, headers=None):
        conn = dpp.UnixHTTPConnection(self.sock, timeout=30)
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        h = {"Content-Type": "application/json"}; h.update(headers or {})
        if data is not None:
            h["Content-Length"] = str(len(data))
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse(); raw = resp.read(); conn.close()
        return resp.status, raw

    # ------------------------------------------------------------- Phase 2: proxy --
    def test_1_proxy_denies_forbidden_requests_before_the_daemon(self):
        job = "20261022-deadbeef0001"
        base = {"Image": self.stub_id, "User": "10001:10001", "Labels": {"prqa.job": job, "prqa.kind": "sandbox"},
                "Cmd": ["--repo", "o/r", "--sha", SHA, "--model", "p/m", "--timeout-minutes", "5", "--tree-sha256", "c" * 64, "--prompt-sha256", "d" * 64, "--prechecked"],
                "Env": ["PRQA_JOB_ID=" + job], "HostConfig": {"NetworkMode": "none", "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"], "ReadonlyRootfs": True,
                                                              "Memory": 2 * 1024 ** 3, "PidsLimit": 256, "NanoCpus": 2 * 10 ** 9, "Tmpfs": {"/tmp": "size=1m"},
                                                              "Mounts": [{"Type": "volume", "Source": f"prqa-checkout-{job}", "Target": "/checkout", "ReadOnly": True}]}}

        def variant(**over):
            spec = json.loads(json.dumps(base))
            for k, v in over.items():
                (spec["HostConfig"] if k.startswith("hc.") else spec)[k.replace("hc.", "")] = v
            return spec
        before = snapshot(); offset = self.log_offset()
        cases = [
            ("privileged container", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(**{"hc.Privileged": True}), 403),
            ("bind mount", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(**{"hc.Binds": ["/:/host"]}), 403),
            ("docker socket mount", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(**{"hc.Mounts": base["HostConfig"]["Mounts"] + [{"Type": "bind", "Source": REAL_SOCK, "Target": "/s"}]}), 403),
            ("host networking", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(**{"hc.NetworkMode": "host"}), 403),
            ("bridge networking", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(**{"hc.NetworkMode": "bridge"}), 403),
            ("added capability", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(**{"hc.CapAdd": ["SYS_ADMIN"]}), 403),
            ("unapproved image", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(Image="python:3.13-slim"), 403),
            ("mutable tag instead of pinned id", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(Image=PROMOTED_TAG), 403),
            ("entrypoint override", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(Entrypoint=["/bin/sh", "-c", "id"]), 403),
            ("shell command", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(Cmd=["/bin/sh", "-c", "id"]), 403),
            ("root user", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(User="0:0"), 403),
            ("foreign volume mount", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(**{"hc.Mounts": [{"Type": "volume", "Source": "prqa_worker_data", "Target": "/checkout", "ReadOnly": True}]}), 403),
            ("other job's volume", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(**{"hc.Mounts": [{"Type": "volume", "Source": "prqa-checkout-20261022-deadbeef0002", "Target": "/checkout", "ReadOnly": True}]}), 403),
            ("writable checkout", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(**{"hc.Mounts": [{"Type": "volume", "Source": f"prqa-checkout-{job}", "Target": "/checkout", "ReadOnly": False}]}), 403),
            ("secret in env", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(Env=["GITHUB_TOKEN=ghp_x"]), 403),
            ("DOCKER_HOST in env", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(Env=["DOCKER_HOST=unix:///var/run/docker.sock"]), 403),
            ("writable rootfs", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(**{"hc.ReadonlyRootfs": False}), 403),
            ("seccomp unconfined", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(**{"hc.SecurityOpt": ["seccomp=unconfined"]}), 403),
            ("label mismatch", "POST", f"/containers/create?name=prqa-sandbox-{job}", variant(Labels={"prqa.job": "20261022-deadbeef0002", "prqa.kind": "sandbox"}), 403),
            ("non-PRQA name", "POST", "/containers/create?name=evil", base, 403),
            ("exec", "POST", f"/containers/prqa-sandbox-{job}/exec", {"Cmd": ["sh"]}, 403),
            ("exec on worker", "POST", "/containers/prqa-prqa-worker-1/exec", {"Cmd": ["sh"]}, 403),
            ("attach + Upgrade", "POST", f"/containers/prqa-sandbox-{job}/attach?stream=1", None, 403),
            ("build", "POST", "/build?t=evil", b"", 403),
            ("pull", "POST", "/images/create?fromImage=alpine", b"", 403),
            ("commit", "POST", f"/commit?container=prqa-sandbox-{job}", b"", 403),
            ("network create", "POST", "/networks/create", {"Name": "evil"}, 403),
            ("container prune", "POST", "/containers/prune", b"", 403),
            ("volume prune", "POST", "/volumes/prune", b"", 403),
            ("image prune", "POST", "/images/prune", b"", 403),
            ("delete worker container", "DELETE", "/containers/prqa-prqa-worker-1?force=1", None, 403),
            ("stop n8n container", "POST", "/containers/prqa-n8n-1/stop", b"", 403),
            ("inspect worker container", "GET", "/containers/prqa-prqa-worker-1/json", None, 403),
            ("delete worker volume", "DELETE", "/volumes/prqa_worker_data?force=1", None, 403),
            ("inspect unapproved image", "GET", "/images/python:3.13-slim/json", None, 403),
            ("events stream", "GET", "/events", None, 403),
            ("plugins", "GET", "/plugins", None, 403),
            ("swarm", "POST", "/swarm/init", {}, 403),
            ("malformed create body", "POST", f"/containers/create?name=prqa-sandbox-{job}", b"{not json", 403),
            ("archive into sandbox", "PUT", f"/containers/prqa-sandbox-{job}/archive?path=/work", b"x", 403),
            ("archive outside /work", "PUT", f"/containers/prqa-populate-{job}/archive?path=/etc", b"x", 403),
            ("PATCH method", "PATCH", "/containers/json", None, 405),
        ]
        results = []
        for name, method, path, body, expected in cases:
            headers = {"Connection": "Upgrade", "Upgrade": "tcp"} if "Upgrade" in name else None
            st, raw = self.raw(method, path, body, headers)
            results.append({"case": name, "method": method, "path": path.split("?")[0], "status": st})
            self.assertEqual(st, expected, f"{name}: {st} {raw[:200]!r}")
        # garbage request line: fail closed at the HTTP layer
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(10); s.connect(self.sock)
        s.sendall(b"\x16\x03\x01 garbage\r\n\r\n"); first = s.recv(200); s.close()
        self.assertTrue(first.startswith(b"HTTP/1.1 400") or b"400" in first or first == b"", first[:60])   # stdlib answers an HTTP/0.9-style 400 page and closes
        results.append({"case": "garbage request line", "status": "400/closed"})
        decisions = self.proxy_decisions(offset)
        self.assertTrue(all(d["decision"] == "deny" and "forwarded=false" in d["rest"] for d in decisions), decisions)
        self.assertGreaterEqual(len(decisions), len(cases))
        self.assertEqual(snapshot(), before, "a denied request changed daemon state")
        rc, out, _ = real(["ps", "--filter", "name=prqa-prqa-worker-1", "--format", "{{.Status}}"]); self.assertIn("Up", out)
        EVIDENCE["proxy_denials"] = results

    def test_2_proxy_allows_production_transport_with_promoted_image_up_to_exit_66(self):
        """The real promoted image through the real proxy: helper extracts the artifact (PUT archive), sandbox runs the
        real entrypoint, which fails verification on a deliberately wrong tree hash (66) BEFORE the OpenCode line."""
        job_id = "20261022-" + format(int(TAG) % (16 ** 12), "012x")
        ws = self.tmp / "p2" / job_id; make_tree(ws / "repo")
        m = pack_checkout_artifact(repo_dir=ws / "repo", out_path=ws / "checkout.tar", job_id=job_id, repository="synthetic-org/sample-orders-api", head_sha=SHA, prompt_text=PROMPT)
        job = {"id": job_id, "execution_mode": "live", "request": {"repository": "synthetic-org/sample-orders-api", "test_level": "standard", "pr": {"number": 1, "head_sha": SHA, "title": "p2"}}}
        plan = opencode_runner.build_sandbox_plan(image=PROMOTED_ID, network="none", job=job, model="p/m", runtime_minutes=1, env={},
                                                  artifact_path=str(ws / "checkout.tar"), tree_sha256="f" * 64, prompt_sha256=m["prompt_sha256"])
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.tmp), "DOCKER_HOST": "unix://" + self.sock}
        EVIDENCE["jobs"][job_id] = {"phase": "2 (promoted image via proxy, wrong tree hash)"}
        offset = self.log_offset()

        def via(argv, timeout=120):
            p = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout, check=False); return p.returncode, p.stdout, p.stderr
        try:
            self.assertEqual(via(plan.volume_create)[0], 0)
            self.assertEqual(via(plan.helper_create)[0], 0)
            rc, out, err = via(plan.helper_copy); self.assertEqual(rc, 0, err)          # docker cp → HEAD/PUT archive through the proxy
            self.assertEqual(via(plan.helper_start)[0], 0)
            rc, out, err = via(plan.helper_wait); self.assertEqual(out.strip().splitlines()[-1], "0", err)
            rc, out, err = via(plan.create); self.assertEqual(rc, 0, err)
            self.assertEqual(via(plan.start)[0], 0)
            for _ in range(120):
                rc, out, _ = via(plan.inspect_state)
                if out.split()[0] == "exited":
                    break
                time.sleep(0.5)
            self.assertEqual(out.split(), ["exited", "66"], out)
            rc, lo, le = via(plan.logs)
            self.assertIn("mismatch", (lo + le).lower()); self.assertNotIn("opencode run", lo + le)
            rc, insp, _ = via(["docker", "container", "inspect", "--format", "image={{.Image}} readonly={{.HostConfig.ReadonlyRootfs}} net={{.HostConfig.NetworkMode}} user={{.Config.User}} "
                                "binds={{len .HostConfig.Binds}} caps={{.HostConfig.CapDrop}} mounts={{range .Mounts}}{{.Type}}:{{.Name}}:rw={{.RW}} {{end}}", plan.container])
            self.assertIn(f"image={PROMOTED_ID}", insp); self.assertIn("readonly=true", insp); self.assertIn("net=none", insp); self.assertIn("user=10001:10001", insp)
            self.assertIn("binds=0", insp); self.assertIn("caps=[ALL]", insp); self.assertIn(f"volume:{plan.volume}:rw=false", insp)
            EVIDENCE["jobs"][job_id]["sandbox_inspect"] = insp.strip()
        finally:
            outcome = opencode_runner.cleanup_plan(plan, runner=lambda argv, **kw: subprocess.run(argv, env=env, **kw))
            EVIDENCE["jobs"][job_id]["cleanup"] = outcome
        self.assertTrue(outcome["ok"], outcome); self.assertEqual(self.job_resources_present(job_id), [])
        decisions = self.proxy_decisions(offset)
        self.assertTrue(decisions and all(d["decision"] == "allow" for d in decisions), [d for d in decisions if d["decision"] != "allow"])
        paths = [f"{d['method']} {d['path']}" for d in decisions]
        for needle in ("POST /volumes/create", "POST /containers/create", f"PUT /containers/prqa-populate-{job_id}/archive",
                       f"POST /containers/prqa-populate-{job_id}/start", f"POST /containers/prqa-populate-{job_id}/wait",
                       f"POST /containers/prqa-sandbox-{job_id}/start", f"GET /containers/prqa-sandbox-{job_id}/json",
                       f"GET /containers/prqa-sandbox-{job_id}/logs", f"DELETE /containers/prqa-sandbox-{job_id}", f"DELETE /volumes/prqa-checkout-{job_id}"):
            self.assertTrue(any(p.startswith(needle) for p in paths), f"{needle} not seen in {paths}")
        EVIDENCE["jobs"][job_id]["proxy_ops"] = paths

    # ------------------------------------------------------ Phase 3: launcher --
    def test_3_launcher_cycle_succeeds_through_the_proxy(self):
        job_id = self.new_pending_job("li-1")
        offset = self.log_offset()
        rc, out = self.run_launcher("launcher-A")
        self.assertEqual(rc, 0, out[-800:]); self.assertIn(f"job {job_id}: succeeded", out, out[-1500:])
        job = self.record_job(job_id, launcher="launcher-A")
        self.assertEqual((job["status"], job["launch_state"], job["launch_owner"]), ("succeeded", "finished", "launcher-A"))
        self.assertEqual(job["result"]["job_id"], job_id); self.assertIn("STUB RESULT", job["result"]["summary"]["executive_summary"])
        self.assertTrue(job["launch"]["cleanup"]["ok"], job["launch"]["cleanup"])
        kinds = self.job_events(job_id)
        for k in ("launch_pending", "launch_claimed", "sandbox_cleanup", "launch_finished"):
            self.assertIn(k, kinds)
        self.assertEqual(kinds.count("launch_claimed"), 1)
        self.assertEqual(self.job_resources_present(job_id), [])
        decisions = self.proxy_decisions(offset)
        denied = [d for d in decisions if d["decision"] != "allow"]
        self.assertEqual(denied, [], denied)
        paths = [f"{d['method']} {d['path']}" for d in decisions]
        self.assertTrue(any(p.startswith(f"GET /images/{STUB_TAG}/json") or p.startswith("GET /images/prqa/launcher-test") for p in paths), paths[:5])
        self.assertEqual(sum(1 for p in paths if p == "POST /containers/create"), 2, "helper + sandbox, nothing else")
        self.assertTrue(all(job_id in p or p.startswith(("HEAD /_ping", "GET /images/", "POST /volumes/create", "POST /containers/create", "GET /containers/json", "GET /volumes")) for p in paths), paths)
        EVIDENCE["jobs"][job_id]["proxy_ops"] = paths
        # hand-off is owner-bound and idempotent
        st, body = self.launcher_api("POST", f"/jobs/{job_id}/launch/result", {"launcher_id": "launcher-B", "result": job["result"], "cleanup": {"ok": True, "resources": []}}, launcher_id="launcher-B")
        self.assertEqual(st, 409, body)
        st, body = self.launcher_api("POST", f"/jobs/{job_id}/launch/result", {"launcher_id": "launcher-A", "result": job["result"], "cleanup": {"ok": True, "resources": []}})
        self.assertEqual((st, body.get("already")), (200, True), body)
        st, body = self.launcher_api("POST", f"/jobs/{job_id}/launch/failed", {"launcher_id": "launcher-A", "error": {"type": "x", "message": "late"}, "cleanup": {"ok": True, "resources": []}})
        self.assertEqual((st, body.get("already")), (200, True)); self.assertEqual(self.app.store.get(job_id)["status"], "succeeded")
        self.assertEqual(self.worker("POST", f"/jobs/{job_id}/launch/heartbeat", {})[0], 401)   # worker-token callers cannot reach launcher endpoints
        self.assertEqual(self.launcher_api("GET", "/launch/pending", launcher_id="x")[0], 200)
        st, _ = self.worker("GET", "/launch/pending", token="", extra={"x-launcher-token": "wrong"}); self.assertEqual(st, 401)

    def test_4_two_launchers_one_claim_no_duplicate_sandbox(self):
        job_id = self.new_pending_job("li-2")
        offset = self.log_offset()
        pa = self.run_launcher("launcher-A", wait=False); pb = self.run_launcher("launcher-B", wait=False)
        oa, _ = pa.communicate(timeout=600); ob, _ = pb.communicate(timeout=600)
        EVIDENCE["launcher_runs"] += [{"launcher": "launcher-A", "race": True, "rc": pa.returncode, "tail": oa[-800:]}, {"launcher": "launcher-B", "race": True, "rc": pb.returncode, "tail": ob[-800:]}]
        winners = [n for n, o in (("launcher-A", oa), ("launcher-B", ob)) if f"job {job_id}: succeeded" in o]
        self.assertEqual(len(winners), 1, (oa[-600:], ob[-600:]))
        loser_out = ob if winners == ["launcher-A"] else oa
        self.assertTrue("CLAIMED_BY_OTHER" in loser_out or f"job {job_id}" not in loser_out, loser_out[-600:])
        job = self.record_job(job_id, winner=winners[0], loser_saw_claimed_by_other="CLAIMED_BY_OTHER" in loser_out)
        self.assertEqual((job["status"], job["launch_owner"]), ("succeeded", winners[0]))
        self.assertEqual(self.job_events(job_id).count("launch_claimed"), 1)
        creates = [d for d in self.proxy_decisions(offset) if d["method"] == "POST" and d["path"] == "/containers/create"]
        self.assertEqual(len(creates), 2, "exactly one helper + one sandbox created for the job")
        self.assertEqual(self.job_resources_present(job_id), [])

    def test_5_cancellation_terminates_running_sandbox(self):
        job_id = self.new_pending_job("li-3", title="STUB-SLEEP=120")
        offset = self.log_offset()
        p = self.run_launcher("launcher-A", wait=False)
        name = f"prqa-sandbox-{job_id}"
        for _ in range(240):
            rc, out, _ = real(["ps", "--filter", f"name=^{name}$", "--filter", "status=running", "--format", "{{.Names}}"])
            if out.strip() == name:
                break
            time.sleep(0.25)
        self.assertEqual(out.strip(), name, "sandbox never reached running state")
        rc, insp, _ = real(["container", "inspect", "--format", "image={{.Image}} readonly={{.HostConfig.ReadonlyRootfs}} net={{.HostConfig.NetworkMode}} user={{.Config.User}} "
                             "binds={{len .HostConfig.Binds}} caps={{.HostConfig.CapDrop}} secopt={{.HostConfig.SecurityOpt}} mounts={{range .Mounts}}{{.Type}}:{{.Name}}:rw={{.RW}} {{end}}env={{.Config.Env}}", name])
        self.assertIn(f"image={self.stub_id}", insp); self.assertIn("readonly=true", insp); self.assertIn("net=none", insp); self.assertIn("user=10001:10001", insp)
        self.assertIn("binds=0", insp); self.assertIn(f"volume:prqa-checkout-{job_id}:rw=false", insp)
        for secret_name in ("GITHUB_TOKEN", "TOKEN=", "OPENROUTER", "DISCORD", "PRQA_WORKER_TOKEN", "PRQA_LAUNCHER_TOKEN", "PRQA_MODEL_TOKEN", "PRQA_MODEL_PROXY", "DOCKER_HOST", LT, WT):
            self.assertNotIn(secret_name, insp)
        EVIDENCE["jobs"][job_id]["sandbox_inspect_while_running"] = insp.strip()[:600]
        st, body = self.worker("POST", f"/jobs/{job_id}/cancel", {"reason": "LABEL_REMOVED"}); self.assertEqual(st, 200, body)
        out, _ = p.communicate(timeout=300)
        EVIDENCE["launcher_runs"].append({"launcher": "launcher-A", "cancel": True, "rc": p.returncode, "tail": out[-800:]})
        self.assertIn(f"job {job_id}: cancelled", out, out[-1200:])
        job = self.record_job(job_id, cancelled_while="running")
        self.assertEqual((job["status"], job["launch_state"]), ("cancelled", "cancelled")); self.assertIsNone(job.get("result"))
        self.assertEqual(self.job_resources_present(job_id), [])
        paths = [f"{d['method']} {d['path']}" for d in self.proxy_decisions(offset)]
        self.assertIn(f"DELETE /containers/{name}", paths)
        self.assertEqual([p for p in paths if p.startswith("POST /containers/create")].__len__(), 2)

    def test_6_reconcile_keeps_active_and_unrelated_resources(self):
        done = next(j for j, rec in EVIDENCE["jobs"].items() if rec.get("final_status") == "succeeded")
        active = self.new_pending_job("li-4")
        st, body = self.launcher_api("POST", f"/jobs/{active}/launch/claim", {"launcher_id": "launcher-Z", "lease_s": 300}, launcher_id="launcher-Z")
        self.assertEqual(st, 200, body)
        decoy_done, decoy_active = f"prqa-checkout-{done}", f"prqa-checkout-{active}"
        for name, job in ((decoy_done, done), (decoy_active, active)):
            self.assertEqual(real(["volume", "create", "--label", f"prqa.job={job}", "--label", "prqa.kind=checkout", name])[0], 0)
        before = snapshot(); offset = self.log_offset()
        try:
            rc, out = self.run_launcher("launcher-A", "--reconcile-only", "--apply")
            self.assertEqual(rc, 0, out[-600:])
            after = snapshot()
            self.assertNotIn(decoy_done, after["volumes"], "terminal job's orphan removed")
            self.assertIn(decoy_active, after["volumes"], "active (claimed) job's volume kept")
            self.assertEqual(before["containers"], after["containers"]); self.assertEqual(before["networks"], after["networks"])
            self.assertEqual(before["volumes"] - {decoy_done}, after["volumes"])
            decisions = self.proxy_decisions(offset)
            self.assertEqual([d for d in decisions if d["decision"] != "allow"], [])
            EVIDENCE["reconcile"] = {"removed": [decoy_done], "kept": [decoy_active], "launcher_tail": out[-500:], "proxy_ops": [f"{d['method']} {d['path']}" for d in decisions]}
        finally:
            real(["volume", "rm", "-f", decoy_active]); real(["volume", "rm", "-f", decoy_done])
            self.worker("POST", f"/jobs/{active}/cancel", {"reason": "PR_CLOSED"})
            self.launcher_api("POST", f"/jobs/{active}/launch/cancelled", {"launcher_id": "launcher-Z", "cleanup": {"ok": True, "resources": []}}, launcher_id="launcher-Z")
            self.record_job(active, role="active decoy (claimed by launcher-Z, never launched)")

    def test_7_config_safety_checks(self):
        env = self.launcher_env("launcher-C")
        for bad_env, needle in (({"PRQA_SANDBOX_IMAGE_ID": "sha256:" + "0" * 64}, "image ID mismatch"), ({"PRQA_SANDBOX_IMAGE_ID": ""}, "pin"),
                                ({"PRQA_SANDBOX_NETWORK": "bridge"}, "network"), ({"PRQA_SANDBOX_NETWORK": "prqa_prqa_internal"}, "network"),
                                ({"PRQA_SANDBOX_IMAGE": PROMOTED_TAG}, "not present locally")):   # the proxy only lets the launcher inspect the pinned tag
            p = subprocess.run([sys.executable, str(ROOT / "scripts" / "services" / "prqa_launcher.py"), "--check"], env={**env, **bad_env}, capture_output=True, text=True, timeout=120)
            self.assertNotEqual(p.returncode, 0, (bad_env, p.stdout, p.stderr)); self.assertIn(needle, (p.stdout + p.stderr).lower() if needle == "pin" else p.stdout + p.stderr)
        p = subprocess.run([sys.executable, str(ROOT / "scripts" / "services" / "prqa_launcher.py"), "--check"], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr); self.assertEqual(json.loads(p.stdout.strip().splitlines()[-1])["image_id"], self.stub_id)
        self.assertNotIn("PRQA_MODEL_PROXY", json.dumps(env)); self.assertFalse(any(k in env for k in ("GITHUB_TOKEN", "DISCORD_WEBHOOK_URL", "OPENROUTER_API_KEY", "PRQA_WORKER_TOKEN")))
        # pass_target_token remains refused by the plan builder (policy level, independent of the proxy)
        job = {"id": "20261022-deadbeef0003", "execution_mode": "live", "request": {"repository": "o/r", "test_level": "standard", "pr": {"number": 1, "head_sha": SHA, "title": "t"}}}
        with self.assertRaises(opencode_runner.PolicyViolation):
            opencode_runner.build_sandbox_plan(image=self.stub_id, network="none", job=job, model="p/m", runtime_minutes=1, env={"TOKEN": "x"},
                                               artifact_path=str(self.tmp / "none.tar"), tree_sha256="a" * 64, prompt_sha256="b" * 64, pass_target_token=True)
        EVIDENCE["notes"].append("model proxy not configured for the launcher: no token issued, no PRQA_MODEL_* in the sandbox env; pass_target_token refused")


if __name__ == "__main__":
    unittest.main()
