"""Per-repository GitHub Actions connections for the `to be test` dispatch (schema 2.0, POST /webhook/prqa/actions).

A *connection* binds ONE exact repository to its own signing secret. It is provisioned explicitly by
``scripts/setup/prqa_connect_repository.py`` into a protected host directory (default ``~/.config/prqa/actions-connections``,
mode 750, file ``connections.json`` mode 640, outside the project drive) that compose mounts read-only into the worker
at ``/run/prqa-connections``. The worker re-reads the file when it changes, so a new connection needs no restart.

The connection id is NOT a credential: it only selects the secret. A request is accepted only when the HMAC made with
that connection's secret verifies AND the signed repository equals the connection's repository exactly.

Shared by the worker (verification) and the setup CLI (provisioning, endpoint validation) so both apply the same rules.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets as _secrets
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

CONNECTIONS_FILE = "connections.json"
CONNECTION_ID_RE = re.compile(r"^prqa_c_[0-9a-f]{24}$")
SECRET_RE = re.compile(r"^[0-9a-f]{64}$")
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
TRIGGER_LABEL = "to be test"                      # exact, case-sensitive
DISPATCH_ACTIONS = ("opened", "reopened", "labeled", "synchronize", "ready_for_review")
INGRESS_PATH = "/webhook/prqa/actions"            # the n8n receiver path that forwards to the worker; never the editor
MAX_CONNECTION_BODY_BYTES = 16 * 1024
REGISTRY_VERSION = 1

# Hosts that can never be a reachable public ingress: loopback/local names, reserved documentation and test domains.
_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "host.docker.internal",
                "gateway.docker.internal", "kubernetes.default", "metadata.google.internal"}
_RESERVED_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home", ".corp", ".private",
                      ".test", ".example", ".invalid", ".localdomain", ".home.arpa")
_DOCUMENTATION_DOMAINS = ("example.com", "example.org", "example.net")
_COMPOSE_SERVICE_RE = re.compile(r"^(prqa-[a-z0-9-]+|n8n)$")          # internal control-plane service names


class ConnectionError_(ValueError):
    """Provisioning or registry problem (fail closed)."""


@dataclass(frozen=True)
class Connection:
    connection_id: str
    repository: str
    secret: str
    label: str = TRIGGER_LABEL
    status: str = "active"

    def public(self) -> dict[str, Any]:
        """Everything about the connection except the secret."""
        return {"connection_id": self.connection_id, "repository": self.repository, "label": self.label, "status": self.status}


def new_connection_id() -> str:
    return "prqa_c_" + _secrets.token_hex(12)


def new_secret() -> str:
    return _secrets.token_hex(32)


def derive_dispatch_key(connection_id: str, pr_number: int, head_sha: str) -> str:
    """One run per connection (= one exact repository)/PR/head SHA: opened + labeled on the same head is ONE job; a new
    commit on a still-labelled PR is a new key (a new run, if policy allows). At most 86 chars (job delivery_id <= 100)."""
    return f"conn:{connection_id}#{int(pr_number)}@{head_sha}"


def delivery_key(connection_id: str, delivery_id: str) -> str:
    return f"conn-delivery:{connection_id}:{delivery_id}"


# ------------------------------------------------------------------ endpoint validation --

def validate_ingress_url(url: str, *, local_test: bool = False) -> tuple[bool, str]:
    """(ok, reason). Production: https, a public DNS name or public IP, no credentials/query/fragment, path exactly
    INGRESS_PATH. local_test=True additionally allows http://127.0.0.1|localhost[:port]INGRESS_PATH (never written as
    a production configuration). No DNS lookup is made (offline)."""
    raw = (url or "").strip()
    if not raw:
        return False, "webhook URL is empty"
    if len(raw) > 300 or any(c.isspace() for c in raw):
        return False, "webhook URL is malformed"
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return False, "webhook URL is malformed"
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.username or parts.password:
        return False, "webhook URL must not contain credentials"
    if parts.query or parts.fragment:
        return False, "webhook URL must not contain a query string or fragment"
    if parts.path.rstrip("/") != INGRESS_PATH:
        return False, f"webhook URL path must be {INGRESS_PATH} (the dedicated PRQA ingress, never the n8n editor, REST API or test webhooks)"
    if local_test and parts.scheme == "http" and host in ("127.0.0.1", "localhost"):
        return True, "local test endpoint (not reachable from GitHub-hosted runners)"
    if parts.scheme != "https":
        return False, "webhook URL must use https:// (http is allowed only with --local-test against 127.0.0.1/localhost)"
    if not host:
        return False, "webhook URL has no host"
    if port is not None and port in (5678, 8080, 8089, 8090):
        return False, f"port {port} is an internal PRQA/n8n service port; expose only the ingress path through an HTTPS reverse proxy"
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None:
        if not ip.is_global or ip.is_multicast:
            return False, "webhook host is a loopback, private, link-local or otherwise non-public address"
        return True, "public IP address (an https certificate for an IP is unusual; a DNS name is recommended)"
    if host in _LOCAL_NAMES or host.endswith(_RESERVED_SUFFIXES) or "." not in host or _COMPOSE_SERVICE_RE.match(host):
        return False, "webhook host is a local, internal or reserved name that GitHub-hosted runners cannot reach"
    if host in _DOCUMENTATION_DOMAINS or host.endswith(tuple("." + d for d in _DOCUMENTATION_DOMAINS)):
        return False, "webhook host is a documentation placeholder domain, not a real endpoint"
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+", host):
        return False, "webhook host is not a valid DNS name"
    return True, "public https endpoint (reachability is not verified offline)"


# ----------------------------------------------------------------------- registry ----

def _check_private_dir(path: Path) -> None:
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise ConnectionError_("connections directory is not a directory")
    if st.st_mode & 0o027:
        raise ConnectionError_("connections directory must not be writable by group or accessible to others (mode 750 or stricter)")


def parse_registry(data: Any) -> dict[str, Connection]:
    if not isinstance(data, dict) or data.get("version") != REGISTRY_VERSION or not isinstance(data.get("connections"), dict):
        raise ConnectionError_("connections registry is malformed")
    out: dict[str, Connection] = {}
    seen_repos: set[str] = set()
    for cid, item in data["connections"].items():
        if not CONNECTION_ID_RE.match(str(cid)) or not isinstance(item, dict):
            raise ConnectionError_("connections registry has a malformed connection id")
        repo, secret = str(item.get("repository") or ""), str(item.get("secret") or "")
        if not REPO_RE.match(repo) or not SECRET_RE.match(secret):
            raise ConnectionError_(f"connection {cid} has a malformed repository or secret")
        status = str(item.get("status") or "active")
        if status not in ("active", "revoked"):
            raise ConnectionError_(f"connection {cid} has an unknown status")
        if status == "active":
            if repo in seen_repos:
                raise ConnectionError_(f"repository {repo} has more than one active connection")
            seen_repos.add(repo)
        out[cid] = Connection(cid, repo, secret, str(item.get("label") or TRIGGER_LABEL), status)
    return out


class ConnectionRegistry:
    """Read side (worker): loads <dir>/connections.json, re-reading it when its mtime/size changes. A missing directory
    or file means "no connections" (every connection request is rejected); a malformed one fails closed the same way."""

    def __init__(self, directory: str | None) -> None:
        self.directory = Path(directory) if directory else None
        self._lock = threading.Lock()
        self._sig: tuple[float, int] | None = None
        self._items: dict[str, Connection] = {}
        self.error: str | None = None

    def _load(self) -> dict[str, Connection]:
        if self.directory is None:
            self.error = "PRQA_ACTIONS_CONNECTIONS_DIR is not configured"
            return {}
        path = self.directory / CONNECTIONS_FILE
        try:
            st = path.stat()
        except OSError:
            self._sig, self._items, self.error = None, {}, "no connections provisioned"
            return {}
        sig = (st.st_mtime, st.st_size)
        if sig == self._sig:
            return self._items
        try:
            items = parse_registry(json.loads(path.read_text(encoding="utf-8")))
            self.error = None
        except (OSError, ValueError, ConnectionError_) as exc:
            items, self.error = {}, f"connections registry unusable: {str(exc)[:120]}"
        self._sig, self._items = sig, items
        return items

    def get(self, connection_id: str) -> Connection | None:
        with self._lock:
            c = self._load().get(connection_id)
        return c if c is not None and c.status == "active" else None

    def summary(self) -> dict[str, Any]:
        with self._lock:
            items = self._load()
        return {"directory_configured": self.directory is not None, "active_connections": sum(c.status == "active" for c in items.values()),
                "error": self.error}


class ConnectionStore:
    """Write side (setup CLI on the host): atomic, private writes into the protected directory."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser()

    def ensure_dir(self) -> None:
        self.directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o750)
        _check_private_dir(self.directory)

    def read(self) -> dict[str, Any]:
        path = self.directory / CONNECTIONS_FILE
        if not path.exists():
            return {"version": REGISTRY_VERSION, "connections": {}}
        data = json.loads(path.read_text(encoding="utf-8"))
        parse_registry(data)                                   # validate before any change
        return data

    def write(self, data: dict[str, Any]) -> None:
        parse_registry(data)
        self.ensure_dir()
        fd, tmp = tempfile.mkstemp(prefix=".connections.", dir=str(self.directory))
        try:
            os.fchmod(fd, 0o640)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.directory / CONNECTIONS_FILE)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def find_by_repository(self, repository: str) -> Connection | None:
        items = parse_registry(self.read())
        return next((c for c in items.values() if c.repository == repository and c.status == "active"), None)

    def connect(self, repository: str, *, rotate_secret: bool = False) -> tuple[Connection, str]:
        """Create or reuse the active connection of `repository`. Returns (connection, 'created'|'reused'|'rotated')."""
        if not REPO_RE.match(repository or ""):
            raise ConnectionError_("repository must be OWNER/REPOSITORY (GitHub naming rules)")
        data = self.read()
        existing = self.find_by_repository(repository)
        if existing and not rotate_secret:
            return existing, "reused"
        cid = existing.connection_id if existing else new_connection_id()
        data["connections"][cid] = {"repository": repository, "secret": new_secret(), "label": TRIGGER_LABEL, "status": "active",
                                    "created_at": data["connections"].get(cid, {}).get("created_at") or int(time.time()),
                                    "rotated_at": int(time.time()) if existing else None}
        self.write(data)
        return parse_registry(data)[cid], ("rotated" if existing else "created")

    def revoke(self, repository: str) -> str | None:
        data = self.read()
        existing = self.find_by_repository(repository)
        if not existing:
            return None
        data["connections"][existing.connection_id]["status"] = "revoked"
        self.write(data)
        return existing.connection_id
