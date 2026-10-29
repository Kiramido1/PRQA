"""Docker API policy for the PRQA launcher: an allowlist of operations AND body-level validation of every
container/volume definition. A path-only allowlist is not a security boundary; this module decides on the parsed
request. Pure functions, unit-tested without Docker. Used by docker_policy_proxy.py.

Allowed (exactly what the reviewed transport needs): ping/version, container create/start/wait/inspect/logs/delete,
archive upload into the helper's /work, volume create/inspect/list/delete, network inspect, image inspect (for the
pinned ID). Everything else — exec, build, pull, commit, image delete, network create, attach/hijack, exec — is denied.

List requests (`GET /containers/json`, `GET /volumes`) are never forwarded as written: the proxy rewrites every
forwarded query to a single canonical `filters=` value that carries the mandatory PRQA label filters, merged with
(narrowing-only) caller filters. Host-wide listings therefore cannot leak engine state even if a daemon change ever
ignored caller-supplied filters: the forwarded query is the query the policy decided on.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

JOB_RE = r"[A-Za-z0-9][A-Za-z0-9_.-]{3,120}"
NAME_RE = re.compile(rf"^prqa-(sandbox|populate)-({JOB_RE})$")
VOLUME_RE = re.compile(rf"^prqa-checkout-({JOB_RE})$")
VERSION_PREFIX = re.compile(r"^/v\d+\.\d+")
HELPER_ENTRYPOINT = "/usr/local/bin/prqa-populate-checkout"
ALLOWED_ENV_KEYS = {"BASE_URL", "PRQA_JOB_ID", "PRQA_JOB_JSON", "PRQA_EVIDENCE_DIR", "PRQA_TEST_LEVEL", "PRQA_MODEL_BASE_URL",
                    "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_PURE", "TOKEN", "PRQA_MODEL_TOKEN"}
ALLOWED_TMPFS = {"/tmp", "/work", "/home/sandbox"}
ALLOWED_SECURITY_OPT = {"no-new-privileges:true", "no-new-privileges"}
MAX_MEMORY = 2 * 1024 ** 3
MAX_PIDS = 256
MAX_CPU_NANO = 2 * 10 ** 9
SANDBOX_NETWORK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$")
FORBIDDEN_NETWORKS = {"host", "bridge", "default"}


def sandbox_networks(name: str | None) -> frozenset[str]:
    """The networks a sandbox may be created on: always 'none', plus the ONE configured sandbox network (the same
    PRQA_SANDBOX_NETWORK value the launcher verifies as internal + prqa.net=sandbox + inhibit_ipv4). Docker's shared
    networks, container:<id> namespaces and malformed names are refused at start-up (ValueError), never permitted."""
    if not name or name == "none":
        return frozenset({"none"})
    if name.lower() in FORBIDDEN_NETWORKS or not SANDBOX_NETWORK_RE.match(name):
        raise ValueError(f"sandbox network {name!r} is not a permissible dedicated network")
    return frozenset({"none", name})


@dataclass(frozen=True)
class Policy:
    allowed_image_ids: frozenset[str]            # sha256:<64hex> — the pinned sandbox image(s)
    allowed_image_tags: frozenset[str] = frozenset({"prqa/opencode-sandbox:local"})
    allowed_networks: frozenset[str] = frozenset({"none"})   # plus the verified internal sandbox network, if any
    archive_path: str = "/work"
    max_archive_bytes: int = 600 * 1024 * 1024


@dataclass
class Decision:
    allowed: bool
    reason: str
    kind: str = ""                               # e.g. "container.create", "volume.delete"
    job_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    forwarded_path: str | None = None         # set when the proxy must send a rewritten query (volume lists)


def _strip_version(path: str) -> str:
    return VERSION_PREFIX.sub("", path) or "/"


def _deny(reason: str, kind: str = "") -> Decision:
    return Decision(False, reason, kind)


def _job_from_name(name: str) -> str | None:
    m = NAME_RE.match(name)
    return m.group(2) if m else None


def _volume_job(name: str) -> str | None:
    m = VOLUME_RE.match(name)
    return m.group(1) if m else None


def _filters_parameters(query: dict[str, list[str]]) -> list[str]:
    """Spellings of the filters parameter present in the request. `parse_qs` percent-decodes keys, so `%66ilters`
    arrives already as `filters`; any differently-cased spelling is ambiguous and must be refused (the daemon's
    r.Form is case-sensitive and ignores it, so forwarding it next to a rewritten `filters=` would look like a
    caller-controlled second opinion)."""
    return [key for key in query if key.lower() == "filters"]


_CONTAINER_LABEL_FILTERS = ("prqa.job", "prqa.kind")      # key-exists form: any PRQA-labelled container
_VOLUME_LABEL_FILTERS = ("prqa.job", "prqa.kind=checkout")  # checkout volumes only


def _inject_required_labels(merged: list[str], required: tuple[str, ...]) -> None:
    """Append every required label filter unless the caller's entries already imply it. A value-form entry
    (`prqa.job=<x>`) implies key-existence (`prqa.job`), so it suppresses the bare form (narrowing preserved)."""
    for needed in required:
        implied = any(entry == needed or (("=" not in needed) and entry.startswith(needed + "=")) for entry in merged)
        if not implied:
            merged.append(needed)


def _narrowed_label_query(query: dict[str, list[str]], required: tuple[str, ...]) -> str | None:
    """Canonical `filters=` JSON for forwarding a list request, or None when the caller's filters are unusable.

    The docker CLI sends `--filter label=…` client-side: for `docker volume ls` it asks the daemon for the whole
    list before filtering, and daemon-side parsing of the `filters` parameter is trusted for container lists. Both
    requests are therefore rewritten the same way: exactly one canonical `filters=` value per forwarded query that
    contains the mandatory PRQA labels (key-exists form) merged with the caller's label filters in a strictly
    narrowing-only way (key-form entries keep their meaning; the docker CLI's map form `{"<term>": true}` — the key
    is the whole term, `k` or `k=v`, and the daemon reads only keys — becomes `<term>`; a string-valued map
    `{"k": "v"}` becomes `k=v`, `{"k": ""}` becomes `k`; any other value fails closed; other caller filter keys are
    preserved as received). Host-wide listing is closed in both paths.
    """
    raw = (query.get("filters") or [""])[0]
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
    else:
        data = {}
    labels = data.get("label")
    if labels is not None and not isinstance(labels, (list, dict)):
        return None                      # a scalar label filter is not a recognised CLI form: fail closed
    merged: list[str] = []
    if isinstance(labels, list):
        merged = [str(entry) for entry in labels]
    elif isinstance(labels, dict):
        for k, v in labels.items():
            if isinstance(v, bool):
                merged.append(str(k))            # docker CLI wire format: {"prqa.kind=checkout": true}
            elif isinstance(v, str):
                merged.append(f"{k}={v}" if v else str(k))
            else:
                return None                      # not a recognised filter form: fail closed
    _inject_required_labels(merged, required)
    data["label"] = merged
    return json.dumps(data, separators=(",", ":"))


def _forwarded_list_path(path: str, query: dict[str, list[str]], filters_json: str) -> str:
    """Rebuild the forwarded query: every caller parameter is kept verbatim (percent-encoded with an empty safe set)
    except any `filters` — the rewritten value is the ONLY filter the daemon sees. Callers never control the query
    text of the forwarded filter; ambiguous spellings are refused upstream in `evaluate`."""
    kept: list[str] = []
    for key, values in query.items():
        if key.lower() == "filters":
            continue
        kept.extend(quote(str(key), safe="") + "=" + quote(str(value), safe="") for value in values)
    kept.append("filters=" + quote(filters_json, safe=""))
    return path + "?" + "&".join(kept)


def _single_filters_value(query: dict) -> bool:
    """True when `filters` appears at most once. If the caller sends several, fail closed: the daemon currently
    applies only the first one, which is the same value the policy checked — pinning that contract explicitly."""
    values = query.get("filters") or []
    return len(values) <= 1


def evaluate(policy: Policy, method: str, raw_path: str, body: bytes | None) -> Decision:
    """Decide one Docker Engine API request. `body` is the raw request body (JSON for create calls, tar for archive)."""
    parts = urlsplit(raw_path)
    path = _strip_version(parts.path)
    query = parse_qs(parts.query)
    method = method.upper()

    if method in ("GET", "HEAD") and path in ("/_ping", "/version"):
        return Decision(True, "ping/version", "engine.info")
    if method == "GET" and path == "/info":
        return _deny("engine info is not part of the reviewed transport", "engine.info")

    # ---- containers ----
    if method == "POST" and path == "/containers/create":
        return _validate_container_create(policy, query, body)
    if method == "GET" and path == "/containers/json":
        # the forwarded query is REWRITTEN: exactly one canonical filters= value carrying the mandatory PRQA label
        # filters merged (narrowing-only) with the caller's — the daemon can never see a caller-written filter set
        if not _single_filters_value(query):
            return _deny("container list: repeated filters= parameter is refused", "container.list")
        if [k for k in _filters_parameters(query) if k != "filters"]:
            return _deny("container list: ambiguous spelling of the filters parameter is refused", "container.list")
        new_filters = _narrowed_label_query(query, _CONTAINER_LABEL_FILTERS)
        if new_filters is None:
            return _deny("container list: unusable filters= (must be a JSON object)", "container.list")
        forwarded = _forwarded_list_path(path, query, new_filters)
        return Decision(True, "container list (prqa-labelled only)", "container.list", None, {}, forwarded)
    m = re.match(r"^/containers/([^/]+)(/[a-z]+)?$", path)
    if m:
        name, action = m.group(1), m.group(2) or ""
        job = _job_from_name(name)
        if job is None:
            return _deny("container reference must be a PRQA job-named container", "container")
        if method == "GET" and action == "/json":
            return Decision(True, "inspect", "container.inspect", job)
        if method == "GET" and action == "/logs":
            return Decision(True, "logs", "container.logs", job)
        if method == "POST" and action == "/start":
            return Decision(True, "start", "container.start", job)
        if method == "POST" and action == "/wait":
            return Decision(True, "wait", "container.wait", job)
        if method == "DELETE" and action == "":
            return Decision(True, "delete (force allowed)", "container.delete", job)
        if action == "/archive":
            dest = (query.get("path") or [""])[0]
            if not name.startswith("prqa-populate-"):
                return _deny("archive upload only into the helper container", "container.archive")
            if method == "HEAD":
                # `docker cp` stats the destination first (the file path), then uploads into its directory
                if dest.rstrip("/") not in (policy.archive_path, policy.archive_path + "/checkout.tar"):
                    return _deny(f"archive stat limited to {policy.archive_path}/checkout.tar", "container.archive_stat")
                return Decision(True, "archive stat", "container.archive_stat", job)
            if dest.rstrip("/") != policy.archive_path:
                return _deny(f"archive path must be {policy.archive_path}", "container.archive")
            if method == "PUT":
                if body is not None and len(body) > policy.max_archive_bytes:
                    return _deny("archive too large", "container.archive")
                return Decision(True, "archive upload", "container.archive", job)
            return _deny("archive: method not allowed", "container.archive")
        if action in ("/attach", "/exec", "/exec/", "/resize", "/update", "/rename", "/export", "/changes", "/stats", "/top",
                      "/pause", "/unpause", "/kill", "/restart", "/stop", "/commit"):
            return _deny(f"container{action} is not permitted", "container")
        return _deny("container operation not permitted", "container")

    # ---- volumes ----
    if method == "POST" and path == "/volumes/create":
        return _validate_volume_create(body)
    if method == "GET" and path == "/volumes":
        if not _single_filters_value(query):
            return _deny("volume list: repeated filters= parameter is refused", "volume.list")
        new_filters = _narrowed_label_query(query, _VOLUME_LABEL_FILTERS)
        if new_filters is None:
            return _deny("volume list: unusable filters= (must be a JSON object)", "volume.list")
        forwarded = _forwarded_list_path(path, query, new_filters)
        return Decision(True, "volume list (prqa-labelled only)", "volume.list", None, {}, forwarded)
    m = re.match(r"^/volumes/([^/]+)$", path)
    if m:
        job = _volume_job(m.group(1))
        if job is None:
            return _deny("volume reference must be a PRQA checkout volume", "volume")
        if method == "GET":
            return Decision(True, "volume inspect", "volume.inspect", job)
        if method == "DELETE":
            return Decision(True, "volume delete", "volume.delete", job)
        return _deny("volume operation not permitted", "volume")

    # ---- networks / images (read-only) ----
    m = re.match(r"^/networks/([^/]+)$", path)
    if m and method == "GET":
        # the launcher inspects only its own configured sandbox network; foreign network metadata stays closed
        if m.group(1) in policy.allowed_networks:
            return Decision(True, "network inspect", "network.inspect")
        return _deny("network inspect limited to the permitted sandbox network(s)", "network.inspect")
    m = re.match(r"^/images/(.+)/json$", path)
    if m and method == "GET":
        ref = m.group(1)
        if ref in policy.allowed_image_ids or ref in policy.allowed_image_tags or ref.replace("sha256:", "") in {i.replace("sha256:", "") for i in policy.allowed_image_ids}:
            return Decision(True, "image inspect", "image.inspect")
        return _deny("image inspect limited to the pinned sandbox image", "image")
    return _deny(f"{method} {path} is not permitted", "")


def _validate_volume_create(body: bytes | None) -> Decision:
    try:
        spec = json.loads((body or b"{}").decode("utf-8")) or {}
    except ValueError:
        return _deny("volume create: body is not JSON", "volume.create")
    name = str(spec.get("Name") or "")
    job = _volume_job(name)
    if job is None:
        return _deny("volume create: name must be prqa-checkout-<job>", "volume.create")
    labels = spec.get("Labels") or {}
    if labels.get("prqa.job") != job or labels.get("prqa.kind") != "checkout":
        return _deny("volume create: labels must bind the volume to its job", "volume.create")
    if spec.get("Driver") not in (None, "", "local") or spec.get("DriverOpts"):
        return _deny("volume create: only the local driver without options", "volume.create")
    return Decision(True, "volume create", "volume.create", job)


def _validate_container_create(policy: Policy, query: dict[str, list[str]], body: bytes | None) -> Decision:
    kind = "container.create"
    try:
        spec = json.loads((body or b"{}").decode("utf-8")) or {}
    except ValueError:
        return _deny("create: body is not JSON", kind)
    name = (query.get("name") or [""])[0]
    job = _job_from_name(name)
    if job is None:
        return _deny("create: container name must be prqa-sandbox-<job> or prqa-populate-<job>", kind)
    is_helper = name.startswith("prqa-populate-")
    labels = spec.get("Labels") or {}
    if labels.get("prqa.job") != job or labels.get("prqa.kind") != ("helper" if is_helper else "sandbox"):
        return _deny("create: labels must bind the container to its job and kind", kind)
    image = str(spec.get("Image") or "")
    if image not in policy.allowed_image_ids and image not in policy.allowed_image_tags:
        return _deny("create: image is not the pinned sandbox image", kind)
    if spec.get("User") != "10001:10001":
        return _deny("create: user must be 10001:10001", kind)
    hc = spec.get("HostConfig") or {}
    if hc.get("Privileged"):
        return _deny("create: privileged mode", kind)
    for key in ("PidMode", "IpcMode", "UTSMode", "UsernsMode", "CgroupnsMode", "Cgroup"):
        if str(hc.get(key) or "") not in ("", "private"):
            return _deny(f"create: {key} must be unset", kind)
    net = str(hc.get("NetworkMode") or "")
    if net not in policy.allowed_networks:
        return _deny(f"create: network {net!r} is not permitted", kind)
    if (spec.get("NetworkingConfig") or {}).get("EndpointsConfig") and set((spec["NetworkingConfig"]["EndpointsConfig"] or {}).keys()) - policy.allowed_networks:
        return _deny("create: endpoint configuration for a non-permitted network", kind)
    if hc.get("Binds"):
        return _deny("create: bind mounts are not permitted", kind)
    if hc.get("Devices") or hc.get("DeviceRequests") or hc.get("DeviceCgroupRules"):
        return _deny("create: devices are not permitted", kind)
    if hc.get("CapAdd"):
        return _deny("create: added capabilities are not permitted", kind)
    if [c.upper() for c in (hc.get("CapDrop") or [])] != ["ALL"]:
        return _deny("create: CapDrop must be exactly ALL", kind)
    sec = set(hc.get("SecurityOpt") or [])
    if not sec or not sec.issubset(ALLOWED_SECURITY_OPT):
        return _deny("create: SecurityOpt must be no-new-privileges only", kind)
    if hc.get("PublishAllPorts") or hc.get("PortBindings") or spec.get("ExposedPorts"):
        return _deny("create: ports are not permitted", kind)
    if hc.get("Sysctls") or hc.get("ExtraHosts") or hc.get("Dns") or hc.get("VolumesFrom") or hc.get("Links"):
        return _deny("create: sysctls/hosts/dns/volumes-from/links are not permitted", kind)
    if int(hc.get("Memory") or 0) <= 0 or int(hc.get("Memory") or 0) > MAX_MEMORY:
        return _deny("create: memory limit missing or above cap", kind)
    if int(hc.get("PidsLimit") or 0) <= 0 or int(hc.get("PidsLimit") or 0) > MAX_PIDS:
        return _deny("create: pids limit missing or above cap", kind)
    if int(hc.get("NanoCpus") or 0) > MAX_CPU_NANO:
        return _deny("create: cpu limit above cap", kind)
    for key in (hc.get("Tmpfs") or {}):
        if key not in ALLOWED_TMPFS:
            return _deny(f"create: tmpfs {key} not permitted", kind)
    mounts = hc.get("Mounts") or []
    if len(mounts) != 1:
        return _deny("create: exactly one mount (the job's checkout volume) is required", kind)
    mnt = mounts[0]
    if mnt.get("Type") != "volume" or mnt.get("Source") != f"prqa-checkout-{job}" or mnt.get("Target") != "/checkout":
        return _deny("create: mount must be the job's own checkout volume at /checkout", kind)
    if is_helper:
        if hc.get("ReadonlyRootfs"):
            return _deny("create: helper must not be read-only (archive upload needs /work)", kind)
        if mnt.get("ReadOnly"):
            return _deny("create: helper needs the volume writable", kind)
        if net != "none":
            return _deny("create: helper network must be none", kind)
        if spec.get("Entrypoint") not in ([HELPER_ENTRYPOINT], HELPER_ENTRYPOINT):
            return _deny("create: helper entrypoint must be the extractor", kind)
        cmd = spec.get("Cmd") or []
        if cmd != ["/checkout", job, "/work/checkout.tar"]:
            return _deny("create: helper command must be '/checkout <job> /work/checkout.tar'", kind)
        if spec.get("Env"):
            return _deny("create: helper takes no environment", kind)
    else:
        if not hc.get("ReadonlyRootfs"):
            return _deny("create: sandbox must have a read-only root filesystem", kind)
        if not mnt.get("ReadOnly"):
            return _deny("create: sandbox must mount the checkout volume read-only", kind)
        if spec.get("Entrypoint") not in (None, [], ""):
            return _deny("create: sandbox entrypoint override is not permitted", kind)
        cmd = spec.get("Cmd") or []
        if not _valid_sandbox_cmd(cmd, job):
            return _deny("create: sandbox command must be the entrypoint's argument set", kind)
        for item in (spec.get("Env") or []):
            key = str(item).split("=", 1)[0]
            if key not in ALLOWED_ENV_KEYS:
                return _deny(f"create: environment variable {key} is not permitted", kind)
        if hc.get("AutoRemove"):
            return _deny("create: sandbox must not auto-remove (cleanup is verified explicitly)", kind)
    return Decision(True, "create", kind, job, {"helper": is_helper, "image": image, "network": net})


def _valid_sandbox_cmd(cmd: list[Any], job: str) -> bool:
    if not isinstance(cmd, list) or len(cmd) < 13:
        return False
    try:
        it = {cmd[i]: cmd[i + 1] for i in range(0, len(cmd) - 1, 2)}
    except (IndexError, TypeError):
        return False
    required = ("--repo", "--sha", "--model", "--timeout-minutes", "--tree-sha256", "--prompt-sha256")
    if cmd[-1] != "--prechecked" or any(k not in it for k in required):
        return False
    if len(cmd) != 13:
        return False
    return bool(re.match(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$", str(it["--repo"])) and re.match(r"^[0-9a-f]{40}$", str(it["--sha"]))
                and re.match(r"^[0-9a-f]{64}$", str(it["--tree-sha256"])) and re.match(r"^[0-9a-f]{64}$", str(it["--prompt-sha256"]))
                and re.match(r"^[0-9]{1,4}$", str(it["--timeout-minutes"])) and re.match(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{1,120}$", str(it["--model"])))
