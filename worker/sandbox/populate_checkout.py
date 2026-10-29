#!/usr/bin/env python3
"""Helper-container extractor (ships in the sandbox IMAGE). Run with `python3 -I`.

    prqa-populate-checkout <dest dir> <job id> [/input/checkout.tar]     (default: tar on stdin)

Reads the per-job artifact (workspace.pack_checkout_artifact, v2 layout) from stdin and writes it into <dest dir>,
the job's named volume, so the sandbox can mount it read-only. Fails closed (exit 70) on anything the format does not
allow: non-regular members (symlinks, hard links, devices, fifos, directories are implied only), absolute paths, `..`,
top-level names other than repo/ and prqa/, trusted names other than prqa/manifest.json and prqa/prompt.md, a manifest
whose job_id differs from <job id>, a non-empty destination, or more than the size budget. Members are written with
mode 0444/0555 and directories end up 0555. Never prints file contents.
"""
import json
import os
import sys
import tarfile

MAX_BYTES = 1024 * 1024 * 1024          # 1 GiB hard ceiling inside the helper (the worker enforces PRQA_CHECKOUT_MAX_MB first)
CHUNK = 1024 * 1024                     # streaming copy: bounded memory per member
TRUSTED = {"prqa/manifest.json", "prqa/prompt.md"}
MARKER = ".prqa-populated.json"


def fail(msg, code=70):
    print("populate failed: " + msg, file=sys.stderr)
    sys.exit(code)


def safe_rel(name):
    if name.startswith("/") or name.startswith("\\") or "\x00" in name:
        fail("absolute or invalid member name")
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        fail("unsafe member path")
    if parts[0] not in ("repo", "prqa"):
        fail("unexpected top-level member: " + parts[0])
    if parts[0] == "prqa" and name not in TRUSTED:
        fail("unexpected trusted member: " + name)
    if parts[0] == "repo" and len(parts) < 2:
        fail("repo member without a path")
    return parts


def main(argv):
    if len(argv) not in (3, 4):
        print("usage: prqa-populate-checkout <dest dir> <job id> [<artifact file>]   (default: tar on stdin)", file=sys.stderr)
        return 64
    dest, job_id = argv[1], argv[2]
    source_path = argv[3] if len(argv) == 4 else None
    if source_path is not None and (not os.path.isfile(source_path) or os.path.islink(source_path)):
        fail("artifact file missing")
    if not os.path.isdir(dest):
        fail("destination is not a directory")
    existing = [e for e in os.listdir(dest) if e not in ("lost+found",)]
    if existing:
        fail("destination is not empty: " + ", ".join(sorted(existing)[:5]))
    total = 0
    files = 0
    manifest = None
    dirs_made = set()
    try:
        source = open(source_path, "rb") if source_path is not None else sys.stdin.buffer
        with tarfile.open(fileobj=source, mode="r|") as tar:
            for member in tar:
                parts = safe_rel(member.name)
                if member.isdir():
                    continue                                  # directories are implied by file paths
                if not member.isreg():
                    fail("non-regular member: " + member.name)
                if member.issym() or member.islnk():
                    fail("link member: " + member.name)
                total += int(member.size)
                if total > MAX_BYTES:
                    fail("artifact exceeds the helper size ceiling")
                target = os.path.join(dest, *parts)
                parent = os.path.dirname(target)
                if parent not in dirs_made:
                    os.makedirs(parent, mode=0o755, exist_ok=True)
                    dirs_made.add(parent)
                if os.path.lexists(target):
                    fail("duplicate member: " + member.name)
                src = tar.extractfile(member)
                if src is None:
                    fail("unreadable member: " + member.name)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                written = 0
                manifest_blob = bytearray() if member.name == "prqa/manifest.json" else None
                with os.fdopen(fd, "wb") as fh:
                    while True:
                        chunk = src.read(CHUNK)                       # bounded memory: never the whole member at once
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > member.size:
                            fail("member larger than its declared size: " + member.name)
                        fh.write(chunk)
                        if manifest_blob is not None:
                            if written > 1024 * 1024:
                                fail("manifest unreasonably large")
                            manifest_blob.extend(chunk)
                os.chmod(target, 0o555 if (member.mode & 0o100) else 0o444)
                files += 1
                if manifest_blob is not None:
                    try:
                        manifest = json.loads(bytes(manifest_blob).decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        fail("manifest is not JSON")
    except tarfile.TarError as exc:
        fail("archive error: " + type(exc).__name__)
    if not isinstance(manifest, dict):
        fail("manifest missing")
    if manifest.get("job_id") != job_id:
        fail("manifest job_id does not match this job")
    if not os.path.isdir(os.path.join(dest, "repo")) or not os.path.isfile(os.path.join(dest, "prqa", "prompt.md")):
        fail("artifact incomplete (repo/ or prqa/prompt.md missing)")
    for d in sorted(dirs_made, key=len, reverse=True):
        os.chmod(d, 0o555)
    with open(os.path.join(dest, MARKER), "w", encoding="utf-8") as fh:
        json.dump({"job_id": job_id, "files": files, "bytes": total}, fh)
    os.chmod(os.path.join(dest, MARKER), 0o444)
    print("populated %d files (%d bytes) for job %s" % (files, total, job_id))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
