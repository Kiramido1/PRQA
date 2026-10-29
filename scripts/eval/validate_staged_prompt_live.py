#!/usr/bin/env python3
"""Bounded live validation of the staged Groq prompt generator (worker/prqa_worker/prompt_stages.py).

Runs ONE staged generation for an already prepared PR run (its context.json and env/openapi.json) in a separate
validation directory, then applies the existing gate locally (the same prompt_builder/qa_rubric code as the worker).
Every provider request goes through the model proxy and its shared budget ledger: the request is sent from inside the
n8n container (`docker exec … node`), which already holds the inference-only prompt credential, so the credential never
reaches this process. Before each request the ledger's read-only capacity check decides (wait or stop).

No stage of the pipeline is run, no job or grant is created, OpenCode is not started, nothing is posted to GitHub or
Discord. At most stages × (1 + retries) provider requests, then it stops.

    python3 scripts/eval/validate_staged_prompt_live.py --from-run runs/prqa-local-pr-runs/<run> --live
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "scripts" / "pipeline"))
from prqa_worker import prompt_builder as pb, prompt_stages as ps  # noqa: E402

N8N = "prqa-n8n-1"
PROXY_URL = "http://prqa-model-proxy:8089/v1/chat/completions"
# Runs inside the n8n container: reads {request, job_id} on stdin, posts to the proxy with the container's own prompt
# credential, prints only the completion's content/finish/usage or the error status — never headers or the credential.
NODE = r"""
let d='';process.stdin.on('data',c=>d+=c).on('end',async()=>{const i=JSON.parse(d);
try{const r=await fetch(process.env.U,{method:'POST',headers:{'Authorization':'Bearer '+process.env.PRQA_MODEL_PROXY_PROMPT_TOKEN,
'Content-Type':'application/json','X-PRQA-Job-Id':i.job_id,'X-Title':'PR QA Pilot'},body:JSON.stringify(i.request),signal:AbortSignal.timeout(360000)});
const t=await r.text();let j=null;try{j=JSON.parse(t)}catch(e){}
if(!r.ok){console.log(JSON.stringify({http_status:r.status,error:(j&&j.error&&j.error.message||'').slice(0,200)}));return}
const ch=j&&j.choices&&j.choices[0]||{};console.log(JSON.stringify({http_status:r.status,content:(ch.message||{}).content||null,
finish_reason:ch.finish_reason||null,usage:j&&j.usage||null,model:j&&j.model||null}))}
catch(e){console.log(JSON.stringify({http_status:null,error:String(e.name||'error')}))}});
"""


def send(request: dict, job_id: str) -> dict:
    p = subprocess.run(["docker", "exec", "-i", "-e", f"U={PROXY_URL}", N8N, "node", "-e", NODE],
                       input=json.dumps({"request": request, "job_id": job_id}), capture_output=True, text=True, timeout=400)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"http_status": None, "error": "no response from the n8n container"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-run", type=Path, required=True)
    ap.add_argument("--live", action="store_true", help="required: send real (bounded) provider requests via the model proxy")
    a = ap.parse_args()
    if not a.live:
        print("refusing without --live")
        return 2
    import prqa_stages as S                                           # the same settings, ledger and job naming the pipeline uses
    src = (ROOT / a.from_run).resolve() if not a.from_run.is_absolute() else a.from_run
    context = json.loads((src / "context.json").read_text())
    openapi = json.loads((src / "env" / "openapi.json").read_text()) if (src / "env" / "openapi.json").is_file() else None
    sha = context["pr"]["head_sha"]
    out = ROOT / "runs" / "prqa-staged-validation" / f"{src.name}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    out.mkdir(parents=True)
    shutil.copy2(src / "context.json", out / "context.json")
    env = S._compose_env()
    cfg = ps.StageConfig.from_env(env)
    tpm = int(env.get("PRQA_BUDGET_TOKENS_PER_MINUTE") or 0) or None
    cfg.check(tpm)
    job_ref = f"staged-validation-{context['pr']['number']}-{sha[:12]}"
    capacity = S._capacity("prompt-" + job_ref)
    if capacity is None:
        print("no shared budget ledger configured: refusing a live request")
        return 2
    pipe = ps.Pipeline(out, prompts_dir=ROOT / "prompts", context=context, openapi=openapi, repository=context["repository"],
                       pr_number=int(context["pr"]["number"]), head_sha=sha, test_level="standard", model=S.prompt_model(),
                       provider="groq", cfg=cfg, capacity=capacity)
    act = pipe.start()
    calls, log = 0, []
    limit = len(ps.MODES[pipe.meta()["mode"]]) * (1 + cfg.max_retries)
    while act["action"] == "call" and calls < limit:
        if act["wait_s"] > cfg.max_wait_s:
            break
        if act["wait_s"]:
            print(f"waiting {act['wait_s']:.1f}s for TPM capacity before {act['stage']} (ledger)", flush=True)
            time.sleep(act["wait_s"])
        t0 = time.time()
        r = send(act["llm_request"], job_ref)
        calls += 1
        entry = {"stage": act["stage"], "attempt": act["attempt"], "estimate": act["estimate"]["total_tokens"],
                 "http_status": r.get("http_status"), "finish": r.get("finish_reason"), "usage": r.get("usage"),
                 "seconds": round(time.time() - t0, 1), "error": r.get("error")}
        log.append(entry)
        print(json.dumps(entry), flush=True)
        status = r.get("http_status")
        gen = {"generated_text": r.get("content"), "finish_reason": r.get("finish_reason"),
               "llm_error": None if status == 200 else "prompt LLM call failed", "http_status": str(status) if status else None}
        act = pipe.record(gen, r.get("usage"))
    meta = pipe.meta()
    result = {"validation_dir": str(out.relative_to(ROOT)), "provider_calls": calls, "calls": log, "mode": meta.get("mode"),
              "status": meta.get("status"), "failure": meta.get("failure"),
              "stage_notes": {s: v.get("notes") for s, v in meta.get("stages", {}).items()}}
    text = pipe.assembled_text()
    if text is not None:
        templates = pb.Templates.load(ROOT / "prompts")
        fin = pb.finalize_prompt(templates, generated_text=text, repository=context["repository"], pr_number=int(context["pr"]["number"]),
                                 test_level="standard", context=context, synthetic=False, finish_reason="stop")
        quality = fin.get("quality") or {}
        (out / "qa-prompt.md").write_text(fin["prompt"]["text"], encoding="utf-8")
        (out / "quality.json").write_text(json.dumps({"validation": fin["validation"], "quality": quality}, indent=2, default=str))
        meta["gate"] = {"final_prompt_source": fin["prompt"]["source"], "final_prompt_sha256": fin["prompt"]["sha256"],
                        "worker_validation": {k: fin["validation"].get(k) for k in ("valid", "errors", "warnings")},
                        "worker_quality": {k: quality.get(k) for k in ("accepted", "score", "threshold", "failure", "hard_fails")},
                        "note": "live validation: gated locally with the worker's gate code; no execution"}
        pipe._save_meta(meta)
        result.update(prompt_source=fin["prompt"]["source"], gate=meta["gate"]["worker_quality"],
                      gate_errors=fin["validation"].get("errors"), assembled_sha256=meta.get("assembled_sha256"))
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
