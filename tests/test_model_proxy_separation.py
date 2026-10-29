"""Model proxy: ADMIN and INFERENCE interfaces are separate (offline; synthetic credentials, loopback fake upstream).

The admin API (/admin/tokens) exists only on a unix socket; the inference listener (TCP) serves only
POST /v1/chat/completions with per-job tokens. These tests drive the real listeners built by
proxy/model_proxy.py::make_servers — the same factory `main()` uses — and check every denial by its explicit policy
body (type proxy_policy + interface), never by a bare 404 or a timeout. A loopback "decoy" listener stands in for
n8n / Docker / arbitrary services and must never receive a request.

    PYTHONPATH=worker python3 -m unittest tests.test_model_proxy_separation -v
"""
from __future__ import annotations

import http.client
import io
import json
import logging
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "proxy"), str(ROOT / "worker")]

import model_proxy as mp  # noqa: E402
from prqa_worker.launcher_service import LauncherConfig, LauncherError, ModelProxyClient  # noqa: E402

PROVIDER_KEY = "sk-or-v1-SYNTHETICSEPARATIONKEY000000000000000000000000000"
ADMIN = "admin-token-SYNTHETIC-separation-0123456789"
MINT = "mint-token-SYNTHETIC-separation-0123456789abcdef"     # the launcher's restricted credential
MODEL = "z-ai/glm-5.3-flash:free"
JOB = "20261027-sep000000001"
SECRET_PROMPT = "SYNTHETIC-PROMPT-BODY-must-not-be-logged"
HIDDEN_REASONING = "SYNTHETIC-HIDDEN-REASONING-must-not-be-logged"
PROVIDER_DETAIL = "provider-internal-detail-acct-12345"
METHODS = ("GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS", "TRACE", "PROPFIND", "HEAD")
ADMIN_SPELLINGS = ("/admin/tokens", f"/admin/tokens/{JOB}", "/ADMIN/tokens", "/Admin/Tokens", "/%61dmin/tokens", "/%2561dmin/tokens",
                   "/admin%2Ftokens", "/v1/../admin/tokens", "/v1/chat/completions/../../admin/tokens", "//admin/tokens",
                   "/./admin/tokens", "/admin/tokens/", "/admin/tokens?job_id=x", "/admin/tokens#x", "/admin;/tokens",
                   "/v1/%2e%2e/admin/tokens", "/\\admin/tokens", "/admin/tokens%00", "/healthz/../admin/tokens")


class Upstream(BaseHTTPRequestHandler):
    received: list = []
    mode = "ok"

    def log_message(self, *a):
        pass

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        Upstream.received.append({"method": self.command, "path": self.path, "headers": dict(self.headers.items()), "body": body})
        status, ctype, data = 200, "application/json", json.dumps({
            "id": "gen-1", "model": MODEL, "choices": [{"message": {"role": "assistant", "content": "SYNTHETIC COMPLETION",
                                                                    "reasoning": HIDDEN_REASONING}}]}).encode()
        if Upstream.mode == "error":
            status, data = 500, json.dumps({"error": {"message": PROVIDER_DETAIL, "key_hint": PROVIDER_KEY[:12]}}).encode()
        elif Upstream.mode == "unauthorized":
            status, data = 401, json.dumps({"error": {"message": "invalid api key " + PROVIDER_DETAIL}}).encode()
        elif Upstream.mode == "not_json":
            ctype, data = "text/html", b"<html>" + PROVIDER_DETAIL.encode() + b"</html>"
        elif Upstream.mode == "no_choices":
            data = json.dumps({"detail": PROVIDER_DETAIL}).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Request-Id", "req-synthetic-sep"); self.send_header("Set-Cookie", "acct=" + PROVIDER_DETAIL)
        self.send_header("X-Provider-Account", PROVIDER_DETAIL)
        self.end_headers(); self.wfile.write(data)

    do_GET = do_POST = _handle


class Decoy(BaseHTTPRequestHandler):
    """Stands in for n8n, the Docker API or any other internal service: must never be reached."""
    hits: list = []

    def log_message(self, *a):
        pass

    def _handle(self):
        Decoy.hits.append((self.command, self.path)); self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()

    do_GET = do_POST = do_DELETE = do_PUT = _handle


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=10):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); self.sock.settimeout(self.timeout); self.sock.connect(self._path)


def raw_request(port, method, target, headers=None, body=b""):
    """Bytes on the wire exactly as written — http.client would normalise or refuse some of these targets."""
    h = {"Host": "127.0.0.1", "Connection": "close", **(headers or {})}
    if body or method in ("POST", "PUT", "PATCH"):
        h["Content-Length"] = str(len(body))
    head = f"{method} {target} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(head.encode("latin-1") + body)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    head_raw, _, payload = data.partition(b"\r\n\r\n")
    status = int(head_raw.split(b" ", 2)[1])
    return status, payload


class SeparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream); cls.upstream.daemon_threads = True
        cls.decoy = ThreadingHTTPServer(("127.0.0.1", 0), Decoy); cls.decoy.daemon_threads = True
        for srv in (cls.upstream, cls.decoy):
            srv.handle_error = lambda *a: None
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        cls.cfg = mp.ProxyConfig(upstream_origin=f"http://127.0.0.1:{cls.upstream.server_port}", upstream_prefix="/api/v1", allowed_model=MODEL,
                                 provider_key=PROVIDER_KEY, admin_token=ADMIN, mint_token=MINT, max_request_bytes=4096, max_response_bytes=4096,
                                 upstream_timeout_s=2.0, max_concurrent_per_job=2, max_requests_per_job=1000)
        cls.tmp = tempfile.mkdtemp(prefix="prqa-sep-")
        cls.sock = os.path.join(cls.tmp, "admin.sock")
        cls.inference, cls.admin = mp.make_servers(cls.cfg, "127.0.0.1", 0, cls.sock, admin_socket_gid=os.getgid())
        for srv in (cls.inference, cls.admin):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        cls.port = cls.inference.server_port
        cls.logbuf = io.StringIO()
        cls.loghandler = logging.StreamHandler(cls.logbuf); cls.loghandler.setLevel(logging.DEBUG)
        mp.log.addHandler(cls.loghandler); mp.log.setLevel(logging.DEBUG)
        cls.responses: list[bytes] = []

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.inference, cls.admin, cls.upstream, cls.decoy):
            srv.shutdown(); srv.server_close()
        mp.log.removeHandler(cls.loghandler)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        Upstream.received = []; Upstream.mode = "ok"; Decoy.hits = []

    # ----------------------------------------------------------------------------------------------- clients --
    def call(self, method, path, body=None, token=None, admin=False, headers=None):
        conn = UnixHTTPConnection(self.sock) if admin else http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        h = {"Content-Type": "application/json", **(headers or {})}
        if token:
            h["Authorization"] = "Bearer " + token
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse(); raw = resp.read(); hdrs = {k.lower(): v for k, v in resp.getheaders()}; conn.close()
        type(self).responses.append(raw + json.dumps(hdrs).encode())
        return resp.status, raw, hdrs

    def issue(self, job=JOB):
        st, raw, _ = self.call("POST", "/admin/tokens", {"job_id": job, "model": MODEL, "ttl_s": 600}, token=ADMIN, admin=True)
        self.assertEqual(st, 201, raw)
        return json.loads(raw)["token"]

    def chat(self, token, **extra):
        return self.call("POST", "/v1/chat/completions", {"model": MODEL, "messages": [{"role": "user", "content": SECRET_PROMPT}], **extra}, token=token)

    def assert_policy_denial(self, status, raw, expected_status, interface="inference", text=None):
        self.assertEqual(status, expected_status, raw[:300])
        err = json.loads(raw)["error"]
        self.assertEqual((err["type"], err["interface"]), ("proxy_policy", interface), err)   # an explicit decision, not a framework 404
        if text:
            self.assertIn(text, err["message"])

    # ------------------------------------------------------------------------------------------------- tests --
    def test_01_trusted_admin_client_performs_its_operations_on_the_unix_socket(self):
        tok = self.issue()
        self.assertTrue(tok.startswith("prqa_mt_"))
        st, raw, _ = self.call("GET", "/admin/tokens", token=ADMIN, admin=True)
        self.assertEqual(st, 200); self.assertEqual([t["job_id"] for t in json.loads(raw)["tokens"]], [JOB])
        st, raw, _ = self.call("DELETE", f"/admin/tokens/{JOB}", token=ADMIN, admin=True)
        self.assertEqual((st, json.loads(raw)["revoked"]), (200, True))
        st, raw, _ = self.call("GET", "/healthz", admin=True)
        self.assertEqual(json.loads(raw)["interface"], "admin")
        # the launcher's real client against the real socket
        cfg = LauncherConfig(worker_url="http://unused", launcher_token="x", launcher_id="l", model_base_url="http://prqa-model-proxy:8089/v1",
                             model_proxy_admin_url="unix://" + self.sock, model_proxy_mint_token=MINT)
        client = ModelProxyClient(cfg)
        tok2 = client.issue("20261027-launcher0001", MODEL, 600)
        self.assertEqual(self.chat(tok2)[0], 200)
        self.assertTrue(client.revoke("20261027-launcher0001"))
        self.assertEqual(self.chat(tok2)[0], 401)

    def test_02_admin_listener_is_a_unix_socket_only(self):
        st = os.lstat(self.sock)
        self.assertTrue(stat.S_ISSOCK(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o660)
        self.assertEqual(st.st_gid, os.getgid())
        self.assertEqual(self.admin.socket.family, socket.AF_UNIX)
        self.assertEqual(self.inference.socket.family, socket.AF_INET)
        self.assertEqual(self.inference.handler_class.role, "inference"); self.assertEqual(self.admin.handler_class.role, "admin")
        self.assertFalse(hasattr(mp, "make_admin_tcp_server"))
        # a TCP admin URL is refused by the launcher client before anything is sent
        for url in ("http://prqa-model-proxy:8089", "https://prqa-model-proxy:8089", "unix://relative.sock", ""):
            cfg = LauncherConfig(worker_url="http://unused", launcher_token="x", launcher_id="l", model_base_url="http://m/v1",
                                 model_proxy_admin_url=url, model_proxy_mint_token=MINT)
            with self.assertRaises(LauncherError):
                ModelProxyClient(cfg).issue(JOB, MODEL, 600)
        # a refusing socket path fails closed (no fallback to anything else)
        cfg = LauncherConfig(worker_url="http://unused", launcher_token="x", launcher_id="l", model_base_url="http://m/v1",
                             model_proxy_admin_url="unix://" + os.path.join(self.tmp, "absent.sock"), model_proxy_mint_token=MINT)
        with self.assertRaises(LauncherError):
            ModelProxyClient(cfg).issue(JOB, MODEL, 600)
        # the admin factory refuses to replace a non-socket file
        regular = os.path.join(self.tmp, "regular"); Path(regular).write_text("x")
        with self.assertRaises(SystemExit):
            mp.make_admin_server(self.cfg, regular)

    def test_03_sandbox_client_can_use_inference_with_its_job_token(self):
        tok = self.issue()
        st, raw, hdrs = self.chat(tok)
        self.assertEqual(st, 200); self.assertIn(b"SYNTHETIC COMPLETION", raw)
        up = Upstream.received[-1]
        self.assertEqual((up["method"], up["path"]), ("POST", "/api/v1/chat/completions"))
        self.assertEqual(up["headers"]["Authorization"], "Bearer " + PROVIDER_KEY)   # injected by the proxy
        self.assertNotIn(tok, json.dumps(up["headers"]))
        self.assertNotIn("set-cookie", hdrs); self.assertNotIn("x-provider-account", hdrs)
        self.assertEqual(hdrs.get("x-request-id"), "req-synthetic-sep")
        st, raw, _ = self.call("GET", "/healthz")
        self.assertEqual(json.loads(raw), {"ok": True, "interface": "inference"})   # no upstream / model / config disclosure

    def test_04_no_admin_spelling_any_method_reaches_admin_on_the_inference_listener(self):
        before = {t["job_id"] for t in self.admin.handler_class.tokens.active()}
        results = {}
        for method in METHODS:
            for target in ADMIN_SPELLINGS:
                body = json.dumps({"job_id": "20261027-attacker0001", "model": MODEL}).encode() if method in ("POST", "PUT", "PATCH") else b""
                st, payload = raw_request(self.port, method, target, {"Authorization": "Bearer " + ADMIN, "Content-Type": "application/json"}, body)
                results[(method, target)] = st
                if method == "HEAD":
                    self.assertIn(st, (400, 403), (method, target))
                    continue
                err = json.loads(payload)["error"]
                self.assertEqual((err["type"], err["interface"]), ("proxy_policy", "inference"), (method, target, err))
                self.assertIn(st, (400, 403), (method, target))
        after = {t["job_id"] for t in self.admin.handler_class.tokens.active()}
        self.assertEqual(before, after, "no token was issued or revoked through the inference listener")
        self.assertEqual(Upstream.received, []); self.assertEqual(Decoy.hits, [])
        self.assertTrue(all(v == 403 for (m, t), v in results.items() if t in ("/admin/tokens", "/%61dmin/tokens", "/ADMIN/tokens")))

    def test_05_inference_credential_cannot_administer(self):
        tok = self.issue()
        st, raw, _ = self.call("POST", "/admin/tokens", {"job_id": "20261027-attacker0001", "model": MODEL, "ttl_s": 86400}, token=tok, admin=True)
        self.assert_policy_denial(st, raw, 401, "admin", "admin authentication failed")
        st, raw, _ = self.call("DELETE", f"/admin/tokens/{JOB}", token=tok, admin=True)
        self.assert_policy_denial(st, raw, 401, "admin")
        st, raw, _ = self.call("GET", "/admin/tokens", token=tok, admin=True)
        self.assert_policy_denial(st, raw, 401, "admin")
        self.assertEqual([t["job_id"] for t in self.admin.handler_class.tokens.active()], [JOB])
        # nor the reverse: the admin token is not an inference credential, and inference is not served on the admin socket
        st, raw, _ = self.chat(ADMIN)
        self.assert_policy_denial(st, raw, 403, "inference", "administrative credential")
        st, raw, _ = self.call("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, token=tok, admin=True)
        self.assert_policy_denial(st, raw, 404, "admin", "inference is not served")
        self.assertEqual(Upstream.received, [])

    def test_06_no_route_discloses_provider_credentials_or_configuration(self):
        tok = self.issue()
        for method, path in (("GET", "/healthz"), ("GET", "/config"), ("GET", "/env"), ("GET", "/v1/models"), ("GET", "/debug"),
                             ("GET", "/metrics"), ("GET", "/stats"), ("GET", "/proc/self/environ"), ("GET", "/run/secrets/prqa_model_provider_key")):
            st, raw, _ = self.call(method, path, token=tok)
            self.assertNotIn(PROVIDER_KEY.encode(), raw); self.assertNotIn(ADMIN.encode(), raw)
            self.assertNotIn(str(self.upstream.server_port).encode(), raw)
            if path != "/healthz":
                self.assert_policy_denial(st, raw, 404, "inference", "path not permitted")

    def test_07_unauthorized_clients_cannot_invoke_inference(self):
        cases = {"no token": None, "forged": "prqa_mt_forged", "provider key": PROVIDER_KEY, "empty bearer": ""}
        for name, token in cases.items():
            st, raw, _ = self.chat(token) if token is not None else self.call("POST", "/v1/chat/completions", {"model": MODEL, "messages": []})
            self.assert_policy_denial(st, raw, 401, "inference")
        st, raw, _ = self.call("POST", "/v1/chat/completions", {"model": MODEL, "messages": []}, headers={"Authorization": "Basic " + ADMIN})
        self.assert_policy_denial(st, raw, 401, "inference")
        tok = self.issue()
        st, raw, _ = self.call("POST", "/v1/chat/completions", {"model": "openai/gpt-oss-120b", "messages": []}, token=tok)
        self.assert_policy_denial(st, raw, 403, "inference", "model identifier")
        self.call("DELETE", f"/admin/tokens/{JOB}", token=ADMIN, admin=True)
        self.assert_policy_denial(*self.chat(tok)[:2], 401, "inference")                 # revoked
        self.assertEqual(Upstream.received, [])

    def test_08_forwarding_rejects_arbitrary_destinations_and_paths(self):
        tok = self.issue()
        decoy = f"127.0.0.1:{self.decoy.server_port}"
        auth = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
        body = json.dumps({"model": MODEL, "messages": []}).encode()
        for target in (f"http://{decoy}/v1/chat/completions", "http://169.254.169.254/latest/meta-data/", "https://evil.example/v1/chat/completions",
                       f"{decoy}", "*", "/v1/chat/completions?upstream=http://" + decoy, "/v1/chat/completions/../../../etc/passwd",
                       "/v1/chat/completions%2F..%2F..%2Fapi", "/v1//chat/completions", "/v1/chat/completions/", "/v1/embeddings",
                       "/v1/completions", "/api/v1/chat/completions", "/v1/chat/completions#frag"):
            with self.subTest(target=target):
                st, payload = raw_request(self.port, "POST", target, auth, body)
                err = json.loads(payload)["error"]
                self.assertEqual(err["type"], "proxy_policy"); self.assertIn(st, (400, 404))
        st, payload = raw_request(self.port, "CONNECT", decoy, auth)
        self.assertEqual(st, 405)
        # Host / forwarding headers cannot redirect the request: the upstream origin is fixed configuration
        st, raw, _ = self.call("POST", "/v1/chat/completions", {"model": MODEL, "messages": [], "url": f"http://{decoy}/"}, token=tok,
                               headers={"Host": decoy, "X-Forwarded-Host": decoy, "X-Forwarded-For": "10.0.0.1", "Forwarded": f"host={decoy}"})
        self.assertEqual(st, 200)
        up = Upstream.received[-1]
        self.assertEqual(up["headers"]["Host"], f"127.0.0.1:{self.upstream.server_port}")
        self.assertEqual(up["path"], "/api/v1/chat/completions")
        self.assertNotIn("X-Forwarded-For", up["headers"])
        self.assertEqual(len(Upstream.received), 1)

    def test_09_never_forwards_to_n8n_docker_host_or_container_ports(self):
        tok = self.issue()
        auth = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
        body = json.dumps({"model": MODEL, "messages": []}).encode()
        for target in ("http://prqa-n8n-1:5678/rest/workflows", "http://n8n:5678/webhook/x", "http://unix/var/run/docker.sock/containers/json",
                       "http://localhost:2375/info", "http://172.17.0.1:22/", f"http://127.0.0.1:{self.decoy.server_port}/",
                       "http://prqa-worker:8080/jobs", "/../../var/run/docker.sock"):
            st, payload = raw_request(self.port, "POST", target, auth, body)
            self.assertIn(st, (400, 404), target); self.assertEqual(json.loads(payload)["error"]["type"], "proxy_policy")
        # the origin is validated at start-up: no internal or plain-http non-loopback upstream can be configured
        for origin in ("http://prqa-n8n-1:5678", "http://172.17.0.1:2375", "https://api.groq.com/openai/v1", "unix:///var/run/docker.sock"):
            with self.assertRaises(SystemExit, msg=origin):
                mp.ProxyConfig(upstream_origin=origin, allowed_model=MODEL, provider_key="k", admin_token=ADMIN).validate()
        self.assertEqual(Decoy.hits, []); self.assertEqual(Upstream.received, [])

    def test_10_malformed_and_oversized_requests_fail_safely(self):
        tok = self.issue()
        auth = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
        cases = [("not json", b"{not json", {}, 400), ("json array", b"[1,2]", {}, 403), ("bad utf-8", b"\xff\xfe", {}, 400),
                 ("oversized", json.dumps({"model": MODEL, "messages": [{"content": "x" * 5000}]}).encode(), {}, 413),
                 ("bad content-length", b"{}", {"Content-Length": "abc"}, 400),
                 ("chunked", b"{}", {"Transfer-Encoding": "chunked"}, 400)]
        for name, body, extra, expected in cases:
            with self.subTest(case=name):
                h = {**auth, **extra}
                if "Content-Length" in extra:
                    head = f"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nConnection: close\r\nAuthorization: Bearer {tok}\r\nContent-Length: abc\r\n\r\n"
                    with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
                        s.sendall(head.encode()); data = b""
                        while chunk := s.recv(65536):
                            data += chunk
                    st = int(data.split(b" ", 2)[1]); payload = data.partition(b"\r\n\r\n")[2]
                else:
                    st, payload = raw_request(self.port, "POST", "/v1/chat/completions", h, body)
                self.assertEqual(st, expected, (name, payload[:200]))
                self.assertEqual(json.loads(payload)["error"]["type"], "proxy_policy")
        for body, expected in ((b"{bad", 400), (b"[]", 400), (json.dumps({"job_id": "../x", "model": MODEL}).encode(), 422),
                               (json.dumps({"job_id": JOB, "model": MODEL, "ttl_s": "forever"}).encode(), 422)):
            st, raw, _ = self.call("POST", "/admin/tokens", body, token=ADMIN, admin=True)
            self.assert_policy_denial(st, raw, expected, "admin")
        st, raw, _ = self.call("DELETE", "/admin/tokens/..%2Fall", token=ADMIN, admin=True)
        self.assertEqual(st, 400)
        self.assertEqual(Upstream.received, [])

    def test_11_provider_errors_and_malformed_responses_are_sanitized(self):
        tok = self.issue()
        for mode, expected in (("error", 500), ("unauthorized", 502), ("not_json", 502), ("no_choices", 502)):
            with self.subTest(mode=mode):
                Upstream.mode = mode
                st, raw, hdrs = self.chat(tok)
                self.assertEqual(st, expected)
                err = json.loads(raw)["error"]
                self.assertEqual(err["type"], "proxy_upstream")
                for leak in (PROVIDER_DETAIL, PROVIDER_KEY[:12], "html"):
                    self.assertNotIn(leak, raw.decode() + json.dumps(hdrs))

    def test_12_logs_carry_no_secret_token_body_or_reasoning(self):
        tok = self.issue("20261027-logcheck0001")
        Upstream.mode = "ok"; self.chat(tok)
        Upstream.mode = "error"; self.chat(tok)
        self.call("POST", "/admin/tokens", {"job_id": "x"}, token="wrong-admin-guess", admin=True)
        log = self.logbuf.getvalue()
        for secret in (PROVIDER_KEY, ADMIN, tok, "wrong-admin-guess", SECRET_PROMPT, HIDDEN_REASONING, PROVIDER_DETAIL, "SYNTHETIC COMPLETION"):
            self.assertNotIn(secret, log)
        stats = json.dumps(self.inference.handler_class.stats.log)
        for secret in (PROVIDER_KEY, ADMIN, tok, SECRET_PROMPT, HIDDEN_REASONING):
            self.assertNotIn(secret, stats)
        self.assertIn("20261027-logcheck0001", log)                                       # the job id is what is logged

    def test_13_no_response_ever_carried_a_secret(self):
        blob = b"".join(type(self).responses)
        self.assertNotIn(PROVIDER_KEY.encode(), blob); self.assertNotIn(ADMIN.encode(), blob)


if __name__ == "__main__":
    unittest.main()
