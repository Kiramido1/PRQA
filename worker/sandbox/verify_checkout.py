#!/usr/bin/env python3
"""Sandbox-side artifact verifier (ships in the sandbox IMAGE, never in the artifact). Run with `python3 -I`.

    prqa-verify-checkout <checkout dir> <repo> <sha> <job id> <expected tree sha256> <expected prompt sha256>

Fails closed (exit 66) unless ALL hold:
  * <dir>/prqa/manifest.json exists, is JSON, artifact_version 2.0, tree_hash_version prqa-tree-v1
  * manifest.repository == repo, manifest.head_sha == sha, manifest.job_id == job id, git_dir_included is false
  * <dir>/repo exists, contains at least one regular file and no .git, no symlinks, no special files, no '..' components
  * the recomputed tree hash (same algorithm as worker/prqa_worker/workspace.py::tree_digest) and file count match the
    embedded manifest AND the worker-expected value passed on the command line (same for the prompt hash)
  * <dir>/prqa/prompt.md exists, is non-empty and its sha256 matches manifest.prompt_sha256
  * nothing else is present under <dir>/prqa (no repository-controlled file can masquerade as trusted)
Prints "checkout verified …" on success. Never prints file contents.
"""
import hashlib
import json
import os
import stat
import sys

TREE_HASH_VERSION = "prqa-tree-v1"
ARTIFACT_VERSION = "2.0"


def fail(msg):
    print("checkout verification failed: " + msg, file=sys.stderr)
    sys.exit(66)


def tree_digest(root):
    entries = []
    for cur, dirs, names in os.walk(root):
        for d in list(dirs):
            if os.path.islink(os.path.join(cur, d)):
                fail("symlinked directory in checkout: " + os.path.relpath(os.path.join(cur, d), root))
            if d == ".git":
                fail(".git directory present in checkout")
        dirs.sort()
        rel_root = os.path.relpath(cur, root)
        for name in names:
            full = os.path.join(cur, name)
            rel = name if rel_root == "." else os.path.join(rel_root, name)
            if ".." in rel.split(os.sep):
                fail("unsafe path in checkout")
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                fail("symlink in checkout: " + rel)
            if not stat.S_ISREG(st.st_mode):
                fail("unsupported file type in checkout: " + rel)
            if rel == ".git" or rel.startswith(".git" + os.sep):
                fail(".git entry present in checkout")
            entries.append((rel.replace(os.sep, "/"), full, st))
    entries.sort(key=lambda e: e[0])
    h = hashlib.sha256()
    h.update(TREE_HASH_VERSION.encode("utf-8") + b"\0")
    for rel, full, st in entries:
        mode = b"x" if st.st_mode & 0o100 else b"r"
        with open(full, "rb") as fh:
            content = hashlib.sha256(fh.read()).hexdigest().encode("ascii")
        h.update(rel.encode("utf-8") + b"\0" + mode + b"\0" + str(st.st_size).encode("ascii") + b"\0" + content + b"\0")
    return h.hexdigest(), len(entries)


def main(argv):
    if len(argv) != 7:
        print("usage: prqa-verify-checkout <checkout dir> <repo> <sha> <job id> <expected tree sha256> <expected prompt sha256>", file=sys.stderr)
        return 64
    base, repo, sha, job_id, exp_tree, exp_prompt = argv[1:7]
    import re
    if not re.match(r"^[0-9a-f]{64}$", exp_tree) or not re.match(r"^[0-9a-f]{64}$", exp_prompt):
        fail("expected hashes missing or malformed")
    stray = sorted(set(os.listdir(base)) - {"repo", "prqa", ".prqa-populated.json", "lost+found"})
    if stray:
        fail("unexpected top-level entries: " + ", ".join(stray[:5]))
    trusted = os.path.join(base, "prqa")
    repo_dir = os.path.join(base, "repo")
    manifest_path = os.path.join(trusted, "manifest.json")
    prompt_path = os.path.join(trusted, "prompt.md")
    if not os.path.isdir(base):
        fail("checkout dir missing")
    if not os.path.isdir(trusted) or os.path.islink(trusted):
        fail("trusted prqa/ dir missing")
    extra = sorted(set(os.listdir(trusted)) - {"manifest.json", "prompt.md"})
    if extra:
        fail("unexpected entries under prqa/: " + ", ".join(extra[:5]))
    if not os.path.isfile(manifest_path) or os.path.islink(manifest_path):
        fail("manifest missing")
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            m = json.load(fh)
        if not isinstance(m, dict):
            raise ValueError("not an object")
    except Exception as exc:  # noqa: BLE001
        fail("manifest unreadable: " + type(exc).__name__)
    problems = []
    if m.get("artifact_version") != ARTIFACT_VERSION:
        problems.append("artifact_version != " + ARTIFACT_VERSION)
    if m.get("tree_hash_version") != TREE_HASH_VERSION:
        problems.append("tree_hash_version != " + TREE_HASH_VERSION)
    if m.get("repository") != repo:
        problems.append("manifest repository != --repo")
    if m.get("head_sha") != sha:
        problems.append("manifest head_sha != --sha")
    if not job_id or m.get("job_id") != job_id:
        problems.append("manifest job_id != PRQA_JOB_ID")
    if m.get("git_dir_included") is not False:
        problems.append("artifact must exclude .git")
    if problems:
        fail("; ".join(problems))
    if not os.path.isdir(repo_dir) or os.path.islink(repo_dir):
        fail("repo/ missing")
    digest, count = tree_digest(repo_dir)
    if count == 0:
        fail("checkout is empty")
    if not isinstance(m.get("files"), int) or m["files"] != count:
        fail("file count mismatch (manifest %s, found %d)" % (m.get("files"), count))
    if m.get("tree_sha256") != digest:
        fail("tree hash mismatch (manifest)")
    if exp_tree != digest:
        fail("tree hash mismatch (worker-expected)")
    if not os.path.isfile(prompt_path) or os.path.islink(prompt_path):
        fail("prompt missing")
    with open(prompt_path, "rb") as fh:
        blob = fh.read()
    if len(blob.strip()) < 200:
        fail("prompt is empty or too short")
    prompt_digest = hashlib.sha256(blob).hexdigest()
    if prompt_digest != m.get("prompt_sha256"):
        fail("prompt hash mismatch (manifest)")
    if prompt_digest != exp_prompt:
        fail("prompt hash mismatch (worker-expected)")
    print("checkout verified: %s @ %s (%d files, job %s)" % (repo, sha, count, job_id))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
