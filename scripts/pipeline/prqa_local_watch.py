#!/usr/bin/env python3
"""Live, read-only view of a local agent run (OpenCode or Claude Code) (opened in a terminal window by scripts/pipeline/prqa_local_opencode.py).

Follows <run>/local/status.json and the run's evidence directory: each agent session (scenario group), the bash
commands it runs with a short excerpt of their output, its messages, and every scenario record as it is written.
Tokens and synthetic passwords are masked. Closing the window does not affect the run.

    python3 scripts/pipeline/prqa_local_watch.py --run-dir runs/prqa-local-pr-runs/<run>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

MASK = [(re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"), "<token>"), (re.compile(r"Prqa-[\w-]{16,}"), "<password>"),
        (re.compile(r"(?i)(bearer\s+)[\w.~+/=-]{12,}"), r"\1<token>")]
C = {"dim": "\033[2m", "b": "\033[1m", "g": "\033[32m", "r": "\033[31m", "y": "\033[33m", "c": "\033[36m", "x": "\033[0m"}
STATUS_COLOR = {"PASS": "g", "FAIL": "r", "BLOCKED": "y", "NOT_APPLICABLE": "dim"}


def mask(text: str) -> str:
    for rx, rep in MASK:
        text = rx.sub(rep, text)
    return text


def say(text: str, color: str = "") -> None:
    print(f"{C.get(color, '')}{mask(text)}{C['x'] if color else ''}", flush=True)


def short(text: Any, n: int = 300) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n] + " …"


def claude_lines(ev: dict[str, Any]) -> list[tuple[str, str]]:
    """Claude Code `--output-format stream-json`: assistant text / tool_use, user tool_result, final result."""
    kind = ev.get("type")
    if kind == "result":
        return [(f"  ! {short(ev.get('result'), 300)}", "r")] if ev.get("is_error") else []
    out: list[tuple[str, str]] = []
    for item in (ev.get("message") or {}).get("content") or []:
        if not isinstance(item, dict):
            continue
        if kind == "assistant" and item.get("type") == "text" and item.get("text"):
            out.append((f"  ▸ {short(item['text'], 500)}", ""))
        elif kind == "assistant" and item.get("type") == "tool_use":
            inp = item.get("input") or {}
            out.append((f"  $ {short(inp.get('command') or inp, 400)}", "c"))
        elif kind == "user" and item.get("type") == "tool_result":
            content = item.get("content")
            if isinstance(content, list):
                content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
            if content:
                out.append((f"    {short(content, 240)}", "r" if item.get("is_error") else "dim"))
    return out


def event_lines(ev: dict[str, Any]) -> list[tuple[str, str]]:
    if ev.get("type") in ("assistant", "user", "result", "system"):
        return claude_lines(ev)
    part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
    kind = ev.get("type") or part.get("type")
    if kind in ("tool_use", "tool"):
        st = part.get("state") or {}
        inp = st.get("input") or {}
        out = [(f"  $ {short(inp.get('command') or inp, 400)}", "c")]
        if st.get("output"):
            out.append((f"    {short(st.get('output'), 240)}", "dim"))
        return out
    if kind == "text" and part.get("text"):
        return [(f"  ▸ {short(part['text'], 500)}", "")]
    if kind == "error" or ev.get("error"):
        err = ev.get("error") or part.get("error") or ev
        return [(f"  ! {short(err if isinstance(err, str) else (err.get('message') if isinstance(err, dict) else err), 300)}", "r")]
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", type=Path, required=True)
    a = ap.parse_args(argv)
    status_file = a.run_dir / "local" / "status.json"
    offsets: dict[Path, int] = {}
    seen_records: set[str] = set()
    header = False
    while True:
        try:
            st = json.loads(status_file.read_text())
        except (OSError, ValueError):
            st = {}
        if not header and st.get("evidence"):
            name = "Claude Code" if st.get("engine") == "claude" else "OpenCode"
            say(f"PRQA · PR #{st.get('pr')} @ {str(st.get('sha'))[:12]} · {st.get('model')} · {name} on this machine (bubblewrap)", "b")
            say("read-only view — closing this window does not stop the run\n", "dim")
            header = True
        ev_dir = Path(st["evidence"]) if st.get("evidence") else None
        if ev_dir and ev_dir.is_dir():
            for f in sorted(ev_dir.glob("*-events-*.jsonl")):
                pos = offsets.get(f)
                if pos is None:
                    say(f"\n── session {f.stem.split('-events-', 1)[1]} ──", "b")
                    pos = 0
                with open(f, encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    for line in fh:
                        if not line.endswith("\n"):
                            break
                        pos += len(line.encode())
                        try:
                            for text, color in event_lines(json.loads(line)):
                                say(text, color)
                        except ValueError:
                            continue
                offsets[f] = pos
            for rec in sorted((ev_dir / "tc").glob("*.json")) if (ev_dir / "tc").is_dir() else []:
                if rec.name in seen_records:
                    continue
                try:
                    r = json.loads(rec.read_text())
                except (OSError, ValueError):
                    continue
                seen_records.add(rec.name)
                s = str(r.get("status") or "?")
                say(f"  ● {r.get('id') or rec.stem}: {s} — {short(r.get('actual') or r.get('scenario'), 160)}", STATUS_COLOR.get(s, ""))
        if st.get("state") in ("finished", "failed"):
            color = "g" if st.get("job_status") == "succeeded" else "r"
            say(f"\nrun {st['state']}: job {st.get('job_status') or '-'}" + (f" — {st.get('error')}" if st.get("error") else ""), color)
            say(f"report: {a.run_dir / 'report.md'} (written by n8n's Report stage)", "dim")
            try:
                input("\npress Enter to close ")
            except EOFError:
                pass
            return 0
        time.sleep(1.5)


if __name__ == "__main__":
    sys.exit(main())
