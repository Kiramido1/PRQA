#!/usr/bin/env python3
"""Trusted, host-side Discord delivery of PRQA results (used by scripts/pipeline/prqa_auto_runner.py; never by the sandbox).

* The webhook URL is read only from the secrets file (DISCORD_WEBHOOK_URL), validated against the exact Discord webhook
  form (https, discord.com / discordapp.com, /api/webhooks/<id>/<token>, no query/fragment), and never printed, logged,
  returned, or written anywhere. Redirects are refused (a redirect is reported as a failure, never followed).
* Content comes only from the worker-validated qa-result (or a structured blocked notice) and is sanitized twice: the
  worker's redaction plus a delivery layer for cookies, passwords, tokens, Authorization headers and `curl -u`. Rendering,
  Discord limits, `allowed_mentions: {"parse": []}`, retries with Retry-After and 5xx back-off are the worker's
  prqa_worker.discord implementation.
* Idempotent: one stable delivery id per run (`<run key>:<job id | blocked>`); a delivered id is never sent again, and a
  failed delivery is recorded separately from the QA verdict (the QA job is never re-run because Discord failed).
* No public report URL exists: the message names the local artifact id (run directory name), not a path or link.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[2]
import sys  # noqa: E402
sys.path.insert(0, str(ROOT / "worker"))
from prqa_worker import discord as dc  # noqa: E402
from prqa_worker.redaction import redact_value  # noqa: E402

DISCORD_RE = re.compile(r"^https://(discord\.com|discordapp\.com)/api/webhooks/[0-9]{5,25}/[A-Za-z0-9_-]{20,200}$")
SIMULATION_BANNER = ("🧪 **SIMULATION** — manually triggered without the PR label (labels unchanged); code checked out at the "
                     "exact head SHA. Real OpenCode execution against a disposable environment.")
SETUP_TEST_TEXT = "PRQA Discord integration test — configuration verification only; no PR test was executed."
_EXTRA = [
    (re.compile(r"(?i)\b(cookie|set-cookie)\s*:\s*[^\"'\n]+"), r"\1: [REDACTED]"),
    (re.compile(r"(?i)\b(authorization)\s*:\s*(?!\s*(?:\[REDACTED|Bearer\s+\$|Bearer\s+\[REDACTED|Bearer\s+<))[^\"'\n]+"), r"\1: [REDACTED]"),
    (re.compile(r"(?i)\"(password|passwd|access_token|refresh_token|token|secret|api_key)\"\s*:\s*\"[^\"]*\""), r'"\1": "[REDACTED]"'),
    (re.compile(r"(?i)\b(password|passwd|access_token|refresh_token|api_key)=([^&\s\"']+)"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)(\s-u\s+|\s--user\s+)(['\"]?)[^\s'\"]+:[^\s'\"]+\2"), r"\1'$USER:$PASSWORD'"),
]


def valid_webhook(url: str) -> bool:
    return bool(DISCORD_RE.match(url or ""))


def scrub(value: Any) -> Any:
    value = redact_value(value)
    if isinstance(value, str):
        for rx, sub in _EXTRA:
            value = rx.sub(sub, value)
        return value
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _no_redirect_open(req, timeout):
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


def make_sender(url: str, *, opener: Callable[..., Any] | None = None, sleep: Callable[[float], None] | None = None) -> dc.DiscordSender:
    if not valid_webhook(url):
        raise ValueError("DISCORD_WEBHOOK_URL is not an https Discord incoming-webhook URL")
    kw: dict[str, Any] = {"opener": opener or _no_redirect_open, "user_agent": "prqa-auto/0.1"}
    if sleep is not None:
        kw["sleep"] = sleep
    return dc.DiscordSender(url, **kw)


def extra_fields(*, artifact_id: str, cleanup: str | None, prompt: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"name": "Run artifact", "value": f"`{dc.sanitize_untrusted(artifact_id, 120, escape_markdown=False)}` (local PRQA host; no public link)", "inline": False},
            {"name": "Prompt gate", "value": dc.sanitize_untrusted(f"{prompt.get('source')} · score {prompt.get('score')} (threshold 85) · accepted {prompt.get('accepted')}", 300), "inline": True},
            {"name": "Cleanup", "value": dc.sanitize_untrusted(cleanup or "unknown", 200), "inline": True}]


def build_messages(*, result: dict[str, Any] | None, pv: dict[str, Any], entry: dict[str, Any], prompt: dict[str, Any],
                   simulation: bool = False) -> list[dict[str, Any]]:
    msgs = _build_messages(result=result, pv=pv, entry=entry, prompt=prompt)
    if simulation:
        first = msgs[0]
        first["content"] = (SIMULATION_BANNER + ("\n" + first["content"] if first.get("content") else ""))[:dc.LIMITS["content"]]
        if first.get("embeds"):
            first["embeds"][0]["title"] = ("🧪 SIMULATION · " + str(first["embeds"][0].get("title") or ""))[:dc.LIMITS["title"]]
        msgs[0] = dc.enforce_limits(first)
    return msgs


def _build_messages(*, result: dict[str, Any] | None, pv: dict[str, Any], entry: dict[str, Any], prompt: dict[str, Any]) -> list[dict[str, Any]]:
    artifact = Path(entry.get("run_dir") or "").name or "?"
    if result:
        res = scrub(result)
        if not res.get("defects"):
            res.setdefault("summary", {})["executive_summary"] = ((res["summary"].get("executive_summary") or "") +
                                                                 "\nNo defects were identified within the executed test coverage.").strip()
        msgs = dc.build_result_messages(res, prompt={"source": prompt.get("source"), "sha256": pv.get("prompt_sha256")}, mode="real")
        msgs[0]["embeds"][0]["fields"] = (msgs[0]["embeds"][0]["fields"] + extra_fields(artifact_id=artifact, cleanup=entry.get("cleanup"), prompt=prompt))[:dc.LIMITS["fields"]]
        return [dc.enforce_limits(m) for m in msgs]
    seed = {"repository": pv.get("repository"), "synthetic": False,
            "pr": {"number": pv.get("pr_number"), "title": pv.get("pr_title"), "head_sha": pv.get("head_sha"), "url": pv.get("pr_url")},
            "trigger": {"source": "prqa-auto-runner", "requested_label": "to be test"}}
    reason = scrub(str(entry.get("reason") or entry.get("status") or "blocked"))
    code = reason.split(":", 1)[0][:60]
    msg = dc.build_blocked_message(seed, {"reason_code": code, "reason": reason[:400]}, mode="real")
    msg["embeds"][0]["fields"] += extra_fields(artifact_id=artifact, cleanup=entry.get("cleanup"), prompt=prompt)
    return [dc.enforce_limits(msg)]


def deliver(*, url: str, delivery_id: str, ledger: dict[str, Any], messages: list[dict[str, Any]],
            opener: Callable[..., Any] | None = None, sleep: Callable[[float], None] | None = None) -> dict[str, Any]:
    """Send once per delivery id. `ledger` is the caller's persisted {delivery_id: record}."""
    prior = ledger.get(delivery_id)
    if prior and prior.get("delivered"):
        return dict(prior, skipped="already delivered")
    if not url:
        rec = {"delivered": False, "status": "not_configured"}
    elif not valid_webhook(url):
        rec = {"delivered": False, "status": "invalid_webhook_url"}
    else:
        sender = make_sender(url, opener=opener, sleep=sleep)
        out = dc.deliver_messages(messages, sender)
        rec = {"delivered": out["delivered"], "status": out["status"], "messages": out["messages"], "sent": out["sent"],
               "results": [{k: r[k] for k in ("message", "ok", "status_code", "attempts")} for r in out["results"]]}
    import datetime as _dt
    rec["at"] = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
    ledger[delivery_id] = rec
    return rec


def setup_test(url: str, *, opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Exactly one clearly labeled message, no PR data, no secrets. Returns HTTP status + time only."""
    sender = make_sender(url, opener=opener)
    sender.max_attempts = 1
    r = sender.send({"username": dc.USERNAME, "content": SETUP_TEST_TEXT, "allowed_mentions": {"parse": []}})
    import datetime as _dt
    return {"ok": r["ok"], "status_code": r["status_code"], "at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")}
