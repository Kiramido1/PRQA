#!/usr/bin/env python3
"""prqa-record — write one scenario record for prqa-chunked-run from plain flags (no hand-written JSON).

gpt-oss writes tool-call arguments as JSON; a heredoc of JSON inside that JSON needs double escaping and is the most
common source of rejected generations (provider 400 `tool_use_failed`). With this helper the model passes plain
strings only:

  prqa-record --id ORGANI-GET-001 --status PASS --http 200 --endpoint "GET /api/v1/organizations/{id}" \
      --scenario "happy path" --expected "200 with working_days" --actual "200, working_days present" --evidence get-001.json
  prqa-record --id AUTH-001 --status FAIL --http 200 ... --defect-title "..." --severity P1 --origin introduced_by_pr \
      --impact "..." --repro 'curl -s "$BASE_URL/..."'

Writes $PRQA_EVIDENCE_DIR/tc/<ID>.json (overwrites an earlier record of the same ID).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ID_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{3}$")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="prqa-record")
    ap.add_argument("--id", required=True)
    ap.add_argument("--status", required=True, type=str.upper, choices=["PASS", "FAIL", "BLOCKED", "NOT_APPLICABLE"])
    ap.add_argument("--http", default="")
    ap.add_argument("--endpoint", default="")
    ap.add_argument("--scenario", default="")
    ap.add_argument("--expected", default="")
    ap.add_argument("--actual", default="")
    ap.add_argument("--evidence", default="")
    ap.add_argument("--defect-title", default="")
    ap.add_argument("--severity", default="P2", type=str.upper, choices=["P0", "P1", "P2", "P3"])
    ap.add_argument("--severity-rationale", default="")
    ap.add_argument("--origin", default="unknown",
                    choices=["introduced_by_pr", "pre_existing", "environment", "test_data", "ambiguous_contract", "unknown"])
    ap.add_argument("--impact", default="")
    ap.add_argument("--repro", default="")
    a = ap.parse_args(argv)
    if not ID_RE.match(a.id):
        print(f"prqa-record: invalid scenario id {a.id!r}", file=sys.stderr)
        return 2
    http = int(a.http) if re.fullmatch(r"\d{3}", a.http.strip()) else None
    rec = {"id": a.id, "status": a.status, "http_status": http, "endpoint": a.endpoint, "scenario": a.scenario,
           "expected": a.expected, "actual": a.actual, "evidence": a.evidence}
    if a.status == "FAIL":
        rec["defect"] = {"title": a.defect_title or f"{a.id} failed", "severity": a.severity, "severity_rationale": a.severity_rationale,
                         "expected": a.expected, "actual": a.actual, "repro_curl": a.repro, "impact": a.impact, "origin": a.origin}
    out = Path(os.environ.get("PRQA_EVIDENCE_DIR") or "./prqa-evidence") / "tc"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{a.id}.json").write_text(json.dumps(rec, indent=1), encoding="utf-8")
    print(f"recorded {a.id} {a.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
