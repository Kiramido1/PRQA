#!/usr/bin/env python3
"""Small helper for the local n8n instance (internal REST API, cookie session). Stdlib only.

  python3 scripts/setup/n8n_api.py setup-owner          # create the owner account from compose/.env (idempotent)
  python3 scripts/setup/n8n_api.py list                 # workflows: id, name, active
  python3 scripts/setup/n8n_api.py activate <id> [true|false]   # n8n 2.x: activate == publish (needs sub-workflows published)
  python3 scripts/setup/n8n_api.py publish-chain [<id>]  # publish sub-workflows in dependency order up to <id> (default: main pipeline)
  python3 scripts/setup/n8n_api.py deactivate-all       # safety: unpublish every PRQA workflow (reverse order)
  python3 scripts/setup/n8n_api.py executions [limit]   # recent executions (status, workflow)
  python3 scripts/setup/n8n_api.py execution <id>       # one execution with node-level results (compact)

Credentials come from compose/.env (N8N_OWNER_EMAIL / N8N_OWNER_PASSWORD). Nothing is printed except ids/names/status.
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:5678"


def env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (ROOT / "compose" / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class Client:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.e = env()

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "browser-id": "prqa-script"})
        try:
            with self.opener.open(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return err.code, json.loads(raw)
            except ValueError:
                return err.code, {"raw": raw[:300].decode("utf-8", "replace")}

    def setup_owner(self) -> str:
        self.wait_ready()
        status, settings = self.call("GET", "/rest/settings")
        um = (settings.get("data") or {}).get("userManagement") or {}
        if status != 200 or "showSetupOnFirstLoad" not in um:
            raise SystemExit(f"cannot read n8n settings (HTTP {status}); is n8n up?")
        if not um["showSetupOnFirstLoad"]:
            return "owner already set up"
        status, body = self.call("POST", "/rest/owner/setup", {
            "email": self.e["N8N_OWNER_EMAIL"], "firstName": "PRQA", "lastName": "Reviewer",
            "password": self.e["N8N_OWNER_PASSWORD"],
        })
        return f"owner setup → HTTP {status}" if status == 200 else f"owner setup FAILED → HTTP {status}: {json.dumps(body)[:200]}"

    def wait_ready(self, seconds: int = 60) -> None:
        import time
        for _ in range(seconds):
            try:
                status, body = self.call("GET", "/rest/settings")
                if status == 200 and "userManagement" in (body.get("data") or {}):
                    return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(1)
        raise SystemExit("n8n REST API not ready after %ss" % seconds)

    def login(self) -> None:
        self.wait_ready()
        status, body = self.call("POST", "/rest/login", {"emailOrLdapLoginId": self.e["N8N_OWNER_EMAIL"], "password": self.e["N8N_OWNER_PASSWORD"]})
        if status != 200:
            status, body = self.call("POST", "/rest/login", {"email": self.e["N8N_OWNER_EMAIL"], "password": self.e["N8N_OWNER_PASSWORD"]})
        if status != 200:
            raise SystemExit(f"login failed: HTTP {status} {json.dumps(body)[:200]}")

    def workflows(self) -> list[dict]:
        status, body = self.call("GET", "/rest/workflows?limit=100")
        data = body.get("data")
        if isinstance(data, dict):
            data = data.get("data") or data.get("workflows") or []
        return [{"id": w["id"], "name": w["name"], "active": w.get("active")} for w in (data or [])]

    def set_active(self, wf_id: str, active: bool) -> str:
        status, body = self.call("GET", f"/rest/workflows/{wf_id}")
        wf = body.get("data") or {}
        if not wf:
            return f"{wf_id}: not found (HTTP {status})"
        # n8n 2.x: activation == publishing a version (POST .../activate), deactivation == POST .../deactivate.
        # Publishing fails until every referenced sub-workflow is published (see publish_chain).
        action = "activate" if active else "deactivate"
        status, body = self.call("POST", f"/rest/workflows/{wf_id}/{action}", {"versionId": wf.get("versionId")})
        if status != 200:
            return f"{wf_id}: HTTP {status} {json.dumps(body)[:300]}"
        return f"{wf_id}: active={body.get('data', {}).get('active')}"

    # Leaves first, then the main pipeline, then the webhook receiver (the only production trigger).
    PUBLISH_ORDER = ["PrqaErrorHandler", "PrqaPrContextCol", "PrqaLlmPromptBld", "PrqaOpencodeDisp", "PrqaResultValidn",
                     "PrqaDiscordRepor", "PrqaGithubCommen", "PrqaMainPipeline", "PrqaWebhookRecvr"]

    def publish_chain(self, up_to: str) -> list[str]:
        out = []
        for wf_id in self.PUBLISH_ORDER:
            out.append(self.set_active(wf_id, True))
            if wf_id == up_to:
                break
        return out

    def unpublish_all(self) -> list[str]:
        return [self.set_active(wf_id, False) for wf_id in reversed(self.PUBLISH_ORDER)]

    def executions(self, limit: int = 20) -> list[dict]:
        status, body = self.call("GET", f"/rest/executions?limit={limit}")
        data = body.get("data") or {}
        results = data.get("results") if isinstance(data, dict) else data
        return [{"id": e.get("id"), "workflow": e.get("workflowName") or e.get("workflowId"), "status": e.get("status"),
                 "mode": e.get("mode"), "started": e.get("startedAt")} for e in (results or [])]

    @staticmethod
    def _unflatten(raw):
        """Decode the 'flatted' encoding n8n uses for stored execution data."""
        if not isinstance(raw, str):
            return raw
        arr = json.loads(raw)
        if not isinstance(arr, list) or not arr:
            return arr
        memo: dict[int, object] = {}

        def resolve(i: int):
            if i in memo:
                return memo[i]
            v = arr[i]
            if isinstance(v, str):
                memo[i] = v
                return v
            if isinstance(v, list):
                out: list = []
                memo[i] = out
                out.extend(resolve(int(x)) if isinstance(x, str) else x for x in v)
                return out
            if isinstance(v, dict):
                outd: dict = {}
                memo[i] = outd
                for k, x in v.items():
                    outd[k] = resolve(int(x)) if isinstance(x, str) else x
                return outd
            memo[i] = v
            return v

        return resolve(0)

    def execution(self, exec_id: str) -> dict:
        status, body = self.call("GET", f"/rest/executions/{exec_id}")
        data = body.get("data") or {}
        run = (self._unflatten(data.get("data")) or {}).get("resultData") or {}
        nodes = {}
        for name, runs in (run.get("runData") or {}).items():
            last = runs[-1] if runs else {}
            out = ((last.get("data") or {}).get("main") or [[]])
            first = out[0][0]["json"] if out and out[0] else None
            nodes[name] = {"status": last.get("executionStatus"), "error": (last.get("error") or {}).get("message"),
                           "output_keys": sorted(first.keys())[:12] if isinstance(first, dict) else None}
        return {"id": data.get("id"), "status": data.get("status"), "workflow": (data.get("workflowData") or {}).get("name"),
                "last_node": run.get("lastNodeExecuted"), "error": (run.get("error") or {}).get("message"), "nodes": nodes}


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    c = Client()
    cmd = argv[0]
    if cmd == "setup-owner":
        print(c.setup_owner())
        return 0
    c.login()
    if cmd == "list":
        for w in c.workflows():
            print(f"{w['id']:<18} active={str(w['active']):<5} {w['name']}")
    elif cmd == "activate":
        print(c.set_active(argv[1], (argv[2].lower() == "true") if len(argv) > 2 else True))
    elif cmd == "deactivate-all":
        for line in c.unpublish_all():
            print(line)
        print("all workflows inactive")
    elif cmd == "publish-chain":
        for line in c.publish_chain(argv[1] if len(argv) > 1 else "PrqaMainPipeline"):
            print(line)
    elif cmd == "executions":
        for e in c.executions(int(argv[1]) if len(argv) > 1 else 20):
            print(json.dumps(e))
    elif cmd == "execution":
        print(json.dumps(c.execution(argv[1]), indent=2))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
