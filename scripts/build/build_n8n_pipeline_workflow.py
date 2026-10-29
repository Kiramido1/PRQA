#!/usr/bin/env python3
"""Generate n8n/trigger/09-prqa-label-pipeline.json — the label-triggered PRQA pipeline orchestrated by n8n.

n8n owns the flow (schedule, branching, wait/poll loop); every stage is one authenticated call to the host bridge
(scripts/services/prqa_n8n_bridge.py → scripts/pipeline/prqa_stages.py). The only $env values used are PRQA_BRIDGE_URL,
PRQA_N8N_BRIDGE_TOKEN and PRQA_MODEL_PROXY_PROMPT_TOKEN (Groq via the model proxy). Exported inactive; activate it explicitly after import:

    python3 scripts/build/build_n8n_pipeline_workflow.py
    docker cp n8n/trigger/09-prqa-label-pipeline.json prqa-n8n-1:/tmp/09.json && docker exec prqa-n8n-1 n8n import:workflow --input=/tmp/09.json
    python3 scripts/setup/n8n_api.py activate PrqaLabelTrigger true
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "n8n" / "trigger" / "09-prqa-label-pipeline.json"
WF_ID = "PrqaLabelTrigger"
CLAIM = "2 · Claim PR"
KEY = f"$('{CLAIM}').first().json.key"
PROMPT_PROXY_URL = "http://prqa-model-proxy:8089/v1/chat/completions"     # worker policy DEFAULT_PROMPT_LLM_PROXY_URL
# Same extraction as workflow 03: the text is UNTRUSTED until the worker validates and gates it; provider error text is
# never forwarded, only that an error happened (the worker classifies it).
EXTRACT_JS = """const r = $input.first().json;
if (r && r.error && !r.choices) {
  const code = r.error.httpCode || r.error.status || null;
  return [{ json: { generation: { generated_text: null, finish_reason: null, llm_error: 'prompt LLM call failed',
                                  http_status: code ? String(code) : null, response_shape: null, usage: null } } }];
}
const content = r && r.choices && r.choices[0] && r.choices[0].message ? r.choices[0].message.content : null;
const providerError = r && (r.error || (r.choices && r.choices[0] && r.choices[0].error));
const hasChoices = Boolean(r && Array.isArray(r.choices) && r.choices.length);
const shape = (hasChoices || providerError) ? null : ((r && typeof r.data === 'string') ? 'non_json_body' : 'no_choices');
return [{ json: { generation: {
  generated_text: typeof content === 'string' ? content : null,
  finish_reason: (r.choices && r.choices[0] && r.choices[0].finish_reason) || null,
  llm_error: (providerError && typeof content !== 'string') ? 'provider error in HTTP 200 body' : null,
  http_status: null,
  response_shape: shape,
  usage: (r && r.usage) ? { prompt_tokens: r.usage.prompt_tokens, completion_tokens: r.usage.completion_tokens,
                            total_tokens: r.usage.total_tokens } : null,
}, groq_model: r.model || null } }];"""

nodes: list[dict] = []
conns: dict[str, dict] = {}
_ids: list[None] = []


def node(name: str, type_: str, version: float, x: int, y: int, params: dict, **extra) -> str:
    nodes.append({"id": f"n09-{len(nodes):02d}", "name": name, "type": f"n8n-nodes-base.{type_}", "typeVersion": version,
                  "position": [x, y], "parameters": params, **extra})
    return name


def link(src: str, *targets: str | None) -> None:
    """targets[i] is connected to output i of src (None leaves an output unconnected)."""
    conns[src] = {"main": [[{"node": t, "type": "main", "index": 0}] if t else [] for t in targets]}


def stage(name: str, stage_: str, x: int, y: int, body: str = "{}", timeout_s: int = 330) -> str:
    return node(name, "httpRequest", 4.2, x, y, {
        "method": "POST", "url": f"={{{{ $env.PRQA_BRIDGE_URL }}}}/stage/{stage_}",
        "sendHeaders": True, "headerParameters": {"parameters": [{"name": "Authorization", "value": "=Bearer {{ $env.PRQA_N8N_BRIDGE_TOKEN }}"}]},
        "sendBody": True, "specifyBody": "json", "jsonBody": f"={{{{ JSON.stringify({body}) }}}}",
        "options": {"timeout": timeout_s * 1000}})


def cond(left: str, op: dict, right="") -> dict:
    _ids.append(None)
    return {"id": f"c{len(_ids):02d}", "leftValue": left, "rightValue": right, "operator": op}


IS_TRUE = {"type": "boolean", "operation": "true", "singleValue": True}
EQ = {"type": "string", "operation": "equals"}


def if_true(name: str, x: int, y: int, left: str) -> str:
    return node(name, "if", 2.2, x, y, {"conditions": {"options": {"caseSensitive": True, "typeValidation": "loose", "version": 2},
                                                       "combinator": "and", "conditions": [cond(left, IS_TRUE)]}, "options": {}})


def switch(name: str, x: int, y: int, rules: list[tuple[str, dict]], fallback: str) -> str:
    return node(name, "switch", 3.2, x, y, {
        "rules": {"values": [{"outputKey": k, "renameOutput": True,
                              "conditions": {"options": {"caseSensitive": True, "typeValidation": "loose", "version": 2}, "combinator": "and",
                                             "conditions": [c]}} for k, c in rules]},
        "options": {"fallbackOutput": "extra", "renameFallbackOutput": fallback}})


def end(name: str, x: int, y: int) -> str:
    return node(name, "noOp", 1, x, y, {})


node("Note", "stickyNote", 1, -60, -380, {"width": 1300, "height": 240, "content": (
    "## PRQA 09 · Label-Triggered Pipeline (Kiramido1/Amenly-Product)\n"
    "Every minute: **Discover** open PRs labelled `to be test` that were not tested at their head SHA → **Claim** (once per SHA, concurrency 1) → "
    "**Env + Context** (disposable API at the SHA) → **Groq Prompt in 3 stages** (scope → coverage → sections, each ≤ 5.5k tokens, "
    "released when the shared ledger has TPM capacity; validated between stages, assembled locally) → **Quality Gate** on the complete "
    "prompt (≥ 85, hard fails; a failed or rejected generation falls back to the reference prompt) → **Start OpenCode Run** (executor `local`: the OpenCode CLI "
    "on this machine, free GLM, bubblewrap-isolated, live terminal window; `sandbox`: grant → worker → launcher → container) → "
    "**Wait / Poll** → **Report + Cleanup + Discord**.\n"
    "A blocked stage cleans up and reports to Discord by itself. n8n holds only the bridge token and the inference-only prompt credential: "
    "no GitHub, provider, grant or Discord secret.")})
trig = node("Every Minute", "scheduleTrigger", 1.2, 0, 0, {"rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}})
man = node("Run Now (manual)", "manualTrigger", 1, 0, 180, {})
disc = stage("1 · Discover PRs", "discover", 240, 80)
act = switch("Next Action", 480, 80, [("run", cond("={{ $json.action }}", EQ, "run")), ("recover", cond("={{ $json.action }}", EQ, "recover")),
                                      ("in progress", cond("={{ $json.action }}", EQ, "wait")), ("idle", cond("={{ $json.action }}", EQ, "idle"))],
             "prerequisites missing / busy")
claim = stage(CLAIM, "claim", 760, -120, "{pr: $json.next.pr}")
rec = stage("Recover Stale Run", "recover", 760, 60)
busy = end("Another PR is being tested", 760, 200)
idle = end("No labelled PR to test", 760, 320)
blockedpre = end("Prerequisites missing / busy (see output)", 760, 440)
claimed = if_true("Claimed?", 1000, -120, "={{ $json.claimed }}")
prep = stage("3 · Env + Context", "prepare", 1240, -200, "{key: $json.key}", timeout_s=3600)
notclaimed = end("Not claimed (see reason)", 1240, -20)
envok = if_true("Env ready?", 1480, -200, "={{ $json.ok }}")
envblk = end("Env blocked: cleaned up + Discord sent", 1720, -40)
hasreq = if_true("Groq request ready?", 1720, -200, "={{ !!$json.llm_request }}")
tpm = node("Wait for TPM capacity", "wait", 1.1, 1840, -420, {"resume": "timeInterval", "amount": "={{ Math.max(1, $json.wait_s || 1) }}",
                                                               "unit": "seconds"}, webhookId="0b6f7a2e-3c41-4f0e-9a51-7d2c8e4b6a10",
           notes="wait_s comes from the shared budget ledger (rolling 60 s window, provider-reported remaining tokens/reset); "
                 "the proxy re-checks and reserves when the request is sent", notesInFlow=True)
groq = node("3b · Groq Prompt (model proxy)", "httpRequest", 4.2, 2080, -420, {
    "method": "POST", "url": PROMPT_PROXY_URL,
    "sendHeaders": True, "headerParameters": {"parameters": [
        {"name": "Authorization", "value": "=Bearer {{ $env.PRQA_MODEL_PROXY_PROMPT_TOKEN }}"},
        {"name": "X-PRQA-Job-Id", "value": "={{ $json.prompt_job_id }}"},
        {"name": "X-Title", "value": "PR QA Pilot"}]},
    "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.llm_request) }}",
    "options": {"timeout": 360000}},
    retryOnFail=False, onError="continueErrorOutput",
    notes="one stage of the staged generator per call; inference-only prompt credential; the proxy pins the model, holds the "
          "Groq key and enforces the shared budget. No n8n retry: the stage runner decides (at most one per stage).", notesInFlow=True)
extract = node("Extract Groq Result", "code", 2, 2320, -420, {"jsCode": EXTRACT_JS})
step = stage("3a · Prompt Stage (validate → next)", "promptstep", 2560, -420, f"{{key: {KEY}, generation: $json.generation}}", timeout_s=120)
nxt = switch("Next Prompt Step", 2800, -420, [("next stage / retry", cond("={{ $json.next }}", EQ, "call"))],
             "assembled or stopped → gate")
qgate = stage("3c · Quality Gate (85 + hard fails)", "gate", 3040, -200,
              f"{{key: {KEY}, generation: $json.generation === undefined ? null : $json.generation}}", timeout_s=600)
gateok = if_true("Gate accepted?", 3280, -200, "={{ $json.ok }}")
sub = stage("4 · Start OpenCode Run", "submit", 3520, -280, f"{{key: {KEY}}}", timeout_s=600)
gateblk = end("Gate rejected: cleaned up + Discord sent", 3520, -100)
subok = if_true("Job created?", 3760, -280, "={{ $json.ok }}")
wait = node("Wait 30s", "wait", 1.1, 4000, -360, {"resume": "timeInterval", "amount": 30, "unit": "seconds"}, webhookId="5f0d0c55-6a8e-4d1a-9c55-0e7a09a1b2c3")
subblk = end("Submit blocked: cleaned up + Discord sent", 4000, -180)
poll = stage("5 · Poll Job", "poll", 4240, -360, f"{{key: {KEY}}}")
pst = switch("Job State", 4480, -360, [("finished", cond("={{ $json.terminal }}", IS_TRUE)), ("timed out", cond("={{ $json.timed_out }}", IS_TRUE))],
             "still running")
fin = stage("6 · Report + Cleanup + Discord", "finalize", 4720, -460, f"{{key: {KEY}}}", timeout_s=600)
tout = stage("Fail: Timed Out (cancel + Discord)", "fail", 4720, -280, f"{{key: {KEY}, reason: 'n8n: OpenCode job exceeded the wait limit'}}")
done = end("Done (verdict in output)", 4960, -460)

link(trig, disc)
link(man, disc)
link(disc, act)
link(act, claim, rec, busy, idle, blockedpre)
link(claim, claimed)
link(claimed, prep, notclaimed)
link(prep, envok)
link(envok, hasreq, envblk)
link(hasreq, tpm, qgate)
link(tpm, groq)
conns[groq] = {"main": [[{"node": extract, "type": "main", "index": 0}], [{"node": extract, "type": "main", "index": 0}]]}   # success + error
link(extract, step)
link(step, nxt)
link(nxt, tpm, qgate)                                                   # bounded by the stage runner: <= stages x (1 + retries)
link(qgate, gateok)
link(gateok, sub, gateblk)
link(sub, subok)
link(subok, wait, subblk)
link(wait, poll)
link(poll, pst)
link(pst, fin, tout, wait)
link(fin, done)

wf = {"id": WF_ID, "name": "PRQA 09 · Label-Triggered Pipeline", "active": False, "nodes": nodes, "connections": conns, "pinData": {},
      "settings": {"executionOrder": "v1", "saveManualExecutions": True, "saveDataSuccessExecution": "all", "saveDataErrorExecution": "all",
                   "saveExecutionProgress": True}}

if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(nodes)} nodes)")
