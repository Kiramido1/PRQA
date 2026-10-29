"""End-to-end synthetic MVP flow with Groq as the active prompt provider: offline, in-process, no network.

Stages (each pinned below, in order): 1 label trigger → 2 PR context → 3 prompt request (/prompts/build) → 4 Groq call
(workflow 03's own credential check, Authorization header and response extraction, run in node against scripted Groq
responses) → 5 validation → 6 quality gate / fallback (/prompts/finalize) → 7 isolated execution (mock executor) →
8 result collection → 9 report validation and rendering → 10 GitHub/Discord delivery preparation (mock sinks only).

Every provider failure (missing key, HTTP error, transport error, provider error in a 200 body, malformed JSON, missing
choices, empty content, truncation) and a gate rejection must take the deterministic fallback, with its own sanitized
failure category, and the pipeline must still complete. Real GitHub, Discord, OpenCode and Groq are never contacted:
outbound sockets to anything but loopback fail the test.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))

from prqa_worker.discord import DiscordSender  # noqa: E402
from prqa_worker.policy import Policy  # noqa: E402
from prqa_worker.server import App  # noqa: E402
from tests.test_discord_reporting import FakeDiscord  # noqa: E402

FIX = ROOT / "fixtures" / "synthetic"
GROQ = "https://api.groq.com/openai/v1"
TOKEN = "test-token"
SYNTHETIC_GROQ_KEY = "gsk_" + "e" * 52            # synthetic, Groq key shape; never a real key
PROXY = "http://prqa-model-proxy:8089/v1"          # where workflow 03 sends a real request (PRQA_PROMPT_LLM_PROXY_URL)
SYNTHETIC_PROMPT_TOKEN = "prompt-token-SYNTHETIC-e2e-" + "p" * 24   # the proxy prompt credential (synthetic)
GROQ_SHAPE = {"model": "openai/gpt-oss-120b", "temperature": 0.2, "max_completion_tokens": 3000, "stream": False,
              "reasoning_effort": "low", "include_reasoning": False}
NODE = shutil.which("node")


def _fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _guard_connect(original):
    def connect(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"outbound connection attempted to {host!r}")
        return original(sock, address)
    return connect


class Workflow03:
    """Runs workflow 03's own expressions and code (from the checked-in JSON) in node."""
    wf = json.loads((ROOT / "n8n/workflows/03-prqa-llm-prompt-builder.json").read_text(encoding="utf-8"))
    nodes = {n["name"]: n for n in wf["nodes"]}

    @classmethod
    def _expr(cls, value):
        return value.removeprefix("={{").removesuffix("}}")

    @classmethod
    def _node(cls, script):
        out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
        return json.loads(out)

    @classmethod
    def key_present(cls, built, env):
        node = cls.nodes["Provider Key Present? (or mock)"]
        left = node["parameters"]["conditions"]["conditions"][0]["leftValue"]
        return cls._node(f"const $json = {json.dumps(built)}; const $env = {json.dumps(env)};\n"
                         f"process.stdout.write(JSON.stringify({cls._expr(left)}));")

    @classmethod
    def authorization(cls, built, env):
        gen = next(n for n in cls.wf["nodes"] if str(n["parameters"].get("url", "")).endswith("/chat/completions"))
        hdr = next(h["value"] for h in gen["parameters"]["headerParameters"]["parameters"] if h["name"] == "Authorization")
        return cls._node(f"const $json = {json.dumps(built)}; const $env = {json.dumps(env)};\n"
                         f"process.stdout.write(JSON.stringify({cls._expr(hdr)}));")

    @classmethod
    def extract(cls, response):
        js = cls.nodes["Extract Generated Prompt"]["parameters"]["jsCode"]
        return cls._node("const $input = { first: () => ({ json: " + json.dumps(response) + " }) };\n"
                         "const out = (() => {" + js + "})();\nprocess.stdout.write(JSON.stringify(out[0].json));")

    @classmethod
    def error_branch(cls, error_json):
        """The 'LLM Unavailable' Set node's assignments, evaluated against an n8n error item."""
        node = cls.nodes["LLM Unavailable → Reference Fallback"]
        out = {}
        for a in node["parameters"]["assignments"]["assignments"]:
            v = a["value"]
            out[a["name"]] = (cls._node(f"const $json = {json.dumps(error_json)};\n"
                                        f"process.stdout.write(JSON.stringify({cls._expr(v)}));")
                              if isinstance(v, str) and v.startswith("={{") else v)
        return out

    @classmethod
    def missing_key_branch(cls):
        node = cls.nodes["Provider Key Missing → Reference Fallback"]
        return {a["name"]: a["value"] for a in node["parameters"]["assignments"]["assignments"]}

    @classmethod
    def finalize_body(cls, item, seed, context):
        """The body the 'Validate Prompt / Fallback' node sends: the same field mapping as its jsonBody."""
        body = cls.nodes["Validate Prompt / Fallback to Reference (worker)"]["parameters"]["jsonBody"]
        for field in ("generated_text", "finish_reason: $json.llm_finish_reason", "llm_error: $json.llm_error",
                      "http_status: $json.llm_http_status", "response_shape: $json.llm_response_shape"):
            assert field in body, field
        return {"generated_text": item.get("generated_text") or None, "finish_reason": item.get("llm_finish_reason") or None,
                "llm_error": item.get("llm_error") or None, "http_status": item.get("llm_http_status") or None,
                "response_shape": item.get("llm_response_shape") or None, "repository": seed["repository"],
                "pr_number": seed["pr"]["number"], "test_level": seed["test_level"], "synthetic": seed["synthetic"],
                "context": context}


def completion(content, finish="stop"):
    return {"id": "chatcmpl-synthetic", "object": "chat.completion", "model": "openai/gpt-oss-120b",
            "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 9000, "completion_tokens": 3000, "total_tokens": 12000}}


@unittest.skipUnless(NODE, "node is not installed (workflow 03 code runs in node)")
class SyntheticGroqPipelineTests(unittest.TestCase):
    ENV = {"AUTOMATION_MODE": "review_only", "PROMPT_LLM_LIVE_IN_REVIEW_ONLY": "false",
           "PROMPT_LLM_API_BASE": GROQ, "PROMPT_LLM_MODEL": "openai/gpt-oss-120b", "PROMPT_LLM_MAX_TOKENS": "3000",
           "PROMPT_LLM_REASONING_EFFORT": "low", "MOCK_SERVICES_URL": "http://prqa-mocks:8090", "PRQA_WORKER_TOKEN": TOKEN,
           "PRQA_REGISTRY_PATH": str(ROOT / "config/repositories.yaml"), "PRQA_SCHEMA_DIR": str(ROOT / "schemas"),
           "PRQA_PROMPTS_DIR": str(ROOT / "prompts"), "PRQA_FIXTURES_DIR": str(ROOT / "fixtures"),
           "MOCK_EXECUTION_DELAY_S": "0", "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/real-must-not-be-used"}

    def setUp(self):
        guard = mock.patch.object(socket.socket, "connect", _guard_connect(socket.socket.connect))
        guard.start()
        self.addCleanup(guard.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = App({**self.ENV, "PRQA_DATA_DIR": self.tmp.name})
        self.addCleanup(self.app.executor.stop)
        self.opener = FakeDiscord([])
        self.sent_to = []
        self.app.make_sender = lambda url: (self.sent_to.append(url),
                                            DiscordSender(url, opener=self.opener, sleep=lambda s: None,
                                                          max_attempts=1, backoff_s=0))[1]
        self.valid = _fixture("llm-response.synthetic.json")["choices"][0]["message"]["content"]
        self.deliveries = 0

    def call(self, method, path, body=None):
        return self.app.route(method, path, {}, body, {"x-worker-token": TOKEN})

    # ---- stages 1-3 -------------------------------------------------------------------------------------------
    def trigger_and_context(self):
        ev = _fixture("github-pr-labeled.synthetic.json")
        status, cfg = self.call("GET", "/config")
        self.assertEqual(status, 200)
        self.assertEqual(cfg["endpoints"]["prompt_llm_api_mode"], "mock")
        ev["label"]["name"] = cfg["trigger"]["label"]
        status, out = self.call("POST", "/webhook/evaluate", {"event": "pull_request", "payload": ev})
        self.assertEqual(status, 200)
        self.assertTrue(out["eligible"], out)                                         # stage 1: label trigger
        seed = out["job_seed"]
        self.assertTrue(seed["synthetic"])
        status, ctx = self.call("POST", "/context/build", {
            "pr": ev["pull_request"], "files": _fixture("pr-files.synthetic.json"),
            "diff": (FIX / "pr-diff.synthetic.diff").read_text(encoding="utf-8"), "openapi": None,
            "repository": seed["repository"], "synthetic": True})
        self.assertEqual(status, 200)                                                 # stage 2: PR context
        self.assertEqual(ctx["context"]["repository"], seed["repository"])
        return seed, ctx["context"]

    def build(self, seed, context):
        status, built = self.call("POST", "/prompts/build", {"repository": seed["repository"], "pr_number": seed["pr"]["number"],
                                                             "test_level": seed["test_level"], "synthetic": True,
                                                             "context": context})
        self.assertEqual(status, 200)                                                 # stage 3: Groq-shaped request
        req = built["llm_request"]
        self.assertEqual({k: v for k, v in req.items() if k not in ("messages", "model")},
                         {k: v for k, v in GROQ_SHAPE.items() if k != "model"})
        self.assertEqual(built["generation"]["provider"], "groq")
        self.assertEqual(built["prompt_llm_api_mode"], "mock")                       # review-only: never the real base
        self.assertTrue(built["prompt_llm_api_base"].startswith("http://prqa-mocks:8090/"))
        return built

    # ---- stages 6-10 ------------------------------------------------------------------------------------------
    def finish(self, seed, context, item, *, source, failure):
        status, fin = self.call("POST", "/prompts/finalize", Workflow03.finalize_body(item, seed, context))
        self.assertEqual(status, 200)                                                 # stages 5-6: validation + gate
        self.assertEqual(fin["prompt"]["source"], source, fin["validation"])
        self.assertEqual(fin["generation"]["failure"], failure)
        self.assertIn("SYNTHETIC FIXTURE", fin["prompt"]["text"])
        self.deliveries += 1
        job_req = {"synthetic": True, "repository": seed["repository"], "pr": seed["pr"], "test_level": seed["test_level"],
                   "trigger": {**seed["trigger"], "delivery_id": f"e2e-{self._testMethodName}-{self.deliveries}"},
                   "prompt": {k: fin["prompt"][k] for k in ("text", "source", "sha256", "template_version")}}
        status, created = self.call("POST", "/jobs", job_req)
        self.assertEqual(status, 201, created)                                        # stage 7: isolated execution
        job_id = created["job"]["id"]
        self.assertEqual(created["job"]["execution_mode"], "mock")
        running = self.app.store.next_queued()
        self.assertEqual(running["id"], job_id)
        self.app.store.succeed(job_id, self.app.executor.execute(running))
        status, got = self.call("GET", f"/jobs/{job_id}")
        self.assertEqual((status, got["job"]["status"]), (200, "succeeded"))         # stage 8: result collected
        result = got["job"]["result"]
        self.assertTrue(result["synthetic"])
        self.assertEqual(result["executed_by"]["engine"], "mock_executor")
        status, check = self.call("POST", "/results/validate", {"result": result})
        self.assertTrue(check["valid"], check)                                        # stage 9: report validated
        status, rendered = self.call("POST", "/reports/render", {"result": result})
        self.assertEqual(status, 200)
        self.assertIn("SYNTHETIC", rendered["report_markdown"])
        self.assertEqual((rendered["discord_mode"], rendered["github_comment_mode"]), ("mock", "mock"))
        self.assertTrue(rendered["github_comment_markdown"])                          # stage 10: GitHub comment prepared
        status, claim = self.call("POST", f"/jobs/{job_id}/notification/claim", {"channel": "github_comment"})
        self.assertEqual((status, claim["claimed"]), (200, True))
        status, sent = self.call("POST", "/notifications/discord", {"kind": "result", "job_id": job_id})
        self.assertEqual((status, sent["mode"], sent["status"]), (200, "mock", "mock_delivered"))
        self.assertTrue(all(u.startswith("http://prqa-mocks:8090/") for u in self.sent_to), self.sent_to)
        self.assertNotIn(self.ENV["DISCORD_WEBHOOK_URL"], json.dumps(self.sent_to))
        return fin, result

    # ---- the flows --------------------------------------------------------------------------------------------
    def test_groq_success_flows_through_all_ten_stages(self):
        seed, context = self.trigger_and_context()
        built = self.build(seed, context)
        live = {**self.ENV, "PROMPT_LLM_LIVE_IN_REVIEW_ONLY": "true"}                 # what a live run would target
        # n8n never targets Groq itself: a real request goes to the model proxy (shared budget, sole key holder)
        self.assertEqual(Policy.from_env(live).endpoints()["prompt_llm_api_base"], PROXY)
        real = {**built, "prompt_llm_api_mode": "real", "prompt_llm_api_base": PROXY}
        env = {"PRQA_MODEL_PROXY_PROMPT_TOKEN": SYNTHETIC_PROMPT_TOKEN, "GROQ_API_KEY": SYNTHETIC_GROQ_KEY}
        self.assertTrue(Workflow03.key_present(real, env))                            # stage 4: Groq call via the proxy
        self.assertEqual(Workflow03.authorization(real, env), "Bearer " + SYNTHETIC_PROMPT_TOKEN)
        item = Workflow03.extract(completion(self.valid))
        self.assertEqual((item["generated_text"], item["llm_error"], item["llm_response_shape"]), (self.valid, None, None))
        fin, _ = self.finish(seed, context, item, source="llm", failure=None)
        self.assertEqual(fin["generation"]["outcome"], "usable")
        self.assertNotIn(SYNTHETIC_GROQ_KEY, json.dumps(fin))

    def test_missing_groq_key_sends_nothing_and_falls_back(self):
        seed, context = self.trigger_and_context()
        built = self.build(seed, context)
        real = {**built, "prompt_llm_api_mode": "real", "prompt_llm_api_base": PROXY}
        # a provider key is never a substitute for the proxy's prompt credential: the request is not sent
        self.assertFalse(Workflow03.key_present(real, {"GROQ_API_KEY": SYNTHETIC_GROQ_KEY, "PROMPT_LLM_API_KEY": "sk-or-v1-" + "0" * 64}))
        self.assertFalse(Workflow03.key_present(real, {"PRQA_MODEL_PROXY_PROMPT_TOKEN": ""}))
        # a provider base (https) is never called, even with the credential: only the plain-http internal proxy is
        direct = {**built, "prompt_llm_api_mode": "real", "prompt_llm_api_base": GROQ}
        self.assertFalse(Workflow03.key_present(direct, {"PRQA_MODEL_PROXY_PROMPT_TOKEN": SYNTHETIC_PROMPT_TOKEN}))
        self.assertTrue(Workflow03.key_present(built, {}))                            # mock mode needs no key
        self.assertEqual(Workflow03.authorization(built, {}), "Bearer mock-key-review-only")
        fin, _ = self.finish(seed, context, Workflow03.missing_key_branch(), source="reference_fallback",
                             failure="missing_credential")
        self.assertEqual(fin["generation"]["outcome"], "provider_error")

    def test_every_provider_failure_takes_the_fallback_with_its_own_category(self):
        seed, context = self.trigger_and_context()
        self.build(seed, context)
        secret = "upstream detail sk-or-v1-" + "b" * 40
        error_items = (
            ("http_error", Workflow03.error_branch({"error": {"message": "Authorization failed - please check your credentials",
                                                              "httpCode": "401", "description": secret}})),
            ("transport_error", Workflow03.error_branch({"error": {"message": "timeout of 360000ms exceeded"}})),
            # the model proxy refusing on the shared provider budget (429) is an ordinary failure: fallback, no retry
            ("http_error", Workflow03.error_branch({"error": {"message": "prqa-model-proxy: provider budget: daily request cap reached",
                                                              "httpCode": "429"}})),
        )
        response_items = (
            ("provider_error_response", Workflow03.extract({"error": {"code": 503, "message": secret}})),
            ("malformed_json", Workflow03.extract({"data": "<html>502 Bad Gateway</html>"})),
            ("missing_choices", Workflow03.extract({"id": "gen-1", "object": "chat.completion"})),
            ("empty_content", Workflow03.extract(completion(""))),
            ("truncated", Workflow03.extract(completion(self.valid, finish="length"))),
        )
        for failure, item in error_items + response_items:
            with self.subTest(failure=failure):
                fin, _ = self.finish(seed, context, item, source="reference_fallback", failure=failure)
                self.assertNotIn(secret, json.dumps(fin))
                self.assertNotIn(secret, json.dumps(item) if failure != "http_error" else "")

    def test_gate_rejected_groq_output_falls_back(self):
        seed, context = self.trigger_and_context()
        self.build(seed, context)
        invented = self.valid.replace("## 6. ", "**ORD-900 (Required) — Purge all orders**\n- Requirement/Risk: R1.\n"
                                                "- Request: `curl -sS -X DELETE \"$BASE_URL/v1/admin/purge\" -o p.json`\n"
                                                "- Expected: HTTP 204.\n\n## 6. ", 1)
        self.assertNotEqual(invented, self.valid)
        item = Workflow03.extract(completion(invented))
        fin, _ = self.finish(seed, context, item, source="reference_fallback", failure="gate_rejected")
        self.assertEqual(fin["generation"]["outcome"], "gate_rejected")
        self.assertTrue(fin["validation"]["errors"])



class GroqSecretHandlingTests(unittest.TestCase):
    def test_groq_key_is_redacted_and_never_reaches_the_sandbox(self):
        from prqa_worker.opencode_runner import SECRET_ENV_NAMES
        from prqa_worker.redaction import redact_text, redact_value
        self.assertIn("GROQ_API_KEY", SECRET_ENV_NAMES)
        self.assertNotIn(SYNTHETIC_GROQ_KEY, redact_text("key=" + SYNTHETIC_GROQ_KEY))
        self.assertNotIn(SYNTHETIC_GROQ_KEY, json.dumps(redact_value({"groq_api_key": SYNTHETIC_GROQ_KEY})))

if __name__ == "__main__":
    unittest.main()
