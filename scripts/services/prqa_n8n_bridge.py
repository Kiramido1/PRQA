#!/usr/bin/env python3
"""Host-side bridge that lets n8n orchestrate the PRQA pipeline stage by stage.

n8n (workflow `PRQA 09 · Label-Triggered Pipeline`) decides what runs and when; the bridge runs exactly the requested
stage of scripts/pipeline/prqa_stages.py as a child process and returns its JSON. n8n never gets a shell, a GitHub token, a
provider key, the grant key or the Discord URL — only this bridge's bearer token and the stage results.

    POST /stage/<discover|claim|prepare|promptstep|gate|submit|poll|finalize|fail|recover>
         body {"pr"?: int, "key"?: str, "reason"?: str, "generation"?: {generated_text, finish_reason, llm_error, http_status,
         response_shape, usage?: {prompt_tokens, completion_tokens, total_tokens}}}
         (generation: promptstep and gate only; handed to the stage on stdin, never in argv)
    GET  /status                                                             last stage runs (no secrets)

Listens on a Unix socket only (default ~/.local/state/prqa/bridge-sock/bridge.sock, dir mode 0700). n8n reaches it through
the `prqa-n8n-bridge-relay` container (scripts/services/prqa_bridge_relay.py), because the host firewall drops container -> host
TCP. Token: PRQA_N8N_BRIDGE_TOKEN from the secrets file.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = Path(os.environ.get("PRQA_BRIDGE_LOG_DIR") or Path.home() / ".local" / "state" / "prqa" / "bridge")
STAGES = ("discover", "claim", "prepare", "promptstep", "gate", "submit", "poll", "finalize", "fail", "recover")
GENERATION_STAGES = ("promptstep", "gate")                         # stages that read a model result on stdin
STAGE_TIMEOUT_S = {"prepare": 3600, "gate": 600, "submit": 600}                 # env build + prompt + gate can take minutes
DEFAULT_TIMEOUT_S = 300
LOG_TAIL_LINES = 20
MAX_BODY = 4096
MAX_GATE_BODY = 512 * 1024                                         # Groq's generated prompt
MAX_GENERATED_CHARS = 200_000
KEY_RE = re.compile(r"^[A-Za-z0-9._/-]+#\d+@[0-9a-f]{40}:[a-z0-9.-]+$")
SECRET_HINTS = ("token", "key=", "api key", "secret", "password", "authorization", "webhook", "bearer")


def safe_lines(text: str, n: int = LOG_TAIL_LINES) -> list[str]:
    """Last lines of a stage log, dropping any line that even mentions a credential-like word."""
    lines = [l[:300] for l in text.splitlines() if l.strip()]
    return [l for l in lines if not any(h in l.lower() for h in SECRET_HINTS)][-n:]


def generation_input(body: dict[str, Any]) -> str | None:
    """gate's model output: only the known fields, bounded, as the JSON the stage reads from stdin."""
    if "generation" not in body:
        return None
    g = body["generation"]
    if g is None:
        return json.dumps({"generation": None})
    if not isinstance(g, dict):
        raise ValueError("generation must be an object or null")
    out: dict[str, Any] = {}
    for k, limit in (("generated_text", MAX_GENERATED_CHARS), ("finish_reason", 64), ("llm_error", 300), ("response_shape", 64)):
        v = g.get(k)
        if v is not None and not isinstance(v, str):
            raise ValueError(f"generation.{k} must be a string")
        out[k] = v[:limit] if isinstance(v, str) else None
    hs = g.get("http_status")
    out["http_status"] = int(hs) if isinstance(hs, (int, str)) and str(hs).isdigit() else None
    usage = g.get("usage")
    if isinstance(usage, dict):                                    # provider-reported token counts only, never text
        out["usage"] = {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                        if isinstance(usage.get(k), int) and not isinstance(usage.get(k), bool) and 0 <= usage[k] < 10_000_000}
    return json.dumps({"generation": out})


def stage_argv(stage: str, body: dict[str, Any]) -> list[str]:
    if stage not in STAGES:
        raise ValueError("unknown stage")
    argv = [stage]
    if "generation" in body:
        if stage not in GENERATION_STAGES:
            raise ValueError("generation is accepted by the promptstep and gate stages only")
        argv.append("--stdin")
    if "pr" in body:
        if not isinstance(body["pr"], int) or isinstance(body["pr"], bool) or body["pr"] <= 0:
            raise ValueError("pr must be a positive integer")
        argv += ["--pr", str(body["pr"])]
    if "key" in body:
        if not isinstance(body["key"], str) or not KEY_RE.match(body["key"]):
            raise ValueError("invalid key")
        argv += ["--key", body["key"]]
    if "reason" in body:
        argv += ["--reason", re.sub(r"[^\w .:;,()/#@=-]", "", str(body["reason"]))[:300]]
    return argv


class Stages:
    def __init__(self, python: str = sys.executable) -> None:
        self.python = python
        self.recent: deque[dict[str, Any]] = deque(maxlen=20)
        self.lock = threading.Lock()

    def cmd(self, argv: list[str]) -> list[str]:
        return ["systemd-inhibit", "--what=sleep:idle", "--who=prqa", f"--why=PRQA n8n stage {argv[0]}", "--mode=block",
                self.python, "-u", str(ROOT / "scripts" / "pipeline" / "prqa_stages.py"), *argv]

    def run(self, argv: list[str], stdin: str | None = None) -> dict[str, Any]:
        stage = argv[0]
        started = time.time()
        LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        log = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{stage}.log"
        try:
            p = subprocess.run(self.cmd(argv), cwd=ROOT, capture_output=True, text=True, timeout=STAGE_TIMEOUT_S.get(stage, DEFAULT_TIMEOUT_S),
                               **({"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}))
            out, err, rc = p.stdout, p.stderr, p.returncode
        except subprocess.TimeoutExpired as exc:
            out, err, rc = "", (exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""), -1
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(err)
        try:
            res = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except ValueError:
            res = {}
        if not res:
            res = {"stage": stage, "error": "stage timed out" if rc == -1 else f"stage exited {rc} without a result"}
        res.update(exit_code=rc, duration_s=round(time.time() - started, 1), log_tail=safe_lines(err))
        with self.lock:
            self.recent.append({"stage": stage, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "exit_code": rc,
                                "summary": {k: res.get(k) for k in ("action", "claimed", "ok", "terminal", "status", "verdict", "busy", "error") if k in res}})
        return res


class UnixHTTPServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def get_request(self):                                       # BaseHTTPRequestHandler expects a (host, port) address
        conn, _ = super().get_request()
        return conn, ("unix", 0)


def make_handler(stages: Stages, token: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:     # request line only; never headers or bodies
            sys.stderr.write(f"{time.strftime('%H:%M:%S')} {fmt % args}\n")

        def _reply(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, indent=1, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            return hmac.compare_digest(self.headers.get("Authorization", "").encode(), f"Bearer {token}".encode())

        def do_GET(self) -> None:
            if not self._authorized():
                return self._reply(401, {"error": "unauthorized"})
            if self.path != "/status":
                return self._reply(404, {"error": "not found"})
            with stages.lock:
                self._reply(200, {"recent_stages": list(stages.recent)})

        def do_POST(self) -> None:
            if not self._authorized():
                return self._reply(401, {"error": "unauthorized"})
            m = re.fullmatch(r"/stage/([a-z]+)", self.path)
            if not m:
                return self._reply(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > (MAX_GATE_BODY if m.group(1) in GENERATION_STAGES else MAX_BODY):
                return self._reply(413, {"error": "body too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                argv = stage_argv(m.group(1), body)
                stdin = generation_input(body)
            except ValueError as exc:
                return self._reply(400, {"error": str(exc)})
            self._reply(200, stages.run(argv, stdin))
    return H


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--socket", type=Path, default=Path.home() / ".local" / "state" / "prqa" / "bridge-sock" / "bridge.sock")
    args = ap.parse_args(argv)
    token = os.environ.pop("PRQA_N8N_BRIDGE_TOKEN", "")
    if len(token) < 32:
        print("PRQA_N8N_BRIDGE_TOKEN missing (load ~/.config/prqa/secrets.env)", file=sys.stderr)
        return 2
    args.socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.socket.parent, 0o700)
    args.socket.unlink(missing_ok=True)
    srv = UnixHTTPServer(str(args.socket), make_handler(Stages(), token))
    os.chmod(args.socket, 0o600)
    print(f"prqa n8n bridge listening on {args.socket}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
