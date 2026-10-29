"""Model proxy: the locked Groq upstream request contract and the client-side response contract OpenCode depends on.

Offline only — the "provider" is a loopback fake that records exactly what the proxy sends; credentials are synthetic.
The client side mirrors what OpenCode 1.19.3 (@ai-sdk/openai-compatible) was measured to send: POST
/v1/chat/completions, `Authorization: Bearer <job token>`, stream=true, stream_options.include_usage, max_tokens
32000, 10 function tools with tool_choice=auto, and session headers. Upstream must receive the locked contract only:
stream=false, temperature 0.2, reasoning_effort low, include_reasoning false, max_completion_tokens <= 3000.

    PYTHONPATH=worker python3 -m unittest tests.test_model_proxy_contract -v
"""
from __future__ import annotations

import http.client
import itertools
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "proxy"), str(ROOT / "worker")]

import model_proxy as mp  # noqa: E402

PROVIDER_KEY = "gsk_SYNTHETIC_contract_provider_key_000000000000000000"
ADMIN = "admin-token-SYNTHETIC-contract-0123456789"
MODEL = "openai/gpt-oss-120b"
JOB = "20261027-contract0001"
HIDDEN_REASONING = "SYNTHETIC-HIDDEN-REASONING-must-never-reach-the-client"
TOOLS = [{"type": "function", "function": {"name": n, "description": "d", "parameters": {"type": "object", "properties": {}}}}
         for n in ("bash", "edit", "glob", "grep", "read", "skill", "task", "todowrite", "webfetch", "write")]
OPENCODE_HEADERS = {"x-opencode-session-id": "ses_synthetic", "x-session-affinity": "ses_synthetic", "x-session-id": "ses_synthetic",
                    "User-Agent": "opencode/1.19.3 ai-sdk/openai-compatible", "Cookie": "a=b", "Origin": "http://evil"}


def completion(content="SYNTHETIC ANSWER", tool_calls=None, finish="stop"):
    msg = {"role": "assistant", "content": content, "reasoning": HIDDEN_REASONING}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"id": "chatcmpl-synthetic", "object": "chat.completion", "created": 1761500000, "model": MODEL,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18, "queue_time": 0.1},
            "system_fingerprint": "fp_synthetic", "x_groq": {"id": "req_synthetic"}}


class Upstream(BaseHTTPRequestHandler):
    received: list = []
    reply: object = None

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        Upstream.received.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                  "body": json.loads(self.rfile.read(n) or b"{}")})
        data = Upstream.reply if isinstance(Upstream.reply, bytes) else json.dumps(Upstream.reply or completion()).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Request-Id", "req-synthetic-contract"); self.end_headers(); self.wfile.write(data)


def parse_sse(raw: bytes) -> list:
    events = []
    for block in raw.decode("utf-8").split("\n\n"):
        if block.strip():
            assert block.startswith("data: "), block
            events.append(block[6:])
    return events


_TURNS = itertools.count()


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream); cls.upstream.daemon_threads = True
        cls.upstream.handle_error = lambda *a: None
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.cfg = mp.ProxyConfig(upstream_origin=f"http://127.0.0.1:{cls.upstream.server_port}", upstream_prefix="/openai/v1",
                                 allowed_model=MODEL, provider_key=PROVIDER_KEY, admin_token=ADMIN, max_requests_per_job=1000)
        cls.tmp = tempfile.mkdtemp(prefix="prqa-contract-")
        cls.sock = os.path.join(cls.tmp, "admin.sock")
        cls.inference, cls.admin = mp.make_servers(cls.cfg, "127.0.0.1", 0, cls.sock, admin_socket_gid=os.getgid())
        for srv in (cls.inference, cls.admin):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        conn = http.client.HTTPConnection("localhost", timeout=10)
        conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); conn.sock.connect(cls.sock)
        conn.request("POST", "/admin/tokens", body=json.dumps({"job_id": JOB, "model": MODEL, "ttl_s": 600}),
                     headers={"Authorization": "Bearer " + ADMIN, "Content-Type": "application/json"})
        resp = conn.getresponse(); cls.token = json.loads(resp.read())["token"]; conn.close()

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.inference, cls.admin, cls.upstream):
            srv.shutdown(); srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        Upstream.received = []; Upstream.reply = None

    def chat(self, body, token=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.inference.server_port, timeout=10)
        h = {"Content-Type": "application/json", **(headers or {})}
        if token is not False:
            h["Authorization"] = "Bearer " + (token or self.token)
        conn.request("POST", "/v1/chat/completions", body=json.dumps(body).encode(), headers=h)
        resp = conn.getresponse(); raw = resp.read(); hdrs = {k.lower(): v for k, v in resp.getheaders()}; conn.close()
        return resp.status, raw, hdrs

    def opencode_body(self, **over):
        body = {"model": MODEL, "max_tokens": 32000, "stream": True, "stream_options": {"include_usage": True}, "tool_choice": "auto",
                "tools": TOOLS, "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                "temperature": 1.5, "top_p": 0.3, "n": 3, "reasoning_effort": "high", "include_reasoning": True,
                "provider": {"order": ["anything"]}, "user": "u"}
        # a fresh turn each call: identical consecutive messages are treated as a client retry (temperature raised)
        body["messages"].append({"role": "user", "content": f"turn {next(_TURNS)}"})
        body.update(over)
        return body

    # --------------------------------------------------------------------------------------------- upstream --
    def test_upstream_receives_exactly_the_locked_contract(self):
        st, raw, _ = self.chat(self.opencode_body(), headers=OPENCODE_HEADERS)
        self.assertEqual(st, 200, raw)
        self.assertEqual(len(Upstream.received), 1)
        up = Upstream.received[0]
        self.assertEqual(up["path"], "/openai/v1/chat/completions")
        b = up["body"]
        self.assertEqual(set(b), {"model", "messages", "tools", "tool_choice", "stream", "temperature", "reasoning_effort",
                                  "include_reasoning", "max_completion_tokens"})
        self.assertEqual((b["model"], b["stream"], b["temperature"], b["reasoning_effort"], b["include_reasoning"], b["max_completion_tokens"]),
                         (MODEL, False, 0.2, "low", False, 3000))
        self.assertEqual(b["tools"], TOOLS); self.assertEqual(b["tool_choice"], "auto")
        self.assertEqual(b["messages"][1]["content"], [{"type": "text", "text": "hi"}], "messages are relayed unchanged")

    def test_upstream_headers_are_a_fixed_allowlist_with_the_injected_key(self):
        self.chat(self.opencode_body(), headers=OPENCODE_HEADERS)
        h = Upstream.received[0]["headers"]
        self.assertEqual(h["authorization"], "Bearer " + PROVIDER_KEY, "the provider key replaces the job token")
        self.assertNotIn(self.token, json.dumps(h))
        self.assertEqual(set(h), {"authorization", "host", "content-type", "content-length", "accept", "accept-encoding", "user-agent", "x-title"})
        self.assertEqual(h["user-agent"], "prqa-model-proxy")

    def test_completion_budget_only_shrinks(self):
        for asked, expected in (({"max_tokens": 32000}, 3000), ({"max_tokens": 500}, 500), ({"max_completion_tokens": 700, "max_tokens": 900}, 700),
                                ({}, 3000), ({"max_tokens": -5}, 3000), ({"max_tokens": True}, 3000), ({"max_tokens": "10"}, 3000)):
            Upstream.received = []
            body = self.opencode_body(); body.pop("max_tokens")
            st, raw, _ = self.chat({**body, **asked})
            self.assertEqual(st, 200, raw)
            self.assertEqual(Upstream.received[0]["body"]["max_completion_tokens"], expected, asked)

    def test_malformed_requests_never_reach_upstream(self):
        for bad in ({"messages": "hi"}, {"messages": [{"content": "no role"}]}, {"messages": [{"role": "root", "content": "x"}]},
                    {"tools": [{"type": "code_interpreter"}]}, {"tools": "bash"}):
            st, raw, _ = self.chat(self.opencode_body(**bad))
            self.assertEqual(st, 400, (bad, raw)); self.assertEqual(json.loads(raw)["error"]["type"], "proxy_policy")
        self.assertEqual(Upstream.received, [])

    def test_job_token_is_required(self):
        for token in (False, "prqa_mt_" + "x" * 43, ADMIN):
            st, raw, _ = self.chat(self.opencode_body(), token=token)
            self.assertIn(st, (401, 403), raw)
        self.assertEqual(Upstream.received, [])

    # --------------------------------------------------------------------------------------------- client --
    def test_stream_client_receives_a_synthesized_sse_text_completion(self):
        st, raw, hdrs = self.chat(self.opencode_body())
        self.assertEqual(st, 200); self.assertEqual(hdrs["content-type"], "text/event-stream")
        self.assertEqual(int(hdrs["content-length"]), len(raw))
        events = parse_sse(raw)
        self.assertEqual(events[-1], "[DONE]")
        chunks = [json.loads(e) for e in events[:-1]]
        self.assertTrue(all(c["object"] == "chat.completion.chunk" for c in chunks))
        text = "".join(ch["delta"].get("content") or "" for c in chunks for ch in c["choices"])
        self.assertEqual(text, "SYNTHETIC ANSWER")
        self.assertEqual([ch["finish_reason"] for c in chunks for ch in c["choices"] if ch["finish_reason"]], ["stop"])
        self.assertEqual(chunks[-1]["choices"], []); self.assertEqual(chunks[-1]["usage"], {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})
        self.assertNotIn(HIDDEN_REASONING.encode(), raw); self.assertNotIn(b"x_groq", raw); self.assertNotIn(b"reasoning", raw)

    def test_stream_client_receives_tool_calls_as_deltas(self):
        calls = [{"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{\"filePath\":\"/work/repo/README.md\"}"}},
                 {"id": "call_2", "type": "function", "function": {"name": "glob", "arguments": "{\"pattern\":\"*.py\"}"}}]
        Upstream.reply = completion(content=None, tool_calls=calls, finish="tool_calls")
        st, raw, _ = self.chat(self.opencode_body())
        self.assertEqual(st, 200, raw)
        chunks = [json.loads(e) for e in parse_sse(raw)[:-1]]
        deltas = [d for c in chunks for ch in c["choices"] for d in ch["delta"].get("tool_calls") or []]
        self.assertEqual([(d["index"], d["id"], d["type"], d["function"]["name"], json.loads(d["function"]["arguments"])) for d in deltas],
                         [(0, "call_1", "function", "read", {"filePath": "/work/repo/README.md"}), (1, "call_2", "function", "glob", {"pattern": "*.py"})])
        self.assertIn("tool_calls", [ch["finish_reason"] for c in chunks for ch in c["choices"]])

    def test_tool_result_round_trip_is_forwarded(self):
        msgs = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call_1", "content": "file body"}]
        st, raw, _ = self.chat(self.opencode_body(messages=msgs))
        self.assertEqual(st, 200, raw); self.assertEqual(Upstream.received[0]["body"]["messages"], msgs)

    def test_non_stream_client_receives_sanitized_json(self):
        st, raw, hdrs = self.chat(self.opencode_body(stream=False))
        self.assertEqual(st, 200); self.assertEqual(hdrs["content-type"], "application/json")
        doc = json.loads(raw)
        self.assertEqual(doc["choices"][0]["message"], {"role": "assistant", "content": "SYNTHETIC ANSWER"})
        self.assertEqual(set(doc), {"id", "object", "created", "model", "choices", "usage"})
        self.assertNotIn(HIDDEN_REASONING.encode(), raw)

    def test_malformed_upstream_completions_are_refused(self):
        for reply in ({"choices": []}, {"choices": [{"message": "x"}]}, {"choices": [{"message": {"content": 5}}]},
                      completion(tool_calls=[{"id": "c", "function": {"name": "read", "arguments": {"not": "a string"}}}]),
                      completion(tool_calls=[{"id": "c", "type": "function", "function": {"name": "", "arguments": "{}"}}]),
                      completion(tool_calls="read"), b"not json"):
            Upstream.reply = reply
            st, raw, _ = self.chat(self.opencode_body())
            self.assertEqual(st, 502, (reply, raw)); self.assertEqual(json.loads(raw)["error"]["type"], "proxy_upstream")
            self.assertNotIn(HIDDEN_REASONING.encode(), raw)

    # --------------------------------------------------------------------------------------------- config --
    def test_from_env_defaults_are_the_locked_groq_contract(self):
        tmp = Path(self.tmp) / "key"; tmp.write_text(PROVIDER_KEY + "\n")
        base = {"PRQA_MODEL_PROVIDER_KEY_FILE": str(tmp), "PRQA_MODEL_ALLOWED_MODEL": MODEL, "PRQA_MODEL_PROXY_ADMIN_TOKEN": ADMIN}
        with self.assertRaises(SystemExit, msg="the default (real Groq) upstream needs the provider budget"):
            mp.ProxyConfig.from_env(base).validate()
        budget_dir = Path(self.tmp) / "budget"; budget_dir.mkdir(mode=0o700)
        cfg = mp.ProxyConfig.from_env({**base, "PRQA_PROVIDER_BUDGET_DIR": str(budget_dir), "PRQA_BUDGET_DAILY_REQUESTS": "5",
                                       "PRQA_BUDGET_DAILY_TOKENS": "40000", "PRQA_BUDGET_JOB_TOKENS": "24000",
                                       "PRQA_BUDGET_TOKENS_PER_MINUTE": "8000", "PRQA_BUDGET_MAX_IN_FLIGHT": "1"})
        cfg.validate()
        self.assertEqual((cfg.budget.daily_requests, cfg.budget.tokens_per_minute, cfg.bytes_per_token), (5, 8000, 4.5))
        self.assertEqual((cfg.upstream_origin, cfg.upstream_prefix, cfg.temperature, cfg.reasoning_effort, cfg.max_completion_tokens),
                         ("https://api.groq.com", "/openai/v1", 0.2, "low", 3000))
        self.assertEqual(cfg.provider_key, PROVIDER_KEY)

    def test_contract_settings_are_validated(self):
        budget_dir = Path(self.tmp) / "budget-validate"; budget_dir.mkdir(mode=0o700, exist_ok=True)
        budget = mp.pb.BudgetConfig(directory=str(budget_dir), daily_requests=5, daily_tokens=40000, job_tokens=24000, tokens_per_minute=8000, max_in_flight=1)
        base = dict(upstream_origin="https://api.groq.com", allowed_model=MODEL, provider_key="k", admin_token=ADMIN, budget=budget)
        for bad in ({"temperature": 3.0}, {"temperature": -0.1}, {"reasoning_effort": "max"}, {"max_completion_tokens": 0},
                    {"max_completion_tokens": 100000}):
            with self.assertRaises(SystemExit, msg=bad):
                mp.ProxyConfig(**base, **bad).validate()


if __name__ == "__main__":
    unittest.main()
