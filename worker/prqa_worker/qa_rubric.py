"""Deterministic scoring rubric for generated PRQA QA prompts (production module; moved verbatim from
tests/qa_rubric.py, which now aliases this module so the worker, evaluators and benchmark share one definition).

Scores a *finalized* prompt (banner + body + result contract) on 10 dimensions, each
0-5 with explicit criteria, plus deterministic hard-fail rules and explicit
human-review flags for the checks automation cannot reliably decide.

The rubric reuses the prompt builder's own evidence helpers (Evidence, scenario
parsing, requirement extraction) and the quality-gate result, so a rubric score is a
structured summary of what the gate saw plus measures the gate does not express
(proportionality, executability, ambiguity handling, safety isolation).

quality_score = sum(scores) / (5 * 10) * 100           (threshold: 85)

Hard-fail conditions (override the score; deterministic where automation is reliable):
  HF-FABRICATED-FACT  — an endpoint/parameter/identifier/limit/status presented as
                        fact by an [FACT: diff|openapi] tag that the trusted evidence
                        does not support (gate error).
  HF-SCOPE-VIOLATION  — a Required/Regression scenario names an endpoint that is in
                        neither the diff/OpenAPI evidence nor the PR description and
                        the scenario carries no verify marker (gate error; intentional
                        out-of-scope expansion, not a hypothesis).
  HF-INJECTION        — a forbidden instruction phrase appears outside a quoted
                        [STATED] block (gate error).
  HF-SECRETS          — a secret-shaped token appears in the prompt (gate error).
  HF-MISSING-CRITICAL — stated requirements coverage error: <40% of >=3 stated
                        requirements find a covering scenario.
  HF-UNUSABLE         — the gate verdict is invalid (output cannot serve the flow).

Human-review flags (documented limits of keyword automation — never hard-fails):
  HR-SEMANTIC-REQUIREMENT   — semantic coverage by paraphrase cannot be keyword-locked;
                              gate partial/missing may be a false negative.
  HR-AMBIGUITY-SENSIBLE     — whether a Conditional scenario is semantically sensible
                              is model self-review + human review.
  HR-EXTRA-SECTION-CONTEXT  — extra sections are accepted; review whether they add
                              context or dilute the shared structure.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from . import prompt_builder as pb
from .prompt_builder import (
    OPERATION_RE, check_structure, extract_stated_requirements,
    parse_scenarios, validate_generated_prompt,
)

THRESHOLD = 85.0
DIMENSIONS = ["scope_accuracy", "business_rules", "test_coverage", "executability",
              "expected_results", "context_grounding", "ambiguity_handling",
              "pr_specificity", "output_structure", "safety_isolation"]
VERIFY_RE = re.compile(r"verify|ambiguous|unverified|blocked", re.I)
CONTRACT_MARKER = "Machine-Readable Output"
FINAL_REQ_PREFIX = "**Final requirement:**"
_STATED_TAG = "[stated"
POSITIVE_HINTS = ("success", "succeed", "happy path", "documented", "create", "persists")
NEGATIVE_HINTS = ("invalid", "missing", "empty", "not_a", "unknown", "malformed", "wrong",
                  "over-length", "boundary", "validation", "error")
BOUNDARY_HINTS = ("boundary", "over-length", "out-of-range", "min_length", "max_length",
                  "exactly", "at least", "at most", "limit", "empty and")
BUILDER_HINT_RE = re.compile(r"from the discovered contract|identified in section 4 for this behaviour|"
                             r"documented by the contract|identified from the contract", re.I)


# One parser and matcher for gate and rubric (see prompt_builder.curl_operations / known_operation).
curl_operations = pb.curl_operations
_known_operation = pb.known_operation


def _hints(text: str, needles: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(n in low for n in needles)


def _path_set(text: str) -> set[str]:
    return {pb._norm_path(m.group(2)) for m in OPERATION_RE.finditer(text)}


def block_bodies_scoped(text: str, blocks: list[dict[str, Any]]) -> None:
    """Fold each scenario's preceding `### Area — op` heading into its body so that
    method/path claims named on the area heading count for the block they describe."""
    lines = text.splitlines()
    for b in blocks:
        if b["start"] > 0:
            prev = lines[b["start"] - 1]
            if prev.lstrip().startswith("###"):
                b["exec_text"] = prev + "\n" + b["body"]
                continue
        b["exec_text"] = b["body"]


@dataclass
class Dimension:
    score: int
    evidence: list[str]


@dataclass
class RubricResult:
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    hard_fails: list[str] = field(default_factory=list)
    human_flags: list[str] = field(default_factory=list)
    quality_score: float = 0.0
    coverage: dict[str, Any] = field(default_factory=dict)
    gate: dict[str, Any] = field(default_factory=dict)

    def score(self, dim: str) -> int:
        return self.dimensions[dim].score if dim in self.dimensions else 0


def score_prompt(text: str, *, pr_title: str, context: dict[str, Any] | None,
                 gate: dict[str, Any] | None = None) -> RubricResult:
    """Score a finalized prompt with the 10-dimension rubric. `gate` may carry an
    existing validate_generated_prompt result; computed here when absent."""
    res = RubricResult()
    if gate is None:
        gate = validate_generated_prompt(text, pr_title=pr_title, context=context)
    res.gate = gate
    res.coverage = dict(gate.get("coverage") or {})
    gate_errors, gate_warnings = list(gate.get("errors") or []), list(gate.get("warnings") or [])
    context = context or {}
    ev = pb.Evidence.from_context(context)
    desc = str((context.get("description") or {}).get("text") or "")
    desc_paths = _path_set(desc)
    stated = extract_stated_requirements(desc, pr_title)
    blocks = parse_scenarios(text)
    block_bodies_scoped(text, blocks)
    lowered = text.lower()

    lint_keys = ("unsupported [FACT] claim", "unsupported claim in a Required/Regression scenario",
                 "[STATED] claim not found in the PR description", "reference-template contamination",
                 "unsupported speculation", "unsupported database assertion",
                 "unsupported claim in an untagged summary/requirement line", "executable request outside a scenario")
    lint_errs = [e for e in gate_errors if any(k in e for k in lint_keys)]
    lint_warns = [w for w in gate_warnings if "unverified claim" in w
                  or "is conditional and has no evidence" in w
                  or ("not established by the PR evidence" in w)]

    # ================= scope accuracy =================
    verify_blocks = {b["id"] for b in blocks if VERIFY_RE.search(b["text_lower"])}
    violations: list[str] = []
    for b in blocks:
        if b["id"] in verify_blocks or b["label"] == "Blocked":
            continue
        for m in OPERATION_RE.finditer(b["exec_text"]):
            method, path, _ = m.group(1).upper(), m.group(2), m.group(3) or ""
            if not ev.supports_path(path) and not pb.match_path(path, desc_paths):
                violations.append(f"{b['id']}: {method} {pb._norm_path(path)}")
    if lint_errs:
        scope = 2 if any("[FACT] claim" in e for e in lint_errs) else max(1, 4 - len(lint_errs))
    elif violations:
        scope = max(1, 4 - min(3, len(violations)))
    else:
        scope = 5
    res.dimensions["scope_accuracy"] = Dimension(scope, violations[:3] or lint_errs[:2] or
        ["every endpoint named by scenarios comes from the diff/OpenAPI evidence, the [STATED] description, or contract-derivation"][:1])

    # ================= business rules =================
    cov = res.coverage
    total = len(cov.get("covered", [])) + len(cov.get("partial", [])) + len(cov.get("missing", []))
    if not stated:
        b_score, b_ev = 5, ["no stated requirements in the description — current/expected distinction carried by the contract-based scenarios"]
    elif total == 0:
        b_score, b_ev = 2, ["stated requirements extracted, but no scenario maps them (coverage list empty)"]
    else:
        ratio = len(cov.get("covered", [])) / total
        b_score = 5 if ratio == 1.0 else 4 if ratio >= 0.8 else 3 if ratio >= 0.6 else 2 if ratio >= 0.4 else 1
        b_ev = [f"gate coverage: {len(cov.get('covered', []))}/{total} stated requirements mapped to distinct scenarios"]
    if _STATED_TAG in lowered and stated:
        b_score = max(b_score, 3 if total else b_score)
        b_ev.append("claimed behaviour is tagged [STATED: PR description] — separated from facts, intent only")
    res.dimensions["business_rules"] = Dimension(b_score, b_ev)

    # ================= test coverage =================
    pos = sum(1 for b in blocks if b["label"] in ("Required",) and _hints(b["text_lower"], POSITIVE_HINTS))
    neg = sum(1 for b in blocks if b["label"] != "Regression" and _hints(b["text_lower"], NEGATIVE_HINTS))
    reg = sum(1 for b in blocks if b["label"] == "Regression")
    auth = sum(1 for b in blocks if b["id"].upper().startswith("AUTH") or _hints(b["text_lower"], ("unauthenticated", "malformed", "expired token", "401")))
    boundary = sum(1 for b in blocks if _hints(b["text_lower"], BOUNDARY_HINTS))
    axes = [("positive", pos > 0), ("negative/validation", neg > 0), ("regression", reg > 0), ("authorization", auth > 0)]
    cv = sum(1 for _, ok in axes if ok)
    cv_score = (5 if cv == 4 and (boundary > 0 or neg >= 2) else min(5, max(1, cv * 1.5)))
    res.dimensions["test_coverage"] = Dimension(cv_score, [f"{k}: {'yes' if ok else 'MISSING'} ({n} block(s))" for (k, ok), n
                    in zip(axes, [pos, neg, reg, auth])] + ([f"boundary/parameterized cases: {boundary} block(s)"] if boundary else ["no explicit boundary cases"]))

    # ================= executability =================
    placeholders = all(p in lowered for p in ("curl", "$base_url", "$token"))
    block_bodies_scoped(text, blocks)
    # A curl request counts as a concrete operation only when its method + path match an operation the
    # diff/OpenAPI evidence or the description names; invented or method-mismatched endpoints earn nothing.
    known_paths = set(ev.paths) | desc_paths
    known_ops = set(ev.operations) | set(ev.documented_ops or ()) | {f"{m.group(1).upper()} {pb._norm_path(m.group(2))}" for m in OPERATION_RE.finditer(desc)}
    exec_blocks = [b["id"] for b in blocks if OPERATION_RE.search(b["exec_text"]) or BUILDER_HINT_RE.search(b["exec_text"])
                   or any(_known_operation(m, p, known_paths, known_ops, bool(ev.documented_ops)) for m, p in curl_operations(b["exec_text"]))]
    frac = (len(exec_blocks) / len(blocks)) if blocks else 0.0
    e_score = (5 if placeholders and frac >= 0.8 else 4 if frac >= 0.6 else 3 if frac >= 0.4
               else 2 if frac > 0 else 1 if placeholders else 0)
    res.dimensions["executability"] = Dimension(e_score, [
        f"{len(exec_blocks)}/{len(blocks)} scenarios name a concrete operation (METHOD /path or an evidence-backed curl request) or derive it from the contract in section 4",
        "curl + $BASE_URL + $TOKEN placeholders present" if placeholders else "execution placeholders incomplete"])

    # ================= expected results =================
    with_expected = [b for b in blocks if pb.EXPECTED_LINE_RE.search(b["body"])]
    grounded = [b for b in with_expected if any(m in b["text_lower"] for m in ("(verify", "[fact", "per the contract", "per contract"))]
    frac2 = (len(with_expected) / len(blocks)) if blocks else 0.0
    g_frac = (len(grounded) / len(with_expected)) if with_expected else 1.0
    ex_score = (5 if frac2 == 1.0 and g_frac >= 0.9 else 4 if frac2 >= 0.9 else 3 if frac2 >= 0.75
                else 2 if frac2 >= 0.5 else 1 if frac2 > 0 else 0)
    res.dimensions["expected_results"] = Dimension(ex_score, [
        f"Expected: present in {len(with_expected)}/{len(blocks)} scenarios",
        f"{len(grounded)}/{max(1, len(with_expected))} expected lines are evidence-grounded or 'per contract (verify)'"])

    # ================= context grounding =================
    n_err, n_warn = len(lint_errs), len(lint_warns)
    gr = (5 if n_err == 0 and n_warn == 0 else 4 if n_err == 0 and n_warn <= 2 else 3 if n_err == 0
          else 2 if n_err == 1 else 1 if n_err <= 3 else 0)
    res.dimensions["context_grounding"] = Dimension(gr, lint_errs[:2] or
        [f"0 unsupported-as-fact claims; {n_warn} unverified-claim warning(s) routed to 'verify' markers"])

    # ================= ambiguity handling =================
    m11 = re.search(r"## 11\. Blockers, Assumptions and Ambiguities\n(.*)", text, re.S)
    sec11 = m11.group(1) if m11 else ""
    sec11_ok = len(re.sub(r"[\s\-*#|>_`]", "", sec11)) >= 150
    n_verify = sum(1 for b in blocks if VERIFY_RE.search(b["text_lower"]))
    has_blocked = any(b["label"] == "Blocked" for b in blocks) or "blocked" in (sec11 + ("".join(b["text_lower"] for b in blocks))).lower()
    a_score = (5 if sec11_ok and n_verify >= max(2, len(blocks) // 2) else 4 if sec11_ok and n_verify >= 2
               else 3 if (n_verify >= 2 or sec11_ok) else 2 if sec11_ok or n_verify else 1)
    res.dimensions["ambiguity_handling"] = Dimension(a_score, [
        f"section 11 populated: {bool(sec11_ok)} — unresolved expectations and missing fixtures recorded there",
        f"{n_verify}/{len(blocks)} scenarios marked verify/ambiguous/unverified" + (f"; {len([b for b in blocks if b['label'] == 'Blocked'])} BLOCKED" if has_blocked else "")])

    # ================= PR specificity =================
    title_in = bool(pr_title and pr_title[:40].lower() in lowered)
    repos_in = str((context.get("pr") or {}).get("number")) and f"pr #{context.get('pr', {}).get('number')}" in lowered
    stated_ids = [r["id"] for r in stated] if stated else []
    stated_in = all(s.lower() in lowered for s in stated_ids[:3]) if stated_ids else True
    s_score = 5 if (title_in and repos_in and stated_in) else 4 if title_in and stated_in else 3 if title_in else 2
    res.dimensions["pr_specificity"] = Dimension(s_score, [
        f"title present: {title_in}; repo+PR# present: {bool(repos_in)}",
        "stated requirement IDs referenced: " + (",".join(stated_ids[:3]) if stated_ids else "n/a (no stated requirements)")])

    # ================= output structure =================
    struct = check_structure(text, blocks)
    has_contract = CONTRACT_MARKER in text
    has_final = FINAL_REQ_PREFIX in text
    o_score = (5 if not struct["errors"] and has_contract and has_final
               else 4 if not struct["errors"] and (has_contract or has_final) else 3 if not struct["errors"] else 1)
    res.dimensions["output_structure"] = Dimension(o_score,
        ["11-section template retained in order; result contract appended; final requirement present"
         if o_score >= 4 else "structure defects: " + "; ".join(struct["errors"][:2])])

    # ================= safety isolation =================
    m8 = re.search(r"## 8\. Execution and Evidence Rules\n(.{0,3000})", text, re.S)
    sec8 = m8.group(1).lower() if m8 else ""
    anchors = {"never fabricate": 0, "never print secrets": 0, "curl": 0}
    anchors_ok = sum(k in sec8 or k in lowered for k in anchors)
    quoted = "> " in text or _STATED_TAG in lowered
    safe = 5
    s_ev = []
    if anchors_ok < 2:
        safe -= 1; s_ev.append("top-level safety section misses key policy anchors")
    if not quoted:
        safe -= 1; s_ev.append("untrusted PR text not visibly quoted/tagged as data")
    res.dimensions["safety_isolation"] = Dimension(safe, s_ev or
        ["description quoted as data; no forbidden commands; no secret-shaped content; policy section intact"])

    # ================= hard fails (deterministic) =================
    if any("[FACT] claim" in e for e in gate_errors):
        res.hard_fails.append("HF-FABRICATED-FACT: unsupported [FACT:] claim (endpoint/param/identifier/limit/status asserted as fact): " + next(e for e in gate_errors if "[FACT] claim" in e)[:160])
    if lint_errs and not any("[FACT] claim" in e for e in lint_errs):
        unsupported = [e for e in lint_errs if "unsupported claim in a Required/Regression scenario" in e]
        if unsupported:
            res.hard_fails.append("HF-SCOPE-VIOLATION: " + unsupported[0][:160])
    if any("forbidden phrase" in e for e in gate_errors):
        res.hard_fails.append("HF-INJECTION: forbidden instruction phrase present outside quoted data")
    if any("secret-looking" in e for e in gate_errors):
        res.hard_fails.append("HF-SECRETS: secret-shaped content in the prompt")
    if stated and total >= 3 and len(cov.get("covered", [])) / total < 0.4:
        res.hard_fails.append(f"HF-MISSING-CRITICAL: only {len(cov.get('covered', []))}/{total} stated requirements find a covering scenario")
    if not gate.get("valid", True) and not res.hard_fails:
        res.hard_fails.append("HF-UNUSABLE: gate rejected the output (" + (gate.get("errors") or ["?"])[0][:140] + ")")
    res.human_flags.append("HR-SEMANTIC-REQUIREMENT: paraphrase coverage cannot be keyword-locked; gate partial/missing may be a false negative")
    res.human_flags.append("HR-AMBIGUITY-SENSIBLE: whether Conditional scenarios are semantically sensible is model self-review + human review")
    if struct.get("warnings"):
        res.human_flags.append("HR-EXTRA-SECTION-CONTEXT: extra sections accepted — review content impact")

    res.quality_score = round(sum(res.score(d) for d in DIMENSIONS) / (5 * len(DIMENSIONS)) * 100, 1)
    return res


# ---------------------------------------------------------------- acceptance policy (worker + evaluators) ---
# Failure categories for a complete candidate the quality gate rejects (generation.failure); all use the fallback.
QUALITY_GATE_REJECTED = "gate_rejected"          # deterministic validator errors
QUALITY_HARD_FAIL = "rubric_hard_fail"           # validator passed, but a rubric hard-fail rule fired
QUALITY_BELOW_THRESHOLD = "rubric_threshold"     # validator passed, score below THRESHOLD (or not a finite number)
QUALITY_UNAVAILABLE = "rubric_unavailable"       # scoring raised or returned a malformed result: fail closed


def score_passes(score: Any) -> bool:
    """True only for a real, finite number >= THRESHOLD (bools, strings, None, NaN and infinities never pass)."""
    return (isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score)
            and score >= THRESHOLD)


def accept_result(result: Any, gate: dict[str, Any]) -> dict[str, Any]:
    """The acceptance rule, applied to an already computed RubricResult (or to anything else, which fails closed).

    Accepted only when the deterministic gate is valid with no errors, the rubric reports no hard fail, and the
    rubric score is a finite number >= THRESHOLD. A high score never overrides a gate error or a hard fail, and a
    valid gate never overrides a low score. Returns only identifiers and numbers, never prompt text."""
    gate_ok = bool(gate.get("valid")) and not gate.get("errors")
    out: dict[str, Any] = {"accepted": False, "failure": None, "score": None, "threshold": THRESHOLD,
                           "hard_fails": [], "gate_valid": gate_ok}
    try:
        score, hard = result.quality_score, result.hard_fails
        if not isinstance(hard, list) or not all(isinstance(h, str) for h in hard):
            raise TypeError("malformed hard_fails")
    except Exception:  # noqa: BLE001 — a malformed result must reject, never accept
        out["failure"] = QUALITY_GATE_REJECTED if not gate_ok else QUALITY_UNAVAILABLE
        return out
    finite = isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score)
    out["score"] = round(float(score), 2) if finite else None   # NaN/inf/non-numeric recorded as None (and fail)
    out["hard_fails"] = [h.split(":", 1)[0] for h in hard]
    if not gate_ok:
        out["failure"] = QUALITY_GATE_REJECTED
    elif hard:
        out["failure"] = QUALITY_HARD_FAIL
    elif not score_passes(score):
        out["failure"] = QUALITY_BELOW_THRESHOLD
    else:
        out["accepted"] = True
    return out


def quality_decision(text: str, *, pr_title: str, context: dict[str, Any] | None,
                     gate: dict[str, Any], scorer: Any = None) -> dict[str, Any]:
    """Score one complete LLM candidate and apply accept_result; used by /prompts/finalize.

    Any scoring exception fails closed (rubric_unavailable, or gate_rejected when the gate already failed).
    `scorer` exists for tests (default: score_prompt)."""
    try:
        result = (scorer or score_prompt)(text, pr_title=pr_title, context=context, gate=gate)
    except Exception:  # noqa: BLE001 — any scoring failure must reject, never accept
        result = None
    return accept_result(result, gate)
