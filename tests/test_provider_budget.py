"""proxy/provider_budget.py and its enforcement in the model proxy — offline (loopback fake upstream, temp ledger).

    PYTHONPATH=worker python3 -m unittest tests.test_provider_budget -v
"""
from __future__ import annotations

import http.client
import io
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))

import model_proxy as mp  # noqa: E402
import provider_budget as pb  # noqa: E402

ADMIN = "admin-token-SYNTHETIC-budget-0123456789"
MODEL = "openai/gpt-oss-120b"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def private_dir(parent=None) -> str:
    d = tempfile.mkdtemp(prefix="prqa-budget-", dir=parent); os.chmod(d, 0o700); return d


def cfg(d, **kw):
    base = dict(directory=d, daily_requests=5, daily_tokens=40000, job_tokens=24000, tokens_per_minute=8000, max_in_flight=1)
    base.update(kw)
    return pb.BudgetConfig(**base)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.dir = private_dir(); self.clock = Clock()
        self.ledger = pb.Ledger(cfg(self.dir), clock=self.clock)

    def tearDown(self):
        import shutil; shutil.rmtree(self.dir, ignore_errors=True)

    def spend(self, tokens, job="job-a", actual=None, advance=61):
        rid = self.ledger.reserve(source="test", job_id=job, tokens=tokens, completion_allowance=512)
        self.ledger.settle(rid, total_tokens=actual, outcome="ok")
        self.clock.t += advance
        return rid

    def test_daily_request_cap_counts_failures_and_is_never_refunded(self):
        for _ in range(5):
            self.spend(1000, job=f"j{_}", actual=None)
        with self.assertRaises(pb.BudgetDenied) as ctx:
            self.ledger.reserve(source="test", job_id="j9", tokens=10, completion_allowance=1)
        self.assertIn("daily request budget", ctx.exception.reason); self.assertIsNone(ctx.exception.retry_after_s)
        self.clock.t += 86400                                           # a new UTC day
        self.ledger.reserve(source="test", job_id="j10", tokens=10, completion_allowance=1)

    def test_daily_and_per_job_token_caps_use_reported_usage_after_settle(self):
        self.spend(7200, job="a", actual=6000)                          # reservation replaced by the reported total
        snap = self.ledger.snapshot(); self.assertEqual((snap["requests_today"], snap["tokens_today"], snap["jobs"]["a"]), (1, 6000, 6000))
        self.spend(7200, job="a", actual=7000); self.spend(7200, job="a", actual=7000)
        with self.assertRaises(pb.BudgetDenied) as ctx:                 # 20000 + 7200 > 24000 per job
            self.ledger.reserve(source="test", job_id="a", tokens=7200, completion_allowance=512)
        self.assertIn("per-job", ctx.exception.reason)
        self.spend(7200, job="b", actual=7900)                          # day: 27900
        with self.assertRaises(pb.BudgetDenied) as ctx:                 # 27900 + 9000 fits the day, but 9000 alone never fits 8000/min
            self.ledger.reserve(source="test", job_id="c", tokens=9000, completion_allowance=3000)
        self.assertEqual(ctx.exception.kind, "request_too_large"); self.assertIsNone(ctx.exception.retry_after_s)
        ledger2 = pb.Ledger(cfg(self.dir, tokens_per_minute=20000), clock=self.clock)
        with self.assertRaises(pb.BudgetDenied) as ctx:
            ledger2.reserve(source="test", job_id="c", tokens=12500, completion_allowance=3000)
        self.assertIn("daily token budget", ctx.exception.reason)

    def test_rolling_minute_window_and_single_in_flight(self):
        rid = self.ledger.reserve(source="test", job_id="a", tokens=7000, completion_allowance=512)
        with self.assertRaises(pb.BudgetDenied) as ctx:
            self.ledger.reserve(source="test", job_id="b", tokens=500, completion_allowance=1)
        self.assertEqual(ctx.exception.retry_after_s, 2.0); self.assertIn("in flight", ctx.exception.reason)
        self.ledger.settle(rid, total_tokens=6900, outcome="ok")
        self.clock.t += 10
        with self.assertRaises(pb.BudgetDenied) as ctx:                 # 6900 + 7000 > 8000 within the minute
            self.ledger.reserve(source="test", job_id="a", tokens=7000, completion_allowance=512)
        self.assertAlmostEqual(ctx.exception.retry_after_s, 50.5, places=3)
        self.clock.t += 51
        self.ledger.reserve(source="test", job_id="a", tokens=7000, completion_allowance=512)

    def test_provider_rate_limit_headers_are_respected(self):
        rid = self.ledger.reserve(source="test", job_id="a", tokens=1000, completion_allowance=1)
        self.ledger.settle(rid, total_tokens=900, outcome="ok", remaining_tokens=300, reset_s=20.0)
        self.clock.t += 61                                              # own window is clear; the provider said 20 s, now past
        self.ledger.reserve(source="test", job_id="a", tokens=1000, completion_allowance=1)
        d2 = private_dir(); l2 = pb.Ledger(cfg(d2), clock=self.clock)
        rid = l2.reserve(source="test", job_id="a", tokens=100, completion_allowance=1)
        l2.settle(rid, total_tokens=100, outcome="ok", remaining_tokens=300, reset_s=90.0)
        self.clock.t += 61
        with self.assertRaises(pb.BudgetDenied) as ctx:
            l2.reserve(source="test", job_id="a", tokens=1000, completion_allowance=1)
        self.assertIn("provider-reported", ctx.exception.reason); self.assertGreater(ctx.exception.retry_after_s, 25)

    def test_fails_closed_on_bad_configuration_or_ledger(self):
        for env in ({}, {"PRQA_PROVIDER_BUDGET_DIR": self.dir}, {"PRQA_PROVIDER_BUDGET_DIR": self.dir, **{k: "0" for k in pb.REQUIRED_KEYS}},
                    {"PRQA_PROVIDER_BUDGET_DIR": "relative", **{k: "5" for k in pb.REQUIRED_KEYS}},
                    {"PRQA_PROVIDER_BUDGET_DIR": "/nonexistent-prqa", **{k: "5" for k in pb.REQUIRED_KEYS}}):
            with self.assertRaises(pb.BudgetError, msg=env):
                pb.BudgetConfig.from_env(env)
        open_dir = private_dir(); os.chmod(open_dir, 0o755)
        with self.assertRaises(pb.BudgetError):
            pb.Ledger(cfg(open_dir))
        Path(self.dir, pb.LEDGER_NAME).write_text("{not json")
        with self.assertRaises(pb.BudgetError):
            self.ledger.reserve(source="test", job_id="a", tokens=10, completion_allowance=1)
        Path(self.dir, pb.LEDGER_NAME).write_text(json.dumps({"version": 1, "days": [], "jobs": {}, "events": []}))
        with self.assertRaises(pb.BudgetError):
            self.ledger.reserve(source="test", job_id="a", tokens=10, completion_allowance=1)
        for bad in (0.5, 9, "4"):
            with self.assertRaises(pb.BudgetError):
                pb.estimate_tokens(100, bad)

    def test_ledger_never_stores_payloads_and_is_private(self):
        self.spend(100, job="job-x", actual=90)
        text = Path(self.dir, pb.LEDGER_NAME).read_text()
        self.assertEqual(oct(os.stat(Path(self.dir, pb.LEDGER_NAME)).st_mode & 0o777), "0o600")
        self.assertEqual(set(json.loads(text)), {"version", "days", "jobs", "events", "provider"})
        self.assertEqual(set(json.loads(text)["events"][0]), {"id", "ts", "source", "job_id", "tokens", "reserved", "completion_allowance",
                                                               "state", "actual", "outcome"})

    def test_observed_underestimate_is_covered_by_the_safety_margin(self):
        # live 2026-10-28, Amenly PR #2 scope stage: the proxy reserved 4187 (2387 prompt + 1800 completion), Groq reported 4715
        size, allowance, actual = 10740, 1800, 4715
        self.assertEqual(pb.estimate_tokens(size, 4.5) + allowance, 4187)               # the old reservation: too low
        reserved = pb.reservation_tokens(size, 4.5, allowance)
        self.assertEqual(reserved, 3104 + 1800)
        self.assertGreaterEqual(reserved, actual)
        self.assertGreaterEqual(pb.reservation_tokens(10740, 4.5, 2400), 2915 + 1946)  # canary 2026-10-28T13:02Z: 4861 actual
        for bad in (0.9, 3.5, "1.3"):
            with self.assertRaises(pb.BudgetError):
                pb.reservation_tokens(100, 4.5, 10, safety=bad)

    def test_single_request_is_capped_below_the_tpm_limit(self):
        self.assertEqual(pb.max_request_tokens(8000), 7200)
        with self.assertRaises(pb.BudgetDenied) as ctx:
            self.ledger.reserve(source="test", job_id="a", tokens=7201, completion_allowance=3000)
        self.assertEqual(ctx.exception.kind, "request_too_large"); self.assertIsNone(ctx.exception.retry_after_s)
        cap = self.ledger.capacity(job_id="a", tokens=7201)
        self.assertEqual((cap["fits"], cap["hard"], cap["kind"]), (False, True, "request_too_large"))
        self.ledger.reserve(source="test", job_id="a", tokens=7200, completion_allowance=3000)

    def test_settle_reconciles_an_overrun_and_the_window_uses_the_actual(self):
        rid = self.ledger.reserve(source="test", job_id="a", tokens=4187, completion_allowance=1800)
        self.ledger.settle(rid, total_tokens=4715, outcome="ok")
        ev = json.loads(Path(self.dir, pb.LEDGER_NAME).read_text())["events"][-1]
        self.assertEqual((ev["reserved"], ev["actual"], ev["overrun"]), (4187, 4715, 528))
        self.clock.t += 5
        cap = self.ledger.capacity(job_id="a", tokens=3500)                             # 4715 + 3500 > 8000 within the minute
        self.assertEqual((cap["fits"], cap["kind"], cap["window_tokens"]), (False, "tpm_window", 4715))
        self.clock.t += 56
        self.assertTrue(self.ledger.capacity(job_id="a", tokens=3500)["fits"])
        rid = self.ledger.reserve(source="test", job_id="b", tokens=3000, completion_allowance=1000)
        self.ledger.settle(rid, total_tokens=None, outcome="transport_error")           # no usage: the reservation stays counted
        ev = json.loads(Path(self.dir, pb.LEDGER_NAME).read_text())["events"][-1]
        self.assertEqual((ev["tokens"], "overrun" in ev), (3000, False))

    def test_reset_header_parsing(self):
        self.assertEqual(pb.parse_reset_seconds("7.66s"), 7.66); self.assertEqual(pb.parse_reset_seconds("2m59.5s"), 179.5)
        self.assertEqual(pb.parse_reset_seconds("450ms"), 0.45); self.assertIsNone(pb.parse_reset_seconds("soon")); self.assertIsNone(pb.parse_reset_seconds(None))


class Upstream(BaseHTTPRequestHandler):
    received: list = []
    mode = "ok"

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        Upstream.received.append(body)
        if Upstream.mode == "error":
            self.send_response(500); self.send_header("Content-Length", "0"); self.end_headers(); return
        data = json.dumps({"id": "c1", "object": "chat.completion", "created": 1, "model": MODEL,
                           "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "reasoning": "HIDDEN",
                                                                "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "read", "arguments": "{\"filePath\":\"/x\"}"}}]},
                                        "finish_reason": "length" if Upstream.mode == "length" else "tool_calls"}],
                           "usage": {"prompt_tokens": 600, "completion_tokens": 40, "total_tokens": 640}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
        self.send_header("x-ratelimit-remaining-tokens", "7000"); self.send_header("x-ratelimit-reset-tokens", "5.5s"); self.end_headers()
        self.wfile.write(data)


class ProxyBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream); cls.upstream.daemon_threads = True
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.tmp = private_dir()
        cls.logbuf = io.StringIO(); h = logging.StreamHandler(cls.logbuf); mp.log.addHandler(h); mp.log.setLevel(logging.INFO)

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown(); cls.upstream.server_close()
        import shutil; shutil.rmtree(cls.tmp, ignore_errors=True)

    def start(self, **budget_kw):
        Upstream.received = []
        self.bdir = private_dir(self.tmp)
        c = mp.ProxyConfig(upstream_origin=f"http://127.0.0.1:{self.upstream.server_port}", upstream_prefix="/openai/v1", allowed_model=MODEL,
                           provider_key="gsk_SYNTHETIC_budget_key_000000000000", admin_token=ADMIN, budget=cfg(self.bdir, **budget_kw),
                           budget_max_wait_s=0)
        sock = os.path.join(self.tmp, f"admin-{len(os.listdir(self.tmp))}.sock")
        self.inf, self.adm = mp.make_servers(c, "127.0.0.1", 0, sock)
        self.sock = sock
        for s in (self.inf, self.adm):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.addCleanup(lambda: [s.shutdown() or s.server_close() for s in (self.inf, self.adm)])

    def admin(self, spec):
        conn = http.client.HTTPConnection("localhost", timeout=10)
        conn.sock = socket.socket(socket.AF_UNIX); conn.sock.connect(self.sock)
        conn.request("POST", "/admin/tokens", body=json.dumps(spec).encode(), headers={"Authorization": "Bearer " + ADMIN, "Content-Type": "application/json"})
        r = conn.getresponse(); d = json.loads(r.read()); conn.close(); return r.status, d

    def chat(self, tok, **extra):
        body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": "SECRET-PROMPT-TEXT"}], "stream": False, **extra}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.inf.server_port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=body, headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json",
                                                                          "Content-Length": str(len(body))})
        r = conn.getresponse(); d = r.read(); conn.close(); return r.status, json.loads(d)

    def test_requests_are_reserved_settled_and_capped_before_the_upstream(self):
        self.start(daily_requests=2)
        st, issued = self.admin({"job_id": "job-budget", "model": MODEL, "max_completion_tokens": 512})
        self.assertEqual((st, issued["max_completion_tokens"]), (201, 512))
        st, doc = self.chat(issued["token"], max_tokens=4000)
        self.assertEqual(st, 200); self.assertEqual(Upstream.received[0]["max_completion_tokens"], 512, "per-token completion cap applied")
        snap = pb.Ledger(cfg(self.bdir, daily_requests=2)).snapshot()
        self.assertEqual((snap["requests_today"], snap["tokens_today"], snap["jobs"]["job-budget"], snap["in_flight"]), (1, 640, 640, 0))
        led = json.loads(Path(self.bdir, pb.LEDGER_NAME).read_text())
        self.assertEqual(led["provider"]["remaining_tokens"], 7000, "provider rate-limit headers recorded")
        # the window still holds 640 tokens and the provider reported 7000 remaining: a second request with its full
        # reservation still fits 8000/min; the daily request cap (2) then stops the third before the upstream
        st, _ = self.chat(issued["token"]); self.assertEqual(st, 200)
        st, doc = self.chat(issued["token"])
        self.assertEqual(st, 429); self.assertIn("daily request budget", doc["error"]["message"]); self.assertEqual(len(Upstream.received), 2)
        logs = self.logbuf.getvalue()
        self.assertIn("finish=tool_calls tool_calls=read", logs); self.assertIn("roles=user", logs)
        for secret in ("SECRET-PROMPT-TEXT", "HIDDEN", "filePath", issued["token"], "gsk_SYNTHETIC"):
            self.assertNotIn(secret, logs)

    def test_rate_window_without_wait_is_a_429_and_nothing_is_sent(self):
        self.start(tokens_per_minute=4000)
        st, issued = self.admin({"job_id": "job-tpm", "model": MODEL, "max_completion_tokens": 3000})
        self.assertEqual(self.chat(issued["token"])[0], 200)            # ~60 + 3000 reserved, settled at 640
        st, doc = self.chat(issued["token"])                              # 640 + ~3060 fits 4000? yes; then a third does not
        st3, doc3 = self.chat(issued["token"])
        self.assertEqual(st3, 429); self.assertIn("tokens-per-minute", doc3["error"]["message"])
        self.assertEqual(len(Upstream.received), 2)

    def test_halt_on_error_revokes_the_token_before_any_retry(self):
        self.start(tokens_per_minute=100000)                            # failed requests keep their full reservation
        st, issued = self.admin({"job_id": "job-halt", "model": MODEL, "max_requests": 3, "halt_on_error": True})
        self.assertEqual((st, issued["halt_on_error"]), (201, True))
        Upstream.mode = "error"
        try:
            self.assertEqual(self.chat(issued["token"])[0], 500)
        finally:
            Upstream.mode = "ok"
        st, doc = self.chat(issued["token"])                              # a client retry: refused locally
        self.assertEqual(st, 401); self.assertEqual(len(Upstream.received), 1)
        st, issued = self.admin({"job_id": "job-trunc", "model": MODEL, "halt_on_error": True})
        Upstream.mode = "length"
        try:
            self.assertEqual(self.chat(issued["token"])[0], 200)          # the truncated reply is delivered once ...
        finally:
            Upstream.mode = "ok"
        self.assertEqual(self.chat(issued["token"])[0], 401)              # ... and nothing follows it
        self.assertEqual(len(Upstream.received), 2)
        st, issued = self.admin({"job_id": "job-nohalt", "model": MODEL})  # default: unchanged behaviour
        Upstream.mode = "error"
        try:
            self.chat(issued["token"])
        finally:
            Upstream.mode = "ok"
        self.assertEqual(self.chat(issued["token"])[0], 200)
        self.assertEqual(self.admin({"job_id": "job-bad", "model": MODEL, "halt_on_error": "yes"})[0], 422)

    def test_completion_cap_cannot_exceed_the_proxy_wide_cap(self):
        self.start()
        for bad in (0, 3001, "512", True):
            self.assertEqual(self.admin({"job_id": "job-bad", "model": MODEL, "max_completion_tokens": bad})[0], 422, bad)

    def test_broken_ledger_refuses_the_request(self):
        self.start()
        st, issued = self.admin({"job_id": "job-broken", "model": MODEL})
        Path(self.bdir, pb.LEDGER_NAME).write_text("garbage")
        st, doc = self.chat(issued["token"])
        self.assertEqual(st, 503); self.assertEqual(Upstream.received, [])


if __name__ == "__main__":
    unittest.main()
