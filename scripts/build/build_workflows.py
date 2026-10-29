#!/usr/bin/env python3
"""Generate the importable n8n workflow JSON files into n8n/workflows/.

The workflows are generated (not hand-written) so node ids, positions and
cross-workflow references stay consistent. Re-run after editing this file:

    python3 scripts/build/build_workflows.py

Every workflow is exported with "active": false. Nothing here contains a secret:
tokens and URLs are read at runtime through $env (see compose/docker-compose.yml).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "n8n" / "workflows"

# Fixed 16-character workflow ids so Execute Workflow nodes can reference each other.
WF = {
    "main": ("PrqaMainPipeline", "PRQA 00 · Main Pipeline"),
    "webhook": ("PrqaWebhookRecvr", "PRQA 01 · GitHub Webhook Receiver"),
    "context": ("PrqaPrContextCol", "PRQA 02 · PR Context Collector"),
    "prompt": ("PrqaLlmPromptBld", "PRQA 03 · Prompt Builder (LLM)"),
    "dispatch": ("PrqaOpencodeDisp", "PRQA 04 · OpenCode Job Dispatcher"),
    "validate": ("PrqaResultValidn", "PRQA 05 · Result Validation & Report"),
    "discord": ("PrqaDiscordRepor", "PRQA 06 · Discord Reporter"),
    "comment": ("PrqaGithubCommen", "PRQA 07 · GitHub PR Commenter"),
    "error": ("PrqaErrorHandler", "PRQA 08 · Error Handler"),
}
FILES = {
    "main": "00-prqa-main-pipeline.json",
    "webhook": "01-prqa-github-webhook-receiver.json",
    "context": "02-prqa-pr-context-collector.json",
    "prompt": "03-prqa-llm-prompt-builder.json",
    "dispatch": "04-prqa-opencode-job-dispatcher.json",
    "validate": "05-prqa-result-validation-report.json",
    "discord": "06-prqa-discord-reporter.json",
    "comment": "07-prqa-github-pr-commenter.json",
    "error": "08-prqa-error-handler.json",
}

WORKER_TOKEN_HEADER = {"name": "X-Worker-Token", "value": "={{ $env.PRQA_WORKER_TOKEN }}"}
X0, Y0, DX, DY = 0, 300, 280, 200


# --------------------------------------------------------------------------- helpers ---
class Builder:
    def __init__(self, key: str) -> None:
        self.id, self.name = WF[key]
        self.nodes: list[dict[str, Any]] = []
        self.connections: dict[str, Any] = {}
        self._names: set[str] = set()

    def _nid(self, name: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"prqa/{self.id}/{name}"))

    def node(self, name: str, type_: str, version: float, params: dict[str, Any], col: float, row: float = 0,
             *, notes: str = "", **extra: Any) -> str:
        if name in self._names:
            raise ValueError(f"duplicate node name {name!r} in {self.name}")
        self._names.add(name)
        node = {
            "parameters": params,
            "id": self._nid(name),
            "name": name,
            "type": type_,
            "typeVersion": version,
            "position": [X0 + int(col * DX), Y0 + int(row * DY)],
        }
        if notes:
            node["notes"] = notes
            node["notesInFlow"] = True
        node.update(extra)
        self.nodes.append(node)
        return name

    def sticky(self, content: str, col: float, row: float, width: int = 520, height: int = 160, color: int = 1) -> None:
        self.nodes.append({
            "parameters": {"content": content, "height": height, "width": width, "color": color},
            "id": self._nid(f"sticky:{content[:40]}:{col}:{row}"),
            "name": f"Note {len([n for n in self.nodes if n['type'] == 'n8n-nodes-base.stickyNote']) + 1}",
            "type": "n8n-nodes-base.stickyNote",
            "typeVersion": 1,
            "position": [X0 + int(col * DX), Y0 + int(row * DY)],
        })

    def connect(self, src: str, dst: str, output: int = 0, input_: int = 0) -> None:
        for n in (src, dst):
            if n not in self._names:
                raise ValueError(f"unknown node {n!r} in {self.name}")
        outputs = self.connections.setdefault(src, {"main": []})["main"]
        while len(outputs) <= output:
            outputs.append([])
        outputs[output].append({"node": dst, "type": "main", "index": input_})

    def chain(self, *names: str) -> None:
        for a, b in zip(names, names[1:]):
            self.connect(a, b)

    def build(self, *, error_workflow: bool = True) -> dict[str, Any]:
        settings: dict[str, Any] = {"executionOrder": "v1", "saveExecutionProgress": True, "saveManualExecutions": True,
                                    "timezone": "UTC", "callerPolicy": "workflowsFromSameOwner"}
        if error_workflow and self.id != WF["error"][0]:
            settings["errorWorkflow"] = WF["error"][0]
        return {
            "id": self.id,
            "name": self.name,
            "active": False,
            "nodes": self.nodes,
            "connections": self.connections,
            "settings": settings,
            "staticData": None,
            "pinData": {},
            "meta": {"templateCredsSetupCompleted": True, "prqa": {"generated_by": "scripts/build/build_workflows.py", "review_only": True}},
            "versionId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"prqa/version/{self.id}")),
        }


def cond(left: str, op_type: str, operation: str, right: Any = None, *, single: bool = False) -> dict[str, Any]:
    c: dict[str, Any] = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"cond/{left}/{operation}/{right}")),
                         "leftValue": left, "rightValue": right if right is not None else "",
                         "operator": {"type": op_type, "operation": operation}}
    if single:
        c["operator"]["singleValue"] = True
    return c


def conditions(*conds: dict[str, Any], combinator: str = "and") -> dict[str, Any]:
    return {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
            "conditions": list(conds), "combinator": combinator}


def if_params(*conds: dict[str, Any], combinator: str = "and") -> dict[str, Any]:
    return {"conditions": conditions(*conds, combinator=combinator), "options": {}}


def switch_params(left: str, routes: list[tuple[str, str]], fallback: str = "none") -> dict[str, Any]:
    return {
        "rules": {"values": [
            {"conditions": conditions(cond(left, "string", "equals", value)), "renameOutput": True, "outputKey": label}
            for value, label in routes
        ]},
        "options": {"fallbackOutput": fallback},
    }


def http(method: str, url: str, *, body: str | None = None, headers: list[dict[str, str]] | None = None,
         timeout_ms: int = 30000, never_error: bool = False, full_response: bool = False, text: bool = False) -> dict[str, Any]:
    p: dict[str, Any] = {"method": method, "url": url, "authentication": "none", "options": {"timeout": timeout_ms}}
    if headers:
        p["sendHeaders"] = True
        p["headerParameters"] = {"parameters": headers}
    if body is not None:
        p["sendBody"] = True
        p["specifyBody"] = "json"
        p["jsonBody"] = body
    resp: dict[str, Any] = {}
    if never_error:
        resp["neverError"] = True
    if full_response:
        resp["fullResponse"] = True
    if text:
        resp["responseFormat"] = "text"
        resp["outputPropertyName"] = "data"
    if resp:
        p["options"]["response"] = {"response": resp}
    return p


def worker_http(method: str, path_expr: str, *, body: str | None = None, **kw: Any) -> dict[str, Any]:
    return http(method, f"={{{{ $env.PRQA_WORKER_URL }}}}{path_expr}", body=body, headers=[WORKER_TOKEN_HEADER], **kw)


def set_params(assignments: list[tuple[str, str, str]], include_other: bool = True) -> dict[str, Any]:
    return {
        "assignments": {"assignments": [
            {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"assign/{name}/{value}")), "name": name, "value": value, "type": type_}
            for name, value, type_ in assignments
        ]},
        "includeOtherFields": include_other,
        "options": {},
    }


def code(js: str) -> dict[str, Any]:
    return {"jsCode": js.strip("\n"), "mode": "runOnceForAllItems"}


def exec_wf(key: str, wait: bool = True) -> dict[str, Any]:
    wf_id, wf_name = WF[key]
    p: dict[str, Any] = {"source": "database",
                         "workflowId": {"__rl": True, "value": wf_id, "mode": "id", "cachedResultName": wf_name},
                         "mode": "once", "options": {}}
    if not wait:
        p["options"]["waitForSubWorkflow"] = False
    return p


RETRY = {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 5000}
RETRY2 = {"retryOnFail": True, "maxTries": 2, "waitBetweenTries": 3000}
# Model-generation call (workflow 03): exactly one attempt, never retried (a retry is a second paid generation).
# Synthetic C01 live runs took 212.8-272.4 s (the slowest exhausted a 24000-token budget), so 360 s leaves ~30% headroom
# at the evaluator ceiling; a timeout falls through to the deterministic reference fallback.
PROMPT_LLM_TIMEOUT_MS = 360_000
ERR_OUT = {"onError": "continueErrorOutput"}

REVIEW_NOTE = (
    "## 🔒 Phase 1 — REVIEW ONLY\n"
    "`AUTOMATION_MODE=review_only` (compose/.env). The worker returns **mock** endpoints for GitHub, the prompt LLM and Discord, "
    "rejects every non-synthetic job with `REVIEW_ONLY_MODE`, and the registry has **0 enabled repositories**.\n\n"
    "Nothing in this workflow can reach a real repository, API or channel until Phase 2 is explicitly approved."
)


# ------------------------------------------------------------------------- JS code ---
JS_VERIFY_SIGNATURE = r"""
// Verify GitHub's X-Hub-Signature-256 over the RAW request body (never over re-serialized JSON).
const crypto = require('crypto');
const item = $input.first();
const headers = item.json.headers || {};
const sig = String(headers['x-hub-signature-256'] || '');
const event = String(headers['x-github-event'] || '');
const deliveryId = String(headers['x-github-delivery'] || '');
const secret = $env.GITHUB_WEBHOOK_SECRET || '';

let raw;
if (item.binary && item.binary.data && item.binary.data.data) {
  raw = Buffer.from(item.binary.data.data, 'base64');          // rawBody option → binary.data
} else {
  raw = Buffer.from(JSON.stringify(item.json.body ?? {}), 'utf8'); // fallback, only if raw body unavailable
}

let valid = false;
let reason = 'ok';
if (!secret) {
  reason = 'GITHUB_WEBHOOK_SECRET is not configured';
} else if (!sig.startsWith('sha256=')) {
  reason = 'missing or malformed X-Hub-Signature-256';
} else {
  const expected = 'sha256=' + crypto.createHmac('sha256', secret).update(raw).digest('hex');
  valid = expected.length === sig.length && crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(sig));
  if (!valid) reason = 'signature mismatch';
}

let payload = item.json.body;
if (typeof payload !== 'object' || payload === null) {
  try { payload = JSON.parse(raw.toString('utf8')); } catch (e) { payload = null; }
}
return [{ json: {
  signature_valid: valid,
  signature_reason: reason,
  event,
  delivery_id: deliveryId,
  raw_body_bytes: raw.length,
  automation_mode: $env.PRQA_AUTOMATION_MODE || 'review_only',
  payload,
}}];
"""

JS_VALIDATE_EVENT = r"""
// Accept `pull_request`/`labeled` with the opt-in label ($env.PRQA_TRIGGER_LABEL, default "to be test" → $env.PRQA_DEFAULT_TEST_LEVEL)
// or a qa-test:* label, on an open non-draft PR. `unlabeled` (PRQA label), `closed` and `synchronize` are LIFECYCLE events:
// not eligible, but they cancel active jobs of that PR (worker /jobs/cancel). Everything else is acknowledged (200) and dropped.
// Mirrors worker/prqa_worker/validation.py::evaluate_event. Payload content is UNTRUSTED data.
const QA = { 'qa-test:smoke': 'smoke', 'qa-test:standard': 'standard', 'qa-test:comprehensive': 'comprehensive', 'qa-test:security': 'security' };
const LEVELS = ['smoke', 'standard', 'comprehensive', 'security'];
const TRIGGER_LABEL = String($env.PRQA_TRIGGER_LABEL || 'to be test').trim().toLowerCase();
const DEFAULT_LEVEL = LEVELS.includes(String($env.PRQA_DEFAULT_TEST_LEVEL || '').toLowerCase()) ? String($env.PRQA_DEFAULT_TEST_LEVEL).toLowerCase() : 'standard';
const LIFECYCLE = { unlabeled: 'LABEL_REMOVED', closed: 'PR_CLOSED', synchronize: 'SUPERSEDED' };
// Optional authorization: $env.PRQA_TRIGGER_ALLOWED_ACTORS (comma-separated logins). Checked against `sender.login`,
// which GitHub sets for the HMAC-authenticated event (the user who added the label) — never a PR-author-controlled field.
const ALLOWED_ACTORS = String($env.PRQA_TRIGGER_ALLOWED_ACTORS || '').split(',').map(a => a.trim().toLowerCase()).filter(Boolean);
const SHA = /^[0-9a-f]{40}$/;
const REPO = /^[A-Za-z0-9-]{1,39}\/[A-Za-z0-9._-]{1,100}$/;
const inp = $input.first().json;
const p = inp.payload || {};
const out = (eligible, reason_code, reason, job_seed, lifecycle) => [{ json: { ...inp, payload: undefined, eligible, reason_code, reason, job_seed: job_seed || null, lifecycle: lifecycle || null } }];
const levelFor = (name) => { const n = String(name || '').trim(); if (TRIGGER_LABEL && n.toLowerCase() === TRIGGER_LABEL) return DEFAULT_LEVEL; return QA[n] || null; };

if (inp.event === 'ping') return out(false, 'PING', 'GitHub ping acknowledged');
if (inp.event !== 'pull_request') return out(false, 'EVENT_NOT_PULL_REQUEST', `event '${inp.event}' ignored`);
if (!p.pull_request || !p.repository) return out(false, 'PAYLOAD_INVALID', 'pull_request/repository missing');
const pr = p.pull_request;
const repository = String(p.repository.full_name || '');
if (!REPO.test(repository)) return out(false, 'PAYLOAD_INVALID', 'invalid repository name');
const label = String((p.label && p.label.name) || '').trim();
const headSha = String((pr.head || {}).sha || ''), baseSha = String((pr.base || {}).sha || '');

if (p.action in LIFECYCLE) {
  if (p.action === 'unlabeled' && levelFor(label) === null) return out(false, 'LABEL_NOT_QA', `removed label '${label}' is not a PRQA label`);
  return out(false, `LIFECYCLE_${p.action.toUpperCase()}`, `'${p.action}' cancels active jobs for this PR`, null, {
    action: 'cancel', reason: LIFECYCLE[p.action], repository, pr_number: Number(pr.number),
    except_head_sha: (p.action === 'synchronize' && SHA.test(headSha)) ? headSha : null, label: label || null, delivery_id: inp.delivery_id,
  });
}
if (p.action !== 'labeled') return out(false, 'ACTION_NOT_LABELED', `action '${p.action}' ignored`);
const level = levelFor(label);
if (level === null) return out(false, 'LABEL_NOT_QA', `label '${label}' is neither '${TRIGGER_LABEL}' nor a qa-test label`);
const sender = String(((p.sender || {}).login) || '').trim();
if (ALLOWED_ACTORS.length && (!sender || !ALLOWED_ACTORS.includes(sender.toLowerCase()))) return out(false, 'ACTOR_NOT_ALLOWED', `label added by '${sender || 'unknown'}' who is not in PRQA_TRIGGER_ALLOWED_ACTORS`);
if (pr.state !== 'open') return out(false, 'PR_NOT_OPEN', 'pull request is not open');
if (pr.draft) return out(false, 'PR_DRAFT', 'draft pull requests are not tested');
if (!SHA.test(headSha) || !SHA.test(baseSha)) return out(false, 'PAYLOAD_INVALID', 'invalid head/base sha');
const headRepo = (pr.head || {}).repo || {};
const isFork = Boolean(headRepo.fork) || (Boolean(headRepo.full_name) && headRepo.full_name !== repository);

return out(true, 'ELIGIBLE', `labeled pull_request with '${label}' → test level ${level}`, {
  repository,
  synthetic: repository.startsWith('synthetic-org/'),
  test_level: level,
  pr: {
    number: Number(pr.number),
    title: String(pr.title || '').slice(0, 300),
    head_sha: headSha,
    base_sha: baseSha,
    head_ref: String((pr.head || {}).ref || '').slice(0, 255),
    base_ref: String((pr.base || {}).ref || '').slice(0, 255),
    is_fork: isFork,
    url: String(pr.html_url || '').slice(0, 500),
  },
  trigger: { source: 'github_webhook', label: `qa-test:${level}`, requested_label: label, delivery_id: inp.delivery_id, requested_by: sender.slice(0, 100) || undefined },
});
"""

JS_DEDUPE = r"""
// First dedupe layer: the worker records every X-GitHub-Delivery id (POST /deliveries/claim → 200 new, 409 seen).
// (Workflow static data is not used: Code-node writes to it are not persisted under n8n task runners.)
// The worker applies the second layer on POST /jobs: one active job per repo#pr@sha:level.
const inp = $('Validate Event & Extract PR').first().json;
const claim = $input.first().json;
const code = Number(claim.statusCode || 0);
if (code !== 200 && code !== 409) throw new Error(`delivery claim failed: worker returned HTTP ${code}`);
return [{ json: { ...inp, duplicate: code === 409, dedupe_layer: 'worker_deliveries', first_seen_at: (claim.body || {}).first_seen_at || null,
  received_at: new Date().toISOString(), webhook_execution_id: $execution.id } }];
"""

JS_PREPARE_ACTIONS_INTAKE = r"""
// GitHub Actions entry (POST /webhook/prqa/actions). Forward the RAW request bytes + the X-PRQA-* headers to the worker,
// which verifies HMAC-SHA256(PRQA_WEBHOOK_SECRET, "<timestamp>.<raw body>") in constant time, enforces the ±300 s window,
// validates the payload (schemas/actions-request.schema.json), derives the idempotency key and claims it (replay protection).
// The webhook secret never has to exist in the n8n container. Nothing from the body is interpreted here.
const item = $input.first();
const headers = item.json.headers || {};
let body_b64;
if (item.binary && item.binary.data && item.binary.data.data) {
  body_b64 = item.binary.data.data;                                            // rawBody option → binary.data (base64)
} else {
  body_b64 = Buffer.from(JSON.stringify(item.json.body ?? {}), 'utf8').toString('base64'); // fallback only
}
return [{ json: {
  body_b64,
  signature: String(headers['x-prqa-signature'] || ''),
  timestamp: String(headers['x-prqa-timestamp'] || ''),
  idempotency_key: String(headers['x-prqa-idempotency-key'] || ''),
  user_agent: String(headers['user-agent'] || '').slice(0, 200),
  receiver_execution_id: $execution.id,
}}];
"""

JS_ACTIONS_PIPELINE_INPUT = r"""
// 202 path: shape the Main Pipeline input exactly like the GitHub-webhook path does (job_seed contract).
const r = $input.first().json;
const b = r.body || {};
if (!b.job_seed) throw new Error('worker intake returned no job_seed');
return [{ json: {
  job_seed: b.job_seed,
  delivery_id: b.idempotency_key,
  idempotency_key: b.idempotency_key,
  received_at: b.received_at || new Date().toISOString(),
  entry_point: 'github_actions',
  automation_mode: b.automation_mode,
  policy_preview: b.policy_preview,
  webhook_execution_id: $execution.id,
}}];
"""

JS_SYNTHETIC_SEED = r"""
// Manual test entry point: the SYNTHETIC fixture PR (synthetic-org/sample-orders-api #101).
// This repository does not exist; the worker serves it from fixtures only.
const stamp = new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 14);
return [{ json: {
  job_seed: {
    repository: 'synthetic-org/sample-orders-api',
    synthetic: true,
    test_level: 'standard',
    pr: {
      number: 101,
      title: 'Add status filter and cancellation reason to orders API (SYNTHETIC)',
      head_sha: 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678',
      base_sha: '9f8e7d6c5b4a39281706f5e4d3c2b1a098765432',
      head_ref: 'feature/order-status-filter',
      base_ref: 'main',
      is_fork: false,
      url: 'https://github.com/synthetic-org/sample-orders-api/pull/101',
    },
    trigger: { source: 'synthetic_test', label: 'qa-test:standard', delivery_id: `synthetic-manual-${stamp}` },
  },
  delivery_id: `synthetic-manual-${stamp}`,
  received_at: new Date().toISOString(),
  entry_point: 'manual_synthetic_test',
}}];
"""

JS_NORMALIZE_INPUT = r"""
// One input contract regardless of trigger (webhook receiver or manual synthetic test).
const inp = $input.first().json;
const seed = inp.job_seed;
if (!seed || !seed.repository || !seed.pr) throw new Error('Pipeline input must contain job_seed {repository, pr, test_level, trigger}');
return [{ json: {
  job_seed: seed,
  delivery_id: inp.delivery_id || (seed.trigger || {}).delivery_id || null,
  received_at: inp.received_at || new Date().toISOString(),
  entry_point: inp.entry_point || 'webhook',
  pipeline_execution_id: $execution.id,
}}];
"""

JS_POLICY_GATE = r"""
// Policy gate — mirrors worker/prqa_worker/policy.py. The worker re-checks everything on POST /jobs,
// so this node is the visible, early decision, not the only one.
const input = $('Normalize Pipeline Input').first().json;
const cfg = $input.first().json;
const seed = input.job_seed;
const policy = cfg.policy || {};
const endpoints = cfg.endpoints || {};
const registry = (cfg.registry && cfg.registry.repositories) || [];
const entry = registry.find(r => r.full_name === seed.repository) || null;

let route = 'blocked', reason_code = 'UNKNOWN', reason = '', execution_mode = null;
if (seed.synthetic) {
  if (!seed.repository.startsWith('synthetic-org/')) { reason_code = 'SYNTHETIC_NAME_INVALID'; reason = 'synthetic jobs must target synthetic-org/*'; }
  else if (!entry || entry.kind !== 'synthetic') { reason_code = 'SYNTHETIC_REPO_NOT_REGISTERED'; reason = 'synthetic repository is not registered'; }
  else { route = 'synthetic'; execution_mode = 'mock'; reason_code = 'SYNTHETIC_MOCK_EXECUTION'; reason = 'fixture job → mock executor, no external calls'; }
} else if (policy.review_only) {
  reason_code = 'REVIEW_ONLY_MODE'; reason = 'AUTOMATION_MODE=review_only — real-project jobs are blocked until Phase 2 is explicitly approved';
} else if (!policy.real_execution_possible) {
  reason_code = 'REAL_PROJECT_TESTING_DISABLED'; reason = 'REAL_PROJECT_TESTING_ENABLED / AUTO_EXECUTION_ENABLED are false';
} else if (!entry) {
  reason_code = 'REPOSITORY_NOT_REGISTERED'; reason = 'repository is not in config/repositories.yaml';
} else if (!entry.effective_enabled) {
  reason_code = entry.approval_status === 'approved' ? 'REPOSITORY_DISABLED' : 'REPOSITORY_PENDING_APPROVAL';
  reason = `registry: enabled=${entry.enabled}, approval_status=${entry.approval_status}`;
} else if (seed.pr.is_fork && !entry.fork_prs_eligible) {
  reason_code = 'FORK_PR_NOT_ELIGIBLE'; reason = 'fork PRs are not eligible for this repository';
} else {
  route = 'allowed'; execution_mode = 'live'; reason_code = 'LIVE_EXECUTION_ALLOWED'; reason = 'all policy gates passed';
}

return [{ json: {
  ...input,
  policy,
  endpoints,
  registry_entry: entry,
  worker_version: cfg.worker_version,
  decision: { route, allowed: route !== 'blocked', reason_code, reason, execution_mode, decided_at: new Date().toISOString() },
}}];
"""

JS_STAGE_ERROR = r"""
// Collect a sanitized description of the failed stage (no raw URLs/tokens leave this node).
const item = $input.first();
const err = item.json.error || item.json;
const message = String((err && (err.message || err.description)) || JSON.stringify(err)).slice(0, 500)
  .replace(/https?:\/\/\S+/g, '[url]').replace(/gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w+|xai-[A-Za-z0-9]{20,}/g, '[token]');
let stage = 'unknown';
try { stage = $prevNode.name; } catch (e) {}
return [{ json: {
  kind: 'stage_error',
  detail: { stage, message, execution_id: $execution.id, workflow: $workflow.name, decision: (item.json.decision || null) },
  job_id: (item.json.job && item.json.job.id) || null,
}}];
"""

JS_ASSEMBLE_RAW_PR = r"""
// Combine the three GitHub responses into one object for the worker's sanitizer.
const base = $('Resolve GitHub API Endpoint').first().json;
const pr = $('Fetch PR Metadata').first().json;
const files = $('Fetch Changed Files').all().map(i => i.json);
const diffItem = $('Fetch Unified Diff').first().json;
const diff = typeof diffItem.data === 'string' ? diffItem.data : JSON.stringify(diffItem);
return [{ json: {
  repository: base.job_seed.repository,
  synthetic: Boolean(base.job_seed.synthetic),
  pr,
  files: Array.isArray(files) && files.length === 1 && Array.isArray(files[0]) ? files[0] : files,
  diff,
  github_api_mode: base.github_api_mode,
  mock_response: String(pr._mock || false) === 'true',
}}];
"""

JS_ATTACH_CONTEXT = r"""
const input = $('Context Input').first().json;
const built = $input.first().json;
return [{ json: { ...input, context: built.context, context_source: $('Assemble Raw PR Data').first().json.github_api_mode } }];
"""

JS_EXTRACT_LLM = r"""
// Pull the assistant text out of the OpenAI-compatible chat-completion response. Treated as UNTRUSTED until validated.
const r = $input.first().json;
const content = r && r.choices && r.choices[0] && r.choices[0].message ? r.choices[0].message.content : null;
// An HTTP 200 body can carry a provider `error` object instead of content: record that it happened, never its text.
const providerError = r && (r.error || (r.choices && r.choices[0] && r.choices[0].error));
// Without choices and without an error object nothing is invented: only the body's shape is reported
// (non-JSON text arrives as `data` because the node auto-detects the response format).
const hasChoices = Boolean(r && Array.isArray(r.choices) && r.choices.length);
const shape = (hasChoices || providerError) ? null : ((r && typeof r.data === 'string') ? 'non_json_body' : 'no_choices');
return [{ json: {
  generated_text: typeof content === 'string' ? content : null,
  llm_model: r.model || null,
  llm_finish_reason: (r.choices && r.choices[0] && r.choices[0].finish_reason) || null,
  llm_usage: r.usage || null,
  llm_error: (providerError && typeof content !== 'string') ? 'provider error in HTTP 200 body' : null,
  llm_response_shape: shape,
}}];
"""

# n8n holds NO provider key. A real prompt-generation request goes to the model proxy (worker endpoints():
# prompt_llm_api_base = PRQA_PROMPT_LLM_PROXY_URL, plain http on the internal prqa_prompt network) with the inference-only
# prompt credential; the proxy reserves the shared provider budget and alone injects the Groq key. A real-mode base that
# is not plain http (i.e. a provider URL) is never called.
LLM_KEY_EXPR = "$env.PRQA_MODEL_PROXY_PROMPT_TOKEN"
LLM_PROXY_BASE_OK = "String($json.prompt_llm_api_base || '').indexOf('http://') === 0"
LLM_JOB_REF_EXPR = ("={{ (String($('Prompt Builder Input').first().json.job_seed.repository || 'pr').replace(/[^A-Za-z0-9_.-]/g, '-') + '-' "
                    "+ String($('Prompt Builder Input').first().json.job_seed.pr.number)).slice(0, 100) }}")

JS_ATTACH_PROMPT = r"""
const input = $('Prompt Builder Input').first().json;
const fin = $input.first().json;
let llm = {};
try { llm = $('Extract Generated Prompt').first().json; } catch (e) { try { llm = $('LLM Unavailable → Reference Fallback').first().json; } catch (e2) {} }
// error: the worker's sanitized failure category only, never n8n's or the provider's error text
return [{ json: { ...input, prompt: fin.prompt, prompt_validation: fin.validation, prompt_generation: fin.generation || null,
  llm: { model: llm.llm_model || null, finish_reason: llm.llm_finish_reason || null, usage: llm.llm_usage || null,
         error: (fin.generation && fin.generation.failure) || null } } }];
"""

JS_BUILD_JOB_REQUEST = r"""
// Shape the request for POST /jobs (schemas/job-request.schema.json). The prompt text travels here only.
const inp = $input.first().json;
const seed = inp.job_seed;
const trig = seed.trigger || {};
return [{ json: {
  synthetic: Boolean(seed.synthetic),
  repository: seed.repository,
  pr: {
    number: Number(seed.pr.number), head_sha: seed.pr.head_sha, base_sha: seed.pr.base_sha, title: String(seed.pr.title || '').slice(0, 300),
    base_ref: seed.pr.base_ref, head_ref: seed.pr.head_ref, is_fork: Boolean(seed.pr.is_fork), url: seed.pr.url,
  },
  test_level: seed.test_level,
  trigger: { source: trig.source || 'manual', delivery_id: inp.delivery_id || trig.delivery_id || undefined, label: trig.label || undefined,
    requested_label: trig.requested_label || undefined, requested_by: trig.requested_by || undefined, run_url: trig.run_url || undefined },
  prompt: { text: inp.prompt.text, source: inp.prompt.source, sha256: inp.prompt.sha256, template_version: inp.prompt.template_version },
  context_sha256: inp.context && inp.context.context_sha256 ? inp.context.context_sha256 : undefined,
  n8n_execution_id: String($execution.id),
}}];
"""

JS_ATTACH_JOB = r"""
const input = $('Dispatcher Input').first().json;
const polled = $input.first().json;
return [{ json: { ...input, job: polled.job, result: polled.job.result } }];
"""

JS_INVALID_RESULT = r"""
// The worker/OpenCode returned something that fails the qa-result schema: report it honestly as INCONCLUSIVE.
const input = $('Result Input').first().json;
const check = $input.first().json;
const r = input.result || {};
const pr = input.job_seed.pr;
return [{ json: { result: {
  result_version: '1.0', synthetic: Boolean(input.job_seed.synthetic), job_id: (input.job && input.job.id) || 'unknown-job',
  repository: input.job_seed.repository,
  pr: { number: pr.number, title: pr.title, head_sha: pr.head_sha },
  environment: { name: input.job_seed.synthetic ? 'mock' : 'staging', base_url_redacted: 'unknown' },
  executed_by: { engine: input.job_seed.synthetic ? 'mock_executor' : 'opencode', mode: input.job_seed.synthetic ? 'mock' : 'live' },
  summary: { overall_status: 'INCONCLUSIVE', total: 0, passed: 0, failed: 0, blocked: 0, not_applicable: 0,
    executive_summary: 'Result rejected by schema/reconciliation validation (INVALID_RESULT).' },
  coverage_matrix: [], test_cases: [], defects: [],
  blockers: ['INVALID_RESULT: ' + (check.errors || []).slice(0, 10).join(' | ')].map(s => s.slice(0, 500)),
  recommendation: { verdict: 'INCONCLUSIVE', rationale: 'The worker returned a result that does not satisfy qa-result.schema.json.' },
}, result_validation: check } }];
"""

JS_SELECT_RESULT = r"""
const input = $('Result Input').first().json;
const cur = $input.first().json;
const result = cur.result || input.result;
const validation = cur.result_validation || cur;
return [{ json: { result, result_validation: { valid: Boolean(validation.valid), errors: validation.errors || [] } } }];
"""

JS_ATTACH_REPORT = r"""
const input = $('Result Input').first().json;
const rendered = $input.first().json;
const sel = $('Select Result for Rendering').first().json;
return [{ json: { ...input, result: sel.result, result_validation: sel.result_validation, report: rendered } }];
"""

JS_DISCORD_STATUS = r"""
// The worker (POST /notifications/discord) rendered, sanitized, split, delivered (with bounded retries / 429 handling)
// and recorded the notification on the job. mock vs real was decided by the worker policy, never here.
const input = $('Discord Input').first().json;
const resp = $input.first().json;
const code = Number(resp.statusCode || 0);
const b = resp.body || {};
const delivered = code === 200 && Boolean(b.delivered);
return [{ json: { ...input, discord: {
  mode: b.mode || 'unknown',
  status: b.status || (code === 200 ? 'unknown' : `worker_http_${code}`),
  delivered,
  skipped_duplicate: b.status === 'skipped_duplicate',
  messages: b.messages || 0, sent: b.sent || 0,
  results: b.results || [],
  overall_status: b.overall_status || null,
  validation_valid: typeof b.validation_valid === 'boolean' ? b.validation_valid : null,
  worker_status_code: code,
  error: code === 200 ? null : String(b.error || b.detail || 'worker error').slice(0, 300),
} } }];
"""

JS_COMMENT_STATUS = r"""
const input = $('Comment Input').first().json;
const resp = $input.first().json;
const code = Number(resp.statusCode || 0);
let mode = 'mock';
try { mode = $('Post to Mock GitHub Sink (dry-run)').first().json ? 'mock' : 'real'; } catch (e) { mode = 'real'; }
const body = resp.body || {};
return [{ json: { ...input, github_comment: { mode, status_code: code, delivered: code >= 200 && code < 300, status: (code >= 200 && code < 300) ? `${mode}_delivered` : `${mode}_failed`, comment_url: body.html_url || null } } }];
"""

JS_SANITIZE_ERROR = r"""
// Error Trigger payload → compact, redacted record (no stack traces, URLs or tokens leave this node).
const e = $input.first().json;
const redact = (s) => String(s || '').slice(0, 400).replace(/https?:\/\/\S+/g, '[url]')
  .replace(/gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w+|xai-[A-Za-z0-9]{20,}|eyJ[\w-]+\.[\w-]+\.[\w-]+/g, '[token]');
const exec = e.execution || {};
const err = (exec.error || e.error || {});
return [{ json: {
  kind: 'pipeline_error',
  detail: {
    workflow_name: (e.workflow || {}).name || null,
    workflow_id: (e.workflow || {}).id || null,
    execution_id: exec.id || null,
    last_node: exec.lastNodeExecuted || null,
    error_message: redact(err.message),
    error_node: (err.node && err.node.name) || null,
    mode: exec.mode || null,
    retry_of: exec.retryOf || null,
  },
}}];
"""


# ----------------------------------------------------------------------- workflows ---
def wf_webhook() -> dict[str, Any]:
    b = Builder("webhook")
    b.sticky("## PRQA 01 · GitHub Webhook Receiver\nReceives `pull_request` events, verifies the HMAC signature over the raw body, "
             "accepts `labeled` with the opt-in label (`PRQA_TRIGGER_LABEL`, default `to be test`) or `qa-test:*` on open non-draft PRs, "
             "cancels active jobs on `unlabeled` / `closed` / `synchronize`, dedupes by `X-GitHub-Delivery`, responds fast (202) and "
             "starts the Main Pipeline asynchronously.\n\nPaths: `POST /webhook/prqa/github` (GitHub) · `POST /webhook/prqa/actions` (GitHub Actions, see below)", 0, -1.6, 760, 190)
    b.sticky(REVIEW_NOTE, 3.0, -1.6, 620, 190, color=3)
    wh = b.node("GitHub Webhook (POST /prqa/github)", "n8n-nodes-base.webhook", 2,
                {"httpMethod": "POST", "path": "prqa/github", "responseMode": "responseNode", "options": {"rawBody": True}}, 0, 0,
                notes="Raw body kept for HMAC verification", webhookId="prqa-github-webhook")
    verify = b.node("Verify HMAC Signature", "n8n-nodes-base.code", 2, code(JS_VERIFY_SIGNATURE), 1, 0,
                    notes="X-Hub-Signature-256 == HMAC-SHA256(raw body, GITHUB_WEBHOOK_SECRET)")
    sig_ok = b.node("Signature Valid?", "n8n-nodes-base.if", 2.2, if_params(cond("={{ $json.signature_valid }}", "boolean", "true", single=True)), 2, 0)
    r401 = b.node("Respond 401 Invalid Signature", "n8n-nodes-base.respondToWebhook", 1.1,
                  {"respondWith": "json", "responseBody": '={{ JSON.stringify({ accepted: false, reason: $json.signature_reason }) }}',
                   "options": {"responseCode": 401}}, 3, 1.2)
    validate = b.node("Validate Event & Extract PR", "n8n-nodes-base.code", 2, code(JS_VALIDATE_EVENT), 3, 0,
                      notes="pull_request + labeled + qa-test:* + open + not draft")
    eligible = b.node("Eligible Event?", "n8n-nodes-base.if", 2.2, if_params(cond("={{ $json.eligible }}", "boolean", "true", single=True)), 4, 0)
    r200i = b.node("Respond 200 Ignored", "n8n-nodes-base.respondToWebhook", 1.1,
                   {"respondWith": "json", "responseBody": '={{ JSON.stringify({ accepted: false, ignored: true, reason_code: $json.reason_code, reason: $json.reason }) }}',
                    "options": {"responseCode": 200}}, 6, 2.0)
    lifecycle = b.node("Lifecycle Event? (label removed / closed / new SHA)", "n8n-nodes-base.if", 2.2,
                       if_params(cond("={{ $json.lifecycle ? 'yes' : 'no' }}", "string", "equals", "yes")), 5, 1.2)
    cancel = b.node("Cancel Active Jobs (worker /jobs/cancel)", "n8n-nodes-base.httpRequest", 4.2,
                    worker_http("POST", "/jobs/cancel", body="={{ JSON.stringify({ repository: $json.lifecycle.repository, pr_number: $json.lifecycle.pr_number, reason: $json.lifecycle.reason, except_head_sha: $json.lifecycle.except_head_sha, event: $json.reason_code, delivery_id: $json.delivery_id }) }}",
                                timeout_ms=15000, never_error=True, full_response=True), 6, 0.8,
                    notes="unlabeled/closed → cancel all active jobs of the PR · synchronize → cancel jobs of older SHAs (re-label to test the new one)")
    r200l = b.node("Respond 200 Lifecycle Handled", "n8n-nodes-base.respondToWebhook", 1.1,
                   {"respondWith": "json", "responseBody": "={{ JSON.stringify({ accepted: false, lifecycle: $('Validate Event & Extract PR').first().json.lifecycle, cancelled: ($json.body || {}).cancelled || [], worker_status: $json.statusCode }) }}",
                    "options": {"responseCode": 200}}, 7, 0.8)
    claim = b.node("Claim Delivery ID (worker /deliveries/claim)", "n8n-nodes-base.httpRequest", 4.2,
                   worker_http("POST", "/deliveries/claim", body="={{ JSON.stringify({ delivery_id: $json.delivery_id }) }}",
                               timeout_ms=15000, never_error=True, full_response=True), 5, 0,
                   notes="200 = first time · 409 = already seen", **RETRY2)
    dedupe = b.node("Dedupe by Delivery ID", "n8n-nodes-base.code", 2, code(JS_DEDUPE), 6, 0, notes="Layer 1 of 2 (worker enforces repo#pr@sha:level on POST /jobs)")
    dup = b.node("Duplicate Delivery?", "n8n-nodes-base.if", 2.2, if_params(cond("={{ $json.duplicate }}", "boolean", "true", single=True)), 7, 0)
    r200d = b.node("Respond 200 Duplicate", "n8n-nodes-base.respondToWebhook", 1.1,
                   {"respondWith": "json", "responseBody": '={{ JSON.stringify({ accepted: false, duplicate: true, delivery_id: $json.delivery_id, first_seen_at: $json.first_seen_at }) }}',
                    "options": {"responseCode": 200}}, 8, -1.2)
    r202 = b.node("Respond 202 Accepted", "n8n-nodes-base.respondToWebhook", 1.1,
                  {"respondWith": "json", "responseBody": '={{ JSON.stringify({ accepted: true, delivery_id: $json.delivery_id, repository: $json.job_seed.repository, pr: $json.job_seed.pr.number, test_level: $json.job_seed.test_level, synthetic: $json.job_seed.synthetic, automation_mode: $json.automation_mode }) }}',
                   "options": {"responseCode": 202}}, 8, 0)
    start = b.node("Start Main Pipeline (async)", "n8n-nodes-base.executeWorkflow", 1.1, exec_wf("main", wait=False), 9, 0,
                   notes="→ PRQA 00 · Main Pipeline (policy gate runs there)")
    # ---- GitHub Actions entry: same receiver workflow, dedicated path and contract (docs/GITHUB_ACTIONS_ONBOARDING.md) ----
    b.sticky("## GitHub Actions entry — `POST /webhook/prqa/actions`\nCalled by the copy-paste workflow `.github/workflows/prqa.yml`. "
             "Headers `X-PRQA-Timestamp` + `X-PRQA-Signature` (HMAC-SHA256 over `<timestamp>.<raw body>` with `PRQA_WEBHOOK_SECRET`). "
             "The **worker** verifies the signature in constant time, enforces the ±300 s window, validates the payload, derives the "
             "idempotency key `gha:<repo>#<pr>@<sha>:<level>` and claims it (replay protection), rate-limits, and returns the `job_seed`. "
             "HTTP 202 means *accepted*, never *passed*: the result arrives on Discord after the job and validation complete.",
             0, 2.4, 1100, 200, color=5)
    awh = b.node("GitHub Actions Webhook (POST /prqa/actions)", "n8n-nodes-base.webhook", 2,
                 {"httpMethod": "POST", "path": "prqa/actions", "responseMode": "responseNode", "options": {"rawBody": True}}, 0, 4,
                 notes="Raw body kept for HMAC verification (worker)", webhookId="prqa-actions-webhook")
    aprep = b.node("Prepare Actions Intake (raw body + X-PRQA headers)", "n8n-nodes-base.code", 2, code(JS_PREPARE_ACTIONS_INTAKE), 1, 4,
                   notes="no secret, no interpretation of the body here")
    aintake = b.node("Verify, Validate & Claim (worker /webhook/actions/intake)", "n8n-nodes-base.httpRequest", 4.2,
                     worker_http("POST", "/webhook/actions/intake", body="={{ JSON.stringify($json) }}", timeout_ms=20000,
                                 never_error=True, full_response=True), 2, 4,
                     notes="202 accepted · 200 duplicate/blocked · 401 bad signature/timestamp · 403 repo not registered/mismatch · 409 replayed · 413 too large · 422 invalid · 429 rate-limited · 503 credential missing")
    aroute = b.node("Route Intake Outcome", "n8n-nodes-base.switch", 3.2,
                    switch_params("={{ String(($json.body || {}).outcome || 'error') }}",
                                  [("accepted", "accepted"), ("duplicate", "duplicate"), ("unauthorized", "unauthorized"),
                                   ("forbidden", "forbidden"), ("rejected", "rejected"), ("rate_limited", "rate_limited"),
                                   ("cancelled", "cancelled"), ("blocked", "blocked"), ("replayed", "replayed"),
                                   ("too_large", "too_large"), ("unavailable", "unavailable")], fallback="extra"), 3, 4)
    err_body = ("={{ JSON.stringify({ accepted: false, outcome: ($json.body || {}).outcome || 'error', reason_code: ($json.body || {}).reason_code || 'WORKER_ERROR', "
                "reason: ($json.body || {}).reason || ('worker returned HTTP ' + $json.statusCode), errors: ($json.body || {}).errors || [], "
                "expected_key: ($json.body || {}).expected_key || null, retry_after_s: ($json.body || {}).retry_after_s || null, receiver_execution_id: $execution.id }) }}")
    apipe = b.node("Build Pipeline Input (Actions)", "n8n-nodes-base.code", 2, code(JS_ACTIONS_PIPELINE_INPUT), 4, 3.0)
    a202 = b.node("Respond 202 Accepted (Actions)", "n8n-nodes-base.respondToWebhook", 1.1,
                  {"respondWith": "json", "responseBody": "={{ JSON.stringify({ accepted: true, outcome: 'accepted', idempotency_key: $json.idempotency_key, "
                   "repository: $json.job_seed.repository, pr: $json.job_seed.pr.number, head_sha: $json.job_seed.pr.head_sha, test_level: $json.job_seed.test_level, "
                   "synthetic: $json.job_seed.synthetic, automation_mode: $json.automation_mode, policy_preview: $json.policy_preview, receiver_execution_id: $json.webhook_execution_id, "
                   "message: 'Request accepted. PRQA will run the policy gate, the QA job, result validation and Discord reporting. HTTP 202 does not mean the test passed.' }) }}",
                   "options": {"responseCode": 202}}, 5, 3.0)
    a200 = b.node("Respond 200 Duplicate (Actions)", "n8n-nodes-base.respondToWebhook", 1.1,
                  {"respondWith": "json", "responseBody": "={{ JSON.stringify({ accepted: false, outcome: 'duplicate', reason_code: $json.body.reason_code, reason: $json.body.reason, "
                   "idempotency_key: $json.body.idempotency_key, first_seen_at: $json.body.first_seen_at || null, job_id: $json.body.job_id || null, receiver_execution_id: $execution.id }) }}",
                   "options": {"responseCode": 200}}, 4, 3.8)
    a401 = b.node("Respond 401 Unauthorized (Actions)", "n8n-nodes-base.respondToWebhook", 1.1,
                  {"respondWith": "json", "responseBody": err_body, "options": {"responseCode": 401}}, 4, 4.6)
    a403 = b.node("Respond 403 Repository Not Registered (Actions)", "n8n-nodes-base.respondToWebhook", 1.1,
                  {"respondWith": "json", "responseBody": err_body, "options": {"responseCode": 403}}, 4, 5.4,
                  notes="onboarding = manual edit of config/repositories.yaml")
    a422 = b.node("Respond 422 Invalid Request (Actions)", "n8n-nodes-base.respondToWebhook", 1.1,
                  {"respondWith": "json", "responseBody": err_body, "options": {"responseCode": 422}}, 4, 6.2,
                  notes="schema / consistency / PR not open / draft / key mismatch")
    a429 = b.node("Respond 429 Rate Limited (Actions)", "n8n-nodes-base.respondToWebhook", 1.1,
                  {"respondWith": "json", "responseBody": err_body, "options": {"responseCode": 429}}, 4, 7.0)
    a503 = b.node("Respond 503 Worker Unavailable (Actions)", "n8n-nodes-base.respondToWebhook", 1.1,
                  {"respondWith": "json", "responseBody": err_body, "options": {"responseCode": 503}}, 4, 7.8)
    a200c = b.node("Respond 200 Lifecycle Cancelled (Actions)", "n8n-nodes-base.respondToWebhook", 1.1,
                   {"respondWith": "json", "responseBody": "={{ JSON.stringify({ accepted: false, outcome: 'cancelled', reason_code: $json.body.reason_code, reason: $json.body.reason, cancelled: $json.body.cancelled || [], receiver_execution_id: $execution.id }) }}",
                    "options": {"responseCode": 200}}, 4, 8.6, notes="poller/Actions observed the label removed or PR closed → active jobs cancelled, nothing started")
    # per-repository `to be test` connection dispatch (schema 2.0, docs/REPOSITORY_CONNECTION_SETUP.md): extra outcomes
    a200b = b.node("Respond 200 Blocked by Policy (Actions)", "n8n-nodes-base.respondToWebhook", 1.1,
                   {"respondWith": "json", "responseBody": "={{ JSON.stringify({ accepted: false, outcome: 'blocked', reason_code: $json.body.reason_code, reason: $json.body.reason, "
                    "idempotency_key: $json.body.idempotency_key || null, automation_mode: $json.body.automation_mode || null, policy_preview: $json.body.policy_preview || null, "
                    "receiver_execution_id: $execution.id }) }}", "options": {"responseCode": 200}}, 4, 9.4,
                   notes="dispatch recorded, never executed (review-only or policy block)")
    a409 = b.node("Respond 409 Delivery Replayed (Actions)", "n8n-nodes-base.respondToWebhook", 1.1,
                  {"respondWith": "json", "responseBody": err_body, "options": {"responseCode": 409}}, 4, 10.2)
    a413 = b.node("Respond 413 Payload Too Large (Actions)", "n8n-nodes-base.respondToWebhook", 1.1,
                  {"respondWith": "json", "responseBody": err_body, "options": {"responseCode": 413}}, 4, 11.0)
    a503c = b.node("Respond 503 Context Credential Missing (Actions)", "n8n-nodes-base.respondToWebhook", 1.1,
                   {"respondWith": "json", "responseBody": err_body, "options": {"responseCode": 503}}, 4, 11.8,
                   notes="GitHub read credential missing: fail closed, nothing started")
    b.chain(awh, aprep, aintake, aroute)
    b.connect(aroute, apipe, 0)
    b.connect(aroute, a200, 1)
    b.connect(aroute, a401, 2)
    b.connect(aroute, a403, 3)
    b.connect(aroute, a422, 4)
    b.connect(aroute, a429, 5)
    b.connect(aroute, a200c, 6)
    b.connect(aroute, a200b, 7)
    b.connect(aroute, a409, 8)
    b.connect(aroute, a413, 9)
    b.connect(aroute, a503c, 10)
    b.connect(aroute, a503, 11)
    b.chain(apipe, a202, start)

    b.chain(wh, verify, sig_ok)
    b.connect(sig_ok, validate, 0)
    b.connect(sig_ok, r401, 1)
    b.connect(validate, eligible)
    b.connect(eligible, claim, 0)
    b.connect(eligible, lifecycle, 1)
    b.connect(lifecycle, cancel, 0)
    b.connect(lifecycle, r200i, 1)
    b.chain(cancel, r200l)
    b.chain(claim, dedupe, dup)
    b.connect(dup, r200d, 0)
    b.connect(dup, r202, 1)
    b.connect(r202, start)
    return b.build()


def wf_main() -> dict[str, Any]:
    b = Builder("main")
    b.sticky("## PRQA 00 · Main Pipeline\nOne orchestration for every repository. Stages run as sub-workflows so each can be reviewed and tested alone:\n"
             "**Policy Gate → 02 Context → 03 LLM Prompt → 04 OpenCode Job → 05 Validate & Report → 06 Discord + 07 GitHub comment**.\n\n"
             "Error outputs of every stage go to *Record Stage Error*; unhandled errors go to *PRQA 08 · Error Handler* (workflow setting).",
             0, -2.2, 900, 190)
    b.sticky(REVIEW_NOTE, 3.6, -2.2, 620, 190, color=3)
    b.sticky("### ▶ Manual synthetic test\nClick **Test workflow** with this trigger selected to run the whole pipeline against the "
             "synthetic fixture PR (#101 on `synthetic-org/sample-orders-api`). All external calls go to `prqa-mocks`.", 0, 1.6, 560, 150, color=4)
    trig = b.node("Pipeline Input (Execute Workflow Trigger)", "n8n-nodes-base.executeWorkflowTrigger", 1.1, {"inputSource": "passthrough"}, 0, 0)
    manual = b.node("Manual Synthetic Test Trigger", "n8n-nodes-base.manualTrigger", 1, {}, 0, 1)
    seed = b.node("Load Synthetic Fixture (job_seed)", "n8n-nodes-base.code", 2, code(JS_SYNTHETIC_SEED), 1, 1, notes="synthetic-org/sample-orders-api #101")
    norm = b.node("Normalize Pipeline Input", "n8n-nodes-base.code", 2, code(JS_NORMALIZE_INPUT), 2, 0)
    cfg = b.node("Load Policy & Registry (worker /config)", "n8n-nodes-base.httpRequest", 4.2, worker_http("GET", "/config", timeout_ms=15000), 3, 0,
                 notes="AUTOMATION_MODE, REAL_* flags, endpoints (mock vs real), registry", **RETRY)
    gate = b.node("Policy Gate: Review-Only & Repository", "n8n-nodes-base.code", 2, code(JS_POLICY_GATE), 4, 0,
                  notes="route = synthetic | allowed | blocked")
    sw = b.node("Execution Allowed?", "n8n-nodes-base.switch", 3.2,
                switch_params("={{ $json.decision.route }}", [("synthetic", "synthetic (mock)"), ("allowed", "allowed (live)"), ("blocked", "blocked")]), 5, 0)
    blocked_log = b.node("Record Blocked Decision (worker /events)", "n8n-nodes-base.httpRequest", 4.2,
                         worker_http("POST", "/events", body='={{ JSON.stringify({ kind: "blocked_by_policy", detail: { decision: $json.decision, repository: $json.job_seed.repository, pr: $json.job_seed.pr.number, delivery_id: $json.delivery_id, execution_id: $execution.id } }) }}',
                                     timeout_ms=15000, never_error=True), 6, 1.6, **RETRY2)
    blocked_notify = b.node("Notify Blocked Decision (worker → Discord)", "n8n-nodes-base.httpRequest", 4.2,
                            worker_http("POST", "/notifications/discord",
                                        body="={{ JSON.stringify({ kind: 'blocked', job_seed: $('Policy Gate: Review-Only & Repository').first().json.job_seed, "
                                             "decision: $('Policy Gate: Review-Only & Repository').first().json.decision, "
                                             "delivery_id: $('Policy Gate: Review-Only & Repository').first().json.delivery_id, "
                                             "pipeline: { execution_id: $execution.id, entry_point: $('Policy Gate: Review-Only & Repository').first().json.entry_point } }) }}",
                                        timeout_ms=120000, never_error=True), 7, 1.6,
                            notes="BLOCKED notice: mock sink in review-only; real only when the worker policy allows")
    blocked_end = b.node("Blocked: Review-Only / Repository Disabled (end)", "n8n-nodes-base.noOp", 1, {}, 8, 1.6,
                         notes="No test was executed for this PR")
    ctx = b.node("Stage 02 · Collect PR Context", "n8n-nodes-base.executeWorkflow", 1.1, exec_wf("context"), 6.3, -0.5, **ERR_OUT)
    prm = b.node("Stage 03 · Build QA Prompt (LLM)", "n8n-nodes-base.executeWorkflow", 1.1, exec_wf("prompt"), 7.3, -0.5, **ERR_OUT)
    job = b.node("Stage 04 · Dispatch OpenCode Job", "n8n-nodes-base.executeWorkflow", 1.1, exec_wf("dispatch"), 8.3, -0.5, **ERR_OUT)
    val = b.node("Stage 05 · Validate Result & Render Report", "n8n-nodes-base.executeWorkflow", 1.1, exec_wf("validate"), 9.3, -0.5, **ERR_OUT)
    dis = b.node("Stage 06 · Discord Report", "n8n-nodes-base.executeWorkflow", 1.1, exec_wf("discord"), 10.3, -1.1, **ERR_OUT)
    com = b.node("Stage 07 · GitHub PR Comment", "n8n-nodes-base.executeWorkflow", 1.1, exec_wf("comment"), 10.3, 0.1, **ERR_OUT)
    merge = b.node("Merge Reporting Results", "n8n-nodes-base.merge", 3, {"mode": "combine", "combineBy": "combineByPosition", "options": {}}, 11.3, -0.5)
    final = b.node("Final Summary", "n8n-nodes-base.set", 3.4, set_params([
        ("pipeline_status", "completed", "string"),
        ("repository", "={{ $json.job_seed.repository }}", "string"),
        ("pr_number", "={{ $json.job_seed.pr.number }}", "number"),
        ("synthetic", "={{ $json.job_seed.synthetic }}", "boolean"),
        ("decision", "={{ $json.decision }}", "object"),
        ("job_id", "={{ $json.job ? $json.job.id : null }}", "string"),
        ("overall_status", "={{ $json.result ? $json.result.summary.overall_status : null }}", "string"),
        ("summary_line", "={{ $json.report ? $json.report.summary_line : null }}", "string"),
        ("discord", "={{ $json.discord || null }}", "object"),
        ("github_comment", "={{ $json.github_comment || null }}", "object"),
        ("prompt_source", "={{ $json.prompt ? $json.prompt.source : null }}", "string"),
        ("entry_point", "={{ $json.entry_point || null }}", "string"),
        ("trigger_source", "={{ $json.job_seed.trigger ? ($json.job_seed.trigger.source || null) : null }}", "string"),
        ("actions_run_url", "={{ $json.job_seed.trigger ? ($json.job_seed.trigger.run_url || null) : null }}", "string"),
        ("result_validation", "={{ $json.result_validation || null }}", "object"),
    ], include_other=False), 12.3, -0.5)
    err = b.node("Stage Failed → Sanitize", "n8n-nodes-base.code", 2, code(JS_STAGE_ERROR), 9.3, 1.3)
    err_log = b.node("Record Stage Error (worker /events)", "n8n-nodes-base.httpRequest", 4.2,
                     worker_http("POST", "/events", body="={{ JSON.stringify($json) }}", timeout_ms=15000, never_error=True), 10.3, 1.3, **RETRY2)
    err_end = b.node("Pipeline Failed (end)", "n8n-nodes-base.noOp", 1, {}, 11.3, 1.3)

    b.connect(trig, norm)
    b.chain(manual, seed, norm)
    b.chain(norm, cfg, gate, sw)
    b.connect(sw, ctx, 0)
    b.connect(sw, ctx, 1)
    b.connect(sw, blocked_log, 2)
    b.chain(blocked_log, blocked_notify, blocked_end)
    b.chain(ctx, prm, job, val)
    b.connect(val, dis)
    b.connect(val, com)
    b.connect(dis, merge, 0, 0)
    b.connect(com, merge, 0, 1)
    b.connect(merge, final)
    for stage in (ctx, prm, job, val, dis, com):
        b.connect(stage, err, 1)
    b.chain(err, err_log, err_end)
    return b.build()


def wf_context() -> dict[str, Any]:
    b = Builder("context")
    b.sticky("## PRQA 02 · PR Context Collector\nReads PR metadata, changed files and the unified diff from the GitHub API base handed over by the "
             "worker (`endpoints.github_api_base`: **mock** in review-only, real only in live mode), then asks the worker to build the sanitized, "
             "redacted, size-limited context package (`POST /context/build`). PR text is labelled UNTRUSTED.", 0, -1.6, 820, 170)
    trig = b.node("Context Input", "n8n-nodes-base.executeWorkflowTrigger", 1.1, {"inputSource": "passthrough"}, 0, 0)
    resolve = b.node("Resolve GitHub API Endpoint", "n8n-nodes-base.set", 3.4, set_params([
        ("github_api_base", "={{ $json.endpoints.github_api_base }}", "string"),
        ("github_api_mode", "={{ $json.endpoints.github_api_mode }}", "string"),
        ("github_auth_header", "={{ $json.endpoints.github_api_mode === 'real' && $env.GITHUB_TOKEN ? 'Bearer ' + $env.GITHUB_TOKEN : 'Bearer mock-token-review-only' }}", "string"),
    ]), 1, 0, notes="mock in review-only; real token only in live mode")
    gh_headers = lambda accept: [  # noqa: E731
        {"name": "Accept", "value": accept},
        {"name": "Authorization", "value": "={{ $('Resolve GitHub API Endpoint').first().json.github_auth_header }}"},
        {"name": "User-Agent", "value": "prqa-n8n/0.1.0"},
        {"name": "X-GitHub-Api-Version", "value": "2022-11-28"},
    ]
    base = "={{ $('Resolve GitHub API Endpoint').first().json.github_api_base }}/repos/{{ $('Resolve GitHub API Endpoint').first().json.job_seed.repository }}/pulls/{{ $('Resolve GitHub API Endpoint').first().json.job_seed.pr.number }}"
    meta = b.node("Fetch PR Metadata", "n8n-nodes-base.httpRequest", 4.2, http("GET", base, headers=gh_headers("application/vnd.github+json")), 2, 0, **RETRY)
    files = b.node("Fetch Changed Files", "n8n-nodes-base.httpRequest", 4.2, http("GET", base + "/files?per_page=100", headers=gh_headers("application/vnd.github+json")), 3, 0, **RETRY)
    diff = b.node("Fetch Unified Diff", "n8n-nodes-base.httpRequest", 4.2, http("GET", base, headers=gh_headers("application/vnd.github.diff"), text=True), 4, 0, **RETRY)
    asm = b.node("Assemble Raw PR Data", "n8n-nodes-base.code", 2, code(JS_ASSEMBLE_RAW_PR), 5, 0)
    build = b.node("Sanitize & Build Context Package (worker)", "n8n-nodes-base.httpRequest", 4.2,
                   worker_http("POST", "/context/build", body="={{ JSON.stringify($json) }}", timeout_ms=60000), 6, 0,
                   notes="redaction, truncation, changed OpenAPI operations, context_sha256", **RETRY2)
    attach = b.node("Attach Context to Pipeline Data", "n8n-nodes-base.code", 2, code(JS_ATTACH_CONTEXT), 7, 0)
    b.chain(trig, resolve, meta, files, diff, asm, build, attach)
    return b.build()


def wf_prompt() -> dict[str, Any]:
    b = Builder("prompt")
    b.sticky("## PRQA 03 · Prompt Builder (LLM)\nThe worker renders the chat-completion request from `prompts/llm/*.v2.md` + the canonical reference prompt "
             "(`prompts/reference/Senior_QA_Reference_Prompt.md`) + the sanitized context. The prompt LLM (active provider: direct Groq `openai/gpt-oss-120b`, "
             "no OpenRouter fallback) rewrites the reference for this PR. n8n holds no provider key: a real request goes to the **model proxy** "
             "(`prqa-model-proxy:8089`, prompt credential PRQA_MODEL_PROXY_PROMPT_TOKEN, shared provider budget, no tools); without the credential "
             "no request is sent. The output is validated "
             "(structure, required sections, no secrets, no injected phrases); on any failure or rejection the "
             "**deterministic reference-derived fallback** is used. The OpenCode result contract (section 15) is always appended by the worker.",
             0, -1.8, 900, 200)
    trig = b.node("Prompt Builder Input", "n8n-nodes-base.executeWorkflowTrigger", 1.1, {"inputSource": "passthrough"}, 0, 0)
    build = b.node("Build LLM Request from Reference Template (worker)", "n8n-nodes-base.httpRequest", 4.2,
                   worker_http("POST", "/prompts/build", body='={{ JSON.stringify({ repository: $json.job_seed.repository, pr_number: $json.job_seed.pr.number, test_level: $json.job_seed.test_level, synthetic: $json.job_seed.synthetic, context: $json.context }) }}',
                               timeout_ms=30000), 1, 0, **RETRY2)
    keyed = b.node("Provider Key Present? (or mock)", "n8n-nodes-base.if", 2.2,
                   if_params(cond("={{ $json.prompt_llm_api_mode !== 'real' || (" + LLM_PROXY_BASE_OK + " && Boolean(" + LLM_KEY_EXPR + ")) }}",
                                  "boolean", "true", single=True)), 1.5, 0,
                   notes="real mode without the proxy prompt credential, or with a non-proxy base: no request is sent; the reference "
                         "fallback is used (missing_credential)")
    no_key = b.node("Provider Key Missing → Reference Fallback", "n8n-nodes-base.set", 3.4, set_params([
        ("generated_text", "", "string"),
        ("llm_error", "missing_credential", "string"),
        ("llm_model", "", "string"),
    ], include_other=False), 2, 1.4, notes="never reaches the provider; worker records generation.failure=missing_credential")
    grok = b.node("Call Prompt LLM via Model Proxy (budgeted chat/completions)", "n8n-nodes-base.httpRequest", 4.2,
                  http("POST", "={{ $json.prompt_llm_api_base }}/chat/completions", body="={{ JSON.stringify($json.llm_request) }}",
                       headers=[{"name": "Authorization", "value": "={{ 'Bearer ' + ($json.prompt_llm_api_mode === 'real' ? (" + LLM_KEY_EXPR + " || '') : 'mock-key-review-only') }}"},
                                {"name": "Content-Type", "value": "application/json"},
                                {"name": "X-PRQA-Job-Id", "value": LLM_JOB_REF_EXPR},
                                {"name": "X-Title", "value": "PR QA Pilot"}], timeout_ms=PROMPT_LLM_TIMEOUT_MS), 2, 0,
                  notes="mock endpoint in review-only; in live mode the model proxy (never the provider): prompt credential, shared provider "
                        "budget (a 429 = budget refusal), locked contract, no tools. Single attempt (no retry): a timeout or error uses the "
                        "reference fallback.", retryOnFail=False, **ERR_OUT)
    extract = b.node("Extract Generated Prompt", "n8n-nodes-base.code", 2, code(JS_EXTRACT_LLM), 3, -0.5)
    fallback = b.node("LLM Unavailable → Reference Fallback", "n8n-nodes-base.set", 3.4, set_params([
        ("generated_text", "", "string"),
        ("llm_error", "={{ ($json.error && $json.error.message) || 'prompt LLM call failed' }}", "string"),
        ("llm_http_status", "={{ ($json.error && ($json.error.httpCode || $json.error.status)) || '' }}", "string"),
        ("llm_model", "", "string"),
    ], include_other=False), 3, 0.7, notes="generated_text = null → worker uses the reference-derived prompt; the error text is only "
                                          "classified by the worker (generation.failure), never stored")
    finalize = b.node("Validate Prompt / Fallback to Reference (worker)", "n8n-nodes-base.httpRequest", 4.2,
                      worker_http("POST", "/prompts/finalize", body='={{ JSON.stringify({ generated_text: $json.generated_text || null, finish_reason: $json.llm_finish_reason || null, llm_error: $json.llm_error || null, http_status: $json.llm_http_status || null, response_shape: $json.llm_response_shape || null, repository: $(\'Prompt Builder Input\').first().json.job_seed.repository, pr_number: $(\'Prompt Builder Input\').first().json.job_seed.pr.number, test_level: $(\'Prompt Builder Input\').first().json.job_seed.test_level, synthetic: $(\'Prompt Builder Input\').first().json.job_seed.synthetic, context: $(\'Prompt Builder Input\').first().json.context }) }}',
                                  timeout_ms=30000), 4, 0, notes="appends section 15 (qa-result.json contract)", **RETRY2)
    src = b.node("Prompt Source?", "n8n-nodes-base.if", 2.2, if_params(cond("={{ $json.prompt.source }}", "string", "equals", "llm")), 5, 0)
    p_grok = b.node("Prompt: LLM-generated ✓", "n8n-nodes-base.noOp", 1, {}, 6, -0.5)
    p_ref = b.node("Prompt: Reference fallback", "n8n-nodes-base.noOp", 1, {}, 6, 0.7)
    attach = b.node("Attach Prompt to Pipeline Data", "n8n-nodes-base.code", 2, code(JS_ATTACH_PROMPT), 7, 0)
    b.chain(trig, build, keyed)
    b.connect(keyed, grok, 0)
    b.connect(keyed, no_key, 1)
    b.connect(no_key, finalize)
    b.connect(grok, extract, 0)
    b.connect(grok, fallback, 1)
    b.connect(extract, finalize)
    b.connect(fallback, finalize)
    b.connect(finalize, src)
    b.connect(src, p_grok, 0)
    b.connect(src, p_ref, 1)
    b.connect(p_grok, attach)
    b.connect(p_ref, attach)
    return b.build()


def wf_dispatch() -> dict[str, Any]:
    b = Builder("dispatch")
    b.sticky("## PRQA 04 · OpenCode Job Dispatcher\n`POST /jobs` on the worker: schema validation → **policy gate** (review-only, registry, approval, fork, label) → "
             "**dedupe** (delivery id + `repo#pr@sha:level`) → queued. The worker executes: synthetic → mock executor (fixture result); real → isolated OpenCode "
             "sandbox (live mode only; `opencode_runner.py`). n8n polls every 10 s, retries a failed job once, and stops with a visible error when the worker rejects the job.",
             0, -1.8, 940, 200)
    trig = b.node("Dispatcher Input", "n8n-nodes-base.executeWorkflowTrigger", 1.1, {"inputSource": "passthrough"}, 0, 0)
    req = b.node("Build Job Request", "n8n-nodes-base.code", 2, code(JS_BUILD_JOB_REQUEST), 1, 0, notes="schemas/job-request.schema.json")
    submit = b.node("Submit Job to Worker (policy gate + dedupe)", "n8n-nodes-base.httpRequest", 4.2,
                    worker_http("POST", "/jobs", body="={{ JSON.stringify($json) }}", timeout_ms=30000, never_error=True, full_response=True), 2, 0,
                    notes="201 accepted · 403 policy rejected · 409 duplicate · 422 invalid", **RETRY2)
    sw = b.node("Job Accepted?", "n8n-nodes-base.switch", 3.2,
                switch_params("={{ String($json.statusCode) }}", [("201", "accepted"), ("409", "duplicate"), ("403", "rejected by policy")], fallback="extra"), 3, 0)
    stop_dup = b.node("Stop: Duplicate Job (already active)", "n8n-nodes-base.stopAndError", 1,
                      {"errorMessage": "={{ 'Duplicate job: ' + JSON.stringify($json.body) }}"}, 4, 1.2)
    stop_rej = b.node("Stop: Rejected by Worker Policy", "n8n-nodes-base.stopAndError", 1,
                      {"errorMessage": "={{ 'Worker policy rejected the job: ' + (($json.body.decision || {}).reason_code || 'UNKNOWN') + ' — ' + (($json.body.decision || {}).reason || '') }}"}, 4, 2.0,
                      notes="REVIEW_ONLY_MODE lands here for any real repository")
    stop_other = b.node("Stop: Worker Error / Invalid Request", "n8n-nodes-base.stopAndError", 1,
                        {"errorMessage": "={{ 'Worker returned HTTP ' + $json.statusCode + ': ' + JSON.stringify($json.body).slice(0, 500) }}"}, 4, 2.8)
    jid = b.node("Extract Job ID", "n8n-nodes-base.set", 3.4, set_params([
        ("job_id", "={{ $json.body.job.id }}", "string"),
        ("execution_mode", "={{ $json.body.job.execution_mode }}", "string"),
        ("decision", "={{ $json.body.decision }}", "object"),
    ], include_other=False), 4, -0.6)
    wait = b.node("Wait 10 s", "n8n-nodes-base.wait", 1.1, {"amount": 10, "unit": "seconds"}, 5, -0.6, webhookId="prqa-dispatch-wait")
    poll = b.node("Poll Job Status (worker)", "n8n-nodes-base.httpRequest", 4.2,
                  worker_http("GET", "/jobs/{{ $('Extract Job ID').first().json.job_id }}", timeout_ms=15000), 6, -0.6, **RETRY)
    fin = b.node("Job Finished?", "n8n-nodes-base.switch", 3.2,
                 switch_params("={{ $json.job.status }}", [("succeeded", "succeeded"), ("failed", "failed"), ("cancelled", "cancelled")], fallback="extra"), 7, -0.6,
                 notes="fallback output = queued/running → keep polling · cancelled = label removed / PR closed / superseded")
    stop_cancelled = b.node("Stop: Job Cancelled (label removed / PR closed / superseded)", "n8n-nodes-base.stopAndError", 1,
                            {"errorMessage": "={{ 'Job ' + $json.job.id + ' was cancelled: ' + (($json.job.cancel || {}).reason || 'unknown reason') }}"}, 8, 1.6,
                            notes="no result is reported for a cancelled job")
    limit = b.node("Poll Limit Reached? (> 360 polls ≈ 60 min)", "n8n-nodes-base.if", 2.2,
                   if_params(cond("={{ $runIndex }}", "number", "gt", 360)), 8, 0.6)
    stop_timeout = b.node("Stop: Poll Timeout", "n8n-nodes-base.stopAndError", 1,
                          {"errorMessage": "={{ 'Job ' + $('Extract Job ID').first().json.job_id + ' did not finish within the polling window' }}"}, 9, 1.4)
    retried = b.node("Already Retried by n8n?", "n8n-nodes-base.if", 2.2,
                     if_params(cond("={{ $json.job.notifications && $json.job.notifications.n8n_retry ? 'yes' : 'no' }}", "string", "equals", "yes")), 8, -0.2)
    stop_failed = b.node("Stop: Job Failed After Retries", "n8n-nodes-base.stopAndError", 1,
                         {"errorMessage": "={{ 'Job ' + $json.job.id + ' failed: ' + JSON.stringify($json.job.error).slice(0, 400) }}"}, 9, -0.2)
    retry = b.node("Request Manual Retry (worker)", "n8n-nodes-base.httpRequest", 4.2,
                   worker_http("POST", "/jobs/{{ $json.job.id }}/retry", body="={{ JSON.stringify({}) }}", timeout_ms=15000), 9, 0.6, **RETRY2)
    mark = b.node("Mark n8n Retry (worker)", "n8n-nodes-base.httpRequest", 4.2,
                  worker_http("POST", "/jobs/{{ $json.job.id }}/notification", body='={{ JSON.stringify({ channel: "n8n_retry", status: "requested", detail: { execution_id: $execution.id } }) }}', timeout_ms=15000), 10, 0.6, **RETRY2)
    attach = b.node("Attach Job Result to Pipeline Data", "n8n-nodes-base.code", 2, code(JS_ATTACH_JOB), 8, -1.2)

    b.chain(trig, req, submit, sw)
    b.connect(sw, jid, 0)
    b.connect(sw, stop_dup, 1)
    b.connect(sw, stop_rej, 2)
    b.connect(sw, stop_other, 3)
    b.chain(jid, wait, poll, fin)
    b.connect(fin, attach, 0)
    b.connect(fin, retried, 1)
    b.connect(fin, stop_cancelled, 2)
    b.connect(fin, limit, 3)
    b.connect(limit, stop_timeout, 0)
    b.connect(limit, wait, 1)
    b.connect(retried, stop_failed, 0)
    b.connect(retried, retry, 1)
    b.chain(retry, mark)
    b.connect(mark, wait)
    return b.build()


def wf_validate() -> dict[str, Any]:
    b = Builder("validate")
    b.sticky("## PRQA 05 · Result Validation & Report\n`POST /results/validate` checks `qa-result.schema.json` **and** count reconciliation "
             "(summary vs test_cases vs coverage matrix, defect references, verdict consistency). Invalid results become an honest `INCONCLUSIVE` placeholder. "
             "`POST /reports/render` produces the Markdown report, the Discord embed and the GitHub comment — all redacted, all marked SYNTHETIC when applicable.",
             0, -1.6, 900, 180)
    trig = b.node("Result Input", "n8n-nodes-base.executeWorkflowTrigger", 1.1, {"inputSource": "passthrough"}, 0, 0)
    val = b.node("Validate Result Schema & Counts (worker)", "n8n-nodes-base.httpRequest", 4.2,
                 worker_http("POST", "/results/validate", body="={{ JSON.stringify({ result: $json.result }) }}", timeout_ms=30000), 1, 0, **RETRY2)
    ok = b.node("Result Valid?", "n8n-nodes-base.if", 2.2, if_params(cond("={{ $json.valid }}", "boolean", "true", single=True)), 2, 0)
    good = b.node("Result Valid ✓", "n8n-nodes-base.noOp", 1, {}, 3, -0.5)
    bad = b.node("Build INVALID_RESULT Placeholder (INCONCLUSIVE)", "n8n-nodes-base.code", 2, code(JS_INVALID_RESULT), 3, 0.7)
    select = b.node("Select Result for Rendering", "n8n-nodes-base.code", 2, code(JS_SELECT_RESULT), 4, 0)
    render = b.node("Render Report / Discord / GitHub Comment (worker)", "n8n-nodes-base.httpRequest", 4.2,
                    worker_http("POST", "/reports/render", body="={{ JSON.stringify({ result: $json.result }) }}", timeout_ms=30000), 5, 0, **RETRY2)
    attach = b.node("Attach Report to Pipeline Data", "n8n-nodes-base.code", 2, code(JS_ATTACH_REPORT), 6, 0)
    b.chain(trig, val, ok)
    b.connect(ok, good, 0)
    b.connect(ok, bad, 1)
    b.connect(good, select)
    b.connect(bad, select)
    b.chain(select, render, attach)
    return b.build()


def wf_discord() -> dict[str, Any]:
    b = Builder("discord")
    b.sticky("## PRQA 06 · Discord Reporter\nHands the **validated** result to the worker (`POST /notifications/discord`), which: refuses jobs that have not "
             "`succeeded`, re-validates the result (invalid → honest INCONCLUSIVE, never PASS), claims the `discord` notification on the job "
             "(duplicate guard), sanitizes untrusted text (mentions, Markdown), splits long reports within Discord's limits, delivers with bounded "
             "retries (timeouts, 429 `Retry-After`, 5xx) and records the outcome on the job. **Real** webhook only when the worker policy reports "
             "`discord_mode == 'real'` (`AUTOMATION_MODE=live` + `REAL_DISCORD_REPORTING_ENABLED=true`) *and* `DISCORD_WEBHOOK_URL` is set in the worker "
             "environment; otherwise the mock sink (`prqa-mocks`, inspect at `/mock/received`). A delivery failure never changes the test result.",
             0, -1.8, 940, 210)
    trig = b.node("Discord Input", "n8n-nodes-base.executeWorkflowTrigger", 1.1, {"inputSource": "passthrough"}, 0, 0)
    deliver = b.node("Deliver Discord Report (worker /notifications/discord)", "n8n-nodes-base.httpRequest", 4.2,
                     worker_http("POST", "/notifications/discord",
                                 body="={{ JSON.stringify({ kind: 'result', job_id: $json.job.id, result: $json.result, result_validation: $json.result_validation, "
                                      "prompt: $json.prompt ? { source: $json.prompt.source, model: ($json.llm || {}).model || null, api_mode: ($json.endpoints || {}).prompt_llm_api_mode || null } : null, "
                                      "pipeline: { execution_id: $execution.id, entry_point: $json.entry_point || null, delivery_id: $json.delivery_id || null } }) }}",
                                 timeout_ms=120000, never_error=True, full_response=True), 1, 0,
                     notes="worker decides mock vs real; 409 = job not completed (never reported)")
    status = b.node("Attach Discord Status", "n8n-nodes-base.code", 2, code(JS_DISCORD_STATUS), 2, 0)
    ok = b.node("Delivered?", "n8n-nodes-base.if", 2.2, if_params(cond("={{ $json.discord.delivered }}", "boolean", "true", single=True)), 3, 0)
    good = b.node("Discord: delivered ✓", "n8n-nodes-base.noOp", 1, {}, 4, -0.6)
    bad = b.node("Discord: failed / skipped (result unchanged)", "n8n-nodes-base.noOp", 1, {}, 4, 0.6,
                 notes="status + per-message results recorded on the job (notifications.discord)")
    out = b.node("Return Pipeline Data", "n8n-nodes-base.code", 2, code("return [{ json: $('Attach Discord Status').first().json }];"), 5, 0)
    b.chain(trig, deliver, status, ok)
    b.connect(ok, good, 0)
    b.connect(ok, bad, 1)
    b.connect(good, out)
    b.connect(bad, out)
    return b.build()


def wf_comment() -> dict[str, Any]:
    b = Builder("comment")
    b.sticky("## PRQA 07 · GitHub PR Commenter\nPosts the rendered Markdown comment to `issues/{pr}/comments`. **Real** GitHub only when "
             "`endpoints.github_comment_mode == 'real'` (requires `AUTOMATION_MODE=live` + `REAL_GITHUB_REPORTING_ENABLED=true`) *and* `GITHUB_TOKEN` is set. "
             "Otherwise the comment goes to the mock GitHub sink. The comment carries `<!-- prqa:job:<id> -->` for idempotent updates.", 0, -1.6, 880, 170)
    trig = b.node("Comment Input", "n8n-nodes-base.executeWorkflowTrigger", 1.1, {"inputSource": "passthrough"}, 0, 0)
    gate = b.node("Real GitHub Reporting Enabled?", "n8n-nodes-base.if", 2.2, if_params(
        cond("={{ $json.endpoints.github_comment_mode }}", "string", "equals", "real"),
        cond("={{ $env.GITHUB_TOKEN || '' }}", "string", "notEmpty", single=True),
    ), 1, 0, notes="false in Phase 1")
    real = b.node("Post PR Comment (REAL GitHub API)", "n8n-nodes-base.httpRequest", 4.2,
                  http("POST", "=https://api.github.com/repos/{{ $json.job_seed.repository }}/issues/{{ $json.job_seed.pr.number }}/comments",
                       body="={{ JSON.stringify({ body: $json.report.github_comment_markdown }) }}",
                       headers=[{"name": "Authorization", "value": "={{ 'Bearer ' + $env.GITHUB_TOKEN }}"}, {"name": "Accept", "value": "application/vnd.github+json"},
                                {"name": "User-Agent", "value": "prqa-n8n/0.1.0"}, {"name": "X-GitHub-Api-Version", "value": "2022-11-28"}],
                       timeout_ms=20000, never_error=True, full_response=True), 2, -0.6, retryOnFail=True, maxTries=3, waitBetweenTries=10000)
    mock = b.node("Post to Mock GitHub Sink (dry-run)", "n8n-nodes-base.httpRequest", 4.2,
                  http("POST", "={{ $json.endpoints.mock_services_url }}/github/repos/{{ $json.job_seed.repository }}/issues/{{ $json.job_seed.pr.number }}/comments",
                       body="={{ JSON.stringify({ body: $json.report.github_comment_markdown }) }}",
                       headers=[{"name": "Content-Type", "value": "application/json"}, {"name": "X-PRQA-Job", "value": "={{ $json.job.id }}"}],
                       timeout_ms=20000, never_error=True, full_response=True), 2, 0.6, **RETRY2)
    status = b.node("Attach GitHub Comment Status", "n8n-nodes-base.code", 2, code(JS_COMMENT_STATUS), 3, 0)
    record = b.node("Record Notification Status (worker)", "n8n-nodes-base.httpRequest", 4.2,
                    worker_http("POST", "/jobs/{{ $json.job.id }}/notification", body='={{ JSON.stringify({ channel: "github_comment", status: $json.github_comment.status, detail: { status_code: $json.github_comment.status_code, mode: $json.github_comment.mode, url: $json.github_comment.comment_url } }) }}',
                                timeout_ms=15000, never_error=True), 4, 0, **RETRY2)
    out = b.node("Return Pipeline Data", "n8n-nodes-base.code", 2, code("return [{ json: $('Attach GitHub Comment Status').first().json }];"), 5, 0)
    b.chain(trig, gate)
    b.connect(gate, real, 0)
    b.connect(gate, mock, 1)
    b.connect(real, status)
    b.connect(mock, status)
    b.chain(status, record, out)
    return b.build()


def wf_error() -> dict[str, Any]:
    b = Builder("error")
    b.sticky("## PRQA 08 · Error Handler\nSet as the *Error workflow* of every PRQA workflow. Records a sanitized event on the worker and sends an alert: "
             "real Discord only when the worker reports `discord_mode == 'real'`, otherwise the mock sink. Raw error messages, URLs and tokens never leave *Sanitize Error*.",
             0, -1.6, 860, 150)
    trig = b.node("Error Trigger", "n8n-nodes-base.errorTrigger", 1, {}, 0, 0)
    san = b.node("Sanitize Error (no raw messages / URLs / tokens)", "n8n-nodes-base.code", 2, code(JS_SANITIZE_ERROR), 1, 0)
    log_ = b.node("Log Error Event (worker /events)", "n8n-nodes-base.httpRequest", 4.2,
                  worker_http("POST", "/events", body="={{ JSON.stringify($json) }}", timeout_ms=15000, never_error=True), 2, 0, **RETRY2)
    cfg = b.node("Load Policy (worker /config)", "n8n-nodes-base.httpRequest", 4.2, worker_http("GET", "/config", timeout_ms=15000, never_error=True), 3, 0, **RETRY2)
    gate = b.node("Real Discord Alerts Enabled?", "n8n-nodes-base.if", 2.2, if_params(
        cond("={{ $json.endpoints ? $json.endpoints.discord_mode : 'mock' }}", "string", "equals", "real"),
        cond("={{ $env.DISCORD_WEBHOOK_URL || '' }}", "string", "notEmpty", single=True),
    ), 4, 0, notes="false in Phase 1")
    payload = "={{ JSON.stringify({ username: 'PR QA Pilot', allowed_mentions: { parse: [] }, content: '🚨 PRQA pipeline error', embeds: [{ title: 'Workflow error: ' + ($('Sanitize Error (no raw messages / URLs / tokens)').first().json.detail.workflow_name || 'unknown'), color: 15158332, fields: [ { name: 'Execution', value: String($('Sanitize Error (no raw messages / URLs / tokens)').first().json.detail.execution_id || 'n/a'), inline: true }, { name: 'Last node', value: String($('Sanitize Error (no raw messages / URLs / tokens)').first().json.detail.last_node || 'n/a'), inline: true }, { name: 'Message', value: String($('Sanitize Error (no raw messages / URLs / tokens)').first().json.detail.error_message || 'n/a').slice(0, 900), inline: false } ] }] }) }}"
    real = b.node("Alert Discord (REAL webhook)", "n8n-nodes-base.httpRequest", 4.2,
                  http("POST", "={{ $env.DISCORD_WEBHOOK_URL }}", body=payload, headers=[{"name": "Content-Type", "value": "application/json"}],
                       timeout_ms=20000, never_error=True), 5, -0.6, retryOnFail=True, maxTries=3, waitBetweenTries=10000)
    mock = b.node("Alert Mock Discord Sink (dry-run)", "n8n-nodes-base.httpRequest", 4.2,
                  http("POST", "={{ $json.endpoints ? $json.endpoints.discord_mock_url : 'http://prqa-mocks:8090/discord/api/webhooks/000000000000000000/mock-token' }}",
                       body=payload, headers=[{"name": "Content-Type", "value": "application/json"}], timeout_ms=20000, never_error=True), 5, 0.6, **RETRY2)
    end = b.node("Error Handled (end)", "n8n-nodes-base.noOp", 1, {}, 6, 0)
    b.chain(trig, san, log_, cfg, gate)
    b.connect(gate, real, 0)
    b.connect(gate, mock, 1)
    b.connect(real, end)
    b.connect(mock, end)
    return b.build(error_workflow=False)


BUILDERS = {
    "main": wf_main, "webhook": wf_webhook, "context": wf_context, "prompt": wf_prompt, "dispatch": wf_dispatch,
    "validate": wf_validate, "discord": wf_discord, "comment": wf_comment, "error": wf_error,
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for key, fn in BUILDERS.items():
        wf = fn()
        path = OUT / FILES[key]
        path.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        real_nodes = [n for n in wf["nodes"] if n["type"] != "n8n-nodes-base.stickyNote"]
        print(f"wrote {path.relative_to(ROOT)}  ({wf['id']}, {len(real_nodes)} nodes, {sum(len(o) for c in wf['connections'].values() for o in c['main'])} connections)")


if __name__ == "__main__":
    main()
