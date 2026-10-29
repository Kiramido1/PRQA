#!/usr/bin/env python3
"""Offline readiness check for real GitHub / Discord delivery — presence, source and format only.

Never prints, hashes or measures a value and never contacts GitHub or Discord (a token's real permissions can only be
verified by an authorized call, which this script does not make). Reports per item:

  GITHUB_TOKEN          fine-grained PAT (github_pat_…) expected; least privilege for PRQA: repository access limited to
                        the allowlisted repositories, Metadata: read, Contents: read, Pull requests: read — plus
                        Issues: write ONLY if PR comments are separately approved. A classic token (ghp_…) cannot be
                        limited per repository and is reported as not least-privilege.
  DISCORD_WEBHOOK_URL   https://discord.com/api/webhooks/<id>/<token> (or discordapp.com), no query or fragment, for a
                        dedicated non-production channel
  GITHUB_WEBHOOK_SECRET, PRQA_WEBHOOK_SECRET   64 hex characters
  delivery mode         real only with AUTOMATION_MODE=live and REAL_GITHUB_REPORTING_ENABLED / REAL_DISCORD_REPORTING_ENABLED;
                        otherwise every delivery goes to the mock sinks

Approved source for the two delivery credentials: ~/.config/prqa/secrets.env (or PRQA_SECRETS_FILE), mode 600, off the
project drive. compose/.env sits on a drive that ignores file permissions, so a credential there is a finding.

    python3 scripts/setup/check_delivery_readiness.py          # exit 0 = no finding (absent credentials are not findings)
"""
from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GITHUB_FINE_RE = re.compile(r"^github_pat_[A-Za-z0-9_]{22,255}$")
GITHUB_CLASSIC_RE = re.compile(r"^gh[pousr]_[A-Za-z0-9]{36,255}$")
DISCORD_RE = re.compile(r"^https://(discord\.com|discordapp\.com)/api/webhooks/[0-9]{5,25}/[A-Za-z0-9_-]{20,200}$")
HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
DELIVERY = ("GITHUB_TOKEN", "DISCORD_WEBHOOK_URL")


def parse(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def github_status(value: str) -> str:
    if GITHUB_FINE_RE.match(value):
        return "fine-grained PAT format (permissions unverified offline)"
    if GITHUB_CLASSIC_RE.match(value):
        return "FINDING: classic token format — cannot be limited to allowlisted repositories; use a fine-grained PAT"
    return "FINDING: not a GitHub token format"


def discord_status(value: str) -> str:
    return "webhook URL format ok (destination unverified offline)" if DISCORD_RE.match(value) else \
        "FINDING: not a https Discord webhook URL (or carries a query/fragment)"


def check(compose_env: dict[str, str], secrets: dict[str, str], secrets_mode: int | None) -> dict[str, str]:
    report: dict[str, str] = {}
    for name in DELIVERY:
        in_compose, in_secrets = bool(compose_env.get(name)), bool(secrets.get(name))
        if in_compose:
            report[f"{name} source"] = "FINDING: set in compose/.env (permission-less drive); move it to the secrets file"
        elif in_secrets:
            report[f"{name} source"] = "secrets file" + ("" if secrets_mode == 0o600 else " — FINDING: secrets file is not mode 600")
        else:
            report[f"{name} source"] = "absent (real delivery not configured; mock sinks only)"
        value = secrets.get(name) or compose_env.get(name) or ""
        if value:
            report[f"{name} format"] = github_status(value) if name == "GITHUB_TOKEN" else discord_status(value)
    for name in ("GITHUB_WEBHOOK_SECRET", "PRQA_WEBHOOK_SECRET"):
        value = secrets.get(name) or compose_env.get(name) or ""
        report[name] = ("64-hex" if HEX64_RE.match(value) else "FINDING: present but not 64 hex characters") if value else "absent"
    merged = {**compose_env, **secrets}
    live = merged.get("AUTOMATION_MODE", "review_only").strip().lower() == "live"
    for flag, channel in (("REAL_GITHUB_REPORTING_ENABLED", "github"), ("REAL_DISCORD_REPORTING_ENABLED", "discord")):
        on = merged.get(flag, "false").strip().lower() in ("1", "true", "yes", "on")
        report[f"{channel} delivery mode"] = "real" if (live and on) else "mock (review-only or flag off)"
    return report


def main() -> int:
    secrets_path = Path(os.environ.get("PRQA_SECRETS_FILE") or Path.home() / ".config/prqa/secrets.env")
    mode = stat.S_IMODE(secrets_path.stat().st_mode) if secrets_path.is_file() else None
    report = check(parse(ROOT / "compose" / ".env"), parse(secrets_path), mode)
    for k, v in report.items():
        print(f"{k}: {v}")
    return 1 if any(v.startswith("FINDING") or " — FINDING" in v for v in report.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
