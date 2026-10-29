#!/usr/bin/env python3
"""Shared provider budget ledger: the one place every real provider request is counted before it is sent.

Used by the model proxy (proxy/model_proxy.py, every upstream request) and by the direct prompt evaluator
(scripts/eval/evaluate_groq_live.py). Both open the SAME ledger directory (PRQA_PROVIDER_BUDGET_DIR: a private host
directory, mode 700, bind-mounted into the proxy), so neither can spend what the other already spent.

Per request, under an exclusive file lock, BEFORE the request leaves the host:
  * requests per UTC day          <= PRQA_BUDGET_DAILY_REQUESTS
  * tokens per UTC day            <= PRQA_BUDGET_DAILY_TOKENS   (reserved estimate, later replaced by reported usage)
  * tokens per job                <= PRQA_BUDGET_JOB_TOKENS
  * tokens in the last 60 s       <= PRQA_BUDGET_TOKENS_PER_MINUTE (rolling window; the caller may wait for it)
  * requests in flight (global)   <= PRQA_BUDGET_MAX_IN_FLIGHT
  * the provider's own rate-limit headers, when a previous response reported them (remaining tokens / reset)
A reservation is the estimated prompt tokens (calibrated byte ratio × ESTIMATE_SAFETY) plus the request's full completion
allowance (`reservation_tokens`); no single request may reserve more than MAX_REQUEST_SHARE of the TPM limit. Requests are counted when
reserved and never refunded (a failed request still counts). After the response, `settle` replaces the reservation by
the provider-reported total_tokens; without usage the reservation stays (conservative). There is no retry anywhere.

Token accounting only: no monetary cost is computed or enforced (the provider does not return a price per request).
A missing or malformed configuration or ledger raises BudgetError: callers must not send the request.
Stdlib only. Never stores prompts, bodies, headers or credentials — job ids, sources, timestamps and counts only.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import stat
import time
import uuid
from dataclasses import dataclass
from typing import Any

WINDOW_S = 60.0
STALE_IN_FLIGHT_S = 600.0          # an in-flight reservation older than this (crashed caller) stops blocking; it stays counted
LEDGER_NAME, LOCK_NAME = "ledger.json", "ledger.lock"
# No local tokenizer is installed, so the prompt estimate is bytes / calibrated ratio × ESTIMATE_SAFETY. Observed on
# 2026-10-28 (Groq openai/gpt-oss-120b, Amenly PR #2 scope stage, code/diff-heavy input): 2387 estimated vs 2915 actual
# prompt tokens (×1.22; reservation 4187 vs actual 4715). The other stages were ×1.01–1.04. 1.3 covers the worst observed
# ratio with ~7 % headroom.
ESTIMATE_SAFETY = 1.3
# One request may use at most this share of the TPM limit (8000 → 7200), so an estimate that is still low cannot push a
# single request past the provider's per-minute limit.
MAX_REQUEST_SHARE = 0.9
REQUIRED_KEYS = ("PRQA_BUDGET_DAILY_REQUESTS", "PRQA_BUDGET_DAILY_TOKENS", "PRQA_BUDGET_JOB_TOKENS",
                 "PRQA_BUDGET_TOKENS_PER_MINUTE", "PRQA_BUDGET_MAX_IN_FLIGHT")


class BudgetError(RuntimeError):
    """Configuration or ledger problem: fail closed (no provider request)."""


class BudgetDenied(RuntimeError):
    """The request does not fit. retry_after_s is set only for transient limits (rate window, in-flight)."""

    def __init__(self, reason: str, retry_after_s: float | None = None, kind: str = ""):
        super().__init__(reason)
        self.reason, self.retry_after_s, self.kind = reason, retry_after_s, kind


@dataclass(frozen=True)
class BudgetConfig:
    directory: str
    daily_requests: int
    daily_tokens: int
    job_tokens: int
    tokens_per_minute: int
    max_in_flight: int

    @classmethod
    def from_env(cls, env: dict[str, str], directory: str | None = None) -> BudgetConfig:
        d = directory if directory is not None else env.get("PRQA_PROVIDER_BUDGET_DIR", "")
        vals = {}
        for k in REQUIRED_KEYS:
            raw = (env.get(k) or "").strip()
            if not raw.isdigit() or int(raw) < 1:
                raise BudgetError(f"{k} is missing or not a positive integer")
            vals[k] = int(raw)
        cfg = cls(directory=d, daily_requests=vals["PRQA_BUDGET_DAILY_REQUESTS"], daily_tokens=vals["PRQA_BUDGET_DAILY_TOKENS"],
                  job_tokens=vals["PRQA_BUDGET_JOB_TOKENS"], tokens_per_minute=vals["PRQA_BUDGET_TOKENS_PER_MINUTE"],
                  max_in_flight=vals["PRQA_BUDGET_MAX_IN_FLIGHT"])
        cfg.check()
        return cfg

    def check(self) -> None:
        if not self.directory or not os.path.isabs(self.directory):
            raise BudgetError("PRQA_PROVIDER_BUDGET_DIR must be an absolute path")
        try:
            st = os.lstat(self.directory)
        except OSError:
            raise BudgetError("PRQA_PROVIDER_BUDGET_DIR does not exist") from None
        if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise BudgetError("PRQA_PROVIDER_BUDGET_DIR is not a directory")
        if st.st_mode & 0o077:
            raise BudgetError("PRQA_PROVIDER_BUDGET_DIR must not be accessible to group/others (mode 700)")
        if st.st_uid != os.getuid():
            raise BudgetError("PRQA_PROVIDER_BUDGET_DIR must be owned by the process user")
        if not os.access(self.directory, os.R_OK | os.W_OK | os.X_OK):
            raise BudgetError("PRQA_PROVIDER_BUDGET_DIR is not writable")
        for n, v in (("daily_requests", self.daily_requests), ("daily_tokens", self.daily_tokens), ("job_tokens", self.job_tokens),
                     ("tokens_per_minute", self.tokens_per_minute), ("max_in_flight", self.max_in_flight)):
            if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                raise BudgetError(f"budget {n} must be a positive integer")


def estimate_tokens(size: int, units_per_token: float) -> int:
    """Conservative prompt-token estimate from a calibrated size ratio (no local tokenizer is installed)."""
    if not (isinstance(units_per_token, (int, float)) and 1.0 <= units_per_token <= 8.0):
        raise BudgetError("calibration ratio must be within 1..8 units per token")
    return int(math.ceil(max(0, size) / float(units_per_token)))


def reservation_tokens(size: int, units_per_token: float, completion_allowance: int, safety: float = ESTIMATE_SAFETY) -> int:
    """The tokens one request reserves: the prompt estimate with the safety margin plus its full completion allowance."""
    if not (isinstance(safety, (int, float)) and 1.0 <= safety <= 3.0):
        raise BudgetError("estimate safety factor must be within 1..3")
    return int(math.ceil(estimate_tokens(size, units_per_token) * float(safety))) + max(0, int(completion_allowance))


def max_request_tokens(tokens_per_minute: int) -> int:
    """The largest reservation a single request may make under a TPM limit."""
    return int(tokens_per_minute * MAX_REQUEST_SHARE)


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _empty() -> dict[str, Any]:
    return {"version": 1, "days": {}, "jobs": {}, "events": [], "provider": {}}


class Ledger:
    def __init__(self, cfg: BudgetConfig, clock=time.time):
        cfg.check()
        self.cfg, self.clock = cfg, clock
        self.path = os.path.join(cfg.directory, LEDGER_NAME)
        self.lock_path = os.path.join(cfg.directory, LOCK_NAME)

    # ----------------------------------------------------------------- storage --
    def _locked(self):
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _load(self) -> dict[str, Any]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return _empty()
        except OSError as exc:
            raise BudgetError(f"ledger unreadable: {type(exc).__name__}") from None
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except ValueError:
                raise BudgetError("ledger is corrupt; refusing to guess the spent budget") from None
        if not isinstance(data, dict) or data.get("version") != 1 or not all(isinstance(data.get(k), t) for k, t in
                                                                              (("days", dict), ("jobs", dict), ("events", list))):
            raise BudgetError("ledger has an unexpected shape; refusing to guess the spent budget")
        data.setdefault("provider", {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ public --
    def reserve(self, *, source: str, job_id: str, tokens: int, completion_allowance: int) -> str:
        """Count one request before it is sent; returns a reservation id. Raises BudgetDenied / BudgetError."""
        if not isinstance(tokens, int) or tokens < 1:
            raise BudgetError("reservation must be a positive token count")
        fd = self._locked()
        try:
            data, now = self._load(), self.clock()
            day = data["days"].setdefault(_day(now), {"requests": 0, "tokens": 0})
            job = data["jobs"].get(job_id, 0)
            self._check(data, now, tokens=tokens, job_id=job_id)
            rid = uuid.uuid4().hex
            data["events"].append({"id": rid, "ts": now, "source": source[:40], "job_id": job_id[:128], "tokens": tokens,
                                   "reserved": tokens, "completion_allowance": completion_allowance, "state": "in_flight"})
            day["requests"] += 1; day["tokens"] += tokens
            data["jobs"][job_id] = job + tokens
            self._prune(data, now)
            self._save(data)
            return rid
        finally:
            os.close(fd)

    def _check(self, data: dict[str, Any], now: float, *, tokens: int, job_id: str) -> None:
        """Every limit a reservation must satisfy (shared by reserve and capacity). Raises BudgetDenied with its kind."""
        day = data["days"].get(_day(now), {"requests": 0, "tokens": 0})
        job = data["jobs"].get(job_id, 0)
        if day["requests"] + 1 > self.cfg.daily_requests:
            raise BudgetDenied(f"daily request budget exhausted ({day['requests']}/{self.cfg.daily_requests})", kind="daily_requests")
        if day["tokens"] + tokens > self.cfg.daily_tokens:
            raise BudgetDenied(f"daily token budget would be exceeded ({day['tokens']} + {tokens} > {self.cfg.daily_tokens})", kind="daily_tokens")
        if job + tokens > self.cfg.job_tokens:
            raise BudgetDenied(f"per-job token budget would be exceeded ({job} + {tokens} > {self.cfg.job_tokens})", kind="job_tokens")
        if tokens > max_request_tokens(self.cfg.tokens_per_minute):
            raise BudgetDenied(f"request reservation {tokens} exceeds the per-request limit {max_request_tokens(self.cfg.tokens_per_minute)} "
                               f"({MAX_REQUEST_SHARE:.0%} of the tokens-per-minute limit {self.cfg.tokens_per_minute})", kind="request_too_large")
        live = [e for e in data["events"] if e.get("state") == "in_flight" and now - e["ts"] < STALE_IN_FLIGHT_S]
        if len(live) >= self.cfg.max_in_flight:
            raise BudgetDenied("another provider request is in flight", retry_after_s=2.0, kind="in_flight")
        window = [e for e in data["events"] if now - e["ts"] < WINDOW_S]
        used = sum(int(e.get("tokens", 0)) for e in window)
        if used + tokens > self.cfg.tokens_per_minute:
            oldest = min(e["ts"] for e in window)
            raise BudgetDenied(f"tokens-per-minute window full ({used} + {tokens} > {self.cfg.tokens_per_minute})",
                               retry_after_s=max(0.5, oldest + WINDOW_S - now + 0.5), kind="tpm_window")
        prov = data.get("provider") or {}
        if isinstance(prov.get("remaining_tokens"), int) and isinstance(prov.get("reset_at"), (int, float)) \
                and prov["remaining_tokens"] < tokens and now < prov["reset_at"]:
            raise BudgetDenied("provider-reported remaining tokens are below the reservation",
                               retry_after_s=max(0.5, prov["reset_at"] - now + 0.5), kind="provider_remaining")

    def capacity(self, *, job_id: str, tokens: int) -> dict[str, Any]:
        """Read-only pre-check for a planned request (nothing is reserved or written): whether a reservation of `tokens`
        would be accepted now, else the limit kind, the reason and — for transient limits only — how long to wait.
        Transient: the rolling tokens-per-minute window, the provider-reported remaining tokens/reset, in flight.
        Hard (retry_after_s None): daily requests/tokens, per-job tokens, a single request larger than the TPM limit."""
        if not isinstance(tokens, int) or tokens < 1:
            raise BudgetError("capacity check needs a positive token count")
        fd = self._locked()
        try:
            data, now = self._load(), self.clock()
        finally:
            os.close(fd)
        window = [e for e in data["events"] if now - e["ts"] < WINDOW_S]
        prov = data.get("provider") or {}
        observed = {"window_tokens": sum(int(e.get("tokens", 0)) for e in window), "tokens_per_minute": self.cfg.tokens_per_minute,
                    "provider_remaining_tokens": prov.get("remaining_tokens") if isinstance(prov.get("remaining_tokens"), int) else None,
                    "provider_reset_in_s": (round(max(0.0, prov["reset_at"] - now), 2) if isinstance(prov.get("reset_at"), (int, float)) else None)}
        try:
            self._check(data, now, tokens=tokens, job_id=job_id)
        except BudgetDenied as exc:
            return {"fits": False, "kind": exc.kind, "reason": exc.reason, "retry_after_s": exc.retry_after_s,
                    "hard": exc.retry_after_s is None, **observed}
        return {"fits": True, "kind": None, "reason": None, "retry_after_s": 0.0, "hard": False, **observed}

    def settle(self, rid: str, *, total_tokens: int | None, outcome: str,
               remaining_tokens: int | None = None, reset_s: float | None = None) -> None:
        """Replace a reservation by the provider-reported usage (or keep it when usage is missing)."""
        fd = self._locked()
        try:
            data, now = self._load(), self.clock()
            ev = next((e for e in data["events"] if e.get("id") == rid), None)
            if ev is None:
                raise BudgetError("unknown reservation")
            if isinstance(total_tokens, int) and not isinstance(total_tokens, bool) and total_tokens >= 0:
                delta = total_tokens - int(ev["tokens"])
                ev["tokens"] = total_tokens; ev["actual"] = total_tokens
                if total_tokens > int(ev.get("reserved", 0)):
                    ev["overrun"] = total_tokens - int(ev["reserved"])   # kept for calibration; the window already uses the actual
                day = data["days"].setdefault(_day(ev["ts"]), {"requests": 0, "tokens": 0})
                day["tokens"] = max(0, day["tokens"] + delta)
                data["jobs"][ev["job_id"]] = max(0, data["jobs"].get(ev["job_id"], 0) + delta)
            ev["state"], ev["outcome"] = "done", outcome[:40]
            if isinstance(remaining_tokens, int) and isinstance(reset_s, (int, float)) and reset_s >= 0:
                data["provider"] = {"remaining_tokens": remaining_tokens, "reset_at": now + float(reset_s), "observed_at": now}
            self._save(data)
        finally:
            os.close(fd)

    def snapshot(self) -> dict[str, Any]:
        fd = self._locked()
        try:
            data, now = self._load(), self.clock()
        finally:
            os.close(fd)
        day = data["days"].get(_day(now), {"requests": 0, "tokens": 0})
        return {"day": _day(now), "requests_today": day["requests"], "tokens_today": day["tokens"],
                "limits": {"daily_requests": self.cfg.daily_requests, "daily_tokens": self.cfg.daily_tokens,
                           "job_tokens": self.cfg.job_tokens, "tokens_per_minute": self.cfg.tokens_per_minute,
                           "max_in_flight": self.cfg.max_in_flight},
                "jobs": dict(data["jobs"]), "in_flight": sum(1 for e in data["events"] if e.get("state") == "in_flight")}

    @staticmethod
    def _prune(data: dict[str, Any], now: float) -> None:
        keep_from = now - 3 * 86400
        data["events"] = [e for e in data["events"] if e["ts"] >= keep_from]
        cutoff = _day(keep_from)
        data["days"] = {d: v for d, v in data["days"].items() if d >= cutoff}


def parse_reset_seconds(value: str | None) -> float | None:
    """Groq-style reset durations ("7.66s", "2m59.56s", "450ms") → seconds; None when absent or unparseable."""
    if not value:
        return None
    import re
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?", value.strip())
    if not m or not any(m.groups()):
        return None
    h, mi, s, ms = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0) + float(ms or 0) / 1000.0
