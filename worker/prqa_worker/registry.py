"""Repository configuration registry (config/repositories.yaml)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .policy import SYNTHETIC_PREFIX, Policy

log = logging.getLogger("prqa.registry")

VALID_KINDS = {"synthetic", "real"}
VALID_APPROVAL = {"pending_approval", "approved", "synthetic_fixture"}


class RegistryError(ValueError):
    pass


@dataclass
class Entry:
    full_name: str
    kind: str
    enabled: bool
    approval_status: str
    display_name: str = ""
    default_branch: str = "main"
    allowed_events: list[str] = field(default_factory=lambda: ["labeled"])
    allowed_labels: list[str] = field(default_factory=list)
    fork_prs_eligible: bool = False
    max_runtime_minutes: int = 30
    max_retries: int = 1
    report: dict[str, Any] = field(default_factory=dict)
    api: dict[str, Any] = field(default_factory=dict)
    opencode: dict[str, Any] = field(default_factory=dict)
    fixtures: dict[str, Any] = field(default_factory=dict)
    approved_by: str | None = None
    approved_at: str | None = None
    warnings: list[str] = field(default_factory=list)

    def sandbox_target_token_env(self) -> str | None:
        """Name of the env var holding the staging target token to pass into the sandbox, or None (default: nothing)."""
        sandbox = (self.api or {}).get("sandbox") or {}
        if not sandbox.get("pass_target_token"):
            return None
        profile = ((self.api or {}).get("auth_profiles") or {}).get(sandbox.get("target_profile")) or {}
        return str(profile.get("token_env")) if profile.get("token_env") else None

    def effective_enabled(self, policy: Policy) -> bool:
        """True only when the entry *and* the global policy allow real execution."""
        return bool(self.kind == "real" and self.enabled and self.approval_status == "approved"
                    and policy.real_execution_possible)

    def summary(self, policy: Policy) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "kind": self.kind,
            "enabled": self.enabled,
            "approval_status": self.approval_status,
            "effective_enabled": self.effective_enabled(policy),
            "display_name": self.display_name,
            "environment": (self.api or {}).get("environment"),
            "allowed_labels": list(self.allowed_labels),
            "fork_prs_eligible": self.fork_prs_eligible,
            "warnings": list(self.warnings),
        }


class Registry:
    def __init__(self, entries: list[Entry], defaults: dict[str, Any], source: str = "") -> None:
        self._entries = {e.full_name: e for e in entries}
        self.defaults = defaults
        self.source = source

    @classmethod
    def load(cls, path: str | Path, policy: Policy) -> Registry:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if str(raw.get("registry_version", "")) != "1.0":
            raise RegistryError("registry_version must be '1.0'")
        defaults = dict(raw.get("defaults") or {})
        # REPOSITORY_REGISTRY_DEFAULT_ENABLED is honoured only in live mode; the file default wins otherwise.
        default_enabled = bool(defaults.get("enabled", False)) or (policy.registry_default_enabled and not policy.review_only)
        entries: list[Entry] = []
        seen: set[str] = set()
        for item in raw.get("repositories") or []:
            entry = cls._parse_entry(item, defaults, default_enabled, policy)
            if entry.full_name in seen:
                raise RegistryError(f"duplicate repository entry: {entry.full_name}")
            seen.add(entry.full_name)
            entries.append(entry)
        return cls(entries, defaults, str(path))

    @staticmethod
    def _parse_entry(item: dict[str, Any], defaults: dict[str, Any], default_enabled: bool, policy: Policy) -> Entry:
        full_name = str(item.get("full_name", "")).strip()
        if not full_name or "/" not in full_name:
            raise RegistryError(f"invalid full_name: {full_name!r}")
        kind = str(item.get("kind", "real"))
        if kind not in VALID_KINDS:
            raise RegistryError(f"{full_name}: kind must be one of {sorted(VALID_KINDS)}")
        if kind == "synthetic" and not full_name.startswith(SYNTHETIC_PREFIX):
            raise RegistryError(f"{full_name}: synthetic entries must be named '{SYNTHETIC_PREFIX}*'")
        if kind == "real" and full_name.startswith(SYNTHETIC_PREFIX):
            raise RegistryError(f"{full_name}: '{SYNTHETIC_PREFIX}*' names are reserved for synthetic fixtures")
        approval = str(item.get("approval_status", defaults.get("approval_status", "pending_approval")))
        if approval not in VALID_APPROVAL:
            raise RegistryError(f"{full_name}: approval_status must be one of {sorted(VALID_APPROVAL)}")
        api = dict(item.get("api") or {})
        if str(api.get("environment", "")).lower() == "production":
            raise RegistryError(f"{full_name}: environment 'production' is forbidden")
        sandbox = api.get("sandbox")
        if sandbox is not None:
            if not isinstance(sandbox, dict):
                raise RegistryError(f"{full_name}: api.sandbox must be a mapping")
            unknown = set(sandbox) - {"pass_target_token", "target_profile"}
            if unknown:
                raise RegistryError(f"{full_name}: api.sandbox has unknown keys {sorted(unknown)}")
            flag = sandbox.get("pass_target_token", False)
            if not isinstance(flag, bool):
                raise RegistryError(f"{full_name}: api.sandbox.pass_target_token must be true or false")
            if flag:
                profile_name = sandbox.get("target_profile")
                profiles = api.get("auth_profiles") or {}
                if not isinstance(profile_name, str) or profile_name not in profiles:
                    raise RegistryError(f"{full_name}: api.sandbox.target_profile must name an entry of api.auth_profiles")
                profile = profiles[profile_name] or {}
                if profile.get("type") != "bearer_token" or not str(profile.get("token_env") or "").strip():
                    raise RegistryError(f"{full_name}: target_profile '{profile_name}' must be a bearer_token profile with token_env")
                if not re.match(r"^[A-Z][A-Z0-9_]{2,63}$", str(profile["token_env"])):
                    raise RegistryError(f"{full_name}: token_env must be an environment variable NAME")
        enabled = bool(item.get("enabled", default_enabled))
        warnings: list[str] = []
        if enabled and kind == "real" and not policy.real_execution_possible:
            warnings.append(
                "enabled=true ignored: AUTOMATION_MODE=review_only or REAL_PROJECT_TESTING_ENABLED/AUTO_EXECUTION_ENABLED are false"
            )
            log.warning("registry %s: %s", full_name, warnings[-1])
        if enabled and kind == "real" and approval != "approved":
            warnings.append("enabled=true ignored: approval_status is not 'approved'")
        report = dict(defaults.get("report") or {})
        report.update(item.get("report") or {})
        return Entry(
            full_name=full_name,
            kind=kind,
            enabled=enabled,
            approval_status=approval,
            display_name=str(item.get("display_name", full_name)),
            default_branch=str(item.get("default_branch", "main")),
            allowed_events=list(item.get("allowed_events", defaults.get("allowed_events", ["labeled"]))),
            allowed_labels=list(item.get("allowed_labels", defaults.get("allowed_labels", []))),
            fork_prs_eligible=bool(item.get("fork_prs_eligible", defaults.get("fork_prs_eligible", False))),
            max_runtime_minutes=int(item.get("max_runtime_minutes", defaults.get("max_runtime_minutes", 30))),
            max_retries=int(item.get("max_retries", defaults.get("max_retries", 1))),
            report=report,
            api=api,
            opencode=dict(item.get("opencode") or {}),
            fixtures=dict(item.get("fixtures") or {}),
            approved_by=item.get("approved_by"),
            approved_at=item.get("approved_at"),
            warnings=warnings,
        )

    def get(self, full_name: str) -> Entry | None:
        return self._entries.get(full_name)

    def entries(self) -> list[Entry]:
        return list(self._entries.values())

    def summary(self, policy: Policy) -> dict[str, Any]:
        items = [e.summary(policy) for e in self._entries.values()]
        return {
            "source": self.source,
            "count": len(items),
            "effective_enabled_count": sum(1 for i in items if i["effective_enabled"]),
            "repositories": items,
        }
