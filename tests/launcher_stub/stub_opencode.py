#!/usr/bin/env python3
"""STUB that stands in for the `opencode` binary in the isolated launcher integration test image ONLY.

Without OPENCODE_CONFIG_CONTENT it never contacts anything. It optionally sleeps (PR title containing STUB-SLEEP=<seconds>,
used by the cancellation case) and writes a deterministic synthetic result document derived from the job identity the
entrypoint already exposes (PRQA_JOB_JSON) into $PRQA_EVIDENCE_DIR/qa-result.json, which the real entrypoint then
prints as the last stdout line exactly as it would for a real run.

When the entrypoint configured the PRQA model proxy (tests/test_phase2_containers.py only, mocked upstream) it does what
the real OpenCode does with that configuration: reads OPENCODE_CONFIG_CONTENT (provider "prqa", baseURL, apiKey
"{env:PRQA_MODEL_TOKEN}"), resolves the token from its environment, and sends ONE OpenCode-shaped chat completion
(stream=true, stream_options.include_usage, max_tokens 32000, -m prqa/<model> → body model <model>) as
`Authorization: Bearer <token>`, parsing the SSE reply. It then probes the administrative API from inside the sandbox
and records the outcomes plus the NAMES of the variables it received (never values) in the result summary:
MODEL-PROXY-PROBE inference=<status> content=... sse_done=... admin_via_inference=<status> ... env_names=[...]
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request


def _status(method, url, token, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except OSError as e:
        return type(e).__name__, b""


def model_proxy_probe():
    raw_cfg = os.environ.get("OPENCODE_CONFIG_CONTENT", "")
    if not raw_cfg:
        return ""
    cfg = json.loads(raw_cfg)
    prov = cfg["provider"]["prqa"]
    base = prov["options"]["baseURL"]
    key_ref = prov["options"]["apiKey"]
    m = re.fullmatch(r"\{env:([A-Z_]+)\}", key_ref)
    token = os.environ.get(m.group(1), "") if m else ""
    model_arg = sys.argv[sys.argv.index("-m") + 1] if "-m" in sys.argv else ""
    model = model_arg.split("/", 1)[1] if model_arg.startswith("prqa/") else model_arg
    body = {"model": model, "max_tokens": 32000, "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "system", "content": "stub"}, {"role": "user", "content": [{"type": "text", "text": "stub"}]}]}
    st, raw = _status("POST", base.rstrip("/") + "/chat/completions", token, body)
    content, done = "", False
    for block in raw.decode("utf-8", "replace").split("\n\n"):
        if block.startswith("data: [DONE]"):
            done = True
        elif block.startswith("data: "):
            try:
                for ch in json.loads(block[6:]).get("choices") or []:
                    content += (ch.get("delta") or {}).get("content") or ""
            except ValueError:
                pass
    origin = base.rstrip("/").rsplit("/v1", 1)[0]
    admin_st, _ = _status("POST", origin + "/admin/tokens", token, {"job_id": "stub-escalation", "model": model, "ttl_s": 86400})
    encoded_st, _ = _status("GET", origin + "/%61dmin/tokens", token)
    socket_visible = os.path.exists("/run/prqa-model-admin/admin.sock") or os.path.exists("/run/prqa-model-admin")
    names = sorted(k for k in os.environ if k not in ("PWD", "SHLVL", "_"))
    return (f" MODEL-PROXY-PROBE inference={st} content={content[:40]!r} sse_done={done} model_arg={model_arg}"
            f" key_ref={key_ref} token_resolved={bool(token)} admin_via_inference={admin_st} admin_encoded={encoded_st}"
            f" admin_socket_visible={socket_visible} provider_key_in_env={any('KEY' in k and 'PROVIDER' in k for k in os.environ)}"
            f" env_names={','.join(names)}")

job = json.loads(os.environ["PRQA_JOB_JSON"])
m = re.search(r"STUB-SLEEP=(\d+)", job.get("pr", {}).get("title", "") or "")
if m:
    time.sleep(int(m.group(1)))
with open("/usr/local/share/prqa-stub-result.json", encoding="utf-8") as fh:
    doc = json.load(fh)
doc.update({"synthetic": False, "job_id": job["job_id"], "repository": job["repository"]})
doc["pr"] = {"number": job["pr"]["number"], "title": job["pr"]["title"], "head_sha": job["pr"]["head_sha"]}
doc["executed_by"] = {"engine": "opencode", "mode": "live"}
doc["environment"] = {"name": "staging", "base_url_redacted": "https://staging.example.test"}
doc["summary"]["executive_summary"] = "STUB RESULT (launcher integration test): no model, no target, no OpenCode." + model_proxy_probe()
os.makedirs(os.environ["PRQA_EVIDENCE_DIR"], exist_ok=True)
with open(os.path.join(os.environ["PRQA_EVIDENCE_DIR"], "qa-result.json"), "w", encoding="utf-8") as fh:
    json.dump(doc, fh)
print(json.dumps({"stub": True, "note": "opencode stub executed; nothing contacted"}))
sys.exit(0)
