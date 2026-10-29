"""Discord reporting tests — SYNTHETIC, no network. Every HTTP call is a fake opener.

Covers: report generation for passed / failed / blocked / inconclusive outcomes, long-report splitting within Discord's
limits, mention + Markdown sanitization, HTTP failure / timeout / rate-limit / retry behaviour, duplicate-notification
prevention, and the invariant that a Discord failure never changes the validated result or re-runs the job.

Run: cd "<project>" && PYTHONPATH=worker python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from prqa_worker import discord  # noqa: E402
from prqa_worker.discord import (DiscordSender, LIMITS, build_blocked_message, build_result_messages,  # noqa: E402
                                 deliver_messages, enforce_limits, sanitize_untrusted)
from prqa_worker.report import inconclusive_placeholder, render_all  # noqa: E402
from prqa_worker.server import App  # noqa: E402
from prqa_worker.validation import SchemaStore, validate_result  # noqa: E402

FIX = ROOT / "fixtures" / "synthetic"
CANARY = "ghp_SYNTHETICCANARY0000000000000000000000000"
WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/SYNTHETIC-TOKEN-abcdefghijklmnopqrstuvwxyz0123456789"


def fixture_result() -> dict:
    return json.loads((FIX / "opencode-result.synthetic.json").read_text(encoding="utf-8"))


def result_with_status(status: str) -> dict:
    r = fixture_result()
    if status in ("PASS", "PASS_WITH_KNOWN_ISSUES"):
        for c in r["test_cases"]:
            c["status"] = "PASS"; c["defect_ref"] = None
        r["defects"] = []
        r["blockers"] = []
        for row in r["coverage_matrix"]:
            row["passed"], row["failed"], row["blocked"] = row["planned"], 0, 0
        r["summary"].update({"passed": len(r["test_cases"]), "failed": 0, "blocked": 0, "overall_status": status})
        r["recommendation"]["verdict"] = status
    elif status == "INCONCLUSIVE":
        r["summary"]["overall_status"] = "INCONCLUSIVE"
        r["recommendation"]["verdict"] = "INCONCLUSIVE"
    return r


class _Resp:
    def __init__(self, status=204, body=b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeDiscord:
    """Scripted responses: each entry is an int status, an Exception instance, or ('429', retry_after)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append({"url": req.full_url, "data": json.loads(req.data.decode("utf-8")), "timeout": timeout})
        step = self.script.pop(0) if self.script else 204
        if isinstance(step, Exception):
            raise step
        if isinstance(step, tuple):
            code, retry_after = step
            hdrs = urllib.request.urllib_response_headers if False else __import__("email").message_from_string("Retry-After: %s\n" % retry_after)
            raise urllib.error.HTTPError(req.full_url, int(code), "Too Many Requests", hdrs, io.BytesIO(json.dumps({"retry_after": retry_after}).encode()))
        if int(step) >= 400:
            raise urllib.error.HTTPError(req.full_url, int(step), "error", __import__("email").message_from_string(""), io.BytesIO(b"{}"))
        return _Resp(int(step))


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.schemas = SchemaStore(ROOT / "schemas")

    def test_messages_for_each_outcome(self):
        for status, word, color in (("PASS", "passed", 3066993), ("PASS_WITH_KNOWN_ISSUES", "passed with known issues", 16776960),
                                    ("FAIL", "failed", 15158332), ("INCONCLUSIVE", "inconclusive", 9807270)):
            r = result_with_status(status)
            self.assertTrue(validate_result(r, self.schemas)["valid"], status)
            msgs = build_result_messages(r, pipeline={"execution_id": "exec-7"}, prompt={"source": "llm", "model": "mock-llm"},
                                         run_url="https://github.com/synthetic-org/sample-orders-api/actions/runs/1", mode="mock")
            first = msgs[0]
            self.assertIn(word, first["content"])
            self.assertEqual(first["allowed_mentions"], {"parse": []})
            embed = first["embeds"][0]
            self.assertEqual(embed["color"], color)
            self.assertIn("synthetic-org/sample-orders-api #101", embed["title"])
            self.assertEqual(embed["url"], "https://github.com/synthetic-org/sample-orders-api/pull/101")
            fields = {f["name"]: f["value"] for f in embed["fields"]}
            self.assertIn(r["pr"]["head_sha"], fields["Tested commit"])
            self.assertIn("passed", fields["Result"]); self.assertIn("failed", fields["Result"]); self.assertIn("skipped", fields["Result"])
            self.assertEqual(fields["Verdict"].split("**")[1], word.upper())
            self.assertIn("exec-7", fields["Links"]); self.assertIn("actions/runs/1", fields["Links"]); self.assertIn(r["job_id"], fields["Links"])
            self.assertIn("live model", fields["Prompt"])
            if status == "FAIL":
                self.assertIn("Bugs found (1)", fields)
                self.assertIn("DEF-001", fields["Bugs found (1)"])
                self.assertIn("P2", fields["Bugs found (1)"])
                self.assertIn("Blockers (2)", fields)
                self.assertEqual(len(msgs), 2, "defect details travel in a follow-up message")
                detail = msgs[1]["embeds"][0]
                dfields = {f["name"]: f["value"] for f in detail["fields"]}
                self.assertIn("P2 · introduced_by_pr", dfields["Severity · origin"])   # enum values are trusted
                self.assertTrue(dfields["Reproduction"].startswith("```"))
                self.assertIn("$TOKEN", dfields["Reproduction"])
                self.assertIn("ORD-CNL-002", dfields["Evidence (test cases)"])
                self.assertIn("(1/2)", msgs[0]["content"]); self.assertIn("(2/2)", msgs[1]["content"])
            else:
                self.assertEqual(len(msgs), 2 if r["defects"] else 1)

    def test_blocked_message(self):
        seed = {"repository": "your-org/your-first-real-repo", "synthetic": False, "test_level": "standard",
                "pr": {"number": 7, "title": "Real PR @here", "head_sha": "a" * 40, "url": "https://github.com/your-org/your-first-real-repo/pull/7"},
                "trigger": {"source": "github_actions", "requested_label": "to be test", "run_url": "https://github.com/your-org/your-first-real-repo/actions/runs/5"}}
        decision = {"route": "blocked", "allowed": False, "reason_code": "REVIEW_ONLY_MODE", "reason": "AUTOMATION_MODE=review_only — blocked"}
        m = build_blocked_message(seed, decision, pipeline={"execution_id": "exec-9"}, mode="mock")
        self.assertIn("blocked", m["content"]); self.assertIn("REVIEW_ONLY_MODE", m["content"])
        self.assertEqual(m["allowed_mentions"], {"parse": []})
        e = m["embeds"][0]
        self.assertEqual(e["color"], 10038562)
        self.assertNotIn("@here", e["description"]); self.assertIn("@\u200bhere", e["description"])
        fields = {f["name"]: f["value"] for f in e["fields"]}
        self.assertIn("BLOCKED", fields["Status"]); self.assertIn("no test was executed", fields["Status"])
        self.assertIn("a" * 40, fields["Requested commit"])
        self.assertIn("to be test", fields["Requested via"])
        self.assertIn("actions/runs/5", fields["Links"]); self.assertIn("exec-9", fields["Links"])

    def test_fallback_prompt_is_clearly_indicated(self):
        r = result_with_status("PASS")
        r["executed_by"]["prompt_source"] = "reference_fallback"
        fields = {f["name"]: f["value"] for f in build_result_messages(r)[0]["embeds"][0]["fields"]}
        self.assertIn("reference fallback", fields["Prompt"]); self.assertIn("NOT a live model", fields["Prompt"])
        fields = {f["name"]: f["value"] for f in build_result_messages(r, prompt={"source": "reference_fallback", "model": None})[0]["embeds"][0]["fields"]}
        self.assertIn("NOT a live model", fields["Prompt"])
        fields = {f["name"]: f["value"] for f in build_result_messages(fixture_result())[0]["embeds"][0]["fields"]}
        self.assertIn("fixture", fields["Prompt"])
        fields = {f["name"]: f["value"] for f in build_result_messages(r, prompt={"source": "llm", "model": "mock-llm", "api_mode": "mock"})[0]["embeds"][0]["fields"]}
        self.assertIn("MOCK LLM endpoint", fields["Prompt"]); self.assertIn("not a live model", fields["Prompt"])

    def test_validation_warnings_and_inconclusive_placeholder(self):
        job = {"id": "20261022-abc", "repository": "synthetic-org/sample-orders-api", "head_sha": "b" * 40, "synthetic": True,
               "request": {"pr": {"number": 101, "title": "T", "head_sha": "b" * 40, "url": "https://github.com/synthetic-org/sample-orders-api/pull/101"},
                           "prompt": {"source": "reference_fallback"}}}
        placeholder = inconclusive_placeholder(job, ["summary.passed=99 but test_cases imply 5"])
        self.assertTrue(validate_result(placeholder, self.schemas)["valid"])
        self.assertEqual(placeholder["summary"]["overall_status"], "INCONCLUSIVE")
        msgs = build_result_messages(placeholder, validation={"valid": False, "errors": ["summary.passed=99 but test_cases imply 5"]})
        fields = {f["name"]: f["value"] for f in msgs[0]["embeds"][0]["fields"]}
        self.assertIn("Validation warnings", fields)
        self.assertIn("summary.passed=99", fields["Validation warnings"])
        self.assertIn("inconclusive", msgs[0]["content"])
        self.assertIn("NOT a live model", fields["Prompt"])

    def test_sanitization_of_mentions_and_markdown(self):
        s = sanitize_untrusted("@everyone @here <@123> <@!456> <@&789> <#42> **bold** `code` [x](https://evil.example) ~~s~~ _u_ |sp| > q # h\x00\x07")
        self.assertNotIn("@everyone", s); self.assertIn("@\u200beveryone", s); self.assertIn("@\u200bhere", s)
        for tag in ("<@123>", "<@!456>", "<@&789>", "<#42>"):
            self.assertNotIn(tag, s)
        for raw, escaped in (("**bold**", "\\*\\*bold\\*\\*"), ("`code`", "\\`code\\`"), ("~~s~~", "\\~\\~s\\~\\~"),
                             ("|sp|", "\\|sp\\|"), ("[x](", "\\[x\\]\\("), (" > q", "\\> q"), (" # h", "\\# h")):
            self.assertNotIn(raw, s); self.assertIn(escaped, s)
        self.assertNotIn("\x00", s); self.assertNotIn("\x07", s)
        self.assertEqual(sanitize_untrusted("x" * 50, 10), "x" * 9 + "…")
        self.assertNotIn(CANARY, sanitize_untrusted(f"leak {CANARY}"))
        # everything untrusted in a result goes through it
        r = fixture_result()
        r["pr"]["title"] = "@everyone ```injected``` <@&1>"
        r["defects"][0]["title"] = "@here *drop* tables"
        r["defects"][0]["repro_curl"] = "curl ``` -H 'Authorization: Bearer abcdefgh12345678' @everyone"
        r["blockers"] = [f"@everyone {CANARY}"]
        r["summary"]["executive_summary"] = "<@123> see https://discord.com/api/webhooks/1/abc"
        blob = json.dumps(build_result_messages(r), ensure_ascii=False)
        for bad in ("@everyone", "@here", "<@&1>", "<@123>", CANARY, "api/webhooks/1/abc", "Bearer abcdefgh"):
            self.assertNotIn(bad, blob, bad)
        self.assertNotIn('"parse": ["everyone"]', blob)
        for m in build_result_messages(r):
            self.assertEqual(m["allowed_mentions"], {"parse": []})

    def test_long_report_is_split_within_limits(self):
        r = fixture_result()
        base = r["defects"][0]
        r["defects"] = []
        r["test_cases"] = []
        for i in range(60):
            d = json.loads(json.dumps(base)); d["id"] = f"DEF-{i + 1:03d}"; d["title"] = f"Defect number {i + 1} " + "x" * 150
            d["expected"] = "e" * 1000; d["actual"] = "a" * 1000; d["impact"] = "i" * 1000; d["repro_curl"] = "curl " + "r" * 1900
            d["test_case_ids"] = [f"ORD-CNL-{i + 1:03d}"]
            r["defects"].append(d)
            r["test_cases"].append({"id": f"ORD-CNL-{i + 1:03d}", "scenario": "s", "endpoint": "POST /x", "expected": "e", "actual": "a",
                                    "status": "FAIL", "defect_ref": d["id"]})
        r["coverage_matrix"] = [{"area": "A", "planned": 60, "passed": 0, "failed": 60, "blocked": 0, "not_applicable": 0}]
        r["summary"].update({"total": 60, "passed": 0, "failed": 60, "blocked": 0, "not_applicable": 0, "executive_summary": "y" * 4000})
        r["blockers"] = ["b" * 500] * 100
        self.assertTrue(validate_result(r, self.schemas)["valid"])
        msgs = build_result_messages(r)
        self.assertGreater(len(msgs), 1)
        self.assertLessEqual(len(msgs), discord.MAX_MESSAGES)
        for m in msgs:
            self.assertLessEqual(len(m["content"]), LIMITS["content"])
            self.assertLessEqual(len(m["embeds"]), LIMITS["embeds"])
            self.assertLessEqual(discord._embed_chars(m["embeds"]), LIMITS["total"])
            for e in m["embeds"]:
                self.assertLessEqual(len(e.get("title", "")), LIMITS["title"])
                self.assertLessEqual(len(e.get("description", "")), LIMITS["description"])
                self.assertLessEqual(len(e.get("fields", [])), LIMITS["fields"])
                for f in e.get("fields", []):
                    self.assertLessEqual(len(f["name"]), LIMITS["field_name"]); self.assertLessEqual(len(f["value"]), LIMITS["field_value"])
                    self.assertTrue(f["value"].strip(), "no empty field values")
        rendered = [e["title"] for m in msgs[1:] for e in m["embeds"]]
        self.assertTrue(any("more defects" in t for t in rendered), "remainder is summarised, not dropped silently")
        self.assertIn("more (see follow-up messages)", json.dumps(msgs[0]))

    def test_enforce_limits_hard_caps(self):
        payload = {"content": "c" * 5000, "embeds": [{"title": "t" * 500, "description": "d" * 7000,
                   "fields": [{"name": "n" * 300, "value": "v" * 2000}] * 40, "footer": {"text": "f" * 3000}}] * 12,
                   "allowed_mentions": {"parse": ["everyone", "roles"]}}
        out = enforce_limits(payload)
        self.assertEqual(len(out["content"]), LIMITS["content"])
        self.assertLessEqual(len(out["embeds"]), LIMITS["embeds"])
        self.assertLessEqual(discord._embed_chars(out["embeds"]), LIMITS["total"])
        self.assertEqual(out["allowed_mentions"], {"parse": []})

    def test_legacy_render_all_uses_sanitized_builder(self):
        r = fixture_result(); r["pr"]["title"] = "@everyone"
        out = render_all(r, discord_mode="mock", github_mode="mock")
        self.assertNotIn("@everyone", json.dumps(out["discord_payload"]))
        self.assertEqual(out["discord_payload"]["allowed_mentions"], {"parse": []})


class SenderTests(unittest.TestCase):
    def sender(self, script, **kw):
        fake = FakeDiscord(script)
        self.sleeps = []
        return DiscordSender(WEBHOOK, opener=fake, sleep=self.sleeps.append, timeout_s=3, max_attempts=3, backoff_s=1, **kw), fake

    def test_success_first_try(self):
        s, fake = self.sender([204])
        out = s.send({"content": "hi"})
        self.assertTrue(out["ok"]); self.assertEqual(out["status_code"], 204); self.assertEqual(len(out["attempts"]), 1)
        self.assertEqual(fake.calls[0]["timeout"], 3)
        self.assertEqual(fake.calls[0]["data"]["allowed_mentions"], {"parse": []})

    def test_timeout_then_success(self):
        s, fake = self.sender([TimeoutError("timed out"), urllib.error.URLError(TimeoutError("t")), 204])
        out = s.send({"content": "hi"})
        self.assertTrue(out["ok"]); self.assertEqual(len(out["attempts"]), 3); self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_rate_limit_honours_retry_after_and_is_bounded(self):
        s, fake = self.sender([("429", 7), 204])
        out = s.send({"content": "hi"})
        self.assertTrue(out["ok"]); self.assertEqual(self.sleeps, [7.0])
        s, fake = self.sender([("429", 999), ("429", 999), ("429", 999), 204])
        out = s.send({"content": "hi"})
        self.assertFalse(out["ok"]); self.assertEqual(out["status_code"], 429); self.assertEqual(len(fake.calls), 3)
        self.assertEqual(self.sleeps, [30.0, 30.0], "Retry-After is capped and attempts are bounded")

    def test_server_errors_retry_client_errors_do_not(self):
        s, fake = self.sender([502, 503, 204])
        self.assertTrue(s.send({"content": "x"})["ok"]); self.assertEqual(len(fake.calls), 3)
        s, fake = self.sender([400, 204])
        out = s.send({"content": "x"})
        self.assertFalse(out["ok"]); self.assertEqual(out["status_code"], 400); self.assertEqual(len(fake.calls), 1)
        s, fake = self.sender([404, 204])
        self.assertFalse(s.send({"content": "x"})["ok"]); self.assertEqual(len(fake.calls), 1)

    def test_connection_failure_exhausts_attempts(self):
        s, fake = self.sender([urllib.error.URLError(ConnectionRefusedError())] * 5)
        out = s.send({"content": "x"})
        self.assertFalse(out["ok"]); self.assertIsNone(out["status_code"]); self.assertEqual(len(out["attempts"]), 3)
        self.assertIn("URLError", out["error"])
        self.assertNotIn("discord.com/api/webhooks", json.dumps(out))

    def test_deliver_messages_stops_after_failure_and_reports_partial(self):
        s, fake = self.sender([204, 400, 204])
        out = deliver_messages([{"content": "1"}, {"content": "2"}, {"content": "3"}], s, spacing_s=0.5)
        self.assertEqual((out["delivered"], out["status"], out["sent"], out["messages"]), (False, "partial", 1, 3))
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(self.sleeps, [0.5])
        s, fake = self.sender([204, 204])
        out = deliver_messages([{"content": "1"}, {"content": "2"}], s, spacing_s=0)
        self.assertEqual((out["delivered"], out["status"]), (True, "delivered"))


class WorkerDeliveryTests(unittest.TestCase):
    """POST /notifications/discord through App.route, with the sender replaced by a scripted fake."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {
            "AUTOMATION_MODE": "review_only", "REAL_DISCORD_REPORTING_ENABLED": "true",   # forced off → mock
            "MOCK_SERVICES_URL": "http://prqa-mocks:8090", "PRQA_WORKER_TOKEN": "test-token",
            "PRQA_REGISTRY_PATH": str(ROOT / "config/repositories.yaml"), "PRQA_SCHEMA_DIR": str(ROOT / "schemas"),
            "PRQA_PROMPTS_DIR": str(ROOT / "prompts"), "PRQA_FIXTURES_DIR": str(ROOT / "fixtures"), "PRQA_DATA_DIR": self.tmp.name,
            "MOCK_EXECUTION_DELAY_S": "0", "DISCORD_WEBHOOK_URL": WEBHOOK,
        }
        self.app = App(self.env)
        self.fake = FakeDiscord([])
        self.sent_to = []
        fake, sent_to = self.fake, self.sent_to
        self.app.make_sender = lambda url: (sent_to.append(url), DiscordSender(url, opener=fake, sleep=lambda s: None, max_attempts=3, backoff_s=0))[1]

    def tearDown(self):
        self.app.executor.stop()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        return self.app.route(method, path, {}, body, {"x-worker-token": "test-token"})

    def finished_job(self) -> str:
        req = {"synthetic": True, "repository": "synthetic-org/sample-orders-api",
               "pr": {"number": 101, "head_sha": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678", "base_sha": "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432",
                      "title": "T", "url": "https://github.com/synthetic-org/sample-orders-api/pull/101"},
               "test_level": "standard", "trigger": {"source": "github_actions", "delivery_id": "gha:k1", "label": "qa-test:standard",
                                                      "run_url": "https://github.com/synthetic-org/sample-orders-api/actions/runs/3"},
               "prompt": {"text": "x" * 300, "source": "reference_fallback", "sha256": "a" * 64}}
        status, created = self.call("POST", "/jobs", req)
        self.assertEqual(status, 201, created)
        job_id = created["job"]["id"]
        running = self.app.store.next_queued()
        self.app.store.succeed(job_id, self.app.executor.execute(running))
        return job_id

    def test_mock_sink_is_used_in_review_only_even_with_url_and_flag(self):
        job_id = self.finished_job()
        status, body = self.call("POST", "/notifications/discord", {"kind": "result", "job_id": job_id, "pipeline": {"execution_id": "e1"}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["mode"], "mock"); self.assertEqual(body["status"], "mock_delivered"); self.assertTrue(body["delivered"])
        self.assertEqual(self.sent_to, ["http://prqa-mocks:8090/discord/api/webhooks/000000000000000000/mock-token"])
        self.assertNotIn(WEBHOOK, json.dumps(self.sent_to))
        self.assertEqual(body["overall_status"], "FAIL"); self.assertEqual(body["messages"], 2); self.assertEqual(body["sent"], 2)
        first = self.fake.calls[0]["data"]
        self.assertIn("failed", first["content"])
        fields = {f["name"]: f["value"] for f in first["embeds"][0]["fields"]}
        self.assertIn("NOT a live model", fields["Prompt"])            # prompt source came from the job request
        self.assertIn("actions/runs/3", fields["Links"])                  # run url came from the job trigger
        job = self.call("GET", f"/jobs/{job_id}")[1]["job"]
        self.assertEqual(job["notifications"]["discord"]["status"], "mock_delivered")
        self.assertEqual(job["notifications"]["discord"]["detail"]["overall_status"], "FAIL")

    def test_real_mode_requires_live_policy_and_url(self):
        self.app.executor.stop()
        live = dict(self.env, AUTOMATION_MODE="live", REAL_PROJECT_TESTING_ENABLED="false", AUTO_EXECUTION_ENABLED="false", PRQA_DATA_DIR=self.tmp.name + "/live")
        app = App(live)
        try:
            self.assertTrue(app.discord_real)
            self.assertEqual(app.config()["discord"]["mode"], "real")
            self.assertEqual(app.config()["discord"]["webhook_url_configured"], True)
            self.assertNotIn(WEBHOOK, json.dumps(app.config()))
        finally:
            app.executor.stop()
        app = App(dict(live, DISCORD_WEBHOOK_URL="", PRQA_DATA_DIR=self.tmp.name + "/live2"))
        try:
            self.assertFalse(app.discord_real, "flag without URL → mock")
        finally:
            app.executor.stop()
        app = App(dict(live, REAL_DISCORD_REPORTING_ENABLED="false", PRQA_DATA_DIR=self.tmp.name + "/live3"))
        try:
            self.assertFalse(app.discord_real, "URL without flag → mock")
        finally:
            app.executor.stop()

    def test_duplicate_notification_prevented(self):
        job_id = self.finished_job()
        self.assertEqual(self.call("POST", "/notifications/discord", {"kind": "result", "job_id": job_id})[1]["status"], "mock_delivered")
        status, body = self.call("POST", "/notifications/discord", {"kind": "result", "job_id": job_id})
        self.assertEqual((status, body["status"], body["reason"]), (200, "skipped_duplicate", "ALREADY_DELIVERED"))
        self.assertEqual(len(self.fake.calls), 2, "no second delivery")
        # the claim endpoint exposes the same guard
        status, body = self.call("POST", f"/jobs/{job_id}/notification/claim", {"channel": "discord"})
        self.assertEqual((status, body["claimed"]), (409, False))
        status, body = self.call("POST", f"/jobs/{job_id}/notification/claim", {"channel": "github_comment"})
        self.assertEqual((status, body["claimed"]), (200, True))
        status, body = self.call("POST", f"/jobs/{job_id}/notification/claim", {"channel": "github_comment"})
        self.assertEqual((status, body["reason"]), (409, "DELIVERY_IN_FLIGHT"))
        self.assertEqual(self.call("POST", "/jobs/nope/notification/claim", {"channel": "discord"})[0], 404)

    def test_failed_delivery_can_be_retried_but_result_and_job_are_untouched(self):
        job_id = self.finished_job()
        before = self.call("GET", f"/jobs/{job_id}")[1]["job"]
        self.fake.script = [500, 500, 500, 500, 500, 500]
        status, body = self.call("POST", "/notifications/discord", {"kind": "result", "job_id": job_id})
        self.assertEqual(status, 200, "a Discord failure is reported, not raised, so the pipeline records it instead of failing the stage")
        self.assertEqual((body["status"], body["delivered"], body["sent"]), ("mock_failed", False, 0))
        self.assertEqual(len(self.fake.calls), 3)
        after = self.call("GET", f"/jobs/{job_id}")[1]["job"]
        self.assertEqual(after["status"], "succeeded")
        self.assertEqual(after["result"], before["result"])                      # validated result untouched
        self.assertEqual(after["attempts"], before["attempts"])                  # job not re-run
        self.assertEqual(self.app.store.stats().get("queued"), None)
        self.assertEqual(after["notifications"]["discord"]["status"], "mock_failed")
        self.assertNotIn(WEBHOOK, json.dumps(after))
        # a later retry is allowed (failed is not delivered) and succeeds
        self.fake.script = [204, 204]
        status, body = self.call("POST", "/notifications/discord", {"kind": "result", "job_id": job_id})
        self.assertEqual(body["status"], "mock_delivered")

    def test_invalid_or_mismatched_result_is_reported_inconclusive_never_pass(self):
        job_id = self.finished_job()
        job = self.app.store.get(job_id)
        forged = json.loads(json.dumps(job["result"]))
        for c in forged["test_cases"]:
            c["status"] = "PASS"; c["defect_ref"] = None
        forged["summary"]["overall_status"] = "PASS"; forged["recommendation"]["verdict"] = "PASS"   # counts now disagree → invalid
        status, body = self.call("POST", "/notifications/discord", {"kind": "result", "job_id": job_id, "result": forged})
        self.assertEqual(body["overall_status"], "INCONCLUSIVE"); self.assertFalse(body["validation_valid"])
        self.assertIn("inconclusive", self.fake.calls[0]["data"]["content"])
        self.assertNotIn("passed for", self.fake.calls[0]["data"]["content"])
        # a valid result that belongs to another job/commit is not trusted either
        job2 = self.finished_job_other_sha()
        other = json.loads(json.dumps(self.app.store.get(job2)["result"]))
        self.fake.calls.clear()
        self.app.store.set_notification(job_id, "discord", "mock_failed", {})
        status, body = self.call("POST", "/notifications/discord", {"kind": "result", "job_id": job_id, "result": other})
        self.assertEqual(body["overall_status"], "INCONCLUSIVE")
        self.assertIn("does not match the job", json.dumps(self.fake.calls[0]["data"]))
        self.assertEqual(self.app.store.get(job_id)["result"]["summary"]["overall_status"], "FAIL", "stored result unchanged")

    def finished_job_other_sha(self) -> str:
        req = {"synthetic": True, "repository": "synthetic-org/sample-orders-api",
               "pr": {"number": 101, "head_sha": "c" * 40, "title": "T"}, "test_level": "standard",
               "trigger": {"source": "synthetic_test", "delivery_id": "gha:k2", "label": "qa-test:standard"},
               "prompt": {"text": "x" * 300, "source": "fixture", "sha256": "a" * 64}}
        status, created = self.call("POST", "/jobs", req)
        job_id = created["job"]["id"]
        running = self.app.store.next_queued()
        self.app.store.succeed(job_id, self.app.executor.execute(running))
        return job_id

    def test_blocked_notice_deduped_by_delivery_id(self):
        seed = {"repository": "your-org/your-first-real-repo", "synthetic": False, "test_level": "standard",
                "pr": {"number": 7, "title": "x", "head_sha": "a" * 40}, "trigger": {"source": "github_actions", "delivery_id": "gha:blocked-1"}}
        decision = {"reason_code": "REVIEW_ONLY_MODE", "reason": "review only"}
        status, body = self.call("POST", "/notifications/discord", {"kind": "blocked", "job_seed": seed, "decision": decision, "pipeline": {"execution_id": "e5"}})
        self.assertEqual((status, body["status"], body["mode"]), (200, "mock_delivered", "mock"))
        self.assertIn("blocked", self.fake.calls[0]["data"]["content"])
        status, body = self.call("POST", "/notifications/discord", {"kind": "blocked", "job_seed": seed, "decision": decision})
        self.assertEqual(body["status"], "skipped_duplicate")
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.call("POST", "/notifications/discord", {"kind": "blocked", "job_seed": {}, "decision": {}})[0], 422)
        self.assertEqual(self.call("POST", "/notifications/discord", {"kind": "weird"})[0], 422)
        self.assertEqual(self.call("POST", "/notifications/discord", {"kind": "result", "job_id": "missing"})[0], 404)

    def test_no_webhook_url_in_events_or_logs(self):
        job_id = self.finished_job()
        self.fake.script = [urllib.error.URLError("boom " + WEBHOOK)] * 3
        with self.assertLogs("prqa", level="INFO") as cm:
            self.call("POST", "/notifications/discord", {"kind": "result", "job_id": job_id})
        blob = "\n".join(cm.output) + json.dumps(self.call("GET", "/events?limit=50")[1]) + json.dumps(self.call("GET", f"/jobs/{job_id}")[1])
        self.assertNotIn("SYNTHETIC-TOKEN", blob)


if __name__ == "__main__":
    unittest.main()
