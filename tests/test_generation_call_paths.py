"""Every code path that can reach a model provider is either under the ONE shared provider-budget ledger or disabled.

Static inventory (offline, no network): every deployable file that names a provider endpoint or the chat-completions
route is listed here with its role, and each role's guarantee is checked against the source:

  budgeted   proxy/model_proxy.py (all runtime inference: OpenCode sandboxes and n8n prompt generation) and
             scripts/eval/evaluate_groq_live.py (operator tool) reserve proxy/provider_budget.py's ledger before connecting
  via-proxy  n8n workflow 03 sends prompt generation to the model proxy with the prompt credential only
  retired    scripts/eval/evaluate_openrouter_live.py refuses in its only network function
  inert      configuration, generators, checkers and mocks: provider names as data, no outbound model request

A new file that names a provider fails test_inventory_is_complete until it is classified here.

    PYTHONPATH=worker python3 -m unittest tests.test_generation_call_paths -v
"""
from __future__ import annotations

import ast
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "worker"), str(ROOT / "proxy")]

PROVIDER_RE = re.compile(r"api\.groq\.com|openrouter\.ai|api\.openai\.com|chat/completions")
DEPLOYABLE = ("proxy", "worker", "scripts", "n8n", "mocks", "compose", "launcher", "config", "prompts", "schemas")
INVENTORY = {
    "proxy/model_proxy.py": "budgeted",
    "scripts/eval/evaluate_groq_live.py": "budgeted",
    "n8n/workflows/03-prqa-llm-prompt-builder.json": "via-proxy",
    "n8n/trigger/09-prqa-label-pipeline.json": "via-proxy",     # the active label-triggered pipeline (Groq prompt step)
    "scripts/eval/evaluate_openrouter_live.py": "retired",
    "worker/prqa_worker/policy.py": "inert",
    "worker/prqa_worker/prompt_builder.py": "inert",
    "scripts/build/build_workflows.py": "inert",
    "scripts/build/build_n8n_pipeline_workflow.py": "inert",
    "scripts/eval/validate_staged_prompt_live.py": "via-proxy",
    "scripts/build/validate_workflows.py": "inert",
    "mocks/mock_services.py": "inert",
    "compose/docker-compose.yml": "inert",
    "compose/.env.example": "inert",
    "compose/.env": "inert",                     # local, non-secret settings (not required to exist)
}
NETWORK_RE = re.compile(r"\burlopen\b|http\.client|HTTPSConnection|HTTPConnection|requests\.|socket\.create_connection")


def function_source(path: Path, qualname: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    parts = qualname.split(".")
    nodes = tree.body
    found = None
    for part in parts:
        found = next(n for n in nodes if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == part)
        nodes = found.body
    return ast.get_source_segment(src, found)


class InventoryTests(unittest.TestCase):
    def test_inventory_is_complete(self):
        hits = set()
        for top in DEPLOYABLE:
            for f in (ROOT / top).rglob("*"):
                if f.is_file() and "__pycache__" not in f.parts and f.suffix not in (".pyc",):
                    try:
                        text = f.read_text(encoding="utf-8")
                    except (UnicodeDecodeError, OSError):
                        continue
                    if PROVIDER_RE.search(text):
                        hits.add(f.relative_to(ROOT).as_posix())
        self.assertEqual(hits - set(INVENTORY), set(), "unclassified file names a model provider")

    def test_inert_files_open_no_connection(self):
        for rel, role in INVENTORY.items():
            path = ROOT / rel
            if role != "inert" or path.suffix != ".py" or not path.exists():
                continue
            with self.subTest(file=rel):
                src = path.read_text(encoding="utf-8")
                if rel == "mocks/mock_services.py":                      # serves, never calls out
                    self.assertNotRegex(src, r"\burlopen\b|HTTPSConnection|requests\.")
                else:
                    self.assertNotRegex(src, NETWORK_RE)


class BudgetedPathTests(unittest.TestCase):
    def test_proxy_reserves_the_ledger_before_any_upstream_connection(self):
        fwd = function_source(ROOT / "proxy/model_proxy.py", "Handler._forward")
        self.assertLess(fwd.index("self._reserve_budget("), fwd.index("self.connection_factory("))
        self.assertIn("if rid is False:", fwd)                       # a denial returns before the connection
        src = (ROOT / "proxy/model_proxy.py").read_text(encoding="utf-8")
        # the upstream connection is opened in exactly one place: _forward
        self.assertEqual(src.count("self.connection_factory("), 1)
        validate = function_source(ROOT / "proxy/model_proxy.py", "ProxyConfig.validate")
        self.assertIn('u.scheme == "https" and self.budget is None', validate)

    def test_every_inference_principal_goes_through_forward(self):
        route = function_source(ROOT / "proxy/model_proxy.py", "Handler._inference_route")
        self.assertEqual(route.count("return self._forward("), 2)    # chat completions (job + prompt) and the off-by-default model list
        self.assertIn("self._prompt_job() if prompt else self.tokens.lookup(bearer)", route)

    def test_evaluator_reserves_before_its_single_send(self):
        main = function_source(ROOT / "scripts/eval/evaluate_groq_live.py", "main")
        self.assertEqual(main.count("http_json("), 1)
        self.assertLess(main.index("ledger.reserve("), main.index("http_json("))
        self.assertLess(main.index("open_budget(env, secrets)"), main.index("ledger.reserve("))

    def test_both_budgeted_paths_use_the_same_ledger_module_and_directory(self):
        proxy = (ROOT / "proxy/model_proxy.py").read_text(encoding="utf-8")
        evaluator = (ROOT / "scripts/eval/evaluate_groq_live.py").read_text(encoding="utf-8")
        self.assertIn("import provider_budget as pb", proxy)
        self.assertRegex(evaluator, r"import provider_budget as pb")
        compose = (ROOT / "compose/docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("source: ${PRQA_PROVIDER_BUDGET_DIR:-/nonexistent/prqa-provider-budget-not-provisioned}", compose)
        self.assertIn("PRQA_PROVIDER_BUDGET_DIR", function_source(ROOT / "scripts/eval/evaluate_groq_live.py", "open_budget"))


class ViaProxyPathTests(unittest.TestCase):
    def test_no_workflow_holds_a_provider_key_or_addresses_a_provider(self):
        for f in sorted((ROOT / "n8n/workflows").glob("*.json")):
            text = f.read_text(encoding="utf-8")
            with self.subTest(workflow=f.name):
                self.assertNotRegex(text, r"\$env\.(GROQ_API_KEY|PROMPT_LLM_API_KEY|OPENROUTER_API_KEY)")
                wf = json.loads(text)
                self.assertIs(wf["active"], False)
                for node in wf["nodes"]:
                    if node["type"] == "n8n-nodes-base.httpRequest":
                        self.assertNotRegex(json.dumps(node["parameters"]), r"api\.groq\.com|openrouter\.ai|api\.openai\.com")

    def test_the_only_generation_node_uses_the_prompt_credential_once_without_retry(self):
        gens = [(f.name, n) for f in sorted((ROOT / "n8n/workflows").glob("*.json"))
                for n in json.loads(f.read_text(encoding="utf-8"))["nodes"]
                if str(n.get("parameters", {}).get("url", "")).rstrip("/").endswith("/chat/completions")]
        self.assertEqual([name for name, _ in gens], ["03-prqa-llm-prompt-builder.json"])
        node = gens[0][1]
        auth = [h["value"] for h in node["parameters"]["headerParameters"]["parameters"] if h["name"] == "Authorization"]
        self.assertEqual(len(auth), 1)
        self.assertIn("$env.PRQA_MODEL_PROXY_PROMPT_TOKEN", auth[0])
        self.assertFalse(node.get("retryOnFail"))
        self.assertEqual(node["parameters"]["url"], "={{ $json.prompt_llm_api_base }}/chat/completions")

    def test_the_worker_hands_n8n_only_the_proxy_url(self):
        from prqa_worker.policy import Policy
        live = Policy.from_env({"AUTOMATION_MODE": "review_only", "PROMPT_LLM_LIVE_IN_REVIEW_ONLY": "true"})
        self.assertEqual(live.endpoints()["prompt_llm_api_base"], "http://prqa-model-proxy:8089/v1")
        self.assertEqual(live.prompt_llm_api_base, "https://api.groq.com/openai/v1", "the provider identity is unchanged")
        review = Policy.from_env({})
        self.assertEqual(review.endpoints()["prompt_llm_api_mode"], "mock")

    def test_no_service_but_the_proxy_can_hold_the_provider_key(self):
        compose = (ROOT / "compose/docker-compose.yml").read_text(encoding="utf-8")
        code = "\n".join(ln.split("#")[0] for ln in compose.splitlines())
        self.assertNotRegex(code, r"\b(GROQ_API_KEY|PROMPT_LLM_API_KEY|OPENROUTER_API_KEY)\b")
        self.assertEqual(code.count("prqa_model_provider_key"), 3)     # proxy env path, proxy secrets entry, top-level secret
        self.assertEqual(code.count("prqa_opencode_provider_key"), 3)  # the same three for the OpenCode proxy only
        self.assertEqual(code.count("PRQA_MODEL_UPSTREAM_ORIGIN"), 2)  # one fixed upstream per proxy service


class OpenCodePathTests(unittest.TestCase):
    def test_the_sandbox_model_is_the_proxy_only(self):
        entry = (ROOT / "worker/sandbox/entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn('[[ "${PRQA_MODEL_BASE_URL:-}" =~ ^http://', entry)
        self.assertIn('"apiKey": "{env:PRQA_MODEL_TOKEN}"', entry)
        self.assertNotRegex(entry, r"api\.groq\.com|openrouter\.ai")


if __name__ == "__main__":
    unittest.main()
