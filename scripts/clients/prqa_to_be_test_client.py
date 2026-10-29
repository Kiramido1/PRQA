#!/usr/bin/env python3
# PRQA `to be test` dispatch client (v2.0). Embedded VERBATIM into the per-repository workflow
# .github/workflows/prqa-to-be-test.yml by scripts/setup/prqa_connect_repository.py — edit this file, then regenerate.
# Python 3.8+ stdlib only. No checkout, no PR code, no GitHub API call, no token.
#
# What it does, in order:
#   1. reads the event JSON from GITHUB_EVENT_PATH (never from shell-interpolated expressions)
#   2. decides, with an EXACT case-sensitive label comparison, whether this event dispatches a run:
#        labeled                                   -> only when the label just added is exactly "to be test"
#        opened / reopened / synchronize / ready_for_review -> only when "to be test" is on the PR
#        anything else (unlabeled, edited, closed, other labels) -> nothing is sent
#      draft, closed and fork pull requests are skipped with a notice
#   3. checks that the event belongs to the repository this workflow was generated for
#   4. reads PRQA_WEBHOOK_URL / PRQA_WEBHOOK_SECRET / PRQA_CONNECTION_ID (fails visibly when one is missing)
#   5. builds the minimal schema-2.0 payload (ids, SHAs, refs; no title, body, files or credentials), signs
#      "<timestamp>.<raw body>" with HMAC-SHA256(connection secret) and POSTs it with a bounded timeout and at most
#      PRQA_MAX_ATTEMPTS attempts (transport errors, 429, 5xx only). The delivery id gha-<run id>-<attempt> lets the
#      receiver reject replays; a 409 DELIVERY_REPLAYED after an earlier failed attempt means "already delivered".
#   6. writes GITHUB_OUTPUT / GITHUB_STEP_SUMMARY. Never prints the secret, the signature or any header value.
#
# HTTP 202 means "dispatch accepted" — never "the PR passed". PRQA reports the validated result itself.
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

CLIENT_VERSION = "2.0"
TRIGGER_LABEL = "to be test"
DISPATCH_ACTIONS = ("opened", "reopened", "labeled", "synchronize", "ready_for_review")
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CONNECTION_ID_RE = re.compile(r"^prqa_c_[0-9a-f]{24}$")
INGRESS_PATH = "/webhook/prqa/actions"
ENV = os.environ   # replaced by main(env=...) in tests


class ClientError(Exception):
    pass


def log(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def annotate(kind, msg):
    log("::%s::%s" % (kind, msg.replace("%", "%25").replace("\r", "").replace("\n", "%0A")))


def write_output(name, value):
    path = ENV.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s=%s\n" % (name, str(value).replace("\n", " ")))


def write_summary(lines):
    path = ENV.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def host_of(url):
    m = re.match(r"^https?://([^/:@]+)", url or "")
    return m.group(1) if m else "<invalid url>"


def env_int(env, name, default, lo, hi):
    raw = str(env.get(name, "") or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise ClientError("%s must be an integer" % name)
    return max(lo, min(hi, value))


def load_event(env):
    path = str(env.get("GITHUB_EVENT_PATH", "") or "")
    if not path:
        raise ClientError("GITHUB_EVENT_PATH is not set (this client only runs inside GitHub Actions)")
    try:
        with open(path, encoding="utf-8") as fh:
            event = json.load(fh)
    except (OSError, ValueError):
        raise ClientError("the GitHub event payload could not be read as JSON")
    if not isinstance(event, dict) or not isinstance(event.get("pull_request"), dict):
        raise ClientError("the GitHub event payload has no pull_request object")
    return event


def label_names(pr):
    out = []
    for item in pr.get("labels") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            out.append(item["name"])
    return out


def decide(event_name, event):
    """Returns (dispatch, reason). Pure: the exact label policy, tested case by case."""
    if event_name != "pull_request":
        return False, "event %r is not pull_request" % str(event_name)[:40]
    action = str(event.get("action") or "")
    pr = event.get("pull_request") or {}
    if action not in DISPATCH_ACTIONS:
        return False, "action %r never dispatches (label removal and other actions start nothing)" % action[:40]
    if action == "labeled":
        added = (event.get("label") or {}).get("name") if isinstance(event.get("label"), dict) else None
        if added != TRIGGER_LABEL:
            return False, "the label just added is not exactly %r" % TRIGGER_LABEL
    elif TRIGGER_LABEL not in label_names(pr):
        return False, "the pull request does not carry the label %r" % TRIGGER_LABEL
    if pr.get("state") != "open":
        return False, "the pull request is not open"
    if pr.get("draft") is True:
        return False, "draft pull requests are not tested (marking it ready for review dispatches)"
    base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    if not head_repo or head_repo != base_repo:
        return False, "fork pull request: repository secrets are not available to forks, so nothing is sent"
    return True, "dispatch (%s)" % action


def read_config(env):
    url = str(env.get("PRQA_WEBHOOK_URL", "") or "").strip()
    secret = str(env.get("PRQA_WEBHOOK_SECRET", "") or "")
    connection_id = str(env.get("PRQA_CONNECTION_ID", "") or "").strip()
    missing = [n for n, v in (("PRQA_WEBHOOK_URL (secret)", url), ("PRQA_WEBHOOK_SECRET (secret)", secret),
                              ("PRQA_CONNECTION_ID (variable)", connection_id)) if not v]
    if missing:
        raise ClientError("PRQA is not configured for this repository: missing %s. See the INSTALL.md generated with this workflow."
                          % ", ".join(missing))
    if not CONNECTION_ID_RE.match(connection_id):
        raise ClientError("PRQA_CONNECTION_ID is malformed (expected prqa_c_ followed by 24 hex characters)")
    if len(secret) < 32:
        raise ClientError("PRQA_WEBHOOK_SECRET is too short; export it again with scripts/setup/prqa_connect_repository.py export-secret")
    local = env.get("PRQA_ALLOW_INSECURE_URL") == "1" and re.match(r"^http://(127\.0\.0\.1|localhost)(:[0-9]{1,5})?/", url)
    if not url.startswith("https://") and not local:
        raise ClientError("PRQA_WEBHOOK_URL must be an https:// URL (host: %s)" % host_of(url))
    if not url.split("?", 1)[0].rstrip("/").endswith(INGRESS_PATH):
        raise ClientError("PRQA_WEBHOOK_URL must end with %s (host: %s)" % (INGRESS_PATH, host_of(url)))
    repo = str(env.get("GITHUB_REPOSITORY", "") or "").strip()
    expected = str(env.get("PRQA_EXPECTED_REPOSITORY", "") or "").strip()
    if not REPO_RE.match(repo):
        raise ClientError("GITHUB_REPOSITORY is missing or malformed")
    if repo != expected:
        raise ClientError("this workflow was generated for %s but runs in %s; generate a package for this repository" % (expected or "?", repo))
    run_id = str(env.get("GITHUB_RUN_ID", "") or "")
    run_attempt = str(env.get("GITHUB_RUN_ATTEMPT", "") or "1")
    if not re.match(r"^[0-9]{1,20}$", run_id) or not re.match(r"^[0-9]{1,5}$", run_attempt):
        raise ClientError("GITHUB_RUN_ID / GITHUB_RUN_ATTEMPT are missing or malformed")
    return {"url": url, "secret": secret, "connection_id": connection_id, "repository": repo, "run_id": run_id,
            "run_attempt": run_attempt, "workflow": str(env.get("GITHUB_WORKFLOW", "") or "")[:200],
            "timeout_s": env_int(env, "PRQA_HTTP_TIMEOUT_SECONDS", 20, 5, 60),
            "max_attempts": env_int(env, "PRQA_MAX_ATTEMPTS", 3, 1, 5)}


def build_payload(cfg, event, now):
    pr = event["pull_request"]
    repo = event.get("repository") or {}
    head, base = pr.get("head") or {}, pr.get("base") or {}
    number = pr.get("number")
    if repo.get("full_name") != cfg["repository"] or (base.get("repo") or {}).get("full_name") != cfg["repository"]:
        raise ClientError("the event's repository does not match %s" % cfg["repository"])
    if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 10000000:
        raise ClientError("the pull request number is malformed")
    if not SHA_RE.match(str(head.get("sha") or "")) or not SHA_RE.match(str(base.get("sha") or "")):
        raise ClientError("head/base SHA are malformed")
    action = str(event["action"])
    repo_id = repo.get("id")
    sender = (event.get("sender") or {}).get("login") if isinstance(event.get("sender"), dict) else None
    return {
        "schema_version": "2.0",
        "type": "prqa.to_be_test.dispatch",
        "connection_id": cfg["connection_id"],
        "repository": {"full_name": cfg["repository"], "id": repo_id if isinstance(repo_id, int) and not isinstance(repo_id, bool) and repo_id > 0 else None},
        "pull_request": {"number": number, "state": "open", "draft": False,
                         "head": {"sha": head["sha"], "ref": str(head.get("ref") or "")[:255],
                                  "repo_full_name": str((head.get("repo") or {}).get("full_name") or "")[:140] or None},
                         "base": {"sha": base["sha"], "ref": str(base.get("ref") or "")[:255]}},
        "event": {"name": "pull_request", "action": action, "label": TRIGGER_LABEL,
                  "sender_login": str(sender)[:100] if isinstance(sender, str) else None},
        "delivery": {"id": "gha-%s-%s" % (cfg["run_id"], cfg["run_attempt"]), "created_at": int(now)},
        "actions": {"run_id": cfg["run_id"], "run_attempt": cfg["run_attempt"], "workflow": cfg["workflow"],
                    "client_version": CLIENT_VERSION},
    }


def sign(secret, timestamp, raw_body):
    mac = hmac.new(secret.encode("utf-8"), ("%d." % int(timestamp)).encode("utf-8") + raw_body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def http(opener, url, body, headers, timeout_s):
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with opener(req, timeout=timeout_s) as resp:
            return int(resp.status), dict(resp.headers.items()), resp.read(65536)
    except urllib.error.HTTPError as err:
        return int(err.code), dict(err.headers.items()) if err.headers else {}, err.read(65536)


def send(cfg, payload, opener, sleep, clock):
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8")
    last_error, earlier_failure = "no attempt made", False
    for attempt in range(1, cfg["max_attempts"] + 1):
        ts = int(clock())
        headers = {"Content-Type": "application/json", "User-Agent": "prqa-to-be-test/" + CLIENT_VERSION,
                   "X-PRQA-Timestamp": str(ts), "X-PRQA-Signature": sign(cfg["secret"], ts, raw)}
        try:
            status, resp_headers, body = http(opener, cfg["url"], raw, headers, cfg["timeout_s"])
        except (urllib.error.URLError, OSError) as err:
            last_error, earlier_failure = type(err).__name__, True
            log("attempt %d/%d: transport failure (%s)" % (attempt, cfg["max_attempts"], last_error))
        else:
            try:
                parsed = json.loads(body.decode("utf-8")) if body else {}
            except ValueError:
                parsed = {}
            parsed = parsed if isinstance(parsed, dict) else {}
            if status == 409 and parsed.get("reason_code") == "DELIVERY_REPLAYED" and earlier_failure:
                return 200, dict(parsed, outcome="delivered_earlier")
            if status in (429, 500, 502, 503, 504) and parsed.get("reason_code") != "GITHUB_READ_CREDENTIAL_MISSING" \
                    and attempt < cfg["max_attempts"]:
                earlier_failure = True
                try:
                    wait = min(30.0, max(1.0, float(resp_headers.get("Retry-After") or parsed.get("retry_after_s"))))
                except (TypeError, ValueError):
                    wait = 5.0 * attempt
                log("attempt %d/%d: HTTP %d, retrying in %.0fs" % (attempt, cfg["max_attempts"], status, wait))
                sleep(wait)
                continue
            return status, parsed
        if attempt < cfg["max_attempts"]:
            sleep(5.0 * attempt)
    raise ClientError("PRQA receiver unreachable after %d attempts (host %s): %s" % (cfg["max_attempts"], host_of(cfg["url"]), last_error))


def interpret(status, body, cfg, payload):
    outcome = str(body.get("outcome") or ("http_%d" % status))[:40]
    reason_code = re.sub(r"[^A-Z0-9_]", "", str(body.get("reason_code") or ""))[:60]
    reason = re.sub(r"[\x00-\x1f]", " ", str(body.get("reason") or ""))[:240]
    pr = payload["pull_request"]
    write_output("outcome", outcome)
    write_output("reason_code", reason_code)
    write_output("delivery_id", payload["delivery"]["id"])
    rows = ["| | |", "|---|---|", "| Repository | `%s` |" % cfg["repository"], "| Pull request | #%d |" % pr["number"],
            "| Head SHA | `%s` |" % pr["head"]["sha"], "| Event | `%s` |" % payload["event"]["action"],
            "| Delivery | `%s` |" % payload["delivery"]["id"], "| Outcome | `%s` %s |" % (outcome, reason_code)]
    if status == 202 and outcome == "accepted":
        log("PRQA accepted the dispatch for #%d (%s)" % (pr["number"], payload["delivery"]["id"]))
        write_summary(["## PRQA dispatch accepted", ""] + rows + ["", "Accepted is not passed: PRQA reports the validated result when the job finishes."])
        return 0
    if status == 200 and outcome in ("duplicate", "delivered_earlier"):
        annotate("notice", "PRQA already has this PR head SHA (%s); nothing new was started." % (reason_code or outcome))
        write_summary(["## PRQA dispatch already received", ""] + rows)
        return 0
    if status == 200 and outcome == "blocked":
        annotate("warning", "PRQA recorded the dispatch but its policy blocks execution (%s): %s" % (reason_code, reason))
        write_summary(["## PRQA dispatch recorded, not executed", ""] + rows + ["", "> " + reason])
        return 0
    hints = {401: "the connection secret or id does not match PRQA, or the runner clock is off; re-export the secret",
             403: "the repository is not registered with PRQA or does not match this connection",
             409: "this delivery was already received", 413: "payload too large",
             422: "the dispatch was rejected by validation", 429: "rate limited; retry later",
             503: "PRQA is unavailable or not provisioned (e.g. its read-only GitHub credential is missing)"}
    msg = "PRQA rejected the dispatch: HTTP %d %s %s. %s" % (status, outcome, reason_code, hints.get(status, ""))
    annotate("error", msg)
    write_summary(["## PRQA dispatch failed", ""] + rows + ["", msg])
    return 1


def main(env=None, opener=urllib.request.urlopen, sleep=time.sleep, clock=time.time):
    global ENV
    env = os.environ if env is None else env
    ENV = env
    try:
        event = load_event(env)
        dispatch, why = decide(str(env.get("GITHUB_EVENT_NAME", "") or ""), event)
        write_output("dispatch", "true" if dispatch else "false")
        if not dispatch:
            annotate("notice", "PRQA: no dispatch — %s." % why)
            write_summary(["## PRQA: nothing dispatched", "", why])
            return 0
        cfg = read_config(env)
        payload = build_payload(cfg, event, clock())
        log("PRQA dispatch: %s #%d %s head=%s target=%s" % (cfg["repository"], payload["pull_request"]["number"],
                                                             payload["event"]["action"], payload["pull_request"]["head"]["sha"][:12], host_of(cfg["url"])))
        status, body = send(cfg, payload, opener, sleep, clock)
        return interpret(status, body, cfg, payload)
    except ClientError as err:
        annotate("error", "PRQA client: %s" % err)
        write_output("outcome", "client_error")
        write_summary(["## PRQA dispatch not sent", "", str(err)])
        return 1


if __name__ == "__main__":
    sys.exit(main())
