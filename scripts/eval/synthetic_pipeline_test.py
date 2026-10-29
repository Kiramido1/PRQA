#!/usr/bin/env python3
"""SYNTHETIC end-to-end checks against the running review stack. Stdlib only. No real service is contacted.

  python3 scripts/eval/synthetic_pipeline_test.py            # worker + mocks checks (always safe)
  python3 scripts/eval/synthetic_pipeline_test.py --webhook  # additionally: temporarily activate the n8n webhook receiver,
                                                        # POST signed SYNTHETIC payloads, wait for the pipeline, deactivate again

Every request goes to 127.0.0.1 (n8n, worker, mocks). The payloads are the fixtures under fixtures/synthetic/.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "setup"))
from n8n_api import Client, env  # noqa: E402

WORKER = "http://127.0.0.1:8080"
MOCKS = "http://127.0.0.1:8090"
N8N = "http://127.0.0.1:5678"
FIX = ROOT / "fixtures" / "synthetic"
E = env()
TOKEN = E["PRQA_WORKER_TOKEN"]
SECRET = E["GITHUB_WEBHOOK_SECRET"]
ACTIONS_SECRET = E.get("PRQA_WEBHOOK_SECRET", "")   # GitHub Actions entry; added to compose/.env by scripts/start.sh
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def req(method: str, url: str, body: bytes | dict | None = None, headers: dict | None = None, timeout: int = 30) -> tuple[int, object]:
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    r = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw)
        except ValueError:
            return err.code, raw[:300].decode("utf-8", "replace")


def worker(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    return req(method, WORKER + path, body, {"X-Worker-Token": TOKEN})


def synthetic_job(delivery: str) -> dict:
    ev = json.loads((FIX / "github-pr-labeled.synthetic.json").read_text())
    pr = ev["pull_request"]
    text = "x" * 400
    return {
        "synthetic": True, "repository": ev["repository"]["full_name"],
        "pr": {"number": pr["number"], "head_sha": pr["head"]["sha"], "base_sha": pr["base"]["sha"], "title": pr["title"],
               "base_ref": "main", "head_ref": pr["head"]["ref"], "is_fork": False, "url": pr["html_url"]},
        "test_level": "standard",
        "trigger": {"source": "synthetic_test", "delivery_id": delivery, "label": "qa-test:standard"},
        "prompt": {"text": text, "source": "fixture", "sha256": hashlib.sha256(text.encode()).hexdigest(), "template_version": "v1"},
    }


def actions_request(repository: str = "synthetic-org/sample-orders-api", number: int = 101, *, head_sha: str | None = None,
                    force: bool = False, run_id: str = "424242") -> bytes:
    """What .github/workflows/prqa.yml sends (schemas/actions-request.schema.json), for the SYNTHETIC fixture PR."""
    ev = json.loads((FIX / "github-pr-labeled.synthetic.json").read_text())
    pr = ev["pull_request"]
    body = {
        "schema_version": "1.0", "source": "github_actions",
        "repository": {"full_name": repository, "html_url": f"https://github.com/{repository}"},
        "pull_request": {
            "number": number, "title": pr["title"], "body": pr["body"][:8000], "state": "open", "draft": False,
            "html_url": f"https://github.com/{repository}/pull/{number}", "user": {"login": pr["user"]["login"]},
            "labels": [{"name": "to be test"}],
            "head": {"ref": pr["head"]["ref"], "sha": head_sha or pr["head"]["sha"], "repo": {"full_name": repository, "fork": False}},
            "base": {"ref": pr["base"]["ref"], "sha": pr["base"]["sha"], "repo": {"full_name": repository}},
        },
        "request": {"test_level": "standard", "trigger": "label", "label": "to be test", "requested_by": "synthetic-qa-lead", "force_rerun": force},
        "actions": {"run_id": run_id, "run_attempt": "1", "run_url": f"https://github.com/{repository}/actions/runs/{run_id}",
                    "workflow": "PRQA · PR QA Pilot", "event_name": "pull_request", "client_version": "1.0"},
    }
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode()


def actions_headers(raw: bytes, *, ts: int | None = None, secret: str | None = None) -> dict:
    ts = int(time.time()) if ts is None else ts
    sig = "sha256=" + hmac.new((ACTIONS_SECRET if secret is None else secret).encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return {"X-PRQA-Timestamp": str(ts), "X-PRQA-Signature": sig, "User-Agent": "prqa-actions-client/synthetic"}


def actions_intake_checks(job_result: dict | None, job_id: str | None) -> None:
    """GitHub Actions entry contract on the worker (no n8n), plus Discord delivery through the worker."""
    if not ACTIONS_SECRET:
        check("PRQA_WEBHOOK_SECRET present in compose/.env (run scripts/start.sh once to add it)", False)
        return
    import base64
    stamp = time.strftime("%Y%m%d%H%M%S")
    sha = hashlib.sha1(stamp.encode()).hexdigest()  # unique head sha per run → unique idempotency key
    raw = actions_request(head_sha=sha)
    h = actions_headers(raw)
    intake = lambda raw, h: worker("POST", "/webhook/actions/intake", {"body_b64": base64.b64encode(raw).decode(), "signature": h["X-PRQA-Signature"],  # noqa: E731
                                                                      "timestamp": h["X-PRQA-Timestamp"], "idempotency_key": ""})
    status, body = intake(raw, h)
    check("actions intake: signed SYNTHETIC request accepted (202, job_seed, no result)", status == 202 and body.get("outcome") == "accepted"
          and body["job_seed"]["trigger"]["source"] == "github_actions" and "overall_status" not in json.dumps(body), f"HTTP {status} {body.get('reason_code')}")
    status, dup = intake(raw, h)
    check("actions intake: replay of the same request → duplicate (200), nothing new", status == 200 and dup.get("outcome") == "duplicate", f"HTTP {status}")
    status, bad = intake(raw, actions_headers(raw, secret="wrong-secret-wrong-secret-wrong-secret"))
    check("actions intake: wrong secret → 401 SIGNATURE_INVALID", status == 401 and bad.get("reason_code") == "SIGNATURE_INVALID", f"HTTP {status} {bad.get('reason_code')}")
    status, old = intake(raw, actions_headers(raw, ts=int(time.time()) - 3600))
    check("actions intake: stale timestamp → 401 TIMESTAMP_EXPIRED", status == 401 and old.get("reason_code") == "TIMESTAMP_EXPIRED", f"HTTP {status} {old.get('reason_code')}")
    unreg = actions_request("nobody/unregistered-repo", 1, head_sha=sha)
    status, forb = intake(unreg, actions_headers(unreg))
    check("actions intake: unregistered repository → 403 REPOSITORY_NOT_REGISTERED", status == 403 and forb.get("reason_code") == "REPOSITORY_NOT_REGISTERED", f"HTTP {status}")
    real = actions_request("your-org/your-first-real-repo", 7, head_sha=sha)
    status, prev = intake(real, actions_headers(real))
    check("actions intake: registered real repo accepted with policy preview = REVIEW_ONLY_MODE (would be blocked)", status == 202
          and prev["policy_preview"]["expected_decision"]["reason_code"] == "REVIEW_ONLY_MODE", f"HTTP {status}")

    if job_id and job_result:
        status, d = worker("POST", "/notifications/discord", {"kind": "result", "job_id": job_id, "result": job_result,
                                                              "prompt": {"source": "fixture"}, "pipeline": {"execution_id": "synthetic-script"}})
        check("discord via worker: validated FAIL result delivered to the MOCK sink (2 messages, mode=mock)", status == 200 and d.get("mode") == "mock"
              and d.get("status") == "mock_delivered" and d.get("sent") == 2, f"HTTP {status} {json.dumps(d)[:200]}")
        status, d2 = worker("POST", "/notifications/discord", {"kind": "result", "job_id": job_id})
        check("discord via worker: second delivery for the same job skipped (duplicate guard)", status == 200 and d2.get("status") == "skipped_duplicate", f"{d2.get('status')}")
        status, got = worker("GET", f"/jobs/{job_id}")
        check("discord via worker: notification recorded on the job, result unchanged", got["job"]["notifications"]["discord"]["status"] == "mock_delivered"
              and got["job"]["result"]["summary"]["overall_status"] == "FAIL")
        status, recv = req("GET", MOCKS + "/mock/received")
        discord_msgs = [r for r in recv["received"] if r["kind"] == "discord_webhook" and "(1/2)" in str((r["body"] or {}).get("content", ""))]
        check("mock Discord sink received the summary message with the job link", any(job_id in json.dumps(r["body"]) for r in discord_msgs), f"{len(discord_msgs)} summary messages")
        seed = {"repository": "your-org/your-first-real-repo", "synthetic": False, "test_level": "standard",
                "pr": {"number": 7, "title": "placeholder", "head_sha": sha + "0" * (40 - len(sha)), "url": "https://github.com/your-org/your-first-real-repo/pull/7"},
                "trigger": {"source": "github_actions", "delivery_id": f"synthetic-blocked-{stamp}"}}
        status, b = worker("POST", "/notifications/discord", {"kind": "blocked", "job_seed": seed, "decision": {"reason_code": "REVIEW_ONLY_MODE", "reason": "review only"}})
        check("discord via worker: BLOCKED notice delivered to the MOCK sink", status == 200 and b.get("status") == "mock_delivered", f"HTTP {status} {b.get('status')}")


def lifecycle_checks(stamp: str) -> None:
    """Label trigger + lifecycle on the worker: evaluate → job → cancel on unlabeled/synchronize, never reported."""
    ev = json.loads((FIX / "github-pr-labeled.synthetic.json").read_text())
    status, cfg = worker("GET", "/config")
    label = cfg.get("trigger", {}).get("label")
    check("worker reports the configurable opt-in label", bool(label), f"label={label!r}")
    ev["label"]["name"] = label
    sha = hashlib.sha1(f"lifecycle-{stamp}".encode()).hexdigest()
    ev["pull_request"]["head"]["sha"] = sha
    status, out = worker("POST", "/webhook/evaluate", {"event": "pull_request", "payload": ev})
    check("labeled event with the opt-in label is eligible (default level)", status == 200 and out.get("eligible") and out["job_seed"]["test_level"] == cfg["trigger"]["default_test_level"], f"{out.get('reason_code')}")
    seed = out.get("job_seed") or {}
    req = {"synthetic": True, "repository": seed.get("repository"), "pr": seed.get("pr"), "test_level": seed.get("test_level"),
           "trigger": dict(seed.get("trigger", {}), delivery_id=f"lifecycle-{stamp}"), "prompt": {"text": "x" * 300, "source": "fixture", "sha256": "a" * 64}}
    status, created = worker("POST", "/jobs", req)
    check("label-triggered synthetic job created (201)", status == 201, f"HTTP {status}")
    job_id = created["job"]["id"] if status == 201 else None
    ev["action"] = "unlabeled"
    status, out = worker("POST", "/webhook/evaluate", {"event": "pull_request", "payload": ev})
    check("unlabeled event → lifecycle cancel (not eligible)", status == 200 and not out.get("eligible") and (out.get("lifecycle") or {}).get("reason") == "LABEL_REMOVED")
    lc = out.get("lifecycle") or {}
    status, body = worker("POST", "/jobs/cancel", {"repository": lc.get("repository"), "pr_number": lc.get("pr_number"), "reason": lc.get("reason"), "except_head_sha": lc.get("except_head_sha")})
    cancelled_ids = [c["id"] for c in (body.get("cancelled") or [])] if status == 200 else []
    check("active job cancelled by the lifecycle event", job_id in cancelled_ids, f"cancelled={cancelled_ids}")
    status, got = worker("GET", f"/jobs/{job_id}") if job_id else (0, {})
    check("cancelled job is terminal (status=cancelled) and has no result", status == 200 and got["job"]["status"] == "cancelled" and got["job"]["result"] is None)
    status, d = worker("POST", "/notifications/discord", {"kind": "result", "job_id": job_id})
    check("cancelled job can never be reported to Discord (409 JOB_NOT_COMPLETED)", status == 409 and d.get("error") == "JOB_NOT_COMPLETED")
    status, ws = worker("GET", "/workspaces")
    check("worker exposes isolated workspaces with a retention policy", status == 200 and "retention_hours" in ws, f"{len(ws.get('workspaces', []))} workspaces")


def worker_and_mock_checks() -> None:
    status, cfg = worker("GET", "/config")
    check("worker /config reachable", status == 200, f"HTTP {status}")
    pol = cfg["policy"]
    check("AUTOMATION_MODE=review_only enforced", pol["review_only"] and pol["automation_mode"] == "review_only")
    check("all REAL_* flags false", not any(pol["flags"].values()), json.dumps(pol["flags"]))
    check("worker hands n8n MOCK endpoints only", cfg["endpoints"]["github_api_mode"] == "mock" and cfg["endpoints"]["prompt_llm_api_mode"] == "mock"
          and cfg["endpoints"]["discord_mode"] == "mock" and cfg["endpoints"]["github_comment_mode"] == "mock", json.dumps(cfg["endpoints"]))
    check("registry: 0 effectively enabled repositories", cfg["registry"]["effective_enabled_count"] == 0,
          ", ".join(f"{r['full_name']} enabled={r['enabled']} status={r['approval_status']}" for r in cfg["registry"]["repositories"]))
    check("reference prompt loaded by worker", len(cfg["reference_prompt_sha256"]) == 64,
          f"sha256={cfg['reference_prompt_sha256'][:16]}… matches prompts/reference/ copy: {hashlib.sha256((ROOT / 'prompts/reference/Senior_QA_Reference_Prompt.md').read_bytes()).hexdigest() == cfg['reference_prompt_sha256']}")

    stamp = time.strftime("%Y%m%d%H%M%S")
    real = synthetic_job(f"real-{stamp}")
    real.update({"synthetic": False, "repository": "your-org/your-first-real-repo", "trigger": {"source": "github_webhook", "label": "qa-test:smoke", "delivery_id": f"real-{stamp}"}})
    status, body = worker("POST", "/jobs", real)
    check("real-looking job REJECTED by worker", status == 403 and body["decision"]["reason_code"] == "REVIEW_ONLY_MODE", f"HTTP {status} {body.get('decision', {}).get('reason_code') if isinstance(body, dict) else body}")

    status, body = worker("POST", "/jobs", synthetic_job(f"synthetic-{stamp}"))
    check("synthetic job accepted (201, mock executor)", status == 201 and body["job"]["execution_mode"] == "mock", f"HTTP {status}")
    job_id = body["job"]["id"] if status == 201 else None
    status, dup = worker("POST", "/jobs", synthetic_job(f"synthetic-{stamp}"))
    check("duplicate delivery id rejected (409)", status == 409 and dup.get("error") == "DUPLICATE_DELIVERY", f"HTTP {status} {dup.get('error') if isinstance(dup, dict) else dup}")
    status, dup2 = worker("POST", "/jobs", synthetic_job(f"synthetic-other-{stamp}"))
    check("duplicate active job (same repo#pr@sha:level) rejected (409)", status == 409 and dup2.get("error") == "DUPLICATE_JOB", f"HTTP {status}")

    final = None
    if job_id:
        for _ in range(40):
            status, got = worker("GET", f"/jobs/{job_id}")
            if got["job"]["status"] in ("succeeded", "failed"):
                final = got["job"]
                break
            time.sleep(0.5)
    check("synthetic job finished via mock executor", bool(final) and final["status"] == "succeeded", f"status={final['status'] if final else 'timeout'}")
    if final and final["status"] == "succeeded":
        res = final["result"]
        check("result marked synthetic + executed by mock_executor", res["synthetic"] and res["executed_by"]["engine"] == "mock_executor")
        status, rendered = worker("POST", "/reports/render", {"result": res})
        check("report rendered (markdown/discord/github comment)", status == 200 and "SYNTHETIC" in rendered["report_markdown"] and rendered["discord_mode"] == "mock")
        status, _ = req("POST", MOCKS + "/discord/api/webhooks/000000000000000000/mock-token", rendered["discord_payload"])
        check("mock Discord sink accepted payload (204)", status == 204, f"HTTP {status}")
        status, _ = req("POST", MOCKS + "/github/repos/synthetic-org/sample-orders-api/issues/101/comments", {"body": rendered["github_comment_markdown"]})
        check("mock GitHub comment sink accepted (201)", status == 201, f"HTTP {status}")
        status, _ = req("POST", MOCKS + "/github/repos/your-org/your-first-real-repo/issues/7/comments", {"body": "must be refused"})
        check("mock GitHub sink refuses non-synthetic repository", status == 404)
    actions_intake_checks(final["result"] if final and final["status"] == "succeeded" else None, job_id)
    lifecycle_checks(stamp)
    status, recv = req("GET", MOCKS + "/mock/received")
    kinds = sorted({r["kind"] for r in recv["received"]}) if status == 200 else []
    check("mocks recorded only mock traffic", status == 200, f"kinds={kinds}")


def webhook_checks() -> None:
    c = Client()
    c.login()
    wf_id = "PrqaWebhookRecvr"
    # n8n 2.x: a workflow can only be published when its sub-workflows are published → publish the whole chain,
    # run the synthetic calls, then unpublish everything again (the receiver is the only production trigger).
    for line in c.publish_chain(wf_id):
        print("publish:", line)
    existing = c.executions(1)
    start_exec_id = int(existing[0]["id"]) if existing else 0
    try:
        time.sleep(2)
        url = N8N + "/webhook/prqa/github"
        raw = (FIX / "github-pr-labeled.synthetic.json").read_bytes()
        stamp = time.strftime("%Y%m%d%H%M%S")
        delivery = f"synthetic-webhook-{stamp}"
        sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        gh = {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": sig, "User-Agent": "GitHub-Hookshot/synthetic"}

        status, body = req("POST", url, raw, {**gh, "X-Hub-Signature-256": "sha256=" + "0" * 64})
        check("webhook: bad signature → 401", status == 401, f"HTTP {status} {body}")
        status, body = req("POST", url, (FIX / "github-pr-opened-ignored.synthetic.json").read_bytes(),
                           {**gh, "X-GitHub-Delivery": delivery + "-opened",
                            "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET.encode(), (FIX / "github-pr-opened-ignored.synthetic.json").read_bytes(), hashlib.sha256).hexdigest()})
        check("webhook: 'opened' event ignored (200, no job)", status == 200 and body.get("ignored") is True, f"HTTP {status} {body}")
        status, body = req("POST", url, raw, gh)
        check("webhook: signed SYNTHETIC labeled event accepted (202)", status == 202 and body.get("accepted") is True and body.get("synthetic") is True, f"HTTP {status} {body}")
        status, body = req("POST", url, raw, gh)
        check("webhook: same delivery id → duplicate (200)", status == 200 and body.get("duplicate") is True, f"HTTP {status} {body}")
        unreg = (FIX / "github-pr-unregistered-repo.synthetic.json").read_bytes()
        status, body = req("POST", url, unreg, {**gh, "X-GitHub-Delivery": delivery + "-unreg", "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET.encode(), unreg, hashlib.sha256).hexdigest()})
        check("webhook: unapproved real-looking repo accepted at receiver (202) → must be BLOCKED by the policy gate", status == 202 and body.get("synthetic") is False, f"HTTP {status} {body}")

        # ---- GitHub Actions entry through n8n (what .github/workflows/prqa.yml calls) ----
        expected_mains = 2
        if ACTIONS_SECRET:
            aurl = N8N + "/webhook/prqa/actions"
            araw = actions_request(head_sha=hashlib.sha1(f"n8n-{stamp}".encode()).hexdigest())
            status, body = req("POST", aurl, araw, {**actions_headers(araw, secret="not-the-secret-not-the-secret-0000"), "Content-Type": "application/json"})
            check("actions webhook (n8n): bad signature → 401", status == 401 and body.get("outcome") == "unauthorized", f"HTTP {status} {body}")
            status, body = req("POST", aurl, araw, {**actions_headers(araw), "Content-Type": "application/json"})
            check("actions webhook (n8n): signed SYNTHETIC request accepted (202, outcome=accepted, receiver execution id)",
                  status == 202 and body.get("outcome") == "accepted" and body.get("receiver_execution_id"), f"HTTP {status} {body}")
            status, body = req("POST", aurl, araw, {**actions_headers(araw), "Content-Type": "application/json"})
            check("actions webhook (n8n): same request again → duplicate (200), no new pipeline", status == 200 and body.get("outcome") == "duplicate", f"HTTP {status} {body}")
            bad = actions_request("nobody/unregistered-repo", 1, head_sha="f" * 40)
            status, body = req("POST", aurl, bad, {**actions_headers(bad), "Content-Type": "application/json"})
            check("actions webhook (n8n): unregistered repository → 403", status == 403 and body.get("reason_code") == "REPOSITORY_NOT_REGISTERED", f"HTTP {status} {body}")
            expected_mains = 3
        else:
            check("actions webhook (n8n): skipped — PRQA_WEBHOOK_SECRET missing from compose/.env", False)

        print("waiting for the Main Pipeline executions…")
        deadline = time.time() + 240
        mains: list[dict] = []
        while time.time() < deadline:
            execs = c.executions(40)
            mains = [e for e in execs if e["workflow"] in ("PRQA 00 · Main Pipeline", "PrqaMainPipeline") and int(e["id"]) > start_exec_id]
            if len(mains) >= expected_mains and all(e["status"] in ("success", "error", "crashed", "canceled") for e in mains):
                break
            time.sleep(5)
        outcomes = {}
        for e in mains:
            detail = c.execution(str(e["id"]))
            outcomes[e["id"]] = (detail["status"], detail["last_node"])
            print(f"   main execution {e['id']}: status={detail['status']} last_node={detail['last_node']} error={detail['error']}")
        check("webhook-driven SYNTHETIC run completed through Final Summary", any(s == "success" and n == "Final Summary" for s, n in outcomes.values()), str(outcomes))
        check("webhook-driven unapproved-repo run ended in the BLOCKED branch (no external call)", any(s == "success" and n and n.startswith("Blocked:") for s, n in outcomes.values()), str(outcomes))
        finals = [s for s, n in outcomes.values() if s == "success" and n == "Final Summary"]
        check("duplicate deliveries never started a pipeline execution", len(mains) == expected_mains, f"{len(mains)} main executions for {expected_mains} accepted deliveries")
        if ACTIONS_SECRET:
            check("Actions-triggered SYNTHETIC run completed through Final Summary (second Final Summary execution)", len(finals) >= 2, f"{len(finals)} Final Summary executions")
        status, recv = req("GET", MOCKS + "/mock/received")
        kinds = [r["kind"] for r in recv["received"]]
        check("mocks saw llm_request + discord_webhook + github_comment from the n8n pipeline", all(k in kinds for k in ("llm_request", "discord_webhook", "github_comment")), f"kinds={sorted(set(kinds))}")
        discord_bodies = [json.dumps(r["body"]) for r in recv["received"] if r["kind"] == "discord_webhook"]
        check("Discord mock received a BLOCKED notice for the unapproved repository (via worker)", any("QA blocked" in b and "REVIEW_ONLY_MODE" in b for b in discord_bodies), f"{len(discord_bodies)} discord payloads")
        check("Discord mock received validated FAIL reports from the pipeline (never a success before validation)", any("QA failed" in b and "(1/2)" in b for b in discord_bodies))
        check("Discord payloads never allow mentions", all('"parse": []' in b for b in discord_bodies))
        status, ev = worker("GET", "/events?limit=50")
        blocked_events = [e for e in ev["events"] if e["kind"] == "blocked_by_policy"]
        check("worker logged blocked_by_policy for the unapproved repository", any((e["detail"].get("decision") or {}).get("reason_code") == "REVIEW_ONLY_MODE" for e in blocked_events),
              f"{len(blocked_events)} blocked events")
    finally:
        for line in c.unpublish_all():
            print("unpublish:", line)
        left_active = [w for w in c.workflows() if w["active"]]
        check("all PRQA workflows left INACTIVE after the synthetic run", not left_active, ", ".join(w["id"] for w in left_active))


def main() -> int:
    worker_and_mock_checks()
    if "--webhook" in sys.argv:
        webhook_checks()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} synthetic checks passed" + (" — FAILURES ABOVE" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
