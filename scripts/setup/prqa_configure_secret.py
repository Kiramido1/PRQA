#!/usr/bin/env python3
"""Store ONE PRQA credential from masked local terminal input (never argv, never chat, never printed).

    python3 scripts/setup/prqa_configure_secret.py discord [--send-test]   # DISCORD_WEBHOOK_URL (+ one labeled setup message)
    python3 scripts/setup/prqa_configure_secret.py github                  # GITHUB_TOKEN (fine-grained, read-only; only for a private project)
    python3 scripts/setup/prqa_configure_secret.py discord --test-only     # re-send the labeled setup message with the saved URL

Run it in your OWN terminal (it needs a TTY for getpass). The value is validated, then written to
~/.config/prqa/secrets.env (or $PRQA_SECRETS_FILE) atomically: unrelated entries are preserved, an existing entry is
replaced (no duplicates), the file stays mode 600 and its directory 700. The previous file is kept as a mode-600 backup
in the same directory. Neither the value nor anything derived from it (prefix, length, hash) is printed or logged.

discord: https only, host discord.com / discordapp.com, path /api/webhooks/<id>/<token>, no query/fragment; with
         --send-test exactly one message "PRQA Discord integration test — configuration verification only; no PR test
         was executed." is posted (redirects refused) and only its HTTP status + time are recorded in
         runs/prqa-auto/discord-setup-test.json.
github:  fine-grained PAT (github_pat_…); verified with three GET reads on the selected project (repository, pulls,
         commits); a classic token (it reports OAuth scopes) or any failed read is refused and nothing is stored.
"""
from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_d = importlib.util.spec_from_file_location("prqa_discord_delivery", ROOT / "scripts" / "pipeline" / "prqa_discord_delivery.py")
D = importlib.util.module_from_spec(_d)
_d.loader.exec_module(D)  # type: ignore[union-attr]
GITHUB_FINE_RE = re.compile(r"^github_pat_[A-Za-z0-9_]{22,255}$")
NAMES = {"discord": "DISCORD_WEBHOOK_URL", "github": "GITHUB_TOKEN"}
SETUP_RECORD = ROOT / "runs" / "prqa-auto" / "discord-setup-test.json"


def secrets_path() -> Path:
    return Path(os.environ.get("PRQA_SECRETS_FILE") or Path.home() / ".config/prqa/secrets.env")


def read_value(path: Path, name: str) -> str:
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(rf"^{name}=(.*)$", line.strip())
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return ""


def store(path: Path, name: str, value: str) -> None:
    """Atomic replace-or-append of NAME=value; 700 dir, 600 file, 600 backup of the previous file."""
    if "\n" in value or "\r" in value:
        raise ValueError("value contains a line break")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines, done = [], False
    for line in old.splitlines():
        if re.match(rf"^\s*{name}=", line):
            if not done:
                lines.append(f"{name}={value}")
                done = True
            continue
        lines.append(line)
    if not done:
        lines.append(f"{name}={value}")
    if old:
        bak = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d%H%M%S')}")
        fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(old)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def record_setup_test(res: dict) -> None:
    SETUP_RECORD.parent.mkdir(parents=True, exist_ok=True)
    SETUP_RECORD.write_text(json.dumps({"ok": res["ok"], "status_code": res["status_code"], "at": res["at"],
                                        "message": D.SETUP_TEST_TEXT}, indent=1) + "\n")


def verify_github(token: str) -> dict:
    spec = importlib.util.spec_from_file_location("prqa_auto_runner", ROOT / "scripts" / "pipeline" / "prqa_auto_runner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.verify_github_read_token(token)


def main(argv=None, *, prompt=getpass.getpass, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("item", choices=sorted(NAMES))
    ap.add_argument("--send-test", action="store_true", help="discord: post the one labeled setup message after storing")
    ap.add_argument("--test-only", action="store_true", help="discord: only post the setup message with the saved URL")
    args = ap.parse_args(argv)
    path, name = secrets_path(), NAMES[args.item]
    if args.test_only:
        url = read_value(path, name)
        if not D.valid_webhook(url):
            out(json.dumps({"ok": False, "error": f"{name} missing or invalid in the secrets file"}))
            return 2
        res = D.setup_test(url)
        record_setup_test(res)
        out(json.dumps({"setup_test": res}))
        return 0 if res["ok"] else 1
    if prompt is getpass.getpass and not sys.stdin.isatty():
        out(json.dumps({"ok": False, "error": "no terminal: run this command in your own terminal (masked input needs a TTY)"}))
        return 2
    value = prompt(f"{name} (input hidden): ").strip()
    if args.item == "discord":
        if not D.valid_webhook(value):
            out(json.dumps({"ok": False, "error": "not an https Discord incoming-webhook URL (discord.com/discordapp.com /api/webhooks/<id>/<token>, no query); nothing stored"}))
            return 2
        store(path, name, value)
        out(json.dumps({"ok": True, "stored": name, "file": str(path), "mode": "600"}))
        if args.send_test:
            res = D.setup_test(value)
            record_setup_test(res)
            out(json.dumps({"setup_test": res}))
            return 0 if res["ok"] else 1
        return 0
    if not GITHUB_FINE_RE.match(value):
        out(json.dumps({"ok": False, "error": "not a fine-grained token (github_pat_…); classic tokens are refused; nothing stored"}))
        return 2
    check = verify_github(value)
    if not check["ok"]:
        out(json.dumps({"ok": False, "error": "token verification failed; nothing stored", "reads": check}))
        return 2
    store(path, name, value)
    out(json.dumps({"ok": True, "stored": name, "file": str(path), "mode": "600", "reads": check}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
