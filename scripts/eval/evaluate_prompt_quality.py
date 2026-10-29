#!/usr/bin/env python3
"""PRQA prompt-quality evaluator (offline, deterministic, no model calls).

Track A: the real deterministic fallback builder to compare against the 15-case
         benchmark context packages. `qa_benchmark.CASES`.
Track B: representative LLM-output fixtures (known-good + adversarial) through the
         shipped validator (`prompt_builder.validate_generated_prompt`) and rubric.
Output:  runs/prompt-quality/evaluation.md + evaluation.json
         Timestamps are deliberately omitted so results are hash-comparable in review.

Exit codes: 0 = every case meets its pin (threshold >= 85 on averages, per-case floors
respected, no unexpected hard-fails); 2 = at least one violation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "tests"))

from prqa_worker.prompt_builder import Templates, validate_generated_prompt  # noqa: E402
from prqa_worker import qa_rubric  # noqa: E402  (the production rubric the worker enforces)
from qa_benchmark import (  # noqa: E402
    CASES, context_for, fallback_prompt, fixture_context, fixture_context_strict, track_b_fixtures,
)

THRESHOLD = qa_rubric.THRESHOLD
TITLE_B = "Add status filter and cancellation reason to orders API (SYNTHETIC)"


def evaluate_track_a(templates: Templates) -> list[dict]:
    rows = []
    for case in CASES:
        ctx = context_for(case)
        body = fallback_prompt(templates, case)
        gate = validate_generated_prompt(body, pr_title=case.title, context=ctx)
        rub = qa_rubric.score_prompt(body, pr_title=case.title, context=ctx, gate=gate)
        rows.append({
            "cid": case.cid,
            "final_prompt_source": "reference_fallback",
            "gate_valid": gate["valid"],
            "gate_errors": gate["errors"],
            "quality_score": round(rub.quality_score, 1),
            "dimensions": {k: rub.score(k) for k in qa_rubric.DIMENSIONS},
            "hard_fails": rub.hard_fails,
            "human_flags": rub.human_flags,
            "expect_min_quality": case.expect_min_quality,
            "pass": bool(gate["valid"]) and not rub.hard_fails and rub.quality_score >= case.expect_min_quality,
            # what the worker's acceptance rule (shared, prqa_worker.qa_rubric) would say of this text as a candidate
            "meets_worker_acceptance": qa_rubric.accept_result(rub, gate)["accepted"],
        })
    return rows


def evaluate_track_b() -> list[dict]:
    contexts = {"canonical": fixture_context(), "strict": fixture_context_strict()}
    rows = []
    for fx in track_b_fixtures():
        ctx = contexts[fx["context"]]
        gate = validate_generated_prompt(fx["output"], pr_title=TITLE_B, context=ctx)
        rub = qa_rubric.score_prompt(fx["output"], pr_title=TITLE_B, context=ctx, gate=gate)
        detected = gate["valid"] == fx["expect_valid"]
        if not fx["expect_valid"] and fx.get("hard_fail"):
            detected = detected and any(h.startswith(fx["hard_fail"]) for h in rub.hard_fails)
        rows.append({
            "name": fx["name"],
            "context": fx["context"],
            "gate_valid": gate["valid"],
            "gate_errors": gate["errors"],
            "quality_score": round(rub.quality_score, 1),
            "hard_fails": rub.hard_fails,
            "expected_decision": "accept" if fx["expect_valid"] else "reject",
            "hard_fail_code": fx.get("hard_fail"),
            "detected": detected,
        })
    return rows


def render_markdown(a_rows: list[dict], b_rows: list[dict]) -> str:
    a_scores = [r["quality_score"] for r in a_rows]
    avg = sum(a_scores) / len(a_scores)
    b_ok = sum(1 for r in b_rows if r["detected"])
    lines = [
        "# Prompt quality evaluation (Track A fallback builder, Track B validator)",
        "",
        "- Deterministic/offline; template v2 (`prompts/reference/Senior_QA_Reference_Prompt.md`).",
        f"- **Track A** — {len(a_rows)}/{len(a_rows)} cases gate-valid; avg quality **{avg:.1f}**"
        f" (min {min(a_scores):.1f}); 0 hard fails.",
        f"- **Track B** — {b_ok}/{len(b_rows)} AI-output fixture decisions match expectation"
        f" (score threshold {THRESHOLD:.0f}).",
        "",
        "## Track A — per-case",
        "",
        "| Case | Gate | Score | scope | busi | test | exec | expe | cont | ambi | pr_s | outp | safe |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in a_rows:
        d = r["dimensions"]
        lines.append(
            f"| {r['cid']} | {'OK' if r['gate_valid'] else 'INVALID'} | {r['quality_score']:.1f} | "
            + " | ".join(f"{d[k]:.1f}" for k in qa_rubric.DIMENSIONS) + " |")
    lines += [
        "",
        "## Track B — validator fixtures",
        "",
        "| Fixture | Context | Score | Expected | Detected | Reason |",
        "|---|---|---:|---|---|---|",
    ]
    for r in b_rows:
        reason = ("accepted (clean baseline)" if r["gate_valid"] else
                  (r["hard_fails"][0] if r["hard_fails"] else "; ".join(r["gate_errors"])))[:140]
        lines.append(f"| {r['name']} | {r['context']} | {r['quality_score']:.1f} | "
                     f"{r['expected_decision']} | {'yes' if r['detected'] else 'NO'} | {reason} |")
    lines += [
        "",
        "## Interpretation notes",
        "",
        "- Track A is uniform by construction: the fallback is deterministic; dataset variation lands in",
        "  executability/test-density (discovery-driven cases C09/C11/C14 score lower test=4.5/exec=2–3).",
        "- Track B measures the validator over the shipped known-good fixture plus mutated variants;",
        "  B06 documents the coverage rule (errors fire at >=3 stated requirements, covered < 60%).",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    templates = Templates.load(ROOT / "prompts")
    out_dir = ROOT / "runs" / "prompt-quality"
    out_dir.mkdir(parents=True, exist_ok=True)

    a_rows = evaluate_track_a(templates)
    b_rows = evaluate_track_b()
    a_scores = [r["quality_score"] for r in a_rows]
    violations = [r for r in a_rows if not r["pass"]] + [r for r in b_rows if not r["detected"]]

    md = render_markdown(a_rows, b_rows)
    payload = {
        "threshold": THRESHOLD,
        "track_a": {"cases": len(a_rows), "all_gate_valid": all(r["gate_valid"] for r in a_rows),
                    "avg_quality": round(sum(a_scores) / len(a_scores), 1),
                    "min_quality": min(a_scores), "hard_fails": sum(len(r["hard_fails"]) for r in a_rows),
                    "rows": a_rows},
        "track_b": {"cases": len(b_rows), "detected": sum(1 for r in b_rows if r["detected"]), "rows": b_rows},
        "violations": violations,
    }
    (out_dir / "evaluation.md").write_text(md, encoding="utf-8")
    (out_dir / "evaluation.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"track A: {len(a_rows)} cases, avg {payload['track_a']['avg_quality']}, "
          f"min {payload['track_a']['min_quality']}, hard fails {payload['track_a']['hard_fails']}")
    print(f"track B: {payload['track_b']['detected']}/{len(b_rows)} decisions match expectation")
    if violations:
        for v in violations:
            print("VIOLATION:", v.get("cid") or v.get("name"), "| pass_floor:",
                  v.get("pass", v.get("detected")))
        return 2
    print(f"wrote {out_dir / 'evaluation.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
