"""Scripted FAKE Groq chat-completions endpoint for tests/test_opencode_proxy_auth.py ONLY (loopback, offline).

Runs inside the model proxy's network namespace on 127.0.0.1, so only the proxy can reach it. It answers the way a
tool-calling model would, driven only by the request shape — never by a real model:
  * a request without tools (OpenCode's title agent) → a short title;
  * the build agent's first turn → a `read` tool call on the checkout's src/app.py;
  * after that tool result → a `write` tool call that writes the synthetic result document (P2IT_RESULT_DOC) to
    $PRQA_EVIDENCE_DIR/qa-result.json, which the real entrypoint then prints as the job result;
  * after that → a `write` tool call OUTSIDE the evidence directory (/work/prqa-outside.txt), which the sandbox's
    OpenCode permission rules must deny;
  * after that → a final text answer.
With P2IT_CAPTURE_FULL=1 (opt-in, test_opencode_proxy_auth.py PRQA_OCAUTH_CAPTURE_OUT) the complete upstream body of
the build agent's first tool-bearing request is also printed once as a `p2it_capture` line — the exact payload the
proxy would send to Groq for that turn (synthetic prompt and checkout only; the proxy never forwards credentials in
the body). One JSON line per request on stdout: path, whether the injected credential is exactly the synthetic provider key, the
body's key names, the locked-contract fields, message roles, tool names and whether the read tool's result carried the
file's content — never header values or message text.
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EXPECTED = "Bearer " + os.environ.get("P2IT_SYNTHETIC_PROVIDER_KEY", "")
RESULT_DOC = os.environ.get("P2IT_RESULT_DOC", "{}")
READ_PATH = "/checkout/repo/src/app.py"
WRITE_PATH = "/work/evidence/qa-result.json"
OUTSIDE_PATH = "/work/prqa-outside.txt"
FINAL_TEXT = "PRQA-OC-PROXY-OK"


def _args(tool: dict, values: dict) -> str:
    params = (tool.get("function") or {}).get("parameters") or {}
    props = params.get("properties") or {}
    out = {}
    for name in props:
        low = name.lower()
        if "path" in low:
            out[name] = values["path"]
        elif low == "content" and "content" in values:
            out[name] = values["content"]
    return json.dumps(out)


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        msgs = body.get("messages") or []
        tools = {(t.get("function") or {}).get("name"): t for t in body.get("tools") or []}
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        read_ok = any("print('hi')" in json.dumps(m.get("content")) for m in tool_msgs[:1])
        msg = {"role": "assistant", "content": None}
        if not tools:
            step, msg["content"] = "title", "PRQA proxy auth"
        elif not tool_msgs and "read" in tools:
            step = "tool_read"
            msg["tool_calls"] = [{"id": "call_read_1", "type": "function",
                                  "function": {"name": "read", "arguments": _args(tools["read"], {"path": READ_PATH})}}]
        elif len(tool_msgs) == 1 and "write" in tools:
            step = "tool_write"
            msg["tool_calls"] = [{"id": "call_write_1", "type": "function",
                                  "function": {"name": "write", "arguments": _args(tools["write"], {"path": WRITE_PATH, "content": RESULT_DOC})}}]
        elif len(tool_msgs) == 2 and "write" in tools:
            step = "tool_write_outside"
            msg["tool_calls"] = [{"id": "call_write_2", "type": "function",
                                  "function": {"name": "write", "arguments": _args(tools["write"], {"path": OUTSIDE_PATH, "content": "x"})}}]
        else:
            step, msg["content"] = "final", FINAL_TEXT
        if os.environ.get("P2IT_CAPTURE_FULL") == "1" and step == "tool_read":
            print(json.dumps({"p2it_capture": step, "body": body}), flush=True)
        print(json.dumps({"p2it_upstream": True, "step": step, "path": self.path, "model": body.get("model"),
                          "auth_is_synthetic_key": self.headers.get("Authorization") == EXPECTED, "body_keys": sorted(body),
                          "header_names": sorted(k.lower() for k in self.headers),
                          "contract": {k: body.get(k) for k in ("stream", "temperature", "reasoning_effort", "include_reasoning", "max_completion_tokens")},
                          "roles": [m.get("role") for m in msgs], "tools": sorted(t for t in tools if t), "tool_choice": body.get("tool_choice"),
                          "tool_results": len(tool_msgs), "read_result_has_file_content": read_ok,
                          "last_tool_result_is_denial": bool(tool_msgs) and any(w in json.dumps(tool_msgs[-1].get("content")).lower()
                                                                                 for w in ("denied", "rejected", "not allowed", "permission")),
                          "tool_param_names": {k: sorted(((tools[k].get("function") or {}).get("parameters") or {}).get("properties") or {})
                                               for k in ("read", "write") if k in tools}}), flush=True)
        data = json.dumps({"id": f"chatcmpl-fake-{step}", "object": "chat.completion", "created": 1761500000, "model": body.get("model"),
                           "choices": [{"index": 0, "message": {**msg, "reasoning": "FAKE HIDDEN REASONING"},
                                        "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
                           "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}, "x_groq": {"id": "req_fake"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Fake).serve_forever()
