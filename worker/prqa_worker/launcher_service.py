"""prqa-launcher: the ONLY PRQA component that talks to Docker, and it talks to it through the policy-enforcing socket
proxy (proxy/docker_policy_proxy.py). It never receives GitHub, Discord, model-provider or target-API credentials.

Loop (run_once): GET /launch/pending → POST claim (lease) → purge any leftover resources of that job (recovery after
a dead launcher) → download the artifact and verify size + sha256 + manifest identity → rebuild the plan from the
claim fields and launcher-owned configuration (pinned image ID, verified internal network) → optional per-job model
token from the model proxy → execute_plan with heartbeat-driven cancellation → report result/failed/cancelled
(idempotent) → revoke token, delete the local artifact copy.

Nothing from the worker is ever used as a Docker argument except the validated job id, repository, SHA, model name,
runtime minutes and the two expected hashes; image, network, mounts and environment come from this process only.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Callable

from . import launcher as lifecycle
from . import opencode_runner
from .redaction import redact_text
from .workspace import read_artifact_manifest

log = logging.getLogger("prqa.launcher")
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
BASE_URL_RE = re.compile(r"^https?://[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?/?$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{1,120}$")
LEVELS = ("smoke", "standard", "comprehensive", "security")
PROXY_MODEL_PREFIX = "prqa/"                   # OpenCode provider id of the PRQA model proxy (worker/sandbox/entrypoint.sh)
LAUNCHER_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")   # shared with the worker (server.py)
INFERENCE_URL_RE = re.compile(r"^http://[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?(:[0-9]{1,5})?/v1$")


class LauncherError(RuntimeError):
    pass


@dataclass
class LauncherConfig:
    worker_url: str
    launcher_token: str
    launcher_id: str
    sandbox_image_tag: str = "prqa/opencode-sandbox:local"
    sandbox_image_id: str = ""                 # pinned, verified against `docker image inspect` before every launch
    sandbox_network: str = "none"              # 'none' or a Docker network that must be internal + labelled prqa.net=sandbox
    docker_cli: str = "docker"
    workdir: str = ""
    lease_s: int = 120
    heartbeat_s: float = 30.0
    poll_s: float = 2.0
    max_artifact_mb: float = 500.0
    model_base_url: str = ""                   # PRQA_MODEL_PROXY_INFERENCE_URL, http://prqa-model-proxy:8089/v1 (non-secret) — the sandbox's PRQA_MODEL_BASE_URL
    model_proxy_admin_url: str = ""            # unix:///run/prqa-model-admin/admin.sock (admin API; never a TCP URL)
    model_proxy_mint_token: str = ""           # PRQA_MODEL_PROXY_MINT_TOKEN: issue/revoke OWN job tokens only; never enters the sandbox
    http_timeout_s: float = 30.0

    @classmethod
    def from_env(cls, env: dict[str, str]) -> LauncherConfig:
        inference = env.get("PRQA_MODEL_PROXY_INFERENCE_URL", "")
        legacy = env.get("PRQA_MODEL_BASE_URL", "")       # older name of the same setting; both set and different = refuse
        if inference and legacy and inference != legacy:
            raise LauncherError("PRQA_MODEL_PROXY_INFERENCE_URL and PRQA_MODEL_BASE_URL disagree; set only PRQA_MODEL_PROXY_INFERENCE_URL")
        if env.get("PRQA_MODEL_PROXY_ADMIN_TOKEN"):
            # least privilege: the launcher mints job tokens with the restricted mint credential, never the full admin one
            raise LauncherError("PRQA_MODEL_PROXY_ADMIN_TOKEN must not be given to the launcher; use PRQA_MODEL_PROXY_MINT_TOKEN")
        return cls(
            worker_url=(env.get("PRQA_WORKER_URL") or "http://prqa-worker:8080").rstrip("/"),
            launcher_token=env.get("PRQA_LAUNCHER_TOKEN", ""),
            launcher_id=env.get("PRQA_LAUNCHER_ID") or f"launcher-{os.getpid()}",
            sandbox_image_tag=env.get("PRQA_SANDBOX_IMAGE", "prqa/opencode-sandbox:local"),
            sandbox_image_id=env.get("PRQA_SANDBOX_IMAGE_ID", ""),
            sandbox_network=env.get("PRQA_SANDBOX_NETWORK", "none"),
            docker_cli=env.get("PRQA_DOCKER_CLI", "docker"),
            workdir=env.get("PRQA_LAUNCHER_WORKDIR", ""),
            lease_s=int(env.get("PRQA_LAUNCH_LEASE_S", "120")),
            heartbeat_s=float(env.get("PRQA_LAUNCH_HEARTBEAT_S", "30")),
            poll_s=float(env.get("PRQA_LAUNCH_POLL_S", "2")),
            max_artifact_mb=float(env.get("PRQA_CHECKOUT_MAX_MB", "500")),
            model_base_url=inference or legacy,
            model_proxy_admin_url=env.get("PRQA_MODEL_PROXY_ADMIN_URL", ""),
            model_proxy_mint_token=env.get("PRQA_MODEL_PROXY_MINT_TOKEN", ""),
        )


# ------------------------------------------------------------------ clients --
class WorkerClient:
    def __init__(self, cfg: LauncherConfig, *, opener: Callable[..., Any] = urllib.request.urlopen) -> None:
        self.cfg = cfg
        self.opener = opener

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any, dict[str, str]]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.cfg.worker_url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "X-Launcher-Token": self.cfg.launcher_token,
                                              "X-Launcher-Id": self.cfg.launcher_id, "User-Agent": "prqa-launcher/1.0"})
        try:
            with self.opener(req, timeout=self.cfg.http_timeout_s) as resp:
                raw = resp.read()
                headers = {k.lower(): v for k, v in resp.headers.items()}
                ctype = headers.get("content-type", "")
                return int(resp.status), (raw if "json" not in ctype else (json.loads(raw.decode("utf-8")) if raw else {})), headers
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return int(err.code), json.loads(raw.decode("utf-8")), {k.lower(): v for k, v in (err.headers.items() if err.headers else [])}
            except ValueError:
                return int(err.code), {"error": "NON_JSON", "raw": raw[:200].decode("utf-8", "replace")}, {}

    def pending(self, limit: int = 10) -> list[dict[str, Any]]:
        status, body, _ = self._call("GET", f"/launch/pending?limit={limit}")
        if status != 200:
            raise LauncherError(f"pending: HTTP {status} {str(body)[:160]}")
        return list(body.get("pending") or [])

    def claim(self, job_id: str) -> tuple[int, dict[str, Any]]:
        status, body, _ = self._call("POST", f"/jobs/{job_id}/launch/claim", {"launcher_id": self.cfg.launcher_id, "lease_s": self.cfg.lease_s})
        return status, body

    def heartbeat(self, job_id: str, *, state: str | None = None) -> tuple[int, dict[str, Any]]:
        body = {"launcher_id": self.cfg.launcher_id, "lease_s": self.cfg.lease_s}
        if state:
            body["state"] = state
        status, resp, _ = self._call("POST", f"/jobs/{job_id}/launch/heartbeat", body)
        return status, resp

    def artifact(self, job_id: str) -> tuple[bytes, dict[str, str]]:
        status, body, headers = self._call("GET", f"/jobs/{job_id}/artifact")
        if status != 200 or not isinstance(body, (bytes, bytearray)):
            raise LauncherError(f"artifact: HTTP {status} {str(body)[:160]}")
        return bytes(body), headers

    def report(self, job_id: str, outcome: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        path = {"succeeded": "result", "failed": "failed", "cancelled": "cancelled"}[outcome]
        status, body, _ = self._call("POST", f"/jobs/{job_id}/launch/{path}", {"launcher_id": self.cfg.launcher_id, **payload})
        return status, body


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class ModelProxyClient:
    """Issues / revokes per-job model tokens with the proxy's MINT credential, which the proxy limits to issuing a job token
    and revoking the tokens it issued (no listing, no other issuer's tokens, no configuration). The admin API is a unix
    socket shared with the model proxy (unix:///path); a TCP admin URL is refused — the administrative interface must
    not be reachable over any network, including the sandbox network that carries inference."""

    def __init__(self, cfg: LauncherConfig) -> None:
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.model_proxy_admin_url and self.cfg.model_proxy_mint_token and self.cfg.model_base_url)

    def check_config(self) -> None:
        """All three settings or none: a partial configuration is refused instead of silently running without a model."""
        names = {"PRQA_MODEL_PROXY_ADMIN_URL": self.cfg.model_proxy_admin_url, "PRQA_MODEL_PROXY_MINT_TOKEN": self.cfg.model_proxy_mint_token,
                 "PRQA_MODEL_PROXY_INFERENCE_URL": self.cfg.model_base_url}
        missing = sorted(n for n, v in names.items() if not v)
        if missing and len(missing) < len(names):
            raise LauncherError("model proxy configuration incomplete; missing " + ", ".join(missing))
        if not missing:
            self._socket_path()
            if not INFERENCE_URL_RE.match(self.cfg.model_base_url):
                raise LauncherError("PRQA_MODEL_PROXY_INFERENCE_URL must be http://<proxy host>[:port]/v1")

    @staticmethod
    def upstream_model(model: str) -> str:
        """The proxy's model id for an OpenCode model: prqa/<upstream> → <upstream>. Anything else is not served."""
        if not model.startswith(PROXY_MODEL_PREFIX) or len(model) <= len(PROXY_MODEL_PREFIX):
            raise LauncherError(f"model must be {PROXY_MODEL_PREFIX}<upstream model> when the model proxy is configured")
        return model[len(PROXY_MODEL_PREFIX):]

    def _socket_path(self) -> str:
        url = self.cfg.model_proxy_admin_url
        if not url.startswith("unix:///") or "?" in url or "#" in url:
            raise LauncherError("model proxy admin URL must be unix:///<socket path> (the admin API is never served over TCP)")
        return url[len("unix://"):]

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        conn = _UnixHTTPConnection(self._socket_path(), self.cfg.http_timeout_s)
        try:
            headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self.cfg.model_proxy_mint_token}
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                return int(resp.status), (json.loads(raw.decode("utf-8")) if raw else {})
            except ValueError:
                return int(resp.status), {"error": "NON_JSON"}
        except OSError as exc:
            raise LauncherError(f"model proxy admin socket unavailable: {type(exc).__name__}") from None
        finally:
            conn.close()

    def issue(self, job_id: str, model: str, ttl_s: int) -> str:
        status, body = self._call("POST", "/admin/tokens", {"job_id": job_id, "model": model, "ttl_s": ttl_s})
        if status != 201 or not body.get("token"):
            raise LauncherError(f"model proxy refused to issue a token: HTTP {status} {body.get('error')}")
        return str(body["token"])

    def revoke(self, job_id: str) -> bool:
        status, _ = self._call("DELETE", f"/admin/tokens/{job_id}")
        return status in (200, 404)


# ------------------------------------------------------------------ service --
@dataclass
class LaunchOutcome:
    job_id: str
    outcome: str                    # succeeded | failed | cancelled | skipped
    detail: dict[str, Any] = field(default_factory=dict)


class LauncherService:
    def __init__(self, cfg: LauncherConfig, *, worker: WorkerClient | None = None, runner: Any = subprocess.run,
                 model_proxy: ModelProxyClient | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not cfg.launcher_token:
            raise LauncherError("PRQA_LAUNCHER_TOKEN is required")
        if not LAUNCHER_TOKEN_RE.match(cfg.launcher_token):
            raise LauncherError("PRQA_LAUNCHER_TOKEN is malformed (expected 32-256 characters of A-Z a-z 0-9 _ -)")
        if cfg.model_proxy_mint_token and cfg.launcher_token == cfg.model_proxy_mint_token:
            raise LauncherError("PRQA_LAUNCHER_TOKEN must differ from PRQA_MODEL_PROXY_MINT_TOKEN")
        self.cfg = cfg
        self.worker = worker or WorkerClient(cfg)
        self.model_proxy = model_proxy or ModelProxyClient(cfg)
        self.model_proxy.check_config()
        if self.model_proxy.enabled and cfg.sandbox_network == "none":
            raise LauncherError("the model proxy is configured but PRQA_SANDBOX_NETWORK is none: the sandbox could not reach it")
        self.runner = runner
        self.clock = clock
        self.sleep = sleep
        self.workdir = Path(cfg.workdir or tempfile.mkdtemp(prefix="prqa-launcher-"))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._image_ref: str | None = None

    # ----------------------------------------------------- launcher-owned checks --
    def _docker(self, argv: list[str], timeout: int = 60) -> tuple[int, str, str]:
        try:
            p = self.runner([self.cfg.docker_cli, *argv], capture_output=True, shell=False, timeout=timeout, check=False, text=True)
        except FileNotFoundError:
            raise LauncherError(f"docker CLI '{self.cfg.docker_cli}' not found")
        return int(p.returncode), str(p.stdout or ""), str(p.stderr or "")

    def verify_image(self) -> str:
        """The image is pinned by ID; a tag that resolves elsewhere is refused. Returns the ID used as image reference."""
        if not re.match(r"^sha256:[0-9a-f]{64}$", self.cfg.sandbox_image_id):
            raise LauncherError("PRQA_SANDBOX_IMAGE_ID must be the pinned image ID (sha256:<64 hex>)")
        rc, out, err = self._docker(["image", "inspect", "--format", "{{.Id}}", self.cfg.sandbox_image_tag])
        if rc != 0:
            raise LauncherError(f"sandbox image {self.cfg.sandbox_image_tag} is not present locally")
        actual = out.strip()
        if actual != self.cfg.sandbox_image_id:
            raise LauncherError(f"sandbox image ID mismatch: tag resolves to {actual[:19]}…, pinned {self.cfg.sandbox_image_id[:19]}…")
        self._image_ref = actual
        return actual

    def verify_network(self) -> str:
        """'none' is always acceptable. Any other network must exist, be internal (no default route) and carry
        prqa.net=sandbox; otherwise the sandbox could reach the internet and the launcher refuses."""
        net = self.cfg.sandbox_network
        if net == "none":
            return net
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$", net):
            raise LauncherError("sandbox network name is malformed")
        rc, out, err = self._docker(["network", "inspect", "--format",
                                     '{{.Internal}} {{index .Labels "prqa.net"}} {{.Driver}} {{index .Options "com.docker.network.bridge.inhibit_ipv4"}}', net])
        if rc != 0:
            raise LauncherError(f"sandbox network {net} does not exist")
        parts = out.strip().split(" ")
        internal, label, _driver, inhibit = (parts + ["", "", "", ""])[:4]
        if internal != "true" or label != "sandbox":
            raise LauncherError(f"sandbox network {net} is not internal + labelled prqa.net=sandbox (internal={internal!r}, label={label!r})")
        if inhibit != "true":
            raise LauncherError(f"sandbox network {net} must set com.docker.network.bridge.inhibit_ipv4=true (no gateway address on the host)")
        return net

    @staticmethod
    def validate_claim(claim: dict[str, Any]) -> dict[str, Any]:
        """Only these worker-provided fields ever influence Docker arguments, and only after validation."""
        job_id = str(claim.get("job_id") or "")
        if not opencode_runner.VOLUME_SAFE.match(job_id):
            raise LauncherError("claim: unsafe job id")
        repo = str(claim.get("repository") or "")
        if not REPO_RE.match(repo):
            raise LauncherError("claim: invalid repository")
        pr = claim.get("pr") or {}
        sha = str(pr.get("head_sha") or "")
        if not SHA_RE.match(sha):
            raise LauncherError("claim: invalid head sha")
        model = str(claim.get("model") or "")
        if not MODEL_RE.match(model):
            raise LauncherError("claim: invalid model identifier")
        level = str(claim.get("test_level") or "")
        if level not in LEVELS:
            raise LauncherError("claim: invalid test level")
        runtime = int(claim.get("max_runtime_minutes") or 0)
        if not 1 <= runtime <= 240:
            raise LauncherError("claim: runtime minutes out of range")
        hashes = claim.get("expected_hashes") or {}
        if not HEX64.match(str(hashes.get("tree_sha256") or "")) or not HEX64.match(str(hashes.get("prompt_sha256") or "")):
            raise LauncherError("claim: expected hashes missing or malformed")
        base_url = str(claim.get("base_url") or "")
        if base_url and not BASE_URL_RE.match(base_url):
            raise LauncherError("claim: target base_url must be http(s)://host[:port] (no credentials, path or query)")
        art = claim.get("artifact") or {}
        if not HEX64.match(str(art.get("sha256") or "")) or not isinstance(art.get("size"), int) or art["size"] <= 0:
            raise LauncherError("claim: artifact metadata missing")
        return {"job_id": job_id, "repository": repo, "head_sha": sha, "pr_number": int(pr.get("number") or 0), "title": str(pr.get("title") or "")[:300],
                "model": model, "test_level": level, "runtime_minutes": runtime, "tree_sha256": hashes["tree_sha256"],
                "prompt_sha256": hashes["prompt_sha256"], "artifact_sha256": art["sha256"], "artifact_size": int(art["size"]),
                "base_url": base_url}

    def fetch_artifact(self, c: dict[str, Any]) -> Path:
        if c["artifact_size"] > self.cfg.max_artifact_mb * 1024 * 1024:
            raise LauncherError("artifact larger than PRQA_CHECKOUT_MAX_MB; refusing")
        data, headers = self.worker.artifact(c["job_id"])
        if len(data) != c["artifact_size"]:
            raise LauncherError(f"artifact size mismatch (claim {c['artifact_size']}, received {len(data)})")
        digest = hashlib.sha256(data).hexdigest()
        if digest != c["artifact_sha256"] or digest != str(headers.get("x-prqa-artifact-sha256") or digest):
            raise LauncherError("artifact sha256 mismatch; refusing to use it")
        job_dir = self.workdir / c["job_id"]
        job_dir.mkdir(mode=0o700, exist_ok=True)
        path = job_dir / "checkout.tar"
        path.write_bytes(data)
        try:
            manifest = read_artifact_manifest(path)
        except Exception as exc:  # noqa: BLE001
            raise LauncherError(f"artifact manifest unreadable: {type(exc).__name__}")
        if manifest.get("job_id") != c["job_id"] or manifest.get("repository") != c["repository"] or manifest.get("head_sha") != c["head_sha"] \
                or manifest.get("tree_sha256") != c["tree_sha256"] or manifest.get("prompt_sha256") != c["prompt_sha256"]:
            raise LauncherError("artifact manifest does not match the claim (job/repository/sha/hashes)")
        return path

    # --------------------------------------------------------------- lifecycle --
    def run_once(self, *, limit: int = 5) -> list[LaunchOutcome]:
        outcomes: list[LaunchOutcome] = []
        self.verify_image()
        self.verify_network()
        for item in self.worker.pending(limit):
            job_id = str(item.get("job_id") or "")
            if not opencode_runner.VOLUME_SAFE.match(job_id):
                outcomes.append(LaunchOutcome(job_id, "skipped", {"reason": "unsafe job id"})); continue
            status, claim = self.worker.claim(job_id)
            if status != 200:
                outcomes.append(LaunchOutcome(job_id, "skipped", {"reason": claim.get("error"), "http": status})); continue
            outcomes.append(self._launch(claim))
        return outcomes

    def _launch(self, claim: dict[str, Any]) -> LaunchOutcome:
        job_id = str(claim.get("job_id"))
        hb_stop = threading.Event()
        state = {"cancel": False, "lost_since": None, "last_ok": self.clock()}

        def heartbeat_loop() -> None:
            while not hb_stop.wait(self.cfg.heartbeat_s):
                try:
                    st, resp = self.worker.heartbeat(job_id, state="launching")
                except Exception:  # noqa: BLE001
                    st, resp = 0, {}
                if st == 200:
                    state["last_ok"] = self.clock(); state["lost_since"] = None
                    state["cancel"] = state["cancel"] or bool(resp.get("cancel_requested"))
                elif st == 409:
                    state["cancel"] = True                      # job is terminal on the worker: nothing we produce can be used
                elif state["lost_since"] is None:
                    state["lost_since"] = self.clock()

        def cancel_check() -> bool:
            if state["cancel"]:
                return True
            lost = state["lost_since"]
            return bool(lost is not None and self.clock() - lost > self.cfg.lease_s)   # lease lost: stop, never run unobserved

        events: list[tuple[str, dict[str, Any]]] = []
        cleanup_outcome: dict[str, Any] = {}
        token_issued = False
        artifact_path: Path | None = None
        try:
            c = self.validate_claim(claim)
            # recovery: resources of a previous (dead) attempt for this job are purged before anything is created
            lifecycle.cancel_sandbox(job_id, runner=self.runner, docker_cli=self.cfg.docker_cli)
            artifact_path = self.fetch_artifact(c)
            env: dict[str, str] = {}
            if c["base_url"]:
                env["BASE_URL"] = c["base_url"]           # reachable only if the target sits on the internal sandbox network
            model_token = False
            if self.model_proxy.enabled:
                upstream = self.model_proxy.upstream_model(c["model"])          # before any token exists
                env["PRQA_MODEL_TOKEN"] = self.model_proxy.issue(job_id, upstream, ttl_s=c["runtime_minutes"] * 60 + 300)
                env["PRQA_MODEL_BASE_URL"] = self.cfg.model_base_url
                token_issued = model_token = True
            elif c["model"].startswith(PROXY_MODEL_PREFIX):
                raise LauncherError(f"model {c['model']} is served by the PRQA model proxy, which is not configured")
            job = {"id": job_id, "execution_mode": "live", "request": {"repository": c["repository"], "test_level": c["test_level"],
                                                                        "pr": {"number": c["pr_number"], "head_sha": c["head_sha"], "title": c["title"]}}}
            plan = opencode_runner.build_sandbox_plan(image=self._image_ref or self.verify_image(), network=self.verify_network(), job=job,
                                                      model=c["model"], runtime_minutes=c["runtime_minutes"], env=env, artifact_path=str(artifact_path),
                                                      tree_sha256=c["tree_sha256"], prompt_sha256=c["prompt_sha256"], docker_cli=self.cfg.docker_cli,
                                                      model_token=model_token)
            hb = threading.Thread(target=heartbeat_loop, name=f"hb-{job_id}", daemon=True); hb.start()
            st, _ = self.worker.heartbeat(job_id, state="launching")
            if st != 200:
                raise LauncherError(f"could not confirm the lease before launch (HTTP {st})")

            def report(kind: str, detail: dict[str, Any]) -> None:
                events.append((kind, detail))
                if kind == "sandbox_cleanup":
                    cleanup_outcome.update(detail)
            result = opencode_runner.execute_plan(plan, env=env, runner=self.runner, timeout_s=c["runtime_minutes"] * 60 + 120,
                                                  cancel_check=cancel_check, report=report, poll_s=self.cfg.poll_s, sleep=self.sleep)
            hb_stop.set()
            return self._report(job_id, "succeeded", {"result": result, "cleanup": cleanup_outcome})
        except opencode_runner.SandboxCancelled as exc:
            hb_stop.set()
            if state["cancel"]:
                return self._report(job_id, "cancelled", {"cleanup": cleanup_outcome, "detail": str(exc)[:200]})
            return self._report(job_id, "failed", {"error": {"type": "LeaseLost", "message": redact_text(str(exc))[:500]}, "cleanup": cleanup_outcome})
        except Exception as exc:  # noqa: BLE001
            hb_stop.set()
            return self._report(job_id, "failed", {"error": {"type": type(exc).__name__, "message": redact_text(str(exc))[:500]}, "cleanup": cleanup_outcome})
        finally:
            hb_stop.set()
            if token_issued:
                try:
                    self.model_proxy.revoke(job_id)
                except Exception:  # noqa: BLE001
                    log.warning("model token revocation failed for %s", job_id)
            if artifact_path is not None:
                shutil.rmtree(artifact_path.parent, ignore_errors=True)

    def _report(self, job_id: str, outcome: str, payload: dict[str, Any]) -> LaunchOutcome:
        """Idempotent hand-back with bounded retries; a 409 means the worker already holds a terminal state."""
        last: dict[str, Any] = {}
        for attempt in range(1, 4):
            try:
                st, body = self.worker.report(job_id, outcome, payload)
            except Exception as exc:  # noqa: BLE001
                st, body = 0, {"error": type(exc).__name__}
            last = {"http": st, **(body if isinstance(body, dict) else {})}
            if st in (200, 409):
                break
            self.sleep(min(10.0, 2.0 * attempt))
        log.info("job %s → %s (worker: %s)", job_id, outcome, last.get("http"))
        return LaunchOutcome(job_id, outcome, {"worker": last, "cleanup_ok": (payload.get("cleanup") or {}).get("ok")})

    def reconcile(self, *, dry_run: bool = False) -> lifecycle.ReconcileReport:
        """Launcher-side orphan recovery using the worker's claim view of job status (unknown → None)."""
        def job_status(job_id: str) -> str | None:
            st, body = self.worker.claim(job_id)          # 410 carries the status of a terminal/non-claimable job
            if st == 410:
                return str(body.get("status") or "") or None
            if st in (200, 409):
                return "running"
            return None
        return lifecycle.reconcile(job_status=job_status, runner=self.runner, docker_cli=self.cfg.docker_cli, dry_run=dry_run)
