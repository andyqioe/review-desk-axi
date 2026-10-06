#!/usr/bin/env python3
"""A stand-in for the GitHub CLI, for the pull request tests.

It serves the PR fixtures in $FAKE_GH_DIR (pr-<n>.json, comments-<n>.json), renders Markdown the way
GitHub's API does (paragraphs as <p dir="auto">), clones through git (the test's url.insteadOf points
https://github.com/ at local bare repositories) and records each posted review in reviews-<n>.jsonl.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path

D = Path(os.environ["FAKE_GH_DIR"])


def opt(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def main(args: list[str]) -> int:
    with (D / "calls.log").open("a") as f:
        f.write(" ".join(args) + "\n")
    if args[:2] == ["pr", "view"]:
        n = args[2]
        pr = json.loads((D / f"pr-{n}.json").read_text())
        fields = (opt(args, "--json") or "").split(",")
        print(json.dumps({k: v for k, v in pr.items() if k in fields}))
        return 0
    if args[:2] == ["repo", "clone"]:
        rest = args[args.index("--") + 1:] if "--" in args else []
        return subprocess.run(["git", "clone", "-q", f"https://github.com/{args[2]}.git", args[3], *rest]).returncode
    if args[0] == "api":
        path = next(a for a in args[1:] if not a.startswith("-") and args[args.index(a) - 1] not in ("--hostname", "--jq", "-X", "--input", "-f"))
        if m := re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)/comments", path):
            for c in json.loads((D / f"comments-{m[1]}.json").read_text()) if (D / f"comments-{m[1]}.json").exists() else []:
                print(json.dumps(c))
            return 0
        if path == "markdown":
            text = next(a[5:] for i, a in enumerate(args) if a.startswith("text=") and args[i - 1] == "-f")
            print("\n".join(f'<p dir="auto">{html.escape(p.strip())}</p>' for p in re.split(r"\n\s*\n", text) if p.strip()))
            return 0
        if m := re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)/reviews", path):
            payload = json.loads(sys.stdin.read())
            with (D / f"reviews-{m[1]}.jsonl").open("a") as f:
                f.write(json.dumps(payload) + "\n")
            n = sum(1 for _ in (D / f"reviews-{m[1]}.jsonl").open())
            print(json.dumps({"id": n, "html_url": f"https://github.com/acme/widgets/pull/{m[1]}#pullrequestreview-{n}"}))
            return 0
    print(f"fake gh: unsupported {args}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
