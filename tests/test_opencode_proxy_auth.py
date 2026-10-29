"""REAL OpenCode → PRQA model proxy → fake Groq, offline (opt-in: PRQA_OPENCODE_PROXY_AUTH=1).

Real pieces: the promoted sandbox image (OpenCode 1.19.3 + the reviewed worker/sandbox/entrypoint.sh — the test refuses
an image whose entrypoint differs from the source), the production plan builder / transport / cleanup
(opencode_runner), the Docker policy proxy (proxy/docker_policy_proxy.py) in front of the host daemon, and the model
proxy (proxy/model_proxy.py) with its unix admin socket. Synthetic pieces: the checkout, the provider key, the admin
token, and the "provider": tests/phase2_stack/fake_groq_opencode.py on the proxy's loopback, scripted to drive a
read → write (evidence dir, allowed) → write (outside it, denied) tool sequence. Everything runs on a disposable internal network with inhibit_ipv4: nothing can leave the
host, no real provider is reachable, no model generates anything.

    PRQA_OPENCODE_PROXY_AUTH=1 PYTHONPATH=worker python3 -m unittest tests.test_opencode_proxy_auth -v
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "worker"), str(ROOT)]

from prqa_worker import opencode_runner  # noqa: E402
from prqa_worker.validation import SchemaStore, validate_result  # noqa: E402
from prqa_worker.workspace import pack_checkout_artifact  # noqa: E402
from tests.test_docker_transport import make_tree  # noqa: E402

ENABLED = os.environ.get("PRQA_OPENCODE_PROXY_AUTH") == "1" and shutil.which("docker") is not None
REAL_SOCK = "/var/run/docker.sock"
IMAGE = os.environ.get("PRQA_OPENCODE_TEST_IMAGE", "prqa/opencode-sandbox:local")
MODEL, UPSTREAM_MODEL = "prqa/openai/gpt-oss-120b", "openai/gpt-oss-120b"
TAG = f"{os.getpid()}{int(time.time()) % 100000}"
NET, PROXY, FAKE = f"prqa-ocauth-{TAG}", f"prqa-ocauth-proxy-{TAG}", f"prqa-ocauth-fake-{TAG}"
ADMIN = "admin-token-SYNTHETIC-ocauth-0123456789abcdef"
PROVIDER_KEY = "gsk_SYNTHETIC_ocauth_provider_key_0000000000000000000"
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
REPO = "synthetic-org/sample-orders-api"
PROMPT = "# SYNTHETIC PRQA prompt (OpenCode proxy authentication test)\n" + ("Read src/app.py, then write the result document to $PRQA_EVIDENCE_DIR/qa-result.json. " * 6)
JOB_OK, JOB_REVOKED, JOB_NOTOKEN = (f"20261027-{TAG[-6:]}ok0001", f"20261027-{TAG[-6:]}rv0002", f"20261027-{TAG[-6:]}nt0003")
CONTRACT = {"stream": False, "temperature": 0.2, "reasoning_effort": "low", "include_reasoning": False, "max_completion_tokens": 3000}
UPSTREAM_KEYS = {"model", "messages", "tools", "tool_choice", "parallel_tool_calls", "stop", "response_format", *CONTRACT}
UPSTREAM_HEADERS = ["accept", "accept-encoding", "authorization", "content-length", "content-type", "host", "user-agent", "x-title"]
EVIDENCE: dict = {"jobs": {}, "notes": []}


def real(argv, *, timeout=120, env=None):
    p = subprocess.run(["docker", "-H", "unix://" + REAL_SOCK, *argv], capture_output=True, text=True, timeout=timeout, check=False, env=env)
    return p.returncode, p.stdout, p.stderr


def result_doc(job_id: str) -> str:
    doc = json.loads((ROOT / "fixtures/synthetic/opencode-result.synthetic.json").read_text(encoding="utf-8"))
    doc.update({"synthetic": False, "job_id": job_id, "repository": REPO, "executed_by": {"engine": "opencode", "mode": "live"},
                "environment": {"name": "staging", "base_url_redacted": "https://staging.example.test"}})
    doc["pr"] = {"number": 1, "title": "OpenCode proxy auth", "head_sha": SHA}
    doc["summary"]["executive_summary"] = "SYNTHETIC result written by OpenCode's write tool (scripted fake provider)."
    return json.dumps(doc)


@unittest.skipUnless(ENABLED, "set PRQA_OPENCODE_PROXY_AUTH=1 with a local Docker daemon, the promoted sandbox image and python:3.13-slim")
class OpenCodeProxyAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for image in (IMAGE, "python:3.13-slim"):
            if real(["image", "inspect", image])[0] != 0:
                raise unittest.SkipTest(f"{image} is not available locally")
        cls.image_id = real(["image", "inspect", IMAGE, "--format", "{{.Id}}"])[1].strip()
        rc, out, _ = real(["run", "--rm", "--network", "none", "--entrypoint", "sha256sum", cls.image_id, "/usr/local/bin/prqa-sandbox-entrypoint"])
        source = hashlib.sha256((ROOT / "worker/sandbox/entrypoint.sh").read_bytes()).hexdigest()
        if rc != 0 or out.split()[0] != source:
            raise unittest.SkipTest(f"{IMAGE} does not carry the reviewed entrypoint (rebuild/overlay it first)")
        cls.tmp = Path(tempfile.mkdtemp(prefix="prqa-oca-"))
        cls.started = []
        key = cls.tmp / "provider.key"; key.write_text(PROVIDER_KEY); key.chmod(0o600)
        admin_dir = cls.tmp / "admin"; admin_dir.mkdir(mode=0o700)
        cls.admin_sock = str(admin_dir / "admin.sock")
        cls.budget_dir = cls.tmp / "budget"; cls.budget_dir.mkdir(mode=0o700); cls.budget_dir.chmod(0o700)   # disposable ledger
        uid, gid = os.getuid(), os.getgid()
        cls._must(["network", "create", "--internal", "-o", "com.docker.network.bridge.inhibit_ipv4=true", "--label", "prqa.net=sandbox",
                   "--label", "prqa.test=ocauth", NET], "network")
        cls._must(["run", "-d", "--name", PROXY, "--label", "prqa.test=ocauth", "--network", NET, "--network-alias", "prqa-model-proxy",
                   "--user", f"{uid}:{gid}", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp:size=16m",
                   "-v", f"{ROOT / 'proxy'}:/app:ro", "-v", f"{key}:/run/secrets/prqa_model_provider_key:ro", "-v", f"{admin_dir}:/run/prqa-model-admin",
                   "-v", f"{cls.budget_dir}:/run/prqa-budget", "-e", "PRQA_PROVIDER_BUDGET_DIR=/run/prqa-budget",
                   "-e", "PRQA_BUDGET_DAILY_REQUESTS=100", "-e", "PRQA_BUDGET_DAILY_TOKENS=10000000", "-e", "PRQA_BUDGET_JOB_TOKENS=1000000",
                   "-e", "PRQA_BUDGET_TOKENS_PER_MINUTE=1000000", "-e", "PRQA_BUDGET_MAX_IN_FLIGHT=1", "-e", "PRQA_BUDGET_MAX_WAIT_S=10",
                   "-e", "PRQA_MODEL_UPSTREAM_ORIGIN=http://127.0.0.1:9099", "-e", "PRQA_MODEL_UPSTREAM_PREFIX=/openai/v1",
                   "-e", f"PRQA_MODEL_ALLOWED_MODEL={UPSTREAM_MODEL}", "-e", "PRQA_MODEL_PROVIDER_KEY_FILE=/run/secrets/prqa_model_provider_key",
                   "-e", f"PRQA_MODEL_PROXY_ADMIN_TOKEN={ADMIN}", "python:3.13-slim", "python3", "-I", "/app/model_proxy.py", "--bind", "0.0.0.0",
                   "--port", "8089", "--admin-socket", "/run/prqa-model-admin/admin.sock", "--admin-socket-gid", str(gid)], PROXY)
        cls._must(["run", "-d", "--name", FAKE, "--label", "prqa.test=ocauth", "--network", f"container:{PROXY}", "--user", "10004:10004",
                   "--read-only", "--cap-drop", "ALL", "-v", f"{ROOT / 'tests' / 'phase2_stack'}:/fake:ro",
                   "-e", f"P2IT_SYNTHETIC_PROVIDER_KEY={PROVIDER_KEY}", "-e", f"P2IT_RESULT_DOC={result_doc(JOB_OK)}",
                   "-e", f"P2IT_CAPTURE_FULL={'1' if os.environ.get('PRQA_OCAUTH_CAPTURE_OUT') else '0'}",
                   "python:3.13-slim", "python3", "-I", "/fake/fake_groq_opencode.py", "9099"], FAKE)
        cls.policy_sock = str(cls.tmp / "policy.sock"); cls.policy_log = cls.tmp / "policy.log"
        cls.policy = subprocess.Popen([sys.executable, str(ROOT / "proxy" / "docker_policy_proxy.py"), "--listen", cls.policy_sock, "--upstream", REAL_SOCK,
                                       "--image-id", cls.image_id, "--image-tag", IMAGE, "--network", NET, "--log-file", str(cls.policy_log)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if os.path.exists(cls.admin_sock) and os.path.exists(cls.policy_sock):
                break
            time.sleep(0.1)
        else:
            cls.tearDownClass()
            raise RuntimeError("model proxy admin socket or policy proxy socket did not appear")
        EVIDENCE["topology"] = {"image": IMAGE, "image_id": cls.image_id, "entrypoint_sha256": source, "network": NET,
                                "network_options": "internal, inhibit_ipv4, prqa.net=sandbox", "model": MODEL, "upstream_model": UPSTREAM_MODEL}

    @classmethod
    def _must(cls, argv, what):
        rc, out, err = real(argv, timeout=120)
        if rc != 0:
            cls.tearDownClass()
            raise RuntimeError(f"could not start {what}: {err[-300:]}")
        cls.started.append(what)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "policy", None):
            cls.policy.terminate()
            try:
                cls.policy.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.policy.kill()
        EVIDENCE["fake_provider_requests"] = cls.fake_requests() if FAKE in getattr(cls, "started", []) else []
        capture_out = os.environ.get("PRQA_OCAUTH_CAPTURE_OUT")     # opt-in: the exact upstream body OpenCode produced
        if capture_out and FAKE in getattr(cls, "started", []):
            caps = [json.loads(l) for l in real(["logs", FAKE])[1].splitlines() if l.startswith('{"p2it_capture"')]
            if caps:
                fd = os.open(capture_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as fh:
                    json.dump(caps[0]["body"], fh)
        _, o, e = real(["logs", PROXY]) if PROXY in getattr(cls, "started", []) else (0, "", "")
        EVIDENCE["model_proxy_log_tail"] = (o + e).splitlines()[-30:]
        for job in (JOB_OK, JOB_REVOKED, JOB_NOTOKEN):
            for argv in (["rm", "-f", f"prqa-sandbox-{job}", f"prqa-populate-{job}"], ["volume", "rm", "-f", f"prqa-checkout-{job}"]):
                real(argv)
        real(["rm", "-f", FAKE, PROXY])
        real(["network", "rm", NET])
        left = real(["ps", "-a", "--filter", "label=prqa.test=ocauth", "--format", "{{.Names}}"])[1].split()
        left += real(["network", "ls", "--filter", "label=prqa.test=ocauth", "--format", "{{.Name}}"])[1].split()
        EVIDENCE["cleanup"] = {"leftovers": left}
        out = os.environ.get("PRQA_OCAUTH_EVIDENCE_OUT")
        if out:
            Path(out).write_text(json.dumps(EVIDENCE, indent=1, default=str))
        print("\nOPENCODE PROXY AUTH EVIDENCE\n" + json.dumps(EVIDENCE, indent=1, default=str)[:12000])
        if getattr(cls, "tmp", None):
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # ----------------------------------------------------------------- helpers --
    @staticmethod
    def fake_requests():
        return [json.loads(l) for l in real(["logs", FAKE])[1].splitlines() if l.startswith('{"p2it_upstream"')]

    def admin(self, method, path, body=None):
        conn = http.client.HTTPConnection("localhost", timeout=10)
        conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); conn.sock.connect(self.admin_sock)
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                     headers={"Authorization": "Bearer " + ADMIN, "Content-Type": "application/json"})
        resp = conn.getresponse(); raw = resp.read(); conn.close()
        return resp.status, (json.loads(raw) if raw else {})

    def run_job(self, job_id, token):
        ws = self.tmp / "jobs" / job_id; make_tree(ws / "repo")
        m = pack_checkout_artifact(repo_dir=ws / "repo", out_path=ws / "checkout.tar", job_id=job_id, repository=REPO, head_sha=SHA, prompt_text=PROMPT)
        job = {"id": job_id, "execution_mode": "live", "request": {"repository": REPO, "test_level": "standard",
                                                                    "pr": {"number": 1, "head_sha": SHA, "title": "OpenCode proxy auth"}}}
        env = {"PRQA_MODEL_TOKEN": token, "PRQA_MODEL_BASE_URL": "http://prqa-model-proxy:8089/v1"} if token else {}
        plan = opencode_runner.build_sandbox_plan(image=self.image_id, network=NET, job=job, model=MODEL, runtime_minutes=4, env=env,
                                                  artifact_path=str(ws / "checkout.tar"), tree_sha256=m["tree_sha256"],
                                                  prompt_sha256=m["prompt_sha256"], model_token=bool(token))
        argv_text = json.dumps([plan.create, plan.helper_create, plan.start])
        for secret in (PROVIDER_KEY, ADMIN) + ((token,) if token else ()):
            self.assertNotIn(secret, argv_text, "no credential on any docker command line")
        self.assertNotIn("/run/secrets", argv_text); self.assertNotIn("prqa-model-admin", argv_text)
        denv = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.tmp), "DOCKER_HOST": "unix://" + self.policy_sock}
        events: list = []
        rec = EVIDENCE["jobs"].setdefault(job_id, {"token": "issued" if token else "none"})
        try:
            result = opencode_runner.execute_plan(plan, env=env, runner=lambda argv, **kw: subprocess.run(argv, env=denv, **kw),
                                                  timeout_s=400, poll_s=1.0, report=lambda k, d: events.append((k, d)))
            rec["outcome"] = "completed"
            return result, events
        except RuntimeError as exc:
            rec["outcome"] = f"failed: {str(exc)[:300]}"
            raise
        finally:
            rec["cleanup_ok"] = next((d.get("ok") for k, d in events if k == "sandbox_cleanup"), None)

    # ------------------------------------------------------------------- tests --
    def test_1_real_opencode_uses_the_proxy_with_the_job_token_and_completes_a_tool_round_trip(self):
        st, issued = self.admin("POST", "/admin/tokens", {"job_id": JOB_OK, "model": UPSTREAM_MODEL, "ttl_s": 900})
        self.assertEqual(st, 201, issued)
        token = issued["token"]
        before = len(self.fake_requests())
        result, events = self.run_job(JOB_OK, token)
        reqs = self.fake_requests()[before:]
        EVIDENCE["jobs"][JOB_OK]["fake_provider_steps"] = [r["step"] for r in reqs]
        self.assertTrue(reqs, "OpenCode never reached the provider through the proxy")
        for r in reqs:
            self.assertEqual((r["path"], r["model"], r["auth_is_synthetic_key"]), ("/openai/v1/chat/completions", UPSTREAM_MODEL, True), r)
            self.assertEqual(r["contract"], CONTRACT, r)
            self.assertLessEqual(set(r["body_keys"]), UPSTREAM_KEYS, r["body_keys"])
            self.assertEqual(r["header_names"], UPSTREAM_HEADERS, "OpenCode's session headers are not relayed")
        steps = [r["step"] for r in reqs]
        self.assertEqual(steps, ["tool_read", "tool_write", "tool_write_outside", "final"])
        write_req = next(r for r in reqs if r["step"] == "tool_write")
        self.assertTrue(write_req["read_result_has_file_content"], "the read tool ran in the sandbox and its result came back through the proxy")
        self.assertEqual(write_req["roles"][-2:], ["assistant", "tool"])
        # the evidence-directory write was allowed (its tool result is no denial); a write elsewhere under /work was denied
        outside_req = next(r for r in reqs if r["step"] == "tool_write_outside")
        self.assertFalse(outside_req["last_tool_result_is_denial"], "write to $PRQA_EVIDENCE_DIR/qa-result.json is permitted")
        self.assertTrue(reqs[-1]["step"] == "final" and reqs[-1]["last_tool_result_is_denial"], "write outside the evidence directory is denied")
        build = next(r for r in reqs if r["tools"])
        self.assertIn("read", build["tools"]); self.assertIn("write", build["tools"])
        EVIDENCE["jobs"][JOB_OK]["opencode_tools"] = build["tools"]; EVIDENCE["jobs"][JOB_OK]["tool_choice"] = build["tool_choice"]
        EVIDENCE["jobs"][JOB_OK]["tool_param_names"] = build["tool_param_names"]
        # the result OpenCode's write tool produced is what the real entrypoint returned, and it validates
        self.assertEqual((result.get("job_id"), result.get("repository")), (JOB_OK, REPO), result.get("summary"))
        self.assertIn("written by OpenCode's write tool", result["summary"]["executive_summary"])
        check = validate_result(result, SchemaStore(str(ROOT / "schemas")))
        self.assertTrue(check["valid"], check)
        EVIDENCE["jobs"][JOB_OK]["result_valid"] = check["valid"]
        # the token counted these requests; the launcher-equivalent revokes it at job end
        st, listing = self.admin("GET", "/admin/tokens")
        mine = [t for t in listing["tokens"] if t["job_id"] == JOB_OK]
        self.assertEqual(mine[0]["requests"], len(reqs))
        ledger = json.loads((self.budget_dir / "ledger.json").read_text())
        self.assertEqual(sum(1 for e in ledger["events"] if e["job_id"] == JOB_OK and e["state"] == "done"), len(reqs),
                         "every upstream request was reserved and settled in the provider budget ledger")
        EVIDENCE["jobs"][JOB_OK]["ledger_requests"] = len([e for e in ledger["events"] if e["job_id"] == JOB_OK])
        self.assertEqual(self.admin("DELETE", f"/admin/tokens/{JOB_OK}")[0], 200)
        _, o, e = real(["logs", PROXY])
        for secret in (PROVIDER_KEY, ADMIN, token, "FAKE HIDDEN REASONING", "print('hi')"):
            self.assertNotIn(secret, o + e)
        self.assertTrue(next(d for k, d in events if k == "sandbox_cleanup")["ok"])

    def test_2_revoked_job_token_is_rejected_and_nothing_reaches_the_provider(self):
        st, issued = self.admin("POST", "/admin/tokens", {"job_id": JOB_REVOKED, "model": UPSTREAM_MODEL, "ttl_s": 900})
        self.assertEqual(st, 201)
        self.assertEqual(self.admin("DELETE", f"/admin/tokens/{JOB_REVOKED}")[0], 200)
        before = len(self.fake_requests())
        _, o0, e0 = real(["logs", PROXY]); offset = len((o0 + e0).splitlines())
        result, _ = self.run_job(JOB_REVOKED, issued["token"])
        self.assertEqual(self.fake_requests()[before:], [], "a revoked token never reaches the provider")
        self.assertEqual(result["summary"]["overall_status"], "INCONCLUSIVE", "no result without model access")
        _, o, e = real(["logs", PROXY])
        new = "\n".join((o + e).splitlines()[offset:])
        self.assertIn("invalid, expired or revoked job token", new)
        EVIDENCE["jobs"][JOB_REVOKED]["proxy_denials"] = new.count("invalid, expired or revoked job token")

    def test_3_missing_job_token_stops_before_opencode(self):
        before = len(self.fake_requests())
        with self.assertRaises(RuntimeError) as ctx:
            self.run_job(JOB_NOTOKEN, None)
        self.assertIn("exited 67", str(ctx.exception))
        self.assertEqual(self.fake_requests()[before:], [])

    def test_4_every_docker_operation_went_through_the_policy_proxy_and_was_allowed(self):
        rows = [l.split(" decision ", 1)[1].split()[:3] for l in self.policy_log.read_text().splitlines() if " decision " in l]
        self.assertTrue(any(r[0] == "allow" and r[1] == "POST" and r[2].endswith("/containers/create") for r in rows), rows[-5:])
        self.assertEqual([r for r in rows if r[0] != "allow"], [], "the production plan needs no denied operation")
        EVIDENCE["policy_proxy_decisions"] = {"allow": sum(r[0] == "allow" for r in rows), "deny": sum(r[0] != "allow" for r in rows)}

if __name__ == "__main__":
    unittest.main()
