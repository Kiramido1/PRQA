"""Review-only policy: the single place that decides whether a job may run.

Fail-closed rules
-----------------
* AUTOMATION_MODE defaults to ``review_only``. Any value other than ``live``
  is treated as ``review_only``.
* While review-only, every REAL_* flag is forced to ``false`` even if the
  environment sets it to ``true``; the forced flags are reported in
  ``forced_off`` so the UI can show it.
* A non-synthetic ("real") job is allowed only when *all* of the following hold:
  mode == live, REAL_PROJECT_TESTING_ENABLED, AUTO_EXECUTION_ENABLED, the
  repository is registered with kind=real, enabled=true, approval_status=approved,
  the event/label are allowed, and fork PRs are eligible when the PR is a fork.
* A synthetic job is allowed only for a registered kind=synthetic repository whose
  name starts with ``synthetic-org/`` and is always executed by the mock executor.
* ``PROMPT_LLM_LIVE_IN_REVIEW_ONLY=true`` is the single, explicit exception: it routes the
  prompt-builder LLM call to the configured provider (Groq / any OpenAI-compatible endpoint)
  while everything else stays review-only. It is not one of the REAL_* flags, cannot enable
  any other integration, and ``Policy.self_check()`` refuses to start the worker if that
  invariant ever breaks.

Prompt-generation path (2026-10-27): no service except the model proxy holds a provider key
------------------------------------------------------------------------------------------
A real prompt-generation request goes from n8n (workflow 03) to the model proxy's inference listener
(PRQA_PROMPT_LLM_PROXY_URL, default http://prqa-model-proxy:8089/v1) with the inference-only prompt credential
PRQA_MODEL_PROXY_PROMPT_TOKEN. The proxy reserves the shared provider-budget ledger, applies the locked request
contract and injects GROQ_API_KEY, which only it holds. PROMPT_LLM_API_BASE still names the provider (it selects the
request adapter); endpoints() never hands it to n8n, and self_check() refuses a proxy URL equal to it.
The per-provider key names below (GROQ_API_KEY / legacy PROMPT_LLM_API_KEY, never across providers) apply only to the
operator's direct evaluator scripts, which reserve the same ledger.

Generation settings (non-secret, validated at start-up; see prompt_builder.resolve_generation_settings)
------------------------------------------------------------------------------------------------------
  PROMPT_LLM_MAX_TOKENS            completion limit, default 3000 (the Groq production budget), 1..24000; sent to
                                   Groq as max_completion_tokens
  PROMPT_LLM_REASONING_EFFORT      Groq GPT-OSS reasoning_effort: low (default) | medium | high; sent with
                                   include_reasoning=false; refused for any other provider/model
  PROMPT_LLM_REASONING             auto (default) | on | off (true/false accepted); "on" is refused for Groq
  PROMPT_LLM_REASONING_MAX_TOKENS  optional reasoning cap, only with reasoning sent and below max_tokens; a request to
                                   the provider, not a guarantee
Invalid values stop the worker before it can build any request.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Mapping

from .prompt_builder import (
    GenerationConfigError, resolve_generation_settings, resolve_reasoning_effort,
)

TRUE_VALUES = {"1", "true", "yes", "on"}
# OpenCode runs with GPT-OSS 120B on direct Groq through the PRQA model proxy unless a registry entry overrides
# opencode.model: "prqa" is the OpenCode provider id the sandbox entrypoint configures for the proxy (per-job token,
# no provider key in the sandbox); the launcher maps prqa/<model> to the proxy's PRQA_MODEL_ALLOWED_MODEL <model>.
DEFAULT_OPENCODE_MODEL = "prqa/openai/gpt-oss-120b"
SYNTHETIC_PREFIX = "synthetic-org/"

# Active prompt-LLM provider: direct Groq (OpenAI-compatible Chat Completions API), GPT-OSS 120B (production model,
# 131,072-token context, 65,536 max completion tokens: https://console.groq.com/docs/models). The same values are the
# compose defaults and compose/.env.example; PROMPT_LLM_API_BASE / PROMPT_LLM_MODEL override them.
DEFAULT_PROMPT_LLM_API_BASE = "https://api.groq.com/openai/v1"
DEFAULT_PROMPT_LLM_MODEL = "openai/gpt-oss-120b"
# 3000: the locked production completion budget. With the ~3.7k-token C01 request it keeps prompt + completion
# well under the account's 8,000 tokens-per-minute limit (the old 24000 budget did not). MAX_TOKENS_CEILING (24000)
# stays the highest value an operator may configure.
DEFAULT_PROMPT_LLM_MAX_TOKENS = 3000
# A REAL prompt-generation request never goes from n8n to the provider: it goes to the model proxy's inference listener
# (shared provider-budget ledger, locked request contract, sole holder of the provider key) with the prompt credential.
DEFAULT_PROMPT_LLM_PROXY_URL = "http://prqa-model-proxy:8089/v1"
PROMPT_LLM_PROXY_URL_RE = re.compile(r"^http://[a-z0-9][a-z0-9.-]{0,62}:[0-9]{2,5}/v1$")

# Provider-name constants (derived from base URL, used by the prompt builder to strip
# provider-specific request fields — e.g. "reasoning" is OpenRouter-only).
PROVIDER_GROQ = "groq"
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_OTHER = "other"
REAL_FLAGS = (
    "REAL_PROJECT_TESTING_ENABLED",
    "AUTO_EXECUTION_ENABLED",
    "REAL_GITHUB_REPORTING_ENABLED",
    "REAL_DISCORD_REPORTING_ENABLED",
    "REPOSITORY_REGISTRY_DEFAULT_ENABLED",
)


def _flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "false").strip().lower() in TRUE_VALUES


def _resolve_prompt_llm_key(env: Mapping[str, str], provider: str) -> str:
    """Resolve the API key for the prompt LLM endpoint of `provider` (from _provider_from_base).

    Groq: GROQ_API_KEY only. Any other (legacy) provider: PROMPT_LLM_API_KEY only. There is no cross-provider fallback,
    so a missing Groq key stays missing. The key is never logged or returned in any public API response.
    """
    name = "GROQ_API_KEY" if provider == "groq" else "PROMPT_LLM_API_KEY"
    return (env.get(name) or "").strip()


def _reasoning_mode(raw: str) -> str:
    """PROMPT_LLM_REASONING as a mode: empty -> auto, true/false aliases -> on/off; anything else is kept verbatim
    so validation rejects it instead of guessing."""
    value = (raw or "").strip().lower()
    if not value:
        return "auto"
    return {"true": "on", "1": "on", "yes": "on", "false": "off", "0": "off", "no": "off"}.get(value, value)


def _prompt_llm_provider(base_url: str) -> str:
    """Map a base URL to a provider name constant.  Used by the prompt builder to
    decide which provider-specific fields to include (e.g. 'reasoning' is
    OpenRouter-only and must be omitted for Groq to avoid a 400 error)."""
    url = (base_url or "").lower().rstrip("/")
    if "groq.com" in url:
        return PROVIDER_GROQ
    if "openrouter.ai" in url:
        return PROVIDER_OPENROUTER
    return PROVIDER_OTHER


@dataclass(frozen=True)
class Policy:
    automation_mode: str
    real_project_testing_enabled: bool
    auto_execution_enabled: bool
    real_github_reporting_enabled: bool
    real_discord_reporting_enabled: bool
    registry_default_enabled: bool
    forced_off: tuple[str, ...]
    mock_services_url: str
    github_api_base: str
    prompt_llm_api_base: str
    prompt_llm_model: str
    opencode_model: str
    prompt_llm_live_in_review_only: bool = False
    # Derived provider name ("groq", "openrouter", "other") — set from prompt_llm_api_base
    prompt_llm_provider: str = PROVIDER_GROQ
    # Generation settings as configured (raw strings; validated by generation_settings() / self_check())
    prompt_llm_max_tokens: str = str(DEFAULT_PROMPT_LLM_MAX_TOKENS)
    prompt_llm_reasoning: str = "auto"
    prompt_llm_reasoning_max_tokens: str = ""
    prompt_llm_reasoning_effort: str = ""
    prompt_llm_proxy_url: str = DEFAULT_PROMPT_LLM_PROXY_URL

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Policy:
        env = os.environ if env is None else env
        mode = env.get("AUTOMATION_MODE", "review_only").strip().lower()
        if mode != "live":
            mode = "review_only"
        raw = {name: _flag(env, name) for name in REAL_FLAGS}
        forced_off: list[str] = []
        if mode == "review_only":
            for name, value in raw.items():
                if value:
                    forced_off.append(name)
                raw[name] = False
        base = env.get("PROMPT_LLM_API_BASE", DEFAULT_PROMPT_LLM_API_BASE).rstrip("/")
        return cls(
            automation_mode=mode,
            real_project_testing_enabled=raw["REAL_PROJECT_TESTING_ENABLED"],
            auto_execution_enabled=raw["AUTO_EXECUTION_ENABLED"],
            real_github_reporting_enabled=raw["REAL_GITHUB_REPORTING_ENABLED"],
            real_discord_reporting_enabled=raw["REAL_DISCORD_REPORTING_ENABLED"],
            registry_default_enabled=raw["REPOSITORY_REGISTRY_DEFAULT_ENABLED"],
            forced_off=tuple(forced_off),
            mock_services_url=env.get("MOCK_SERVICES_URL", "http://prqa-mocks:8090").rstrip("/"),
            github_api_base=env.get("GITHUB_API_BASE", "https://api.github.com").rstrip("/"),
            prompt_llm_api_base=base,
            prompt_llm_model=env.get("PROMPT_LLM_MODEL", DEFAULT_PROMPT_LLM_MODEL),
            opencode_model=env.get("OPENCODE_MODEL", "").strip() or DEFAULT_OPENCODE_MODEL,
            prompt_llm_live_in_review_only=_flag(env, "PROMPT_LLM_LIVE_IN_REVIEW_ONLY"),
            prompt_llm_provider=_prompt_llm_provider(base),
            prompt_llm_max_tokens=env.get("PROMPT_LLM_MAX_TOKENS", "").strip() or str(DEFAULT_PROMPT_LLM_MAX_TOKENS),
            prompt_llm_reasoning=_reasoning_mode(env.get("PROMPT_LLM_REASONING", "")),
            prompt_llm_reasoning_max_tokens=env.get("PROMPT_LLM_REASONING_MAX_TOKENS", "").strip(),
            prompt_llm_reasoning_effort=env.get("PROMPT_LLM_REASONING_EFFORT", "").strip(),
            prompt_llm_proxy_url=env.get("PRQA_PROMPT_LLM_PROXY_URL", "").strip().rstrip("/") or DEFAULT_PROMPT_LLM_PROXY_URL,
        )

    def generation_settings(self) -> dict[str, Any]:
        """Validated {max_tokens, reasoning, reasoning_max_tokens, reasoning_effort} for the configured provider adapter
        and the *configured* model (also in review-only, so the mock request has the production shape).
        Raises GenerationConfigError; self_check() turns that into a start-up failure."""
        settings = resolve_generation_settings(self.prompt_llm_provider, max_tokens=self.prompt_llm_max_tokens,
                                               reasoning=self.prompt_llm_reasoning,
                                               reasoning_max_tokens=self.prompt_llm_reasoning_max_tokens or None)
        settings["reasoning_effort"] = resolve_reasoning_effort(self.prompt_llm_provider, self.prompt_llm_model,
                                                                self.prompt_llm_reasoning_effort or None)
        return settings

    @property
    def review_only(self) -> bool:
        return self.automation_mode != "live"

    @property
    def real_execution_possible(self) -> bool:
        return (
            not self.review_only
            and self.real_project_testing_enabled
            and self.auto_execution_enabled
        )

    @property
    def prompt_llm_real(self) -> bool:
        """The prompt LLM is the only integration that may go live inside review-only mode."""
        return (not self.review_only) or self.prompt_llm_live_in_review_only

    def endpoints(self) -> dict[str, Any]:
        """Service endpoints handed to n8n. Real URLs leave the worker only when the
        corresponding flag is on; otherwise n8n receives the mock service URLs."""
        mock = self.mock_services_url
        github_real = self.real_execution_possible
        llm_real = self.prompt_llm_real
        return {
            "github_api_base": self.github_api_base if github_real else f"{mock}/github",
            "github_api_mode": "real" if github_real else "mock",
            # real: the model proxy (never the provider itself; n8n holds no provider key) — see DEFAULT_PROMPT_LLM_PROXY_URL
            "prompt_llm_api_base": self.prompt_llm_proxy_url if llm_real else f"{mock}/llm/v1",
            "prompt_llm_api_mode": "real" if llm_real else "mock",
            "prompt_llm_model": self.prompt_llm_model if llm_real else "mock-llm",
            "prompt_llm_live_in_review_only": self.prompt_llm_live_in_review_only,
            "discord_mode": "real" if self.real_discord_reporting_enabled else "mock",
            "discord_mock_url": f"{mock}/discord/api/webhooks/000000000000000000/mock-token",
            "github_comment_mode": "real" if self.real_github_reporting_enabled else "mock",
            "mock_services_url": mock,
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "automation_mode": self.automation_mode,
            "review_only": self.review_only,
            "flags": {
                "REAL_PROJECT_TESTING_ENABLED": self.real_project_testing_enabled,
                "AUTO_EXECUTION_ENABLED": self.auto_execution_enabled,
                "REAL_GITHUB_REPORTING_ENABLED": self.real_github_reporting_enabled,
                "REAL_DISCORD_REPORTING_ENABLED": self.real_discord_reporting_enabled,
                "REPOSITORY_REGISTRY_DEFAULT_ENABLED": self.registry_default_enabled,
            },
            "forced_off": list(self.forced_off),
            "real_execution_possible": self.real_execution_possible,
            "prompt_llm_live_in_review_only": self.prompt_llm_live_in_review_only,
            "prompt_llm_provider": self.prompt_llm_provider,
            "prompt_llm_generation": self._public_generation_settings(),
            "opencode_model": self.opencode_model,
            "exceptions": (["PROMPT_LLM_LIVE_IN_REVIEW_ONLY: prompt-builder LLM calls go to the real provider "
                            f"({self.prompt_llm_provider}); GitHub, Discord, PR comments, OpenCode and the "
                            "registry stay mock/blocked"]
                           if self.review_only and self.prompt_llm_live_in_review_only else []),
            "endpoints": self.endpoints(),
        }

    def _public_generation_settings(self) -> dict[str, Any]:
        try:
            return self.generation_settings()
        except GenerationConfigError as e:
            return {"error": str(e)}

    def self_check(self) -> None:
        """Configuration invariants, enforced at worker start-up (fail closed)."""
        problems: list[str] = []
        if not PROMPT_LLM_PROXY_URL_RE.match(self.prompt_llm_proxy_url) or self.prompt_llm_proxy_url == self.prompt_llm_api_base:
            problems.append("PRQA_PROMPT_LLM_PROXY_URL must be the model proxy's inference URL http://<proxy host>:<port>/v1 "
                            "(prompt generation never contacts the provider directly)")
        try:
            self.generation_settings()
        except GenerationConfigError as e:
            problems.append(f"prompt LLM generation settings: {e}")
        if self.review_only:
            ep = self.endpoints()
            if self.real_execution_possible:
                problems.append("review_only but real_execution_possible is true")
            for key in ("github_api_mode", "discord_mode", "github_comment_mode"):
                if ep[key] != "mock":
                    problems.append(f"review_only but endpoints.{key}={ep[key]!r}")
            if any((self.real_project_testing_enabled, self.auto_execution_enabled,
                    self.real_github_reporting_enabled, self.real_discord_reporting_enabled,
                    self.registry_default_enabled)):
                problems.append("review_only but a REAL_* flag is still true")
            if ep["prompt_llm_api_mode"] == "real" and not self.prompt_llm_live_in_review_only:
                problems.append("prompt LLM is real without PROMPT_LLM_LIVE_IN_REVIEW_ONLY")
        if problems:
            raise SystemExit("policy self-check failed: " + "; ".join(problems))


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason_code: str
    reason: str
    execution_mode: str | None = None  # "mock" | "live" | None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "execution_mode": self.execution_mode,
            "details": self.details,
        }


def _reject(code: str, reason: str, **details: Any) -> Decision:
    return Decision(False, code, reason, None, details)


def decide(policy: Policy, entry: Any | None, job: Mapping[str, Any]) -> Decision:
    """Decide whether ``job`` may execute. ``entry`` is the registry entry (or None).

    ``job`` needs: repository, synthetic (bool); optional trigger.label,
    trigger.source, pr.is_fork.
    """
    repository = str(job.get("repository", ""))
    synthetic = bool(job.get("synthetic", False))
    trigger = job.get("trigger") or {}
    label = trigger.get("label")
    pr = job.get("pr") or {}

    if synthetic:
        if not repository.startswith(SYNTHETIC_PREFIX):
            return _reject(
                "SYNTHETIC_NAME_INVALID",
                f"synthetic jobs must target a '{SYNTHETIC_PREFIX}*' repository",
                repository=repository,
            )
        if entry is None or entry.kind != "synthetic":
            return _reject(
                "SYNTHETIC_REPO_NOT_REGISTERED",
                "synthetic repository is not registered with kind=synthetic",
                repository=repository,
            )
        if label and label not in entry.allowed_labels:
            return _reject("LABEL_NOT_ALLOWED", f"label '{label}' is not allowed", label=label)
        return Decision(
            True,
            "SYNTHETIC_MOCK_EXECUTION",
            "synthetic fixture job: served by the mock executor, no external calls",
            "mock",
            {"repository": repository},
        )

    # ---- real job: every gate below must pass ----
    if repository.startswith(SYNTHETIC_PREFIX):
        return _reject("SYNTHETIC_FLAG_REQUIRED", "synthetic-org repositories must set synthetic=true")
    if policy.review_only:
        return _reject(
            "REVIEW_ONLY_MODE",
            "AUTOMATION_MODE=review_only: real-project jobs are rejected until Phase 2 is approved",
            repository=repository,
        )
    if not policy.real_project_testing_enabled:
        return _reject("REAL_PROJECT_TESTING_DISABLED", "REAL_PROJECT_TESTING_ENABLED=false")
    if not policy.auto_execution_enabled:
        return _reject("AUTO_EXECUTION_DISABLED", "AUTO_EXECUTION_ENABLED=false")
    if entry is None:
        return _reject("REPOSITORY_NOT_REGISTERED", "repository is not in config/repositories.yaml", repository=repository)
    if entry.kind != "real":
        return _reject("REPOSITORY_KIND_INVALID", f"registry kind '{entry.kind}' cannot run real jobs")
    if not entry.enabled:
        return _reject("REPOSITORY_DISABLED", "registry entry has enabled=false", repository=repository)
    if entry.approval_status != "approved":
        return _reject(
            "REPOSITORY_PENDING_APPROVAL",
            f"registry approval_status is '{entry.approval_status}', not 'approved'",
            repository=repository,
        )
    if (entry.api or {}).get("environment") == "production":
        return _reject("PRODUCTION_TARGET_FORBIDDEN", "production environments are never tested")
    if pr.get("is_fork") and not entry.fork_prs_eligible:
        return _reject("FORK_PR_NOT_ELIGIBLE", "fork PRs are not eligible for this repository")
    if label and label not in entry.allowed_labels:
        return _reject("LABEL_NOT_ALLOWED", f"label '{label}' is not allowed", label=label)
    source = trigger.get("source")
    if source == "synthetic_test":
        return _reject("TRIGGER_SOURCE_INVALID", "synthetic_test triggers cannot start real jobs")
    return Decision(True, "LIVE_EXECUTION_ALLOWED", "all policy gates passed", "live", {"repository": repository})


# Gates a verified single-run grant stands in for (and nothing else): review-only, the two global REAL_* execution
# flags, and the registry's enabled/approved state. Every other rule below is the same as in decide().
GRANT_REPLACEABLE = ("REVIEW_ONLY_MODE", "REAL_PROJECT_TESTING_DISABLED", "AUTO_EXECUTION_DISABLED",
                     "REPOSITORY_DISABLED", "REPOSITORY_PENDING_APPROVAL")


def decide_with_grant(policy: Policy, entry: Any | None, job: Mapping[str, Any]) -> Decision:
    """Decision for a job that carries a verified, correctly bound single-run grant (run_grants.py). Never used for
    synthetic jobs, never widens anything except the five GRANT_REPLACEABLE gates, never changes global state."""
    repository = str(job.get("repository", ""))
    trigger = job.get("trigger") or {}
    label = trigger.get("label")
    pr = job.get("pr") or {}
    if job.get("synthetic") or repository.startswith(SYNTHETIC_PREFIX):
        return _reject("GRANT_SYNTHETIC_FORBIDDEN", "single-run grants never apply to synthetic repositories")
    if entry is None:
        return _reject("REPOSITORY_NOT_REGISTERED", "repository is not in config/repositories.yaml", repository=repository)
    if entry.kind != "real":
        return _reject("REPOSITORY_KIND_INVALID", f"registry kind '{entry.kind}' cannot run real jobs")
    if (entry.api or {}).get("environment") == "production":
        return _reject("PRODUCTION_TARGET_FORBIDDEN", "production environments are never tested")
    if pr.get("is_fork") and not entry.fork_prs_eligible:
        return _reject("FORK_PR_NOT_ELIGIBLE", "fork PRs are not eligible for this repository")
    if label and label not in entry.allowed_labels:
        return _reject("LABEL_NOT_ALLOWED", f"label '{label}' is not allowed", label=label)
    if trigger.get("source") == "synthetic_test":
        return _reject("TRIGGER_SOURCE_INVALID", "synthetic_test triggers cannot start real jobs")
    return Decision(True, "SINGLE_RUN_GRANT", "one-time operator grant bound to this repository/PR/head SHA", "live",
                    {"repository": repository, "replaced_gates": list(GRANT_REPLACEABLE)})
