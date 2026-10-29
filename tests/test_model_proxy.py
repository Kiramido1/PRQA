"""Stage 4 — model egress proxy against a local fake upstream on loopback. No real provider, no network.

Run: cd "<project>" && PYTHONPATH=worker python3 -m unittest tests.test_model_proxy -v
"""
from __future__ import annotations

import http.client
import io
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))

import model_proxy as mp  # noqa: E402

PROVIDER_KEY = "sk-or-v1-SYNTHETICPROVIDERKEY0000000000000000000000000000"
ADMIN = "admin-token-SYNTHETIC-0123456789abcdef"
MODEL = "z-ai/glm-5.3-flash:free"
JOB = "20261022-abc123def456"


class FakeUpstream(BaseHTTPRequestHandler):
    received: list = []
    behaviour: dict = {}

    def log_message(self, *a):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        FakeUpstream.received.append({"method": self.command, "path": self.path, "headers": dict(self.headers.items()), "body": body})
        mode = FakeUpstream.behaviour.get("mode", "ok")
        if mode == "sleep":
            time.sleep(FakeUpstream.behaviour.get("seconds", 2))
        if mode == "error":
            self.send_response(500); self.send_header("Content-Length", "0"); self.end_headers(); return
        if mode == "status":
            data = json.dumps(FakeUpstream.behaviour.get("body", {})).encode()
            self.send_response(FakeUpstream.behaviour["status"]); self.send_header("Content-Length", str(len(data)))
            for k, v in (FakeUpstream.behaviour.get("headers") or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data); return
        if mode == "big":
            data = b"x" * FakeUpstream.behaviour.get("bytes", 100000)
        else:
            data = json.dumps({"id": "gen-1", "model": MODEL, "choices": [{"message": {"role": "assistant", "content": "SYNTHETIC COMPLETION"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.send_header("X-Request-Id", "req-synthetic-1")
        self.send_header("Set-Cookie", "upstream=secret"); self.end_headers(); self.wfile.write(data)

    do_POST = do_GET = _handle


class UnixHTTPConnection(http.client.HTTPConnection):
    """Test client for the admin listener, which exists only as a unix socket."""

    def __init__(self, path, timeout=10):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class ModelProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FakeUpstream.received = []; FakeUpstream.behaviour = {}
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream); cls.upstream.daemon_threads = True
        cls.upstream.handle_error = lambda *a: None          # the proxy stops reading at its size limit: broken pipes are expected
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.cfg = mp.ProxyConfig(upstream_origin=f"http://127.0.0.1:{cls.upstream.server_port}", upstream_prefix="/api/v1", allowed_model=MODEL,
                                 provider_key=PROVIDER_KEY, admin_token=ADMIN, max_request_bytes=2048, max_response_bytes=50000,
                                 upstream_timeout_s=1.0, max_concurrent_per_job=1, max_requests_per_job=5)
        cls.tmp = tempfile.mkdtemp(prefix="prqa-mp-")
        cls.admin_sock = os.path.join(cls.tmp, "admin.sock")
        cls.server, cls.admin = mp.make_servers(cls.cfg, "127.0.0.1", 0, cls.admin_sock)
        cls.handler, cls.admin_handler = cls.server.handler_class, cls.admin.handler_class
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        threading.Thread(target=cls.admin.serve_forever, daemon=True).start()
        cls.port = cls.server.server_port
        cls.logbuf = io.StringIO()
        h = logging.StreamHandler(cls.logbuf); h.setLevel(logging.DEBUG); mp.log.addHandler(h); mp.log.setLevel(logging.DEBUG)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.admin.shutdown(); cls.admin.server_close()
        cls.upstream.shutdown(); cls.upstream.server_close()
        import shutil; shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        FakeUpstream.received = []; FakeUpstream.behaviour = {}
        self.use_tokens(mp.TokenStore()); stats = mp.Stats()
        self.handler.stats = self.admin_handler.stats = stats

    def use_tokens(self, store):
        self.handler.tokens = self.admin_handler.tokens = store       # one store behind both listeners, as in production

    def req(self, method, path, body=None, token=None, headers=None, raw_target=False, admin=False):
        conn = UnixHTTPConnection(self.admin_sock) if admin else http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = "Bearer " + token
        h.update(headers or {})
        if data:
            h["Content-Length"] = str(len(data))
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse(); raw = resp.read(); hdrs = dict(resp.getheaders()); conn.close()
        return resp.status, raw, hdrs

    def issue(self, job=JOB, model=MODEL, ttl=600):
        st, raw, _ = self.req("POST", "/admin/tokens", {"job_id": job, "model": model, "ttl_s": ttl}, token=ADMIN, admin=True)
        self.assertEqual(st, 201, raw); return json.loads(raw)["token"]

    def test_successful_forward_injects_credential_and_strips_headers(self):
        tok = self.issue()
        st, raw, hdrs = self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}, token=tok,
                                 headers={"Cookie": "a=b", "HTTP-Referer": "https://evil", "X-Title": "evil"})
        self.assertEqual(st, 200); self.assertIn(b"SYNTHETIC COMPLETION", raw)
        up = FakeUpstream.received[-1]
        self.assertEqual(up["path"], "/api/v1/chat/completions")
        self.assertEqual(up["headers"]["Authorization"], "Bearer " + PROVIDER_KEY)      # provider key injected
        self.assertEqual(up["headers"]["X-Title"], "PR QA Pilot"); self.assertNotIn("Cookie", up["headers"]); self.assertNotIn("HTTP-Referer", up["headers"])
        self.assertNotIn("Set-Cookie", hdrs)
        self.assertNotIn(tok, json.dumps(up["headers"]))                                 # job token never forwarded
        log = self.logbuf.getvalue()
        for secret in (PROVIDER_KEY, tok, ADMIN, "SYNTHETIC COMPLETION", '"content": "hi"'):
            self.assertNotIn(secret, log)
        self.assertNotIn(PROVIDER_KEY, json.dumps(self.handler.stats.log)); self.assertEqual(self.handler.stats.log[-1]["upstream_request_id"], "req-synthetic-1")

    def test_rejections(self):
        tok = self.issue()
        body = {"model": MODEL, "messages": []}
        self.assertEqual(self.req("POST", "/v1/chat/completions", body)[0], 401)                                   # no token
        self.assertEqual(self.req("POST", "/v1/chat/completions", body, token="prqa_mt_forged")[0], 401)           # foreign token
        self.assertEqual(self.req("POST", "/v1/chat/completions", {"model": "other/model", "messages": []}, token=tok)[0], 403)
        self.assertEqual(self.req("POST", "/v1/embeddings", body, token=tok)[0], 404)
        self.assertEqual(self.req("GET", "/v1/models", token=tok)[0], 404, "listing disabled by default")
        self.assertEqual(self.req("POST", "http://evil.example/v1/chat/completions", body, token=tok, raw_target=True)[0], 400)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("CONNECT", "evil.example:443"); resp = conn.getresponse(); self.assertEqual(resp.status, 405); conn.close()
        self.assertEqual(self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": [{"content": "x" * 3000}]}, token=tok)[0], 413)
        self.assertEqual(self.req("POST", "/admin/tokens", {"job_id": JOB, "model": MODEL}, token="wrong", admin=True)[0], 401)
        self.assertEqual(self.req("POST", "/admin/tokens", {"job_id": JOB, "model": "other"}, token=ADMIN, admin=True)[0], 422)
        self.assertEqual(FakeUpstream.received, [], "no rejected request reached the upstream")

    def test_expiry_and_revocation(self):
        clock = {"t": 1000.0}
        self.use_tokens(mp.TokenStore(clock=lambda: clock["t"]))
        tok = self.issue(ttl=60)
        self.assertEqual(self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok)[0], 200)
        clock["t"] += 61
        self.assertEqual(self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok)[0], 401, "expired")
        tok2 = self.issue(ttl=600)
        st, raw, _ = self.req("DELETE", f"/admin/tokens/{JOB}", token=ADMIN, admin=True)
        self.assertEqual((st, json.loads(raw)["revoked"]), (200, True))
        self.assertEqual(self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok2)[0], 401, "revoked")
        self.assertEqual(self.req("DELETE", f"/admin/tokens/{JOB}", token=ADMIN, admin=True)[0], 404)
        self.assertEqual(self.req("DELETE", f"/admin/tokens/{JOB}", token="wrong", admin=True)[0], 401)
        self.issue(ttl=600); self.issue(ttl=600)                         # re-issue replaces the previous token
        self.assertEqual(len(self.handler.tokens.active()), 1)

    def test_limits_timeouts_and_upstream_failures(self):
        tok = self.issue()
        FakeUpstream.behaviour = {"mode": "sleep", "seconds": 2}
        st, raw, _ = self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok)
        self.assertEqual(st, 502); self.assertIn("timed out", raw.decode())
        FakeUpstream.behaviour = {"mode": "error"}
        st, raw, _ = self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok)
        self.assertEqual(st, 500, "upstream status is relayed")
        self.assertEqual(json.loads(raw)["error"]["type"], "proxy_upstream", "upstream body replaced by a sanitized proxy error")
        FakeUpstream.behaviour = {"mode": "big", "bytes": 200000}
        st, raw, _ = self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok)
        self.assertEqual(st, 502); self.assertIn("size limit", raw.decode())                      # bounded, never relayed
        st, raw, _ = self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": [], "stream": True}, token=tok)
        # upstream is never streamed (locked contract): an oversized reply is refused for stream=true clients too
        self.assertEqual(st, 502); self.assertIn("size limit", raw.decode()); self.assertLessEqual(len(raw), 1024, "nothing of it relayed")
        FakeUpstream.behaviour = {}
        for _ in range(2):
            self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok)
        st, raw, _ = self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok)
        self.assertEqual(st, 429, "request budget (5) exhausted"); self.assertIn("budget", raw.decode())

    def test_a_rejected_generation_is_relayed_as_retryable_and_only_its_error_code_is_kept(self):
        tok = self.issue()
        echo = "SYSTEM PROMPT ECHO org_123 secret-ish"
        FakeUpstream.behaviour = {"mode": "status", "status": 400,
                                  "body": {"error": {"message": f"Tool call validation failed: {echo}", "type": "invalid_request_error",
                                                     "code": "tool_use_failed", "failed_generation": echo}}}
        with self.assertLogs("prqa.model-proxy", level="ERROR") as logs:
            st, raw, _ = self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok)
        self.assertEqual(st, 503, "a bad generation is retryable for the client")
        self.assertNotIn(b"ECHO", raw)
        self.assertIn("tool_use_failed", "\n".join(logs.output))
        self.assertNotIn("ECHO", "\n".join(logs.output))
        FakeUpstream.behaviour = {"mode": "status", "status": 400, "body": {"error": {"message": "bad", "code": "invalid_request_error"}}}
        st, _, _ = self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok)
        self.assertEqual(st, 400, "any other 400 is still relayed as a client error (no retry loop)")
        FakeUpstream.behaviour = {"mode": "status", "status": 400, "body": {"error": {"code": "Not A Code <script>"}}}
        with self.assertLogs("prqa.model-proxy", level="ERROR") as logs:
            st, _, _ = self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok)
        self.assertEqual(st, 400)
        self.assertNotIn("script", "\n".join(logs.output))
        FakeUpstream.behaviour = {}
        self.assertEqual(mp.upstream_error_code(b"not json"), "")

    def test_reasoning_none_sends_no_reasoning_parameters(self):
        import dataclasses
        payload = {"model": MODEL, "messages": [{"role": "user", "content": "x"}]}
        self.assertIn("reasoning_effort", mp.build_upstream_body(payload, self.cfg))
        body = mp.build_upstream_body(payload, dataclasses.replace(self.cfg, reasoning_effort="none"))
        self.assertNotIn("reasoning_effort", body); self.assertNotIn("include_reasoning", body)
        self.assertEqual((body["stream"], body["temperature"]), (False, self.cfg.temperature))

    def test_long_tool_outputs_are_clipped_before_the_upstream(self):
        big = "A" * 2300 + "MIDDLE" + "Z" * 1200
        payload = {"model": MODEL, "messages": [{"role": "user", "content": "U" * 5000},
                                                {"role": "assistant", "content": None, "tool_calls": [
                                                    {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
                                                {"role": "tool", "tool_call_id": "c1", "content": big}]}
        sent = mp.build_upstream_body(payload, self.cfg)["messages"]
        self.assertEqual(sent[0]["content"], "U" * 5000, "only tool results are clipped")
        tool = sent[2]["content"]
        self.assertLess(len(tool), mp.TOOL_OUTPUT_MAX_CHARS + 300)
        self.assertNotIn("MIDDLE", tool); self.assertIn("cut by the PRQA model proxy", tool)
        self.assertTrue(tool.startswith("A" * 100) and tool.endswith("Z" * 100))
        self.assertEqual(sent[2]["tool_call_id"], "c1")
        self.assertEqual(payload["messages"][2]["content"], big, "the client payload itself is not mutated")
        self.assertEqual(mp.clip_tool_output("short"), "short")

    def test_a_spent_quota_429_is_final_but_a_busy_minute_stays_retryable(self):
        tok = self.issue()
        body = {"model": MODEL, "messages": [{"role": "user", "content": "q"}]}
        FakeUpstream.behaviour = {"mode": "status", "status": 429, "headers": {"Retry-After": "86293"},
                                  "body": {"error": {"type": "FreeUsageLimitError", "message": "account text"}}}
        st, raw, _ = self.req("POST", "/v1/chat/completions", body, token=tok)
        self.assertEqual(st, 400); self.assertIn(b"provider_quota_exhausted", raw); self.assertNotIn(b"account text", raw)
        FakeUpstream.behaviour = {"mode": "status", "status": 429, "headers": {"Retry-After": "20"}, "body": {"error": {}}}
        st, _, _ = self.req("POST", "/v1/chat/completions", dict(body, messages=[{"role": "user", "content": "r"}]), token=tok)
        self.assertEqual(st, 429, "a short wait is a rate limit the client may retry")
        FakeUpstream.behaviour = {}

    def test_a_turn_rejected_too_often_ends_with_a_final_400(self):
        tok = self.issue()
        FakeUpstream.behaviour = {"mode": "status", "status": 400, "body": {"error": {"code": "tool_use_failed"}}}
        body = {"model": MODEL, "messages": [{"role": "user", "content": "same turn"}]}
        codes = [self.req("POST", "/v1/chat/completions", body, token=tok)[0] for _ in range(mp.RETRY_MAX_REPEATS + 1)]
        FakeUpstream.behaviour = {}
        self.assertEqual(codes[:-1], [503] * mp.RETRY_MAX_REPEATS, "retryable while the repeat count is below the cap")
        self.assertEqual(codes[-1], 400, "then final, so the client session ends instead of looping on its budget")

    def test_identical_retries_sample_more_freely_and_new_turns_reset(self):
        tok = self.issue()
        msgs = {"model": MODEL, "messages": [{"role": "user", "content": "x"}]}
        temps = []
        for body in (msgs, msgs, msgs, msgs, dict(msgs, messages=[{"role": "user", "content": "y"}])):
            FakeUpstream.received = []
            self.req("POST", "/v1/chat/completions", body, token=tok)
            temps.append(json.loads(FakeUpstream.received[-1]["body"])["temperature"])
        base = self.cfg.temperature
        self.assertEqual(temps[0], base)
        self.assertGreater(temps[1], base)
        self.assertGreater(temps[2], temps[1])
        self.assertLessEqual(max(temps), mp.RETRY_TEMPERATURE_MAX)
        self.assertEqual(temps[4], base, "a new turn is back at the locked temperature")

    def test_per_token_request_cap_stops_before_the_upstream(self):
        st, raw, _ = self.req("POST", "/admin/tokens", {"job_id": JOB, "model": MODEL, "max_requests": 1}, token=ADMIN, admin=True)
        self.assertEqual(st, 201, raw); body = json.loads(raw); tok = body["token"]; self.assertEqual(body["max_requests"], 1)
        st, _, _ = self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok)
        self.assertEqual((st, len(FakeUpstream.received)), (200, 1))
        st, raw, _ = self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok)
        self.assertEqual(st, 429); self.assertIn("budget", raw.decode()); self.assertEqual(len(FakeUpstream.received), 1, "never reached upstream")
        # a failed first attempt also consumes the single request: there is no retry path through the proxy
        FakeUpstream.received = []; FakeUpstream.behaviour = {"mode": "error"}
        st, raw, _ = self.req("POST", "/admin/tokens", {"job_id": "job-capped-2", "model": MODEL, "max_requests": 1}, token=ADMIN, admin=True)
        tok2 = json.loads(raw)["token"]
        self.assertEqual(self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok2)[0], 500)
        FakeUpstream.behaviour = {}
        self.assertEqual(self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok2)[0], 429)
        self.assertEqual(len(FakeUpstream.received), 1)
        st, raw, _ = self.req("GET", "/admin/tokens", token=ADMIN, admin=True)
        self.assertEqual({t["job_id"]: t["max_requests"] for t in json.loads(raw)["tokens"]}, {JOB: 1, "job-capped-2": 1})
        # the cap can only tighten the proxy-wide budget (5 here), never widen it, and is rejected when malformed
        for bad in (0, 6, -1, "1", True, 1.0):
            st, _, _ = self.req("POST", "/admin/tokens", {"job_id": "job-bad", "model": MODEL, "max_requests": bad}, token=ADMIN, admin=True)
            self.assertEqual(st, 422, bad)
        # a token issued without a cap keeps the proxy-wide budget (normal jobs unchanged)
        tok3 = self.issue(job="job-uncapped")
        for _ in range(5):
            self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok3)
        self.assertEqual(self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok3)[0], 429)

    def test_concurrency_limit_per_job(self):
        tok = self.issue()
        FakeUpstream.behaviour = {"mode": "sleep", "seconds": 0.5}
        results = []

        def go():
            results.append(self.req("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok)[0])
        t = [threading.Thread(target=go) for _ in range(2)]
        [x.start() for x in t]; [x.join() for x in t]
        self.assertEqual(sorted(results), [200, 429])

    def test_config_validation_fails_closed(self):
        with self.assertRaises(SystemExit):
            mp.ProxyConfig(upstream_origin="https://openrouter.ai/api/v1?x=1", allowed_model=MODEL, provider_key="k", admin_token=ADMIN).validate()
        with self.assertRaises(SystemExit):
            mp.ProxyConfig(upstream_origin="http://openrouter.ai", allowed_model=MODEL, provider_key="k", admin_token=ADMIN).validate()
        with self.assertRaises(SystemExit):
            mp.ProxyConfig(upstream_origin="https://openrouter.ai", allowed_model="", provider_key="k", admin_token=ADMIN).validate()
        with self.assertRaises(SystemExit):
            mp.ProxyConfig(upstream_origin="https://openrouter.ai", allowed_model=MODEL, provider_key="", admin_token=ADMIN).validate()
        with self.assertRaises(SystemExit):
            mp.ProxyConfig(upstream_origin="https://openrouter.ai", allowed_model=MODEL, provider_key="k", admin_token="short").validate()
        with self.assertRaises(SystemExit, msg="a real (https) upstream without the provider budget refuses to start"):
            mp.ProxyConfig(upstream_origin="https://openrouter.ai", allowed_model=MODEL, provider_key="k", admin_token=ADMIN).validate()
        budget_dir = tempfile.mkdtemp(prefix="prqa-budget-"); os.chmod(budget_dir, 0o700)
        budget = mp.pb.BudgetConfig(directory=budget_dir, daily_requests=5, daily_tokens=40000, job_tokens=24000, tokens_per_minute=8000, max_in_flight=1)
        mp.ProxyConfig(upstream_origin="https://openrouter.ai", allowed_model=MODEL, provider_key="k", admin_token=ADMIN, budget=budget).validate()


if __name__ == "__main__":
    unittest.main()
