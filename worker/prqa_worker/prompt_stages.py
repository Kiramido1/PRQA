"""Staged Groq QA prompt generation: three focused requests (or two, when they safely fit) instead of one oversized one.

The single-request path (prompt_builder.build_llm_request) sends the whole context package — every OpenAPI operation, the
claim ledger for every operation, the full diff — in one request of ~14k tokens, which the provider's 8,000
tokens-per-minute limit refuses outright. Here the same evidence is partitioned:

  1 scope     PR metadata, description, changed files, changed diff lines, the deterministic S-IDs and the candidate
              operations (only those the diff or the description points at) → a compact, provenance-tagged scope package
  2 coverage  the validated scope + OpenAPI excerpts of the scope's operations only (parameters, request schema,
              security, documented responses — extracted locally from the head's OpenAPI document) → test cases
  3 sections  scope summary + test-case index → PR-specific execution, evidence, severity, regression, blocker and report
              notes (the mandatory rules themselves are fixed text, never regenerated)
  two-stage   scope_coverage (1+2 in one request, only when the estimate fits the per-request target) → sections

Every stage output is parsed, schema-checked and provenance-checked before the next request is built: an unsupported
`[FACT]` claim is downgraded to unverified (never the reverse), operations outside the head's OpenAPI document are dropped,
duplicate test cases are removed, and stated requirements the cases miss are filled from the deterministic fallback's own
stated-requirement scenarios — nothing is dropped silently, every change is recorded. The final prompt is assembled
locally from the deterministic reference assembler (prompt_builder.reference_fallback_prompt: role, discovery, execution
rules, defect template, report format) with the validated stage content in its sections; it is never sent back to the
model. The existing quality gate (validator + 85-point rubric) then decides on the complete assembled prompt; passing the
per-stage checks is not acceptance.

Token control (per request and across time): each request's reservation is estimated the way the model proxy reserves it
(request bytes / calibrated bytes-per-token + the full completion allowance) and must stay under the per-stage target.
Before a request is released, the caller's capacity check (the shared ledger's read-only Ledger.capacity: rolling 60 s
window, provider-reported remaining tokens/reset, daily and per-job limits) decides whether it fits now, must wait, or can
never fit (hard limit → explicit failure). The proxy still reserves and settles every request in the same ledger.

Retries: at most `max_retries` (default 1) per stage, only for rate limits, transient provider/transport errors and
unusable output, and only after the capacity check allows it. Identical requests, same temperature. A completed stage is
never repeated; a validated artifact is reused when its fingerprint (generator version, stage, mode, model, head SHA,
exact request) is unchanged and never when the head SHA differs.

Stdlib only; no network. The caller (scripts/pipeline/prqa_stages.py, driven by n8n) owns the provider calls.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Callable

from . import prompt_builder as pb

GENERATOR_VERSION = "prqa-staged-v1"
STAGE_PROMPTS = {"scope": "llm/staged/stage1_scope.v1.md", "coverage": "llm/staged/stage2_coverage.v1.md",
                 "sections": "llm/staged/stage3_sections.v1.md", "scope_coverage": "llm/staged/stage12_scope_coverage.v1.md"}
ARTIFACTS = {"scope": "stage1_scope.json", "coverage": "stage2_api_coverage.json", "scope_coverage": "stage1_scope_coverage.json",
             "sections": "stage3_prompt_sections.json"}
SECTIONS_MD = "stage3_prompt_sections.md"
METADATA = "generation-metadata.json"
ASSEMBLED = "qa-prompt.staged.md"            # the candidate handed to the gate; qa-prompt.md stays the gate's output
MODES = {"3": ("scope", "coverage", "sections"), "2": ("scope_coverage", "sections")}
# completion caps are shared with the model's hidden reasoning (live: stage 1 used 1,794 of 1,800 and was truncated once)
DEFAULT_COMPLETION = {"scope": 2400, "coverage": 3000, "sections": 1400, "scope_coverage": 3000}
REQUEST_OVERHEAD_BYTES = 160   # keys the proxy adds to the upstream body (stream, temperature, reasoning)
ESTIMATE_SAFETY = 1.3          # = proxy/provider_budget.ESTIMATE_SAFETY (the proxy's reservation; a test keeps them equal)
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
DEFAULT_RETRY_WAIT_S = 60.0                  # one rolling window, when neither the ledger nor the provider says more
MAX_TEXT = 220
RAW_KEEP_CHARS = 30_000
DESCRIPTION_CHARS = 4000
MAX_CANDIDATES = 40
SOURCES = ("diff", "openapi", "description", "unverified")
TAG = {"diff": "[FACT: diff]", "openapi": "[FACT: openapi]", "description": "[STATED: PR description]", "unverified": "[UNVERIFIED]"}
LABELS = ("Required", "Regression", "Conditional", "Blocked")
CASE_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{3}$")
S_ID_RE = re.compile(r"^S\d{1,2}$")
RISK_ID_RE = re.compile(r"^RISK-\d{1,2}$")
ANY_TAG_RE = re.compile(r"\[(?:FACT|STATED|UNVERIFIED|ASSUMPTION)\b[^\]]*\]", re.I)
SEVERITY_RE = re.compile(r"^P[0-3]$")
_OP_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(/[A-Za-z0-9_{}./:-]*)")
_BACKTICK_RE = re.compile(r"`([^`\n]{1,80})`")
_WORD_RE = re.compile(r"[a-z][a-z0-9_]{3,}")
_NUMBER_RE = re.compile(r"(?<![\w.])\d{1,6}(?![\w.])")


class StageConfigError(ValueError):
    """Invalid staged-generation configuration (fail closed: no request is built)."""


@dataclass(frozen=True)
class StageConfig:
    mode: str = "3"                          # "3" (default) or "2" (used only when its estimate fits)
    target_tokens: int = 5500                # per request: estimated input + reserved completion
    completion: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_COMPLETION))
    bytes_per_token: float = 4.5             # the model proxy's calibrated request bytes per prompt token
    max_retries: int = 1
    max_wait_s: float = 600.0                # longest deferral accepted for one request before giving up
    max_operations: int = 12                 # operations one coverage request can represent reliably
    max_cases: int = 24                      # test cases one coverage completion can hold

    @classmethod
    def from_env(cls, env: dict[str, str]) -> StageConfig:
        def num(key: str, default: Any, kind=int) -> Any:
            raw = (env.get(key) or "").strip()
            if not raw:
                return default
            try:
                return kind(raw)
            except ValueError:
                raise StageConfigError(f"{key} must be a number") from None
        completion = dict(DEFAULT_COMPLETION)
        for stage in completion:
            completion[stage] = num(f"PRQA_PROMPT_STAGE_COMPLETION_{stage.upper()}", completion[stage])
        cfg = cls(mode=(env.get("PRQA_PROMPT_STAGE_MODE") or "3").strip(), target_tokens=num("PRQA_PROMPT_STAGE_TARGET_TOKENS", 5500),
                  completion=completion, bytes_per_token=num("PRQA_BUDGET_PROXY_BYTES_PER_TOKEN", 4.5, float),
                  max_retries=num("PRQA_PROMPT_STAGE_MAX_RETRIES", 1), max_wait_s=num("PRQA_PROMPT_STAGE_MAX_WAIT_S", 600.0, float))
        cfg.check()
        return cfg

    def check(self, tokens_per_minute: int | None = None) -> None:
        if self.mode not in MODES:
            raise StageConfigError("PRQA_PROMPT_STAGE_MODE must be 3 or 2")
        if not 1000 <= self.target_tokens <= 32000:
            raise StageConfigError("per-stage target must be 1000..32000 tokens")
        if tokens_per_minute is not None and self.target_tokens > tokens_per_minute:
            raise StageConfigError(f"per-stage target {self.target_tokens} exceeds the tokens-per-minute limit {tokens_per_minute}")
        for stage, n in self.completion.items():
            if not 200 <= n < self.target_tokens:
                raise StageConfigError(f"completion cap for {stage} must be 200..target")
        if not 1.0 <= self.bytes_per_token <= 8.0:
            raise StageConfigError("bytes per token must be within 1..8")
        if not 0 <= self.max_retries <= 2:
            raise StageConfigError("max retries per stage must be 0..2")


# ----------------------------------------------------------------------------------------------- small helpers --

def _sha(obj: Any) -> str:
    data = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _clean(text: Any, limit: int = MAX_TEXT) -> str:
    """One line of model text made inert for Markdown assembly: no evidence tags (the assembler sets them), no headings,
    bold markers, fences or line breaks; bounded."""
    s = ANY_TAG_RE.sub("", str(text or ""))
    s = s.replace("```", "").replace("**", "").replace("\r", " ").replace("\n", " ")
    s = re.sub(r"^\s*#+\s*", "", s)
    s = re.sub(r"\s+", " ", s).strip(" -–—")
    return s[:limit].rstrip()


def _norm_op(op: Any) -> str | None:
    m = _OP_RE.search(str(op or ""))
    return f"{m.group(1).upper()} {pb._norm_path(m.group(2))}" if m else None


def _op_index(known: list[str]) -> dict[str, str]:
    """normalized 'METHOD /path' → the operation as the context lists it"""
    out = {}
    for op in known:
        n = _norm_op(op)
        if n:
            out.setdefault(n, op)
    return out


def _forbidden(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in pb.FORBIDDEN_PHRASES) or any(pat.search(text) for name, pat in pb.PATTERNS if name != "email")


def parse_json_object(text: str | None) -> dict[str, Any] | None:
    """The first JSON object in a completion (a code fence or a short preamble is tolerated); None when there is none."""
    if not isinstance(text, str) or not text.strip():
        return None
    s = re.sub(r"^```[a-zA-Z]*\s*|\s*```\s*$", "", text.strip())
    start = s.find("{")
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(s[start:])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def estimate_request(request: dict[str, Any], cfg: StageConfig) -> dict[str, int]:
    """The reservation the model proxy will make for this request (proxy/provider_budget.reservation_tokens: body bytes /
    calibrated ratio × safety margin + the completion)."""
    body = len(json.dumps(request).encode("utf-8")) + REQUEST_OVERHEAD_BYTES
    completion = int(request.get("max_completion_tokens") or 0)
    total = int(math.ceil(math.ceil(body / cfg.bytes_per_token) * ESTIMATE_SAFETY)) + completion
    return {"input_tokens": total - completion, "completion_tokens": completion, "total_tokens": total, "request_bytes": body}


# ------------------------------------------------------------------------------------------------ stage inputs --

def diff_by_file(diff_text: str) -> list[tuple[str, list[str]]]:
    """(path, changed lines) per file: hunk headers and added/removed lines only (unchanged context is not evidence of
    the change and costs tokens)."""
    files: list[tuple[str, list[str]]] = []
    for chunk in re.split(r"(?m)^diff --git ", diff_text or ""):
        if not chunk.strip():
            continue
        m = re.match(r"a/(\S+) b/(\S+)", chunk)
        path = m.group(2) if m else "?"
        lines = [ln for ln in chunk.splitlines()[1:]
                 if (ln.startswith(("+", "-", "@@")) and not ln.startswith(("+++", "---")))]
        files.append((path, lines))
    if not files and diff_text:
        files.append(("?", [ln for ln in diff_text.splitlines() if ln.startswith(("+", "-", "@@"))]))
    return files


def diff_excerpts(diff_text: str, budget_chars: int) -> tuple[list[dict[str, Any]], bool]:
    """Changed lines per file within a character budget, shared fairly between files; (excerpts, truncated)."""
    files = diff_by_file(diff_text)
    out: list[dict[str, Any]] = []
    truncated = False
    remaining = max(0, budget_chars)
    for i, (path, lines) in enumerate(files):
        share = remaining // max(1, len(files) - i)
        text = "\n".join(lines)
        if len(text) > share:
            cut = text[:share]
            text = cut[:cut.rfind("\n")] if "\n" in cut else cut
            truncated = True
        remaining -= len(text)
        out.append({"path": path, "changed_lines": text})
    return out, truncated


def candidate_operations(context: dict[str, Any]) -> list[str]:
    """Operations of the head's OpenAPI document the change points at: routes the diff declares (decorator paths, matched
    by suffix so a router prefix does not matter), operations the description names, and the context's changed
    operations. Only operations that exist in the document; at most MAX_CANDIDATES."""
    api = context.get("api_spec") or {}
    known = list(api.get("known_operations") or [])
    diff_text = str((context.get("diff") or {}).get("text") or "")
    description = str((context.get("description") or {}).get("text") or "")
    added = "\n".join(ln[1:] for ln in diff_text.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    picked: list[str] = []

    def add(op: str) -> None:
        if op not in picked:
            picked.append(op)

    routes = [(m.group(1).upper(), pb._norm_path(m.group(2))) for m in pb.DECORATOR_OP_RE.finditer(added)]
    for method, path in routes:
        for op in known:
            n = _norm_op(op)
            if n and n.split(" ", 1)[0] == method and (n.split(" ", 1)[1].endswith(path) if path != "/" else False):
                add(op)
    for m in pb.OPERATION_RE.finditer(description.replace("`", " ")):
        method, path = m.group(1).upper(), pb._norm_path(m.group(2))
        for op in known:
            n = _norm_op(op)
            if n and n.split(" ", 1)[0] == method and n.split(" ", 1)[1].endswith(path):
                add(op)
    for m in re.finditer(r"\|\s*`?(GET|POST|PUT|PATCH|DELETE)`?\s*\|\s*`?(/[A-Za-z0-9_{}./-]+)`?", description):
        for op in known:
            n = _norm_op(op)
            if n and n.split(" ", 1)[0] == m.group(1) and n.split(" ", 1)[1].endswith(pb._norm_path(m.group(2))):
                add(op)
    for op in api.get("changed_operations") or []:
        if op in known:
            add(op)
    return picked[:MAX_CANDIDATES]


def stated_ids(context: dict[str, Any]) -> list[dict[str, str]]:
    desc = str((context.get("description") or {}).get("text") or "")
    title = str((context.get("pr") or {}).get("title") or "")
    return [{"id": r["id"], "text": _clean(r["text"], 200)} for r in pb.extract_stated_requirements(desc, title)]


def description_only_identifiers(context: dict[str, Any]) -> str:
    """The claim ledger's description-only line (the gate's own rule), without its per-operation status-code list."""
    for line in pb.claim_ledger(context).splitlines():
        if line.startswith("Description-only"):
            return line[:1200]
    return ""


def _resolve(schema: Any, comps: dict[str, Any], depth: int = 0) -> Any:
    if isinstance(schema, dict) and "$ref" in schema and depth < 3:
        name = str(schema["$ref"]).rsplit("/", 1)[-1]
        return _resolve(comps.get(name) or {}, comps, depth + 1)
    return schema


def _schema_brief(schema: Any, comps: dict[str, Any]) -> Any:
    s = _resolve(schema, comps)
    if not isinstance(s, dict):
        return None
    for key in ("anyOf", "oneOf", "allOf"):
        if isinstance(s.get(key), list):
            return [_schema_brief(x, comps) for x in s[key][:4]]
    out = {k: s[k] for k in ("type", "format", "enum", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                             "minLength", "maxLength", "default") if k in s}
    if isinstance(s.get("properties"), dict):
        out["properties"] = {n: _schema_brief(v, comps) for n, v in list(s["properties"].items())[:25]}
        if s.get("required"):
            out["required"] = list(s["required"])[:25]
    if isinstance(s.get("items"), dict):
        out["items"] = _schema_brief(s["items"], comps)
    return out


def contract_excerpt(op: str, context: dict[str, Any], openapi: dict[str, Any] | None) -> dict[str, Any]:
    """The OpenAPI facts of one operation, extracted locally (no model call): parameters, request schema, security and
    documented responses. Without the document, the context's documented responses only."""
    api = context.get("api_spec") or {}
    out: dict[str, Any] = {"op": op, "responses": (api.get("documented_responses") or {}).get(op) or
                           {c: "" for c in (api.get("documented_status_codes") or {}).get(op) or []}}
    if not isinstance(openapi, dict):
        out["contract"] = "OpenAPI document unavailable: status codes only"
        return out
    method, _, path = op.partition(" ")
    comps = (openapi.get("components") or {}).get("schemas") or {}
    item = (openapi.get("paths") or {}).get(path) or {}
    spec = item.get(method.lower()) if isinstance(item, dict) else None
    if not isinstance(spec, dict):
        out["contract"] = "operation not found in the OpenAPI document"
        return out
    params = [p for p in (item.get("parameters") or []) + (spec.get("parameters") or []) if isinstance(p, dict)]
    if params:
        out["parameters"] = [{"name": p.get("name"), "in": p.get("in"), "required": bool(p.get("required")),
                              "schema": _schema_brief(p.get("schema"), comps)} for p in params[:20]]
    body = (((spec.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")
    if body is not None:
        out["request_body"] = {"required": bool((spec.get("requestBody") or {}).get("required")), "schema": _schema_brief(body, comps)}
    security = spec.get("security", openapi.get("security"))
    out["security"] = sorted({k for s in (security or []) if isinstance(s, dict) for k in s}) or "none documented"
    return out


def scope_payload(context: dict[str, Any], *, repository: str, pr_number: int, diff_chars: int,
                  candidates: list[str]) -> tuple[dict[str, Any], bool]:
    pr = context.get("pr") or {}
    excerpts, truncated = diff_excerpts(str((context.get("diff") or {}).get("text") or ""), diff_chars)
    truncated = truncated or bool((context.get("diff") or {}).get("truncated"))
    payload = {
        "pr": {"repository": repository, "number": pr_number, "title": _clean(pr.get("title"), pb.PR_TITLE_MAX),
               "base_ref": pr.get("base_ref"), "head_ref": pr.get("head_ref")},
        "description": str((context.get("description") or {}).get("text") or "")[:DESCRIPTION_CHARS],
        "changed_files": [{k: f.get(k) for k in ("path", "status", "additions", "deletions")} for f in context.get("changed_files") or []][:60],
        "diff_excerpts": excerpts, "diff_truncated": truncated,
        "stated_requirements": stated_ids(context),
        "candidate_operations": candidates,
    }
    return payload, truncated


def compact_scope(scope: dict[str, Any]) -> dict[str, Any]:
    """The validated scope as later stages see it: tagged claims, no artifact bookkeeping."""
    return {
        "summary": scope.get("summary", ""),
        "requirements": [{"id": r["id"], "header": r["header"],
                          "claims": [f"{c['text']} [{c['source']}]" for c in r.get("claims", [])][:4]} for r in scope.get("requirements", [])],
        "operations": [o["op"] for o in scope.get("operations", [])],
        "risks": [{"id": r["id"], "text": r["text"]} for r in scope.get("risks", [])],
        "assumptions": [a["text"] for a in scope.get("assumptions", [])][:6],
    }


def coverage_payload(context: dict[str, Any], scope: dict[str, Any], openapi: dict[str, Any] | None, cfg: StageConfig) -> dict[str, Any]:
    budget = pb.plan_budget(context)
    return {"scope": compact_scope(scope),
            "contracts": [contract_excerpt(o["op"], context, openapi) for o in scope.get("operations", [])],
            "scenario_budget": min(budget["max_scenarios"], cfg.max_cases),
            "description_only_identifiers": description_only_identifiers(context)}


def sections_payload(scope: dict[str, Any], coverage: dict[str, Any]) -> dict[str, Any]:
    cases = coverage.get("test_cases", [])
    return {"scope": {"summary": scope.get("summary", ""),
                      "requirements": [{"id": r["id"], "header": r["header"]} for r in scope.get("requirements", [])],
                      "risks": [{"id": r["id"], "text": r["text"]} for r in scope.get("risks", [])],
                      "assumptions": [a["text"] for a in scope.get("assumptions", [])][:6]},
            "test_cases": [{"id": c["id"], "label": c["label"], "op": c["op"], "title": c["title"]} for c in cases],
            "uncovered": coverage.get("uncovered", []),
            "blocked": [{"id": c["id"], "reason": c.get("blocked_reason", "")} for c in cases if c["label"] == "Blocked"],
            "operations": [o["op"] for o in scope.get("operations", [])]}


# ------------------------------------------------------------------------------------------ output validation --

class Evidence:
    """What a stage claim may be checked against (the same trusted inputs the gate uses)."""

    def __init__(self, context: dict[str, Any]) -> None:
        api = context.get("api_spec") or {}
        self.known = _op_index(list(api.get("known_operations") or []))
        self.paths = {str(f.get("path")) for f in context.get("changed_files") or []}
        diff_text = str((context.get("diff") or {}).get("text") or "")
        self.diff = "\n".join(ln for ln in diff_text.splitlines() if ln.startswith(("+", "-")))
        self.description = str((context.get("description") or {}).get("text") or "")
        self.desc_words = set(_WORD_RE.findall(self.description.lower()))
        self.s_ids = [r["id"] for r in stated_ids(context)]

    def ops_in(self, text: str) -> list[str | None]:
        return [self.known.get(f"{m.group(1).upper()} {pb._norm_path(m.group(2))}") for m in _OP_RE.finditer(text)]

    def supports(self, source: str, text: str, ref: str) -> bool:
        """Conservative: a claim keeps a FACT/STATED source only when its literal content is found in that source."""
        if source == "diff":
            ids = [t for t in _BACKTICK_RE.findall(text)]
            nums = _NUMBER_RE.findall(text)
            return (ref in self.paths or any(ref and p.endswith(ref) for p in self.paths)) and bool(self.diff) and \
                all(i.strip() in self.diff for i in ids) and all(n in self.diff for n in nums) and not re.search(r"\b[1-5]\d\d\b", text)
        if source == "openapi":
            ops = self.ops_in(text) + ([self.known.get(_norm_op(ref) or "")] if _norm_op(ref) else [])
            return bool(ops) and all(ops) and not _BACKTICK_RE.search(text.replace(ref, ""))
        if source == "description":
            words = set(_WORD_RE.findall(text.lower()))
            if not words:
                return False
            return len(words & self.desc_words) / len(words) >= 0.6 and all(n in self.description for n in _NUMBER_RE.findall(text))
        return source == "unverified"


def _claim(item: Any, ev: Evidence, notes: list[str], where: str) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    text, ref = _clean(item.get("text")), _clean(item.get("ref"), 120)
    source = item.get("source") if item.get("source") in SOURCES else "unverified"
    if not text or _forbidden(text):
        notes.append(f"{where}: dropped an empty or forbidden claim")
        return None
    if any(op is None for op in ev.ops_in(text)):
        notes.append(f"{where}: dropped a claim naming an operation that is not in the head's OpenAPI document")
        return None
    if source != "unverified" and not ev.supports(source, text, ref):
        notes.append(f"{where}: '{text[:60]}' downgraded {source} → unverified (not found literally in that source)")
        source = "unverified"
    return {"text": text, "source": source, "ref": ref}


def validate_scope(raw: Any, context: dict[str, Any], candidates: list[str]) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    """(scope, notes, errors). errors → the stage output is unusable (retry within the budget, else stop)."""
    errors: list[str] = []
    notes: list[str] = []
    if not isinstance(raw, dict):
        return None, notes, ["stage 1 output is not a JSON object"]
    ev = Evidence(context)
    seen_text: set[str] = set()

    def claims(items: Any, where: str, limit: int) -> list[dict[str, str]]:
        out = []
        for it in (items if isinstance(items, list) else [])[:limit]:
            c = _claim(it, ev, notes, where)
            if c is None:
                continue
            key = re.sub(r"[^a-z0-9]", "", c["text"].lower())
            if key in seen_text:                       # one statement, one source: never a fact and an assumption
                notes.append(f"{where}: dropped a duplicate statement")
                continue
            seen_text.add(key)
            out.append(c)
        return out

    reqs: list[dict[str, Any]] = []
    given = {}
    for r in raw.get("requirements") if isinstance(raw.get("requirements"), list) else []:
        if isinstance(r, dict) and S_ID_RE.match(str(r.get("id") or "")) and r["id"] in ev.s_ids and r["id"] not in given:
            given[r["id"]] = r
        elif isinstance(r, dict):
            notes.append(f"requirements: dropped unknown or duplicate id {str(r.get('id'))[:12]!r}")
    stated = {s["id"]: s["text"] for s in stated_ids(context)}
    for sid in ev.s_ids:                               # every deterministic S-ID, in order; none lost
        r = given.get(sid)
        header = _clean((r or {}).get("header"), 160)
        if not header or any(op is None for op in ev.ops_in(header)):
            header = f"Stated requirement {sid}"
        if r is None:
            notes.append(f"requirements: {sid} missing from the model output; carried from the description")
        reqs.append({"id": sid, "header": header,
                     "claims": claims((r or {}).get("claims"), f"requirement {sid}", 6) or
                     [{"text": _clean(stated[sid], 200), "source": "description", "ref": sid}]})
    ops: list[dict[str, str]] = []
    allowed = {(_norm_op(c) or c): c for c in candidates}
    for o in raw.get("operations") if isinstance(raw.get("operations"), list) else []:
        op = allowed.get(_norm_op((o or {}).get("op") if isinstance(o, dict) else o) or "")
        if op is None:
            notes.append("operations: dropped an operation outside the candidate list")
            continue
        if op not in [x["op"] for x in ops]:
            ops.append({"op": op, "source": (o or {}).get("source") if isinstance(o, dict) and (o or {}).get("source") in SOURCES else "openapi",
                        "ref": _clean((o or {}).get("ref") if isinstance(o, dict) else "", 120)})
    if not ops:
        errors.append("stage 1 named no operation from the candidate list")
    risks = []
    for i, r in enumerate((raw.get("risks") if isinstance(raw.get("risks"), list) else [])[:8], start=1):
        text = _clean((r or {}).get("text") if isinstance(r, dict) else r)
        if text and not _forbidden(text) and not any(op is None for op in ev.ops_in(text)):
            risks.append({"id": f"RISK-{i}", "text": text, "ref": _clean((r or {}).get("ref") if isinstance(r, dict) else "", 120)})
    scope = {
        "summary": _clean(raw.get("summary"), 200),
        "changed_behavior": claims(raw.get("changed_behavior"), "changed_behavior", 12),
        "requirements": reqs,
        "operations": ops[:25],
        "assumptions": [dict(a, source="unverified") for a in claims(raw.get("assumptions"), "assumptions", 10)],
        "risks": risks,
        "out_of_scope": [t for t in (_clean(x) for x in (raw.get("out_of_scope") if isinstance(raw.get("out_of_scope"), list) else [])[:6])
                         if t and not _forbidden(t) and not any(op is None for op in ev.ops_in(t))],
    }
    if any(op is None for op in ev.ops_in(scope["summary"])) or _forbidden(scope["summary"]):
        scope["summary"] = ""
    return (None if errors else scope), notes, errors


def _valid_request(req: str, ops: dict[str, str], op: str) -> str | None:
    """A curl line the gate can accept as executable, or None (the case is then run from the discovered contract)."""
    r = " ".join(str(req or "").replace("`", "").split())
    if not r:
        return None
    if not pb.CURL_START_RE.search(r) or ("$BASE_URL" not in r and "${BASE_URL}" not in r) or re.search(r"https?://", r):
        return None
    if pb.LITERAL_TEMPLATE_RE.search(r) or _forbidden(r):
        return None
    for m in pb.CURL_BODY_ARG_RE.finditer(r):
        if pb.BODY_PLACEHOLDER_RE.search(m.group(1) or m.group(2) or ""):
            return None
    if re.search(r"(?i)bearer\s+(?!\$\{?TOKEN\}?|<INVALID_TOKEN>|<EXPIRED_TOKEN>)", r):
        return None
    called = pb.curl_operations(r)
    if not called or any((_norm_op(f"{m} {p}") or "") not in ops for m, p in called):
        return None
    if any(ops[_norm_op(f"{m} {p}")] != op for m, p in called):
        return None
    return r[:600]


def validate_coverage(raw: Any, context: dict[str, Any], scope: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    errors: list[str] = []
    notes: list[str] = []
    if not isinstance(raw, dict) or not isinstance(raw.get("test_cases"), list):
        return None, notes, ["stage 2 output has no test_cases list"]
    ops = {(_norm_op(o["op"]) or o["op"]): o["op"] for o in scope.get("operations", [])}
    s_ids = {r["id"] for r in scope.get("requirements", [])}
    risk_ids = {r["id"] for r in scope.get("risks", [])}
    cases: list[dict[str, Any]] = []
    ids: set[str] = set()
    keys: set[str] = set()
    for c in raw["test_cases"]:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip().upper()
        op = ops.get(_norm_op(c.get("op")) or "")
        label = str(c.get("label") or "").strip().capitalize()
        title = _clean(c.get("title"), 120)
        if not CASE_ID_RE.match(cid) or label not in LABELS or not title or op is None:
            notes.append(f"test case {cid or '?'}: dropped (invalid id, label, title or operation outside the scope)")
            continue
        if cid in ids:
            notes.append(f"test case {cid}: duplicate id dropped")
            continue
        key = f"{op}|{label}|{re.sub(r'[^a-z0-9]', '', title.lower())}"
        if key in keys:
            notes.append(f"test case {cid}: duplicate of an earlier case dropped")
            continue
        if _forbidden(title) or any(o is None for o in Evidence(context).ops_in(title)):
            notes.append(f"test case {cid}: dropped (forbidden content or unknown operation in the title)")
            continue
        links = [l for l in (str(x).strip().upper() for x in (c.get("links") or []) if isinstance(c.get("links"), list))
                 if l in s_ids or l in risk_ids][:4]
        s_links = [l for l in links if l in s_ids][:2]
        links = s_links + [l for l in links if l in risk_ids]
        request = _valid_request(c.get("request"), ops, op)
        if c.get("request") and request is None:
            notes.append(f"test case {cid}: request replaced (not an executable curl of its own operation with $BASE_URL)")
        expected = _clean(c.get("expected"))
        if expected and (any(o is None for o in Evidence(context).ops_in(expected)) or _forbidden(expected)):
            expected = ""
        ids.add(cid)
        keys.add(key)
        cases.append({"id": cid, "label": label, "title": title, "op": op, "links": links,
                      "preconditions": _clean(c.get("preconditions")), "request": request,
                      "expected": expected or "per contract (verify)",
                      "evidence": c.get("evidence") if c.get("evidence") in ("openapi", "diff", "description") else "description",
                      "blocked_reason": _clean(c.get("blocked_reason"))})
    if not any(c["label"] == "Required" for c in cases):
        errors.append("stage 2 produced no usable Required test case")
    uncovered = []
    for u in raw.get("uncovered") if isinstance(raw.get("uncovered"), list) else []:
        if isinstance(u, dict) and str(u.get("ref") or "").upper() in s_ids | risk_ids:
            uncovered.append({"ref": str(u["ref"]).upper(), "reason": _clean(u.get("reason")) or "not covered by a generated case"})
    covered = {l for c in cases for l in c["links"] if c["label"] in ("Required", "Regression", "Conditional", "Blocked")}
    gaps = [sid for sid in sorted(s_ids, key=lambda x: int(x[1:])) if sid not in covered and sid not in {u["ref"] for u in uncovered}]
    if gaps:
        notes.append("requirements without a generated case: " + ", ".join(gaps) + " (filled from the deterministic stated-requirement scenarios)")
    missing_ops = [o for o in ops.values() if o not in {c["op"] for c in cases}]
    if missing_ops:
        notes.append("operations without a generated case: " + ", ".join(missing_ops) + " (recorded in section 11)")
    return (None if errors else {"test_cases": cases, "uncovered": uncovered, "gaps": gaps, "operations_without_cases": missing_ops}), notes, errors


def validate_sections(raw: Any, context: dict[str, Any], scope: dict[str, Any], coverage: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    notes: list[str] = []
    if not isinstance(raw, dict):
        return None, notes, ["stage 3 output is not a JSON object"]
    ev = Evidence(context)
    scope_ops = {o["op"] for o in scope.get("operations", [])}
    case_ids = {c["id"] for c in coverage.get("test_cases", [])}

    def items(key: str) -> list[str]:
        out = []
        for x in (raw.get(key) if isinstance(raw.get(key), list) else [])[:6]:
            t = _clean(x)
            named = ev.ops_in(t)
            if not t or _forbidden(t) or any(o is None or o not in scope_ops for o in named) or re.search(r"\b[1-5]\d\d\b", t):
                notes.append(f"{key}: dropped an item (empty, forbidden, a status code, or an operation outside the scope)")
                continue
            out.append(t)
        return out

    severity = []
    for s in (raw.get("severity_guidance") if isinstance(raw.get("severity_guidance"), list) else [])[:8]:
        if not isinstance(s, dict):
            continue
        ref, sev, why = str(s.get("ref") or "").strip().upper(), str(s.get("severity") or "").strip().upper(), _clean(s.get("rationale"))
        if ref in case_ids and SEVERITY_RE.match(sev) and why and not _forbidden(why) and not re.search(r"\b[1-5]\d\d\b", why) \
                and not any(o is None or o not in scope_ops for o in ev.ops_in(why)):
            severity.append({"ref": ref, "severity": sev, "rationale": why})
        else:
            notes.append("severity_guidance: dropped an item (unknown case, severity or unsupported content)")
    out = {k: items(k) for k in ("execution_notes", "evidence_notes", "regression_analysis", "blockers", "report_notes")}
    out["severity_guidance"] = severity
    errors = [] if any(out.values()) else ["stage 3 produced no usable guidance"]
    return (None if errors else out), notes, errors


def validate_scope_coverage(raw: Any, context: dict[str, Any], candidates: list[str]):
    if not isinstance(raw, dict):
        return None, [], ["two-stage output is not a JSON object"]
    scope, n1, e1 = validate_scope(raw.get("scope"), context, candidates)
    if scope is None:
        return None, n1, e1
    cov, n2, e2 = validate_coverage(raw.get("coverage"), context, scope)
    return (None if cov is None else {"scope": scope, "coverage": cov}), n1 + n2, e1 + e2


# --------------------------------------------------------------------------------------------------- assembly --

def _split_sections(text: str) -> tuple[str, dict[int, str]]:
    """(preamble up to section 2, {n: '## n. …' block}) of a prompt with the 11 numbered H2 sections."""
    parts = re.split(r"(?m)^(?=## (\d{1,2})\. )", text)
    head = parts[0]
    blocks: dict[int, str] = {}
    i = 1
    while i < len(parts):
        n = int(parts[i])
        blocks[n] = parts[i + 1] if i + 1 < len(parts) else ""
        i += 2
    return head, blocks


def _section_body(block: str) -> str:
    body = block.split("\n", 1)[1] if "\n" in block else ""
    return re.sub(r"\n---\s*\n*$", "", body.rstrip()).strip()


def _tagged(c: dict[str, str]) -> str:
    return f"- {c['text']} {TAG[c['source']]}"


def _stated_text(text: str) -> str:
    """A stated requirement as one readable line (a description table row loses its pipes)."""
    text = text.replace("\\|", "/").replace("**", "")
    return re.sub(r"\s*\|\s*", " — ", text.strip().strip("|")).strip(" —")


def _case_block(c: dict[str, Any], scope_claims: dict[str, str], stated: dict[str, str]) -> str:
    title = c["title"]
    if c["label"] in ("Required", "Regression") and "(verify)" not in title:
        title += " (verify)"
    s_links = [l for l in c["links"] if l.startswith("S")]
    if s_links:
        risk = "; ".join(f"{sid} [STATED: PR description] — {_stated_text(stated.get(sid, ''))}" for sid in s_links) + \
               " (intent only; not evidence that it is implemented)"
    elif c["links"]:
        risk = f"{', '.join(c['links'])} [UNVERIFIED] — {scope_claims.get(c['links'][0], 'risk of this change')}"
    else:
        risk = f"`{c['op']}` changed by this PR [FACT: openapi]"
    req = f"`{c['request']}`" if c["request"] else f"build it from the contract of `{c['op']}` confirmed in section 4."
    lines = [f"**{c['id']} ({c['label']}) — {title}**", f"- Requirement/Risk: {risk}"]
    pre = c["blocked_reason"] if c["label"] == "Blocked" and c["blocked_reason"] else c["preconditions"]
    if pre:
        lines.append(f"- Preconditions: {pre}{' — BLOCKED until available' if c['label'] == 'Blocked' else ''}")
    lines.append(f"- Request: {req}")
    expected = c["expected"]
    if c["label"] in ("Required", "Regression") and "(verify)" not in expected and c["evidence"] != "openapi":
        expected += " (verify)"
    lines.append(f"- Expected: {expected}")
    return "\n".join(lines)


def assemble_prompt(templates: pb.Templates, *, repository: str, pr_number: int, test_level: str, context: dict[str, Any],
                    scope: dict[str, Any], coverage: dict[str, Any], sections: dict[str, Any], mode: str,
                    notes: list[str]) -> tuple[str, str]:
    """(complete prompt, stage-3 sections Markdown). Deterministic: the reference assembler's fixed sections (role,
    discovery, execution rules, defect template, report format, final requirement) around the validated stage content."""
    base = pb.reference_fallback_prompt(templates, repository=repository, pr_number=pr_number, test_level=test_level, context=context)
    head, blocks = _split_sections(base)
    head = (head + blocks.get(1, "")).rstrip().removesuffix("---").rstrip()      # title + section 1 (role, level, budget)
    head = re.sub(r"(?m)^Note: this prompt was assembled deterministically.*$",
                  f"Note: sections 2, 3, 5, 6, 7 and the PR-specific notes in 8–11 were generated in {len(MODES[mode])} validated stages "
                  "by the prompt model and assembled with the fixed rules below. Statements are tagged `[FACT: diff]`, `[FACT: openapi]`, "
                  "`[STATED: PR description]` or `[UNVERIFIED]`; none of them is proof that the behaviour exists.", head)
    sec2_base = _section_body(blocks[2]).splitlines()
    meta_lines = [ln for ln in sec2_base if ln.startswith(("- Repository:", "- Changed files", "  -", "- `")) and "OpenAPI diff" not in ln
                  and "openapi diff]" not in ln]
    ops = scope.get("operations", [])
    s2 = ["\n".join(ln for ln in meta_lines if not ln.startswith("- `") or ln.endswith("[FACT: diff]"))]
    s2.append("- Operations in scope (each exists in the head's OpenAPI document):\n" +
              "\n".join(f"  - `{o['op']}` [FACT: openapi]" for o in ops))
    if scope.get("changed_behavior"):
        s2.append("- Changed behaviour:\n" + "\n".join("  " + _tagged(c) for c in scope["changed_behavior"]))
    if scope.get("summary"):
        s2.append(f"- Summary: {scope['summary']} [UNVERIFIED]")
    if scope.get("out_of_scope"):
        s2.append("- Out of scope: " + "; ".join(scope["out_of_scope"]) + " [UNVERIFIED]")
    sec3 = []
    for r in scope.get("requirements", []):
        sec3.append(f"**{r['id']}** — {r['header']} [STATED: PR description]\n" + "\n".join(_tagged(c) for c in r["claims"]))
    sec3.append("Unverified / to confirm during discovery:\n\n" + ("\n".join(_tagged(a) for a in scope.get("assumptions", []))
                or "- Every expected status code and response shape not documented by OpenAPI. [UNVERIFIED]") +
                ("\n" + "\n".join(f"- {r['id']}: {r['text']} [UNVERIFIED]" for r in scope.get("risks", [])) if scope.get("risks") else ""))

    risk_text = {r["id"]: r["text"] for r in scope.get("risks", [])}
    desc = str((context.get("description") or {}).get("text") or "")
    stated = {r["id"]: r["text"] for r in pb.extract_stated_requirements(desc, str((context.get("pr") or {}).get("title") or ""))}
    cases = coverage.get("test_cases", [])
    budget = pb.plan_budget(context)["max_scenarios"]
    gap_blocks: list[str] = []
    if coverage.get("gaps"):
        reqs = [r for r in pb.extract_stated_requirements(desc, str((context.get("pr") or {}).get("title") or ""))
                if r["id"] in coverage["gaps"]]
        behaviour, regression = pb.stated_scenarios(reqs)
        taken = {c["id"] for c in cases}
        for b in behaviour + regression:
            m = re.match(r"\*\*([A-Z0-9-]+-)(\d{3})", b)
            if m and b.split(" ", 1)[0].strip("*") in taken:
                b = b.replace(m.group(1), "SGAP-", 1)
            gap_blocks.append(b)
    main = [c for c in cases if c["label"] != "Regression" and not c["id"].startswith("AUTH-")]
    auth = [c for c in cases if c["id"].startswith("AUTH-")]
    reg = [c for c in cases if c["label"] == "Regression" and not c["id"].startswith("AUTH-")]
    overflow: list[dict[str, Any]] = []
    while len(main) + len(auth) + len(reg) + len(gap_blocks) + len(coverage.get("operations_without_cases", [])) > budget:
        victim = next((c for c in reversed(main) if c["label"] == "Conditional"), None) or next((c for c in reversed(main) if c["label"] != "Required"), None)
        if victim is None:
            break
        main.remove(victim)
        overflow.append(victim)
    if overflow:
        notes.append(f"{len(overflow)} lower-priority case(s) moved to section 11 to respect the scenario budget ({budget})")

    sec5_intro = _section_body(blocks[5]).split("\n\n", 1)[0]
    groups: list[str] = []
    for o in ops:
        bl = [_case_block(c, risk_text, stated) for c in main if c["op"] == o["op"]]
        if bl:
            groups.append(f"### `{o['op']}`\n\n" + "\n\n".join(bl))
    for n, op in enumerate(coverage.get("operations_without_cases", []), start=1):   # in scope: tested, never silently skipped
        groups.append(f"### `{op}` (no generated case)\n\n**GAP-{n:03d} (Conditional) — Documented happy path of `{op}`**\n"
                      f"- Requirement/Risk: `{op}` is in this PR's scope [FACT: openapi]\n"
                      f"- Request: build it from the contract of `{op}` confirmed in section 4.\n"
                      "- Expected: the documented success response (verify); never an unhandled 5xx.")
    if gap_blocks:
        groups.append("### Stated requirements without a generated case (deterministic, intent only — verify)\n\n" + "\n\n".join(gap_blocks))
    sec5 = sec5_intro + "\n\n" + "\n\n".join(groups)
    sec6 = "\n\n".join(_case_block(c, risk_text, stated) for c in auth) if auth else _section_body(blocks[6])
    sec7 = ("\n\n".join(_case_block(c, risk_text, stated) for c in reg) if reg else _section_body(blocks[7]))
    if sections.get("regression_analysis"):
        sec7 += "\n\nRegression analysis for this PR:\n" + "\n".join(f"- {t}" for t in sections["regression_analysis"])
    sec7 += "\n\nKeep regression within the changed operations; do not expand into unrelated modules or full-system testing."

    staged_md: list[str] = []
    sec8 = _section_body(blocks[8])
    if sections.get("execution_notes"):
        part = "### 8.3 PR-specific execution notes\n\n" + "\n".join(f"- {t}" for t in sections["execution_notes"])
        sec8 += "\n\n" + part
        staged_md.append(part)
    if sections.get("evidence_notes"):
        part = "### 8.4 Evidence to capture for this PR\n\n" + "\n".join(f"- {t}" for t in sections["evidence_notes"])
        sec8 += "\n\n" + part
        staged_md.append(part)
    sec9 = _section_body(blocks[9])
    if sections.get("severity_guidance"):
        part = "### PR-specific severity guidance (suggestions; the observed impact decides)\n\n" + "\n".join(
            f"- `{s['ref']}` — {s['severity']} if it fails: {s['rationale']}" for s in sections["severity_guidance"])
        sec9 += "\n\n" + part
        staged_md.append(part)
    sec10 = _section_body(blocks[10])
    if sections.get("report_notes"):
        part = "### Report notes for this PR\n\n" + "\n".join(f"- {t}" for t in sections["report_notes"])
        sec10 += "\n\n" + part
        staged_md.append(part)
    sec11 = _section_body(blocks[11]).split("\n\n**Final requirement:**")[0].rstrip()
    extra11 = []
    for u in coverage.get("uncovered", []):
        extra11.append(f"- BLOCKED / not covered: {u['ref']} — {u['reason']}")
    for c in cases:
        if c["label"] == "Blocked":
            extra11.append(f"- BLOCKED: `{c['id']}` — {c['blocked_reason'] or 'missing fixture, role or credential'}")
    for c in overflow:
        extra11.append(f"- Conditional (over the scenario budget, run only if time allows): `{c['id']}` — {c['title']} on `{c['op']}`")
    for o in coverage.get("operations_without_cases", []):
        extra11.append(f"- `{o}` is in scope but the generated plan has no case for it: only the Conditional happy path in section 5 covers it.")
    for t in sections.get("blockers", []):
        extra11.append(f"- {t}")
    if extra11:
        sec11 += "\n\nFrom the staged analysis of this PR:\n\n" + "\n".join(extra11)
        staged_md.append("### Blockers (section 11)\n\n" + "\n".join(f"- {t}" for t in sections.get("blockers", [])))
    final_req = base[base.rindex(pb.FINAL_REQUIREMENT_MARKER):].strip()
    out = [head, "", "---", "", "## 2. Verified PR Summary and Scope", "", "\n".join(s2), "", "---", "",
           "## 3. Requirements and Evidence", "", "\n\n".join(sec3), "", "---", "", blocks[4].rstrip(), "",
           "## 5. Prioritized Test Scenarios", "", sec5, "", "---", "", "## 6. Authorization and Security Checks", "", sec6, "", "---", "",
           "## 7. Regression Coverage", "", sec7, "", "---", "", "## 8. Execution and Evidence Rules", "", sec8, "", "---", "",
           "## 9. Defect Reporting", "", sec9, "", "---", "", "## 10. Final QA Report", "", sec10, "", "---", "",
           "## 11. Blockers, Assumptions and Ambiguities", "", sec11, "", final_req, ""]
    return "\n".join(out), "\n\n".join(staged_md) + "\n"


# --------------------------------------------------------------------------------------------- the state machine --

def classify_result(generation: dict[str, Any] | None) -> dict[str, Any]:
    """What one provider call returned, as n8n reports it (generated_text, finish_reason, llm_error, http_status)."""
    g = generation or {}
    status = g.get("http_status")
    status = int(status) if isinstance(status, (int, str)) and re.fullmatch(r"[1-5]\d\d", str(status)) else None
    text = g.get("generated_text")
    if status is not None and status >= 300:
        kind = "rate_limited" if status == 429 else ("transient" if status in RETRYABLE_STATUS else "rejected")
        return {"ok": False, "kind": kind, "http_status": status}
    if g.get("llm_error"):
        return {"ok": False, "kind": "transient", "http_status": status}
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "kind": "empty", "http_status": status}
    if pb.is_truncated(g.get("finish_reason")):
        return {"ok": False, "kind": "truncated", "http_status": status}
    return {"ok": True, "kind": "ok", "http_status": status, "text": text}


class Pipeline:
    """Staged generation for one PR head, persisted in the run directory (resumable, idempotent per stage).

    capacity(tokens) → Ledger.capacity-like dict ({fits, hard, retry_after_s, kind, reason, ...}); None = no ledger check."""

    def __init__(self, run_dir: Path, *, prompts_dir: Path, context: dict[str, Any], openapi: dict[str, Any] | None,
                 repository: str, pr_number: int, head_sha: str, test_level: str, model: str, provider: str,
                 cfg: StageConfig, capacity: Callable[[int], dict[str, Any] | None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.run_dir, self.prompts_dir = Path(run_dir), Path(prompts_dir)
        self.context, self.openapi = context, openapi
        self.repository, self.pr_number, self.head_sha = repository, pr_number, head_sha
        self.test_level, self.model, self.provider, self.cfg = test_level, model, provider, cfg
        self.capacity, self.clock = capacity, clock
        self.templates = pb.Templates.load(self.prompts_dir)
        self.candidates = candidate_operations(context)

    # -- persistence
    def _path(self, name: str) -> Path:
        return self.run_dir / name

    def _write(self, name: str, data: Any) -> None:
        p = self._path(name)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n" if not isinstance(data, str) else data, encoding="utf-8")
        tmp.replace(p)

    def _read(self, name: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def meta(self) -> dict[str, Any]:
        return self._read(METADATA) or {}

    def _save_meta(self, meta: dict[str, Any]) -> None:
        meta["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))
        self._write(METADATA, meta)

    # -- requests
    def _system(self, stage: str) -> str:
        text = (self.prompts_dir / STAGE_PROMPTS[stage]).read_text(encoding="utf-8")
        if stage == "scope_coverage":
            body = lambda s: (self.prompts_dir / STAGE_PROMPTS[s]).read_text(encoding="utf-8").split("\n", 2)[2]
            text = text.replace("{{STAGE1}}", body("scope")).replace("{{STAGE2}}", body("coverage"))
        return text

    def _request(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"model": self.model, "stream": False, "temperature": 0.2, "max_completion_tokens": self.cfg.completion[stage],
                "messages": [{"role": "system", "content": self._system(stage)},
                             {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}]}

    def _scope_request(self, stage: str) -> tuple[dict[str, Any], bool]:
        """Stage 1 (or the two-stage first request) with the diff excerpt sized to the per-stage target."""
        diff_chars = 12000
        while True:
            payload, truncated = scope_payload(self.context, repository=self.repository, pr_number=self.pr_number,
                                               diff_chars=diff_chars, candidates=self.candidates)
            if stage == "scope_coverage":
                payload["contracts"] = [contract_excerpt(op, self.context, self.openapi) for op in self.candidates]
                payload["scenario_budget"] = min(pb.plan_budget(self.context)["max_scenarios"], self.cfg.max_cases)
                payload["description_only_identifiers"] = description_only_identifiers(self.context)
            req = self._request(stage, payload)
            if estimate_request(req, self.cfg)["total_tokens"] <= self.cfg.target_tokens or diff_chars <= 600:
                return req, truncated
            diff_chars = int(diff_chars * 0.7)

    def build(self, stage: str, meta: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """(request, extra info) for a stage from the validated earlier artifacts."""
        if stage in ("scope", "scope_coverage"):
            req, truncated = self._scope_request(stage)
            return req, {"diff_truncated": truncated}
        if stage == "coverage":
            scope = self.output("scope")
            return self._request("coverage", coverage_payload(self.context, scope, self.openapi, self.cfg)), {}
        scope, coverage = self.scope_and_coverage()
        return self._request("sections", sections_payload(scope, coverage)), {}

    def fingerprint(self, stage: str, request: dict[str, Any], mode: str) -> str:
        return _sha({"generator": GENERATOR_VERSION, "stage": stage, "mode": mode, "model": self.model,
                     "head_sha": self.head_sha, "request": _sha(request)})

    def output(self, stage: str) -> dict[str, Any]:
        art = self._read(ARTIFACTS[stage]) or {}
        return art.get("output") or {}

    def scope_and_coverage(self) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.meta().get("mode") == "2":
            both = self.output("scope_coverage")
            return both.get("scope") or {}, both.get("coverage") or {}
        return self.output("scope"), self.output("coverage")

    def _valid_artifact(self, stage: str, fp: str) -> bool:
        art = self._read(ARTIFACTS[stage])
        return bool(art) and art.get("status") == "valid" and art.get("fingerprint") == fp and art.get("head_sha") == self.head_sha \
            and art.get("generator_version") == GENERATOR_VERSION

    # -- the public steps
    def choose_mode(self) -> tuple[str, str]:
        if self.cfg.mode == "2":
            if len(self.candidates) > self.cfg.max_operations:
                return "3", f"two-stage refused: {len(self.candidates)} candidate operations exceed the coverage capacity {self.cfg.max_operations}"
            req, truncated = self._scope_request("scope_coverage")
            est = estimate_request(req, self.cfg)["total_tokens"]
            if est > self.cfg.target_tokens or truncated:
                return "3", f"two-stage refused: its request needs ~{est} tokens (target {self.cfg.target_tokens})" + \
                            (" and would truncate the diff" if truncated else "")
            return "2", f"two-stage: the combined request fits (~{est} tokens)"
        return "3", "three-stage (default)"

    def start(self) -> dict[str, Any]:
        """Begin (or resume) generation for this head; returns the next action."""
        old = self.meta()
        if old.get("head_sha") == self.head_sha and old.get("generator_version") == GENERATOR_VERSION and old.get("status") in ("running", "assembled"):
            return self.next_action()
        mode, why = self.choose_mode()
        stages = MODES[mode]
        meta = {"generator_version": GENERATOR_VERSION, "repository": self.repository, "pr_number": self.pr_number,
                "head_sha": self.head_sha, "model": self.model, "provider": self.provider, "mode": mode, "mode_reason": why,
                "stage_order": list(stages), "status": "running", "target_tokens_per_stage": self.cfg.target_tokens,
                "max_retries_per_stage": self.cfg.max_retries, "candidate_operations": len(self.candidates),
                "stages": {s: {"status": "pending", "attempts": 0, "cached": False, "retries": [], "notes": []} for s in stages},
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))}
        if len(self.candidates) > self.cfg.max_operations and mode == "3":
            meta.update(status="failed", failure=f"CONTEXT_LIMIT: {len(self.candidates)} candidate operations exceed what one coverage "
                                                 f"request can represent ({self.cfg.max_operations}); staged generation not attempted")
            self._save_meta(meta)
            return self._failed(meta)
        self._save_meta(meta)
        return self.next_action()

    def _failed(self, meta: dict[str, Any]) -> dict[str, Any]:
        return {"action": "failed", "reason": meta.get("failure"), "http_status": meta.get("failure_http_status")}

    def next_action(self) -> dict[str, Any]:
        meta = self.meta()
        if meta.get("status") == "failed":
            return self._failed(meta)
        if meta.get("status") == "assembled":
            return {"action": "done", "prompt": ASSEMBLED, "sha256": meta.get("assembled_sha256")}
        for stage in meta["stage_order"]:
            st = meta["stages"][stage]
            if st["status"] == "valid":
                continue
            request, info = self.build(stage, meta)
            fp = self.fingerprint(stage, request, meta["mode"])
            if self._valid_artifact(stage, fp):                         # resume: same head, inputs, generator, config
                st.update(status="valid", cached=True, fingerprint=fp)
                self._save_meta(meta)
                continue
            est = estimate_request(request, self.cfg)
            st.update(fingerprint=fp, estimate=est, **info)
            if est["total_tokens"] > self.cfg.target_tokens:
                meta.update(status="failed", failure=f"CONTEXT_LIMIT: stage {stage} needs ~{est['total_tokens']} tokens "
                                                     f"(per-stage target {self.cfg.target_tokens}); the scope cannot be represented reliably")
                self._save_meta(meta)
                return self._failed(meta)
            wait, cap = self._wait_for(est["total_tokens"], st)
            if wait is None:
                meta.update(status="failed", failure=f"BUDGET_{(cap or {}).get('kind') or 'UNAVAILABLE'}: {(cap or {}).get('reason') or 'no capacity'}"
                                                     " — not sent; raise the limit or wait for the daily/job budget to renew")
                self._save_meta(meta)
                return self._failed(meta)
            st.update(status="requested", requested_wait_s=wait, capacity=cap)
            meta["current"] = stage
            self._save_meta(meta)
            return {"action": "call", "stage": stage, "attempt": st["attempts"] + 1, "llm_request": request, "wait_s": wait,
                    "estimate": est}
        return self._assemble(meta)

    def _wait_for(self, tokens: int, st: dict[str, Any], minimum: float = 0.0) -> tuple[float | None, dict[str, Any] | None]:
        cap = self.capacity(tokens) if self.capacity else None
        if cap is None:
            return minimum, None
        if cap.get("fits"):
            return minimum, cap
        if cap.get("hard") or cap.get("retry_after_s") is None:
            return None, cap
        wait = max(minimum, float(cap["retry_after_s"]))
        return (wait if wait <= self.cfg.max_wait_s else None), cap

    def record(self, generation: dict[str, Any] | None, usage: dict[str, Any] | None = None) -> dict[str, Any]:
        """Consume the provider result of the requested stage; returns the next action."""
        meta = self.meta()
        stage = meta.get("current")
        if meta.get("status") != "running" or stage not in meta.get("stages", {}) or meta["stages"][stage]["status"] != "requested":
            return self.next_action()
        st = meta["stages"][stage]
        st["attempts"] += 1
        if isinstance(usage, dict):
            st["usage"] = {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens") if isinstance(usage.get(k), int)}
        res = classify_result(generation)
        reason = None
        if res["ok"]:
            raw = parse_json_object(res["text"])
            validated, notes, errors = self._validate(stage, raw, meta)
            st["notes"] = notes[:40]
            if validated is not None:
                art = {"stage": stage, "status": "valid", "fingerprint": st["fingerprint"], "head_sha": self.head_sha,
                       "generator_version": GENERATOR_VERSION, "model": self.model, "repository": self.repository,
                       "pr_number": self.pr_number, "output": validated, "notes": notes, "usage": st.get("usage"),
                       "estimate": st.get("estimate"), "attempts": st["attempts"],
                       "raw_output": res["text"][:RAW_KEEP_CHARS]}            # what the model said, for debugging dropped items
                self._write(ARTIFACTS[stage], art)
                st.update(status="valid")
                meta.pop("current", None)
                self._save_meta(meta)
                return self.next_action()
            reason = "invalid output: " + "; ".join(errors)[:300]
            self._write(ARTIFACTS[stage], {"stage": stage, "status": "invalid", "head_sha": self.head_sha, "errors": errors,
                                           "notes": notes, "raw_output": (res["text"] or "")[:RAW_KEEP_CHARS]})
            retryable = True
        else:
            reason = f"{res['kind']}" + (f" (HTTP {res['http_status']})" if res.get("http_status") else "")
            retryable = res["kind"] in ("rate_limited", "transient", "empty", "truncated")
            if res["kind"] == "truncated":
                reason += " — the completion cap was reached; partial output is never used"
        st["retries"].append({"attempt": st["attempts"], "cause": reason})
        if retryable and st["attempts"] <= self.cfg.max_retries:
            minimum = DEFAULT_RETRY_WAIT_S if res.get("kind") == "rate_limited" else 0.0
            wait, cap = self._wait_for(st["estimate"]["total_tokens"], st, minimum)
            if wait is not None:
                st.update(status="requested", requested_wait_s=wait, capacity=cap)
                self._save_meta(meta)
                request, _ = self.build(stage, meta)
                return {"action": "call", "stage": stage, "attempt": st["attempts"] + 1, "llm_request": request, "wait_s": wait,
                        "estimate": st["estimate"], "retry_of": reason}
            reason += f"; no capacity for a retry ({(cap or {}).get('reason')})"
        st["status"] = "failed"
        meta.update(status="failed", failure=f"stage {stage} failed after {st['attempts']} attempt(s): {reason}",
                    failure_http_status=res.get("http_status"))
        self._save_meta(meta)
        return self._failed(meta)

    def _validate(self, stage: str, raw: Any, meta: dict[str, Any]):
        if stage == "scope":
            return validate_scope(raw, self.context, self.candidates)
        if stage == "coverage":
            return validate_coverage(raw, self.context, self.output("scope"))
        if stage == "scope_coverage":
            return validate_scope_coverage(raw, self.context, self.candidates)
        scope, coverage = self.scope_and_coverage()
        return validate_sections(raw, self.context, scope, coverage)

    def _assemble(self, meta: dict[str, Any]) -> dict[str, Any]:
        scope, coverage = self.scope_and_coverage()
        sections = self.output("sections")
        notes: list[str] = []
        text, sections_md = assemble_prompt(self.templates, repository=self.repository, pr_number=self.pr_number,
                                            test_level=self.test_level, context=self.context, scope=scope, coverage=coverage,
                                            sections=sections, mode=meta["mode"], notes=notes)
        self._write(SECTIONS_MD, sections_md)
        self._write(ASSEMBLED, text)
        req_ids = [r["id"] for r in scope.get("requirements", [])]
        meta.update(status="assembled", assembled_sha256=pb.sha256(text), assembled_chars=len(text), assembly_notes=notes,
                    requirements=req_ids, test_cases=len(coverage.get("test_cases", [])),
                    unresolved={"uncovered": coverage.get("uncovered", []), "gaps": coverage.get("gaps", []),
                                "operations_without_cases": coverage.get("operations_without_cases", []),
                                "assumptions": [a["text"] for a in scope.get("assumptions", [])]})
        meta.pop("current", None)
        self._save_meta(meta)
        return {"action": "done", "prompt": ASSEMBLED, "sha256": meta["assembled_sha256"]}

    def assembled_text(self) -> str | None:
        meta = self.meta()
        if meta.get("status") != "assembled" or meta.get("head_sha") != self.head_sha:
            return None
        try:
            text = self._path(ASSEMBLED).read_text(encoding="utf-8")
        except OSError:
            return None
        return text if pb.sha256(text) == meta.get("assembled_sha256") else None
