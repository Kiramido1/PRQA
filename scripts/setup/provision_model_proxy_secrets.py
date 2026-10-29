#!/usr/bin/env python3
"""Provision the PRQA model proxy's credentials from the existing protected secrets file — without printing any value.

Single source of truth: GROQ_API_KEY in ~/.config/prqa/secrets.env (or PRQA_SECRETS_FILE). This script

  * derives the raw provider key file the model proxy needs (compose secret, PRQA_MODEL_PROVIDER_KEY_FILE): written
    atomically (O_EXCL temp file, mode 600, fsync, rename) next to the secrets file, outside the project drive, and
    rewritten only when GROQ_API_KEY changed (re-run after rotating the key);
  * keeps PRQA_MODEL_PROXY_ADMIN_TOKEN (proxy only), PRQA_LAUNCHER_TOKEN (worker <-> launcher),
    PRQA_MODEL_PROXY_MINT_TOKEN (launcher: issue/revoke its own job tokens only) and PRQA_MODEL_PROXY_PROMPT_TOKEN
    (n8n prompt generation: inference only, no tools), each independent of the provider key and of each other: a valid
    existing token is preserved, otherwise one is generated with the `secrets` CSPRNG;
  * records PRQA_MODEL_PROVIDER_KEY_FILE, PRQA_MODEL_PROXY_UID (owner of the key file: compose file secrets are bind
    mounts that keep the host mode, so only that uid can read a mode-600 file) and the admin token in the secrets file,
    rewritten atomically with mode 600; every other line is kept verbatim.

  * creates the private provider-budget ledger directory (PRQA_PROVIDER_BUDGET_DIR = budget/ next to the secrets file,
    mode 700, outside the project drive) shared by the model proxy and the direct evaluator (proxy/provider_budget.py),
    and records its path; an existing ledger is never touched.

Values never appear in argv, stdout, stderr or logs; output is a status per item. No network access, no provider call.

    python3 scripts/setup/provision_model_proxy_secrets.py            # provision / synchronise
    python3 scripts/setup/provision_model_proxy_secrets.py --check    # verify only, change nothing (exit 1 if not ready)
"""
from __future__ import annotations

import argparse
import hmac
import os
import re
import secrets
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KEY_NAME = "GROQ_API_KEY"
ADMIN_NAME = "PRQA_MODEL_PROXY_ADMIN_TOKEN"
LAUNCHER_NAME = "PRQA_LAUNCHER_TOKEN"            # worker <-> launcher hand-off credential (both read it via compose)
MINT_NAME = "PRQA_MODEL_PROXY_MINT_TOKEN"        # launcher -> proxy admin socket: issue/revoke own job tokens only
PROMPT_NAME = "PRQA_MODEL_PROXY_PROMPT_TOKEN"    # n8n workflow 03 -> proxy inference: prompt generation only
KEY_FILE_NAME = "PRQA_MODEL_PROVIDER_KEY_FILE"
UID_NAME = "PRQA_MODEL_PROXY_UID"
KEY_FILE_BASENAME = "model-provider.key"
BUDGET_DIR_NAME = "PRQA_PROVIDER_BUDGET_DIR"
BUDGET_DIR_BASENAME = "budget"
LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
KEY_RE = re.compile(r"^[\x21-\x7e]{20,512}$")            # printable, no whitespace; the value itself is never shown
UNSAFE_FS = {"fuseblk", "fuse", "vfat", "exfat", "ntfs", "ntfs3", "msdos", "cifs", "smb3", "nfs", "nfs4", "9p"}


class ProvisionError(Exception):
    pass


def _fs_type(path: Path) -> str:
    best, fstype = "", ""
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3:
                    mnt = parts[1].replace("\\040", " ")
                    if (str(path) == mnt or str(path).startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
                        best, fstype = mnt, parts[2]
    except OSError:
        return ""
    return fstype


def _check_private_location(path: Path, project_root: Path) -> None:
    real = path.resolve()
    if real == project_root or project_root in real.parents:
        raise ProvisionError(f"{path} is inside the project directory; credentials must live outside it")
    fstype = _fs_type(real)
    if fstype in UNSAFE_FS:
        raise ProvisionError(f"{path} is on a {fstype} filesystem that cannot enforce mode 600")


def _check_secrets_file(path: Path) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        raise ProvisionError(f"secrets file {path} does not exist") from None
    if not stat.S_ISREG(st.st_mode):
        raise ProvisionError(f"secrets file {path} is not a regular file")
    if st.st_uid != os.getuid():
        raise ProvisionError(f"secrets file {path} is not owned by the current user")
    if st.st_mode & 0o077:
        raise ProvisionError(f"secrets file {path} is readable by group/others (chmod 600 it first)")


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = LINE_RE.match(line.strip())
        if m:
            value = m.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            out[m.group(1)] = value
    return out


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _set_lines(text: str, updates: dict[str, str]) -> str:
    lines, seen = text.splitlines(), set()
    out = []
    for line in lines:
        m = LINE_RE.match(line.strip())
        if m and m.group(1) in updates:
            if m.group(1) in seen:
                continue                                        # duplicate definitions collapse into the first
            out.append(f"{m.group(1)}={updates[m.group(1)]}")
            seen.add(m.group(1))
        else:
            out.append(line)
    missing = [k for k in updates if k not in seen]
    if missing:
        out.append("# --- PRQA model proxy (maintained by scripts/setup/provision_model_proxy_secrets.py; values never printed) ---")
        out.extend(f"{k}={updates[k]}" for k in missing)
    return "\n".join(out) + "\n"


def _ensure_token(env: dict[str, str], name: str, label: str, must_differ: list[str], check_only: bool, status: dict[str, str]) -> str:
    """Preserve a valid existing token; otherwise (not in check mode) generate one with the CSPRNG. A token equal to any
    other credential is invalid. Never returns a value to print — callers only store it."""
    token = env.get(name, "")
    if TOKEN_RE.match(token) and not any(hmac.compare_digest(token, other) for other in must_differ if other):
        status[label] = "preserved"
        return token
    if check_only:
        status[label] = "MISSING OR INVALID"
        return token
    while True:
        token = secrets.token_urlsafe(48)
        if not any(hmac.compare_digest(token, other) for other in must_differ if other):
            break
    status[label] = "generated" if not env.get(name) else "replaced (previous value was invalid)"
    return token


def provision(secrets_file: Path, *, check_only: bool = False, project_root: Path = ROOT) -> dict[str, str]:
    """Returns a status per item (never a value). Raises ProvisionError on anything that is not safe or not ready."""
    _check_secrets_file(secrets_file)
    _check_private_location(secrets_file, project_root)
    text = secrets_file.read_text(encoding="utf-8")
    env = parse(text)
    key = env.get(KEY_NAME, "")
    if not key:
        raise ProvisionError(f"{KEY_NAME} is missing or empty in {secrets_file}")
    if not KEY_RE.match(key):
        raise ProvisionError(f"{KEY_NAME} in {secrets_file} is not a single printable token")
    key_file = secrets_file.parent / KEY_FILE_BASENAME
    _check_private_location(key_file, project_root)
    status: dict[str, str] = {}

    current = None
    if key_file.is_symlink():
        raise ProvisionError(f"{key_file} is a symlink; refusing to write through it")
    if key_file.exists():
        st = key_file.stat()
        if st.st_uid != os.getuid():
            raise ProvisionError(f"{key_file} is not owned by the current user")
        current = key_file.read_bytes().strip()
    in_sync = current is not None and hmac.compare_digest(current, key.encode("utf-8"))
    mode_ok = key_file.exists() and stat.S_IMODE(key_file.stat().st_mode) == 0o600
    if check_only:
        status["provider_key_file"] = "in sync" if in_sync else ("missing" if current is None else "OUT OF SYNC")
        status["provider_key_file_mode"] = "600" if mode_ok else "NOT 600"
    elif in_sync and mode_ok:
        status["provider_key_file"] = "unchanged"
    else:
        _atomic_write(key_file, key.encode("utf-8"))
        status["provider_key_file"] = "created" if current is None else "updated"

    token = _ensure_token(env, ADMIN_NAME, "admin_token", [key], check_only, status)
    launcher = _ensure_token(env, LAUNCHER_NAME, "launcher_token", [key, token], check_only, status)
    mint = _ensure_token(env, MINT_NAME, "mint_token", [key, token, launcher], check_only, status)
    prompt = _ensure_token(env, PROMPT_NAME, "prompt_token", [key, token, launcher, mint], check_only, status)

    budget_dir = secrets_file.parent / BUDGET_DIR_BASENAME
    _check_private_location(budget_dir, project_root)
    if budget_dir.is_symlink():
        raise ProvisionError(f"{budget_dir} is a symlink; refusing to use it")
    if budget_dir.exists():
        bst = budget_dir.stat()
        ok = stat.S_ISDIR(bst.st_mode) and bst.st_uid == os.getuid() and stat.S_IMODE(bst.st_mode) == 0o700
        status["budget_dir"] = "present (700)" if ok else "NOT A PRIVATE DIRECTORY"
        if not ok and not check_only:
            raise ProvisionError(f"{budget_dir} exists but is not a mode-700 directory owned by the current user")
    elif check_only:
        status["budget_dir"] = "missing"
    else:
        budget_dir.mkdir(mode=0o700)
        os.chmod(budget_dir, 0o700)
        status["budget_dir"] = "created (700)"

    updates = {KEY_FILE_NAME: str(key_file), ADMIN_NAME: token, LAUNCHER_NAME: launcher, MINT_NAME: mint, PROMPT_NAME: prompt,
               UID_NAME: str(os.getuid()), BUDGET_DIR_NAME: str(budget_dir)}
    recorded = all(env.get(k) == v for k, v in updates.items())
    if check_only:
        status["secrets_file_entries"] = "complete" if recorded else "INCOMPLETE"
    elif recorded:
        status["secrets_file_entries"] = "unchanged"
    else:
        _atomic_write(secrets_file, _set_lines(text, updates).encode("utf-8"))
        status["secrets_file_entries"] = "updated"
    status["provider_key_file_path"] = str(key_file)
    return status


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="verify only; change nothing")
    args = ap.parse_args(argv)
    path = Path(os.environ.get("PRQA_SECRETS_FILE") or Path.home() / ".config/prqa/secrets.env")
    try:
        status = provision(path, check_only=args.check)
    except ProvisionError as exc:
        print(f"model proxy credentials NOT ready: {exc}", file=sys.stderr)
        return 3
    for k, v in status.items():
        print(f"{k}: {v}")
    if args.check and not is_ready(status):
        return 1
    return 0


def is_ready(status: dict[str, str]) -> bool:
    return (status.get("provider_key_file") == "in sync" and status.get("provider_key_file_mode") == "600"
            and all(status.get(k) == "preserved" for k in ("admin_token", "launcher_token", "mint_token", "prompt_token"))
            and status.get("budget_dir") == "present (700)" and status.get("secrets_file_entries") == "complete")


if __name__ == "__main__":
    sys.exit(main())
