#!/usr/bin/env python3
"""Validate n8n/workflows/*.json without n8n: syntax, structure, node types, connections, references, secrets.

    python3 scripts/build/validate_workflows.py            # human-readable
    python3 scripts/build/validate_workflows.py --json     # machine-readable

Exit code 0 when every file passes.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WF_DIR = ROOT / "n8n" / "workflows"

# Node types the design allows. Execute Command / local file nodes are deliberately absent (SECURITY.md §11).
ALLOWED_TYPES = {
    "n8n-nodes-base.webhook": {2},
    "n8n-nodes-base.respondToWebhook": {1.1},
    "n8n-nodes-base.code": {2},
    "n8n-nodes-base.if": {2.2},
    "n8n-nodes-base.switch": {3.2},
    "n8n-nodes-base.set": {3.4},
    "n8n-nodes-base.httpRequest": {4.2},
    "n8n-nodes-base.executeWorkflow": {1.1},
    "n8n-nodes-base.executeWorkflowTrigger": {1.1},
    "n8n-nodes-base.manualTrigger": {1},
    "n8n-nodes-base.errorTrigger": {1},
    "n8n-nodes-base.wait": {1.1},
    "n8n-nodes-base.merge": {3},
    "n8n-nodes-base.noOp": {1},
    "n8n-nodes-base.stopAndError": {1},
    "n8n-nodes-base.stickyNote": {1},
}
FORBIDDEN_TYPES = {"n8n-nodes-base.executeCommand", "n8n-nodes-base.localFileTrigger", "n8n-nodes-base.readWriteFile", "n8n-nodes-base.ssh"}
SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), re.compile(r"xai-[A-Za-z0-9]{20,}"), re.compile(r"sk-or-v1-[A-Za-z0-9]{32,}"),
    re.compile(r"discord(app)?\.com/api/webhooks/\d{5,}/[A-Za-z0-9_-]{20,}"), re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"),
]
REAL_HOST_ALLOWLIST = {"https://api.github.com"}
# Shared provider budget: no workflow may hold a model-provider key or address a provider. Model generation goes to the
# model proxy (its ledger, locked contract and key) with the inference-only prompt credential, the only accepted bearer.
PROVIDER_KEY_ENV_RE = re.compile(r"\$env\.(GROQ_API_KEY|PROMPT_LLM_API_KEY|OPENROUTER_API_KEY|OPENAI_API_KEY)\b")
PROVIDER_HOST_RE = re.compile(r"(api\.groq\.com|openrouter\.ai|api\.openai\.com)", re.I)
GENERATION_CREDENTIAL = "$env.PRQA_MODEL_PROXY_PROMPT_TOKEN"
# A model-generation request (POST .../chat/completions) is never retried and must outlast the slowest observed
# synthetic generation (272.4 s, C01 at a 24000-token budget).
GENERATION_TIMEOUT_MIN_MS = 300_000


def validate_file(path: Path, all_ids: set[str]) -> dict:
    report = {"file": str(path.relative_to(ROOT)), "errors": [], "warnings": [], "nodes": 0, "connections": 0, "sub_workflow_refs": []}
    try:
        wf = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report["errors"].append(f"invalid JSON: {exc}")
        return report
    for key in ("id", "name", "nodes", "connections", "settings"):
        if key not in wf:
            report["errors"].append(f"missing top-level key {key!r}")
    if wf.get("active") is not False:
        report["errors"].append("workflow must be exported with active=false")
    names: dict[str, dict] = {}
    for node in wf.get("nodes", []):
        t, v, n = node.get("type"), node.get("typeVersion"), node.get("name")
        if not n or n in names:
            report["errors"].append(f"duplicate or empty node name {n!r}")
        names[n] = node
        if t in FORBIDDEN_TYPES:
            report["errors"].append(f"{n}: forbidden node type {t}")
        elif t not in ALLOWED_TYPES:
            report["errors"].append(f"{n}: node type {t} not in the design allowlist")
        elif v not in ALLOWED_TYPES[t]:
            report["warnings"].append(f"{n}: typeVersion {v} for {t} differs from the pinned version {sorted(ALLOWED_TYPES[t])}")
        if not isinstance(node.get("position"), list) or len(node["position"]) != 2:
            report["errors"].append(f"{n}: position must be [x, y]")
        if t == "n8n-nodes-base.executeWorkflow":
            ref = (node.get("parameters", {}).get("workflowId") or {})
            target = ref.get("value") if isinstance(ref, dict) else ref
            report["sub_workflow_refs"].append(target)
            if target not in all_ids:
                report["errors"].append(f"{n}: references unknown workflow id {target!r}")
        if t == "n8n-nodes-base.httpRequest":
            url = str(node.get("parameters", {}).get("url", ""))
            if url.startswith("https://") and not any(url.startswith(h) for h in REAL_HOST_ALLOWLIST):
                report["errors"].append(f"{n}: hard-coded external URL {url}")
            if url.startswith("http://") and "prqa-mocks" not in url and "$env" not in url and "$json" not in url:
                report["errors"].append(f"{n}: hard-coded plain-http URL {url}")
            if PROVIDER_HOST_RE.search(json.dumps(node.get("parameters", {}))):
                report["errors"].append(f"{n}: addresses a model provider directly (generation must go through the model proxy)")
            if url.rstrip("/").endswith("/chat/completions"):
                auth = [h.get("value", "") for h in ((node.get("parameters", {}).get("headerParameters") or {}).get("parameters") or [])
                        if str(h.get("name", "")).lower() == "authorization"]
                if len(auth) != 1 or "$env." in auth[0].replace(GENERATION_CREDENTIAL, ""):
                    report["errors"].append(f"{n}: model-generation request must authenticate with {GENERATION_CREDENTIAL} only")
                if node.get("retryOnFail") or int(node.get("maxTries") or 1) > 1:
                    report["errors"].append(f"{n}: model-generation request must not retry (retryOnFail/maxTries)")
                timeout = (node.get("parameters", {}).get("options") or {}).get("timeout")
                if not isinstance(timeout, int) or timeout < GENERATION_TIMEOUT_MIN_MS:
                    report["errors"].append(f"{n}: model-generation timeout {timeout!r} ms is below {GENERATION_TIMEOUT_MIN_MS} ms")
    report["nodes"] = len([n for n in names.values() if n["type"] != "n8n-nodes-base.stickyNote"])
    for src, outs in wf.get("connections", {}).items():
        if src not in names:
            report["errors"].append(f"connection from unknown node {src!r}")
        for branch in outs.get("main", []):
            for edge in branch:
                report["connections"] += 1
                if edge.get("node") not in names:
                    report["errors"].append(f"{src} → unknown node {edge.get('node')!r}")
    triggers = [n for n in names.values() if n["type"] in ("n8n-nodes-base.webhook", "n8n-nodes-base.executeWorkflowTrigger",
                                                           "n8n-nodes-base.manualTrigger", "n8n-nodes-base.errorTrigger")]
    if not triggers:
        report["errors"].append("no trigger node")
    connected = set(wf.get("connections", {}).keys()) | {e["node"] for outs in wf.get("connections", {}).values() for b in outs.get("main", []) for e in b}
    for n, node in names.items():
        if node["type"] != "n8n-nodes-base.stickyNote" and n not in connected:
            report["warnings"].append(f"{n}: not connected to anything")
    text = path.read_text(encoding="utf-8")
    for m in sorted(set(PROVIDER_KEY_ENV_RE.findall(text))):
        report["errors"].append(f"references the provider key {m} (only the model proxy may hold a provider key)")
    for pat in SECRET_PATTERNS:
        if pat.search(text):
            report["errors"].append(f"secret-looking content matches {pat.pattern}")
    if "n8n-nodes-base.webhook" in {n["type"] for n in names.values()}:
        for n, node in names.items():
            if node["type"] == "n8n-nodes-base.webhook" and not node.get("parameters", {}).get("options", {}).get("rawBody"):
                report["errors"].append(f"{n}: webhook must keep rawBody for HMAC verification")
    if wf["id"] != "PrqaErrorHandler" and wf.get("settings", {}).get("errorWorkflow") != "PrqaErrorHandler":
        report["warnings"].append("errorWorkflow setting does not point to PrqaErrorHandler")
    return report


def main() -> int:
    files = sorted(WF_DIR.glob("*.json"))
    if not files:
        print(f"no workflow files in {WF_DIR}")
        return 1
    ids = set()
    for f in files:
        try:
            ids.add(json.loads(f.read_text(encoding="utf-8")).get("id"))
        except json.JSONDecodeError:
            pass
    reports = [validate_file(f, ids) for f in files]
    ok = all(not r["errors"] for r in reports)
    if "--json" in sys.argv:
        print(json.dumps({"ok": ok, "reports": reports}, indent=2))
    else:
        for r in reports:
            status = "OK " if not r["errors"] else "ERR"
            print(f"[{status}] {r['file']}: {r['nodes']} nodes, {r['connections']} connections, refs={r['sub_workflow_refs']}")
            for e in r["errors"]:
                print(f"      error:   {e}")
            for w in r["warnings"]:
                print(f"      warning: {w}")
        print("ALL WORKFLOWS VALID" if ok else "VALIDATION FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
