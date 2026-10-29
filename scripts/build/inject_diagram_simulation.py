#!/usr/bin/env python3
"""Add the workflow simulation (docs/diagrams/simulation-overlay.html) to the rendered architecture diagram.

Run after re-rendering docs/diagrams/system-architecture.html. Safe to re-run: an earlier copy of the overlay is
replaced, never duplicated.

    python3 scripts/build/inject_diagram_simulation.py [--html docs/diagrams/system-architecture.html]
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "docs" / "diagrams" / "simulation-overlay.html"
START, END = "<!-- prqa-simulation:start -->", "<!-- prqa-simulation:end -->"


def inject(html: str, overlay: str) -> str:
    html = re.sub(re.escape(START) + r".*?" + re.escape(END) + r"\n?", "", html, flags=re.S)
    if "</body>" not in html:
        raise SystemExit("no </body> in the diagram HTML")
    block = f"{START}\n{overlay.strip()}\n{END}\n"
    i = html.rindex("</body>")
    return html[:i] + block + html[i:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--html", type=Path, default=ROOT / "docs" / "diagrams" / "system-architecture.html")
    a = ap.parse_args()
    a.html.write_text(inject(a.html.read_text(encoding="utf-8"), OVERLAY.read_text(encoding="utf-8")), encoding="utf-8")
    print(f"simulation added to {a.html.relative_to(ROOT) if a.html.is_relative_to(ROOT) else a.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
