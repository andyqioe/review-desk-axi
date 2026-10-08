"""Review data straight from git: the fallback change set, and one change set per commit.

implementation-summary's `build_page.py --review-data` writes a richer manifest (origins,
rename pairing, hunk symbols) from its change model; `write` produces the same shape from
`git diff <base>` plus untracked files, so the desk works on any repository.

`sync_commits` splits the review into the commits it is made of, for the desk's commit picker:

  <session dir>/commits/index.json         the commits in <base>..HEAD, oldest first, and the uncommitted rest
  <session dir>/commits/<sha>/             manifest.json + diffs/<n>.json for one commit against its parent
  <session dir>/commits/uncommitted/       the working tree (untracked files included) against HEAD

A commit never changes, so its folder is written once and reused: a reload only adds the
commits made since, which is what lets the desk append them instead of folding them into one diff.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from store import read_json, write_json

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
STATUS_NAME = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "N": "untracked"}
UNCOMMITTED = "uncommitted"
MAX_COMMITS = 100  # the newest ones; older commits stay visible only in "All changes"
COMMIT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def rev(repo: Path, ref: str) -> str | None:
    """The commit `ref` names, or None (no such ref, or a repository with no commits yet)."""
    p = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                       capture_output=True, text=True, check=False)
    return p.stdout.strip() or None if p.returncode == 0 else None


def empty_tree(repo: Path) -> str:
    """What a root commit is diffed against (its hash depends on the repository's object format)."""
    return subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "tree", "--stdin"], input="",
                          capture_output=True, text=True, check=True).stdout.strip()


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


def write(repo: Path, out: Path, base: str = "HEAD", paths: list[str] | None = None, label: str | None = None,
          head: str | None = None) -> dict:
    """manifest.json + diffs/<n>.json in `out`: `base` against the working tree and its untracked files,
    or, with `head`, against that commit alone."""
    repo = Path(git(repo, "rev-parse", "--show-toplevel").strip())
    scope = ["--", *paths] if paths else []
    sides = [base, head] if head else [base]
    files = []
    (out / "diffs").mkdir(parents=True, exist_ok=True)
    for line in git(repo, "diff", "--name-status", "-M", *sides, *scope).splitlines():
        parts = line.split("\t")
        st = parts[0][0]
        path, old = (parts[2], parts[1]) if st in "RC" else (parts[1], None)
        files.append((st, path, old))
    if not head:
        for path in git(repo, "ls-files", "--others", "--exclude-standard", *scope).splitlines():
            files.append(("N", path, None))
    for stale in (out / "diffs").glob("*.json"):
        stale.unlink()
    manifest = {"repo": str(repo), "link_root": str(repo), "base_label": label or base, "source": "git", "base": base,
                "paths": paths or [], "files": []}
    if head:
        manifest["head"] = head
    for i, (st, path, old) in enumerate(files, 1):
        notes, binary = [], False
        if st == "N":
            text, notes, binary = untracked_lines(repo / path)
            hunks = [{"n": 1, "old_start": 0, "old_len": 0, "new_start": 1, "new_len": len(text), "symbol": ""}] if text else []
            rows = [["", "", f"#1  @@ -0,0 +1,{len(text)} @@", "hunk"]] + [["", str(k), t, "add"] for k, t in enumerate(text, 1)] if text else []
        else:
            hunks, rows = parse(git(repo, "diff", "-M", "-U3", *sides, "--", *([old] if old else []), path))
        adds = sum(r[3] == "add" for r in rows)
        dels = sum(r[3] == "del" for r in rows)
        manifest["files"].append({"n": i, "path": path, "old_path": old, "status": STATUS_NAME.get(st, st),
                                  "badge": st, "origins": [], "adds": adds, "dels": dels, "notes": notes,
                                  "binary": binary, "hunks": hunks, "cited": False})
        (out / "diffs" / f"{i}.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
    write_json(out / "manifest.json", manifest)
    return manifest


def totals(manifest: dict) -> dict:
    files = manifest.get("files", [])
    return {"files": len(files), "adds": sum(f.get("adds", 0) for f in files), "dels": sum(f.get("dels", 0) for f in files)}


def sync_commits(repo: Path, out: Path, base: str | None, paths: list[str] | None = None) -> dict:
    """Write commits/: one change set per commit in base..HEAD (only the ones not written yet) and the
    uncommitted rest, then commits/index.json listing them. Returns the index."""
    root = out / "commits"
    root.mkdir(parents=True, exist_ok=True)
    repo = Path(git(repo, "rev-parse", "--show-toplevel").strip())
    head = rev(repo, "HEAD")
    index = {"base": base, "head": head, "entries": [], "uncommitted": None, "merges": 0, "omitted": 0}
    if base and head:
        scope = ["--", *paths] if paths else []
        span = f"{base}..{head}"
        log = git(repo, "log", "--reverse", "--topo-order", "--no-merges", f"--max-count={MAX_COMMITS}",
                  "--format=%H%x1f%h%x1f%an%x1f%at%x1f%s", span, *scope)
        for line in log.splitlines():
            sha, short, author, at, subject = line.split("\x1f", 4)
            d = root / sha
            m = read_json(d / "manifest.json")
            if m is None:  # a commit's change set never changes; a folder without its manifest is a torn write
                m = write(repo, d, rev(repo, f"{sha}^") or empty_tree(repo), paths, f"{short}^", head=sha)
            index["entries"].append({"id": sha, "short": short, "author": author, "time": int(at), "subject": subject,
                                     **totals(m)})
        listed = len(index["entries"])
        total = int(git(repo, "rev-list", "--count", "--no-merges", span, *scope).strip() or 0)
        index["omitted"] = max(0, total - listed)
        index["merges"] = int(git(repo, "rev-list", "--count", "--merges", span).strip() or 0)
    if head:
        u = write(repo, root / UNCOMMITTED, head, paths, "HEAD")
        if u["files"]:
            index["uncommitted"] = totals(u)
    write_json(root / "index.json", index)
    return index
