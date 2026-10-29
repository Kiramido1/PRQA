"""Phase-2 container integration and launcher egress isolation (opt-in: PRQA_PHASE2_CONTAINERS=1).

DisposablePhase2StackTests brings up a DISPOSABLE compose project (`prqa-p2it-<tag>`) from the real
compose/docker-compose.yml plus a generated override, next to — never instead of — the persistent `prqa` project:

  * prqa-worker        production worker code (read-only bind of worker/) in the prqa/worker image, started through
                       tests/phase2_stack/worker_entry.py: test-only live_env (placeholder repository approved,
                       synthetic tokens) and a synthetic git checkout, so a live job reaches launch_state=pending offline
  * prqa-mocks         the mock GitHub / prompt-LLM / Discord sinks (unchanged)
  * prqa-docker-proxy  the real policy proxy, pinned to a stub sandbox image built offline (never the OpenCode image)
  * prqa-launcher      the built prqa/launcher:0.1.0 image with its compose hardening and its compose network

Override deviations (all toward isolation): no published ports; the project's prqa_internal network is internal too, so
nothing in the disposable stack has a route off the host; the sandbox image is the disposable stub. n8n and the model
proxy are not started. No real credential is passed (the env file holds synthetic values only).

Checks: the launcher's network membership, routes and reachability (a host listener with a positive control, not
timeouts alone), no Docker daemon access except the policy socket, proxy denials with their specific reasons, no
secret or credential in the launcher, the synthetic flow label event → fixture context → prompt build → mock LLM →
gate + rubric (and the fallback) → live job → launcher → stub sandbox through the proxy → result validation → report →
Discord / GitHub at the mock sinks, and label-based orphan cleanup. Teardown removes only the project's own resources
and the stub image, then compares the daemon's containers, volumes and networks with the baseline.

PersistentWorkerBoundaryTests talks to the persistent review-only worker on 127.0.0.1:8080 (read-mostly: prompt
build/finalize and validation endpoints; no job, no n8n execution): auth, malformed input, budget 3000, gate and
rubric rejection → fallback, mock-only endpoints.

    PRQA_PHASE2_CONTAINERS=1 PYTHONPATH=worker:. python3 -m unittest tests.test_phase2_containers -v
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "worker"), str(ROOT)]

from tests.test_launch_lifecycle import LT, WT, live_job_request  # noqa: E402

ENABLED = os.environ.get("PRQA_PHASE2_CONTAINERS") == "1" and shutil.which("docker") is not None
REAL_SOCK = "/var/run/docker.sock"
COMPOSE = ROOT / "compose" / "docker-compose.yml"
TAG = f"{os.getpid()}{int(time.time()) % 100000}"
PROJECT = f"prqa-p2it-{TAG}"
STUB_TAG = f"prqa/launcher-test:stub-p2it-{TAG}"
OPENCODE_TAG = "prqa/opencode-sandbox:local"             # the real OpenCode image: must never be started here
SECRETS_DIR = Path.home() / ".config" / "prqa"
CREDENTIAL_NAMES = ("GROQ_API_KEY", "PROMPT_LLM_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN", "DISCORD_WEBHOOK_URL",
                    "PRQA_WORKER_TOKEN", "N8N_ENCRYPTION_KEY", "GITHUB_WEBHOOK_SECRET", "PRQA_WEBHOOK_SECRET")
UNREACHABLE = ("ENETUNREACH", "EHOSTUNREACH")      # definitive local errors: no route / no such host on the link
FIX = ROOT / "fixtures" / "synthetic"
SANDBOX_NET = f"{PROJECT}_prqa_sandbox"
CLAIM_MODEL = "prqa/openai/gpt-oss-120b"                    # the worker's default OpenCode model (provider "prqa" = the model proxy)
UPSTREAM_MODEL = "openai/gpt-oss-120b"                       # what the launcher's token binds and the proxy forwards to (fake) Groq
SANDBOX_OPENCODE_ENV = {"HOME", "PATH", "LANG", "TMPDIR", "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_PURE", "OPENCODE_DISABLE_MODELS_FETCH",
                        "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_LSP_DOWNLOAD", "OPENCODE_DISABLE_DEFAULT_PLUGINS",
                        "OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_EXTERNAL_SKILLS", "OPENCODE_CONFIG_CONTENT", "PRQA_MODEL_TOKEN",
                        "PRQA_JOB_ID", "PRQA_EVIDENCE_DIR", "PRQA_JOB_JSON", "PRQA_TEST_LEVEL", "BASE_URL", "TOKEN"}
P2IT_ADMIN = "p2it-admin-token-SYNTHETIC-0123456789abcdef"            # proxy only (never the launcher)
P2IT_MINT = "p2it-mint-token-SYNTHETIC-0123456789abcdef0123"         # the launcher's restricted credential
P2IT_PROMPT = "p2it-prompt-token-SYNTHETIC-0123456789abcdef01"       # n8n's prompt-generation credential
P2IT_PROVIDER_KEY = "sk-or-v1-P2ITSYNTHETICPROVIDERKEY00000000000000000000000000"   # synthetic; mocked upstream only
EVIDENCE: dict = {"project": PROJECT, "probes": {}, "flow": {}, "policy": {}, "cleanup": {}}

# HTTP client run INSIDE a container (stdin: {url, method, body?, headers?}); stdlib only, prints {status, body}
CLIENT = r"""
import json, sys, urllib.request, urllib.error
r = json.load(sys.stdin)
data = None if r.get("body") is None else json.dumps(r["body"]).encode()
req = urllib.request.Request(r["url"], data=data, method=r.get("method", "GET"), headers={"Content-Type": "application/json", **r.get("headers", {})})
try:
    with urllib.request.urlopen(req, timeout=r.get("timeout", 30)) as resp:
        st, raw = resp.status, resp.read()
except urllib.error.HTTPError as e:
    st, raw = e.code, e.read()
try:
    body = json.loads(raw) if raw else None
except ValueError:
    body = {"_text": raw.decode("utf-8", "replace")}
print(json.dumps({"status": st, "body": body}))
"""

# network probe run INSIDE the launcher: routing table, name resolution and TCP connects with errno + elapsed time
PROBE = r"""
import errno, json, socket, sys, time
spec = json.loads(sys.argv[1]); out = {"routes": [], "routes6_default": False, "resolve": {}, "connect": {}}
for line in open("/proc/net/route").read().splitlines()[1:]:
    f = line.split(); out["routes"].append({"iface": f[0], "dest": f[1], "gateway": f[2], "mask": f[7]})
try:
    for line in open("/proc/net/ipv6_route").read().splitlines():
        f = line.split()
        if f[0] == "0" * 32 and f[1] == "00" and f[9] != "lo":
            out["routes6_default"] = True
except OSError:
    pass
for name in spec.get("resolve", []):
    try:
        out["resolve"][name] = sorted({a[4][0] for a in socket.getaddrinfo(name, None)})
    except socket.gaierror as e:
        out["resolve"][name] = "GAIERROR:" + str(e.errno)
for host, port in spec.get("connect", []):
    t = time.time()
    try:
        socket.create_connection((host, port), timeout=spec.get("timeout", 4)).close(); res = "CONNECTED"
    except socket.timeout:
        res = "TIMEOUT"
    except OSError as e:
        res = errno.errorcode.get(e.errno, str(e))
    out["connect"][f"{host}:{port}"] = {"result": res, "elapsed_s": round(time.time() - t, 2)}
print(json.dumps(out))
"""


# listening sockets of a network namespace (LISTEN entries of /proc/net/tcp{,6}) + the admin socket's metadata
LISTEN = r"""
import json, os, socket, stat
def addrs(path, v6):
    out = []
    try:
        lines = open(path).read().splitlines()[1:]
    except OSError:
        return out
    for line in lines:
        f = line.split()
        if f[3] != "0A":
            continue
        ip_hex, port_hex = f[1].split(":")
        raw = bytes.fromhex(ip_hex)
        if v6:
            ip = socket.inet_ntop(socket.AF_INET6, b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4)))
        else:
            ip = socket.inet_ntoa(raw[::-1])
        out.append(f"{ip}:{int(port_hex, 16)}")
    return sorted(out)
st = os.lstat("/run/prqa-model-admin/admin.sock")
print(json.dumps({"tcp": addrs("/proc/net/tcp", False), "tcp6": addrs("/proc/net/tcp6", True),
                  "admin_socket": {"mode": oct(stat.S_IMODE(st.st_mode)), "gid": st.st_gid, "is_socket": stat.S_ISSOCK(st.st_mode)}}))
"""

# the launcher's view of the ADMIN interface: its own client code against the shared unix socket
ADMIN_FROM_LAUNCHER = r"""
import json, os, sys
sys.path.insert(0, "/app")
from prqa_worker.launcher_service import _UnixHTTPConnection
def call(method, path, token=None, body=None):
    c = _UnixHTTPConnection("/run/prqa-model-admin/admin.sock", 10)
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
    r = c.getresponse(); raw = r.read(); c.close()
    return [r.status, json.loads(raw or b"null")]
tok = os.environ["PRQA_MODEL_PROXY_MINT_TOKEN"]
print(json.dumps({"healthz": call("GET", "/healthz"), "list": call("GET", "/admin/tokens", tok),
                  "list_without_token": call("GET", "/admin/tokens"),
                  "reserved_prompt_job": call("POST", "/admin/tokens", tok, {"job_id": "prompt-escalate", "model": sys.argv[1]}),
                  "admin_token_in_env": "PRQA_MODEL_PROXY_ADMIN_TOKEN" in os.environ,
                  "inference_on_admin": call("POST", "/v1/chat/completions", tok, {"model": "m", "messages": []})}))
"""

# the operator's view (docker exec into the proxy, which alone holds the admin token): full listing
ADMIN_FROM_PROXY = r"""
import json, os, socket
s = socket.socket(socket.AF_UNIX); s.connect("/run/prqa-model-admin/admin.sock")
s.sendall(("GET /admin/tokens HTTP/1.1\r\nHost: x\r\nConnection: close\r\nAuthorization: Bearer "
           + os.environ["PRQA_MODEL_PROXY_ADMIN_TOKEN"] + "\r\n\r\n").encode())
raw = b""
while True:
    chunk = s.recv(65536)
    if not chunk:
        break
    raw += chunk
head, _, body = raw.partition(b"\r\n\r\n")
print(json.dumps([int(head.split()[1]), json.loads(body)]))
"""

# a sandbox-equivalent client on the sandbox network: what the untrusted side can and cannot do
SANDBOX_SIDE = r"""
import errno, json, os, socket, sys, urllib.request, urllib.error
spec = json.loads(sys.argv[1]); out = {"http": {}, "connect": {}, "resolve": {}}
def http(method, url, token=None, body=None, extra=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {}),
                                          **(extra or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return [r.status, json.loads(r.read() or b"null")]
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return [e.code, json.loads(raw)]
        except ValueError:
            return [e.code, raw[:200].decode("utf-8", "replace")]
for name, (method, url, token, body, *extra) in spec["http"].items():
    out["http"][name] = http(method, url, token, body, *extra)
for host, port in spec["connect"]:
    try:
        socket.create_connection((host, port), timeout=4).close(); res = "CONNECTED"
    except socket.timeout:
        res = "TIMEOUT"
    except OSError as e:
        res = errno.errorcode.get(e.errno, str(e))
    out["connect"][f"{host}:{port}"] = res
for name in spec["resolve"]:
    try:
        out["resolve"][name] = sorted({a[4][0] for a in socket.getaddrinfo(name, None)})
    except socket.gaierror as e:
        out["resolve"][name] = "GAIERROR:" + str(e.errno)
out["admin_dir_visible"] = os.path.exists("/run/prqa-model-admin")
out["env_names"] = sorted(os.environ)
print(json.dumps(out))
"""


def real(argv, *, timeout=120, stdin=None):
    p = subprocess.run(["docker", "-H", "unix://" + REAL_SOCK, *argv], capture_output=True, text=True, timeout=timeout,
                       check=False, input=stdin)
    return p.returncode, p.stdout, p.stderr


def snapshot():
    out = {}
    for kind, argv in (("containers", ["ps", "-a", "--format", "{{.Names}}"]), ("volumes", ["volume", "ls", "--format", "{{.Name}}"]),
                       ("networks", ["network", "ls", "--format", "{{.Name}}"]), ("images", ["image", "ls", "-aq", "--no-trunc"])):
        out[kind] = set(real(argv)[1].split())
    return out


class HostListener:
    """A TCP listener on every host address (0.0.0.0, ephemeral port) that records who connected: the positive evidence
    behind 'unreachable' (a timeout alone proves nothing)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", 0)); self.sock.listen(16); self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self.peers: list[str] = []
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self._stop:
            try:
                c, addr = self.sock.accept()
                self.peers.append(addr[0]); c.close()
            except OSError:
                continue

    def close(self):
        self._stop = True
        self.sock.close()


def compose_env(stub_id: str, key_file: str, budget_dir: str) -> str:
    """Synthetic values only: no provider, GitHub or Discord credential exists in the disposable stack. The model proxy
    gets a synthetic key file and a LOOPBACK fake upstream (its own network namespace): no provider is reachable."""
    return "\n".join([
        "N8N_IMAGE_TAG=unused-disposable", "N8N_ENCRYPTION_KEY=synthetic-unused-0000000000000000",
        f"PRQA_WORKER_TOKEN={WT}", f"PRQA_LAUNCHER_TOKEN={LT}", f"PRQA_SANDBOX_IMAGE_ID={stub_id}",
        f"PRQA_SANDBOX_NETWORK={SANDBOX_NET}", "PRQA_LAUNCHER_GID=10002", "AUTOMATION_MODE=review_only",
        "PRQA_MODEL_PROXY_INFERENCE_URL=http://prqa-model-proxy:8089/v1", "PRQA_MODEL_PROXY_ADMIN_URL=unix:///run/prqa-model-admin/admin.sock",
        f"PRQA_MODEL_PROXY_ADMIN_TOKEN={P2IT_ADMIN}", f"PRQA_MODEL_PROXY_MINT_TOKEN={P2IT_MINT}",
        f"PRQA_MODEL_PROXY_PROMPT_TOKEN={P2IT_PROMPT}", f"PRQA_MODEL_PROVIDER_KEY_FILE={key_file}", f"PRQA_MODEL_PROXY_UID={os.getuid()}",
        # a private, disposable ledger: the proxy's budget path runs exactly as in production (generous synthetic caps)
        f"PRQA_PROVIDER_BUDGET_DIR={budget_dir}", "PRQA_BUDGET_DAILY_REQUESTS=200", "PRQA_BUDGET_DAILY_TOKENS=10000000",
        "PRQA_BUDGET_JOB_TOKENS=1000000", "PRQA_BUDGET_TOKENS_PER_MINUTE=1000000", "PRQA_BUDGET_MAX_IN_FLIGHT=1",
        "PRQA_BUDGET_PROXY_BYTES_PER_TOKEN=4.5", "PRQA_BUDGET_MAX_WAIT_S=10", ""])


def compose_override(stub_id: str) -> str:
    q = json.dumps
    binds = "".join(f"      - type: bind\n        source: {q(str(ROOT / d))}\n        target: /repo/{d}\n        read_only: true\n"
                    for d in ("worker", "tests", "config", "schemas", "prompts", "fixtures"))
    proxy_cmd = ["python3", "-I", "/app/docker_policy_proxy.py", "--listen", "/run/prqa/docker-policy.sock", "--upstream",
                 "/var/run/docker.sock", "--image-id", stub_id, "--image-tag", STUB_TAG, "--network", SANDBOX_NET, "--socket-gid", "10002"]
    return f"""# generated by tests/test_phase2_containers.py for the disposable project {PROJECT}
services:
  prqa-worker:
    ports: !reset []
    restart: "no"
    command: ["python3", "/repo/tests/phase2_stack/worker_entry.py"]
    volumes:
{binds}  prqa-mocks:
    ports: !reset []
    restart: "no"
  prqa-docker-proxy:
    restart: "no"
    command: {q(proxy_cmd)}
  prqa-launcher:
    environment:
      PRQA_SANDBOX_IMAGE: {q(STUB_TAG)}
      PRQA_LAUNCH_POLL_S: "1"
    command: ["python3", "/app/prqa_launcher.py", "--interval", "3"]
  prqa-model-proxy:
    restart: "no"
    environment:
      PRQA_MODEL_UPSTREAM_ORIGIN: http://127.0.0.1:9099     # the loopback fake below replaces api.groq.com (prefix, model, contract unchanged)
  p2it-fake-upstream:
    profiles: ["phase2"]
    image: python:3.13-slim
    network_mode: "service:prqa-model-proxy"
    depends_on: [prqa-model-proxy]
    user: "10004:10004"
    read_only: true
    cap_drop: [ALL]
    environment:
      P2IT_SYNTHETIC_PROVIDER_KEY: {q(P2IT_PROVIDER_KEY)}
    command: ["python3", "-I", "/repo/tests/phase2_stack/fake_upstream.py", "9099"]
    volumes:
      - type: bind
        source: {q(str(ROOT / "tests"))}
        target: /repo/tests
        read_only: true
networks:
  prqa_internal:
    internal: true
  prqa_egress:
    internal: true
"""


@unittest.skipUnless(ENABLED, "set PRQA_PHASE2_CONTAINERS=1 with a local Docker daemon, prqa/launcher:0.1.0, prqa/worker:0.1.0, prqa/mocks:0.1.0")
class DisposablePhase2StackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for image in ("prqa/launcher:0.1.0", "prqa/worker:0.1.0", "prqa/mocks:0.1.0", "python:3.13-slim"):
            if real(["image", "inspect", image])[0] != 0:
                raise unittest.SkipTest(f"{image} is not available locally")
        cls.baseline = snapshot()
        cls.tmp = Path(tempfile.mkdtemp(prefix="prqa-p2it-"))
        cls.started = False
        # --- disposable stub sandbox image, built offline (same recipe as tests/test_launcher_integration.py) ---
        ctx = cls.tmp / "ctx"; ctx.mkdir()
        for f in ("entrypoint.sh", "verify_checkout.py", "populate_checkout.py"):
            shutil.copy(ROOT / "worker" / "sandbox" / f, ctx / f)
        for f in ("Dockerfile", "stub_opencode.py"):
            shutil.copy(ROOT / "tests" / "launcher_stub" / f, ctx / f)
        shutil.copy(FIX / "opencode-result.synthetic.json", ctx / "result-template.json")
        rc, _, err = real(["build", "--network", "none", "--pull=false", "-t", STUB_TAG, str(ctx)], timeout=600)
        if rc != 0:
            raise unittest.SkipTest(f"stub image build failed offline: {err[-300:]}")
        cls.stub_id = real(["image", "inspect", STUB_TAG, "--format", "{{.Id}}"])[1].strip()
        cls.opencode_id = real(["image", "inspect", OPENCODE_TAG, "--format", "{{.Id}}"])[1].strip()
        assert cls.stub_id != cls.opencode_id
        key_file = cls.tmp / "synthetic-provider-key"
        key_file.write_text(P2IT_PROVIDER_KEY); key_file.chmod(0o600)               # synthetic; mode 600, read only by the proxy (PRQA_MODEL_PROXY_UID)
        budget_dir = cls.tmp / "budget"; budget_dir.mkdir(mode=0o700); budget_dir.chmod(0o700)
        (cls.tmp / "p2it.env").write_text(compose_env(cls.stub_id, str(key_file), str(budget_dir)))
        (cls.tmp / "override.yml").write_text(compose_override(cls.stub_id))
        cls.listener = HostListener()
        cls.started = True
        rc, out, err = cls.compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "120",
                                   "prqa-worker", "prqa-mocks", "prqa-docker-proxy", "prqa-model-proxy", "p2it-fake-upstream",
                                   "prqa-launcher", timeout=300)
        if rc != 0:
            cls.tearDownClass()
            raise AssertionError(f"disposable stack failed to start: {err[-1500:]}")
        cls.c = {s: f"{PROJECT}-{s}-1" for s in ("prqa-worker", "prqa-mocks", "prqa-docker-proxy", "prqa-launcher", "prqa-model-proxy",
                                                  "p2it-fake-upstream")}
        cls.net = {n: f"{PROJECT}_{n}" for n in ("prqa_launcher", "prqa_internal", "prqa_sandbox", "prqa_egress", "prqa_prompt")}
        cls.budget_dir = budget_dir
        # decoy "n8n" listener on the project's shared network (where the persistent n8n lives in production)
        cls.decoy = f"{PROJECT}-decoy-n8n"
        real(["run", "-d", "--name", cls.decoy, "--label", f"com.docker.compose.project={PROJECT}", "--network", cls.net["prqa_internal"],
              "--network-alias", "n8n", "--read-only", "--cap-drop", "ALL", "--user", "10001:10001", "python:3.13-slim",
              "python3", "-m", "http.server", "5678", "--bind", "0.0.0.0", "--directory", "/usr/share"])
        # comparison network: internal like the launcher's, but WITHOUT inhibit_ipv4 (the host keeps its gateway address)
        cls.control_net = f"{PROJECT}-control"
        real(["network", "create", "--internal", "--label", f"com.docker.compose.project={PROJECT}", cls.control_net])

    @classmethod
    def compose(cls, *args, timeout=180):
        p = subprocess.run(["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE), "-f", str(cls.tmp / "override.yml"),
                            "--env-file", str(cls.tmp / "p2it.env"), "--profile", "phase2", *args],
                           capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, p.stdout, p.stderr

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "started", False):
            logs = {s: real(["logs", "--tail", "400", f"{PROJECT}-{s}-1"])[1:] for s in ("prqa-launcher", "prqa-docker-proxy", "prqa-worker",
                                                                                             "prqa-model-proxy", "p2it-fake-upstream")}
            for name in (getattr(cls, "decoy", ""), f"{PROJECT}-control-probe"):
                if name:
                    real(["rm", "-f", name])
            leftovers = []
            for job_id in EVIDENCE["flow"].get("job_ids", []) + ["p2itprobe01", "p2itorphan01"]:
                for kind, name in (("container", f"prqa-sandbox-{job_id}"), ("container", f"prqa-populate-{job_id}"), ("volume", f"prqa-checkout-{job_id}")):
                    if real(["container" if kind == "container" else "volume", "inspect", name])[0] == 0:
                        real(["rm", "-f", name] if kind == "container" else ["volume", "rm", "-f", name]); leftovers.append(name)
            rc, _, err = cls.compose("down", "-v", "--remove-orphans", "--timeout", "5")
            real(["network", "rm", getattr(cls, "control_net", f"{PROJECT}-control")])
            real(["rmi", "-f", STUB_TAG])
            cls.listener.close()
            after = snapshot()
            EVIDENCE["cleanup"] = {"compose_down_rc": rc, "leftovers_removed_at_teardown": leftovers,
                                   "daemon_delta": {k: {"added": sorted(after[k] - cls.baseline[k]), "missing": sorted(cls.baseline[k] - after[k])} for k in after}}
            out = os.environ.get("PRQA_P2IT_EVIDENCE_OUT")
            if out:
                Path(out).write_text(json.dumps(EVIDENCE, indent=1, default=str))
                Path(out).with_suffix(".logs.txt").write_text("\n".join(f"===== {s} =====\n{o}\n{e}" for s, (o, e) in logs.items()))
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------------------------------------------- helpers --
    def http(self, container, method, url, body=None, headers=None, timeout=30):
        rc, out, err = real(["exec", "-i", container, "python3", "-I", "-c", CLIENT],
                            stdin=json.dumps({"url": url, "method": method, "body": body, "headers": headers or {}, "timeout": timeout}), timeout=timeout + 30)
        self.assertEqual(rc, 0, err[-500:])
        r = json.loads(out)
        return r["status"], r["body"]

    def worker(self, method, path, body=None, token=WT):
        return self.http(self.c["prqa-worker"], method, "http://127.0.0.1:8080" + path, body, {"X-Worker-Token": token} if token else {})

    def probe(self, spec):
        rc, out, err = real(["exec", self.c["prqa-launcher"], "python3", "-I", "-c", PROBE, json.dumps(spec)], timeout=120)
        self.assertEqual(rc, 0, err[-500:])
        return json.loads(out)

    def ips(self, network):
        info = json.loads(real(["network", "inspect", network])[1])[0]
        members = {c["Name"]: c["IPv4Address"].split("/")[0] for c in info["Containers"].values()}
        return info, members

    def proxy_log_lines(self):
        _, out, err = real(["logs", self.c["prqa-docker-proxy"]])
        return [l for l in (out + err).splitlines() if "decision " in l]

    def launcher_docker(self, args):
        rc, out, err = real(["exec", self.c["prqa-launcher"], "sh", "-c", "docker " + args], timeout=120)
        return rc, out, err

    # ------------------------------------------------------------------------------------- 1: membership --
    def test_01_launcher_is_only_on_its_internal_network(self):
        insp = json.loads(real(["inspect", self.c["prqa-launcher"]])[1])[0]
        nets = sorted(insp["NetworkSettings"]["Networks"])
        self.assertEqual(nets, [self.net["prqa_launcher"]])
        hc = insp["HostConfig"]
        self.assertNotIn(hc["NetworkMode"], ("host", "bridge", "default"))
        self.assertFalse(hc["Privileged"]); self.assertTrue(hc["ReadonlyRootfs"]); self.assertEqual(hc["CapDrop"], ["ALL"])
        self.assertIn("no-new-privileges:true", hc["SecurityOpt"]); self.assertEqual(insp["Config"]["User"], "10002:10002")
        mounts = [(m["Type"], m.get("Name") or m.get("Source"), m["Destination"]) for m in insp["Mounts"]]
        self.assertEqual(sorted(d for _, _, d in mounts), ["/run/prqa", "/run/prqa-model-admin", "/work"])
        self.assertFalse([m for m in insp["Mounts"] if m["Destination"] == "/run/prqa-model-admin"][0]["RW"])   # read-only
        self.assertTrue(all(t == "volume" for t, _, _ in mounts), mounts)            # no bind: no docker.sock, no host path
        info, members = self.ips(self.net["prqa_launcher"])
        self.assertTrue(info["Internal"])
        self.assertEqual(info["Options"].get("com.docker.network.bridge.inhibit_ipv4"), "true")
        host_addr = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", "br-" + info["Id"][:12]], capture_output=True, text=True).stdout
        self.assertEqual(host_addr.strip(), "", "the host must have no IPv4 address on the launcher bridge")
        self.assertEqual(sorted(members), sorted([self.c["prqa-launcher"], self.c["prqa-worker"]]))
        proxy = json.loads(real(["inspect", self.c["prqa-docker-proxy"]])[1])[0]
        self.assertEqual(proxy["HostConfig"]["NetworkMode"], "none")
        socket_holders = [n for n in self.c.values()
                          if any(m.get("Source") in (REAL_SOCK, "/run/docker.sock") for m in json.loads(real(["inspect", n])[1])[0]["Mounts"])]
        self.assertEqual(socket_holders, [self.c["prqa-docker-proxy"]])
        EVIDENCE["probes"]["membership"] = {"launcher_networks": nets, "launcher_network_internal": info["Internal"],
                                            "launcher_network_options": info["Options"], "host_ipv4_on_launcher_bridge": host_addr.strip() or "none",
                                            "launcher_network_members": sorted(members), "launcher_mounts": mounts,
                                            "docker_socket_holders": socket_holders}

    # ------------------------------------------------------------------------------------- 2: no egress --
    def test_02_launcher_network_has_no_route_off_the_host(self):
        r = self.probe({"connect": [["192.0.2.1", 443], ["198.51.100.7", 80]]})   # RFC 5737 documentation addresses
        defaults = [x for x in r["routes"] if x["dest"] == "00000000"]
        self.assertEqual(defaults, [], r["routes"])
        self.assertFalse(r["routes6_default"])
        for target, res in r["connect"].items():
            self.assertEqual(res["result"], "ENETUNREACH", (target, res))           # refused locally: no packet leaves
            self.assertLess(res["elapsed_s"], 1.0)
        EVIDENCE["probes"]["egress"] = r

    # ------------------------------------------------------------------------------ 3: approved interface --
    def test_03_launcher_reaches_the_worker_launcher_api_only_with_its_token(self):
        launcher = self.c["prqa-launcher"]
        st, body = self.http(launcher, "GET", "http://prqa-worker:8080/healthz")
        self.assertEqual(st, 200)
        st, body = self.http(launcher, "GET", "http://prqa-worker:8080/launch/pending", headers={"X-Launcher-Token": LT, "X-Launcher-Id": "launcher-1"})
        self.assertEqual(st, 200, body)
        st_bad, _ = self.http(launcher, "GET", "http://prqa-worker:8080/launch/pending", headers={"X-Launcher-Token": "wrong-" + LT, "X-Launcher-Id": "launcher-1"})
        st_none, _ = self.http(launcher, "GET", "http://prqa-worker:8080/launch/pending")
        st_jobs, _ = self.http(launcher, "GET", "http://prqa-worker:8080/jobs", headers={"X-Launcher-Token": LT})   # worker-token API
        self.assertEqual((st_bad, st_none, st_jobs), (401, 401, 401))
        rc, out, err = real(["exec", launcher, "python3", "/app/prqa_launcher.py", "--check"])
        self.assertEqual(rc, 0, err[-400:])
        self.assertEqual(json.loads(out)["image_id"], self.stub_id)
        EVIDENCE["probes"]["approved_interface"] = {"healthz": 200, "launch_pending_with_token": 200, "wrong_token": st_bad,
                                                    "no_token": st_none, "worker_token_api_with_launcher_token": st_jobs, "check": json.loads(out)}

    # -------------------------------------------------------------------------- 4: unrelated services/host --
    def test_04_launcher_cannot_reach_unrelated_services_or_the_host(self):
        _, internal = self.ips(self.net["prqa_internal"])
        linfo, _ = self.ips(self.net["prqa_launcher"])
        persistent = {}
        rc, out, _ = real(["inspect", "prqa-n8n-1", "--format", "{{json .NetworkSettings.Networks}}"])
        if rc == 0:
            persistent = {k: v["IPAddress"] for k, v in json.loads(out).items()}
        gateways = {cfg.get("Gateway") for cfg in linfo["IPAM"]["Config"] if cfg.get("Gateway")}
        for n in (self.net["prqa_internal"], "bridge", "prqa_prqa_internal"):
            rc, out, _ = real(["network", "inspect", n, "--format", "{{json .IPAM.Config}}"])
            if rc == 0:
                gateways |= {c.get("Gateway") for c in json.loads(out) if c.get("Gateway")}
        # comparison: on an internal network WITHOUT inhibit_ipv4 the host keeps the gateway address; record what a
        # member reaches there (the hole inhibit_ipv4 closes). Informational: depends on the host firewall and services.
        cinfo = json.loads(real(["network", "inspect", self.control_net])[1])[0]
        cgw = cinfo["IPAM"]["Config"][0]["Gateway"]
        rc, out, err = real(["run", "--rm", "--name", f"{PROJECT}-control-probe", "--network", self.control_net, "--read-only",
                             "--cap-drop", "ALL", "--user", "10002:10002", "--entrypoint", "python3", "prqa/launcher:0.1.0", "-I", "-c", PROBE,
                             json.dumps({"connect": [[cgw, 22], [cgw, self.listener.port]]})], timeout=120)
        control = json.loads(out)["connect"] if rc == 0 else {"error": err[-200:]}
        targets = [[ip, 8090] for name, ip in internal.items() if name == self.c["prqa-mocks"]]
        targets += [[ip, 5678] for name, ip in internal.items() if name == self.decoy]
        targets += [[ip, 8080] for name, ip in internal.items() if name == self.c["prqa-worker"]]   # worker via the OTHER network
        targets += [[ip, 5678] for ip in persistent.values()]
        for n in (self.net["prqa_sandbox"], self.net["prqa_egress"]):                    # the model proxy's inference + egress side
            targets += [[ip, 8089] for name, ip in self.ips(n)[1].items() if name == self.c["prqa-model-proxy"]]
        targets += [[gw, port] for gw in sorted(gateways) for port in (self.listener.port, 22, 2375, 2376)]   # host services, daemon TCP
        before = len(self.listener.peers)
        r = self.probe({"resolve": ["prqa-mocks", "n8n", "prqa-n8n-1", "prqa-docker-proxy", "prqa-model-proxy", self.c["prqa-mocks"], self.decoy],
                        "connect": targets + [["prqa-worker", 8081]], "timeout": 4})
        time.sleep(0.5)
        for name, res in r["resolve"].items():
            self.assertTrue(isinstance(res, str) and res.startswith("GAIERROR"), (name, res))
        self.assertEqual(r["connect"].pop("prqa-worker:8081")["result"], "ECONNREFUSED")   # the worker exposes 8080 only
        for target, res in r["connect"].items():
            self.assertIn(res["result"], UNREACHABLE, target)                           # never CONNECTED, never a bare timeout
        self.assertEqual(self.listener.peers[before:], [], "the launcher reached the host listener")
        EVIDENCE["probes"]["unrelated"] = {"comparison_internal_without_inhibit": control, "probe": r,
                                           "host_listener_peers_from_launcher": self.listener.peers[before:]}

    # ---------------------------------------------------------------------------------- 5: Docker daemon --
    def test_05_launcher_has_no_direct_docker_daemon_access(self):
        launcher = self.c["prqa-launcher"]
        rc, out, _ = real(["exec", launcher, "sh", "-c", "ls -l /var/run/docker.sock /run/docker.sock 2>&1; echo DOCKER_HOST=$DOCKER_HOST; "
                                                          "command -v dockerd containerd runc || true"])
        self.assertIn("No such file", out)
        self.assertIn("DOCKER_HOST=unix:///run/prqa/docker-policy.sock", out)
        self.assertNotIn("/dockerd", out)
        rc_direct, _, err_direct = real(["exec", launcher, "docker", "-H", "unix:///var/run/docker.sock", "version"])
        self.assertNotEqual(rc_direct, 0)
        rc_info, _, err_info = self.launcher_docker("info")
        self.assertNotEqual(rc_info, 0)
        self.assertIn("engine info is not part of the reviewed transport", err_info)
        EVIDENCE["probes"]["daemon"] = {"sockets_absent": True, "direct_daemon_rc": rc_direct, "info_via_proxy_rc": rc_info}

    # ------------------------------------------------------------------------------- 6 + 7: proxy policy --
    def test_06_proxy_refuses_prohibited_operations_with_the_specific_reason(self):
        job = "p2itprobe01"
        base = f"create --name prqa-sandbox-{job} --label prqa.job={job} --label prqa.kind=sandbox"
        pinned = self.stub_id
        probes = {
            "unpinned image (real OpenCode tag)": (f"{base} {OPENCODE_TAG} true", "image is not the pinned sandbox image"),
            "unpinned image (python)": (f"{base} python:3.13-slim true", "image is not the pinned sandbox image"),
            "privileged": (f"{base} --user 10001:10001 --privileged {pinned} true", "privileged mode"),
            "host network": (f"{base} --user 10001:10001 --network host {pinned} true", "network 'host' is not permitted"),
            "bind of the host secrets dir": (f"{base} --user 10001:10001 --network none -v {SECRETS_DIR}:/s:ro {pinned} true", "bind mounts are not permitted"),
            "bind of host root": (f"{base} --user 10001:10001 --network none -v /:/host {pinned} true", "bind mounts are not permitted"),
            "foreign container name": (f"create --name not-prqa {pinned} true", "container name must be"),
            "inspect persistent n8n": ("inspect prqa-n8n-1", "PRQA job-named container"),
            "exec into persistent n8n": ("exec prqa-n8n-1 true", "PRQA job-named container"),
            "pull": ("pull alpine:3.20", "not permitted"),
            "build": ("build -t prqa/x - </dev/null", "not permitted"),
            "engine info": ("info", "engine info"),
        }
        results = {}
        for name, (args, reason) in probes.items():
            with self.subTest(probe=name):
                n_before = len(self.proxy_log_lines())
                rc, out, err = self.launcher_docker(args)
                new = self.proxy_log_lines()[n_before:]
                denies = [l for l in new if "decision deny" in l]
                results[name] = {"rc": rc, "deny": [l.split("decision ", 1)[1][:200] for l in denies]}
                self.assertNotEqual(rc, 0, f"{name} must be refused")
                self.assertTrue(denies, f"{name}: no deny decision")
                self.assertTrue(any(reason in l for l in denies), (name, denies))
                self.assertFalse(any(re.search(r"decision allow POST (/v[\d.]+)?/containers/create", l) for l in new), name)
        self.assertNotEqual(real(["container", "inspect", f"prqa-sandbox-{job}"])[0], 0, "nothing was created")
        rc, out, _ = self.launcher_docker("ps -a --format '{{.Names}}'")
        self.assertEqual(rc, 0)
        self.assertFalse([n for n in out.split() if not n.startswith("prqa-")], out)   # only PRQA-labelled resources are listed
        self.assertNotIn("prqa-n8n-1", out.split())
        EVIDENCE["policy"]["prohibited"] = results
        EVIDENCE["policy"]["visible_containers"] = out.split()

    # ---------------------------------------------------------------------------------------- 8 + 9: secrets --
    def test_07_no_secret_or_credential_reaches_the_launcher(self):
        launcher = self.c["prqa-launcher"]
        insp = json.loads(real(["inspect", launcher])[1])[0]
        env_names = sorted(e.split("=", 1)[0] for e in insp["Config"]["Env"])
        self.assertFalse(set(env_names) & set(CREDENTIAL_NAMES), env_names)
        env = dict(e.split("=", 1) for e in insp["Config"]["Env"])
        # the launcher holds only the restricted MINT credential (issue/revoke its own job tokens on the admin unix socket),
        # never the full admin token and never the provider key: the key exists only as the proxy's secret file
        self.assertEqual(env.get("PRQA_MODEL_PROXY_MINT_TOKEN"), P2IT_MINT)
        self.assertNotIn("PRQA_MODEL_PROXY_ADMIN_TOKEN", env)
        self.assertNotIn(P2IT_ADMIN, json.dumps(insp["Config"]["Env"]))
        self.assertNotIn(P2IT_PROVIDER_KEY, json.dumps(insp["Config"]["Env"]))
        self.assertFalse([k for k in env_names if "PROVIDER" in k or k.endswith("_KEY_FILE")], env_names)
        img_env = json.loads(real(["image", "inspect", "prqa/launcher:0.1.0", "--format", "{{json .Config.Env}}"])[1])
        self.assertFalse({e.split("=", 1)[0] for e in img_env} & set(CREDENTIAL_NAMES))
        rc, out, _ = real(["exec", launcher, "python3", "-I", "-c",
                           "import os,json; p=os.path.expanduser('~'); print(json.dumps({'secrets_dir': os.path.exists(%r), "
                           "'compose_env': os.path.exists('/repo/compose/.env'), 'home_config': os.path.exists('/home')}))" % str(SECRETS_DIR)])
        seen = json.loads(out)
        self.assertFalse(seen["secrets_dir"]); self.assertFalse(seen["compose_env"])
        rc, out, _ = real(["exec", launcher, "sh", "-c", "grep -rlE 'gsk_[A-Za-z0-9]{40,}|sk-or-v1-[A-Za-z0-9]{32,}|ghp_[A-Za-z0-9]{36}|discord(app)?\\.com/api/webhooks/[0-9]{15,}' /app /etc 2>/dev/null | head -5"])
        self.assertEqual(out.strip(), "")
        EVIDENCE["probes"]["secrets"] = {"launcher_env_names": env_names, "secrets_dir_visible": seen["secrets_dir"],
                                         "credential_patterns_in_app_or_etc": out.strip() or "none"}

    # ------------------------------------------------------------------------------- 4 + 5: synthetic flow --
    def test_08_synthetic_flow_through_launcher_proxy_and_mock_sinks(self):
        mocks = "http://prqa-mocks:8090"
        w = self.c["prqa-worker"]
        flow = EVIDENCE["flow"]
        self.http(w, "DELETE", mocks + "/mock/received")
        # 1 label event (fixture) through the worker's trigger evaluation
        ev = json.loads((FIX / "github-pr-labeled.synthetic.json").read_text())
        st, cfg = self.worker("GET", "/config")
        self.assertEqual(st, 200)
        ev["label"]["name"] = cfg["trigger"]["label"]
        st, out = self.worker("POST", "/webhook/evaluate", {"event": "pull_request", "payload": ev})
        self.assertTrue(st == 200 and out["eligible"], out)
        seed = out["job_seed"]
        # 2 PR context from the mock GitHub API (fixtures)
        repo = seed["repository"]; num = seed["pr"]["number"]
        st, pr = self.http(w, "GET", f"{mocks}/github/repos/{repo}/pulls/{num}")
        st2, files = self.http(w, "GET", f"{mocks}/github/repos/{repo}/pulls/{num}/files")
        st3, diff = self.http(w, "GET", f"{mocks}/github/repos/{repo}/pulls/{num}", headers={"Accept": "application/vnd.github.v3.diff"})
        self.assertEqual((st, st2, st3), (200, 200, 200))
        st, ctx = self.worker("POST", "/context/build", {"pr": pr, "files": files, "diff": diff["_text"], "openapi": None,
                                                         "repository": repo, "synthetic": True})
        self.assertEqual(st, 200, ctx)
        context = ctx["context"]
        # 3 prompt construction
        st, built = self.worker("POST", "/prompts/build", {"repository": repo, "pr_number": num, "test_level": seed["test_level"],
                                                          "synthetic": True, "context": context})
        self.assertEqual(st, 200, built)
        req = built["llm_request"]
        self.assertEqual(req["max_completion_tokens"], 3000)
        self.assertEqual(req["model"], "openai/gpt-oss-120b")
        # 4 mocked generation: the mock prompt-LLM sink (never a provider)
        st, gen = self.http(w, "POST", mocks + "/llm/v1/chat/completions", req)
        self.assertEqual(st, 200)
        content = gen["choices"][0]["message"]["content"]
        # 5 + 6 gate and rubric; then the fallback paths (gate rejection, provider failure, truncation)
        base = {"repository": repo, "pr_number": num, "test_level": seed["test_level"], "synthetic": True, "context": context}
        st, fin = self.worker("POST", "/prompts/finalize", {**base, "generated_text": content, "finish_reason": "stop"})
        self.assertEqual(st, 200, fin)
        st, fb = self.worker("POST", "/prompts/finalize", {**base, "generated_text": None, "llm_error": "provider_http_error", "http_status": 503})
        self.assertEqual((st, fb["prompt"]["source"]), (200, "reference_fallback"))
        st, rej = self.worker("POST", "/prompts/finalize", {**base, "generated_text": "# Plan\nrun everything\n", "finish_reason": "stop"})
        self.assertEqual((st, rej["prompt"]["source"]), (200, "reference_fallback"))
        self.assertEqual(rej["generation"]["failure"], "gate_rejected")
        st, trunc = self.worker("POST", "/prompts/finalize", {**base, "generated_text": content, "finish_reason": "length"})
        self.assertEqual((st, trunc["prompt"]["source"]), (200, "reference_fallback"))
        flow["prompt"] = {"llm_request_max_completion_tokens": req["max_completion_tokens"], "llm_request_model": req["model"],
                          "mock_candidate": {"source": fin["prompt"]["source"], "outcome": fin["generation"]["outcome"],
                                             "failure": fin["generation"]["failure"], "gate_valid": fin["validation"]["valid"],
                                             "quality": {k: (fin.get("quality") or {}).get(k) for k in ("accepted", "score", "threshold", "failure")}},
                          "provider_failure": {"source": fb["prompt"]["source"], "failure": fb["generation"]["failure"]},
                          "gate_rejection": {"source": rej["prompt"]["source"], "failure": rej["generation"]["failure"]},
                          "truncated": {"source": trunc["prompt"]["source"], "failure": trunc["generation"]["failure"]}}
        prompt = {k: fin["prompt"][k] for k in ("text", "source", "sha256")}
        # 7 live job (placeholder repository, test-only approval) → synthetic checkout → launcher → proxy → stub sandbox
        job_req = live_job_request(delivery=f"p2it-{TAG}")
        job_req["prompt"] = prompt
        st, created = self.worker("POST", "/jobs", job_req, token=None)
        self.assertEqual(st, 401)                                                      # worker API requires its token
        st, created = self.worker("POST", "/jobs", job_req)
        self.assertEqual(st, 201, created)
        job_id = created["job"]["id"]
        flow["job_ids"] = [job_id]
        final = None
        for _ in range(120):
            st, got = self.worker("GET", f"/jobs/{job_id}")
            if got["job"]["status"] in ("succeeded", "failed", "cancelled"):
                final = got["job"]; break
            time.sleep(2)
        self.assertIsNotNone(final, "launcher did not complete the job")
        flow["job"] = {"status": final["status"], "launch_state": final.get("launch_state"), "execution_mode": final.get("execution_mode"),
                       "error": final.get("error")}
        self.assertEqual(final["status"], "succeeded", final.get("error"))
        result = final["result"]
        # 8 result parsing and validation (consumes the launcher's output)
        st, check = self.worker("POST", "/results/validate", {"result": result})
        self.assertTrue(st == 200 and check["valid"], check)
        st, bad = self.worker("POST", "/results/validate", {"result": {**result, "summary": None, "scenarios": "not-a-list"}})
        self.assertFalse(bad["valid"])
        st, rendered = self.worker("POST", "/reports/render", {"result": result})
        self.assertEqual(st, 200)
        # 9 GitHub and Discord at the mock sinks only
        st, d = self.worker("POST", "/notifications/discord", {"kind": "result", "job_id": job_id})
        self.assertEqual((st, d.get("mode")), (200, "mock"), d)
        st_gh, _ = self.http(w, "POST", f"{mocks}/github/repos/your-org/your-first-real-repo/issues/7/comments", {"body": rendered["github_comment_markdown"]})
        self.assertEqual(st_gh, 404)                                                   # the mock refuses non-synthetic repos
        st_syn, _ = self.http(w, "POST", f"{mocks}/github/repos/{repo}/issues/{num}/comments", {"body": rendered["github_comment_markdown"]})
        self.assertEqual(st_syn, 201)
        st, recv = self.http(w, "GET", mocks + "/mock/received")
        kinds = [r["kind"] for r in recv["received"]]
        self.assertIn("llm_request", kinds); self.assertIn("discord_webhook", kinds)
        self.assertIn("github_comment", kinds); self.assertIn("github_comment_refused", kinds)
        decisions = [l.split("decision ", 1)[1][:160] for l in self.proxy_log_lines() if job_id in l]
        self.assertTrue(any(re.match(r"allow POST /v[\d.]+/containers/create", l) for l in decisions), decisions)
        flow["sandbox_summary"] = result["summary"]["executive_summary"]
        flow["result"] = {"valid": check["valid"], "malformed_rejected": not bad["valid"], "executed_by": result.get("executed_by"),
                          "discord": {"status": d.get("status"), "mode": d.get("mode")}, "github_mock_placeholder": st_gh,
                          "github_mock_synthetic": st_syn, "mock_received_kinds": sorted(set(kinds)), "proxy_decisions_for_job": decisions}
        for kind, name in (("container", f"prqa-sandbox-{job_id}"), ("container", f"prqa-populate-{job_id}"), ("volume", f"prqa-checkout-{job_id}")):
            self.assertNotEqual(real(["container" if kind == "container" else "volume", "inspect", name])[0], 0, f"{name} left behind")

    # ------------------------------------------------------------------------------- 10: orphan cleanup --
    def test_09_label_based_orphan_cleanup_still_works(self):
        job = EVIDENCE["flow"].get("job_ids", ["p2itorphan01"])[0]           # a terminal job: its resources are orphans
        rc, out, err = self.launcher_docker(f"volume create --label prqa.job={job} --label prqa.kind=checkout prqa-checkout-{job}")
        self.assertEqual(rc, 0, err)
        rc, dry, err = real(["exec", self.c["prqa-launcher"], "python3", "/app/prqa_launcher.py", "--reconcile-only"])
        self.assertEqual(real(["volume", "inspect", f"prqa-checkout-{job}"])[0], 0, "dry run removed something")
        self.assertIn(f"prqa-checkout-{job}", dry)
        rc, applied, err = real(["exec", self.c["prqa-launcher"], "python3", "/app/prqa_launcher.py", "--reconcile-only", "--apply"])
        self.assertEqual(rc, 0, err[-400:])
        self.assertNotEqual(real(["volume", "inspect", f"prqa-checkout-{job}"])[0], 0, "orphan volume not removed")
        EVIDENCE["policy"]["orphan_cleanup"] = {"dry_run": dry.strip()[-400:], "applied": applied.strip()[-400:]}

    # ---------------------------------------------------------------- model proxy: admin vs inference --
    def test_10_model_proxy_admin_and_inference_interfaces_are_separate(self):
        mpc = self.c["prqa-model-proxy"]
        insp = json.loads(real(["inspect", mpc])[1])[0]
        self.assertEqual(sorted(insp["NetworkSettings"]["Networks"]), sorted([self.net["prqa_egress"], self.net["prqa_sandbox"], self.net["prqa_prompt"]]))
        sinfo, smembers = self.ips(self.net["prqa_sandbox"])
        self.assertTrue(sinfo["Internal"])
        self.assertEqual(sinfo["Options"].get("com.docker.network.bridge.inhibit_ipv4"), "true")
        self.assertNotIn(mpc, self.ips(self.net["prqa_launcher"])[1])
        # listeners: exactly the inference TCP port (+ the test's loopback fake upstream); the admin API is a 0660 socket
        rc, out, err = real(["exec", mpc, "python3", "-I", "-c", LISTEN])
        self.assertEqual(rc, 0, err)
        listen = json.loads(out)
        # 127.0.0.11 is Docker's embedded DNS resolver, present in every container namespace on a user-defined network
        self.assertEqual([a for a in listen["tcp"] if not a.startswith("127.0.0.11:")], ["0.0.0.0:8089", "127.0.0.1:9099"])
        self.assertEqual(listen["tcp6"], [])
        self.assertEqual(listen["admin_socket"], {"mode": "0o660", "gid": 10002, "is_socket": True})
        # the trusted control plane (launcher) reaches ADMIN over the socket; inference is not served there
        rc, out, err = real(["exec", self.c["prqa-launcher"], "python3", "-I", "-c", ADMIN_FROM_LAUNCHER, UPSTREAM_MODEL])
        self.assertEqual(rc, 0, err)
        admin = json.loads(out)
        self.assertEqual(admin["healthz"][1]["interface"], "admin")
        self.assertEqual(admin["list"][0], 403, "the launcher's mint credential cannot list tokens")
        self.assertEqual(admin["reserved_prompt_job"][0], 422)
        self.assertIs(admin["admin_token_in_env"], False)
        self.assertEqual(admin["list_without_token"][0], 401)
        rc, out, err = real(["exec", self.c["prqa-model-proxy"], "python3", "-I", "-c", ADMIN_FROM_PROXY])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), [200, {"tokens": []}], "the job's token was revoked at job end")
        self.assertEqual((admin["inference_on_admin"][0], admin["inference_on_admin"][1]["error"]["interface"]), (404, "admin"))
        # the untrusted side: a sandbox-equivalent container on the sandbox network, holding even the ADMIN token
        mp_ip = smembers[mpc]
        gw = sinfo["IPAM"]["Config"][0]["Gateway"]
        base = "http://prqa-model-proxy:8089"
        spec = {"http": {
                    "healthz": ["GET", base + "/healthz", None, None],
                    "admin_list_with_admin_token": ["GET", base + "/admin/tokens", P2IT_ADMIN, None],
                    "admin_issue_with_admin_token": ["POST", base + "/admin/tokens", P2IT_ADMIN, {"job_id": "escalate-0001", "model": UPSTREAM_MODEL}],
                    "admin_revoke": ["DELETE", base + "/admin/tokens/escalate-0001", P2IT_ADMIN, None],
                    "admin_encoded": ["GET", base + "/%61dmin/tokens", P2IT_ADMIN, None],
                    "admin_dotdot": ["GET", base + "/v1/../admin/tokens", P2IT_ADMIN, None],
                    "admin_token_as_inference": ["POST", base + "/v1/chat/completions", P2IT_ADMIN, {"model": UPSTREAM_MODEL, "messages": []}],
                    "mint_token_as_inference": ["POST", base + "/v1/chat/completions", P2IT_MINT, {"model": UPSTREAM_MODEL, "messages": []}],
                    "mint_token_admin_route": ["GET", base + "/admin/tokens", P2IT_MINT, None],
                    "no_token_inference": ["POST", base + "/v1/chat/completions", None, {"model": UPSTREAM_MODEL, "messages": []}],
                    "config": ["GET", base + "/config", None, None]},
                "connect": [[mp_ip, 9099], [mp_ip, 8088], [mp_ip, 2375], [gw, 22]],
                "resolve": ["prqa-worker", "prqa-launcher", "n8n", "prqa-n8n-1", "prqa-mocks", "prqa-docker-proxy"]}
        rc, out, err = real(["run", "--rm", "--name", f"{PROJECT}-sandbox-probe", "--label", f"com.docker.compose.project={PROJECT}",
                             "--network", self.net["prqa_sandbox"], "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                             "--user", "10001:10001", "--entrypoint", "python3", self.stub_id, "-I", "-c", SANDBOX_SIDE, json.dumps(spec)], timeout=180)
        self.assertEqual(rc, 0, err[-500:])
        side = json.loads(out)
        h = side["http"]
        self.assertEqual(h["healthz"], [200, {"ok": True, "interface": "inference"}])
        for name in ("admin_list_with_admin_token", "admin_issue_with_admin_token", "admin_revoke", "admin_encoded", "admin_dotdot"):
            st, body = h[name]
            self.assertEqual(st, 403, (name, body))
            self.assertEqual((body["error"]["type"], body["error"]["interface"]), ("proxy_policy", "inference"), name)
        self.assertEqual(h["admin_token_as_inference"][0], 403)
        self.assertEqual((h["mint_token_as_inference"][0], h["mint_token_admin_route"][0]), (403, 403))
        self.assertIn("administrative credential", h["admin_token_as_inference"][1]["error"]["message"])
        self.assertEqual(h["no_token_inference"][0], 401)
        self.assertEqual(h["config"][0], 404); self.assertEqual(h["config"][1]["error"]["type"], "proxy_policy")
        for port in (9099, 8088, 2375):
            self.assertEqual(side["connect"][f"{mp_ip}:{port}"], "ECONNREFUSED", port)   # only 8089 listens on the sandbox side
        self.assertIn(side["connect"][f"{gw}:22"], UNREACHABLE)                          # no host address on the sandbox bridge
        for name, res in side["resolve"].items():
            self.assertTrue(isinstance(res, str) and res.startswith("GAIERROR"), (name, res))
        self.assertFalse(side["admin_dir_visible"])
        # the real job's sandbox (stub) used inference through the proxy and failed to reach admin
        summary = EVIDENCE["flow"].get("sandbox_summary", "")
        for expected in ("MODEL-PROXY-PROBE inference=200", "'P2IT MOCK COMPLETION'", "sse_done=True", f"model_arg={CLAIM_MODEL}",
                         "key_ref={env:PRQA_MODEL_TOKEN}", "token_resolved=True", "admin_via_inference=403", "admin_encoded=403",
                         "admin_socket_visible=False", "provider_key_in_env=False"):
            self.assertIn(expected, summary)
        self.assertNotIn("HIDDEN REASONING", summary)
        env_names = set(summary.rsplit("env_names=", 1)[1].split(","))
        self.assertLessEqual(env_names, SANDBOX_OPENCODE_ENV, "OpenCode sees only the allowlisted variables")
        self.assertLessEqual({"OPENCODE_CONFIG_CONTENT", "PRQA_MODEL_TOKEN", "PRQA_JOB_ID", "HOME", "PATH"}, env_names)
        self.assertNotIn("PRQA_MODEL_BASE_URL", env_names)
        # the fake provider saw exactly the job's request, with the proxy-injected synthetic key, on the fixed path
        up = [json.loads(l) for l in real(["logs", self.c["p2it-fake-upstream"]])[1].splitlines() if l.startswith("{")]
        self.assertEqual([(u["method"], u["path"], u["model"], u["auth_is_synthetic_key"]) for u in up],
                         [("POST", "/openai/v1/chat/completions", UPSTREAM_MODEL, True)])
        self.assertEqual(up[0]["contract"], {"stream": False, "temperature": 0.2, "reasoning_effort": "low", "include_reasoning": False,
                                             "max_completion_tokens": 3000})
        self.assertEqual(up[0]["body_keys"], sorted(["model", "messages", "stream", "temperature", "reasoning_effort", "include_reasoning",
                                                     "max_completion_tokens"]))
        self.assertEqual(up[0]["header_names"], sorted(["authorization", "host", "content-type", "content-length", "accept", "accept-encoding",
                                                        "user-agent", "x-title"]))
        _, o, e = real(["logs", mpc])
        proxy_log = o + e
        for secret in (P2IT_PROVIDER_KEY, P2IT_ADMIN, P2IT_MINT, P2IT_PROMPT, "prqa_mt_", "P2IT MOCK COMPLETION"):
            self.assertNotIn(secret, proxy_log)
        self.assertIn("deny [inference]", proxy_log)
        EVIDENCE["model_proxy"] = {"networks": sorted(insp["NetworkSettings"]["Networks"]), "listeners": listen, "admin_from_launcher": admin,
                                   "sandbox_side": {k: [v[0], v[1].get("error", v[1]) if isinstance(v[1], dict) else v[1]] for k, v in h.items()},
                                   "sandbox_connect": side["connect"], "sandbox_resolve": side["resolve"], "sandbox_env_names": side["env_names"],
                                   "job_sandbox_summary": summary, "fake_upstream_requests": up}


    # ------------------------------------------------------------- 11: prompt generation (n8n workflow 03 path) --
    def test_11_prompt_generation_reaches_the_provider_only_through_the_proxy_and_its_ledger(self):
        """n8n's place on prqa_prompt, taken by a probe: the prompt credential gets one locked, budgeted upstream request;
        tools, administrative credentials and admin routes are refused; nothing else is reachable from that network."""
        base = "http://prqa-model-proxy:8089"
        msgs = [{"role": "system", "content": "synthetic system"}, {"role": "user", "content": "synthetic prompt request"}]
        body = {"model": UPSTREAM_MODEL, "messages": msgs, "max_completion_tokens": 3000, "temperature": 0.2, "reasoning_effort": "low",
                "include_reasoning": False, "stream": False}
        tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]
        spec = {"http": {
                    "prompt": ["POST", base + "/v1/chat/completions", P2IT_PROMPT, body, {"X-PRQA-Job-Id": "p2it-prompt-1"}],
                    "prompt_with_tools": ["POST", base + "/v1/chat/completions", P2IT_PROMPT, {**body, "tools": tools}],
                    "prompt_admin_route": ["GET", base + "/admin/tokens", P2IT_PROMPT, None],
                    "mint_as_inference": ["POST", base + "/v1/chat/completions", P2IT_MINT, body],
                    "admin_as_inference": ["POST", base + "/v1/chat/completions", P2IT_ADMIN, body]},
                "connect": [], "resolve": ["prqa-worker", "prqa-launcher", "prqa-docker-proxy", "prqa-mocks"]}
        rc, out, err = real(["run", "--rm", "--name", f"{PROJECT}-prompt-probe", "--label", f"com.docker.compose.project={PROJECT}",
                             "--network", self.net["prqa_prompt"], "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                             "--user", "10001:10001", "--entrypoint", "python3", self.stub_id, "-I", "-c", SANDBOX_SIDE, json.dumps(spec)], timeout=180)
        self.assertEqual(rc, 0, err[-500:])
        side = json.loads(out); h = side["http"]
        self.assertEqual(h["prompt"][0], 200, h["prompt"])
        self.assertEqual(h["prompt"][1]["choices"][0]["message"]["role"], "assistant")
        self.assertEqual(h["prompt_with_tools"][0], 403)
        self.assertIn("tools are not permitted", h["prompt_with_tools"][1]["error"]["message"])
        self.assertEqual((h["prompt_admin_route"][0], h["mint_as_inference"][0], h["admin_as_inference"][0]), (403, 403, 403))
        for name, res in side["resolve"].items():
            self.assertTrue(isinstance(res, str) and res.startswith("GAIERROR"), (name, res))
        up = [json.loads(l) for l in real(["logs", self.c["p2it-fake-upstream"]])[1].splitlines() if l.startswith("{")]
        self.assertEqual(len(up), 2, "the job's request (step 10) plus exactly one prompt request")
        self.assertEqual(up[-1]["contract"], {"stream": False, "temperature": 0.2, "reasoning_effort": "low", "include_reasoning": False,
                                              "max_completion_tokens": 3000})
        self.assertNotIn("tools", up[-1]["body_keys"]); self.assertTrue(up[-1]["auth_is_synthetic_key"])
        sys.path.insert(0, str(ROOT / "proxy"))
        import provider_budget as pb
        snap = pb.Ledger(pb.BudgetConfig(directory=str(self.budget_dir), daily_requests=200, daily_tokens=10_000_000, job_tokens=1_000_000,
                                         tokens_per_minute=1_000_000, max_in_flight=1)).snapshot()
        self.assertIn("prompt-p2it-prompt-1", snap["jobs"], "the prompt request was reserved and settled in the shared ledger")
        self.assertEqual((snap["requests_today"], snap["in_flight"]), (2, 0))
        EVIDENCE["prompt_path"] = {"statuses": {k: v[0] for k, v in h.items()}, "resolve": side["resolve"],
                                   "upstream_requests": len(up), "ledger_jobs": sorted(snap["jobs"]), "ledger_requests_today": snap["requests_today"]}


class ComposeLauncherTopologyTests(unittest.TestCase):
    """Always run (no Docker): the compose file keeps the launcher on its own internal, host-address-less network."""

    @classmethod
    def setUpClass(cls):
        try:
            import yaml
        except ImportError:
            raise unittest.SkipTest("PyYAML not installed")
        cls.doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
        cls.svc = cls.doc["services"]

    def test_launcher_is_only_on_the_internal_launcher_network(self):
        launcher = self.svc["prqa-launcher"]
        self.assertEqual(launcher["networks"], ["prqa_launcher"])
        self.assertNotIn("network_mode", launcher)
        net = self.doc["networks"]["prqa_launcher"]
        self.assertIs(net["internal"], True)
        self.assertEqual(net["driver_opts"]["com.docker.network.bridge.inhibit_ipv4"], "true")

    def test_only_the_worker_shares_the_launcher_network(self):
        members = sorted(name for name, s in self.svc.items() if "prqa_launcher" in (s.get("networks") or []))
        self.assertEqual(members, ["prqa-launcher", "prqa-worker"])
        self.assertEqual(self.svc["prqa-launcher"]["environment"]["PRQA_WORKER_URL"], "http://prqa-worker:8080")

    def test_docker_socket_only_in_the_network_less_policy_proxy(self):
        holders = sorted(name for name, s in self.svc.items() if any("docker.sock" in str(v) for v in s.get("volumes") or []))
        self.assertEqual(holders, ["prqa-docker-proxy"])
        self.assertEqual(self.svc["prqa-docker-proxy"]["network_mode"], "none")
        launcher = self.svc["prqa-launcher"]
        self.assertEqual(launcher["environment"]["DOCKER_HOST"], "unix:///run/prqa/docker-policy.sock")
        self.assertEqual(sorted(launcher["volumes"]), ["prqa_docker_proxy_sock:/run/prqa", "prqa_launcher_work:/work",
                                                       "prqa_model_admin_sock_opencode:/run/prqa-model-admin:ro"])
        self.assertFalse(any(k in launcher for k in ("privileged", "pid", "ipc", "ports", "devices", "cap_add")))
        self.assertEqual(launcher["cap_drop"], ["ALL"]); self.assertIs(launcher["read_only"], True)

    def test_model_proxy_admin_is_a_socket_and_inference_the_only_port(self):
        mp = self.svc["prqa-model-proxy"]
        cmd = mp["command"]
        self.assertEqual(cmd[cmd.index("--admin-socket") + 1], "/run/prqa-model-admin/admin.sock")
        self.assertEqual((cmd[cmd.index("--bind") + 1], cmd[cmd.index("--port") + 1]), ("0.0.0.0", "8089"))
        # never the launcher network; prqa_prompt carries only n8n's prompt-generation requests (internal)
        self.assertEqual(sorted(mp["networks"]), ["prqa_egress", "prqa_prompt", "prqa_sandbox"])
        self.assertEqual(sorted(n for n, s in self.svc.items() if "prqa_prompt" in (s.get("networks") or [])), ["n8n", "prqa-model-proxy"])
        prompt_net = self.doc["networks"]["prqa_prompt"]
        self.assertIs(prompt_net["internal"], True); self.assertEqual(prompt_net["labels"]["prqa.net"], "prompt")
        # four distinct credentials: admin only in the proxy, mint in the launcher, prompt in n8n
        holders = {name: sorted(n for n, s in self.svc.items() if name in (s.get("environment") or {}))
                   for name in ("PRQA_MODEL_PROXY_ADMIN_TOKEN", "PRQA_MODEL_PROXY_MINT_TOKEN", "PRQA_MODEL_PROXY_PROMPT_TOKEN")}
        self.assertEqual(holders, {"PRQA_MODEL_PROXY_ADMIN_TOKEN": ["prqa-model-proxy", "prqa-model-proxy-opencode"],
                                   "PRQA_MODEL_PROXY_MINT_TOKEN": ["prqa-launcher", "prqa-model-proxy", "prqa-model-proxy-opencode"],
                                   "PRQA_MODEL_PROXY_PROMPT_TOKEN": ["n8n", "prqa-model-proxy"]})
        self.assertNotIn("ports", mp)
        self.assertIn("prqa_model_admin_sock:/run/prqa-model-admin", mp["volumes"])
        def sock_holders(vol_name):
            return sorted(n for n, s in self.svc.items() if any(str(v).split(":")[0] == vol_name for v in s.get("volumes") or []))
        # each proxy's admin socket is shared with nobody but the launcher (and the launcher holds only the OpenCode one)
        self.assertEqual(sock_holders("prqa_model_admin_sock"), ["prqa-model-proxy"])
        self.assertEqual(sock_holders("prqa_model_admin_sock_opencode"), ["prqa-launcher", "prqa-model-proxy-opencode"])
        for vol_name in ("prqa_model_admin_sock", "prqa_model_admin_sock_opencode"):
            vol = self.doc["volumes"][vol_name]["driver_opts"]
            self.assertEqual(vol["type"], "tmpfs"); self.assertIn("mode=0770", vol["o"]); self.assertIn("uid=${PRQA_MODEL_PROXY_UID:-10003}", vol["o"])
        self.assertEqual(mp["user"], "${PRQA_MODEL_PROXY_UID:-10003}:10003", "the proxy runs as the key file's owner (mode 600)")
        holders = {n: s["secrets"] for n, s in self.svc.items() if s.get("secrets")}
        self.assertEqual(holders, {"prqa-model-proxy": ["prqa_model_provider_key"], "prqa-model-proxy-opencode": ["prqa_opencode_provider_key"]},
                         "only the model proxies mount a provider key, each its own")
        oc = self.svc["prqa-model-proxy-opencode"]
        self.assertEqual(sorted(oc["networks"]), ["prqa_egress", "prqa_sandbox"], "the OpenCode proxy is not on the prompt network")
        self.assertNotIn("ports", oc); self.assertEqual(oc["user"], mp["user"]); self.assertIs(oc["read_only"], True)
        self.assertEqual(oc["cap_drop"], ["ALL"]); self.assertEqual(oc["command"], mp["command"])
        sandbox = self.doc["networks"]["prqa_sandbox"]
        self.assertIs(sandbox["internal"], True)
        self.assertEqual(sandbox["driver_opts"]["com.docker.network.bridge.inhibit_ipv4"], "true")
        self.assertEqual(mp["environment"]["PRQA_MODEL_PROVIDER_KEY_FILE"], "/run/secrets/prqa_model_provider_key")
        self.assertFalse({"GROQ_API_KEY", "OPENROUTER_API_KEY", "PROMPT_LLM_API_KEY"} & set(mp["environment"]))

    def test_no_provider_github_or_discord_credential_in_the_launcher(self):
        env = self.svc["prqa-launcher"]["environment"]
        self.assertFalse(set(env) & set(CREDENTIAL_NAMES), sorted(env))
        self.assertNotIn("env_file", self.svc["prqa-launcher"])
        self.assertNotIn("secrets", self.svc["prqa-launcher"])


def _persistent_token():
    for line in (ROOT / "compose" / ".env").read_text().splitlines():
        if line.startswith("PRQA_WORKER_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


@unittest.skipUnless(ENABLED, "set PRQA_PHASE2_CONTAINERS=1 with the persistent review-only stack running")
class PersistentWorkerBoundaryTests(unittest.TestCase):
    URL = "http://127.0.0.1:8080"

    def call(self, method, path, body=None, token=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Worker-Token"] = token
        req = urllib.request.Request(self.URL + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raw_body = e.read()
            try:
                return e.code, json.loads(raw_body or b"null")
            except ValueError:
                return e.code, {"_text": raw_body[:200].decode("utf-8", "replace")}

    @classmethod
    def setUpClass(cls):
        try:
            urllib.request.urlopen(cls.URL + "/healthz", timeout=5).read()
        except OSError:
            raise unittest.SkipTest("persistent worker is not running")
        cls.token = _persistent_token()

    def test_1_review_only_config_and_auth(self):
        st, health = self.call("GET", "/healthz")
        self.assertEqual((st, health["automation_mode"]), (200, "review_only"))
        self.assertEqual(self.call("GET", "/config")[0], 401)
        self.assertEqual(self.call("GET", "/config", token="invalid-token-000000000000")[0], 401)
        st, cfg = self.call("GET", "/config", token=self.token)
        self.assertEqual(st, 200)
        self.assertTrue(cfg["policy"]["review_only"]); self.assertFalse(any(cfg["policy"]["flags"].values()))
        self.assertEqual(cfg["endpoints"]["prompt_llm_api_mode"], "mock")
        self.assertEqual(cfg["endpoints"]["github_api_mode"], "mock")
        self.assertEqual(self.call("GET", "/launch/pending")[0], 401)
        self.assertIn(self.call("GET", "/launch/pending", token=self.token)[0], (401, 403))   # worker token is not a launcher token
        EVIDENCE.setdefault("persistent", {})["config"] = {"automation_mode": health["automation_mode"], "flags": cfg["policy"]["flags"],
                                                          "prompt_llm_api_mode": cfg["endpoints"]["prompt_llm_api_mode"]}

    def test_2_budget_gate_rubric_and_fallback(self):
        sys.path.insert(0, str(ROOT / "tests"))
        from qa_benchmark import CASES, context_for                                   # noqa: E402
        c01 = next(c for c in CASES if c.cid == "C01-clear-behaviour"); ctx = context_for(c01)
        base = {"repository": "synthetic-org/bench-repo", "pr_number": c01.pr_number, "test_level": "standard", "context": ctx, "synthetic": True}
        st, built = self.call("POST", "/prompts/build", base, token=self.token)
        self.assertEqual(st, 200)
        self.assertEqual(built["llm_request"]["max_completion_tokens"], 3000)
        self.assertEqual(built["generation"]["provider"], "groq")
        self.assertEqual(built["generation"]["max_tokens"], 3000)
        self.assertEqual((built["prompt_llm_api_mode"], built["llm_request"]["model"]), ("mock", "mock-llm"))   # review-only: mock sink
        self.assertTrue(built["prompt_llm_api_base"].startswith("http://prqa-mocks:8090/"))
        pin = lambda d: json.loads((ROOT / "fixtures/recorded/groq-c01" / d / "case-C01-clear-behaviour.response.json").read_text())["content"]
        out = {}
        for name, body, failure in (
                ("gate rejection (run-3 output)", {"generated_text": pin("groq-c01-gate-rejected-20261026-run3"), "finish_reason": "stop"}, "gate_rejected"),
                ("below 85 (run-4 output, gate-valid)", {"generated_text": pin("groq-c01-gate-valid-rubric78-20261026-run4"), "finish_reason": "stop"}, "rubric_threshold"),
                ("provider failure", {"generated_text": None, "llm_error": "provider_http_error", "http_status": 500}, None)):
            st, fin = self.call("POST", "/prompts/finalize", {**base, **body}, token=self.token)
            self.assertEqual(st, 200, fin)
            self.assertEqual(fin["prompt"]["source"], "reference_fallback", name)
            if failure:
                self.assertEqual(fin["generation"]["failure"], failure, name)
            out[name] = {"source": fin["prompt"]["source"], "failure": fin["generation"]["failure"],
                         "score": (fin.get("quality") or {}).get("score"), "threshold": (fin.get("quality") or {}).get("threshold")}
        self.assertEqual(out["below 85 (run-4 output, gate-valid)"]["threshold"], 85.0)
        st, _ = self.call("POST", "/prompts/finalize", raw=b"{not json", token=self.token)
        self.assertEqual(st, 400)
        st, _ = self.call("POST", "/prompts/finalize", {**base, "generated_text": None}, token="invalid-token-000000000000")
        self.assertEqual(st, 401)
        st, v = self.call("POST", "/results/validate", {"result": {"schema_version": "x", "verdict": 3}}, token=self.token)
        self.assertEqual(st, 200); self.assertFalse(v["valid"])
        EVIDENCE.setdefault("persistent", {})["finalize"] = out


if __name__ == "__main__":
    unittest.main()
