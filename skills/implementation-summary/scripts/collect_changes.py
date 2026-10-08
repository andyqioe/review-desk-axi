#!/usr/bin/env python3
"""Print a line-accurate map of every change, for writing an implementation summary.

For every changed file: status, +/- counts, notes (rename source, binary, mode, symlink)
and each hunk as `#n path:range`, with the enclosing symbol and symbols defined inside.
`#n` is the number `data-hunks` on the companion page uses; the ranges are what path:line
links and data-src excerpts should point at.

  collect_changes.py                                  # every change vs the default base
  collect_changes.py --paths src/a.rs scripts/        # only what you touched
  collect_changes.py --base main                      # committed + uncommitted work since main
  collect_changes.py --paths ... --format inventory   # Markdown list of every changed file
  collect_changes.py --commit a1b2c3d                 # exactly one commit, against its parent

Default base: merge-base with main/master on another branch, otherwise HEAD. The working
tree is always included: committed, staged, unstaged and untracked work. `.lavish/` (this
skill's own output) is excluded unless a --paths entry points inside it.

When the work holds two or more parts (commits since the base, plus any uncommitted rest),
the map opens with a Commits block: write one independent summary per part, each from
`--commit <sha>` (and `--base HEAD` for the uncommitted rest).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from changes import ORIGINS, ChangeSet, FileChange, collect, commits_between, hunk_symbols, in_scope  # noqa: E402

DEFINES_CAP = 30


def group_key(path: str) -> str:
    parent = str(Path(path).parent)
    return "./" if parent == "." else parent + "/"


def groups(cs: ChangeSet) -> "OrderedDict[str, list[FileChange]]":
    out: OrderedDict[str, list[FileChange]] = OrderedDict()
    for f in cs.files:
        out.setdefault(group_key(f.path), []).append(f)
    return out


def counts(f: FileChange) -> str:
    return "binary" if f.binary else f"+{f.adds} -{f.dels}"


def status_mix(cs: ChangeSet) -> str:
    names = {"M": "modified", "A": "added", "?": "new", "R": "renamed", "C": "copied", "T": "type-changed", "D": "deleted", "U": "unmerged"}
    by: OrderedDict[str, int] = OrderedDict()
    for f in sorted(cs.files, key=lambda f: "MA?RCTDU".find(f.status)):
        key = "renamed" if f.paired else names.get(f.status, f.status)
        by[key] = by.get(key, 0) + 1
    return ", ".join(f"{n} {k}" for k, n in by.items())


def origin_mix(cs: ChangeSet) -> str:
    """How many files carry each origin; a file edited again after its commit counts in both."""
    return ", ".join(f"{sum(o in f.origins for f in cs.files)} {o}" for o in ORIGINS if any(o in f.origins for f in cs.files))


def origin_label(f: FileChange, sep: str) -> str:
    """'(committed+unstaged)', or nothing when the status already says untracked."""
    return "" if f.origins == ["untracked"] and f.status == "?" else f" ({sep.join(f.origins)})"


def scope_label(cs: ChangeSet) -> str:
    return f" in {' '.join(s or '.' for s in cs.scope)}" if cs.scope else ""


def against(cs: ChangeSet) -> str:
    """'vs <base>' for the working tree, 'in commit <short> <subject>' for one commit."""
    return f"in commit {cs.base_label}" if cs.commit else f"vs {cs.base_label}"


def parts(cs: ChangeSet, paths: list[str] | None, excludes: list[str]) -> tuple[list[dict], bool]:
    """The commits since the base that touch the scope, and whether uncommitted work remains in it."""
    commits = commits_between(cs.repo, cs.base, paths)
    rest = collect(cs.repo, "HEAD", paths, excludes) if commits else None
    return commits, bool(rest and rest.files)


def print_commits(commits: list[dict], uncommitted: bool) -> None:
    n = len(commits) + uncommitted
    if n < 2:
        return
    rest = " plus uncommitted work" if uncommitted else ""
    print(f"# Commits: {len(commits)}{rest}. PER-COMMIT MODE: write one independent summary per part below")
    print("# (collect_changes.py / build_page.py --commit <sha>; --base HEAD for the uncommitted part),")
    print("# plus a short overview for the whole review. The map that follows is the whole review.")
    for i, c in enumerate(commits, 1):
        print(f"#   {i}/{n} {c['short']} {c['subject']}")
    if uncommitted:
        print(f"#   {n}/{n} uncommitted (working tree vs HEAD)")
    print()


def defines(inside: list[tuple[str, int]]) -> str:
    shown = ", ".join(f"{n} L{i}" for n, i in inside[:DEFINES_CAP])
    return shown + (f" ... (+{len(inside) - DEFINES_CAP} more)" if len(inside) > DEFINES_CAP else "")


def print_map(cs: ChangeSet) -> None:
    print(f"# Changes {against(cs)}{scope_label(cs)}: {len(cs.files)} files, +{cs.adds} -{cs.dels} ({status_mix(cs)})")
    print(f"# Origin: {origin_mix(cs)}. #n = data-hunks index.")
    if cs.base_label == "HEAD (auto)":
        print("# Base is HEAD: work you committed is not included. If you committed during this work, rerun with")
        print("# --base <the commit before your first one> (the activation hook names it).")
    print()
    gs = groups(cs)
    if len(gs) > 1:
        print("By directory:")
        width = max(len(g) for g in gs)
        for g, fs in gs.items():
            a, d = sum(f.adds for f in fs), sum(f.dels for f in fs)
            print(f"  {g:<{width}}  {len(fs):>3} file{'s' if len(fs) != 1 else ' '}  +{a} -{d}")
        print()
    for f in cs.files:
        print(f"## {f.path}  [{f.status_name}]  {counts(f)}{origin_label(f, ', ')}")
        for note in f.notes():
            print(f"   note: {note}")
        if f.status == "D":
            print(f"   deleted: {f.dels} line(s) removed; link what replaced it")
        elif f.status == "?" and not f.paired and f.hunks:
            h = f.hunks[0]
            _, inside = hunk_symbols(f, h)
            print(f"   #1 {f.path}:1-{h.new_len}  whole file" + (f"  defines {defines(inside)}" if inside else ""))
        else:
            for h in f.hunks:
                where, inside = hunk_symbols(f, h)
                ctx = f"  in {where[0]} (L{where[1]})" if where else ""
                if h.new_len == 0:
                    spot = "at file start" if h.new_start == 0 else f"after L{h.new_start}"
                    print(f"   #{h.index} {f.path}:{h.link_line()}  removed {h.dels} line(s) {spot}{ctx}")
                else:
                    print(f"   #{h.index} {f.path}:{h.new_range()}  +{h.adds} -{h.dels}{ctx}"
                          + (f"  defines {defines(inside)}" if inside else ""))
        print()


def inventory_md(cs: ChangeSet) -> str:
    """Every changed file, grouped by directory, as a collapsed Markdown block."""
    out = ["<details>",
           f"<summary>Every change - {len(cs.files)} files, +{cs.adds} -{cs.dels} {against(cs)}{scope_label(cs)}</summary>",
           "", f"Origin: {origin_mix(cs)}.", ""]
    for g, fs in groups(cs).items():
        a, d = sum(f.adds for f in fs), sum(f.dels for f in fs)
        out += [f"**{g}** {len(fs)} file{'s' if len(fs) != 1 else ''}, +{a} -{d}", ""]
        for f in fs:
            line = f.link_line()
            ref = f"`{f.path}:{line}`" if line else f"`{f.path}`"
            notes = "; ".join(f.notes())
            what = notes if f.binary else counts(f) + (f" - {notes}" if notes else "")
            out.append(f"- {ref} {f.status_name}{origin_label(f, '+')}, {what}")
        out.append("")
    out.append("</details>")
    return "\n".join(out) + "\n"


def to_json(cs: ChangeSet) -> dict:
    return {
        "base": cs.base, "base_label": cs.base_label, "commit": cs.commit, "scope": cs.scope, "adds": cs.adds, "dels": cs.dels,
        "files": [{
            "path": f.path, "old_path": f.old_path, "status": f.status, "status_name": f.status_name,
            "adds": f.adds, "dels": f.dels, "binary": f.binary, "notes": f.notes(), "origins": f.origins,
            "hunks": [{"index": h.index, "old_start": h.old_start, "old_len": h.old_len,
                       "new_start": h.new_start, "new_len": h.new_len, "adds": h.adds, "dels": h.dels}
                      for h in f.hunks],
        } for f in cs.files],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", default=None)
    ap.add_argument("--commit", default=None, help="exactly this commit against its parent (ignores --base)")
    ap.add_argument("--paths", nargs="*", default=None, help="limit to these files/dirs/globs (recommended in dirty trees)")
    ap.add_argument("--exclude", nargs="*", default=[], help="extra files/dirs/globs to leave out")
    ap.add_argument("--format", choices=["map", "inventory", "json"], default="map")
    args = ap.parse_args()

    if args.commit and args.base:
        ap.error("--commit and --base are exclusive: a commit is always shown against its parent")
    cs = collect(Path(args.repo), args.base, args.paths, args.exclude, commit=args.commit)
    for s in cs.scope:
        if s and not (cs.repo / s).exists() and not any(in_scope(p, [s]) for f in cs.files for p in (f.path, f.old_path) if p):
            print(f"collect_changes: warning: --paths entry '{s}' matches no file", file=sys.stderr)
    commits, uncommitted = ([], False) if cs.commit else parts(cs, args.paths, args.exclude)
    if not cs.files:
        print(f"No changes {against(cs)}{scope_label(cs)}.")
        return 0
    if args.format == "json":
        print(json.dumps({**to_json(cs), "commits": commits, "uncommitted": uncommitted}, indent=1))
    elif args.format == "inventory":
        print(inventory_md(cs), end="")
    else:
        print_commits(commits, uncommitted)
        print_map(cs)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"collect_changes: {e}", file=sys.stderr)
        sys.exit(2)
