#!/usr/bin/env python3
"""Generate docs/assets/prqa-flow.svg: the animated pipeline shown at the top of README.md.

Pure SVG + CSS/SMIL (no JavaScript), so it animates inside GitHub's README image renderer. One loop shows a run
travelling through the eight main stages; the log line under the stages follows the packet. Values are from a
recorded run (one pull request, Claude Code).

    python3 scripts/build/build_readme_flow.py
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "assets" / "prqa-flow.svg"

T = 20.0                       # loop length, seconds
FIRST, STEP, DWELL = 0.4, 2.1, 1.5
W, H = 1000, 470
CARD_W, CARD_H = 200, 104
XS = [40, 280, 520, 760]
ROW_Y = [92, 252]

STAGES = [
    ("Label", "to be test on a PR", "gh · PR #2 labelled ‘to be test’ · head 292ad89"),
    ("Discover", "n8n checks every minute", "n8n → POST /stage/discover · eligible: [#2]"),
    ("Claim", "once per PR head SHA", "claimed OWNER/REPO#2@292ad89 · run state locked"),
    ("Prepare", "disposable API + Postgres", "disposable env up · OpenAPI loaded · identity probe ok"),
    ("Prompt", "Groq · 3 staged calls", "Groq: scope ✓  coverage ✓  sections ✓ · within 8k TPM"),
    ("Gate", "rubric ≥ 85 · hard fails", "quality gate 98 / 85 → ACCEPTED · hard fails: none"),
    ("Execute", "Claude Code · sandboxed", "claude -p · 30 scenarios in 11 sessions · curl only"),
    ("Report", "report.md + Discord", "report.md written · environment destroyed · Discord delivered"),
]


def card_xy(i: int) -> tuple[int, int]:
    """Snake layout: stages 1-4 left to right, 5-8 right to left."""
    row = i // 4
    col = i % 4 if row == 0 else 3 - i % 4
    return XS[col], ROW_Y[row]


def pct(t: float) -> str:
    return f"{max(0.0, min(100.0, t / T * 100)):.2f}%"


def arrive(i: int) -> float:
    return FIRST + i * STEP


def leave(i: int) -> float:
    return arrive(i) + DWELL


def path_and_points() -> tuple[str, list[float]]:
    cx = [card_xy(i)[0] + CARD_W / 2 for i in range(8)]
    cy = [card_xy(i)[1] + CARD_H / 2 for i in range(8)]
    d = f"M{cx[0]:.0f},{cy[0]:.0f} H{cx[3]:.0f} V{cy[4]:.0f} H{cx[7]:.0f}"
    dist, pts = 0.0, [0.0]
    for i in range(1, 8):
        dist += abs(cx[i] - cx[i - 1]) + abs(cy[i] - cy[i - 1])
        pts.append(dist)
    return d, [p / dist for p in pts]


def motion(fracs: list[float]) -> tuple[str, str]:
    times, points = [0.0], [0.0]
    for i, f in enumerate(fracs):
        times += [arrive(i) / T, leave(i) / T]
        points += [f, f]
    times.append(1.0)
    points.append(1.0)
    return ";".join(f"{t:.4f}" for t in times), ";".join(f"{p:.4f}" for p in points)


def css() -> str:
    out = []
    end_done = 97.0
    for i in range(8):
        a, lv = arrive(i), leave(i)
        out.append(f"""
  @keyframes f{i} {{ 0%, {pct(a - .15)} {{ stroke: var(--line); fill: var(--card); filter: none; }}
    {pct(a + .1)}, {pct(lv)} {{ stroke: var(--active); fill: var(--card-on); filter: url(#glow); }}
    {pct(lv + .25)}, {end_done}% {{ stroke: var(--done-line); fill: var(--card); filter: none; }}
    99.5%, 100% {{ stroke: var(--line); fill: var(--card); filter: none; }} }}
  @keyframes n{i} {{ 0%, {pct(a - .15)} {{ fill: var(--muted); }} {pct(a + .1)}, {pct(lv)} {{ fill: var(--active); }}
    {pct(lv + .25)}, {end_done}% {{ fill: var(--done); }} 99.5%, 100% {{ fill: var(--muted); }} }}
  @keyframes w{i} {{ 0%, {pct(a - .15)} {{ opacity: 1; }} {pct(a)}, 99.4% {{ opacity: 0; }} 99.5%, 100% {{ opacity: 1; }} }}
  @keyframes r{i} {{ 0%, {pct(a - .1)} {{ opacity: 0; }} {pct(a + .1)}, {pct(lv)} {{ opacity: 1; }} {pct(lv + .2)}, 100% {{ opacity: 0; }} }}
  @keyframes d{i} {{ 0%, {pct(lv)} {{ opacity: 0; }} {pct(lv + .25)}, {end_done}% {{ opacity: 1; }} 99.5%, 100% {{ opacity: 0; }} }}
  @keyframes l{i} {{ 0%, {pct(a - .05)} {{ opacity: 0; transform: translateY(6px); }} {pct(a + .25)}, {pct(lv + .45)} {{ opacity: 1; transform: none; }}
    {pct(lv + .6)}, 100% {{ opacity: 0; transform: translateY(-6px); }} }}
  .s{i} .frame {{ animation: f{i} {T}s linear infinite; }}
  .s{i} .num {{ animation: n{i} {T}s linear infinite; }}
  .s{i} .st-wait {{ animation: w{i} {T}s linear infinite; }}
  .s{i} .st-run {{ animation: r{i} {T}s linear infinite; }}
  .s{i} .st-done {{ animation: d{i} {T}s linear infinite; }}
  .log{i} {{ animation: l{i} {T}s ease-out infinite; opacity: 0; }}""")
    s0, s1 = leave(7) + .3, T - .5
    out.append(f"""
  @keyframes sum {{ 0%, {pct(s0)} {{ opacity: 0; transform: translateY(6px); }} {pct(s0 + .4)}, {pct(s1)} {{ opacity: 1; transform: none; }}
    {pct(s1 + .3)}, 100% {{ opacity: 0; }} }}
  .summary {{ animation: sum {T}s ease-out infinite; opacity: 0; }}""")
    return "".join(out)


def build() -> str:
    d, fracs = path_and_points()
    key_times, key_points = motion(fracs)
    trail_vals = ";".join(f"{1 - float(p):.4f}" for p in key_points.split(";"))
    cards = []
    for i, (title, sub, _) in enumerate(STAGES):
        x, y = card_xy(i)
        cards.append(f"""
  <g class="s{i}">
    <rect class="frame" x="{x}" y="{y}" width="{CARD_W}" height="{CARD_H}" rx="14" stroke-width="1.5"/>
    <text class="num" x="{x + 18}" y="{y + 28}">STAGE {i + 1:02d}</text>
    <text class="title" x="{x + 18}" y="{y + 56}">{title}</text>
    <text class="sub" x="{x + 18}" y="{y + 78}">{sub}</text>
    <g class="state" transform="translate({x + CARD_W - 18} {y + 28})">
      <text class="st st-wait" text-anchor="end">waiting</text>
      <text class="st st-run" text-anchor="end">● running</text>
      <text class="st st-done" text-anchor="end">✓ done</text>
    </g>
  </g>""")
    logs = "".join(f'\n    <text class="logline log{i}" x="110" y="427"><tspan class="prompt">❯</tspan> {line}</text>'
                   for i, (_, _, line) in enumerate(STAGES))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-labelledby="t d">
  <title id="t">PRQA pipeline</title>
  <desc id="d">Animated: a pull request labelled "to be test" moves through Label, Discover, Claim, Prepare, Prompt, Gate, Execute and Report.</desc>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0b1220"/><stop offset="1" stop-color="#111b30"/></linearGradient>
    <linearGradient id="trail" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#22d3ee"/><stop offset="1" stop-color="#a78bfa"/></linearGradient>
    <radialGradient id="pk"><stop offset="0" stop-color="#ffffff"/><stop offset=".45" stop-color="#67e8f9"/><stop offset="1" stop-color="#22d3ee" stop-opacity="0"/></radialGradient>
    <pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" fill="#1f2a44"/></pattern>
    <filter id="glow" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="6" result="b"/>
      <feFlood flood-color="#22d3ee" flood-opacity=".55"/><feComposite in2="b" operator="in"/><feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge></filter>
    <filter id="soft"><feGaussianBlur stdDeviation="3"/></filter>
  </defs>
  <style>
  :root {{ --card: #121a2b; --card-on: #13253a; --line: #26324d; --done-line: #2f6f55; --active: #22d3ee; --done: #34d399;
          --text: #e6edf3; --muted: #7d8aa5; }}
  text {{ font-family: ui-sans-serif, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
  .brand {{ fill: var(--text); font-size: 15px; font-weight: 700; letter-spacing: .02em; }}
  .brand-sub {{ fill: var(--muted); font-size: 12px; }}
  .live {{ fill: #f43f5e; font-size: 11px; font-weight: 700; letter-spacing: .14em; }}
  .num {{ font: 700 10.5px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; letter-spacing: .14em; fill: var(--muted); }}
  .title {{ fill: var(--text); font-size: 20px; font-weight: 700; }}
  .sub {{ fill: var(--muted); font-size: 12.5px; }}
  .st {{ font: 600 10.5px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  .st-wait {{ fill: var(--muted); }} .st-run {{ fill: var(--active); opacity: 0; }} .st-done {{ fill: var(--done); opacity: 0; }}
  .rail {{ fill: none; stroke: #1e2a44; stroke-width: 3; stroke-dasharray: 2 7; stroke-linecap: round; }}
  .progress {{ fill: none; stroke: url(#trail); stroke-width: 3; stroke-linecap: round; }}
  .term {{ fill: #0a0f1a; stroke: #1f2a44; }}
  .logline {{ font: 13px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; fill: #c9d4e5; }}
  .prompt {{ fill: var(--active); font-weight: 700; }}
  .summary text {{ font: 700 13px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  @keyframes blink {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: .25; }} }}
  .pulse {{ animation: blink 1.4s ease-in-out infinite; }}
  {css()}
  @media (prefers-reduced-motion: reduce) {{ * {{ animation: none !important; }} .st-done, .log7, .summary {{ opacity: 1 !important; }} .st-wait, .log0 {{ opacity: 0; }} }}
  </style>
  <rect width="{W}" height="{H}" rx="18" fill="url(#bg)"/>
  <rect width="{W}" height="{H}" rx="18" fill="url(#dots)" opacity=".7"/>
  <text class="brand" x="40" y="50">PRQA</text>
  <text class="brand-sub" x="92" y="50">label → tested → reported · one pull request, end to end</text>
  <circle class="pulse" cx="905" cy="45" r="4.5" fill="#f43f5e"/>
  <text class="live" x="918" y="49">LIVE</text>

  <path class="rail" d="{d}"/>
  <path class="progress" d="{d}" pathLength="1" stroke-dasharray="1 1">
    <animate attributeName="stroke-dashoffset" dur="{T}s" repeatCount="indefinite" calcMode="linear" keyTimes="{key_times}" values="{trail_vals}"/>
    <animate attributeName="opacity" dur="{T}s" repeatCount="indefinite" keyTimes="0;0.97;0.995;1" values="1;1;0;0"/>
  </path>
  <g>
    <circle r="16" fill="url(#pk)" opacity=".55" filter="url(#soft)">
      <animateMotion dur="{T}s" repeatCount="indefinite" calcMode="linear" keyTimes="{key_times}" keyPoints="{key_points}" path="{d}"/>
    </circle>
    <circle r="6" fill="#ecfeff">
      <animateMotion dur="{T}s" repeatCount="indefinite" calcMode="linear" keyTimes="{key_times}" keyPoints="{key_points}" path="{d}"/>
    </circle>
  </g>
  {"".join(cards)}

  <rect class="term" x="40" y="398" width="920" height="48" rx="11"/>
  <circle cx="62" cy="422" r="4" fill="#f43f5e"/><circle cx="76" cy="422" r="4" fill="#f59e0b"/><circle cx="90" cy="422" r="4" fill="#10b981"/>
  <g>{logs}
  </g>
  <g class="summary">
    <text x="110" y="427"><tspan fill="#34d399">✓ run executed</tspan><tspan fill="#c9d4e5"> · 30 scenarios · </tspan><tspan fill="#34d399">14 pass</tspan><tspan fill="#c9d4e5"> · </tspan><tspan fill="#fb7185">11 fail</tspan><tspan fill="#c9d4e5"> · 5 n/a · verdict posted to Discord</tspan></text>
  </g>
</svg>
"""


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build(), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
