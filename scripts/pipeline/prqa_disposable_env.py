#!/usr/bin/env python3
"""Disposable, isolated API environment for ONE PRQA run against a FastAPI/alembic target. Per-project recipes are the
PROFILES below (`--profile amenly` — Kiramido1/Amenly-Product, the default and only one).

    python3 scripts/pipeline/prqa_disposable_env.py up     --run-id ID --git-dir CLONE --sha SHA --state-dir DIR [--profile P] [--seed]
    python3 scripts/pipeline/prqa_disposable_env.py status --state-dir DIR
    python3 scripts/pipeline/prqa_disposable_env.py probe  --state-dir DIR     (health + OpenAPI from the sandbox network)
    python3 scripts/pipeline/prqa_disposable_env.py auth-probe --state-dir DIR --register-path P --me-path P
                                                                   (a synthetic self-registered identity works; codes only)
    python3 scripts/pipeline/prqa_disposable_env.py down   --state-dir DIR

What `up` creates (every resource labelled prqa.disposable=<run id> and recorded in <state-dir>/env-state.json):
  * source: `git archive <sha> backend` of an isolated clone (never the user's working clone) into <private-dir>/src;
  * image  prqa/disposable-api:<run id>  built from that tree's backend/Dockerfile (BUILD_SHA=<sha>);
  * network prqa-dx-<run id>-db: internal (no egress), only the database and the API are attached;
  * container prqa-dx-<run id>-db: postgres:16-alpine, data on tmpfs (vanishes with the container), random password;
  * one-shot containers: the profile's migration (+ its optional seed command, synthetic data only) against
    that database only;
  * container prqa-dx-<run id>-api: the image's own server, DEBUG=false, docs enabled, random SECRET_KEY, non-root, read-only
    rootfs, cap-drop ALL, no-new-privileges, no Docker socket, no host mounts, no published port. Attached to the DB
    network and to the PRQA sandbox network (internal; --sandbox-network) under the alias --api-alias, so the OpenCode
    sandbox reaches it at http://<alias>:8000 and nothing else does.
The DB password and SECRET_KEY are generated here, written only to mode-600 env files inside --private-dir (a local,
non-FUSE directory; removed by `down`) and never printed. Health (/health) and OpenAPI (/api/v1/openapi.json) are verified from a
throw-away probe container on the sandbox network, i.e. exactly the path the sandbox will use.

`down` removes exactly the resources listed in the state file that also carry this run's label; nothing else.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

LABEL = "prqa.disposable"
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,40}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DB_IMAGE = "postgres:16-alpine"
PROBE_IMAGE = "prqa/opencode-sandbox:local"
HARDEN = ["--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--pids-limit", "512"]
REDIS_IMAGE = "redis:7-alpine"


def _amenly_env(pw: str, key: str, db_host: str, redis_host: str) -> str:
    # Postgres + Redis only. Qdrant / Ollama / Supabase (AI and document features) point nowhere: those endpoints fail
    # inside the disposable environment instead of reaching a real service.
    return (f"DATABASE_URL=postgresql+psycopg://amenly:{pw}@{db_host}:5432/amenly\nSECRET_KEY={key}\n"
            "DEBUG=false\nENABLE_DOCS=true\nPROJECT_NAME=Amenly\n"
            f"REDIS_URL=redis://{redis_host}:6379/0\nQDRANT_URL=http://127.0.0.1:9\nOLLAMA_URL=http://127.0.0.1:9\n"
            "SUPABASE_URL=\nSUPABASE_KEY=\nBACKEND_CORS_ORIGINS=[]\n")


# Per-project recipe: database identity, app env (generated secrets only — never the repository's .env), optional Redis,
# migration and seed commands, extra writable tmpfs paths for the read-only API container.
# Amenly's alembic chain does not apply to an empty database (f52949bac1f6 drops `position_control_mappings`, which no
# earlier revision creates; pre-existing on main, not part of any PR). When `upgrade head` fails, the schema is built
# from the ORM models the API itself uses (app.database.base imports them all) and stamped at head; migrate.log says so.
AMENLY_MIGRATE = (
    "alembic upgrade head || { echo 'PRQA: alembic upgrade head failed on an empty database; falling back to the ORM schema "
    "(Base.metadata.create_all) + alembic stamp head'; PYTHONPATH=. python -c 'import asyncio, os\n"
    "from sqlalchemy.ext.asyncio import create_async_engine\n"
    "from app.database.base import Base\n"
    "async def main():\n"
    "    e = create_async_engine(os.environ[\"DATABASE_URL\"])\n"
    "    async with e.begin() as c:\n"
    "        await c.run_sync(Base.metadata.create_all)\n"
    "    await e.dispose()\n"
    "asyncio.run(main())' && alembic stamp head; } && alembic current")

PROFILES: dict[str, dict[str, Any]] = {
    "amenly": {"db": "amenly", "app_env": _amenly_env, "redis": True, "migrate": AMENLY_MIGRATE,
               "seed": None, "api_tmpfs": ["/app/logs:rw,size=64m,uid=65534"]},
}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def docker(*args: str, check: bool = True, timeout: int = 900, capture: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["docker", *args], capture_output=capture, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise SystemExit(f"docker {args[0]} {args[1] if len(args) > 1 else ''} failed (rc {proc.returncode}): {(proc.stderr or '')[-800:]}")
    return proc


class Env:
    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.state_path = state_dir / "env-state.json"
        self.state: dict[str, Any] = json.loads(self.state_path.read_text()) if self.state_path.is_file() else {}

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=1, sort_keys=True) + "\n")

    def record(self, kind: str, name: str) -> None:
        self.state.setdefault("resources", []).append({"kind": kind, "name": name, "created_at": now()})
        self.save()

    def step(self, name: str, **info: Any) -> None:
        self.state.setdefault("steps", []).append({"step": name, "at": now(), **info})
        self.save()


def up(args: argparse.Namespace) -> int:
    if not args.private_dir:
        raise SystemExit("--private-dir is required (a local, non-FUSE directory: env files must keep mode 600)")
    if not RUN_ID_RE.match(args.run_id) or not SHA_RE.match(args.sha):
        raise SystemExit("run id or sha malformed")
    env = Env(Path(args.state_dir))
    if env.state.get("resources"):
        raise SystemExit("state dir already has resources; run `down` first")
    rid, label = args.run_id, f"{LABEL}={args.run_id}"
    prof = PROFILES[args.profile]
    env.state.update({"profile": args.profile, "run_id": rid, "sha": args.sha, "sandbox_network": args.sandbox_network, "api_alias": args.api_alias,
                      "base_url": f"http://{args.api_alias}:8000", "started_at": now(),
                      "private_dir": str(Path(args.private_dir).resolve())})
    env.save()
    sb = docker("network", "inspect", "--format", '{{.Internal}} {{index .Labels "prqa.net"}}', args.sandbox_network).stdout.split()
    if sb != ["true", "sandbox"]:
        raise SystemExit(f"{args.sandbox_network} is not the internal PRQA sandbox network")
    if docker("ps", "-a", "--filter", f"network={args.sandbox_network}", "--filter", f"name=^{args.api_alias}$", "-q").stdout.strip():
        raise SystemExit("api alias already in use")

    # 1. exact source tree (git archive: no .git, no hooks, no untracked files)
    src = Path(env.state["private_dir"]) / "src"
    if src.exists():
        shutil.rmtree(src)
    src.mkdir(parents=True)
    head = subprocess.run(["git", "-C", args.git_dir, "rev-parse", f"{args.sha}^{{commit}}"], capture_output=True, text=True, check=True).stdout.strip()
    if head != args.sha:
        raise SystemExit("sha not present in the isolated clone")
    arch = subprocess.run(["git", "-C", args.git_dir, "archive", "--format=tar", args.sha, "backend"], capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(src)], input=arch, check=True)
    for leftover in (src / "backend" / ".env",):
        if leftover.exists():
            raise SystemExit("a committed backend/.env exists at this SHA; refusing (could carry real endpoints)")
    env.step("source", path=str(src / "backend"), sha=args.sha)

    # 2. secrets (never printed) → private env file
    private = Path(env.state["private_dir"])
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    pw, key = secrets.token_hex(24), secrets.token_hex(32)
    db_host, redis_host, dbn = f"prqa-dx-{rid}-db", f"prqa-dx-{rid}-redis", prof["db"]
    db_env, app_env = private / "db.env", private / "app.env"
    for path, text in ((db_env, f"POSTGRES_USER={dbn}\nPOSTGRES_DB={dbn}\nPOSTGRES_PASSWORD={pw}\n"),
                       (app_env, prof["app_env"](pw, key, db_host, redis_host))):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
    del pw, key

    # 3. image
    image = f"prqa/disposable-api:{rid}"
    t0 = time.time()
    docker("build", "-q", "--label", label, "--build-arg", f"BUILD_SHA={args.sha}", "-t", image, str(src / "backend"), timeout=1800)
    env.record("image", image)
    env.step("build", image=image, seconds=round(time.time() - t0, 1))

    # 4. isolated DB network + database (tmpfs data)
    dbnet = f"prqa-dx-{rid}-db"
    docker("network", "create", "--internal", "--label", label, dbnet)
    env.record("network", dbnet)
    docker("run", "-d", "--name", db_host, "--label", label, "--network", dbnet, "--env-file", str(db_env),
           "--tmpfs", "/var/lib/postgresql/data:rw,size=1g", "--memory", "1g", DB_IMAGE)
    env.record("container", db_host)
    for _ in range(60):
        if docker("exec", db_host, "pg_isready", "-U", dbn, "-d", dbn, check=False).returncode == 0:
            break
        time.sleep(1)
    else:
        raise SystemExit("database did not become ready")
    env.step("database", container=db_host, image=DB_IMAGE, network=dbnet)
    if prof["redis"]:
        # non-root from the start: with ALL capabilities dropped the image's entrypoint cannot chown /data and switch user
        docker("run", "-d", "--name", redis_host, "--label", label, "--network", dbnet, *HARDEN, "--read-only", "--user", "999:999",
               "--tmpfs", "/data:rw,size=64m,uid=999", "--memory", "256m", REDIS_IMAGE, "redis-server", "--save", "", "--appendonly", "no")
        env.record("container", redis_host)
        env.step("redis", container=redis_host, image=REDIS_IMAGE, network=dbnet)

    # 5. migrations (+ synthetic seed) against THIS database only
    common = ["--rm", "--label", label, "--network", dbnet, "--env-file", str(app_env), *HARDEN, "--user", "65534:65534",
              "--read-only", "--tmpfs", "/tmp:rw,size=64m", "-e", "HOME=/tmp"]
    mig = docker("run", "--name", f"prqa-dx-{rid}-migrate", *common, image, "sh", "-c", prof["migrate"],
                 check=False, timeout=600)
    (env.dir / "migrate.log").write_text((mig.stdout or "") + (mig.stderr or ""))
    env.step("migrate", rc=mig.returncode)
    if mig.returncode != 0:
        raise SystemExit("alembic upgrade failed (see migrate.log)")
    if args.seed and prof["seed"]:
        sd = docker("run", "--name", f"prqa-dx-{rid}-seed", *common, image, *prof["seed"], check=False, timeout=900)
        seed_log = (sd.stdout or "")[-20000:] + (sd.stderr or "")[-20000:]
        # the target's seed may print its demo credentials: never keep them in evidence
        seed_log = re.sub(r"(?i)(password[^:\n]{0,40}:)[^\n]*", r"\1 [REDACTED]", seed_log)
        (env.dir / "seed.log").write_text(seed_log)
        env.step("seed", rc=sd.returncode)
        if sd.returncode != 0:
            raise SystemExit("seed failed (see seed.log)")

    # 6. API: DB network + sandbox network (alias), hardened, no published port
    api = f"prqa-dx-{rid}-api"
    docker("create", "--name", api, "--label", label, "--network", dbnet, "--env-file", str(app_env), *HARDEN,
           "--user", "65534:65534", "--read-only", "--tmpfs", "/tmp:rw,size=64m",
           *[a for t in prof["api_tmpfs"] for a in ("--tmpfs", t)], "-e", "HOME=/tmp", "--memory", "1g", image)
    env.record("container", api)
    docker("network", "connect", "--alias", args.api_alias, args.sandbox_network, api)
    docker("start", api)
    env.step("api", container=api, networks=[dbnet, args.sandbox_network], alias=args.api_alias)

    # 7. health + OpenAPI from the sandbox network (the path OpenCode will use)
    probe = (f"for i in $(seq 1 60); do curl -fsS {env.state['base_url']}/health >/dev/null 2>&1 && break; sleep 1; done; "
             f"curl -sS -o /dev/null -w 'health %{{http_code}}\\n' {env.state['base_url']}/health; "
             f"curl -sS -o /tmp/o.json -w 'openapi %{{http_code}}\\n' {env.state['base_url']}/api/v1/openapi.json; "
             "python3 -c \"import json;d=json.load(open('/tmp/o.json'));print('paths',len(d.get('paths',{})));print('title',d['info']['title'],d['info'].get('version'))\" 2>/dev/null"
             " || node -e \"const d=require('/tmp/o.json');console.log('paths',Object.keys(d.paths||{}).length);console.log('title',d.info.title,d.info.version)\"; "
             f"curl -sS {env.state['base_url']}/api/v1/openapi.json")
    pr = docker("run", "--rm", "--label", label, "--network", args.sandbox_network, *HARDEN, "--entrypoint", "sh", PROBE_IMAGE, "-c", probe,
                check=False, timeout=180)
    lines = (pr.stdout or "").split("\n")
    spec_text = lines[-1] if lines and lines[-1].startswith("{") else "\n".join(l for l in lines if l.startswith("{"))
    summary = [l for l in lines if l.startswith(("health", "openapi", "paths", "title"))]
    if spec_text:
        (env.dir / "openapi.json").write_text(spec_text)
    egress = docker("exec", api, "python", "-c",
                    "import socket;s=socket.socket();s.settimeout(3);\ntry:\n s.connect(('1.1.1.1',443));print('egress OPEN')\nexcept Exception as e:\n print('egress blocked')",
                    check=False).stdout.strip()
    env.step("verify", probe=summary, api_egress=egress)
    ok = "health 200" in summary and "openapi 200" in summary
    if not ok:  # keep the API's own startup output for diagnosis (credential-looking values redacted)
        lg = docker("logs", "--tail", "200", api, check=False)
        api_log = re.sub(r"(?i)((?:password|secret|token|key)[^=:\n]{0,40}[=:])[^\n]*", r"\1 [REDACTED]",
                         (lg.stdout or "")[-20000:] + (lg.stderr or "")[-20000:])
        (env.dir / "api.log").write_text(api_log)
    env.state["healthy"] = ok
    env.save()
    print(json.dumps({"healthy": ok, "base_url": env.state["base_url"], "probe": summary, "api_egress": egress,
                      "resources": env.state["resources"]}, indent=1))
    return 0 if ok else 1


def down(args: argparse.Namespace) -> int:
    env = Env(Path(args.state_dir))
    rid = env.state.get("run_id")
    if not rid:
        raise SystemExit("no state to tear down")
    removed, skipped = [], []
    for res in reversed(env.state.get("resources", [])):
        kind, name = res["kind"], res["name"]
        fmt = "{{index .Config.Labels \"%s\"}}" % LABEL if kind == "container" else "{{index .Labels \"%s\"}}" % LABEL
        if kind == "image":
            fmt = "{{index .Config.Labels \"%s\"}}" % LABEL
        insp = docker(kind if kind != "container" else "container", "inspect", "--format", fmt, name, check=False)
        if insp.returncode != 0:
            skipped.append({"name": name, "why": "already gone"})
            continue
        if insp.stdout.strip() != rid:
            skipped.append({"name": name, "why": "label mismatch: not ours, left untouched"})
            continue
        if kind == "container":
            docker("rm", "-f", "-v", name, check=False)
        elif kind == "network":
            docker("network", "rm", name, check=False)
        elif kind == "image":
            docker("image", "rm", name, check=False)
        removed.append(f"{kind}:{name}")
    leftovers = docker("ps", "-a", "--filter", f"label={LABEL}={rid}", "-q").stdout.split()
    private = Path(env.state.get("private_dir") or "")
    if env.state.get("private_dir") and private.is_dir():
        shutil.rmtree(private)
    env.step("teardown", removed=removed, skipped=skipped, leftover_containers=len(leftovers))
    env.state["torn_down_at"] = now()
    env.save()
    print(json.dumps({"removed": removed, "skipped": skipped, "leftover_containers": len(leftovers)}, indent=1))
    return 0 if not leftovers else 1


def probe(args: argparse.Namespace) -> int:
    """Re-verify, from a throw-away container on the sandbox network, that the recorded API answers /health and its
    OpenAPI document (the exact path the OpenCode sandbox uses). Read-only; creates nothing that outlives the call."""
    env = Env(Path(args.state_dir))
    base, net, rid = env.state.get("base_url"), env.state.get("sandbox_network"), env.state.get("run_id")
    if not base or not net or env.state.get("torn_down_at"):
        print(json.dumps({"reachable": False, "reason": "no live environment recorded"}))
        return 1
    script = (f"curl -sS -m 10 -o /dev/null -w 'health %{{http_code}}\\n' {base}/health; "
              f"curl -sS -m 10 -o /dev/null -w 'openapi %{{http_code}}\\n' {base}/api/v1/openapi.json")
    pr = docker("run", "--rm", "--label", f"{LABEL}={rid}", "--network", net, *HARDEN, "--entrypoint", "sh", PROBE_IMAGE, "-c", script,
                check=False, timeout=120)
    lines = [l for l in (pr.stdout or "").splitlines() if l.startswith(("health", "openapi"))]
    ok = "health 200" in lines and "openapi 200" in lines
    print(json.dumps({"reachable": ok, "base_url": base, "probe": lines}))
    return 0 if ok else 1


AUTH_PATH_RE = re.compile(r"^/[A-Za-z0-9/_-]{1,80}$")
AUTH_PROBE_PY = r"""
import json, secrets, urllib.request, urllib.error
base, reg, me = "@BASE@", "@REG@", "@ME@"
def call(method, path, body=None, token=None):
    h = {"content-type": "application/json"}
    if token:
        h["authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
def find_token(d):
    if isinstance(d, dict):
        if isinstance(d.get("access_token"), str):
            return d["access_token"]
        for v in d.values():
            t = find_token(v)
            if t:
                return t
    return ""
spec = json.loads(call("GET", "/api/v1/openapi.json")[1] or b"{}")
op = (spec.get("paths", {}).get(reg) or {}).get("post") or {}
schema = ((op.get("requestBody") or {}).get("content", {}).get("application/json") or {}).get("schema") or {}
if "$ref" in schema:
    node = spec
    for part in schema["$ref"].split("/")[1:]:
        node = node[part]
    schema = node
n = secrets.token_hex(6)
body = {}
for f in schema.get("required") or ["email", "password"]:
    lf = f.lower()
    body[f] = (f"prqa-probe-{n}@prqa-synthetic.dev" if "email" in lf else "Prqa-" + secrets.token_urlsafe(18) if "password" in lf
               else f"PRQA probe {n}")
st, raw = call("POST", reg, body)
print("register", st)
try:
    tok = find_token(json.loads(raw or b"{}"))
except ValueError:
    tok = ""
print("token present" if tok else "token absent")
print("me_anonymous", call("GET", me)[0])
print("me_token", call("GET", me, token=tok)[0] if tok else 0)
"""


def auth_probe(args: argparse.Namespace) -> int:
    """Prove that the sandbox can provision its OWN synthetic identity: from a throw-away container on the sandbox network,
    POST <register> with a random synthetic user (password generated inside the container, never printed or stored),
    expect 201 + a bearer token, then GET <me> without the token (expect 401) and with it (expect 200). Prints status
    codes only. The synthetic user lives in the disposable database and is destroyed by `down`."""
    env = Env(Path(args.state_dir))
    base, net, rid = env.state.get("base_url"), env.state.get("sandbox_network"), env.state.get("run_id")
    if not base or not net or env.state.get("torn_down_at"):
        print(json.dumps({"ok": False, "reason": "no live environment recorded"}))
        return 1
    if not (AUTH_PATH_RE.match(args.register_path or "") and AUTH_PATH_RE.match(args.me_path or "")):
        print(json.dumps({"ok": False, "reason": "auth paths missing or malformed"}))
        return 1
    # The request body is built from the register operation's OWN schema in the running API's OpenAPI document (required
    # fields only; email → synthetic address, password → generated inside the container); the token is the first
    # `access_token` anywhere in the response. Nothing but status codes is printed.
    script = "python3 -I - <<'PY'\n" + AUTH_PROBE_PY.replace("@BASE@", base).replace("@REG@", args.register_path).replace("@ME@", args.me_path) + "\nPY"
    pr = docker("run", "--rm", "--label", f"{LABEL}={rid}", "--network", net, *HARDEN, "--entrypoint", "sh", PROBE_IMAGE, "-c", script,
                check=False, timeout=120)
    lines = [l for l in (pr.stdout or "").splitlines() if l.startswith(("register", "token", "me_"))]
    ok = lines == ["register 201", "token present", "me_anonymous 401", "me_token 200"]
    env.step("auth_probe", lines=lines, ok=ok)
    print(json.dumps({"ok": ok, "probe": lines, "register_path": args.register_path, "me_path": args.me_path}))
    return 0 if ok else 1


def status(args: argparse.Namespace) -> int:
    env = Env(Path(args.state_dir))
    rid = env.state.get("run_id")
    out = docker("ps", "-a", "--filter", f"label={LABEL}={rid}", "--format", "{{.Names}} {{.Status}}").stdout.strip() if rid else ""
    print(json.dumps({"run_id": rid, "base_url": env.state.get("base_url"), "containers": out.splitlines()}, indent=1))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["up", "down", "status", "probe", "auth-probe"])
    ap.add_argument("--run-id")
    ap.add_argument("--git-dir")
    ap.add_argument("--sha")
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--private-dir", help="off the project drive (mode 700): source tree + generated env files; removed by down")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="amenly", help="per-project recipe (database, env, services)")
    ap.add_argument("--sandbox-network", default="prqa_prqa_sandbox")
    ap.add_argument("--api-alias", default="prqa-target-api")
    ap.add_argument("--register-path", help="auth-probe: documented self-registration operation path")
    ap.add_argument("--me-path", help="auth-probe: documented current-user operation path")
    args = ap.parse_args(argv)
    return {"up": up, "down": down, "status": status, "probe": probe, "auth-probe": auth_probe}[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
