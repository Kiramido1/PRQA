# Sandbox trust boundary: reading a repository vs. executing repository-controlled code

Status (2026-10-22): describes the design after the audit fixes. Live execution remains **disabled** (review-only);
nothing in this document has been exercised against a real repository, container or model.

## 1. Two different things

| | Reading repository content | Executing repository-controlled code |
|---|---|---|
| What | checkout at the exact PR head SHA, packing the tree, building the prompt from diff/metadata | anything that runs *because of* repository content: hooks, package hooks, builds, tests, and **an agent (OpenCode) that reads the repository and decides to run commands** |
| Who | the **worker**, outside the sandbox, with the GitHub token | the **sandbox container** only, never the worker |
| Protection | pinned SHA (`fetch --depth 1 origin <sha>` → `checkout --detach FETCH_HEAD`, HEAD verified), hooks disabled, submodules off, credential helpers off, https only, `GIT_CONFIG_GLOBAL=/dev/null`, size and time bounds, token only in `GIT_CONFIG_*` env (`worker/prqa_worker/workspace.py`) | container limits: read-only root, tmpfs work dirs, `--cap-drop ALL`, `no-new-privileges`, uid 10001, memory/cpu/pids caps, network **none** by default, no socket, no host mounts (`worker/prqa_worker/opencode_runner.py`) |
| What it does **not** prevent | — | prompt injection: repository text can talk OpenCode into running commands inside the sandbox. Disabled hooks and the read-only filesystem bound *what* such commands can reach; they do not stop them from running |

Rule: **everything that needs a credential happens in the worker; the sandbox gets data, not credentials.**

## 2. What crosses into the sandbox

Artifact layout (`checkout.tar`, built by `workspace.pack_checkout_artifact`). Transport (Docker refuses `docker cp`
into a `--read-only` container, verified on Docker 27.5.1): `docker volume create prqa-checkout-<job>` → a short-lived
helper container (same image, `--entrypoint prqa-populate-checkout`, `--network none`, read-only rootfs, uid 10001, no
mounts except that volume) extracts the archive from stdin with a safe extractor → `docker create` of the sandbox with
`--mount type=volume,src=prqa-checkout-<job>,dst=/checkout,readonly` (rootfs stays `--read-only`) → `docker start -a` →
removal of the sandbox, the helper and the volume (all named by the job id). The image's `ENTRYPOINT` receives only
`--repo … --sha … --model … --timeout-minutes … --prechecked`.

| Member | Trust | Content |
|---|---|---|
| `repo/<path>` | untrusted (repository-controlled) | the working tree at the exact SHA, **without `.git`**, symlinks skipped, modes 0444/0555, uid/gid 0 |
| `prqa/manifest.json` | trusted (worker) | `artifact_version 2.0`, `tree_hash_version prqa-tree-v1`, job id, repository, head SHA, file count, `tree_sha256`, `prompt_sha256`, `git_dir_included: false` |
| `prqa/prompt.md` | trusted (worker) | the PRQA-generated prompt; refused at pack time if it contains a known secret value or secret-looking content |

`repo/` and `prqa/` are disjoint, so a repository file named `prqa/prompt.md` lands under `repo/prqa/…` and can never
replace the trusted prompt. The sandbox reads the prompt only from `/checkout/prqa/prompt.md`.

The helper's extractor (`worker/sandbox/populate_checkout.py`, exit 70 on any violation) accepts regular files only
under `repo/` or the two trusted names, rejects absolute paths, `..`, symlinks, hard links, devices, fifos, duplicate
members, a non-empty destination, a manifest whose job id differs from the helper's job argument, and a 1 GiB ceiling;
files are written 0444/0555, directories 0555, and `.prqa-populated.json` records the count.

Tree hash (`workspace.tree_digest`, reimplemented byte-for-byte in `worker/sandbox/verify_checkout.py`):
`sha256("prqa-tree-v1\0" + for each regular file in sorted relative-path order: path\0 mode(x|r)\0 size\0 sha256(content)\0)`.

Before OpenCode starts, the entrypoint (`worker/sandbox/entrypoint.sh`) fails closed (exit 66) unless: `PRQA_JOB_ID` is
set; `--tree-sha256` and `--prompt-sha256` are present and well-formed; `repo/` and `prqa/` exist and are **not
writable**; the verifier (shipped in the **image**, never in the artifact) confirms the manifest versions, repository, SHA
and job id, finds no `.git`, symlink, special file or `..` path, recomputes the tree hash and file count, checks the
prompt's presence, minimum length and hash, finds nothing else under `prqa/` **and matches the recomputed hashes against
both the embedded manifest and the worker-expected values**. Running without `--prechecked` is refused. The sandbox
never clones.

**Hash binding.** The worker records `tree_sha256` and `prompt_sha256` at packing time on the job (`workspace` record,
outside the artifact) and passes them to the sandbox as validated entrypoint arguments (`build_docker_argv`). A
self-consistent but re-signed artifact (tampered content with a matching manifest) is therefore rejected with
`tree hash mismatch (worker-expected)`; the manifest alone is never the authority.

| Item | Carried by | Present in sandbox? |
|---|---|---|
| `PRQA_JOB_ID`, `PRQA_TEST_LEVEL`, `PRQA_JOB_JSON` (job id, repo, PR number/title/SHA), `PRQA_EVIDENCE_DIR`, `BASE_URL` | `-e` in `docker create` (`ALLOWED_ENV`) | yes |
| `TOKEN` (the staging target's test credential) | `--env-file /dev/stdin` at create time, **only** when the registry entry sets `api.sandbox: {pass_target_token: true, target_profile: <bearer_token profile with token_env>}` — validated at registry load, invalid or missing profiles are a load error | optional; readable by anything in the sandbox, including `/proc/1/environ` |
| `GITHUB_TOKEN`, model API keys, PRQA service secrets | — | **never** (`SECRET_ENV_NAMES` stripped; no clone path) |
| Host filesystem, Docker socket, other jobs' workspaces, worker `/data` | — | never: no `-v`/`--mount`; the artifact must live under the job's own workspace |

After the sandbox step (success, failure or timeout) the worker deletes `repo/` and `checkout.tar` from the workspace at
once (`WorkspaceManager.purge_sources`, scoped to the owning, marker-verified directory); `checkout-plan.json`,
`diagnostics.json` and the marker remain until retention.

**Cleanup is verified, not assumed.** `cleanup_plan` removes exactly this job's sandbox container, helper container and
volume, checks each with `docker … inspect` before (already absent counts as cleaned) and after (must be absent), and
returns a structured outcome. `execute_plan` runs it in a `finally` block and reports it through `report("sandbox_cleanup",
…)`, which the executor stores as a job event and in the job's `workspace.sandbox_cleanup`; a cleanup failure is logged
and visible but never masks the original execution error. A missing artifact never launches anything; step timeouts and
the sandbox's own timeout terminate the container (`rm -f`) and are converted into failures.

**Cancellation.** `execute_plan` consults `cancel_check()` before every step and every `poll_s` seconds while `start -a`
runs; the executor wires it to the job's `cancel` flag. Cancellation terminates the sandbox with `rm -f`, raises
`SandboxCancelled`, and the atomic job-store rules then record the job as cancelled (no result is ever reported).

**Reconciliation (launcher side).** Every sandbox resource carries `prqa.job=<id>` and `prqa.kind=sandbox|helper|checkout`.
`worker/prqa_worker/launcher.py::reconcile` lists labelled resources and removes only those whose name matches the
labelled job, whose job is terminal, or whose job is unknown to the status source **and** older than the grace period
(6 h default); resources of active jobs are kept, and each removal is verified. `scripts/pipeline/prqa_sandbox_reconcile.py`
runs it on a host with Docker access (dry-run by default; job status from the worker API on loopback). The worker
container itself has no Docker access, so a crashed launcher is recovered by the next reconciliation pass, not by the
worker. **Missing interface:** a resident launcher process that consumes the plan from the worker and runs
reconciliation on start and periodically does not exist yet; the functions above are its building blocks.

OpenCode model: always `OPENCODE_MODEL`, default **`prqa/openai/gpt-oss-120b`**: GPT-OSS 120B on direct Groq through
the PRQA model proxy (provider id `prqa`, §5d), unless a registry entry sets `opencode.model` explicitly. OpenRouter is
not an active provider or fallback.

## 3. Credential trace (worker side)

`GITHUB_TOKEN` → worker env (`compose/docker-compose.yml`, worker service only) → `Executor._github_token` →
`run_checkout(plan, token=…)` → `GIT_CONFIG_COUNT/KEY_0/VALUE_0` of the git subprocesses only. Never in argv (the plan
is written to `checkout-plan.json` without it), never persisted in `.git/config`, never in the artifact (no `.git`),
redacted from errors and events (`redact_text` with the token and the derived Basic value). The sandbox plan drops
every name in `SECRET_ENV_NAMES`. Failure messages from the launcher are redacted as well.

## 4. Launching the sandbox (Phase 2 foundations, 2026-10-22)

The worker image carries **no Docker CLI and no socket**, and with `PRQA_ENFORCE_NO_DOCKER=true` (set in compose) the
worker refuses to start if a Docker socket, `DOCKER_HOST` or a `docker` binary is reachable (it reports the path/name,
never a value). `opencode_runner.run_live` is a dead end in the worker process (`RunnerUnavailable`); the executor
only **hands off**: it prepares the workspace, packs the artifact, records its size and SHA-256 and marks the job
`launch_state=pending`.

A **separate launcher process** (`worker/prqa_worker/launcher_service.py`, CLI `scripts/services/prqa_launcher.py`, compose
service `prqa-launcher`, profile `phase2`, never started automatically) pulls work from the worker over an
authenticated API (`X-Launcher-Token`, distinct from the worker token):

```
GET  /launch/pending                     → jobs in launch_state=pending
POST /jobs/{id}/launch/claim             → 200 claim payload | 409 CLAIMED_BY_OTHER | 410 NOT_CLAIMABLE (+status)
GET  /jobs/{id}/artifact                 → tar bytes, owner only, X-PRQA-Artifact-SHA256 / -Size
POST /jobs/{id}/launch/heartbeat         → renews the lease; returns cancel_requested
POST /jobs/{id}/launch/result|failed|cancelled → idempotent, owner-bound, validated result
```

Job state and lease transitions (worker-owned columns `launch_state`, `launch_owner`, `lease_until`, `launch_attempts`):

```
 queued ──executor──▶ running/—  ──handoff (artifact packed, hashed)──▶ running/pending
                                                                            │ claim(launcher, lease)   409 for everyone else
                                                                            ▼
                                     lease expired, attempts<2 ◀──── running/claimed ──heartbeat──▶ (lease renewed)
                                     (back to pending, owner cleared)       │ result|failed|cancelled (owner only; duplicates → already=true)
                                     lease expired, attempts≥2 → failed     ▼
                                                                   succeeded | failed(/queued on QA retry) | cancelled
 cancel request at any point: heartbeat answers cancel_requested=true; a result delivered after a cancel is discarded.
 worker restart: mock jobs re-queued; live jobs keep their lease and recover through the sweep above.
```

The launcher **claims before it creates anything**, rebuilds the sandbox plan from validated claim fields plus its own
configuration (image pinned by digest/ID, network, resource caps; nothing from the artifact or the job request is used as
a flag), purges leftovers of a dead run, verifies the artifact (size, SHA-256, response headers, manifest job/repo/sha),
issues a per-job model token from the model proxy, executes the plan through `execute_plan`/`cleanup_plan`, and reports
once with retries. Heartbeats run on a thread; a lost lease (409/403/unreachable worker for longer than the lease)
terminates the sandbox. The launcher never receives GitHub, Discord or provider credentials. It refuses to start when the
image tag does not resolve to the pinned ID or when the sandbox network is not `none` and not an `internal` network
labelled `prqa.net=sandbox`.

**Transport without attach/exec**: volume create → helper `create` → `docker cp` of the artifact into the helper's
writable `/work` (archive API) → helper `start`/`wait`/`logs` → sandbox `create`/`start` → `inspect` polling → `logs`
→ `rm -f` → `volume rm`. No step hijacks the connection, so the Docker policy proxy can inspect every request.

### Docker trust boundary: the policy proxy

The launcher's `DOCKER_HOST` is a unix socket served by `proxy/docker_policy_proxy.py` (compose service
`prqa-docker-proxy`, `network_mode: none`), which holds the only reference to the real daemon socket. Every request is
evaluated by `proxy/docker_policy.py` at the body level before it is forwarded:

* permitted: ping/version, `containers/create` for `prqa-populate-<job>` / `prqa-sandbox-<job>` with the exact
  hardened spec (pinned image ID, user 10001:10001, labels bound to the job and kind, `CapDrop=[ALL]`, no CapAdd, no
  Privileged, no host PID/net/IPC, no Binds, no Devices, no ports, no sysctls, `SecurityOpt ⊆ no-new-privileges`, memory
  / pids / cpu caps, tmpfs allowlist, exactly one volume mount `prqa-checkout-<job>` at `/checkout`, sandbox
  `ReadonlyRootfs` + read-only mount + entrypoint not overridden + 13-token `--prechecked` command + env key allowlist;
  helper: network `none`, extractor entrypoint, command `[/checkout, job, /work/checkout.tar]`, no env), container
  inspect/logs/start/wait/delete for PRQA-named containers, container *list always re-narrowed by the proxy* —
  every forwarded `GET /containers/json` query is rebuilt with exactly one canonical
  `filters={"label":["prqa.job","prqa.kind"]}` value carrying the mandatory PRQA labels merged in a narrowing-only
  way with caller filters (value-form entries keep their values; other filter keys ride along; unparseable,
  non-object JSON and repeated or differently-cased `filters` spellings are refused without forwarding), so the
  daemon only ever sees a query the policy decided on (`docker ps -a` and its `all`/`limit`/`size` parameters are
  preserved for the reconciliation pass), `PUT archive?path=/work` into the helper only (size capped), volume
  create/named+labelled per job (local driver, no options)/inspect/delete, volume *list always re-narrowed by the
  proxy* (the docker CLI sends `--filter label=…` client-side and asks the daemon for the full list first, so the
  proxy rewrites every `GET /volumes` query to `filters={"label":["prqa.job","prqa.kind=checkout"]}` — the CLI's map-form
  label filters are normalised to the list form the daemon accepts), network inspect restricted to the configured
  sandbox network (`allowed_networks`), image inspect of the pinned image; `/info` is denied;
* denied: everything else, including exec, attach and any `Upgrade` hijack, build, pull, commit, image delete, network
  create, prune, events, swarm, and any reference to a non-PRQA container or volume.

**Accurate blast radius of a compromised launcher** (2026-10-22 correction — the earlier "create more sandboxes"
wording understated the scope; cross-job confidentiality/integrity wording sharpened 2026-10-23). Compromising the
launcher process yields full management of the PRQA *namespace* of Docker resources, **including other jobs'**:
create hardened sandboxes/helpers for any job id, start/wait/delete any
`prqa-sandbox-<job>`/`prqa-populate-<job>` container (including a running one), read any PRQA container's logs,
re-upload the artifact into any helper (`PUT archive?path=/work`), and create/delete any `prqa-checkout-<job>` volume.
It still yields **not** root on the host, no exec/attach into any container, no host mounts, no non-PRQA container or
volume access, no engine metadata beyond ping/version, no image pull/build/delete, and no network attach.

The cross-job reach is a **real confidentiality and integrity risk, not a cosmetic detail**:

* *Confidentiality* — `docker logs` of another job's sandbox or helper discloses that job's sandbox output: the
  structured result the worker consumes, any repository-controlled text the agent printed, and anything the sandbox
  wrote to stdout (which may contain data from the *other* job's repository or staging target). Job-to-job log
  isolation does not exist in the single-principal model. Other PRQA jobs' logs **are readable** by the launcher
  principal.
* *Integrity* — another job's sandbox/helper containers (including running ones) and its checkout volume **may be
  deleted or interrupted**; a deleted running container loses the job's result, and re-uploading a tampered or
  foreign artifact is DoS (the in-sandbox tree-hash check rejects it).
* This does **not** establish host-root access, does not cross the sandbox hardening (uid 10001, cap_drop ALL,
  read-only rootfs, no outbound network), and does not expose the launcher token or provider credentials — the
  launcher principal holds none of them. The harm is operational (cross-job disclosure and disruption), never
  privilege.

With a single launcher deployment this trade-off is **accepted by design** and pinned by tests
(`tests/test_docker_policy.py::CrossJobBehaviorTests`).

*Option-B trigger conditions* (documented; per-job authorization is NOT implemented). The accepted single-principal
risk must be re-evaluated and Option B implemented before any of the following becomes true:

1. two or more `prqa-launcher` deployments ever share one policy proxy socket;
2. jobs from mutually distrusting tenants are executed with one launcher deployment (one `PRQA_LAUNCHER_TOKEN`
   authorises claim/heartbeat/result for any pending job at the worker API too — the same single-principal trust);
3. launcher-container compromise stops being treated as operationally equivalent to the operator (it is the only
   reason F1-style cross-job reach stays acceptable today).

Under Option B the proxy would bind each launcher credential/socket to an allowed job-id set and enforce job
ownership on every name/label/mount reference; `tests/test_docker_policy.py::CrossJobBehaviorTests.test_05/06`
(accepted-by-design pins) must then be rewritten to expect 403, as their own docstrings require.

Two CLI behaviours the proxy accommodates without widening policy: `docker cp` first stats the destination file
(`HEAD …/archive?path=/work/checkout.tar`, allowed for the helper only; uploads stay `PUT …?path=/work`) and streams the
upload chunked (the proxy de-chunks, applies the size cap, forwards with an explicit length); `docker logs` addresses the
container by the ID it got from an inspect, so the proxy resolves a bare ID on the real daemon and rewrites the request
to the job-named container it belongs to (unknown or non-PRQA IDs stay denied, one bounded pre-auth inspect read each).
`PATCH`/`OPTIONS`/`CONNECT`/`TRACE` are refused without reading a body. Every decision is logged as
`decision allow|deny METHOD PATH …` (`--log-file`).

**Proxy socket access, mount scope and Unix users** (verified 2026-10-22 against compose + the live stack):

* Phase 1 (the running stack): the worker container mounts **no** Docker socket and **no** proxy socket
  (`/run/prqa` does not exist in it, `DOCKER_HOST` is unset) and refuses to start if either becomes reachable
  (`PRQA_ENFORCE_NO_DOCKER`, `worker/prqa_worker/server.py`; the doc's earlier mention of `worker/prqa_worker/app.py`
  was stale). The named volume `prqa_docker_proxy_sock` and the phase2 proxy/launcher containers do not exist on the
  review daemon. With review-only mode there is no launcher process at all, so **nobody** consumes a policy socket
  today; the opt-in test suites spawn their own proxy on temporary host-level paths as the test user.
* Phase 2 design — **proxy principal, explicit**: the users able to open the policy socket are exactly
  (1) the root `prqa-docker-proxy` process that creates and serves it, and (2) **any process running inside the
  `prqa-launcher` container** — the container's user is `10002:10002` and the socket is `0660 root:10002`
  (`--socket-gid`, wired to `PRQA_LAUNCHER_GID`=10002 in compose and documented in `compose/.env.example`), so
  every process in that container — the launcher CLI loop, its heartbeat thread and each `docker` CLI subprocess it
  spawns — uses the socket with the same single identity. There is no per-process distinction inside the launcher
  container; that is the single-principal model. The proxy container itself holds the real daemon socket (its
  compromise is host-docker-equivalent; it runs `network_mode: none`, read-only rootfs, and keeps default
  capabilities only because the socket chown needs `CAP_CHOWN`). The named volume is mounted by exactly those two
  services and nothing else. No sandbox is ever created with a bind (create validation rejects all `Binds`), so
  sandbox-controlled code cannot reach the proxy socket, and every sandbox keeps `network_mode none` unless an
  internal labelled network is configured.

**Isolated integration test (`tests/test_launcher_integration.py`, opt-in `PRQA_LAUNCHER_INTEGRATION=1`, 2026-10-22):**
the real launcher CLI as a separate process, the real proxy process in front of the host daemon socket, the production
worker App over loopback HTTP (git checkout faked), a disposable stub image (real entrypoint/verifier/extractor, `opencode`
replaced by a stub that writes a synthetic result) and the promoted image where its entrypoint stops before OpenCode
(wrong tree hash → 66). It verified 42 denial cases that never reached the daemon, a full claim → verify → execute →
result → cleanup cycle, a two-launcher race (one claim, one helper + one sandbox), cancellation of a running sandbox,
reconciliation that removes a terminal job's orphan and keeps an active job's, and the pin/network refusals. 7/7 passed;
no OpenCode run, no model call, no change to the daemon's other containers, volumes or networks.

## 5. Network and the model key: the model egress proxy

`proxy/model_proxy.py` (compose service `prqa-model-proxy`) is the only process that holds the provider key
(read from `PRQA_MODEL_PROVIDER_KEY_FILE`, never from the environment of the sandbox or the launcher). Policy:

* fixed upstream origin + path prefix from configuration; `https` required unless the origin is loopback;
  `POST /v1/chat/completions` is the only forwarded endpoint (`GET /v1/models` optional, off by default);
  `CONNECT` → 405, absolute-form request targets → 400, other paths/methods → 404/405;
* per-job bearer tokens issued by the launcher through the admin API (`POST /admin/tokens`, `DELETE /admin/tokens/{job}`,
  separate admin token), bound to job + model with a TTL; expired/revoked/unknown tokens → 401; a request naming another
  model → 403; re-issuing replaces the previous token;
* locked upstream request contract (2026-10-27, §5d): the client body is rebuilt, never relayed — only `model`,
  `messages`, `tools`, `tool_choice`, `parallel_tool_calls`, `stop`, `response_format` are copied, then `stream=false`,
  `temperature=0.2`, `reasoning_effort=low`, `include_reasoning=false` and `max_completion_tokens` = min(client
  budget, 3000) are set by the proxy; malformed `messages`/`tools` → 400 before any upstream call;
* the upstream completion is validated and rebuilt (no reasoning text, no provider extras); a `stream=true` client
  receives it as a synthesized OpenAI SSE stream (content, tool_calls deltas, finish reason, usage, `[DONE]`);
* request body size, response size, upstream timeout, per-job concurrency and request budget are enforced
  (413/502/429); upstream errors are relayed by status, never invented;
* upstream headers are a fixed allowlist (`Authorization` = provider key, `Host`, `Content-Type`, `Content-Length`,
  `Accept`, `Accept-Encoding`, `User-Agent`, `X-Title`): nothing from the client (OpenCode session ids, cookies,
  referers) is relayed; logs record job id, status, sizes and the upstream request id, never bodies or tokens;
* fail closed: missing model, key, admin token or a non-https upstream refuses to start.

The sandbox network `prqa_sandbox` is `internal: true` (label `prqa.net=sandbox`); only the model proxy is attached to
both it and the egress network. No staging-target forwarding exists yet (`pass_target_token` remains refused).

**OpenCode provider configuration:** verified with the real OpenCode 1.19.3 binary offline (2026-10-27), see §5d.

## 5b. Launcher network isolation (2026-10-26)

Until 2026-10-26 `prqa-launcher` sat on `prqa_internal`, a normal bridge shared with n8n (which holds the prompt
provider key, GitHub and Discord credentials), the mocks and the worker. That bridge has a default route, so the
launcher had unrestricted egress and could open any port of n8n or the mocks. Now:

| network | `internal` | members | why |
|---|---|---|---|
| `prqa_launcher` | yes, plus `com.docker.network.bridge.inhibit_ipv4: "true"` | `prqa-launcher`, `prqa-worker` | the launcher's only peer is the worker's launcher API (`http://prqa-worker:8080`, `X-Launcher-Token`) |
| `prqa_internal` | no (unchanged) | n8n, worker, mocks | the launcher is no longer attached |
| — | — | `prqa-docker-proxy` | `network_mode: none`; reached only through the unix socket in volume `prqa_docker_proxy_sock` (0660, gid 10002) |

`inhibit_ipv4` matters: on a plain `internal: true` network the host keeps the bridge's gateway address, and a
member can still connect to host services the host firewall allows. Measured on this host, a container on such a
network reached host port 22. With `inhibit_ipv4` the host has no address on the bridge, so every host address fails
with `EHOSTUNREACH`, whatever the host firewall says. The launcher needs no gateway: the worker is on the same link,
and name resolution uses Docker's in-namespace resolver.

**The model proxy admin API** is reached through a unix socket, never over the network (see §5c). Superseded
2026-10-27: until then the admin API and the forwarding endpoint shared one TCP listener, so the model proxy was kept off
`prqa_launcher` entirely.

Verified by `tests/test_phase2_containers.py`:
* `ComposeLauncherTopologyTests` (always run, static): the compose file still has this topology.
* `DisposablePhase2StackTests` (opt-in `PRQA_PHASE2_CONTAINERS=1`): a disposable compose project built from this file
  confirms membership, the absence of default routes, `ENETUNREACH`/`EHOSTUNREACH` (never a bare timeout) towards
  every other service, gateway and Docker TCP port, name-resolution failure for every other service, policy-proxy
  denials with their specific reasons, and a full launch through the proxy.

## 5c. Model proxy: separate admin and inference interfaces (2026-10-27)

**Before.** `proxy/model_proxy.py` served everything on one TCP listener, `0.0.0.0:8089`:
* the admin routes `GET`/`POST /admin/tokens` and `DELETE /admin/tokens/{job}`, behind the Bearer admin token;
* inference, `POST /v1/chat/completions` (plus `GET /v1/models` when enabled), behind per-job tokens;
* `/healthz`, unauthenticated, which returned the upstream host and the model.

The sandbox network therefore carried the admin API, protected only by the admin token. Paths were compared exactly,
but nothing refused admin spellings on the sandbox side. Upstream errors were relayed with their body and headers.

**Now.** There are two listeners with two roles, built by `make_servers` over one token store.

| | ADMIN | INFERENCE |
|---|---|---|
| transport | unix socket `/run/prqa-model-admin/admin.sock` only (no TCP form exists in the code) | TCP `0.0.0.0:8089` in the proxy container |
| reachable from | `prqa-launcher` only: volume `prqa_model_admin_sock` (tmpfs, owner uid 10003, group = launcher GID, dir 0770, socket 0660), mounted read-only in the launcher | `prqa_sandbox` (internal, `inhibit_ipv4`); the proxy alone also sits on `prqa_egress` |
| routes | `GET`/`POST /admin/tokens`, `DELETE /admin/tokens/{job}` (job id `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`), `/healthz` (may show upstream host + model) | `POST /v1/chat/completions` (`GET /v1/models` only if enabled), `/healthz` → `{"ok": true, "interface": "inference"}` |
| credential | Bearer `PRQA_MODEL_PROXY_ADMIN_TOKEN` (proxy only; operator via `docker exec`) or the restricted `PRQA_MODEL_PROXY_MINT_TOKEN` (launcher; see §5e) | per-job `prqa_mt_…` token bound to job + model + TTL, or `PRQA_MODEL_PROXY_PROMPT_TOKEN` (n8n, §5e); **the admin and mint tokens are explicitly refused** |
| everything else | `/v1/*` → 404 "inference is not served on the administrative interface" | any spelling of `admin` (case, `%61`, `%2561`, `%2F`, `..`, `//`, `;`, `\`, `%00`) → **403**, under any method |

**Shared rules on both listeners:**
* Every HTTP method, including TRACE, PROPFIND and unknown ones, reaches one dispatcher, and every denial is an
  explicit JSON policy body naming the interface.
* Request targets must be plain paths, compared verbatim and never normalised. Queries, fragments, percent-encoding,
  backslashes, dot segments and empty segments are refused with 400.
* Absolute-form targets are refused with 400, and `CONNECT` with 405.
* Chunked or encoded request bodies and an invalid `Content-Length` are refused with 400. Oversized bodies get 413.

**Forwarding:**
* The fixed origin and prefix come from configuration, validated at start-up: https, or plain http to loopback only.
* `Host`, `Authorization`, forwarding headers, cookies and referers are replaced or stripped.
* Upstream statuses other than 2xx become a sanitized `proxy_upstream` error that keeps the status. Upstream 401/403
  becomes 502, because those concern the proxy's own credential.
* A non-stream response that is not a JSON object with `choices`, or is over the size limit, becomes 502. Only
  `Content-Type` and a well-formed `X-Request-Id` are relayed.
* Logs carry the job id, status and sizes, never keys, tokens, bodies or reasoning.

**Launcher client:** `ModelProxyClient` accepts only `unix:///…` admin URLs and refuses TCP URLs before sending
anything. A missing socket fails the job closed, and revocation still runs in `finally`.

**Provider credential:** it is read only from `PRQA_MODEL_PROVIDER_KEY_FILE`, the compose secret
`prqa_model_provider_key`, inside the proxy. It never reaches the launcher, the sandbox, an image, a build argument or
a log. (Update 2026-10-27: the key file is now provisioned outside the project drive and the upstream is direct Groq;
see §5d.) The prompt-builder provider (direct Groq) is separate and unaffected.

**Verified by:**
* `tests/test_model_proxy_separation.py` (13 tests): the method × admin-spelling matrix, credential crossing, SSRF
  targets, a loopback decoy that is never hit, malformed and oversized input, sanitized upstream failures, and logs
  free of secrets.
* `tests/test_model_proxy.py`: the existing behaviour, with tokens now issued on the admin socket.
* `tests/test_phase2_containers.py`: the static compose topology, and in the disposable stack:
  * the model proxy's listeners, i.e. the LISTEN table of its namespace;
  * the admin socket's mode and group;
  * admin calls from the launcher over the socket;
  * a sandbox-equivalent container that holds even the admin token and still gets 403 on every admin spelling;
  * the real job's stub sandbox completing one mocked inference with the proxy-injected synthetic key, against a
    loopback fake upstream.

## 5d. OpenCode → model proxy → Groq, job-token authentication (2026-10-27)

**Locked provider.** Direct Groq, `openai/gpt-oss-120b`, for both the prompt builder (n8n → Groq, `GROQ_API_KEY`) and
the sandbox path (OpenCode → `prqa-model-proxy` → `https://api.groq.com/openai/v1`). The compose file fixes the proxy's
upstream origin, prefix, model and request contract; nothing in it names OpenRouter.

**Flow.** The launcher validates the claim; the OpenCode model must be `prqa/<model>` and the launcher asks the admin
socket for a token bound to `<model>` (anything else fails closed before a token exists). The sandbox receives
`PRQA_MODEL_BASE_URL` (non-secret) and `PRQA_MODEL_TOKEN` (env-file on stdin, never argv). The entrypoint validates
both (token shape, `http://<host>[:port]/v1`, model `prqa/…`, evidence dir under `/work`; exit 67 otherwise) and
writes `OPENCODE_CONFIG_CONTENT`:

```json
{"enabled_providers": ["prqa"], "autoupdate": false, "share": "disabled", "model": "prqa/openai/gpt-oss-120b",
 "permission": {"external_directory": {"*": "deny", "/work/evidence/*": "allow"}},
 "provider": {"prqa": {"npm": "@ai-sdk/openai-compatible", "name": "PRQA model proxy",
   "options": {"baseURL": "http://prqa-model-proxy:8089/v1", "apiKey": "{env:PRQA_MODEL_TOKEN}"},
   "models": {"openai/gpt-oss-120b": {"name": "openai/gpt-oss-120b", "tool_call": true, "limit": {"context": 131072, "output": 3000}}}}}}
```

OpenCode then runs under `env -i` with an explicit allowlist: `HOME PATH LANG TMPDIR`, `OPENCODE_CONFIG_CONTENT`, the
fixed `OPENCODE_DISABLE_*`/`OPENCODE_PURE` switches (project config, models.dev fetch, autoupdate, LSP download, default
plugins, Claude-Code compatibility, external skills), `PRQA_MODEL_TOKEN`, `PRQA_JOB_ID`, `PRQA_EVIDENCE_DIR`,
`PRQA_JOB_JSON`, `PRQA_TEST_LEVEL`, and `BASE_URL`/`TOKEN` only when present. With a non-`prqa/` model and no token
(the stub-only suites) no provider is configured; a `prqa/` model without a token stops with exit 67.

**Measured OpenCode 1.19.3 contract (real binary, offline).** `POST <baseURL>/chat/completions`,
`Authorization: Bearer <PRQA_MODEL_TOKEN>`, body model `openai/gpt-oss-120b` (the `prqa/` prefix is the provider id),
always `stream: true` with `stream_options.include_usage`, `max_tokens` (the 3000 output limit), 10 function tools
(bash, edit, glob, grep, read, skill, task, todowrite, webfetch, write) with `tool_choice: "auto"`, and session headers
(`x-opencode-session-id`, `x-session-affinity`, `x-session-id`) that the proxy does not relay. The proxy therefore calls
Groq non-streaming and synthesizes the SSE stream OpenCode parses; tool calls round-trip as `tool_calls` deltas and
`role: tool` messages.

**Evidence directory permission.** OpenCode works in the read-only `/checkout/repo`; writing
`$PRQA_EVIDENCE_DIR/qa-result.json` is an `external_directory` access, which `opencode run` auto-rejects by default
("permission requested: external_directory (/work/evidence/*); auto-rejecting", observed). Before this change no real
run could have produced a result document. The configuration allows exactly the evidence directory (a tmpfs inside
the sandbox) and denies every other external directory; both are verified with the real binary.

**Credentials.** `scripts/setup/provision_model_proxy_secrets.py` derives `~/.config/prqa/model-provider.key` (mode 600,
atomic, outside the project drive) from `GROQ_API_KEY` in `~/.config/prqa/secrets.env` (single source of truth; re-run
after a key rotation), generates `PRQA_MODEL_PROXY_ADMIN_TOKEN` once with the `secrets` CSPRNG (a valid token is
preserved), and records `PRQA_MODEL_PROVIDER_KEY_FILE` and `PRQA_MODEL_PROXY_UID` there. It prints statuses only.
Compose file secrets are bind mounts that keep the host owner and mode (per-secret `uid`/`mode` are ignored outside
swarm, verified), so the proxy runs as `PRQA_MODEL_PROXY_UID` (the key file's owner): the only container that mounts
the key, and the only one that can read it. The admin token reaches only the launcher and the proxy.
The same script provisions `PRQA_LAUNCHER_TOKEN` (2026-10-27): generated once (`secrets.token_urlsafe(48)`), kept
while valid, and replaced when malformed or equal to the provider key or the admin token. It lives only in
`secrets.env`, never in `compose/.env`. Both consumers fail closed on a bad value. The launcher refuses to start when
the token is missing, does not match `^[A-Za-z0-9_-]{32,256}$`, or equals the admin token. The worker disables its
launcher API (every call gets 401) when the token is malformed or equals the worker token. The token never reaches the
sandbox: it is in `opencode_runner.SECRET_ENV_NAMES` and outside the policy proxy's environment allowlist.

**Starting Phase 2.** `scripts/start.sh --phase2` is the supported start. Before anything is (re)started it refuses
when any of these fails:
* `AUTOMATION_MODE` is `review_only`;
* `provision_model_proxy_secrets.py --check` passes;
* `PRQA_SANDBOX_IMAGE_ID` equals `prqa/opencode-sandbox:local`.

It then starts the base stack, rebuilds the launcher image, and starts `prqa-docker-proxy`, `prqa-model-proxy` and
`prqa-launcher` with `--no-deps`. No volume is removed. While review-only, the worker answers every launcher poll with
`403 REVIEW_ONLY_MODE` (after the token check), so no job can be claimed. No health check contacts the provider.

**Sandbox network.** `PRQA_SANDBOX_NETWORK` is read by both the launcher and the Docker policy proxy (one variable, so
they cannot disagree); unset means `none`. The policy proxy refuses `host`, `bridge`, `default`, `container:*` and
malformed names at start-up, and permits container create only on `none` or that one network (`NetworkMode` and
every `EndpointsConfig` key). The launcher refuses a partial model-proxy configuration, a malformed inference URL,
`none` while the model proxy is configured, and a network that is not internal + `prqa.net=sandbox` + `inhibit_ipv4`.

**Verified by:**
* `tests/test_model_proxy_contract.py`: the exact upstream body and headers, budget clamping, SSE text and tool-call
  synthesis, tool-result round trip, sanitized JSON, malformed upstream completions → 502, and config validation.
* `tests/test_sandbox_network_policy.py`: network agreement, mismatch, missing settings, unauthorised networks, model
  mapping and partial configuration.
* `tests/test_entrypoint_model_config.py`: entrypoint fail-closed cases and the generated configuration.
* `tests/test_provision_model_proxy_secrets.py`: the provisioning script, with no value ever printed.
* `tests/test_opencode_proxy_auth.py` (opt-in `PRQA_OPENCODE_PROXY_AUTH=1`): the real OpenCode 1.19.3 in the promoted
  image, run through the production plan and the Docker policy proxy, against the model proxy and a scripted fake
  Groq. It covers:
  * the token header and the locked contract;
  * read → write (evidence) → write (outside, denied) → final;
  * a valid result document;
  * a revoked token rejected with nothing upstream;
  * a missing token stopping at exit 67.
* `tests/test_phase2_containers.py`: the full disposable stack. The stub follows `OPENCODE_CONFIG_CONTENT` /
  `{env:PRQA_MODEL_TOKEN}`, the key file is mode 600, and the OpenCode environment is exactly the allowlist.

**Image.** `prqa/opencode-sandbox:local` = `sha256:d0efff568953…`, an offline overlay (no network, no registry) of
`1af154a6` with the reviewed entrypoint (in-image SHA-256 equals the source). Rollback tag:
`prqa/opencode-sandbox:rollback-1af154a6`.

**Per-token request cap (2026-10-27).** An admin token request may carry `max_requests` (an integer from 1 up to the
proxy-wide `PRQA_MODEL_MAX_REQUESTS`; anything else gets 422). The proxy counts every attempt under its lock *before*
the upstream call, failures included, so a token capped at 1 can cause at most one upstream request. A later request
gets 429 without contacting the provider. The proxy has no retry path. Tokens issued without the field (every launcher
job) keep the proxy-wide budget. Test: `tests/test_model_proxy.py::test_per_token_request_cap_stops_before_the_upstream`.

**One live tool-calling check (2026-10-27, user-authorized, exactly one upstream request).**
* *Request:* the exact upstream body the real OpenCode 1.19.3 produced for its first tool-bearing turn, captured offline
  through the fake provider (`PRQA_OCAUTH_CAPTURE_OUT`). It contains OpenCode's 10 tools, `tool_choice: auto`, the
  locked contract, and a synthetic prompt and checkout. It was sent once through the persistent proxy with a token capped
  at 1.
* *Result:* Groq returned HTTP 200 with `finish_reason: tool_calls` and one schema-valid `read` call. The proxy stripped
  the reasoning, and no tool was executed.
* *What it shows:* Groq and GPT-OSS 120B accept OpenCode's request format and return a compatible tool call. It does not
  show a complete multi-turn OpenCode conversation (`role: tool` follow-ups were not sent live). See
  `runs/groq-toolcall-compat-report-20261027.md`.

**Provider budget (2026-10-27).** `proxy/provider_budget.py` is a file-locked ledger in
`PRQA_PROVIDER_BUDGET_DIR`: `~/.config/prqa/budget`, mode 700, created by the provisioning script, bind-mounted only
into `prqa-model-proxy`. Both the model proxy (every upstream request) and `scripts/eval/evaluate_groq_live.py` reserve in
it before sending.
* *Limits* (in `compose/.env`):
  * 5 requests and 40,000 tokens per UTC day;
  * 24,000 tokens per job;
  * 8,000 tokens per minute (rolling 60 s window; the proxy waits up to 90 s);
  * 1 request in flight;
  * Groq's `x-ratelimit-remaining/reset-tokens` respected when reported.
* *Reservation:* calibrated prompt estimate + full completion allowance. It is settled with the reported usage
  *before* the reply reaches the client. Requests are never refunded.
* *Fail closed:* an https upstream without a valid budget refuses to start, the evaluator blocks, and a corrupt ledger
  gets 503.
* *Limitations:*
  * token accounting only, no monetary cost (Groq returns no price, so no cost is computed or enforced);
  * ~~the n8n prompt-builder path calls Groq directly~~ — closed 2026-10-27, see §5e.
* *Per-token admin options:* `max_completion_tokens` (≤ 3000) and `halt_on_error` (the first failed or `length`
  result revokes the token before the client can retry).
* *Tests:* `tests/test_provider_budget.py`.

## 5e. One budget for every generation path; least-privilege proxy credentials (2026-10-27)

**Every path that can reach the provider** (inventory pinned by `tests/test_generation_call_paths.py`):

| path | route to Groq | budget |
|---|---|---|
| OpenCode in a sandbox | `prqa_sandbox` → model proxy, per-job token | proxy reserves the ledger before connecting |
| n8n workflow 03 (prompt generation) | `prqa_prompt` (internal; n8n + proxy only) → model proxy, `PRQA_MODEL_PROXY_PROMPT_TOKEN` | same ledger, same reservation; ledger job id `prompt-<repo>-<pr>` |
| `scripts/eval/evaluate_groq_live.py` (operator) | host → Groq | reserves the same ledger directory before its single send |
| `scripts/eval/evaluate_openrouter_live.py` | none: its only network function refuses (retired) | n/a |

No service receives `GROQ_API_KEY` through its environment any more: n8n lost it, and only `prqa-model-proxy` reads the
key file. The worker hands n8n the proxy URL (`PRQA_PROMPT_LLM_PROXY_URL`) in real mode and refuses to start if that URL is
not a plain-http proxy URL distinct from the provider base; workflow 03 never calls a non-`http://` base and sends only the
prompt credential; `scripts/build/validate_workflows.py` rejects any workflow that references a provider key or host. Because
the proxy is in the `phase2` profile, n8n's real request fails while it is not running, and the deterministic fallback is
used.

**Four credentials, enforced by the proxy** (`validate()` refuses equal values or a value equal to the provider key):

| credential | holder | can | cannot |
|---|---|---|---|
| admin | proxy only (operator: `docker exec` into the proxy) | list, issue and revoke any token | inference (403) |
| mint | launcher | issue a per-job token (same caps and validation as admin); revoke tokens **it** issued | list tokens (403), revoke or replace another issuer's token (404 / 409), use `prompt-*` job ids (422), inference (403) |
| per-job `prqa_mt_…` | one sandbox | inference for its job, model and TTL | any admin route (401) |
| prompt | n8n | inference with the configured model, no tools, one upstream attempt per call | any admin route (401), tools (403) |

The launcher refuses to start if it is given `PRQA_MODEL_PROXY_ADMIN_TOKEN`. Audit lines name the job and the issuing
principal (`token issued for job X by mint`), never a token value. Tests: `tests/test_model_proxy_capabilities.py`.

**Not verified offline.** Groq's acceptance of this exact request has not been tested against the live API. That
includes OpenCode's tool JSON schemas, `role: tool` messages and `reasoning_effort` together with tools. Neither has
GPT-OSS 120B's actual tool-calling behaviour.

## 5a. Local Docker transport test (`tests/test_docker_transport.py`)

Opt-in with `PRQA_DOCKER_INTEGRATION=1`. It builds a disposable image from the locally cached `python:3.13-slim`
(no pull, `--network none`) with the real entrypoint, verifier and extractor, then runs the real plan for: a valid
artifact, a wrong expected SHA (66), 500 files, missing / malformed / tampered / symlink artifacts, cross-job isolation
and the exact plan arguments, **plus the production `execute_plan` path itself**: artifact uploaded into the helper (archive API, no attach), the
INCONCLUSIVE fallback document returned, helper (70) and sandbox (66) exit codes propagated, a real timeout and a real
cancellation terminating a running sandbox, an observable cleanup failure (volume kept busy by another container), and
launcher reconciliation removing a terminal job's orphans while keeping an active job's. It inspects every sandbox container (`ReadonlyRootfs=true`, `NetworkMode=none`,
`User=10001:10001`, no binds, the job's volume mounted `rw=false`, no secret names in the environment) and removes every
container, volume, image and temporary directory it created, including on failure. It never runs OpenCode (the
disposable image has none; the exact-plan case ends in the honest INCONCLUSIVE document). Docker access is needed on the
host running the test, not in the worker.

**Image rebuild (resolved 2026-10-22, 14:41):** the first build (`sha256:bbc07187…`) predated the three-argument
extractor (exit 64 on `/work/checkout.tar`). Because `Dockerfile.opencode` starts from `node:22-bookworm-slim`, which
lives only in BuildKit's cache and would need registry tag resolution (network), the fix was built **offline as an
overlay** on the previous image (kept as `prqa/opencode-sandbox:rollback-bbc07187`): the three reviewed files from
`worker/sandbox/` copied over the identical base layers, `--network none`, no registry contact (metadata resolved
locally in 0.0 s, no new outbound connections). Result `prqa/opencode-sandbox:local` =
`sha256:1af154a6ed99dd1b8d1f8c2fd5126ed86839099abbc6406fe2e691dbac30b5df`; in-image SHA-256 of entrypoint, verifier
and extractor equal the source files; OpenCode 1.19.3 present, never executed. `Dockerfile.opencode` remains the
canonical recipe for the next build that is allowed to reach the registry. The suite can target an existing image with
`PRQA_DOCKER_TEST_IMAGE=<ref>` (cases F and G are then skipped because they would reach the OpenCode invocation); run
against the new image: 10 passed, 2 skipped; stub image: 12 passed.

Operational cleanup: `docker volume ls --filter label=prqa.kind=checkout` lists checkout volumes; a volume or container
left behind by a crashed launcher is named `prqa-checkout-<job>`, `prqa-populate-<job>`, `prqa-sandbox-<job>` and may be
removed once the job is terminal. Never remove resources of a job that is still `running`.

## 6. Authorization model for label triggers

- Webhook path: the event is HMAC-authenticated; `sender.login` is set by GitHub and names the user who added the label.
  `PRQA_TRIGGER_ALLOWED_ACTORS` (comma-separated logins, case-insensitive) restricts who may trigger; empty = anyone
  with triage rights on the repository. Fork PRs stay ineligible unless `fork_prs_eligible` is set per repository.
- Actions path: `requested_by` is supplied by the caller (`GITHUB_ACTOR`); trust equals trust in the holder of
  `PRQA_WEBHOOK_SECRET`. The allowlist is **not** enforced on it.
- Local poller: resolves who added the label from the GitHub issue-events API (GitHub-set `actor`) and skips labels
  added by anyone outside the allowlist; it forwards the login as `labelled_by` for the report.

## 7. Resource bounds

`PRQA_CHECKOUT_TIMEOUT_S` (300) for the whole checkout, `PRQA_CHECKOUT_MAX_MB` (500) for the tree and the artifact,
`PRQA_WORKSPACE_MIN_FREE_MB` (1024) before creating a workspace, `PRQA_WORKSPACE_MAX_COUNT` (50) marked workspaces
(released ones are swept first), `PRQA_WORKSPACE_RETENTION_HOURS` (24), registry `max_runtime_minutes` for the sandbox,
and the container caps above. Cleanup removes only real directories directly under the root whose marker names the
directory itself; symlinks, unmarked or foreign-marked directories are skipped.
