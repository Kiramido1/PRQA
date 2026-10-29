#!/usr/bin/env python3
"""Policy-enforcing Docker socket proxy (stdlib only). Listens on a unix socket for the launcher's Docker CLI
(DOCKER_HOST=unix://<listen>), evaluates every request with docker_policy.evaluate (operation allowlist + body-level
validation) and forwards only approved requests to the real Docker socket. Denied requests get 403 and are logged by
method/path/reason only. No streaming hijack is supported by design (attach/exec are denied), so the launcher's
transport uses create/cp/start/wait/logs only.

    python3 proxy/docker_policy_proxy.py --listen /run/prqa/docker-policy.sock --upstream /var/run/docker.sock \\
        --image-id sha256:<pinned> [--network prqa_sandbox]
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import logging
import os
import socket
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from docker_policy import NAME_RE, Policy, evaluate, sandbox_networks  # noqa: E402

log = logging.getLogger("prqa.docker-proxy")
MAX_JSON_BODY = 1024 * 1024
MAX_DRAIN_BYTES = 16 * 1024 * 1024
MAX_CHUNKS = 1000000
MAX_TRAILERS = 64
CHUNK_SIZE_RE = re.compile(rb"[0-9A-Fa-f]+")
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 300.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


CONTAINER_ID_PATH = re.compile(r"^((?:/v\d+\.\d+)?)/containers/([0-9a-f]{12,64})((?:/[a-z]+)?(?:\?.*)?)$")


class Handler(BaseHTTPRequestHandler):
    policy: Policy
    upstream: str
    protocol_version = "HTTP/1.1"
    timeout = 300
    # reply with a parseable HTTP/1.1 status line even when the request line is garbage
    # (the stdlib would otherwise fall back to statusless HTTP/0.9-style errors)
    default_request_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: D102 — quiet; decisions are logged explicitly
        pass

    def _reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command.upper() != "HEAD":
            self.wfile.write(data)

    def _read_body(self) -> bytes:
        """Request body with the size cap applied; supports chunked uploads (the docker CLI streams `docker cp`).

        Framing is strict and fails closed: Transfer-Encoding must be exactly chunked and never combined with
        Content-Length, chunk sizes must be non-negative hex with CRLF/LF terminators, trailers are bounded, and any
        malformed body raises before a request is forwarded. The archive cap applies to PUT …/archive bodies only;
        every other body is capped at MAX_JSON_BODY.
        """
        limit = self.policy.max_archive_bytes if (self.command.upper() == "PUT" and self.path.split("?")[0].rstrip("/").endswith("/archive")) else MAX_JSON_BODY
        te_values = self.headers.get_all("Transfer-Encoding") or []
        if te_values:
            codings = {c.strip().lower() for v in te_values for c in v.split(",") if c.strip()}
            if codings != {"chunked"}:
                raise ValueError("unsupported transfer encoding")
            if self.headers.get_all("Content-Length"):
                raise ValueError("conflicting Content-Length and Transfer-Encoding headers")
        content_lengths = self.headers.get_all("Content-Length") or []
        if len(content_lengths) > 1 and any(c.strip() != content_lengths[0].strip() for c in content_lengths):
            raise ValueError("conflicting Content-Length headers")
        if not te_values:
            length = int(content_lengths[0] or 0) if content_lengths else 0
            if length < 0 or length > limit:
                if 0 < length <= MAX_DRAIN_BYTES:
                    drained = 0
                    while drained < length:
                        leftover = self.rfile.read(min(65536, length - drained))
                        if not leftover:
                            raise ValueError("truncated body")
                        drained += len(leftover)
                raise ValueError("body too large")
            data = self.rfile.read(length) if length else b""
            if len(data) != length:
                raise ValueError("truncated body")
            return data
        chunks: list[bytes] = []
        total = 0
        nchunks = 0
        while True:
            line = self.rfile.readline(65537)
            if not line:
                raise ValueError("truncated chunked body")
            raw_size = line.split(b";", 1)[0].strip()
            if not CHUNK_SIZE_RE.fullmatch(raw_size):
                raise ValueError("invalid chunk size line")
            size = int(raw_size, 16)
            if size == 0:
                trailers = 0
                while True:
                    t = self.rfile.readline(8193)
                    if t == b"":
                        raise ValueError("truncated chunked body")
                    if t in (b"\r\n", b"\n"):
                        break
                    if len(t) > 4096:
                        raise ValueError("chunked trailer line too large")
                    trailers += 1
                    if trailers > MAX_TRAILERS:
                        raise ValueError("too many chunked trailers")
                break
            nchunks += 1
            if nchunks > MAX_CHUNKS:
                raise ValueError("too many chunks")
            total += size
            if total > limit:
                raise ValueError("body too large")
            chunk = self.rfile.read(size)
            if len(chunk) != size:
                raise ValueError("truncated chunked body")
            chunks.append(chunk)
            term = self.rfile.readline(3)
            if term not in (b"\r\n", b"\n", b""):
                raise ValueError("invalid chunk framing")
        return b"".join(chunks)

    def _canonical_path(self, raw_path: str) -> str:
        """The docker CLI sometimes addresses a container by the ID it got from an inspect (e.g. `docker logs`).
        IDs carry no job binding, so the proxy resolves them itself on the real daemon and rewrites the request to the
        container's NAME; only PRQA job-named containers resolve, anything else stays an (unknown, denied) reference."""
        m = CONTAINER_ID_PATH.match(raw_path)
        if not m:
            return raw_path
        prefix, ref, rest = m.group(1), m.group(2), m.group(3)
        conn = UnixHTTPConnection(self.upstream, timeout=30)
        try:
            conn.request("GET", f"/containers/{ref}/json", headers={"Host": "docker"})
            resp = conn.getresponse()
            data = resp.read()
            if resp.status != 200:
                return raw_path
            info = json.loads(data)
            name = str(info.get("Name") or "").lstrip("/")
            labels = (info.get("Config") or {}).get("Labels") or {}
            if NAME_RE.match(name) and labels.get("prqa.job") and name.endswith("-" + labels["prqa.job"]):
                log.info("resolved container id %s… → %s", ref[:12], name)
                return f"{prefix}/containers/{name}{rest}"
            return raw_path
        except (OSError, ValueError, http.client.HTTPException):
            return raw_path
        finally:
            conn.close()

    def _handle(self) -> None:
        method = self.command.upper()
        path_only = self.path.split("?")[0]
        if urlsplit(self.path).netloc or urlsplit(self.path).scheme:
            log.warning("decision deny %s %s reason=%r forwarded=false", method, path_only, "request target must be origin-form")
            self.close_connection = True
            self._reply(400, {"message": "prqa-docker-proxy: origin-form request target required"})
            return
        if any("upgrade" in (v or "").lower() for v in (self.headers.get_all("Connection") or [])) or self.headers.get("Upgrade"):
            log.warning("decision deny %s %s reason=%r forwarded=false", method, path_only, "connection upgrade (hijack) is not supported")
            self.close_connection = True
            self._reply(403, {"message": "prqa-docker-proxy: upgrade/hijack not permitted"})
            return
        try:
            body = self._read_body()
        except (ValueError, TimeoutError) as exc:
            log.warning("decision deny %s %s reason=%r forwarded=false", method, path_only, str(exc) or type(exc).__name__)
            self.close_connection = True
            self._reply(413 if "large" in str(exc) else 400, {"message": f"prqa-docker-proxy: {exc or 'request body read timed out'}"})
            return
        path_for_policy = self._canonical_path(self.path)          # 64-hex container IDs → the job-named container they belong to
        path_only = path_for_policy.split("?")[0]                     # audit lines name the job-bound container, never a bare ID
        decision = evaluate(self.policy, method, path_for_policy, body)
        if not decision.allowed:
            log.warning("decision deny %s %s reason=%r forwarded=false", method, path_only, decision.reason)
            self._reply(403, {"message": f"prqa-docker-proxy: {decision.reason}"})
            return
        send_path = decision.forwarded_path or path_for_policy
        if decision.forwarded_path:
            log.info("rewrote %s %s → %s (prqa-labelled only)", method, path_only, send_path)
        log.info("decision allow %s %s kind=%s job=%s body=%d forwarded=true", method, path_only, decision.kind, decision.job_id or "-", len(body))
        conn = UnixHTTPConnection(self.upstream)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP and k.lower() != "host"}
        headers["Host"] = "docker"
        headers["Content-Length"] = str(len(body))              # always explicit: chunked uploads were de-chunked above
        try:
            conn.request(method, send_path, body=body if body else None, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                # Server/Date are added by send_response itself; CL is re-framed below
                if k.lower() in HOP_BY_HOP or k.lower() in ("content-length", "server", "date"):
                    continue
                self.send_header(k, v)
            chunked = (resp.getheader("Transfer-Encoding") or "").lower() == "chunked"
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            else:
                data = resp.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            log.info("upstream %s %s status=%d", method, path_only, resp.status)
        except (OSError, http.client.HTTPException) as exc:
            log.error("upstream failure for %s %s: %s", method, path_only, type(exc).__name__)
            self._reply(502, {"message": "prqa-docker-proxy: upstream unavailable"})
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _handle

    def do_PATCH(self) -> None:  # noqa: N802 — never used by the launcher: fail closed without touching the body
        log.warning("decision deny PATCH %s reason=%r forwarded=false", self.path.split("?")[0], "method not supported")
        self.close_connection = True
        self._reply(405, {"message": "prqa-docker-proxy: method not permitted"})

    do_OPTIONS = do_CONNECT = do_TRACE = do_PATCH


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(listen: str, upstream: str, policy: Policy, socket_gid: int = 0) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    Handler.policy = policy
    Handler.upstream = upstream
    if os.path.exists(listen):
        os.unlink(listen)
    Path(listen).parent.mkdir(parents=True, exist_ok=True)
    # Bind under a temporary sibling name, set group and mode there, then rename into place: the socket appears at
    # `listen` only once it is 0660 (binding at `listen` and chmod-ing afterwards left it at 0777 & ~umask in between).
    staging = f"{listen}.{os.getpid()}.{threading.get_ident()}.tmp"
    if os.path.exists(staging):
        os.unlink(staging)
    httpd = UnixServer(staging, Handler)
    try:
        # the socket is 0660 owner+group; in the phase2 deployment the proxy (root, it holds the real daemon
        # socket) chowns it to the launcher's group so that uid 10002 can connect through the shared volume
        if socket_gid:
            try:
                os.chown(staging, -1, socket_gid)
            except OSError as exc:
                log.warning("cannot chown %s to gid %d (%s); leaving root:root 0660", listen, socket_gid, exc)
        os.chmod(staging, 0o660)
        os.replace(staging, listen)
    except BaseException:
        httpd.server_close()
        if os.path.exists(staging):
            os.unlink(staging)
        raise
    log.info("prqa-docker-proxy listening on %s → %s (images=%d, networks=%s)", listen, upstream, len(policy.allowed_image_ids), sorted(policy.allowed_networks))
    httpd.serve_forever()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PRQA policy-enforcing Docker socket proxy")
    ap.add_argument("--listen", required=True)
    ap.add_argument("--upstream", default="/var/run/docker.sock")
    ap.add_argument("--image-id", required=True, action="append", help="pinned sandbox image ID (sha256:…); repeatable")
    ap.add_argument("--image-tag", default="prqa/opencode-sandbox:local")
    ap.add_argument("--network", default=None, help="verified internal sandbox network name (besides 'none')")
    ap.add_argument("--socket-gid", type=int, default=0, help="chown the listen socket to this gid (phase2: the launcher's)")
    ap.add_argument("--log-file", default=None, help="append decision log here as well as stdout")
    args = ap.parse_args(argv)
    if args.log_file:
        fh = logging.FileHandler(args.log_file); fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh); logging.getLogger().setLevel(logging.INFO)
    try:
        nets = sandbox_networks(args.network)
    except ValueError as exc:
        print(f"prqa-docker-proxy configuration error: {exc}", file=sys.stderr)
        return 2
    policy = Policy(allowed_image_ids=frozenset(args.image_id), allowed_image_tags=frozenset({args.image_tag}), allowed_networks=nets)
    serve(args.listen, args.upstream, policy, args.socket_gid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
