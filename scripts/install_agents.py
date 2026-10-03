#!/usr/bin/env python3
"""Generate the reviewer agent definitions from agents/reviewer.md.tmpl.

  install_agents.py [--dest ~/.claude/agents] [--check]

Writes review-desk-{low,medium,high}.md, one per effort, so the three never drift apart.
The model is picked at spawn time with the Agent tool's `model` parameter (`model: inherit`
here). --check exits 1 if an installed file differs from what the template generates.
Claude Code reads agent definitions at session start, so a fresh install is usable from the
next session.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from store import EFFORTS  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent.parent / "agents" / "reviewer.md.tmpl"


def render(effort: str) -> str:
    return TEMPLATE.read_text(encoding="utf-8").replace("{{EFFORT}}", effort)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", default=str(Path.home() / ".claude" / "agents"))
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    dest = Path(a.dest).expanduser()
    stale = []
    for effort in EFFORTS:
        path = dest / f"review-desk-{effort}.md"
        text = render(effort)
        if a.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(path)
            continue
        dest.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    if a.check:
        print("\n".join(f"stale: {p}" for p in stale) or "agents up to date")
        return 1 if stale else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
