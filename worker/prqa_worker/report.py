"""Report rendering: Markdown report, Discord payload and GitHub PR comment from a validated result."""
from __future__ import annotations

from datetime import datetime, UTC
from typing import Any

from .redaction import redact_text, redact_value

STATUS_EMOJI = {"PASS": "🟢", "PASS_WITH_KNOWN_ISSUES": "🟡", "FAIL": "🔴", "INCONCLUSIVE": "⚪"}
STATUS_COLOR = {"PASS": 3066993, "PASS_WITH_KNOWN_ISSUES": 16776960, "FAIL": 15158332, "INCONCLUSIVE": 9807270}
SYNTHETIC_BANNER = "🧪 **SYNTHETIC FIXTURE — not a real test run.** Produced by the mock executor for pipeline review."
MAX_FIELD = 1000
MAX_CASES_IN_COMMENT = 60


def _counts_line(summary: dict[str, Any]) -> str:
    return (f"✅ PASS {summary['passed']} · ❌ FAIL {summary['failed']} · ⛔ BLOCKED {summary['blocked']} "
            f"· ➖ N/A {summary['not_applicable']} · total {summary['total']}")


def _clip(text: str, limit: int = MAX_FIELD) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_markdown(result: dict[str, Any], job: dict[str, Any] | None = None) -> str:
    result = redact_value(result)
    s = result["summary"]
    status = s["overall_status"]
    lines: list[str] = []
    lines.append(f"# QA Report — {result['repository']} PR #{result['pr']['number']} — {STATUS_EMOJI.get(status, '')} {status}")
    lines.append("")
    if result.get("synthetic"):
        lines.append(SYNTHETIC_BANNER)
        lines.append("")
    lines.append("## A. Executive Summary")
    lines.append("")
    lines.append(f"- **PR:** {result['pr']['title']} (`{result['pr']['head_sha'][:12]}`)")
    lines.append(f"- **Environment:** {result['environment']['name']} — `{result['environment']['base_url_redacted']}`")
    lines.append(f"- **Executed by:** {result['executed_by']['engine']} ({result['executed_by']['mode']} mode)"
                 + (f", model `{result['executed_by'].get('model')}`" if result['executed_by'].get('model') else ""))
    lines.append("- **Method:** curl-based API testing")
    lines.append(f"- **Results:** {_counts_line(s)}")
    lines.append(f"- **Recommendation:** **{result['recommendation']['verdict']}** — {result['recommendation']['rationale']}")
    if s.get("executive_summary"):
        lines.append("")
        lines.append(s["executive_summary"])
    lines.append("")
    lines.append("## B. Coverage Matrix")
    lines.append("")
    lines.append("| Area | Planned | Passed | Failed | Blocked | N/A |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for row in result["coverage_matrix"]:
        lines.append(f"| {row['area']} | {row['planned']} | {row['passed']} | {row['failed']} | {row['blocked']} | {row['not_applicable']} |")
    lines.append(f"| **Total** | {s['total']} | {s['passed']} | {s['failed']} | {s['blocked']} | {s['not_applicable']} |")
    lines.append("")
    lines.append("## C. Detailed Test Results")
    lines.append("")
    lines.append("| Test ID | Scenario | Endpoint | Expected | Actual | Status | Evidence |")
    lines.append("|---|---|---|---|---|---|---|")
    for c in result["test_cases"]:
        lines.append(f"| {c['id']} | {_clip(c['scenario'], 120)} | `{c['endpoint']}` | {_clip(c['expected'], 160)} | {_clip(c['actual'], 160)} | {c['status']} | {c.get('evidence') or ''} |")
    lines.append("")
    lines.append("## D. Defect Report")
    lines.append("")
    if not result["defects"]:
        lines.append("No defects were identified within the executed test coverage.")
    for d in result["defects"]:
        lines.append(f"### {d['id']} — {d['title']} ({d['severity']})")
        lines.append("")
        lines.append(f"- **Endpoint:** `{d['endpoint']}`")
        lines.append(f"- **Severity rationale:** {d.get('severity_rationale', '')}")
        lines.append(f"- **Origin:** {d['origin']}")
        lines.append(f"- **Expected:** {d['expected']}")
        lines.append(f"- **Actual:** {d['actual']}")
        if d.get("impact"):
            lines.append(f"- **Impact:** {d['impact']}")
        if d.get("repro_curl"):
            lines.append("")
            lines.append("```bash")
            lines.append(d["repro_curl"])
            lines.append("```")
        lines.append("")
    lines.append("## E. Regression Results")
    lines.append("")
    lines.append(result.get("regression_summary") or "No regression summary provided.")
    lines.append("")
    lines.append("## F. Blockers and Limitations")
    lines.append("")
    lines.extend(f"- {b}" for b in result["blockers"]) if result["blockers"] else lines.append("None reported.")
    lines.append("")
    lines.append("## G. Final Recommendation")
    lines.append("")
    lines.append(f"**{result['recommendation']['verdict']}** — {result['recommendation']['rationale']}")
    lines.append("")
    lines.append("## H. Evidence Artifacts")
    lines.append("")
    ev = result.get("evidence") or {}
    lines.append(f"- Directory: `{ev.get('directory', 'n/a')}`")
    for f in (ev.get("files") or [])[:50]:
        lines.append(f"- `{f}`")
    lines.append("")
    lines.append(f"_Generated by PR QA Pilot · job `{result['job_id']}` · {datetime.now(UTC).isoformat(timespec='seconds')}_")
    return redact_text("\n".join(lines))


def render_discord(result: dict[str, Any], job: dict[str, Any] | None = None, *, mode: str = "mock") -> dict[str, Any]:
    """Summary message (first of possibly several). Rendering lives in discord.py: sanitized untrusted text,
    Discord size limits enforced, allowed_mentions always empty."""
    from .discord import build_result_messages  # local import: discord.py imports redaction only
    return build_result_messages(result, mode=mode)[0]


def render_github_comment(result: dict[str, Any], job: dict[str, Any] | None = None) -> str:
    result = redact_value(result)
    s = result["summary"]
    status = s["overall_status"]
    lines = [f"<!-- prqa:job:{result['job_id']} -->"]
    if result.get("synthetic"):
        lines.append(SYNTHETIC_BANNER)
        lines.append("")
    lines.append(f"## {STATUS_EMOJI.get(status, '')} PR QA Pilot — {status}")
    lines.append("")
    lines.append(f"**{_counts_line(s)}**")
    lines.append("")
    lines.append(f"Recommendation: **{result['recommendation']['verdict']}** — {_clip(result['recommendation']['rationale'], 500)}")
    lines.append("")
    if result["defects"]:
        lines.append("### Defects")
        lines.append("")
        lines.append("| ID | Severity | Title | Endpoint | Origin |")
        lines.append("|---|---|---|---|---|")
        for d in result["defects"][:20]:
            lines.append(f"| {d['id']} | {d['severity']} | {_clip(d['title'], 100)} | `{d['endpoint']}` | {d['origin']} |")
        lines.append("")
    if result["blockers"]:
        lines.append("### Blockers")
        lines.append("")
        lines.extend(f"- {b}" for b in result["blockers"][:10])
        lines.append("")
    lines.append("<details><summary>Test cases</summary>")
    lines.append("")
    lines.append("| Test ID | Scenario | Status |")
    lines.append("|---|---|---|")
    for c in result["test_cases"][:MAX_CASES_IN_COMMENT]:
        lines.append(f"| {c['id']} | {_clip(c['scenario'], 100)} | {c['status']} |")
    if len(result["test_cases"]) > MAX_CASES_IN_COMMENT:
        lines.append(f"| … | {len(result['test_cases']) - MAX_CASES_IN_COMMENT} more in the full report | |")
    lines.append("")
    lines.append("</details>")
    lines.append("")
    lines.append(f"_Executed by {result['executed_by']['engine']} ({result['executed_by']['mode']}) · job `{result['job_id']}` · head `{result['pr']['head_sha'][:12]}`_")
    return redact_text("\n".join(lines))


def inconclusive_placeholder(job: dict[str, Any], errors: list[str], *, reason: str = "INVALID_RESULT") -> dict[str, Any]:
    """Honest INCONCLUSIVE result used when a job result fails validation or does not match the job.
    Mirrors the n8n node *Build INVALID_RESULT Placeholder (INCONCLUSIVE)* so both paths render identically."""
    req = job.get("request") or {}
    pr = req.get("pr") or {}
    synthetic = bool(job.get("synthetic"))
    return {
        "result_version": "1.0", "synthetic": synthetic, "job_id": job["id"], "repository": job["repository"],
        "pr": {"number": int(pr.get("number") or job.get("pr_number") or 1), "title": str(pr.get("title", ""))[:300],
               "head_sha": pr.get("head_sha") or job.get("head_sha"), **({"url": str(pr["url"])[:500]} if pr.get("url") else {})},
        "environment": {"name": "mock" if synthetic else "staging", "base_url_redacted": "unknown"},
        "executed_by": {"engine": "mock_executor" if synthetic else "opencode", "mode": "mock" if synthetic else "live",
                        **({"prompt_source": req["prompt"]["source"]} if (req.get("prompt") or {}).get("source") in ("llm", "reference_fallback", "fixture") else {})},
        "summary": {"overall_status": "INCONCLUSIVE", "total": 0, "passed": 0, "failed": 0, "blocked": 0, "not_applicable": 0,
                    "executive_summary": f"Result rejected by schema/reconciliation validation ({reason})."},
        "coverage_matrix": [], "test_cases": [], "defects": [],
        "blockers": [(f"{reason}: " + " | ".join(str(e) for e in errors[:10]))[:500]],
        "recommendation": {"verdict": "INCONCLUSIVE", "rationale": "The worker returned a result that does not satisfy qa-result.schema.json or does not match the job."},
    }


def render_all(result: dict[str, Any], job: dict[str, Any] | None = None, *, discord_mode: str, github_mode: str) -> dict[str, Any]:
    s = result["summary"]
    return {
        "report_markdown": render_markdown(result, job),
        "discord_payload": render_discord(result, job, mode=discord_mode),
        "github_comment_markdown": render_github_comment(result, job),
        "github_comment_mode": github_mode,
        "discord_mode": discord_mode,
        "summary_line": f"{s['overall_status']}: {_counts_line(s)}",
        "synthetic": bool(result.get("synthetic")),
    }
