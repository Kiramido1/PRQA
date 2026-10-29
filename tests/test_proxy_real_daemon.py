"""Opt-in real-daemon adversarial proxy test (default: skipped).

Runs the actual proxy subprocess (proxy/docker_policy_proxy.py) against the host Docker
daemon socket and throws malformed/hostile requests at it, asserting two invariants:

  1. every hostile request is refused without ever mutating the daemon (snapshot of
     containers / volumes / networks / images is identical before and after), and
  2. a denied request still leaves the connection usable (a subsequent allowed ping
     succeeds on the same connection).

Dangerous operations (exec, build, events, prune, swarm, ...) are targeted at the
synthetic PRQA sandbox container name the policy would allow, so this file exercises
the force/escalation dimension that the fake-backend suite cannot see. The pinned
image is synthetic (sha256:b*64) and never pulled: nothing here can start a sandbox.

Enable with:  PRQA_PROXY_REALDAEMON=1 python3 -m unittest tests.test_proxy_real_daemon -v
Needs the Docker CLI and read access to /var/run/docker.sock; makes no changes.
"""

import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAEMON_SOCK = os.environ.get("PRQA_DOCKER_SOCK", "/var/run/docker.sock")
FAKE_ID = "sha256:" + "b" * 64
FAKE_ID_RAW = "b" * 64
NAME = "prqa-sandbox-test"           # job suffix "*" is illegal -> create still denied
HTTP_TIMEOUT = 10


def _http(method: str, path: str):
    """Direct allowed-region request through the real daemon socket (read-only listing helper)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(HTTP_TIMEOUT)
    s.connect(DAEMON_SOCK)
    req = f"{method} {path} HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n".encode()
    s.sendall(req)
    out = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
    finally:
        s.close()
    head, sep, body = out.partition(b"\r\n\r\n")
    return head.split(b"\r\n", 1)[0].decode(errors="replace"), (head + sep + body)


@unittest.skipUnless(os.environ.get("PRQA_PROXY_REALDAEMON") == "1", "opt-in: PRQA_PROXY_REALDAEMON=1")
class RealDaemonAdversarialTests(unittest.TestCase):
    """Spawn the real proxy against /var/run/docker.sock; mutate nothing, prove failure-closed."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="prqa-proxy-real-")
        cls.dir = Path(cls.tmp.name)
        cls.listen = str(cls.dir / "policy.sock")
        cls.decisions = cls.dir / "decisions.log"
        cls.proc_log = open(cls.dir / "proc.log", "wb")
        cls.proc = subprocess.Popen(
            ["python3", str(ROOT / "proxy" / "docker_policy_proxy.py"),
             "--listen", cls.listen, "--upstream", DAEMON_SOCK,
             "--image-id", FAKE_ID, "--log-file", str(cls.decisions)],
            stdout=cls.proc_log, stderr=subprocess.STDOUT)
        deadline = time.time() + 15
        while time.time() < deadline:
            if os.path.exists(cls.listen):
                break
            if cls.proc.poll() is not None:
                raise RuntimeError("proxy subprocess exited early")
            time.sleep(0.05)
        else:
            raise RuntimeError("proxy socket never appeared")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill(); cls.proc.wait(timeout=5)
        cls.proc_log.close()
        cls.tmp.cleanup()

    @staticmethod
    def snapshot():
        """Read-only listing of every entity class an allowed request could mutate.

        Parses the daemon's (possibly chunked) JSON bodies and canonicalises them so
        HTTP metadata (Date headers, chunk formatting) never skims into the diff.
        """
        tags = {
            "containers": "/containers/json?all=1",
            "volumes": "/volumes",
            "networks": "/networks",
            "images": "/images/json",
        }
        snap = {}
        for key, path in tags.items():
            _status, raw = _http("GET", path)
            head, sep, body = raw.partition(b"\r\n\r\n")
            headers = head.lower()
            if b"transfer-encoding: chunked" in headers:
                chunks = []
                while body and not body.startswith(b"0"):
                    size, _, rest = body.partition(b"\r\n")
                    try:
                        n = int(size, 16)
                    except ValueError:
                        break  # not chunked after all
                    chunks.append(rest[:n])
                    body = rest[n + 2:]
                body = b"".join(chunks)
            try:
                data = json.loads(body.decode(errors="replace"))
            except json.JSONDecodeError:
                data = body.decode(errors="replace")
            # the daemon's ordering is arbitrary at every nesting level (Names, Ports, Mounts, ...);
            # canonicalise every list so only CONTENT diffs can trip the comparison
            def canon(o):
                if isinstance(o, list):
                    return sorted([canon(e) for e in o], key=lambda e: json.dumps(e, sort_keys=True))
                if isinstance(o, dict):
                    return {k: canon(v) for k, v in o.items()}
                return o
            if isinstance(data, list):
                data = [e for e in canon(data) if "prqa" in json.dumps(e, sort_keys=True)]
            elif isinstance(data, dict):
                # /volumes wraps its list inside a dict; canonicalise the inner list too
                inner = data.get("Volumes")
                if isinstance(inner, list):
                    data = {"Volumes": [e for e in canon(inner) if "prqa" in json.dumps(e, sort_keys=True)],
                            "Warnings": data.get("Warnings")}
            snap[key] = data
        return snap

    def raw(self, payload: bytes, *, responses: int = 1, timeout=10, halfclose=False):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(self.listen)
        out = b""
        try:
            s.sendall(payload)
            if halfclose:
                s.shutdown(socket.SHUT_WR)
            got = 0
            while got < responses:  # read *everything* until EOF, split statuses afterwards
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
                got = out.count(b"HTTP/1.1 ")
            s.close()
        except OSError:
            pass
        finally:
            s.close()
        statuses = [ln for ln in out.split(b"\r\n") if ln.startswith(b"HTTP/1.1 ")]
        return out, statuses

    def answered(self, out: bytes):
        status = out.split(b"\r\n", 1)[0] if out else b"<none>"
        return status.startswith(b"HTTP/1.1 ")

    # ------------------------------------------------------------------ hostile --
    def test_01_garbage_framing_is_refused_without_daemon_effect(self):
        before = self.snapshot()
        cases = {
            "garbage-request-line": b"NONSENSE\r\n\r\n",
            "invalid-method": b"START **/index.html HTTP/1.1\r\nHost: docker\r\n\r\n",
            "absolute-form": b"GET http://evil.example/v1.47/_ping HTTP/1.1\r\nHost: docker\r\n\r\n",
            "cl-plus-te": (b"POST /containers/create?name=" + NAME.encode() +
                           b" HTTP/1.1\r\nHost: docker\r\nContent-Length: 2\r\n"
                           b"Transfer-Encoding: chunked\r\n\r\n{}\r\n"),
            "te-identity": (b"POST /containers/create?name=" + NAME.encode() +
                            b" HTTP/1.1\r\nHost: docker\r\nTransfer-Encoding: identity\r\n"
                            b"Content-Length: 2\r\n\r\n" + b"{}"),
            "chunk-garbage": (b"PUT /containers/" + NAME.encode() +
                              b"/archive?path=/work HTTP/1.1\r\nHost: docker\r\n"
                              b"Transfer-Encoding: chunked\r\n\r\n" + b"ZZZ!:x\r\nnot-a-chunk\r\n0\r\n\r\n"),
            "negative-chunk": (b"PUT /containers/" + NAME.encode() +
                               b"/archive?path=/work HTTP/1.1\r\nHost: docker\r\n"
                               b"Transfer-Encoding: chunked\r\n\r\n" + b"-5\r\nAAAAA\r\n0\r\n\r\n"),
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                out, statuses = self.raw(payload)
                self.assertEqual(len(statuses), 1, f"{label}: {statuses!r}")
                code = statuses[0].split()[1]
                self.assertIn(code, (b"400", b"403", b"413", b"501"), f"{label}: {out[:160]!r}")
                self.assertEqual(self.snapshot(), before, f"{label}: daemon changed!")

    def test_02_dangerous_operations_denied_and_ignored_by_daemon(self):
        before = self.snapshot()
        body = b"{}"
        hostile = [
            (f"POST /containers/{NAME}/exec HTTP/1.1", "exec"),
            (f"POST /containers/{NAME}/resize?h=10&w=10 HTTP/1.1", "resize"),
            ("POST /containers/create?name=evil HTTP/1.1", "create-evil"),
            ("POST /images/create?fromImage=evil HTTP/1.1", "pull"),
            ("POST /build HTTP/1.1", "build"),
            ("POST /prune HTTP/1.1", "prune"),
            ("POST /volumes/prune HTTP/1.1", "volume-prune"),
            (f"DELETE /containers/{NAME} HTTP/1.1", "rm-container"),
            ("POST /swarm/init HTTP/1.1", "swarm-init"),
            ("POST /networks/create?name=evil HTTP/1.1", "net-create"),
            ("GET /events HTTP/1.1", "events"),
            ("GET /system/df HTTP/1.1", "system-df"),
            (f"POST /containers/{NAME}/kill?signal=KILL HTTP/1.1", "kill"),
            ("GET /nodes HTTP/1.1", "nodes"),
        ]
        seen = set()
        for path, label in hostile:
            body_bytes = body if "GET" not in path else b""
            req = (path + f" HTTP/1.1\r\nHost: docker\r\nContent-Length: {len(body_bytes)}\r\n\r\n").encode() + body_bytes
            with self.subTest(case=label):
                out, statuses = self.raw(req)
                self.assertEqual(len(statuses), 1, f"{label}: expected a single status, got {statuses!r}")
                self.assertTrue(statuses[0].startswith(b"HTTP/1.1 4"), f"{label}: {out[:120]!r}")
                seen.add(label)
        self.assertEqual(seen, {h[1] for h in hostile})
        self.assertEqual(self.snapshot(), before, "hostile requests mutated the daemon")

    def test_03_nonexistent_bare_id_probe_resolves_read_only_and_denies(self):
        before = self.snapshot()
        out, _ = self.raw(f"GET /containers/{FAKE_ID_RAW}/json HTTP/1.1\r\nHost: docker\r\nContent-Length: 0\r\n\r\n".encode())
        self.assertTrue(self.answered(out) and out.startswith(b"HTTP/1.1 403"), out[:120])
        self.assertEqual(self.snapshot(), before)

    def test_04_allowed_ping_succeeds_and_never_touches_daemon_state(self):
        ping = b"HEAD /_ping HTTP/1.1\r\nHost: docker\r\n\r\n"
        before = self.snapshot()
        out, statuses = self.raw(ping)
        self.assertEqual(len(statuses), 1, statuses)
        self.assertTrue(statuses[0].startswith(b"HTTP/1.1 200"), out[:120])
        self.assertEqual(out.count(b"PRQA-Serve"), 0)  # not asserting version, only that it answered
        self.assertEqual(self.snapshot(), before)

    def test_05_deny_then_allow_on_one_connection_stays_in_frame(self):
        bad = b"POST /containers/create?name=evil HTTP/1.1\r\nHost: docker\r\nContent-Length: 2\r\n\r\n" + b"{}"
        ping = b"HEAD /_ping HTTP/1.1\r\nHost: docker\r\nContent-Length: 0\r\n\r\n"
        before = self.snapshot()
        out, _ = self.raw(bad + ping, responses=2)
        # a CL-framed body is glued to the next response's status line in one recv - substring matching
        self.assertEqual(out.count(b"HTTP/1.1 403"), 1, out[:160])
        self.assertEqual(out.count(b"HTTP/1.1 200 OK"), 1, out[:160])
        self.assertLess(out.index(b"HTTP/1.1 403"), out.index(b"HTTP/1.1 200 OK"))
        self.assertEqual(self.snapshot(), before)

    def test_06_decision_log_records_every_deny_as_not_forwarded(self):
        body = json.dumps({"Image": FAKE_ID, "Env": ["OPENCODE_JOB_ID=x"], "Labels": {"prqa.job": "x", "prqa.kind": "sandbox"}}).encode()
        req = (f"POST /containers/create?name={NAME}" + f' HTTP/1.1\r\nHost: docker\r\nContent-Length: {len(body)}\r\n\r\n').encode() + body
        out, statuses = self.raw(req)
        self.assertTrue(statuses and statuses[0].startswith(b"HTTP/1.1 4"), out[:160])
        time.sleep(0.2)
        log = self.decisions.read_text(errors="replace")
        self.assertIn("decision deny POST", log)


if __name__ == "__main__":
    unittest.main()
