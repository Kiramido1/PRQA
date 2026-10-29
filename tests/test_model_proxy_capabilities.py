"""Model proxy credential capabilities — offline (loopback fake upstream, temporary ledger, synthetic credentials).

Four distinct credentials, each limited by the SERVER (not by where it is stored):
  * admin  (proxy only)            list / issue / revoke any token on the unix admin socket
  * mint   (the launcher)          issue a job token, revoke only the tokens it issued — no listing, no reserved ids
  * job    (a sandbox)             inference only
  * prompt (n8n workflow 03)       inference only, no tools, one upstream attempt per call, shared ledger

    PYTHONPATH=worker python3 -m unittest tests.test_model_proxy_capabilities -v
"""
from __future__ import annotations

import http.client
import io
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "proxy"), str(ROOT / "worker")]

import model_proxy as mp  # noqa: E402
import provider_budget as pb  # noqa: E402
from prqa_worker.launcher_service import LauncherConfig, LauncherError  # noqa: E402

MODEL = "openai/gpt-oss-120b"
PROVIDER_KEY = "gsk_SYNTHETIC_capabilities_key_000000000000"
ADMIN = "admin-token-SYNTHETIC-capabilities-0123456789"
MINT = "mint-token-SYNTHETIC-capabilities-0123456789abcd"
PROMPT = "prompt-token-SYNTHETIC-capabilities-0123456789ab"
SECRET_PROMPT = "SYNTHETIC-PROMPT-BODY-must-not-be-logged"


class Upstream(BaseHTTPRequestHandler):
    received: list = []
    delay = 0.0
    arrived = threading.Event()
    refuse: int = 0          # when set: answer with this status (and a Retry-After) instead of a completion

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        Upstream.received.append(json.loads(self.rfile.read(n) or b"{}"))
        Upstream.arrived.set()
        time.sleep(Upstream.delay)
        if Upstream.refuse:
            data = b'{"error": {"code": "rate_limit_exceeded"}}'
            self.send_response(Upstream.refuse); self.send_header("Retry-After", "5"); self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        data = json.dumps({"id": "c1", "model": MODEL, "created": 1, "usage": {"prompt_tokens": 90, "completion_tokens": 10, "total_tokens": 100},
                           "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "# SYNTHETIC PLAN",
                                                                                         "reasoning": "HIDDEN"}}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)


def private_dir(parent=None) -> str:
    d = tempfile.mkdtemp(prefix="prqa-cap-", dir=parent); os.chmod(d, 0o700); return d


def budget(d, **kw):
    base = dict(directory=d, daily_requests=5, daily_tokens=40000, job_tokens=24000, tokens_per_minute=100000, max_in_flight=1)
    base.update(kw)
    return pb.BudgetConfig(**base)


class CapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream); cls.upstream.daemon_threads = True
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.tmp = private_dir()
        cls.logbuf = io.StringIO(); cls.loghandler = logging.StreamHandler(cls.logbuf)
        mp.log.addHandler(cls.loghandler); mp.log.setLevel(logging.INFO)

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown(); cls.upstream.server_close()
        mp.log.removeHandler(cls.loghandler)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        Upstream.received = []; Upstream.delay = 0.0; Upstream.arrived.clear()
        self.start()

    def start(self, **budget_kw):
        self.bdir = private_dir(self.tmp)
        self.budget_cfg = budget(self.bdir, **budget_kw)
        c = mp.ProxyConfig(upstream_origin=f"http://127.0.0.1:{self.upstream.server_port}", upstream_prefix="/openai/v1", allowed_model=MODEL,
                           provider_key=PROVIDER_KEY, admin_token=ADMIN, mint_token=MINT, prompt_token=PROMPT,
                           budget=self.budget_cfg, budget_max_wait_s=0)
        self.sock = os.path.join(self.tmp, f"admin-{len(os.listdir(self.tmp))}.sock")
        self.inf, self.adm = mp.make_servers(c, "127.0.0.1", 0, self.sock)
        for s in (self.inf, self.adm):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.addCleanup(lambda srvs=(self.inf, self.adm): [s.shutdown() or s.server_close() for s in srvs])

    # ------------------------------------------------------------------------------------------- clients --
    def admin(self, method, path, token, body=None):
        conn = http.client.HTTPConnection("localhost", timeout=10)
        conn.sock = socket.socket(socket.AF_UNIX); conn.sock.connect(self.sock)
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        r = conn.getresponse(); raw = r.read(); conn.close()
        return r.status, (json.loads(raw) if raw else {})

    def issue(self, token, job, **extra):
        return self.admin("POST", "/admin/tokens", token, {"job_id": job, "model": MODEL, "ttl_s": 600, **extra})

    def chat(self, token, headers=None, **extra):
        body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": SECRET_PROMPT}], **extra}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.inf.server_port, timeout=20)
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", **(headers or {})})
        r = conn.getresponse(); raw = r.read(); conn.close()
        return r.status, json.loads(raw)

    def jobs(self):
        return pb.Ledger(self.budget_cfg).snapshot()["jobs"]

    # ------------------------------------------------------------------------------------------- mint --
    def test_mint_issues_a_job_token_that_works_for_inference_only(self):
        st, body = self.issue(MINT, "20261027-mint00000001")
        self.assertEqual(st, 201)
        self.assertEqual(self.chat(body["token"])[0], 200)
        self.assertEqual(self.admin("GET", "/admin/tokens", body["token"])[0], 401, "a job token is never administrative")

    def test_mint_cannot_list_tokens(self):
        self.issue(ADMIN, "20261027-admin0000001")
        st, body = self.admin("GET", "/admin/tokens", MINT)
        self.assertEqual(st, 403)
        self.assertNotIn("tokens", body)
        self.assertEqual(self.admin("GET", "/admin/tokens", ADMIN)[0], 200)

    def test_mint_cannot_revoke_or_replace_another_issuers_token(self):
        st, admin_tok = self.issue(ADMIN, "20261027-shared000001")
        self.assertEqual(st, 201)
        self.assertEqual(self.admin("DELETE", "/admin/tokens/20261027-shared000001", MINT)[0], 404)
        self.assertEqual(self.issue(MINT, "20261027-shared000001")[0], 409)
        self.assertEqual(self.chat(admin_tok["token"])[0], 200, "the admin-issued token is untouched")
        self.assertEqual(self.admin("DELETE", "/admin/tokens/20261027-shared000001", ADMIN)[0], 200)

    def test_mint_revokes_its_own_token_and_admin_can_revoke_any(self):
        _, own = self.issue(MINT, "20261027-mintown00001")
        self.assertEqual(self.admin("DELETE", "/admin/tokens/20261027-mintown00001", MINT)[0], 200)
        self.assertEqual(self.chat(own["token"])[0], 401)
        _, other = self.issue(MINT, "20261027-mintown00002")
        self.assertEqual(self.admin("DELETE", "/admin/tokens/20261027-mintown00002", ADMIN)[0], 200)
        self.assertEqual(self.chat(other["token"])[0], 401)

    def test_prompt_job_ids_are_reserved(self):
        for token in (MINT, ADMIN):
            self.assertEqual(self.issue(token, "prompt-builder")[0], 422)

    def test_mint_caps_still_only_tighten(self):
        self.assertEqual(self.issue(MINT, "20261027-mintcap0001", max_completion_tokens=999999)[0], 422)
        self.assertEqual(self.issue(MINT, "20261027-mintcap0002", max_requests=0)[0], 422)

    def test_administrative_credentials_are_refused_for_inference(self):
        for token in (MINT, ADMIN):
            st, body = self.chat(token)
            self.assertEqual(st, 403)
            self.assertIn("administrative credential", body["error"]["message"])
        self.assertEqual(Upstream.received, [])

    def test_inference_credentials_are_refused_on_the_admin_socket(self):
        _, job = self.issue(MINT, "20261027-mintjob0001")
        for token in (PROMPT, job["token"], "garbage-token-0000000000000000000000"):
            self.assertEqual(self.issue(token, "20261027-escalate0001")[0], 401)
            self.assertEqual(self.admin("GET", "/admin/tokens", token)[0], 401)
            self.assertEqual(self.admin("DELETE", "/admin/tokens/20261027-mintjob0001", token)[0], 401)

    def test_audit_log_names_principal_never_the_token(self):
        _, job = self.issue(MINT, "20261027-audit0000001")
        self.chat(job["token"]); self.chat(PROMPT, headers={"X-PRQA-Job-Id": "audit-1"})
        self.admin("DELETE", "/admin/tokens/20261027-audit0000001", MINT)
        log = self.logbuf.getvalue()
        self.assertIn("token issued for job 20261027-audit0000001 by mint", log)
        self.assertIn("token revoked for job 20261027-audit0000001 by mint", log)
        for secret in (ADMIN, MINT, PROMPT, job["token"], PROVIDER_KEY, SECRET_PROMPT, "HIDDEN"):
            self.assertNotIn(secret, log)

    # ----------------------------------------------------------------------------------------- prompt --
    def test_prompt_credential_forwards_once_under_the_locked_contract_and_ledger(self):
        st, doc = self.chat(PROMPT, headers={"X-PRQA-Job-Id": "20261027-c01"}, max_completion_tokens=99999, temperature=1.5, stream=False)
        self.assertEqual(st, 200)
        self.assertEqual(doc["choices"][0]["message"], {"role": "assistant", "content": "# SYNTHETIC PLAN"}, "no reasoning relayed")
        self.assertEqual(len(Upstream.received), 1)
        up = Upstream.received[0]
        self.assertEqual((up["max_completion_tokens"], up["temperature"], up["stream"], up["reasoning_effort"], up["include_reasoning"]),
                         (3000, 0.2, False, "low", False))
        self.assertEqual(self.jobs(), {"prompt-20261027-c01": 100})

    def test_a_refused_request_releases_its_reservation(self):
        for status in (429, 413):
            Upstream.refuse = status
            try:
                st, _ = self.chat(PROMPT, headers={"X-PRQA-Job-Id": f"20261028-refused{status}"})
            finally:
                Upstream.refuse = 0
            self.assertEqual(st, status)
            self.assertEqual(self.jobs()[f"prompt-20261028-refused{status}"], 0, "nothing was processed upstream: nothing is counted")

    def test_prompt_job_id_falls_back_for_a_malformed_reference(self):
        self.chat(PROMPT, headers={"X-PRQA-Job-Id": "../../etc"})
        self.assertEqual(list(self.jobs()), ["prompt-builder"])

    def test_prompt_credential_cannot_use_tools(self):
        for extra in ({"tools": [{"type": "function", "function": {"name": "read", "parameters": {}}}]}, {"tool_choice": "auto"}):
            st, body = self.chat(PROMPT, **extra)
            self.assertEqual(st, 403); self.assertIn("tools are not permitted", body["error"]["message"])
        self.assertEqual(Upstream.received, [])

    def test_prompt_credential_is_bound_to_the_configured_model(self):
        body = json.dumps({"model": "other/model", "messages": [{"role": "user", "content": "x"}]}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.inf.server_port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=body, headers={"Authorization": "Bearer " + PROMPT, "Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 403); conn.close()
        self.assertEqual(Upstream.received, [])

    def test_exhausted_daily_budget_refuses_the_prompt_path_before_the_upstream(self):
        self.start(daily_requests=1)
        self.assertEqual(self.chat(PROMPT)[0], 200)
        st, body = self.chat(PROMPT)
        self.assertEqual(st, 429); self.assertIn("provider budget", body["error"]["message"])
        self.assertEqual(len(Upstream.received), 1)

    def test_prompt_and_job_paths_share_one_in_flight_slot(self):
        _, job = self.issue(MINT, "20261027-inflight0001")
        Upstream.delay = 1.0
        first: list = []
        t = threading.Thread(target=lambda: first.append(self.chat(job["token"])))
        t.start()
        self.assertTrue(Upstream.arrived.wait(5))
        st, body = self.chat(PROMPT)
        t.join(10)
        self.assertEqual(first[0][0], 200)
        self.assertEqual(st, 429); self.assertIn("in flight", body["error"]["message"])
        self.assertEqual(len(Upstream.received), 1)

    def test_corrupt_ledger_fails_closed_for_the_prompt_path(self):
        Path(self.bdir, pb.LEDGER_NAME).write_text("{not json", encoding="utf-8")
        self.assertEqual(self.chat(PROMPT)[0], 503)
        self.assertEqual(Upstream.received, [])

    # ----------------------------------------------------------------------------------------- config --
    def test_credentials_must_be_distinct_and_strong(self):
        base = dict(upstream_origin="https://api.groq.com", allowed_model=MODEL, provider_key=PROVIDER_KEY, admin_token=ADMIN,
                    budget=budget(self.bdir))
        mp.ProxyConfig(**base, mint_token=MINT, prompt_token=PROMPT).validate()
        for bad in ({"mint_token": ADMIN}, {"prompt_token": ADMIN}, {"mint_token": MINT, "prompt_token": MINT},
                    {"mint_token": "short"}, {"prompt_token": PROVIDER_KEY}, {"mint_token": "x" * 31}):
            with self.subTest(bad=list(bad)), self.assertRaises(SystemExit):
                mp.ProxyConfig(**base, **bad).validate()

    def test_launcher_refuses_the_full_admin_credential(self):
        env = {"PRQA_LAUNCHER_TOKEN": "l" * 40, "PRQA_MODEL_PROXY_ADMIN_TOKEN": ADMIN}
        with self.assertRaises(LauncherError):
            LauncherConfig.from_env(env)
        cfg = LauncherConfig.from_env({"PRQA_LAUNCHER_TOKEN": "l" * 40, "PRQA_MODEL_PROXY_MINT_TOKEN": MINT})
        self.assertEqual(cfg.model_proxy_mint_token, MINT)


if __name__ == "__main__":
    unittest.main()
