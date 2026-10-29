"""Mock external services for Phase 1 (review-only). Stdlib only.

Routes (all under one port, default 8090):
  GET  /healthz
  GET  /github/repos/{owner}/{repo}/pulls/{n}            PR metadata (fixture)          [Accept: application/vnd.github.diff -> diff]
  GET  /github/repos/{owner}/{repo}/pulls/{n}/files      changed files (fixture)
  POST /github/repos/{owner}/{repo}/issues/{n}/comments  records the comment, returns 201 (mock "PR comment")
  POST /llm/v1/chat/completions                          returns a chat completion whose content is a PR-specific prompt
                                                          rendered from the reference template (deterministic, no model)
  POST /discord/api/webhooks/{id}/{token}                 records the payload, returns 204
  GET  /target/openapi.json                               OpenAPI of the synthetic target API
  GET  /mock/received                                     everything recorded (for synthetic tests / review)
  DELETE /mock/received                                   clear recordings

Every response for GitHub/LLM carries the header ``X-PRQA-Mock: true``.
Only synthetic-org/* repositories are served; anything else returns 404 with MOCK_REFUSES_NON_SYNTHETIC.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("prqa.mocks")
FIXTURES = Path(os.environ.get("PRQA_FIXTURES_DIR", "fixtures")) / "synthetic"
PROMPTS = Path(os.environ.get("PRQA_PROMPTS_DIR", "prompts"))
DATA = Path(os.environ.get("PRQA_MOCK_DATA_DIR", "/data"))
MAX_BODY = 8 * 1024 * 1024
_LOCK = threading.Lock()
_RECEIVED: list[dict[str, Any]] = []
_RECEIVED_FILE = DATA / "received.jsonl"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _record(kind: str, path: str, body: Any, headers: dict[str, str]) -> dict[str, Any]:
    item = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "kind": kind, "path": path,
        "headers": {k: ("[present]" if k in ("authorization", "x-hub-signature-256") else v) for k, v in headers.items()
                    if k in ("authorization", "content-type", "user-agent", "x-hub-signature-256", "x-github-delivery", "x-prqa-job")},
        "body": body,
    }
    with _LOCK:
        _RECEIVED.append(item)
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            with _RECEIVED_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        except OSError:
            pass
    return item


def _llm_content(request: dict[str, Any]) -> str:
    """Deterministic 'generated prompt': the fixture prompt, re-titled from the request so reviewers
    can see the job data flowing through. No model is involved."""
    fixture = _load("llm-response.synthetic.json")["choices"][0]["message"]["content"]
    user = next((m.get("content", "") for m in request.get("messages", []) if m.get("role") == "user"), "")
    m = re.search(r'"title":\s*"([^"]{1,200})"', user)
    title = m.group(1) if m else "SYNTHETIC PR"
    level = re.search(r"test_level:\s*([a-z]+)", user)
    content = fixture.replace("Add status filter and cancellation reason to orders API (SYNTHETIC)", title)
    content += f"\n\n<!-- mock-llm: test_level={level.group(1) if level else 'unknown'}; rendered {datetime.now(UTC).isoformat(timespec='seconds')} -->\n"
    return content


class Handler(BaseHTTPRequestHandler):
    server_version = "prqa-mocks/0.1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s %s", self.command, self.path)

    def _send(self, status: int, payload: Any = None, *, content_type: str = "application/json; charset=utf-8", raw: bytes | None = None) -> None:
        data = raw if raw is not None else (json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b"")
        self.send_response(status)
        self.send_header("X-PRQA-Mock", "true")
        if data:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return None
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"_raw": raw[:2000].decode("utf-8", "replace")}

    def _route(self, method: str) -> None:
        path = urlsplit(self.path).path
        headers = {k.lower(): v for k, v in self.headers.items()}
        if method == "GET" and path == "/healthz":
            return self._send(200, {"ok": True, "mock": True})
        if path == "/mock/received":
            if method == "DELETE":
                with _LOCK:
                    _RECEIVED.clear()
                return self._send(200, {"cleared": True})
            with _LOCK:
                return self._send(200, {"count": len(_RECEIVED), "received": list(_RECEIVED)})
        if method == "GET" and path == "/target/openapi.json":
            return self._send(200, _load("openapi.synthetic.json"))

        m = re.match(r"^/github/repos/([^/]+)/([^/]+)/pulls/(\d+)(/files)?$", path)
        if m and method == "GET":
            full = f"{m.group(1)}/{m.group(2)}"
            if not full.startswith("synthetic-org/"):
                return self._send(404, {"message": "MOCK_REFUSES_NON_SYNTHETIC", "repository": full})
            fixture = _load("github-pr-labeled.synthetic.json")
            if int(m.group(3)) != fixture["number"]:
                return self._send(404, {"message": "Not Found (mock: only the fixture PR exists)"})
            if m.group(4):
                return self._send(200, _load("pr-files.synthetic.json"))
            if "diff" in headers.get("accept", ""):
                return self._send(200, raw=(FIXTURES / "pr-diff.synthetic.diff").read_bytes(), content_type="application/vnd.github.v3.diff; charset=utf-8")
            pr = dict(fixture["pull_request"])
            pr["_mock"] = True
            return self._send(200, pr)

        m = re.match(r"^/github/repos/([^/]+)/([^/]+)/issues/(\d+)/comments$", path)
        if m and method == "POST":
            full = f"{m.group(1)}/{m.group(2)}"
            body = self._body()
            if not full.startswith("synthetic-org/"):
                _record("github_comment_refused", path, body, headers)
                return self._send(404, {"message": "MOCK_REFUSES_NON_SYNTHETIC", "repository": full})
            item = _record("github_comment", path, body, headers)
            return self._send(201, {"id": int(time.time()), "html_url": f"https://github.com/{full}/pull/{m.group(3)}#issuecomment-mock",
                                    "body": (body or {}).get("body", ""), "_mock": True, "recorded_at": item["ts"]})

        if method == "POST" and path == "/llm/v1/chat/completions":
            body = self._body() or {}
            _record("llm_request", path, {"model": body.get("model"), "messages": [{"role": m.get("role"), "chars": len(m.get("content", ""))} for m in body.get("messages", [])]}, headers)
            template = _load("llm-response.synthetic.json")
            template.pop("_synthetic", None)
            template["created"] = int(time.time())
            template["choices"][0]["message"]["content"] = _llm_content(body)
            return self._send(200, template)

        m = re.match(r"^/discord/api/webhooks/([^/]+)/([^/]+)$", path)
        if m and method == "POST":
            body = self._body()
            _record("discord_webhook", path, body, headers)
            return self._send(204)

        _record("unmatched", path, None, headers)
        return self._send(404, {"message": "mock route not found", "path": path})

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._route("DELETE")


def serve(bind: str = "0.0.0.0", port: int = 8090) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    httpd = ThreadingHTTPServer((bind, port), Handler)
    log.info("prqa-mocks listening on %s:%s (fixtures=%s)", bind, port, FIXTURES)
    httpd.serve_forever()


if __name__ == "__main__":
    serve(os.environ.get("PRQA_BIND", "0.0.0.0"), int(os.environ.get("PRQA_PORT", "8090")))
