"""Loopback FAKE model provider for the DISPOSABLE Phase-2 container test (tests/test_phase2_containers.py) only.

Runs in the model proxy's network namespace (compose `network_mode: service:prqa-model-proxy`) on 127.0.0.1, so the
proxy's loopback-only plain-http rule admits it and nothing leaves the host. Answers every chat completion with a
fixed synthetic Groq-shaped reply (with a reasoning field the proxy must strip) and prints one JSON line per request:
method, path, whether the injected credential equals the synthetic key from P2IT_SYNTHETIC_PROVIDER_KEY, the body's key
names, the locked-contract fields and the message roles — never header values, prompts or message content.
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EXPECTED = "Bearer " + os.environ.get("P2IT_SYNTHETIC_PROVIDER_KEY", "")


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        print(json.dumps({"p2it_upstream": True, "method": self.command, "path": self.path, "model": body.get("model"),
                          "auth_is_synthetic_key": self.headers.get("Authorization") == EXPECTED,
                          "host": self.headers.get("Host"), "body_keys": sorted(body), "header_names": sorted(k.lower() for k in self.headers),
                          "contract": {k: body.get(k) for k in ("stream", "temperature", "reasoning_effort", "include_reasoning", "max_completion_tokens")},
                          "roles": [m.get("role") for m in body.get("messages") or []]}), flush=True)
        data = json.dumps({"id": "p2it-1", "object": "chat.completion", "created": 1761500000, "model": body.get("model"),
                           "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "P2IT MOCK COMPLETION",
                                                                                          "reasoning": "P2IT HIDDEN REASONING"}}],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}, "x_groq": {"id": "req_p2it"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Fake).serve_forever()
