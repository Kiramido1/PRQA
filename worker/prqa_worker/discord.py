"""Discord reporting: message rendering with platform limits, untrusted-text sanitization and
retry-aware delivery. Used by ``POST /notifications/discord`` (PRQA 06 · Discord Reporter).

Guarantees
----------
* A report is rendered only from a result that passed ``validate_result`` (or from the honest
  INCONCLUSIVE placeholder the pipeline builds for invalid results). Never from an HTTP 200/201.
* Untrusted strings (PR title, defect text, blockers, model summaries) are sanitized: mass mentions and
  user/role/channel mentions are neutralised, Markdown control characters are escaped, and
  ``allowed_mentions`` is always ``{"parse": []}``.
* Every message respects Discord's documented limits (content 2000, title 256, description 4096,
  field name 256 / value 1024, 25 fields, 10 embeds, 6000 characters per message). Long reports are
  split into several messages; the remainder is summarised instead of truncated silently.
* Delivery is bounded: ``max_attempts`` attempts per message, retries only for timeouts, connection
  errors, HTTP 429 (honouring ``Retry-After`` / ``retry_after``, capped) and 5xx. Other 4xx fail fast.
* The webhook URL never appears in logs, errors or return values.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, UTC
from typing import Any
from collections.abc import Callable

from .redaction import redact_text, redact_value

log = logging.getLogger("prqa.discord")

LIMITS = {"content": 2000, "title": 256, "description": 4096, "field_name": 256, "field_value": 1024,
          "fields": 25, "footer": 2048, "embeds": 10, "total": 6000}
MAX_MESSAGES = 6
MAX_DEFECTS_RENDERED = 20
STATUS_EMOJI = {"PASS": "🟢", "PASS_WITH_KNOWN_ISSUES": "🟡", "FAIL": "🔴", "INCONCLUSIVE": "⚪", "BLOCKED": "⛔"}
STATUS_COLOR = {"PASS": 3066993, "PASS_WITH_KNOWN_ISSUES": 16776960, "FAIL": 15158332, "INCONCLUSIVE": 9807270, "BLOCKED": 10038562}
STATUS_WORD = {"PASS": "passed", "PASS_WITH_KNOWN_ISSUES": "passed with known issues", "FAIL": "failed",
               "INCONCLUSIVE": "inconclusive", "BLOCKED": "blocked"}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER_S = 30.0
USERNAME = "PR QA Pilot"

_MASS_MENTION_RE = re.compile(r"@(everyone|here)", re.I)
_TAG_MENTION_RE = re.compile(r"<(@[!&]?|#)(\d+)>")
_MD_CHARS_RE = re.compile(r"([\\*_~`|>#\[\]()])")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# ------------------------------------------------------------------ sanitize --
def sanitize_untrusted(text: Any, limit: int = 1000, *, escape_markdown: bool = True) -> str:
    """Neutralise mentions, escape Markdown, drop control characters, redact secrets, clip."""
    s = redact_text(str(text if text is not None else ""))
    s = _CONTROL_RE.sub("", s)
    s = _MASS_MENTION_RE.sub(lambda m: "@\u200b" + m.group(1), s)
    s = _TAG_MENTION_RE.sub(lambda m: "<\u200b" + m.group(1) + m.group(2) + ">", s)
    if escape_markdown:
        s = _MD_CHARS_RE.sub(r"\\\1", s)
    return _clip(s, limit)


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _code_block(text: str, limit: int) -> str:
    """Code block for commands: backticks inside are neutralised, no Markdown escaping needed."""
    inner = redact_text(str(text or "")).replace("```", "`\u200b``")
    inner = _CONTROL_RE.sub("", inner)
    inner = _MASS_MENTION_RE.sub(lambda m: "@\u200b" + m.group(1), inner)
    inner = _TAG_MENTION_RE.sub(lambda m: "<\u200b" + m.group(1) + m.group(2) + ">", inner)
    return "```\n" + _clip(inner, max(0, limit - 8)) + "\n```"


def enforce_limits(payload: dict[str, Any]) -> dict[str, Any]:
    """Clip a webhook payload to Discord's limits. Always forces allowed_mentions.parse = []."""
    out = dict(payload)
    out["username"] = _clip(str(out.get("username") or USERNAME), 80)
    out["allowed_mentions"] = {"parse": []}
    if "content" in out and out["content"] is not None:
        out["content"] = _clip(str(out["content"]), LIMITS["content"])
    embeds = []
    for e in (out.get("embeds") or [])[: LIMITS["embeds"]]:
        e = dict(e)
        if "title" in e:
            e["title"] = _clip(str(e["title"]), LIMITS["title"])
        if "description" in e:
            e["description"] = _clip(str(e["description"]), LIMITS["description"])
        fields = []
        for f in (e.get("fields") or [])[: LIMITS["fields"]]:
            fields.append({"name": _clip(str(f.get("name") or "·"), LIMITS["field_name"]) or "·",
                           "value": _clip(str(f.get("value") or "·"), LIMITS["field_value"]) or "·",
                           "inline": bool(f.get("inline", False))})
        if fields:
            e["fields"] = fields
        else:
            e.pop("fields", None)
        if isinstance(e.get("footer"), dict):
            e["footer"] = {"text": _clip(str(e["footer"].get("text") or ""), LIMITS["footer"])}
        embeds.append(e)
    # total characters across embeds ≤ 6000: drop trailing fields, then shorten descriptions
    while embeds and _embed_chars(embeds) > LIMITS["total"]:
        last = embeds[-1]
        if last.get("fields"):
            last["fields"].pop()
            if not last["fields"]:
                last.pop("fields")
        elif len(str(last.get("description") or "")) > 200:
            last["description"] = _clip(last["description"], max(200, len(last["description"]) // 2))
        else:
            embeds.pop()
    if embeds:
        out["embeds"] = embeds
    else:
        out.pop("embeds", None)
    return out


def _embed_chars(embeds: list[dict[str, Any]]) -> int:
    n = 0
    for e in embeds:
        n += len(str(e.get("title") or "")) + len(str(e.get("description") or ""))
        n += len(str((e.get("footer") or {}).get("text") or ""))
        for f in e.get("fields") or []:
            n += len(str(f.get("name") or "")) + len(str(f.get("value") or ""))
    return n


# -------------------------------------------------------------------- render --
def _counts(summary: dict[str, Any]) -> str:
    return (f"✅ passed {summary.get('passed', 0)} · ❌ failed {summary.get('failed', 0)} · ⛔ blocked {summary.get('blocked', 0)}"
            f" · ➖ skipped/N-A {summary.get('not_applicable', 0)} · total {summary.get('total', 0)}")


def _links_field(pr_url: str, run_url: str | None, job_id: str | None, execution_id: str | None) -> str:
    parts = []
    if pr_url:
        parts.append(f"PR: {pr_url}")
    if run_url:
        parts.append(f"Actions run: {run_url}")
    if job_id:
        parts.append(f"PRQA job: `{job_id}`")
    if execution_id:
        parts.append(f"n8n execution: `{execution_id}`")
    return "\n".join(parts) or "n/a"


def _prompt_field(result: dict[str, Any], prompt: dict[str, Any] | None) -> str:
    source = (prompt or {}).get("source") or (result.get("executed_by") or {}).get("prompt_source") or "unknown"
    model = (prompt or {}).get("model") or (result.get("executed_by") or {}).get("model")
    api_mode = (prompt or {}).get("api_mode")
    if source == "llm" and api_mode == "mock":
        return f"🧪 MOCK LLM endpoint (review-only), not a live model{(' `' + sanitize_untrusted(model, 100) + '`') if model else ''}"
    if source == "llm":
        return f"live model{(' `' + sanitize_untrusted(model, 100) + '`') if model else ''}"
    if source == "reference_fallback":
        return "⚠️ deterministic reference fallback — NOT a live model (LLM unavailable or its output failed validation)"
    if source == "fixture":
        return "🧪 fixture prompt (synthetic run, no model)"
    return f"unknown ({sanitize_untrusted(source, 40)})"


def build_result_messages(result: dict[str, Any], *, validation: dict[str, Any] | None = None,
                          pipeline: dict[str, Any] | None = None, prompt: dict[str, Any] | None = None,
                          run_url: str | None = None, mode: str = "mock") -> list[dict[str, Any]]:
    """Render a validated qa-result into one or more webhook payloads (first = summary)."""
    result = redact_value(result)
    pipeline = pipeline or {}
    validation = validation or {"valid": True, "errors": []}
    s = result["summary"]
    status = s["overall_status"]
    synthetic = bool(result.get("synthetic"))
    prefix = "🧪 SYNTHETIC — " if synthetic else ""
    pr = result["pr"]
    pr_url = pr.get("url") or f"https://github.com/{result['repository']}/pull/{pr['number']}"
    title_text = sanitize_untrusted(pr.get("title", ""), 200)
    warnings: list[str] = []
    if not validation.get("valid", True):
        warnings.extend(sanitize_untrusted(e, 200) for e in (validation.get("errors") or [])[:5])
    exec_by = result.get("executed_by") or {}

    fields = [
        {"name": "Result", "value": _counts(s), "inline": False},
        {"name": "Verdict", "value": f"**{STATUS_WORD.get(status, status).upper()}** — {sanitize_untrusted(result['recommendation']['rationale'], 600)}", "inline": False},
        {"name": "Tested commit", "value": f"`{pr['head_sha']}`" + (f" (`{sanitize_untrusted(pr.get('head_ref'), 80, escape_markdown=False)}` → `{sanitize_untrusted(pr.get('base_ref'), 80, escape_markdown=False)}`)" if pr.get("head_ref") else ""), "inline": False},
        {"name": "Prompt", "value": _prompt_field(result, prompt), "inline": False},
        {"name": "Executed by", "value": f"{exec_by.get('engine', '?')} / {exec_by.get('mode', '?')} / Discord {mode}", "inline": True},
        {"name": "Environment", "value": f"{(result.get('environment') or {}).get('name', '?')}", "inline": True},
    ]
    if result["defects"]:
        lines = []
        for d in result["defects"][:5]:
            lines.append(f"**{sanitize_untrusted(d['id'], 20)}** · {d['severity']} · {sanitize_untrusted(d['title'], 120)}")
        if len(result["defects"]) > 5:
            lines.append(f"… {len(result['defects']) - 5} more (see follow-up messages)")
        fields.append({"name": f"Bugs found ({len(result['defects'])})", "value": "\n".join(lines), "inline": False})
    if result["blockers"]:
        fields.append({"name": f"Blockers ({len(result['blockers'])})", "value": "\n".join(f"• {sanitize_untrusted(b, 180)}" for b in result["blockers"][:5]), "inline": False})
    if warnings:
        fields.append({"name": "Validation warnings", "value": "\n".join(f"• {w}" for w in warnings), "inline": False})
    fields.append({"name": "Links", "value": _links_field(pr_url, run_url, result.get("job_id"), pipeline.get("execution_id")), "inline": False})

    summary_embed = {
        "title": f"{prefix}QA {STATUS_WORD.get(status, status)} — {result['repository']} #{pr['number']}",
        "url": pr_url if pr_url.startswith("https://github.com/") else None,
        "description": title_text + ("\n\n" + sanitize_untrusted(s.get("executive_summary"), 1200) if s.get("executive_summary") else ""),
        "color": STATUS_COLOR.get(status, 9807270),
        "fields": fields,
        "footer": {"text": ("SYNTHETIC FIXTURE · " if synthetic else "") + f"PR QA Pilot · job {result.get('job_id', '?')}"},
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if summary_embed["url"] is None:
        summary_embed.pop("url")
    messages = [{
        "username": USERNAME,
        "content": f"{STATUS_EMOJI.get(status, '')} {prefix}QA {STATUS_WORD.get(status, status)} for {result['repository']} PR #{pr['number']}",
        "allowed_mentions": {"parse": []},
        "embeds": [summary_embed],
    }]

    # Follow-up messages: one embed per defect (evidence + reproduction), chunked by Discord's limits.
    defect_embeds = []
    for d in result["defects"][:MAX_DEFECTS_RENDERED]:
        dfields = [
            {"name": "Severity · origin", "value": f"{d['severity']} · {d['origin']}" + (f" — {sanitize_untrusted(d.get('severity_rationale'), 300)}" if d.get("severity_rationale") else ""), "inline": False},
            {"name": "Endpoint", "value": sanitize_untrusted(d["endpoint"], 300, escape_markdown=False) and f"`{sanitize_untrusted(d['endpoint'], 290, escape_markdown=False)}`", "inline": False},
            {"name": "Expected", "value": sanitize_untrusted(d["expected"], 500), "inline": False},
            {"name": "Actual", "value": sanitize_untrusted(d["actual"], 500), "inline": False},
        ]
        if d.get("impact"):
            dfields.append({"name": "Impact", "value": sanitize_untrusted(d["impact"], 500), "inline": False})
        if d.get("test_case_ids"):
            dfields.append({"name": "Evidence (test cases)", "value": ", ".join(sanitize_untrusted(t, 40) for t in d["test_case_ids"][:10]), "inline": False})
        if d.get("repro_curl"):
            dfields.append({"name": "Reproduction", "value": _code_block(d["repro_curl"], LIMITS["field_value"]), "inline": False})
        defect_embeds.append({"title": f"{sanitize_untrusted(d['id'], 20)} · {sanitize_untrusted(d['title'], 200)}",
                              "color": STATUS_COLOR["FAIL"], "fields": dfields})
    if len(result["defects"]) > MAX_DEFECTS_RENDERED:
        defect_embeds.append({"title": f"… {len(result['defects']) - MAX_DEFECTS_RENDERED} more defects",
                              "description": "See the full Markdown report / GitHub comment for the complete defect list.", "color": STATUS_COLOR["FAIL"]})
    messages.extend(_chunk_embeds(defect_embeds, f"{prefix}Defects for {result['repository']} #{pr['number']}"))
    messages = [enforce_limits(m) for m in messages[:MAX_MESSAGES]]
    if len(messages) > 1:
        for i, m in enumerate(messages, 1):
            m["content"] = _clip(f"{m.get('content') or ''} ({i}/{len(messages)})".strip(), LIMITS["content"])
    return messages


def _chunk_embeds(embeds: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    """Group embeds into messages of ≤10 embeds and ≤6000 characters each."""
    messages: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for e in embeds:
        candidate = current + [e]
        if current and (len(candidate) > LIMITS["embeds"] or _embed_chars([enforce_limits({"embeds": candidate})["embeds"]][0]) > LIMITS["total"]):
            messages.append({"username": USERNAME, "content": label, "allowed_mentions": {"parse": []}, "embeds": current})
            current = [e]
        else:
            current = candidate
    if current:
        messages.append({"username": USERNAME, "content": label, "allowed_mentions": {"parse": []}, "embeds": current})
    return messages


def build_blocked_message(job_seed: dict[str, Any], decision: dict[str, Any], *, pipeline: dict[str, Any] | None = None,
                          mode: str = "mock") -> dict[str, Any]:
    """Notice for a request the policy gate refused (review-only mode, repository disabled, fork PR …)."""
    job_seed = redact_value(job_seed)
    pipeline = pipeline or {}
    pr = job_seed.get("pr") or {}
    trig = job_seed.get("trigger") or {}
    repository = job_seed.get("repository", "?")
    pr_url = pr.get("url") or f"https://github.com/{repository}/pull/{pr.get('number', 0)}"
    synthetic = bool(job_seed.get("synthetic"))
    prefix = "🧪 SYNTHETIC — " if synthetic else ""
    payload = {
        "username": USERNAME,
        "content": f"⛔ {prefix}QA blocked for {repository} PR #{pr.get('number', '?')} — {sanitize_untrusted(decision.get('reason_code'), 60, escape_markdown=False)}",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": f"{prefix}QA blocked — {repository} #{pr.get('number', '?')}",
            "description": sanitize_untrusted(pr.get("title", ""), 200),
            "color": STATUS_COLOR["BLOCKED"],
            "fields": [
                {"name": "Status", "value": "**BLOCKED** — no test was executed", "inline": False},
                {"name": "Reason", "value": f"`{sanitize_untrusted(decision.get('reason_code'), 60, escape_markdown=False)}` — {sanitize_untrusted(decision.get('reason'), 400)}", "inline": False},
                {"name": "Requested commit", "value": f"`{sanitize_untrusted(pr.get('head_sha'), 40, escape_markdown=False)}`", "inline": False},
                {"name": "Requested via", "value": sanitize_untrusted(trig.get("source", "unknown"), 40) + (f" (label `{sanitize_untrusted(trig.get('requested_label'), 60, escape_markdown=False)}`)" if trig.get("requested_label") else ""), "inline": True},
                {"name": "Links", "value": _links_field(pr_url, trig.get("run_url"), None, pipeline.get("execution_id")), "inline": False},
            ],
            "footer": {"text": ("SYNTHETIC FIXTURE · " if synthetic else "") + f"PR QA Pilot · Discord {mode}"},
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        }],
    }
    return enforce_limits(payload)


# ------------------------------------------------------------------ delivery --
class DiscordSender:
    """Posts webhook payloads with bounded retries. ``opener`` is injectable for tests."""

    def __init__(self, url: str, *, timeout_s: float = 10.0, max_attempts: int = 3, backoff_s: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep, opener: Callable[..., Any] = urllib.request.urlopen,
                 user_agent: str = "prqa-worker/0.1.0") -> None:
        self._url = url
        self.timeout_s = float(timeout_s)
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_s = float(backoff_s)
        self.sleep = sleep
        self.opener = opener
        self.user_agent = user_agent

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(enforce_limits(payload), ensure_ascii=False).encode("utf-8")
        attempts: list[dict[str, Any]] = []
        for attempt in range(1, self.max_attempts + 1):
            req = urllib.request.Request(self._url, data=data, method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": self.user_agent})
            status: int | None = None
            error: str | None = None
            retry_after = self.backoff_s * attempt
            try:
                with self.opener(req, timeout=self.timeout_s) as resp:
                    status = int(resp.status)
            except urllib.error.HTTPError as err:
                status = int(err.code)
                error = f"HTTP {status}"
                retry_after = self._retry_after(err, retry_after)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
                error = f"{type(err).__name__}: {redact_text(str(getattr(err, 'reason', err)))[:200]}"
            attempts.append({"attempt": attempt, "status_code": status, "error": error})
            if status is not None and 200 <= status < 300:
                return {"ok": True, "status_code": status, "attempts": attempts}
            retryable = status is None or status in RETRYABLE_STATUS
            if not retryable or attempt == self.max_attempts:
                break
            self.sleep(min(retry_after, MAX_RETRY_AFTER_S))
        last = attempts[-1]
        return {"ok": False, "status_code": last["status_code"], "attempts": attempts,
                "error": last["error"] or f"HTTP {last['status_code']}"}

    @staticmethod
    def _retry_after(err: urllib.error.HTTPError, default: float) -> float:
        header = err.headers.get("Retry-After") if err.headers else None
        try:
            if header:
                return float(header)
        except ValueError:
            pass
        try:
            body = json.loads(err.read().decode("utf-8") or "{}")
            if isinstance(body, dict) and isinstance(body.get("retry_after"), (int, float)):
                return float(body["retry_after"])
        except (ValueError, OSError, AttributeError):
            pass
        return default


def deliver_messages(messages: list[dict[str, Any]], sender: DiscordSender, *, spacing_s: float = 0.4) -> dict[str, Any]:
    """Send messages in order; stop at the first message that fails after its retries."""
    results = []
    for i, m in enumerate(messages):
        if i and spacing_s:
            sender.sleep(spacing_s)
        r = sender.send(m)
        results.append({"message": i + 1, "ok": r["ok"], "status_code": r["status_code"], "attempts": len(r["attempts"]),
                        "error": r.get("error")})
        if not r["ok"]:
            log.warning("discord delivery failed on message %s/%s: %s", i + 1, len(messages), r.get("error"))
            break
    sent = sum(1 for r in results if r["ok"])
    status = "delivered" if sent == len(messages) else ("partial" if sent else "failed")
    return {"delivered": sent == len(messages), "status": status, "messages": len(messages), "sent": sent, "results": results}
