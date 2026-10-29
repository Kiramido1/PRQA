"""Stage 3 — Docker policy (body-level validation) and the socket proxy against a fake Docker backend on a unix socket.
No Docker daemon is involved.

Run: cd "<project>" && PYTHONPATH=worker python3 -m unittest tests.test_docker_policy -v
"""
from __future__ import annotations

import json
import os
import socket
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))

import docker_policy as dp  # noqa: E402
import docker_policy_proxy as dpp  # noqa: E402

IMAGE_ID = "sha256:" + "b" * 64
JOB = "20261022-abc123def456"
POLICY = dp.Policy(allowed_image_ids=frozenset({IMAGE_ID}), allowed_networks=frozenset({"none", "prqa_sandbox"}))
LABEL_JOB, LABEL_KIND = "prqa.job", "prqa.kind"


def URLQ(data) -> str:
    return quote(json.dumps(data, separators=(",", ":")))


def sandbox_spec(**over):
    spec = {
        "Image": IMAGE_ID, "User": "10001:10001",
        "Labels": {"prqa.job": JOB, "prqa.kind": "sandbox"},
        "Cmd": ["--repo", "o/r", "--sha", "a" * 40, "--model", "openrouter/x:free", "--timeout-minutes", "5",
                "--tree-sha256", "c" * 64, "--prompt-sha256", "d" * 64, "--prechecked"],
        "Env": ["PRQA_JOB_ID=" + JOB, "PRQA_TEST_LEVEL=standard", "OPENCODE_DISABLE_PROJECT_CONFIG=1"],
        "HostConfig": {"NetworkMode": "none", "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"], "ReadonlyRootfs": True,
                       "Memory": 2 * 1024 ** 3, "PidsLimit": 256, "NanoCpus": 2 * 10 ** 9,
                       "Tmpfs": {"/tmp": "size=256m", "/work": "size=1g", "/home/sandbox": "size=64m"},
                       "Mounts": [{"Type": "volume", "Source": f"prqa-checkout-{JOB}", "Target": "/checkout", "ReadOnly": True}]},
    }
    for k, v in over.items():
        if k.startswith("HostConfig."):
            spec["HostConfig"][k.split(".", 1)[1]] = v
        else:
            spec[k] = v
    return spec


def helper_spec(**over):
    spec = {
        "Image": IMAGE_ID, "User": "10001:10001", "Labels": {"prqa.job": JOB, "prqa.kind": "helper"},
        "Entrypoint": ["/usr/local/bin/prqa-populate-checkout"], "Cmd": ["/checkout", JOB, "/work/checkout.tar"],
        "HostConfig": {"NetworkMode": "none", "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"], "ReadonlyRootfs": False,
                       "Memory": 512 * 1024 ** 2, "PidsLimit": 64, "NanoCpus": 10 ** 9, "Tmpfs": {"/tmp": "size=64m"},
                       "Mounts": [{"Type": "volume", "Source": f"prqa-checkout-{JOB}", "Target": "/checkout"}]},
    }
    for k, v in over.items():
        if k.startswith("HostConfig."):
            spec["HostConfig"][k.split(".", 1)[1]] = v
        else:
            spec[k] = v
    return spec


def ev(method, path, body=None):
    return dp.evaluate(POLICY, method, path, json.dumps(body).encode() if isinstance(body, (dict, list)) else body)


class PolicyTests(unittest.TestCase):
    def test_allowed_operations(self):
        for method, path in (("GET", "/_ping"), ("HEAD", "/_ping"), ("GET", "/v1.47/version"),
                             ("GET", f"/containers/prqa-sandbox-{JOB}/json"), ("GET", f"/containers/prqa-sandbox-{JOB}/logs?stdout=1"),
                             ("POST", f"/containers/prqa-sandbox-{JOB}/start"), ("POST", f"/containers/prqa-populate-{JOB}/wait"),
                             ("DELETE", f"/containers/prqa-sandbox-{JOB}?force=1"), ("HEAD", f"/containers/prqa-populate-{JOB}/archive?path=/work"),
                             ("HEAD", f"/containers/prqa-populate-{JOB}/archive?path=/work/checkout.tar"),
                             ("GET", f"/volumes/prqa-checkout-{JOB}"), ("DELETE", f"/volumes/prqa-checkout-{JOB}?force=1"),
                             ("GET", "/networks/prqa_sandbox"), ("GET", f"/images/{IMAGE_ID}/json"), ("GET", "/images/prqa/opencode-sandbox:local/json"),
                             # host-wide listings must carry the reconcile pass's label filters
                             ("GET", f'/containers/json?all=1&filters={URLQ({"label": [LABEL_JOB, LABEL_KIND]})}'),
                             # bare /volumes is allowed but the proxy injects the prqa labels before forwarding
                             ("GET", "/volumes"),
                             ("GET", f'/volumes?filters={URLQ({"label": {LABEL_JOB: "", LABEL_KIND: "checkout"}})}')):
            d = ev(method, path)
            self.assertTrue(d.allowed, f"{method} {path}: {d.reason}")
        d = ev("GET", "/volumes")
        self.assertIn("prqa.kind", d.forwarded_path); self.assertIn("prqa.job", d.forwarded_path)
        self.assertIn("checkout", d.forwarded_path)
        d = ev("PUT", f"/v1.47/containers/prqa-populate-{JOB}/archive?path=/work", b"tar-bytes")
        self.assertTrue(d.allowed); self.assertEqual(d.job_id, JOB)
        self.assertTrue(ev("POST", f"/containers/create?name=prqa-sandbox-{JOB}", sandbox_spec()).allowed)
        self.assertTrue(ev("POST", f"/containers/create?name=prqa-populate-{JOB}", helper_spec()).allowed)
        self.assertTrue(ev("POST", "/volumes/create", {"Name": f"prqa-checkout-{JOB}", "Labels": {"prqa.job": JOB, "prqa.kind": "checkout"}}).allowed)

    def test_denied_operations(self):
        for method, path in (("POST", f"/containers/prqa-sandbox-{JOB}/exec"), ("POST", f"/containers/prqa-sandbox-{JOB}/attach?stream=1"),
                             ("POST", "/build"), ("POST", "/images/create?fromImage=evil"), ("DELETE", f"/images/{IMAGE_ID}"),
                             ("POST", f"/commit?container=prqa-sandbox-{JOB}"), ("POST", "/networks/create"), ("POST", "/exec/abc/start"),
                             ("GET", "/containers/abcdef123456/json"), ("DELETE", "/containers/prqa-n8n-1?force=1"), ("DELETE", "/volumes/n8n_data"),
                             ("POST", f"/containers/prqa-sandbox-{JOB}/kill"), ("PUT", f"/containers/prqa-sandbox-{JOB}/archive?path=/work"),
                             ("PUT", f"/containers/prqa-populate-{JOB}/archive?path=/etc"), ("GET", "/images/ubuntu:latest/json"),
                             ("PUT", f"/containers/prqa-populate-{JOB}/archive?path=/work/checkout.tar"), ("HEAD", f"/containers/prqa-populate-{JOB}/archive?path=/etc"),
                             ("HEAD", f"/containers/prqa-sandbox-{JOB}/archive?path=/work"),
                             ("POST", "/volumes/prune"), ("POST", "/containers/prune"), ("GET", "/events"), ("POST", "/swarm/init"),
                             ("GET", "/info"), ("GET", "/networks/foreign_net"),
                             ("GET", "/volumes?filters=notjson"), ("GET", '/volumes?filters=%7B%5D%7D'),
                             # bare container lists are REWRITTEN with the prqa labels (never denied), but a
                             # differently-cased filters spelling is ambiguous and refused outright
                             ("GET", "/containers/json?FILTERS=" + URLQ({"label": [LABEL_JOB, LABEL_KIND]}))):
            d = ev(method, path, b"{}")
            self.assertFalse(d.allowed, f"{method} {path} should be denied")

    def test_container_create_body_policy(self):
        cases = {
            "privileged": sandbox_spec(**{"HostConfig.Privileged": True}),
            "host pid": sandbox_spec(**{"HostConfig.PidMode": "host"}),
            "host net": sandbox_spec(**{"HostConfig.NetworkMode": "host"}),
            "bridge net": sandbox_spec(**{"HostConfig.NetworkMode": "bridge"}),
            "host ipc": sandbox_spec(**{"HostConfig.IpcMode": "host"}),
            "bind mount": sandbox_spec(**{"HostConfig.Binds": ["/:/host"]}),
            "extra mount": sandbox_spec(**{"HostConfig.Mounts": [{"Type": "volume", "Source": f"prqa-checkout-{JOB}", "Target": "/checkout", "ReadOnly": True},
                                                                  {"Type": "bind", "Source": "/var/run/docker.sock", "Target": "/sock"}]}),
            "foreign volume": sandbox_spec(**{"HostConfig.Mounts": [{"Type": "volume", "Source": "prqa-checkout-20261022-other0001", "Target": "/checkout", "ReadOnly": True}]}),
            "rw volume": sandbox_spec(**{"HostConfig.Mounts": [{"Type": "volume", "Source": f"prqa-checkout-{JOB}", "Target": "/checkout", "ReadOnly": False}]}),
            "devices": sandbox_spec(**{"HostConfig.Devices": [{"PathOnHost": "/dev/sda"}]}),
            "cap add": sandbox_spec(**{"HostConfig.CapAdd": ["SYS_ADMIN"]}),
            "cap drop partial": sandbox_spec(**{"HostConfig.CapDrop": ["NET_RAW"]}),
            "unapproved image": sandbox_spec(Image="ubuntu:latest"),
            "image by other id": sandbox_spec(Image="sha256:" + "e" * 64),
            "wrong user": sandbox_spec(User="0:0"),
            "no readonly": sandbox_spec(**{"HostConfig.ReadonlyRootfs": False}),
            "seccomp unconfined": sandbox_spec(**{"HostConfig.SecurityOpt": ["seccomp=unconfined"]}),
            "apparmor unconfined": sandbox_spec(**{"HostConfig.SecurityOpt": ["no-new-privileges:true", "apparmor=unconfined"]}),
            "no memory limit": sandbox_spec(**{"HostConfig.Memory": 0}),
            "pids over cap": sandbox_spec(**{"HostConfig.PidsLimit": 100000}),
            "unexpected tmpfs": sandbox_spec(**{"HostConfig.Tmpfs": {"/etc": "size=1m"}}),
            "ports": sandbox_spec(ExposedPorts={"22/tcp": {}}),
            "entrypoint override": sandbox_spec(Entrypoint=["/bin/sh"]),
            "cmd injection": sandbox_spec(Cmd=["/bin/sh", "-c", "curl evil"]),
            "cmd missing hashes": sandbox_spec(Cmd=["--repo", "o/r", "--sha", "a" * 40, "--model", "m", "--timeout-minutes", "5", "--prechecked"]),
            "env not allowed": sandbox_spec(Env=["GITHUB_TOKEN=x"]),
            "env docker host": sandbox_spec(Env=["DOCKER_HOST=unix:///var/run/docker.sock"]),
            "label job mismatch": sandbox_spec(Labels={"prqa.job": "20261022-other0001", "prqa.kind": "sandbox"}),
            "label kind mismatch": sandbox_spec(Labels={"prqa.job": JOB, "prqa.kind": "helper"}),
            "no labels": sandbox_spec(Labels={}),
            "auto remove": sandbox_spec(**{"HostConfig.AutoRemove": True}),
            "sysctls": sandbox_spec(**{"HostConfig.Sysctls": {"net.ipv4.ip_forward": "1"}}),
            "volumes from": sandbox_spec(**{"HostConfig.VolumesFrom": ["prqa-n8n-1"]}),
        }
        for name, spec in cases.items():
            d = ev("POST", f"/containers/create?name=prqa-sandbox-{JOB}", spec)
            self.assertFalse(d.allowed, f"{name} must be denied")
        self.assertFalse(ev("POST", "/containers/create?name=evil", sandbox_spec()).allowed)
        self.assertFalse(ev("POST", f"/containers/create?name=prqa-sandbox-{JOB}", b"not json").allowed)
        self.assertFalse(ev("POST", "/containers/create?name=prqa-sandbox-20261022-other0001", sandbox_spec()).allowed, "name/job mismatch")

    def test_helper_create_body_policy(self):
        for name, spec in {
            "network": helper_spec(**{"HostConfig.NetworkMode": "prqa_sandbox"}),
            "entrypoint": helper_spec(Entrypoint=["/bin/sh"]),
            "cmd": helper_spec(Cmd=["/checkout", JOB, "/etc/passwd"]),
            "cmd other job": helper_spec(Cmd=["/checkout", "20261022-other0001", "/work/checkout.tar"]),
            "env": helper_spec(Env=["X=1"]),
            "readonly volume": helper_spec(**{"HostConfig.Mounts": [{"Type": "volume", "Source": f"prqa-checkout-{JOB}", "Target": "/checkout", "ReadOnly": True}]}),
            "privileged": helper_spec(**{"HostConfig.Privileged": True}),
            "cap add": helper_spec(**{"HostConfig.CapAdd": ["CHOWN"]}),
        }.items():
            self.assertFalse(ev("POST", f"/containers/create?name=prqa-populate-{JOB}", spec).allowed, name)

    def test_volume_create_body_policy(self):
        for name, spec in {
            "bad name": {"Name": "n8n_data", "Labels": {"prqa.job": JOB, "prqa.kind": "checkout"}},
            "label mismatch": {"Name": f"prqa-checkout-{JOB}", "Labels": {"prqa.job": "other", "prqa.kind": "checkout"}},
            "no labels": {"Name": f"prqa-checkout-{JOB}"},
            "driver opts": {"Name": f"prqa-checkout-{JOB}", "Labels": {"prqa.job": JOB, "prqa.kind": "checkout"}, "DriverOpts": {"device": "/dev/sda"}},
            "nfs driver": {"Name": f"prqa-checkout-{JOB}", "Labels": {"prqa.job": JOB, "prqa.kind": "checkout"}, "Driver": "nfs"},
        }.items():
            self.assertFalse(ev("POST", "/volumes/create", spec).allowed, name)

    def test_archive_size_limit(self):
        small = dp.Policy(allowed_image_ids=frozenset({IMAGE_ID}), max_archive_bytes=10)
        self.assertFalse(dp.evaluate(small, "PUT", f"/containers/prqa-populate-{JOB}/archive?path=/work", b"x" * 11).allowed)
        self.assertTrue(dp.evaluate(small, "PUT", f"/containers/prqa-populate-{JOB}/archive?path=/work", b"x" * 10).allowed)


class PolicyEdgeTests(unittest.TestCase):
    """Encoded, malformed and alternate request-target forms: the daemon percent-decodes paths and queries, so every
    form that could decode differently must either be rejected outright or decide identically to `r.Form`/`URL.Path`.
    First query value wins in both implementations; anything ambiguous fails closed."""

    def test_percent_encoded_and_foreign_forms_fail_closed(self):
        for method, path in (("GET", "/containers/prqa-sandbox-20261022-abc123def456%2Fev/logs"),
                             ("GET", "/containers/prqa%2Dsandbox-20261022-abc123def456/logs"),
                             ("GET", "/containers/prqa-sandbox-20261022-abc123def456/LOGS"),
                             ("GET", f"/containers/prqa-sandbox-{JOB}/logs/"),
                             ("POST", f"/containers/prqa-sandbox-{JOB}/start/"),
                             ("GET", "/containers/.create"),
                             ("HEAD", f"/containers/prqa-populate-{JOB}"),   # bare name, unknown action
                             ("GET", f"/containers/prqa-populate-{JOB}/stat"),
                             ("GET", f"/containers/prqa-populate-{JOB}/archive"),  # daemon would return the tar: deny
                             ("DELETE", f"/containers/prqa-populate-{JOB}/archive"),
                             ("PUT", f"/containers/prqa-sandbox-{JOB}/archive?path=/work"),
                             ("GET", "/v1.47/x/version"),
                             ("POST", "//containers/create")):
            self.assertFalse(ev(method, path).allowed, f"{method} {path}")
        d = ev("GET", "//v1.47/version")
        self.assertTrue(d.allowed and d.reason == "ping/version",
                        "urlsplit treats //host/path as netloc+path; the proxy refuses netloc-form targets before policy (test_target_shape)")

    def test_archive_destination_query_forms(self):
        d = ev("PUT", f"/containers/prqa-populate-{JOB}/archive", b"x")
        self.assertFalse(d.allowed, "missing path must be denied")
        self.assertFalse(ev("PUT", f"/containers/prqa-populate-{JOB}/archive?path=", b"x").allowed)
        self.assertFalse(ev("PUT", f"/containers/prqa-populate-{JOB}/archive?path=/work%0A", b"x").allowed, "trailing newline is not /work")
        self.assertFalse(ev("PUT", f"/containers/prqa-populate-{JOB}/archive?path=/etc&path=/work", b"x").allowed, "first value wins (matches r.Form.Get)")
        self.assertTrue(ev("PUT", f"/containers/prqa-populate-{JOB}/archive?path=/work&path=/etc", b"x").allowed, "first value wins (matches r.Form.Get)")
        self.assertTrue(ev("PUT", f"/containers/prqa-populate-{JOB}/archive?%70ath=/work", b"x").allowed, "daemon decodes the key identically")
        self.assertTrue(ev("PUT", f"/containers/prqa-populate-{JOB}/archive?path=/work&type=0&noOverwriteDirNonDir=1", b"x").allowed)
        self.assertFalse(ev("PUT", f"/containers/prqa-populate-{JOB}/archive?path=/work%2F..%2Fetc", b"x").allowed)
        self.assertFalse(ev("PUT", f"/containers/prqa-populate-{JOB}/archive?path=/work%252Fetc", b"x").allowed)

    def test_version_and_image_forms(self):
        self.assertTrue(ev("GET", "/v1.99/version").allowed, "any major.minor handled the same by the daemon")
        self.assertFalse(ev("GET", "/version/").allowed)
        self.assertFalse(ev("GET", "/images/ubuntu:latest/json/").allowed)
        self.assertTrue(ev("GET", f"/images/{IMAGE_ID.replace('sha256:', '')}/json").allowed, "digest without the sha256: prefix")

    def test_double_slash_and_odd_methods(self):
        self.assertFalse(ev("BREW", f"/containers/prqa-sandbox-{JOB}/start").allowed)
        self.assertFalse(ev("POST", f"/containers/prqa-sandbox-{JOB}/zebra").allowed, "unknown sub-action denied")
        self.assertFalse(ev("PUT", f"/containers/prqa-sandbox-{JOB}/json").allowed)


# ----------------------------------------------------------------- proxy over a fake backend ---
class FakeDockerBackend(BaseHTTPRequestHandler):
    received: list = []

    def log_message(self, *a):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        FakeDockerBackend.received.append({"method": self.command, "path": self.path, "body": body, "auth": self.headers.get("Authorization")})
        data = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(200 if self.command != "DELETE" else 204)
        if self.command != "DELETE":
            self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        else:
            self.send_header("Content-Length", "0"); self.end_headers()

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _handle


class ProxyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.backend_path = os.path.join(self.tmp.name, "docker.sock")
        self.listen_path = os.path.join(self.tmp.name, "policy.sock")
        FakeDockerBackend.received = []

        class BackendServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
        self.backend = BackendServer(self.backend_path, FakeDockerBackend)
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        dpp.Handler.policy = POLICY; dpp.Handler.upstream = self.backend_path
        self.proxy = dpp.UnixServer(self.listen_path, dpp.Handler)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()

    def tearDown(self):
        self.proxy.shutdown(); self.proxy.server_close(); self.backend.shutdown(); self.backend.server_close(); self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        conn = dpp.UnixHTTPConnection(self.listen_path, timeout=10)
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        if data:
            h["Content-Length"] = str(len(data))
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse(); raw = resp.read(); conn.close()
        return resp.status, raw

    def test_allowed_requests_are_forwarded_and_denied_never_reach_docker(self):
        st, raw = self.call("GET", "/_ping")
        self.assertEqual(st, 200); self.assertEqual(json.loads(raw)["path"], "/_ping")
        st, raw = self.call("POST", f"/v1.47/containers/create?name=prqa-sandbox-{JOB}", sandbox_spec())
        self.assertEqual(st, 200)
        st, raw = self.call("PUT", f"/containers/prqa-populate-{JOB}/archive?path=/work", b"TARBYTES", {"Content-Type": "application/x-tar"})
        self.assertEqual(st, 200)
        self.assertEqual(FakeDockerBackend.received[-1]["body"], b"TARBYTES")
        self.assertEqual(len(FakeDockerBackend.received), 3)
        for method, path, body in (("POST", f"/containers/create?name=prqa-sandbox-{JOB}", sandbox_spec(**{"HostConfig.Privileged": True})),
                                   ("POST", f"/containers/create?name=prqa-sandbox-{JOB}", sandbox_spec(Image="ubuntu")),
                                   ("POST", f"/containers/create?name=prqa-sandbox-{JOB}", sandbox_spec(**{"HostConfig.Binds": ["/:/h"]})),
                                   ("POST", f"/containers/create?name=prqa-sandbox-{JOB}", sandbox_spec(**{"HostConfig.NetworkMode": "host"})),
                                   ("POST", f"/containers/prqa-sandbox-{JOB}/exec", {"Cmd": ["sh"]}),
                                   ("POST", "/build", b""), ("DELETE", "/containers/prqa-n8n-1", None), ("GET", "/events", None)):
            st, raw = self.call(method, path, body)
            self.assertEqual(st, 403, f"{method} {path}"); self.assertIn("prqa-docker-proxy", raw.decode())
        self.assertEqual(len(FakeDockerBackend.received), 3, "denied requests never reached the backend")

    def test_upgrade_hijack_is_refused(self):
        st, raw = self.call("POST", f"/containers/prqa-sandbox-{JOB}/attach?stream=1", b"", {"Connection": "Upgrade", "Upgrade": "tcp"})
        self.assertEqual(st, 403); self.assertEqual(len(FakeDockerBackend.received), 0)

    def test_oversized_archive_rejected_before_forwarding(self):
        dpp.Handler.policy = dp.Policy(allowed_image_ids=frozenset({IMAGE_ID}), max_archive_bytes=16)
        st, raw = self.call("PUT", f"/containers/prqa-populate-{JOB}/archive?path=/work", b"x" * 32, {"Content-Type": "application/x-tar"})
        self.assertEqual(st, 413); self.assertEqual(len(FakeDockerBackend.received), 0)
        dpp.Handler.policy = POLICY


class RoutingFakeBackend(BaseHTTPRequestHandler):
    """Fake daemon that also answers `GET /containers/<ref>/json` from a canned map so bare-ID resolution runs offline."""
    received: list = []
    inspect_map: dict = {}

    def log_message(self, *a):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = self.path.split("?")[0]
        segs = [s for s in path.split("/") if s]
        is_inspect = len(segs) == 3 and segs[0] == "containers" and segs[2] == "json" and self.command == "GET"
        RoutingFakeBackend.received.append({"method": self.command, "path": self.path, "body": body})
        if is_inspect:
            info = RoutingFakeBackend.inspect_map.get(segs[1])
            payload = json.dumps(info if info is not None else {"message": f"no such container: {segs[1]}"})
            code = 200 if info is not None else 404
            data = payload.encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)
            return
        data = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(204 if self.command == "DELETE" else 200)
        if self.command != "DELETE":
            self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)
        else:
            self.send_header("Content-Length", "0"); self.end_headers()

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _handle


class AdversarialProxyTests(unittest.TestCase):
    """Malformed HTTP framing, body caps and bare-ID resolution must all fail closed and never desync a reused
    connection: a rejected request is consumed, never forwarded, and the same connection stays usable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.backend_path = os.path.join(self.tmp.name, "docker.sock")
        self.listen_path = os.path.join(self.tmp.name, "policy.sock")
        RoutingFakeBackend.received = []
        RoutingFakeBackend.inspect_map = {}

        class BackendServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
        self.backend = BackendServer(self.backend_path, RoutingFakeBackend)
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        dpp.Handler.policy = POLICY
        dpp.Handler.upstream = self.backend_path
        self.proxy = dpp.UnixServer(self.listen_path, dpp.Handler)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()

    def tearDown(self):
        self.proxy.shutdown(); self.proxy.server_close(); self.backend.shutdown(); self.backend.server_close(); self.tmp.cleanup()

    def raw(self, payload: bytes, *, responses: int = 1, halfclose=False, timeout=10):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(self.listen_path)
        out = b""
        try:
            s.sendall(payload)
            if halfclose:
                s.shutdown(socket.SHUT_WR)
            for _ in range(responses):
                out += self.read_one_response(s)
        finally:
            s.close()
        return out

    def headed(self, out: bytes):
        return out.split(b"\r\n", 1)[0] if out else b"<none>"

    def read_one_response(self, sock: socket.socket) -> bytes:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                return buf
            buf += chunk
        header, rest = buf.split(b"\r\n\r\n", 1)
        want, chunked_rsp = None, False
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                want = int(line.split(b":", 1)[1] or 0)
            elif line.lower().startswith(b"transfer-encoding:"):
                chunked_rsp = b"chunked" in line.lower()
        if chunked_rsp:
            while not rest.endswith(b"0\r\n\r\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                rest += chunk
        elif want is None:
            rest = self._read_to_close(sock, rest)
        else:
            while len(rest) < want:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                rest += chunk
        return header + b"\r\n\r\n" + rest

    def _read_to_close(self, sock: socket.socket, rest: bytes) -> bytes:
        parts = [rest]
        while True:
            try:
                chunk = sock.recv(65536)
            except (TimeoutError, OSError):
                return b"".join(parts) + b"<STILL-OPEN>"
            if not chunk:
                return b"".join(parts) + b"<EOF>"
            parts.append(chunk)

    # ------------------------------------------------------------------ framing --
    def test_00_origin_form_targets_only(self):
        # absolute-form targets are rejected outright; "//host/..." is rewritten to "/host/..." by
        # BaseHTTPRequestHandler.parse_request (CPython gh-87389, fixed in 3.11.9+/3.12.x), so the proxy
        # never sees a netloc and the policy 403s the leftover path - fail closed either way
        for target in ("http://evil.example/v1.47/_ping", "https://docker/containers/json"):
            RoutingFakeBackend.received = []
            out = self.raw(f"GET {target} HTTP/1.1\r\nHost: docker\r\nContent-Length: 0\r\n\r\n".encode())
            self.assertTrue(self.headed(out).startswith(b"HTTP/1.1 400"), f"{target}: {out[:120]!r}")
            self.assertEqual(RoutingFakeBackend.received, [], f"{target} reached the backend")
        RoutingFakeBackend.received = []
        out = self.raw(b"GET //host/v1.47/version HTTP/1.1\r\nHost: docker\r\nContent-Length: 0\r\n\r\n")
        self.assertTrue(self.headed(out).startswith(b"HTTP/1.1 403"), out[:120])
        self.assertEqual(RoutingFakeBackend.received, [], "collapsed netloc-form reached the backend")

    def test_01_valid_chunked_uploads_still_flow(self):
        payload = (f"PUT /containers/prqa-populate-{JOB}/archive?path=/work HTTP/1.1\r\nTransfer-Encoding: chunked\r\n"
                   "Host: docker\r\n\r\n4;ext=abc\r\nTARB\r\n5\r\nBIKES\r\n0\r\nX-A: b\r\n\r\n").encode()
        out = self.raw(payload)
        self.assertTrue(self.headed(out).startswith(b"HTTP/1.1 200"), out[:200])
        self.assertEqual([r["body"] for r in RoutingFakeBackend.received if r["method"] == "PUT"][0], b"TARBBIKES")
        send = self.raw((f"PUT /containers/prqa-populate-{JOB}/archive?path=/work HTTP/1.1\r\nTransfer-Encoding: chunked\r\n"
                         "Host: docker\r\n\r\n4\r\nTARB\r\n0\r\n\r\n").encode())
        self.assertTrue(self.headed(send).startswith(b"HTTP/1.1 200"), send[:200])

    def test_02_conflicting_framing_headers_are_rejected_before_forwarding(self):
        archive = f"PUT /containers/prqa-populate-{JOB}/archive?path=/work HTTP/1.1\r\n".encode()
        host = b"Host: docker\r\n\r\n"
        rejects = {
            "te+content-length": archive + b"Content-Length: 3\r\nTransfer-Encoding: chunked\r\n" + host + b"5\r\nHELLO\r\n0\r\n\r\n",
            "te gzip,chunked": archive + b"Transfer-Encoding: gzip, chunked\r\n" + host + b"5\r\nHELLO\r\n0\r\n\r\n",
            "te identity": archive + b"Transfer-Encoding: identity\r\nContent-Length: 5\r\n" + host + b"HELLO",
            "content-length twice different": archive + b"Content-Length: 5\r\nContent-Length: 9\r\n" + host + b"HELLO",
        }
        for name, payload in rejects.items():
            RoutingFakeBackend.received = []
            out = self.raw(payload)
            self.assertTrue(self.headed(out).startswith(b"HTTP/1.1 400"), f"{name}: {out[:120]!r}")
            self.assertEqual(RoutingFakeBackend.received, [], f"{name} reached the backend")
        # identical duplicate tokens are unambiguous: single chunked framing, re-emitted with one explicit length
        RoutingFakeBackend.received = []
        cl_req = f"PUT /containers/prqa-populate-{JOB}/archive?path=/work HTTP/1.1\r\nContent-Length: 5\r\nContent-Length: 5\r\nHost: docker\r\n\r\nTARBY".encode()
        out = self.raw(cl_req)
        self.assertTrue(self.headed(out).startswith(b"HTTP/1.1 200"), out[:120])
        te_req = (f"PUT /containers/prqa-populate-{JOB}/archive?path=/work HTTP/1.1\r\nTransfer-Encoding: chunked\r\n"
                  "Transfer-Encoding: chunked\r\nHost: docker\r\n\r\n5\r\nHELLO\r\n0\r\n\r\n").encode()
        out = self.raw(te_req)
        self.assertTrue(self.headed(out).startswith(b"HTTP/1.1 200"), out[:120])
        self.assertEqual([r["body"] for r in RoutingFakeBackend.received], [b"TARBY", b"HELLO"])

    def test_03_chunk_grammar_is_strict(self):
        archive = f"PUT /containers/prqa-populate-{JOB}/archive?path=/work HTTP/1.1\r\nTransfer-Encoding: chunked\r\nHost: docker\r\n\r\n".encode()
        cases = {
            "garbage chunk terminator": b"5\r\nHELLO\rXX\r\n0\r\n\r\n",
            "empty size line": b"5\r\nHELLO\r\n\r\n0\r\n\r\n",
            "non-hex size": b"zz\r\nhello\r\n0\r\n\r\n",
            "underscore size": b"5_5\r\nhello\r\n0\r\n\r\n",
            "negative size full body": b"-1\r\nSADDATA\r\n0\r\n\r\n",
            "plus size": b"+8\r\nhello\r\n0\r\n\r\n",
            "truncated before terminator": b"4\r\nTAR",
        }
        for name, tail in cases.items():
            RoutingFakeBackend.received = []
            out = self.raw(archive + tail, halfclose=name == "truncated before terminator")
            self.assertTrue(self.headed(out).startswith(b"HTTP/1.1 400") or self.headed(out).startswith(b"HTTP/1.1 413"),
                            f"{name}: got {out[:120]!r}")
            self.assertEqual(RoutingFakeBackend.received, [], f"{name} reached the backend")

    def test_04_negative_chunk_size_never_reads_unbounded(self):
        out = self.raw(f"PUT /containers/prqa-populate-{JOB}/archive?path=/work HTTP/1.1\r\nTransfer-Encoding: chunked\r\nHost: docker\r\n\r\n-1\r\nSADDATA\r\n0\r\n\r\n".encode())
        self.assertTrue(self.headed(out).startswith(b"HTTP/1.1 400"), out[:120])
        self.assertEqual(RoutingFakeBackend.received, [])

    def test_05_oversized_declared_chunk_is_rejected_without_being_read(self):
        out = self.raw(f"PUT /containers/prqa-populate-{JOB}/archive?path=/work HTTP/1.1\r\nTransfer-Encoding: chunked\r\nHost: docker\r\n\r\nFFFFFF0000\r\n".encode(), timeout=6)
        self.assertTrue(self.headed(out).startswith(b"HTTP/1.1 413"), out[:120])
        self.assertEqual(RoutingFakeBackend.received, [])

    def test_06_json_bodies_are_capped_at_1mib_and_archive_bodies_are_not(self):
        pad = b'{"pad":"'
        too_big = pad + b"a" * (dpp.MAX_JSON_BODY + 1) + b'"}'
        out = self.raw(f"POST /containers/create?name=prqa-sandbox-{JOB} HTTP/1.1\r\nContent-Length: {len(too_big)}\r\nHost: docker\r\n\r\n".encode() + too_big)
        self.assertTrue(self.headed(out).startswith(b"HTTP/1.1 413"), out[:120])
        self.assertEqual(RoutingFakeBackend.received, [], "an oversized JSON body must never be forwarded")
        spec = sandbox_spec(**{"HostConfig.Tmpfs": {"/tmp": "size=" + "b" * 1024}})   # valid create, well below the cap
        blob = json.dumps(spec).encode()
        self.assertLess(len(blob), dpp.MAX_JSON_BODY)
        out = self.raw(f"POST /containers/create?name=prqa-sandbox-{JOB} HTTP/1.1\r\nContent-Length: {len(blob)}\r\nHost: docker\r\n\r\n".encode() + blob)
        self.assertTrue(self.headed(out).startswith(b"HTTP/1.1 200"), out[:120])
        self.assertEqual(len([r for r in RoutingFakeBackend.received if r["path"].startswith("/containers/create")]), 1)

    def test_07_denied_requests_with_bodies_never_reach_backend_and_keep_connection_consistent(self):
        bad_create = f"POST /containers/create?name=prqa-sandbox-{JOB} HTTP/1.1\r\nContent-Length: 2\r\nHost: docker\r\n\r\n".encode() + b"{}"  # labels missing
        exec_req = f"POST /containers/prqa-sandbox-{JOB}/exec HTTP/1.1\r\nContent-Length: 2\r\nHost: docker\r\n\r\n".encode() + b"{}"
        ping = b"GET /_ping HTTP/1.1\r\nHost: docker\r\nContent-Length: 0\r\n\r\n"
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(10)
        conn.connect(self.listen_path)
        try:
            for i, payload in enumerate([bad_create, ping, exec_req, ping]):
                conn.sendall(payload)
                resp = self.read_one_response(conn)
                status = resp.split(b"\r\n", 1)[0]
                self.assertTrue(status.startswith(b"HTTP/1.1 403") if i % 2 == 0 else status.startswith(b"HTTP/1.1 200"),
                                f"req {i}: {status!r}")
            non_ping = [r for r in RoutingFakeBackend.received if r["path"] != "/_ping"]
            self.assertEqual(len([r for r in RoutingFakeBackend.received if r["path"] == "/_ping"]), 2)
            self.assertEqual(non_ping, [], non_ping)
        finally:
            conn.close()

    def test_08_pipelined_deny_then_allow_stays_in_frame(self):
        bad = b"POST /containers/create?name=evil HTTP/1.1\r\nContent-Length: 2\r\nHost: docker\r\n\r\n" + b"{}"
        ping = b"GET /_ping HTTP/1.1\r\nHost: docker\r\nContent-Length: 0\r\n\r\n"
        out = self.raw(bad + ping, responses=2)
        # the 200's status line may be glued to the 403's body within one recv (no \r\n between a
        # Content-Length body and the next request's response) - assert on substrings, not line joins
        self.assertIn(b"HTTP/1.1 403", out)
        self.assertIn(b"HTTP/1.1 200 OK", out)
        self.assertEqual([r["path"] for r in RoutingFakeBackend.received], ["/_ping"])

    # ---------------------------------------------------------------- bare IDs --
    def test_09_bare_ids_resolve_and_bind_to_the_helper_before_policy(self):
        fake_id, foreign_id, other_job_id = "a" * 64, "b" * 64, "c" * 64
        RoutingFakeBackend.inspect_map = {
            fake_id: {"Name": f"/prqa-populate-{JOB}", "Config": {"Labels": {"prqa.job": JOB}}},
            foreign_id: {"Name": "prqa-n8n-1", "Config": {"Labels": {}}},
            other_job_id: {"Name": "/prqa-populate-20261022-other0001", "Config": {"Labels": {"prqa.job": "20261022-other0001"}}},
        }
        # nonexistent ID: resolution fails closed, the request is denied
        out = self.raw(f"GET /containers/{'d' * 64}/json HTTP/1.1\r\nHost: docker\r\nContent-Length: 0\r\n\r\n".encode())
        self.assertTrue(self.headed(out).startswith(b"HTTP/1.1 403"), out[:120])
        self.assertEqual(len(RoutingFakeBackend.received), 1, "only the resolution inspect ran")
        # unrelated container: never rewritten → denied
        RoutingFakeBackend.received = []
        out = self.raw(f"DELETE /containers/{foreign_id}?force=1 HTTP/1.1\r\nHost: docker\r\nContent-Length: 0\r\n\r\n".encode())
        self.assertTrue(self.headed(out).startswith(b"HTTP/1.1 403"), out[:120])
        self.assertFalse(any(r["method"] == "DELETE" for r in RoutingFakeBackend.received))
        # helper ID + allowed destination: rewritten to the job-named container and forwarded
        RoutingFakeBackend.received = []
        out = self.raw(f"PUT /containers/{fake_id}/archive?path=/work HTTP/1.1\r\nContent-Length: 3\r\nHost: docker\r\n\r\n".encode() + b"tar")
        self.assertTrue(self.headed(out).startswith(b"HTTP/1.1 200"), out[:120])
        forwarded = [r for r in RoutingFakeBackend.received if r["method"] == "PUT"]
        self.assertTrue(forwarded and forwarded[0]["path"].startswith(f"/containers/prqa-populate-{JOB}/archive"))
        # helper ID + forbidden destination: rewritten but still denied
        RoutingFakeBackend.received = []
        out = self.raw(f"PUT /containers/{fake_id}/archive?path=/etc HTTP/1.1\r\nContent-Length: 3\r\nHost: docker\r\n\r\n".encode() + b"tar")
        self.assertTrue(self.headed(out).startswith(b"HTTP/1.1 403"), out[:120])
        self.assertFalse(any(r["method"] == "PUT" for r in RoutingFakeBackend.received))
        # another job's container gets rewritten and stays policy-decidable on the RESOLVED name (documented design:
        # one launcher principal serves all jobs; sandbox hashes contain the impact of cross-job interference)
        RoutingFakeBackend.received = []
        out = self.raw(f"GET /containers/{other_job_id}/logs HTTP/1.1\r\nHost: docker\r\nContent-Length: 0\r\n\r\n".encode())
        self.assertTrue(self.headed(out).startswith(b"HTTP/1.1 200"), out[:120])
        forwarded = [r for r in RoutingFakeBackend.received if r["method"] == "GET" and r["path"] != f"/containers/{other_job_id}/json"]
        self.assertTrue(forwarded and forwarded[0]["path"].startswith("/containers/prqa-populate-20261022-other0001/logs"))

    def test_10_head_archive_stat_is_bound_to_the_helper_name_and_destination(self):
        for dest, ok in (("/work", True), ("/work/checkout.tar", True), ("/work/.", False), ("/workx", False), ("/work%0A", False)):
            p = POLICY
            d = dp.evaluate(p, "HEAD", f"/containers/prqa-populate-{JOB}/archive?path={dest}", b"")
            self.assertEqual(d.allowed, ok, dest)
            d = dp.evaluate(p, "HEAD", f"/containers/prqa-sandbox-{JOB}/archive?path={dest}", b"")
            self.assertFalse(d.allowed, f"stat must be helper-only: {dest}")


class CrossJobBehaviorTests(unittest.TestCase):
    """Residual threat-model pins (fake backend, no daemon): host-wide listings are label-filter-only, network
    inspect is limited to the permitted network, and — **as accepted by the single-principal design** — a job-named
    container/volume of ANY job stays reachable (start/stop/delete/logs), including via a resolved bare ID. These
    tests pin the *current* boundary; they fail if the accepted risk changes shape silently."""

    OTHER = "20261022-zzz999other"   # deliberate second job id

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        RoutingFakeBackend.received = []
        RoutingFakeBackend.inspect_map = {}

        class BServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.backend_path = os.path.join(self.tmp.name, "docker.sock")
        self.backend = BServer(self.backend_path, RoutingFakeBackend)
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        self.listen_path = os.path.join(self.tmp.name, "policy.sock")
        dpp.Handler.policy = POLICY
        dpp.Handler.upstream = self.backend_path
        self.proxy = dpp.UnixServer(self.listen_path, dpp.Handler)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()

    def tearDown(self):
        self.proxy.shutdown(); self.proxy.server_close(); self.backend.shutdown(); self.backend.server_close(); self.tmp.cleanup()

    def raw_path(self, method: str, target: str, body: bytes = b"") -> bytes:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(self.listen_path)
        out = b""
        try:
            s.sendall(f"{method} {target} HTTP/1.1\r\nHost: docker\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
            while b"\r\n\r\n" not in out:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
            header, _, rest = out.partition(b"\r\n\r\n")
            want = None
            for line in header.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    want = int(line.split(b":", 1)[1] or 0)
            if want is not None:
                while len(rest) < want:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    rest += chunk
            out = header + b"\r\n\r\n" + rest
        finally:
            s.close()
        return out

    def status(self, out: bytes) -> bytes:
        return out.split(b"\r\n", 1)[0] if out else b"<none>"

    def test_01_host_wide_container_listings_are_rewritten_to_prqa_labels(self):
        """Every forwarded /containers/json carries BOTH mandatory prqa labels injected by the proxy (never the
        caller's filter text): the daemon sees exactly one canonical filters= value plus the caller's other
        parameters, so a daemon change in filter parsing cannot widen the result set. Unusable or ambiguous
        caller filters fail closed."""
        forwarded_cases = {
            "/containers/json": URLQ({"label": [LABEL_JOB, LABEL_KIND]}),
            f"/containers/json?filters={URLQ({'label': [LABEL_JOB]})}": URLQ({"label": [LABEL_JOB, LABEL_KIND]}),
        }
        for target, expected in forwarded_cases.items():
            RoutingFakeBackend.received = []
            out = self.raw_path("GET", target)
            self.assertTrue(self.status(out).startswith(b"HTTP/1.1 200"), f"{target}: {out[:120]!r}")
            sent = [r for r in RoutingFakeBackend.received if r["path"].startswith("/containers/json")]
            self.assertEqual(len(sent), 1, f"{target}: {RoutingFakeBackend.received}")
            got_filters = sent[0]["path"].split("filters=", 1)[1] if "filters=" in sent[0]["path"] else ""
            self.assertEqual(got_filters, expected, f"{target} → {sent[0]['path']}")
            self.assertEqual(sent[0]["path"].count("filters="), 1, target)
        # reconciliation semantics survive: `all=1` (stopped containers) and other list parameters are preserved
        RoutingFakeBackend.received = []
        out = self.raw_path("GET", "/containers/json?all=1&limit=10&size=1")
        self.assertTrue(self.status(out).startswith(b"HTTP/1.1 200"), out[:120])
        sent_path = [r["path"] for r in RoutingFakeBackend.received if r["path"].startswith("/containers/json")][0]
        decoded = urllib.parse.unquote(sent_path)
        for token in ("all=1", "limit=10", "size=1", "prqa.job", "prqa.kind"):
            self.assertIn(token, decoded, sent_path)
        self.assertEqual(sent_path.count("filters="), 1)
        # fail closed: malformed / non-object / array-form filters and ambiguous (case-variant) spellings are refused
        for target in (f'/containers/json?filters={URLQ({"label": ["prqa.job"]})}&filters=garbage',
                       "/containers/json?filters=notjson",
                       '/containers/json?filters=%5B%22prqa.job%22%5D',
                       '/containers/json?filters=true',
                       "/containers/json?FILTERS=" + URLQ({"label": [LABEL_JOB, LABEL_KIND]})):
            RoutingFakeBackend.received = []
            out = self.raw_path("GET", target)
            self.assertTrue(self.status(out).startswith(b"HTTP/1.1 403"), f"{target}: {out[:120]!r}")
            self.assertEqual([r for r in RoutingFakeBackend.received if r["path"].startswith("/containers/json")],
                             [], f"{target} was forwarded")

    def test_02_volume_lists_are_narrowed_by_injected_labels(self):
        """`docker volume ls` asks for the whole list and filters client-side, so the proxy injects the
        prqa.job/prqa.kind=checkout labels into EVERY forwarded /volumes query instead of trusting the caller."""
        cases = {
            "/volumes": URLQ({"label": ["prqa.job", "prqa.kind=checkout"]}),
            "/volumes?all=1": URLQ({"label": ["prqa.job", "prqa.kind=checkout"]}),
            "/volumes?filters=%7B%22dangling%22%3A%5B%22true%22%5D%7D": URLQ({"dangling": ["true"], "label": ["prqa.job", "prqa.kind=checkout"]}),
            f"/volumes?filters={URLQ({'label': ['prqa.job', 'prqa.kind=checkout']})}": URLQ({"label": ["prqa.job", "prqa.kind=checkout"]}),
        }
        for target, expected in cases.items():
            RoutingFakeBackend.received = []
            out = self.raw_path("GET", target)
            self.assertTrue(self.status(out).startswith(b"HTTP/1.1 200"), f"{target}: {out[:120]!r}")
            sent = [r for r in RoutingFakeBackend.received if r["path"].startswith("/volumes")]
            self.assertEqual(len(sent), 1, f"{target}: {RoutingFakeBackend.received}")
            got_filters = sent[0]["path"].split("filters=", 1)[1] if "filters=" in sent[0]["path"] else ""
            self.assertIn("prqa.job", got_filters); self.assertIn("checkout", got_filters)
            if expected is not None:
                self.assertEqual(got_filters, expected, f"{target} → {sent[0]['path']}")

    def test_03_network_inspect_limited_to_the_permitted_sandbox_network(self):
        RoutingFakeBackend.received = []
        out = self.raw_path("GET", "/networks/foreign_default")
        self.assertTrue(self.status(out).startswith(b"HTTP/1.1 403"), out[:120])
        self.assertEqual([r for r in RoutingFakeBackend.received if r["path"].startswith("/networks/")], [])
        out = self.raw_path("GET", "/networks/prqa_sandbox")
        self.assertTrue(self.status(out).startswith(b"HTTP/1.1 200"), out[:120])

    def test_04_engine_info_is_refused(self):
        out = self.raw_path("GET", "/info")
        self.assertTrue(self.status(out).startswith(b"HTTP/1.1 403"), out[:120])

    def test_05_cross_job_by_name_is_accepted_by_design(self):
        """Pins the documented single-principal residual risk: name-based cross-job start/stop/delete stays allowed.
        If a future change adds per-job authorization, this test must be REWRITTEN to expect 403."""
        out = self.raw_path("POST", f"/containers/prqa-sandbox-{self.OTHER}/start")
        self.assertTrue(self.status(out).startswith(b"HTTP/1.1 200"), out[:120])
        self.assertEqual([r["path"] for r in RoutingFakeBackend.received], [f"/containers/prqa-sandbox-{self.OTHER}/start"])
        RoutingFakeBackend.received = []
        out = self.raw_path("DELETE", f"/containers/prqa-sandbox-{self.OTHER}?force=1")
        self.assertTrue(self.status(out).startswith(b"HTTP/1.1 204"), out[:120])
        RoutingFakeBackend.received = []
        out = self.raw_path("DELETE", f"/volumes/prqa-checkout-{self.OTHER}?force=1")
        self.assertTrue(self.status(out).startswith(b"HTTP/1.1 204"), out[:120])

    def test_06_bare_id_of_another_job_resolves_and_forwards_to_that_job(self):
        other_id = "a" * 64
        RoutingFakeBackend.inspect_map = {other_id: {"Name": f"/prqa-sandbox-{self.OTHER}",
                                                    "Config": {"Labels": {"prqa.job": self.OTHER, "prqa.kind": "sandbox"}}}}
        RoutingFakeBackend.received = []
        out = self.raw_path("GET", f"/containers/{other_id}/logs?stdout=1")
        self.assertTrue(self.status(out).startswith(b"HTTP/1.1 200"), out[:120])
        forwarded = [r["path"] for r in RoutingFakeBackend.received if "/logs" in r["path"]]
        self.assertEqual(forwarded, [f"/containers/prqa-sandbox-{self.OTHER}/logs?stdout=1"], "expected a cross-job rewrite (accepted-by-design)")

    def test_07_foreign_bare_id_costs_one_preauth_read_and_never_forwards(self):
        foreign_id = "e" * 64
        RoutingFakeBackend.inspect_map = {}          # the daemon (fake) does not know it
        RoutingFakeBackend.received = []
        out = self.raw_path("GET", f"/containers/{foreign_id}/json")
        self.assertTrue(self.status(out).startswith(b"HTTP/1.1 403"), out[:120])
        self.assertEqual(len(RoutingFakeBackend.received), 1, RoutingFakeBackend.received)
        self.assertEqual(RoutingFakeBackend.received[0]["path"], f"/containers/{foreign_id}/json")
        self.assertEqual(RoutingFakeBackend.received[0]["method"], "GET")


class TransportBoundTests(unittest.TestCase):
    """Synthetic resource-bound pins: client stall, oversized request line, header flood and a stalled backend.
    Short timeouts are monkeypatched; in production the same code paths ramp to 300 s and are bounded per-op."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        RoutingFakeBackend.received = []
        RoutingFakeBackend.inspect_map = {}

        class BServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.backend_path = os.path.join(self.tmp.name, "docker.sock")
        self.backend = BServer(self.backend_path, RoutingFakeBackend)
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        self.listen_path = os.path.join(self.tmp.name, "policy.sock")
        dpp.Handler.policy = POLICY
        dpp.Handler.upstream = self.backend_path
        self.proxy = dpp.UnixServer(self.listen_path, dpp.Handler)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.saved_timeout = dpp.Handler.timeout

    def tearDown(self):
        dpp.Handler.timeout = self.saved_timeout
        self.proxy.shutdown(); self.proxy.server_close(); self.backend.shutdown(); self.backend.server_close(); self.tmp.cleanup()

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(8)
        s.connect(self.listen_path)
        return s

    @staticmethod
    def robust_read(sock) -> bytes:
        buf = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if b"HTTP/1.1 " in buf and (b"\r\n\r\n" in buf or not chunk):
                    break
        except OSError:
            pass
        return buf

    def test_01_stalled_client_does_not_hold_the_handler_forever(self):
        dpp.Handler.timeout = 0.5
        s = self.connect()
        try:
            t0 = time.monotonic()
            s.sendall(b"GET /_ping HTTP/1.1\r\nHost: docker\r\n")   # header block never terminates
            got = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                got += chunk
            elapsed = time.monotonic() - t0
            self.assertEqual(got, b"", "an incomplete request must be silently timed out")
            self.assertLess(elapsed, 3.0, "connection should be closed by the socket timeout, not held")
        finally:
            s.close()
        # the server stays healthy afterwards
        s2 = self.connect()
        try:
            s2.sendall(b"GET /_ping HTTP/1.1\r\nHost: docker\r\nContent-Length: 0\r\n\r\n")
            reply = self.robust_read(s2)
            self.assertTrue(reply.startswith(b"HTTP/1.1 200"), reply[:80])
        finally:
            s2.close()

    def test_02_oversized_request_line_is_rejected_in_bounds(self):
        deadline = time.monotonic() + 5.0
        s = self.connect()
        try:
            s.sendall(b"GET /" + b"A" * 70000 + b" HTTP/1.1\r\nHost: docker\r\n\r\n")
            s.settimeout(5)
            buf = self.robust_read(s)
            self.assertLess(time.monotonic(), deadline, "no timely reply for an oversized request line")
            self.assertTrue(buf.startswith(b"HTTP/1.1 4"), buf[:80])
            self.assertEqual(RoutingFakeBackend.received, [])
        finally:
            s.close()

    def test_03_header_flood_is_rejected_without_forwarding(self):
        deadline = time.monotonic() + 5.0
        s = self.connect()
        try:
            head = b"GET /_ping HTTP/1.1\r\nHost: docker\r\n" + b"".join(b"X-Pad: x\r\n" for _ in range(200)) + b"\r\n"
            s.sendall(head)
            s.settimeout(5)
            buf = self.robust_read(s)
            self.assertLess(time.monotonic(), deadline)
            self.assertEqual(RoutingFakeBackend.received, [], "a flooded request must not be forwarded")
            # CPython >= 3.11 turns the header limit into a well-formed 400; a bare close is acceptable too
            self.assertTrue(buf == b"" or buf.startswith(b"HTTP/1.1 4"), buf[:80])
        finally:
            s.close()

    def test_04_stalled_backend_is_bounded_by_the_upstream_timeout(self):
        orig_handle = RoutingFakeBackend.__dict__["_handle"]

        def slow_handle(self_backend):
            time.sleep(5.0)
            orig_handle(self_backend)
        # do_* attrs were created before this test, so rebind every alias, not just `_handle`
        self.addCleanup(setattr, RoutingFakeBackend, "_handle", orig_handle)
        setattr(RoutingFakeBackend, "_handle", slow_handle)
        for name in ("do_GET", "do_POST", "do_PUT", "do_DELETE", "do_HEAD"):
            self.addCleanup(setattr, RoutingFakeBackend, name, getattr(RoutingFakeBackend, name))
            setattr(RoutingFakeBackend, name, slow_handle)
        orig_init = dpp.UnixHTTPConnection.__init__

        def fast_init(conn, path, timeout=300.0):
            orig_init(conn, path, timeout=0.8)
        dpp.UnixHTTPConnection.__init__ = fast_init
        self.addCleanup(setattr, dpp.UnixHTTPConnection, "__init__", orig_init)
        deadline = time.monotonic() + 4.0
        s = self.connect()
        try:
            s.sendall(b"GET /_ping HTTP/1.1\r\nHost: docker\r\nContent-Length: 0\r\n\r\n")
            s.settimeout(4)
            buf = self.robust_read(s)
            self.assertLess(time.monotonic(), deadline, "proxy hung past the upstream timeout")
            self.assertTrue(buf.startswith(b"HTTP/1.1 502"), buf[:80])
        finally:
            s.close()


class FilterInjectionBypassTests(unittest.TestCase):
    """Pins that the volume-filter injection and the container-list label requirement resist caller-controlled
    query encodings: repeated filters parameters (the daemon applies only the first one — live-verified), alternate
    path forms and odd JSON shapes fail closed, and the listen socket stays owner+group-only (optionally chowned to
    the launcher's gid for the phase2 deployment). Synthetic; no daemon contact."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        RoutingFakeBackend.received = []
        RoutingFakeBackend.inspect_map = {}

        class BServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.backend_path = os.path.join(self.tmp.name, "docker.sock")
        self.backend = BServer(self.backend_path, RoutingFakeBackend)
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        self.listen_path = os.path.join(self.tmp.name, "policy.sock")
        dpp.Handler.policy = POLICY
        dpp.Handler.upstream = self.backend_path
        self.proxy = dpp.UnixServer(self.listen_path, dpp.Handler)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()

    def tearDown(self):
        self.proxy.shutdown(); self.proxy.server_close(); self.backend.shutdown(); self.backend.server_close(); self.tmp.cleanup()

    def raw(self, target: str):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(self.listen_path)
        try:
            s.sendall(f"GET {target} HTTP/1.1\r\nHost: docker\r\nContent-Length: 0\r\n\r\n".encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        finally:
            s.close()
        status = buf.split(b"\r\n", 1)[0] if buf else b"<none>"
        return status, list(RoutingFakeBackend.received)

    def test_01_repeated_filters_parameter_is_refused(self):
        good = URLQ({"label": [LABEL_JOB, LABEL_KIND]})
        for target in (f"/containers/json?filters={good}&filters={good}", f"/volumes?filters={good}&filters={good}",
                       f"/volumes?filters={good}&filters=garbage"):
            RoutingFakeBackend.received = []
            status, sent = self.raw(target)
            self.assertTrue(status.startswith(b"HTTP/1.1 403"), f"{target}: {status!r}")
            self.assertEqual(sent, [], f"{target} was forwarded")

    def test_02_alternate_request_forms_fail_closed(self):
        """CPython's request-line parser collapses leading `//` before the handler runs (gh-87389), so an
        authority-shaped target is normalized AND the policy is evaluated on exactly that path; the invariant is
        that the daemon only ever receives the path the policy decided on — never an authority component."""
        good = URLQ({"label": [LABEL_JOB, LABEL_KIND]})
        refusals = ("/volumes/", "/v1.47/volumes/", "/v%6flumes?filters=" + good,
                    "/containers/json?FILTERS=" + good, "/x/volumes")
        for target in refusals:
            RoutingFakeBackend.received = []
            status, sent = self.raw(target)
            self.assertTrue(status.startswith(b"HTTP/1.1 4"), f"{target}: {status!r}")
            self.assertEqual(sent, [], f"{target} was forwarded")
        # bare container lists are allowed and forwarded ONLY in the narrowed form (labels injected by the proxy)
        RoutingFakeBackend.received = []
        status, sent = self.raw("/containers/json?size=1")
        self.assertTrue(status.startswith(b"HTTP/1.1 200"), status)
        self.assertTrue(all(r["path"].startswith("/containers/json") and "prqa.kind" in r["path"] and "prqa.job" in r["path"]
                            for r in sent), sent)
        # for volume lists, unrelated caller parameters are simply ignored — the proxy injects the
        # prqa labels into every query, so `?FILTERS=…` (uppercase) results in the same narrowed forward
        RoutingFakeBackend.received = []
        status, sent = self.raw("/volumes?FILTERS=" + good)
        self.assertTrue(status.startswith(b"HTTP/1.1 200"), status)
        self.assertTrue(all(r["path"].startswith("/volumes") and "prqa.kind%3Dcheckout" in r["path"] for r in sent), sent)
        # `//volumes` is normalized to `/volumes` by stdlib; if anything is forwarded it must be the narrowed form
        RoutingFakeBackend.received = []
        status, sent = self.raw("//volumes")
        self.assertTrue(status.startswith(b"HTTP/1.1 "), status)
        self.assertTrue(all(r["path"].startswith("/volumes") and "prqa.kind%3Dcheckout" in r["path"] for r in sent),
                        f"forwarded request must keep origin-form path with injected labels: {sent}")

    def test_03_policy_shape_rules_for_filters_values(self):
        good_list = URLQ({"label": [LABEL_JOB, LABEL_KIND]})
        good_map = URLQ({"label": {LABEL_JOB: "", LABEL_KIND: "checkout"}})
        self.assertTrue(ev("GET", f"/containers/json?filters={good_list}").allowed)
        self.assertTrue(ev("GET", f"/containers/json?filters={good_map}").allowed, "map form carries both label keys")
        self.assertTrue(ev("GET", f"/containers/json?filters={URLQ({'label': [LABEL_JOB, LABEL_KIND, 'other']})}").allowed)
        self.assertFalse(ev("GET", "/containers/json?filters=%5B%22prqa.job%22%5D").allowed, "JSON array value is not dict")
        self.assertFalse(ev("GET", '/containers/json?filters=true').allowed)
        # caller-only narrowing filters no longer need to carry BOTH keys: the proxy injects the missing
        # mandatory label, so the forwarded query is always narrowed to prqa-labelled resources
        d = ev("GET", '/containers/json?filters=' + URLQ({"label": {"prqa.job": "x"}}))
        self.assertTrue(d.allowed, "proxy injects the missing prqa.kind key label")
        self.assertIn('"prqa.kind"', urllib.parse.unquote(d.forwarded_path)); self.assertIn('"prqa.job=x"', urllib.parse.unquote(d.forwarded_path))
        d = ev("GET", '/containers/json?filters=' + URLQ({"label": ["prqa.job"]}))
        self.assertTrue(d.allowed, "proxy injects the missing prqa.kind list entry")
        self.assertIn('"prqa.kind"', urllib.parse.unquote(d.forwarded_path))
        self.assertFalse(ev("GET", "/volumes?filters=garbage").allowed)
        # a partial map is fine for volumes: the proxy always MERGES the missing prqa labels in (result is narrower)
        self.assertTrue(ev("GET", '/volumes?filters=' + URLQ({"label": {"prqa.job": ""}})).allowed)
        self.assertIn("prqa.kind=checkout", urllib.parse.unquote(ev("GET", '/volumes?filters=' + URLQ({"label": {"prqa.job": ""}})).forwarded_path))

    def test_03b_docker_cli_wire_format_bool_map_keeps_whole_terms(self):
        # what `docker volume ls --filter label=prqa.job --filter label=prqa.kind=checkout` actually sends: the key is
        # the whole term and the value is `true`. It used to become "prqa.job=True" (matching nothing), which made
        # reconcile list no orphans (tests/test_launcher_integration.py::test_6).
        def labels(path, filters):
            d = ev("GET", path + "?filters=" + URLQ(filters))
            self.assertTrue(d.allowed, filters)
            return json.loads(urllib.parse.parse_qs(urllib.parse.urlsplit(d.forwarded_path).query)["filters"][0])["label"]
        self.assertEqual(sorted(labels("/volumes", {"label": {LABEL_JOB: True, "prqa.kind=checkout": True}})),
                         sorted([LABEL_JOB, "prqa.kind=checkout"]))
        self.assertEqual(sorted(labels("/containers/json", {"label": {f"{LABEL_JOB}=20261022-abc0001": True}})),
                         sorted([f"{LABEL_JOB}=20261022-abc0001", LABEL_KIND]))   # still narrowed: prqa.kind injected
        for path in ("/volumes", "/containers/json"):
            out = labels(path, {"label": {"anything": True}})
            self.assertFalse(any(t.endswith("=True") for t in out), out)
            self.assertIn(LABEL_JOB, out)                                       # mandatory labels always present
        for bad in ({"x": 1}, {"x": None}, {"x": ["y"]}, {"x": {"y": True}}):
            with self.subTest(bad=bad):
                self.assertFalse(ev("GET", "/volumes?filters=" + URLQ({"label": bad})).allowed, "unknown value type fails closed")

    def test_04_injected_volume_query_stays_narrow_with_extra_filters(self):
        # the rewrite REPLACES only the filters value with one canonical filters= parameter; caller filters are
        # merged in and every other caller parameter is carried through verbatim (daemon-applied narrowing only)
        d = ev("GET", '/volumes?all=1&filters=' + URLQ({"label": ["driver-prqa"], "dangling": ["true"]}))
        self.assertTrue(d.allowed)
        decoded = urllib.parse.unquote(d.forwarded_path)
        self.assertIn("prqa.job", decoded); self.assertIn("prqa.kind=checkout", decoded)
        self.assertIn("driver-prqa", decoded); self.assertIn('"dangling"', decoded)
        self.assertIn("all=1", decoded, "non-filters caller parameters are preserved")
        self.assertTrue(d.forwarded_path.startswith("/volumes?"))
        self.assertEqual(d.forwarded_path.count("filters="), 1, "no caller-controlled query parameter survives the rewrite")

    def test_05_listen_socket_is_owner_group_only_and_supports_the_launcher_gid(self):
        path = os.path.join(self.tmp.name, "gid.sock")
        th = threading.Thread(target=dpp.serve, args=(path, self.backend_path, POLICY, os.getgid()), daemon=True)
        th.start()
        deadline = time.monotonic() + 3.0
        while not os.path.exists(path) and time.monotonic() < deadline:
            time.sleep(0.05)
        st = os.stat(path)
        self.assertEqual(st.st_mode & 0o777, 0o660, "socket must stay owner+group rw")
        self.assertEqual(st.st_gid, os.getgid(), "--socket-gid must chown the socket for the phase2 launcher")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))


    def test_06_listen_socket_never_appears_with_a_wider_mode(self):
        """Regression: serve() used to bind the socket at its final path and chmod it afterwards, so a client (or the
        test above, which polls for the path and then stats it) could see the socket at 0o777 & ~umask in between.
        chmod is slowed here to make that window deterministic; the socket must only ever appear at 0o660."""
        path = os.path.join(self.tmp.name, "race.sock")
        real_chmod = os.chmod
        chmod_done = threading.Event()

        def slow_chmod(target, mode, *a, **kw):
            time.sleep(0.4)
            real_chmod(target, mode, *a, **kw)
            chmod_done.set()
        seen = []
        with mock.patch.object(dpp.os, "chmod", slow_chmod):
            th = threading.Thread(target=dpp.serve, args=(path, self.backend_path, POLICY, os.getgid()), daemon=True)
            th.start()
            deadline = time.monotonic() + 3.0
            while not chmod_done.is_set() and time.monotonic() < deadline:
                try:
                    seen.append(os.stat(path).st_mode & 0o777)
                except FileNotFoundError:
                    pass
                time.sleep(0.005)
            self.assertTrue(chmod_done.wait(3.0))
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o660)
        self.assertEqual(sorted(set(seen) - {0o660}), [], "socket was reachable at its final path before chmod 0660")
        leftovers = [n for n in os.listdir(self.tmp.name) if n.startswith("race.sock.")]
        self.assertEqual(leftovers, [], "no temporary socket name may remain")


class ContainerListInjectionTests(unittest.TestCase):
    """Regression pins for the container-list rewrite (F2 hardening): EVERY forwarded /containers/json query is
    built by the proxy — one canonical filters= value carrying the mandatory prqa.job + prqa.kind labels merged
    narrowing-only with caller filters; other caller parameters ride along verbatim; unparseable/non-object JSON
    and any ambiguous (repeated or differently-cased) filters spelling are refused without forwarding. Synthetic:
    fake backend on a unix socket, no daemon contact."""

    OTHER = "20261023-aaa111other"   # deliberate second job id

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        RoutingFakeBackend.received = []
        RoutingFakeBackend.inspect_map = {}

        class BServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.backend_path = os.path.join(self.tmp.name, "docker.sock")
        self.backend = BServer(self.backend_path, RoutingFakeBackend)
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        self.listen_path = os.path.join(self.tmp.name, "policy.sock")
        dpp.Handler.policy = POLICY
        dpp.Handler.upstream = self.backend_path
        self.proxy = dpp.UnixServer(self.listen_path, dpp.Handler)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()

    def tearDown(self):
        self.proxy.shutdown(); self.proxy.server_close(); self.backend.shutdown(); self.backend.server_close(); self.tmp.cleanup()

    def raw(self, target: str):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(self.listen_path)
        try:
            s.sendall(f"GET {target} HTTP/1.1\r\nHost: docker\r\nContent-Length: 0\r\n\r\n".encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        finally:
            s.close()
        status = buf.split(b"\r\n", 1)[0] if buf else b"<none>"
        return status, list(RoutingFakeBackend.received)

    def forwarded_query(self) -> str:
        sent = [r["path"] for r in RoutingFakeBackend.received if r["path"].startswith("/containers/json")]
        self.assertEqual(len(sent), 1, RoutingFakeBackend.received)
        return urllib.parse.unquote(sent[0])

    def test_01_forwarded_query_carries_the_mandatory_labels_and_merges_callers_narrowing_only(self):
        RoutingFakeBackend.received = []
        status, _ = self.raw("/containers/json")
        self.assertTrue(status.startswith(b"HTTP/1.1 200"), status)
        query = self.forwarded_query()
        self.assertEqual(query.split("?", 1)[1].split("filters=", 1)[1],
                         json.dumps({"label": [LABEL_JOB, LABEL_KIND]}, separators=(",", ":")),
                         f"bare list must forward exactly the injected labels: {query}")
        self.assertEqual(query.count("filters="), 1)
        # value-form caller entries keep their value and suppress the bare-key re-injection (strict narrowing)
        RoutingFakeBackend.received = []
        status, _ = self.raw("/containers/json?filters=" + URLQ({"label": [f"{LABEL_JOB}={self.OTHER}", f"{LABEL_KIND}=sandbox"]}))
        self.assertTrue(status.startswith(b"HTTP/1.1 200"), status)
        query = self.forwarded_query()
        for token in (f"{LABEL_JOB}={self.OTHER}", f"{LABEL_KIND}=sandbox"):
            self.assertIn(token, query)
        self.assertNotIn(f'"{LABEL_JOB},"', query, "bare key must not be re-injected next to a value-form entry")
        self.assertNotIn(f'"{LABEL_KIND}"', query.replace(f'"{LABEL_KIND}=sandbox"', ""), query)
        self.assertEqual(query.count("filters="), 1)
        # callere map form: {job: OTHER, kind: ""} → job=OTHER (value kept) + kind key-exists
        RoutingFakeBackend.received = []
        status, _ = self.raw("/containers/json?filters=" + URLQ({"label": {LABEL_JOB: self.OTHER, LABEL_KIND: ""}}))
        self.assertTrue(status.startswith(b"HTTP/1.1 200"), status)
        query = self.forwarded_query()
        for token in (f'"{LABEL_JOB}={self.OTHER}"', f'"{LABEL_KIND}"'):
            self.assertIn(token, query)
        self.assertEqual(query.count("filters="), 1)
        # unrelated caller filter keys ride along verbatim (daemon applies them — narrowing only)
        RoutingFakeBackend.received = []
        status, _ = self.raw(f"/containers/json?filters={URLQ({'label': [LABEL_JOB], 'status': ['paused']})}")
        self.assertTrue(status.startswith(b"HTTP/1.1 200"), status)
        query = self.forwarded_query()
        for token in ('"status":["paused"]', f'"{LABEL_JOB}"', f'"{LABEL_KIND}"'):
            self.assertIn(token, query)
        self.assertEqual(query.count("filters="), 1)

    def test_02_percent_encoded_filter_keys_and_values_stay_canonical(self):
        # parse_qs and the daemon percent-decode keys identically: `%66ilters` IS the filters parameter
        RoutingFakeBackend.received = []
        status, _ = self.raw("/containers/json?%66ilters=" + URLQ({"label": [LABEL_JOB]}))
        self.assertTrue(status.startswith(b"HTTP/1.1 200"), status)
        query = self.forwarded_query()
        self.assertIn(f'"{LABEL_KIND}"', query)
        self.assertIn(f'"{LABEL_JOB}"', query)
        self.assertEqual(query.count("filters="), 1)
        self.assertNotIn("%66ilters", query, "forwarded key is the canonical spelling")

    def test_03_repeated_or_case_variant_filters_are_refused(self):
        good = URLQ({"label": [LABEL_JOB, LABEL_KIND]})
        for target in (f"/containers/json?filters={good}&filters={good}",       # repeated, even identical
                       f"/containers/json?filters={good}&filters=garbage",      # repeated with junk
                       f"/containers/json?filters={good}&FILTERS={good}",       # case-variant second opinion
                       f"/containers/json?filters={good}&fIlTeRs={good}"):      # mixed case
            RoutingFakeBackend.received = []
            status, sent = self.raw(target)
            self.assertTrue(status.startswith(b"HTTP/1.1 403"), f"{target}: {status!r}")
            self.assertEqual(sent, [], f"{target} was forwarded")

    def test_04_unrelated_parameter_names_are_not_ambiguity(self):
        # a genuinely unrelated parameter name is forwarded verbatim; only `filters` is rewritten
        RoutingFakeBackend.received = []
        status, _ = self.raw(f"/containers/json?GovernorFilter=x&filters={URLQ({'label': [LABEL_JOB]})}")
        self.assertTrue(status.startswith(b"HTTP/1.1 200"), status)
        query = self.forwarded_query()
        self.assertIn("GovernorFilter=x", query)
        self.assertIn(f'"{LABEL_KIND}"', query)
        self.assertEqual(query.count("filters="), 1)

    def test_05_malformed_or_non_object_filters_fail_closed(self):
        for target in ("/containers/json?filters=notjson",
                       "/containers/json?filters=%7B%22label%22%3A%22notalist%22%7D",   # label: string
                       "/containers/json?filters=%5B%5D",                              # JSON array object
                       "/containers/json?filters=%22string%22",                        # JSON string
                       "/containers/json?filters=true"):
            RoutingFakeBackend.received = []
            status, sent = self.raw(target)
            self.assertTrue(status.startswith(b"HTTP/1.1 403"), f"{target}: {status!r}")
            self.assertEqual(sent, [], f"{target} was forwarded")

    def test_06_forwarded_path_is_always_origin_form(self):
        # a version prefix is stripped and replaced by the canonical narrowed query (same as the volume rewrite)
        RoutingFakeBackend.received = []
        status, _ = self.raw("/v1.47/containers/json?all=1&limit=10&size=1")
        self.assertTrue(status.startswith(b"HTTP/1.1 200"), status)
        sent = [r["path"] for r in RoutingFakeBackend.received if r["path"].startswith("/containers/json")][0]
        self.assertTrue(sent.startswith("/containers/json?"), sent)
        decoded = urllib.parse.unquote(sent)
        for token in ("all=1", "limit=10", "size=1", f'"{LABEL_JOB}"', f'"{LABEL_KIND}"'):
            self.assertIn(token, decoded, sent)
        self.assertEqual(sent.count("filters="), 1, sent)
        # stdlib's request-line normalisation collapses a leading //; the forwarded form stays origin-form
        RoutingFakeBackend.received = []
        status, _ = self.raw("//containers/json")
        self.assertTrue(status.startswith(b"HTTP/1.1 200"), status)
        sent = [r["path"] for r in RoutingFakeBackend.received if r["path"].startswith("/containers/json")][0]
        self.assertTrue(sent.startswith("/containers/json?") and not sent.startswith("//"), sent)
        self.assertIn(f'"{LABEL_KIND}"', urllib.parse.unquote(sent))


if __name__ == "__main__":
    unittest.main()
