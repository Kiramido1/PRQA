"""Sandbox network + model-proxy configuration: the launcher and the Docker policy proxy must agree, and every missing,
inconsistent or unauthorised setting fails closed (offline; fake docker CLI, in-process policy evaluation).

    PYTHONPATH=worker python3 -m unittest tests.test_sandbox_network_policy -v
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "proxy"), str(ROOT / "worker"), str(ROOT)]

import docker_policy as dp  # noqa: E402
import docker_policy_proxy as dpp  # noqa: E402
from prqa_worker.launcher_service import LauncherConfig, LauncherError, LauncherService, ModelProxyClient  # noqa: E402
from tests.test_docker_policy import IMAGE_ID as POLICY_IMAGE, sandbox_spec  # noqa: E402
from tests.test_launcher_service import InProcessWorker, LauncherBase  # noqa: E402

COMPOSE = ROOT / "compose" / "docker-compose.yml"
SANDBOX_NET = "prqa_prqa_sandbox"
ADMIN_URL = "unix:///run/prqa-model-admin/admin.sock"
INFERENCE_URL = "http://prqa-model-proxy:8089/v1"


def create(policy, network, endpoints=None):
    spec = sandbox_spec(**{"HostConfig.NetworkMode": network})
    if endpoints is not None:
        spec["NetworkingConfig"] = {"EndpointsConfig": {n: {} for n in endpoints}}
    return dp.evaluate(policy, "POST", "/v1.45/containers/create?name=prqa-sandbox-20261022-abc123def456", json.dumps(spec).encode())


class DockerPolicyNetworkTests(unittest.TestCase):
    def test_permitted_networks_are_none_plus_exactly_the_configured_one(self):
        self.assertEqual(dp.sandbox_networks(None), frozenset({"none"}))
        self.assertEqual(dp.sandbox_networks(""), frozenset({"none"}))
        self.assertEqual(dp.sandbox_networks("none"), frozenset({"none"}))
        self.assertEqual(dp.sandbox_networks(SANDBOX_NET), frozenset({"none", SANDBOX_NET}))

    def test_shared_namespaced_or_malformed_networks_are_refused_at_start(self):
        for bad in ("host", "HOST", "bridge", "default", "container:abc", "prqa sandbox", "../x", "-x", "a" * 80, "x"):
            with self.assertRaises(ValueError, msg=bad):
                dp.sandbox_networks(bad)
            self.assertEqual(dpp.main(["--listen", "/nonexistent/sock", "--image-id", POLICY_IMAGE, "--network=" + bad]), 2, bad)

    def test_mismatched_network_is_denied_at_container_create(self):
        policy = dp.Policy(allowed_image_ids=frozenset({POLICY_IMAGE}), allowed_networks=dp.sandbox_networks(SANDBOX_NET))
        self.assertTrue(create(policy, SANDBOX_NET).allowed)
        self.assertTrue(create(policy, "none").allowed)
        for other in ("prqa_sandbox", "prqa_prqa_internal", "prqa_prqa_egress", "bridge", "host", "container:prqa-n8n-1", "a" * 64):
            d = create(policy, other)
            self.assertFalse(d.allowed, other); self.assertIn("network", d.reason)
        d = create(policy, SANDBOX_NET, endpoints=[SANDBOX_NET, "prqa_prqa_internal"])
        self.assertFalse(d.allowed); self.assertIn("endpoint", d.reason)

    def test_missing_proxy_setting_denies_the_launchers_network(self):
        policy = dp.Policy(allowed_image_ids=frozenset({POLICY_IMAGE}), allowed_networks=dp.sandbox_networks(None))
        self.assertFalse(create(policy, SANDBOX_NET).allowed, "proxy without --network: only 'none' (fail closed)")

    def test_network_attach_and_foreign_inspect_are_denied(self):
        policy = dp.Policy(allowed_image_ids=frozenset({POLICY_IMAGE}), allowed_networks=dp.sandbox_networks(SANDBOX_NET))
        for method, path in (("POST", f"/v1.45/networks/{SANDBOX_NET}/connect"), ("POST", f"/v1.45/networks/{SANDBOX_NET}/disconnect"),
                             ("POST", "/v1.45/networks/prqa_prqa_internal/connect"), ("POST", "/v1.45/networks/create"),
                             ("DELETE", f"/v1.45/networks/{SANDBOX_NET}"), ("GET", "/v1.45/networks/prqa_prqa_internal")):
            self.assertFalse(dp.evaluate(policy, method, path, b"{}").allowed, (method, path))
        self.assertTrue(dp.evaluate(policy, "GET", f"/v1.45/networks/{SANDBOX_NET}", None).allowed)


class ComposeNetworkAgreementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import yaml
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("PyYAML not installed")
        cls.doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    def test_launcher_and_policy_proxy_read_the_same_single_variable(self):
        launcher = self.doc["services"]["prqa-launcher"]["environment"]["PRQA_SANDBOX_NETWORK"]
        cmd = self.doc["services"]["prqa-docker-proxy"]["command"]
        proxy = cmd[cmd.index("--network") + 1]
        self.assertEqual(launcher, proxy, "one source of truth for the sandbox network")
        self.assertEqual(launcher, "${PRQA_SANDBOX_NETWORK:-none}", "unset means no network (fail closed), never an inferred compose name")

    def test_sandbox_network_is_internal_inhibit_ipv4_and_labelled(self):
        net = self.doc["networks"]["prqa_sandbox"]
        self.assertTrue(net["internal"]); self.assertEqual(net["labels"]["prqa.net"], "sandbox")
        self.assertEqual(str(net["driver_opts"]["com.docker.network.bridge.inhibit_ipv4"]).lower(), "true")
        self.assertEqual(self.doc["services"]["prqa-model-proxy"]["networks"], ["prqa_sandbox", "prqa_prompt", "prqa_egress"])
        self.assertNotIn("prqa_prompt", self.doc["services"]["prqa-launcher"]["networks"])

    def test_model_proxy_settings_are_the_locked_groq_path(self):
        env = self.doc["services"]["prqa-model-proxy"]["environment"]
        self.assertEqual((env["PRQA_MODEL_UPSTREAM_ORIGIN"], env["PRQA_MODEL_UPSTREAM_PREFIX"], env["PRQA_MODEL_ALLOWED_MODEL"]),
                         ("https://api.groq.com", "/openai/v1", "openai/gpt-oss-120b"))
        self.assertEqual((env["PRQA_MODEL_TEMPERATURE"], env["PRQA_MODEL_REASONING_EFFORT"], env["PRQA_MODEL_MAX_COMPLETION_TOKENS"]), ("0.2", "low", "3000"))
        launcher = self.doc["services"]["prqa-launcher"]["environment"]
        self.assertIn("PRQA_MODEL_PROXY_INFERENCE_URL", launcher); self.assertNotIn("PRQA_MODEL_BASE_URL", launcher)
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(?i)openrouter\.ai", text), "no OpenRouter default or fallback")
        # the OpenCode test model (operator decision 2026-10-28) lives only in its own proxy; prompts stay on Groq
        oc = self.doc["services"]["prqa-model-proxy-opencode"]["environment"]
        self.assertEqual((oc["PRQA_MODEL_UPSTREAM_ORIGIN"], oc["PRQA_MODEL_UPSTREAM_PREFIX"], oc["PRQA_MODEL_ALLOWED_MODEL"], oc["PRQA_MODEL_REASONING_EFFORT"]),
                         ("${PRQA_OPENCODE_UPSTREAM_ORIGIN:-https://opencode.ai}", "${PRQA_OPENCODE_UPSTREAM_PREFIX:-/inference/openai/v1}",
                          "${PRQA_OPENCODE_ALLOWED_MODEL:-glm-5.3-flash-free}", "none"))
        for name, svc in self.doc["services"].items():
            if name != "prqa-model-proxy-opencode":
                self.assertNotRegex(json.dumps(svc.get("environment") or {}), r"(?i)glm-", name)


class LauncherNetworkAndModelConfigTests(LauncherBase):
    def proxy_cfg(self, **over):
        base = dict(worker_url="x", launcher_token=self.cfg.launcher_token, launcher_id="launcher-C", sandbox_image_id=IMAGE_ID_L,
                    sandbox_network="prqa_sandbox", workdir=self.tmp.name + "/lc", model_base_url=INFERENCE_URL,
                    model_proxy_admin_url=ADMIN_URL, model_proxy_mint_token="mint-token-SYNTHETIC-0123456789abcdefghij")
        base.update(over)
        return LauncherConfig(**base)

    def test_model_proxy_with_network_none_is_refused(self):
        with self.assertRaises(LauncherError) as ctx:
            LauncherService(self.proxy_cfg(sandbox_network="none"), runner=self.fake.run)
        self.assertIn("PRQA_SANDBOX_NETWORK", str(ctx.exception))
        LauncherService(self.proxy_cfg(), runner=self.fake.run)                         # consistent configuration starts

    def test_partial_or_malformed_model_proxy_configuration_is_refused(self):
        for over in ({"model_proxy_mint_token": ""}, {"model_base_url": ""}, {"model_proxy_admin_url": ""},
                     {"model_proxy_admin_url": "http://prqa-model-proxy:8090"}, {"model_base_url": "https://prqa-model-proxy:8089/v1"},
                     {"model_base_url": "http://user:pw@prqa-model-proxy:8089/v1"}, {"model_base_url": "http://prqa-model-proxy:8089/v1/"},
                     {"model_base_url": "http://prqa-model-proxy:8089/admin"}):
            with self.assertRaises(LauncherError, msg=over):
                LauncherService(self.proxy_cfg(**over), runner=self.fake.run)

    def test_inference_url_names_must_not_disagree(self):
        env = {"PRQA_LAUNCHER_TOKEN": "t", "PRQA_MODEL_PROXY_INFERENCE_URL": INFERENCE_URL}
        self.assertEqual(LauncherConfig.from_env(env).model_base_url, INFERENCE_URL)
        self.assertEqual(LauncherConfig.from_env({**env, "PRQA_MODEL_BASE_URL": INFERENCE_URL}).model_base_url, INFERENCE_URL)
        with self.assertRaises(LauncherError):
            LauncherConfig.from_env({**env, "PRQA_MODEL_BASE_URL": "http://elsewhere:8089/v1"})

    def test_network_without_inhibit_ipv4_or_label_or_internal_is_refused(self):
        self.svc.cfg.sandbox_network = "prqa_sandbox"
        self.assertEqual(self.svc.verify_network(), "prqa_sandbox")
        for change in ({"inhibit_ipv4": "<no value>"}, {"inhibit_ipv4": "false"}, {"label": ""}, {"internal": "false"}):
            saved = dict(self.fake.networks["prqa_sandbox"])
            self.fake.networks["prqa_sandbox"].update(change)
            with self.assertRaises(LauncherError, msg=change):
                self.svc.verify_network()
            self.fake.networks["prqa_sandbox"] = saved

    def test_non_proxy_model_with_the_proxy_is_refused_before_a_token_exists(self):
        job_id = self.pending_job()
        st, claim = self.worker.claim(job_id)
        self.assertEqual(st, 200)
        self.assertEqual(claim["model"], "prqa/openai/gpt-oss-120b", "worker default: Groq via the PRQA model proxy")
        for model in ("openrouter/z-ai/glm-5.3-flash:free", "openai/gpt-oss-120b", "prqa/"):
            out = self.svc._launch({**claim, "model": model})
            self.assertEqual(out.outcome, "failed", model)
            self.assertEqual(self.svc.model_proxy.issued, [], "no token is issued for a model the proxy does not serve")
            self.assertFalse(any(c[0][1] == "create" for c in self.fake.calls), "no container created")

    def test_proxy_model_without_the_proxy_is_refused(self):
        cfg = LauncherConfig(worker_url="x", launcher_token=self.cfg.launcher_token, launcher_id="launcher-D", sandbox_image_id=IMAGE_ID_L,
                             sandbox_network="prqa_sandbox", workdir=self.tmp.name + "/ld", heartbeat_s=0.05, poll_s=0.01)
        disabled = ModelProxyClient(cfg)
        svc = LauncherService(cfg, worker=InProcessWorker(cfg, self.app), runner=self.fake.run, model_proxy=disabled, sleep=lambda s: None)
        self.pending_job()
        outcomes = svc.run_once()
        self.assertEqual([o.outcome for o in outcomes], ["failed"])
        self.assertFalse(any(c[0][1] == "create" for c in self.fake.calls), "no container created")

    def test_missing_malformed_or_reused_launcher_token_fails_at_start(self):
        for bad in ("", "short", "x" * 31, "token with spaces " * 3, "a" * 257):
            with self.assertRaises(LauncherError, msg=repr(bad[:10])):
                LauncherService(self.proxy_cfg(launcher_token=bad), runner=self.fake.run)
        same = "mint-token-SYNTHETIC-0123456789abcdefghij"
        with self.assertRaises(LauncherError):
            LauncherService(self.proxy_cfg(launcher_token=same + "abcdef", model_proxy_mint_token=same + "abcdef"), runner=self.fake.run)

    def test_worker_disables_the_launcher_api_for_a_malformed_or_reused_token(self):
        from prqa_worker.server import App
        from tests.test_launch_lifecycle import LT, WT, live_env
        import tempfile
        for bad in ("short", WT):
            app = App(live_env(tempfile.mkdtemp(dir=self.tmp.name), PRQA_LAUNCHER_TOKEN=bad))
            try:
                st, _ = app.route("GET", "/launch/pending", {}, None, {"x-launcher-token": bad})
                self.assertEqual(st, 401, bad[:6])
                self.assertEqual(app.launcher_token, "", "the malformed/reused credential is not kept as a launcher credential")
            finally:
                app.executor.stop()
        app = App(live_env(tempfile.mkdtemp(dir=self.tmp.name)))
        try:
            self.assertEqual(app.route("GET", "/launch/pending", {}, None, {"x-launcher-token": LT})[0], 200)
        finally:
            app.executor.stop()


IMAGE_ID_L = "sha256:" + "b" * 64


if __name__ == "__main__":
    unittest.main()
