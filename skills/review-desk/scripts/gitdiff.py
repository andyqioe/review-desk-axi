"""Fallback review data straight from git, for sessions not opened from implementation-summary.

implementation-summary's `build_page.py --review-data` writes a richer manifest (origins,
rename pairing, hunk symbols) from its change model; this produces the same shape from
`git diff <base>` plus untracked files, so the desk works on any repository.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from store import write_json

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
STATUS_NAME = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "N": "untracked"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def parse(patch: str) -> tuple[list[dict], list[list[str]]]:
    hunks, rows = [], []
    o = n = 0
    for line in patch.splitlines():
        m = HUNK_RE.match(line)
        if m:
            os_, ol, ns, nl, hdr = int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1), m[5].strip()
            hunks.append({"n": len(hunks) + 1, "old_start": os_, "old_len": ol, "new_start": ns, "new_len": nl, "symbol": hdr})
            rows.append(["", "", f"#{len(hunks)}  @@ -{os_},{ol} +{ns},{nl} @@{('  in ' + hdr) if hdr else ''}", "hunk"])
            o, n = os_, ns
        elif not hunks:
            continue
        elif line.startswith("+"):
            rows.append(["", str(n), line[1:], "add"]); n += 1
        elif line.startswith("-"):
            rows.append([str(o), "", line[1:], "del"]); o += 1
        elif line.startswith(" "):
            rows.append([str(o), str(n), line[1:], ""]); o += 1; n += 1
    return hunks, rows


def untracked_lines(path: Path) -> tuple[list[str], list[str], bool]:
    """An untracked path as git would add it: a symlink is its target (even one to a directory),
    a binary file has no lines. Returns (lines, notes, binary)."""
    if path.is_symlink():
        target = os.readlink(path)
        return [target], [f"symlink -> {target}"], False
    data = path.read_bytes()
    if b"\0" in data[:8000]:  # git's own binary heuristic
        return [], [f"binary, {len(data):,} B"], True
    return data.decode("utf-8", errors="replace").splitlines(), [], False


def write(repo: Path, out: Path, base: str = "HEAD", paths: list[str] | None = None) -> dict:
    repo = Path(git(repo, "rev-parse", "--show-toplevel").strip())
    scope = ["--", *paths] if paths else []
    files = []
    (out / "diffs").mkdir(parents=True, exist_ok=True)
    for line in git(repo, "diff", "--name-status", "-M", base, *scope).splitlines():
        parts = line.split("\t")
        st = parts[0][0]
        path, old = (parts[2], parts[1]) if st in "RC" else (parts[1], None)
        files.append((st, path, old))
    for path in git(repo, "ls-files", "--others", "--exclude-standard", *scope).splitlines():
        files.append(("N", path, None))
    for stale in (out / "diffs").glob("*.json"):
        stale.unlink()
    manifest = {"repo": str(repo), "link_root": str(repo), "base_label": base, "source": "git", "base": base,
                "paths": paths or [], "files": []}
    for i, (st, path, old) in enumerate(files, 1):
        notes, binary = [], False
        if st == "N":
            text, notes, binary = untracked_lines(repo / path)
            hunks = [{"n": 1, "old_start": 0, "old_len": 0, "new_start": 1, "new_len": len(text), "symbol": ""}] if text else []
            rows = [["", "", f"#1  @@ -0,0 +1,{len(text)} @@", "hunk"]] + [["", str(k), t, "add"] for k, t in enumerate(text, 1)] if text else []
        else:
            hunks, rows = parse(git(repo, "diff", "-M", "-U3", base, "--", *([old] if old else []), path))
        adds = sum(r[3] == "add" for r in rows)
        dels = sum(r[3] == "del" for r in rows)
        manifest["files"].append({"n": i, "path": path, "old_path": old, "status": STATUS_NAME.get(st, st),
                                  "badge": st, "origins": [], "adds": adds, "dels": dels, "notes": notes,
                                  "binary": binary, "hunks": hunks, "cited": False})
        (out / "diffs" / f"{i}.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
    write_json(out / "manifest.json", manifest)
    return manifest
