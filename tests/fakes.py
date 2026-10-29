"""Shared test fakes: a stateful fake `docker` CLI (no Docker needed) matching the no-attach transport."""
from __future__ import annotations



class FakeDocker:
    """Tracks containers/volumes/copies, answers inspect truthfully, records every call. Failure injection per step.

    Supported argv: volume create|rm|inspect|ls, create, cp, start, wait, logs, rm -f, container inspect, ps -a,
    image inspect, network inspect. Sandbox containers run `sandbox_exit_after` polls before exiting with `start_rc`."""

    def __init__(self, *, populate_rc=0, populate_stderr="", start_rc=0, start_stdout='{"ok": 1}\n', start_stderr="",
                 start_hang=False, volume_rm_fails=False, listed=None, image_ids=None, networks=None, sandbox_exit_after=1):
        self.calls: list = []
        self.existing: set = set()
        self.state: dict = {}                      # container name → "created" | "running" | "exited"
        self.polls: dict = {}
        self.populate_rc, self.populate_stderr = populate_rc, populate_stderr
        self.start_rc, self.start_stdout, self.start_stderr, self.start_hang = start_rc, start_stdout, start_stderr, start_hang
        self.volume_rm_fails = volume_rm_fails
        self.sandbox_killed = False
        self.copied: list = []
        self.listed = listed or []
        self.image_ids = image_ids or {"img": "sha256:" + "1" * 64, "prqa/opencode-sandbox:local": "sha256:" + "1" * 64}
        self.networks = networks or {"none": {"internal": "false", "label": ""}}
        self.sandbox_exit_after = sandbox_exit_after

    class P:
        def __init__(self, rc=0, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def steps(self):
        out = []
        for argv, kw in self.calls:
            a = argv[1:]
            if a[:2] in (["volume", "create"], ["volume", "rm"]):
                out.append(" ".join(a[:2]))
            elif a[0] in ("create", "cp", "start", "wait", "logs") or a[:2] == ["rm", "-f"]:
                out.append(a[0] if a[0] != "rm" else "rm")
        return out

    def run(self, argv, **kw):
        self.calls.append((argv, kw))
        a = argv[1:]
        if a[:2] == ["volume", "create"]:
            self.existing.add(("volume", a[-1])); return self.P(0, a[-1] + "\n")
        if a[0] == "create":
            name = a[a.index("--name") + 1]
            if ("container", name) in self.existing:
                return self.P(125, "", f"Error response from daemon: Conflict. The container name \"/{name}\" is already in use")
            self.existing.add(("container", name)); self.state[name] = "created"; return self.P(0, "abc123\n")
        if a[0] == "cp":
            dest = a[-1]
            self.copied.append(dest); return self.P(0)
        if a[0] == "start":
            name = a[-1]
            if ("container", name) not in self.existing:
                return self.P(1, "", f"Error response from daemon: No such container: {name}")
            self.state[name] = "running"; self.polls[name] = 0; return self.P(0, name + "\n")
        if a[0] == "wait":
            name = a[-1]
            self.state[name] = "exited"
            return self.P(0, f"{self.populate_rc}\n") if name.startswith("prqa-populate-") else self.P(0, f"{self.start_rc}\n")
        if a[0] == "logs":
            name = a[-1]
            if name.startswith("prqa-populate-"):
                return self.P(0, "populated 5 files\n", self.populate_stderr)
            return self.P(0, self.start_stdout, self.start_stderr)
        if a[:2] == ["rm", "-f"]:
            name = a[-1]
            if ("container", name) in self.existing:
                self.existing.discard(("container", name)); self.state.pop(name, None)
                self.sandbox_killed = self.sandbox_killed or name.startswith("prqa-sandbox-")
                return self.P(0, name + "\n")
            return self.P(1, "", f"Error response from daemon: No such container: {name}")
        if a[:3] == ["volume", "rm", "-f"]:
            name = a[-1]
            if self.volume_rm_fails and ("volume", name) in self.existing:
                return self.P(1, "", f"Error response from daemon: remove {name}: volume is in use")
            if ("volume", name) in self.existing:
                self.existing.discard(("volume", name)); return self.P(0, name + "\n")
            return self.P(1, "", f"Error response from daemon: get {name}: no such volume")
        if a[:2] == ["container", "inspect"]:
            name = a[-1]
            if ("container", name) not in self.existing:
                return self.P(1, "", f"Error: No such container: {name}")
            if "{{.State.Status}}" in " ".join(a):
                st = self.state.get(name, "created")
                if st == "running" and name.startswith("prqa-sandbox-"):
                    self.polls[name] = self.polls.get(name, 0) + 1
                    if not self.start_hang and self.polls[name] >= self.sandbox_exit_after:
                        self.state[name] = "exited"; st = "exited"
                return self.P(0, f"{st} {self.start_rc if st == 'exited' else 0}\n")
            return self.P(0, "/" + name)
        if a[:2] == ["volume", "inspect"]:
            name = a[-1]
            if ("volume", name) in self.existing:
                created = next((r["created"] for r in self.listed if r["name"] == name), "2026-10-22T10:00:00Z")
                return self.P(0, created if "{{.CreatedAt}}" in " ".join(a) else name)
            return self.P(1, "", f"Error response from daemon: get {name}: no such volume")
        if a[:2] == ["image", "inspect"]:
            ref = a[-1]
            if ref in self.image_ids:
                return self.P(0, self.image_ids[ref] + "\n")
            return self.P(1, "", f"Error: No such image: {ref}")
        if a[:2] == ["network", "inspect"]:
            name = a[-1]
            net = self.networks.get(name)
            if net is None:
                return self.P(1, "", f"Error: No such network: {name}")
            return self.P(0, f"{net['internal']} {net['label']} bridge {net.get('inhibit_ipv4', '<no value>')}\n")
        if a[:2] == ["ps", "-a"]:
            rows = [f"{r['name']}\t{r['job_id']}\t{r['kind']}\t{r['created']}\t{r['state']}" for r in self.listed if r["type"] == "container"]
            return self.P(0, "\n".join(rows) + ("\n" if rows else ""))
        if a[:2] == ["volume", "ls"]:
            rows = [f"{r['name']}\t{r['job_id']}" for r in self.listed if r["type"] == "volume"]
            return self.P(0, "\n".join(rows) + ("\n" if rows else ""))
        return self.P(0)
