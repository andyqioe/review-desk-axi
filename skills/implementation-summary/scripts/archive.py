"""Persist every summary into one tree: <root>/<project>/<milestone>/<component>/.

  root       ~/.claude/implementation-summaries, or $IMPL_SUMMARY_ROOT
  project    the repository's name (the main checkout's, so worktrees share it)
  milestone  the current branch, or `detached-<sha>`
  component  the area the change touches most: the directory with the most changed lines,
             descending while one subdirectory still holds at least half of the change

Each summary adds files sharing one `<YYYYMMDD-HHMM>-<slug>` stem to its component folder
(a same-day rebuild of the same title replaces them):
the page (.html), the terminal summary (.md), the page body (.body.html), the change
model with origins (.changes.json) and the inventory (.inventory.md). The project's
INDEX.md is rebuilt from the tree on every write, so it never drifts from what is on disk.

  archive.py adopt ~/repo/.lavish/impl-summary-*.html [--dry-run]   # file pages built before the tree
  archive.py sweep ~/repo/.lavish/implementation-summaries [--move]  # merge a sandboxed build's fallback tree
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from changes import ChangeSet, git  # noqa: E402

MAX_DEPTH = 3
MAJORITY = 0.5
SUFFIXES = (".html", ".md", ".body.html", ".changes.json", ".inventory.md")


def root_dir() -> Path:
    return Path(os.environ.get("IMPL_SUMMARY_ROOT") or Path.home() / ".claude" / "implementation-summaries").expanduser()


def slugify(text: str, limit: int = 60) -> str:
    s = re.sub(r"[^\w.]+", "-", text.strip().lower()).strip("-.")
    return (s[:limit].rstrip("-") or "untitled")


def project_name(repo: Path) -> str:
    """Name of the main checkout, so a worktree files its summaries under the same project."""
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    return slugify(common.parent.name if common.name == ".git" else repo.name)


def milestone_name(repo: Path) -> str:
    branch = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", ok=(0, 1)).strip() or "HEAD"  # works before the first commit
    if branch == "HEAD":
        return "detached-" + git(repo, "rev-parse", "--short", "HEAD").strip()
    return slugify(branch.replace("/", "-"))


def component_name(cs: ChangeSet) -> str:
    """The most-touched area: heaviest top-level directory, then deeper while it holds a majority."""
    return component_from_weights({f.path: max(1, f.adds + f.dels) for f in cs.files})


def component_from_weights(weight: dict[str, int]) -> str:
    """component_name over any {repo-relative path: weight} map (changed lines, or link counts)."""
    total = sum(weight.values())
    if not total:
        return "no-changes"
    prefix: list[str] = []
    for depth in range(MAX_DEPTH):
        by = defaultdict(int)
        for path, w in weight.items():
            parts = path.split("/")
            if parts[:depth] == prefix and len(parts) > depth + 1:
                by[parts[depth]] += w
        if not by:
            break
        name, w = max(by.items(), key=lambda kv: (kv[1], kv[0]))
        here = sum(w2 for path, w2 in weight.items() if path.split("/")[:-1] == prefix)
        if (depth and w < MAJORITY * total) or here > w:  # files directly here outweigh any subfolder
            break
        prefix.append(name)
    return slugify("-".join(prefix) if prefix else "root")


def target_dir(cs: ChangeSet, project: str | None = None, milestone: str | None = None,
               component: str | None = None, source: Path | None = None) -> Path:
    """Folder for a summary of `cs`. `source` is the real project when cs.repo is a scratch copy of it."""
    if source is not None:
        project = project or project_name_for(source)
        milestone = milestone or (milestone_name(source) if is_git_root(source) else dt.date.today().isoformat())
    return (root_dir() / slugify(project or project_name(cs.repo)) / slugify(milestone or milestone_name(cs.repo))
            / slugify(component or component_name(cs)))


def stem(title: str, now: dt.datetime | None = None) -> str:
    return f"{(now or dt.datetime.now()).strftime('%Y%m%d-%H%M')}-{slugify(title, 48)}"


# Where a build persists when the central tree is not writable (a sandboxed agent can usually
# write only inside its workspace). `archive.py sweep` later merges it into the central tree.
FALLBACK_DIR = Path(".lavish") / "implementation-summaries"


def persist_or_fallback(folder: Path, repo: Path, *args) -> tuple[list[Path], Path | None]:
    """persist() into the central tree, or into <repo>/.lavish/implementation-summaries when that fails.

    Returns (written files, fallback tree root or None).
    """
    try:
        return persist(folder, *args), None
    except OSError:
        fallback = repo / FALLBACK_DIR
        return persist(fallback / folder.relative_to(root_dir()), *args), fallback


def write_index(project_dir: Path) -> Path:
    """Rebuild <project>/INDEX.md: one line per summary, newest first, grouped by milestone."""
    rows: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for page in project_dir.glob("*/*/*.html"):
        if page.name.endswith(".body.html"):
            continue
        milestone, component = page.parent.parent.name, page.parent.name
        rows[milestone].append((page.stem, component, page.relative_to(project_dir).as_posix()))
    lines = [f"# {project_dir.name} implementation summaries", ""]
    for milestone in sorted(rows, key=lambda m: max(r[0] for r in rows[m]), reverse=True):
        lines += [f"## {milestone}", ""]
        for name, component, rel in sorted(rows[milestone], reverse=True):
            md = rel[: -len(".html")] + ".md"
            extra = f" · [summary]({md})" if (project_dir / md).exists() else ""
            lines.append(f"- `{component}` [{name}]({rel}){extra}")
        lines.append("")
    out = project_dir / "INDEX.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def persist(folder: Path, name: str, page: str, body: str, changes_json: str, inventory_md: str,
            summary: Path | None) -> list[Path]:
    """Write one summary's files into its component folder and refresh the project index.

    A rebuild of the same title on the same day replaces the earlier files instead of piling
    up near-duplicates; a later day keeps both.
    """
    folder.mkdir(parents=True, exist_ok=True)
    day, slug = name[:8], name[14:]
    for old in folder.glob(f"{day}-????-{slug}.*"):
        if old.is_dir():
            continue  # a review-desk session folder (<stem>.review/) outlives rebuilds
        if old.name[:13] != name[:13] and old.name[14:].split(".", 1)[0] == slug:
            old.unlink()
    out = []
    for suffix, content in ((".html", page), (".body.html", body), (".changes.json", changes_json),
                            (".inventory.md", inventory_md)):
        p = folder / f"{name}{suffix}"
        p.write_text(content, encoding="utf-8")
        out.append(p)
    if summary is not None:
        p = folder / f"{name}.md"
        shutil.copyfile(summary, p)
        out.append(p)
    write_index(folder.parent.parent)
    return out


# ------------------------------------------------------------------ adopt
# Pages built before the tree existed sit in repos' .lavish/ folders. `adopt` files a copy of
# each one, reading where it belongs from the page itself: its title, its "generated" stamp
# and the files its editor links point at.

LINK_RE = re.compile(r'(?:vscode|cursor|windsurf|zed)://file(/[^"#?]+?)(?::\d+)?"|"file://(/[^"#?]+)"')
GENERATED_RE = re.compile(r"generated (\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
ROOT_MARKERS = (".git", "SKILL.md", "package.json", "pyproject.toml", "Cargo.toml", "go.mod")


def page_facts(page: Path) -> tuple[str, dt.datetime, dict[str, int]]:
    """(title, generated time, {absolute linked file: link count}) read from a built page."""
    text = page.read_text(encoding="utf-8", errors="replace")
    m = TITLE_RE.search(text)
    title = html.unescape(m.group(1)).strip() if m else page.stem
    g = GENERATED_RE.search(text)
    when = dt.datetime.strptime(g.group(1), "%Y-%m-%d %H:%M") if g else dt.datetime.fromtimestamp(page.stat().st_mtime)
    links: dict[str, int] = defaultdict(int)
    for a, b in LINK_RE.findall(text):
        links[a or b] += 1
    return title, when, dict(links)


def project_root(files: list[str]) -> Path | None:
    """Nearest directory at or above the files' common ancestor that looks like a project root."""
    if not files:
        return None
    common = Path(os.path.commonpath(files))
    if common.is_file() or not common.exists():
        common = common.parent
    for d in (common, *common.parents):
        if any((d / m).exists() for m in ROOT_MARKERS):
            return d
    return common


def branch_at(repo: Path, when: dt.datetime) -> str | None:
    """The branch HEAD was on at local time `when`, from the reflog's checkout entries."""
    try:
        log = git(repo, "reflog", "show", "--date=iso-strict", "--format=%gd%x09%gs", "HEAD")
    except RuntimeError:
        return None
    for line in log.splitlines():  # newest first
        sel, _, msg = line.partition("\t")
        m = re.search(r"@\{(.+)\}$", sel)
        moved = re.match(r"checkout: moving from .+ to (.+)$", msg)
        if not (m and moved):
            continue
        at = dt.datetime.fromisoformat(m.group(1)).astimezone().replace(tzinfo=None)
        if at <= when:
            return moved.group(1)
    return None


def is_git_root(d: Path) -> bool:
    try:
        return Path(git(d, "rev-parse", "--show-toplevel").strip()).resolve() == d.resolve()
    except RuntimeError:
        return False


def plan_adoption(page: Path, project: str | None = None, milestone: str | None = None,
                  component: str | None = None) -> tuple[Path, str]:
    """(target folder, file name) for a built page, without writing anything."""
    title, when, links = page_facts(page)
    root = project_root(sorted(links))
    if root is None:
        raise RuntimeError(f"{page}: no editor links to infer a project from; pass --project/--milestone/--component")
    if milestone is None:
        if is_git_root(root):
            sha = git(root, "rev-parse", "--short", "HEAD", ok=(0, 128)).strip()
            branch = branch_at(root, when) or git(root, "symbolic-ref", "--quiet", "--short", "HEAD", ok=(0, 1)).strip()
            milestone = slugify(branch.replace("/", "-")) if branch else f"detached-{sha}"
        else:
            milestone = when.strftime("%Y-%m-%d")  # no branches outside git; the day is the milestone
    if component is None:
        rel = {}
        for f, n in links.items():
            try:
                rel[Path(f).relative_to(root).as_posix()] = n
            except ValueError:
                continue
        component = component_from_weights(rel) if rel else "root"
    folder = root_dir() / slugify(project or project_name_for(root)) / slugify(milestone) / slugify(component)
    return folder, f"{stem(title, when)}.html"


def project_name_for(root: Path) -> str:
    return project_name(root) if is_git_root(root) else slugify(root.name)


def adopt(page: Path, project: str | None = None, milestone: str | None = None,
          component: str | None = None, dry_run: bool = False) -> tuple[str, Path]:
    """Copy one page into the tree. Returns (action, path): filed, already filed, or would file."""
    folder, name = plan_adoption(page, project, milestone, component)
    data = page.read_bytes()
    project_dir = folder.parent.parent
    for existing in project_dir.glob("*/*/*.html") if project_dir.exists() else []:
        if existing.read_bytes() == data:
            return "already filed", existing
    target = folder / name
    if dry_run:
        return "would file", target
    folder.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    write_index(project_dir)
    return "filed", target


def sweep(src_root: Path, move: bool = False) -> dict[str, int]:
    """Merge a tree with the same <project>/<milestone>/<component>/ layout into the central one."""
    src_root = src_root.resolve()
    dest_root = root_dir().resolve()
    if src_root == dest_root:
        raise RuntimeError("source is the central tree itself")
    counts = {"copied": 0, "identical": 0}
    projects: set[Path] = set()
    for f in sorted(src_root.glob("*/*/*/*")):
        if not f.is_file():
            continue
        dest = dest_root / f.relative_to(src_root)
        if dest.exists() and dest.read_bytes() == f.read_bytes():
            counts["identical"] += 1
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
            counts["copied"] += 1
        projects.add(dest.parent.parent.parent)
        if move:
            f.unlink()
    for project_dir in projects:
        write_index(project_dir)
    if move:  # drop the now-empty source folders and their stale indexes
        for d in sorted((p for p in src_root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            index = d / "INDEX.md"
            if index.exists() and all(c == index for c in d.iterdir()):
                index.unlink()
            if not any(d.iterdir()):
                d.rmdir()
        if src_root.exists() and not any(src_root.iterdir()):
            src_root.rmdir()
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="File earlier summary pages into the summary tree.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("adopt", help="copy built pages (e.g. .lavish/impl-summary-*.html) into the tree")
    a.add_argument("pages", nargs="+", type=Path)
    a.add_argument("--project")
    a.add_argument("--milestone")
    a.add_argument("--component")
    a.add_argument("--dry-run", action="store_true")
    w = sub.add_parser("sweep", help="merge a fallback tree (e.g. <repo>/.lavish/implementation-summaries) into the central one")
    w.add_argument("roots", nargs="+", type=Path)
    w.add_argument("--move", action="store_true", help="delete each source file once it is in the central tree")
    args = ap.parse_args(argv)
    if args.cmd == "sweep":
        for r in args.roots:
            c = sweep(r, args.move)
            print(f"swept {r}: {c['copied']} copied, {c['identical']} already there" + (", source removed" if args.move else ""))
        return 0
    status = 0
    for page in args.pages:
        try:
            action, path = adopt(page, args.project, args.milestone, args.component, args.dry_run)
            print(f"{action}: {page} -> {path}")
        except (RuntimeError, OSError) as e:
            print(f"skipped: {page}: {e}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
