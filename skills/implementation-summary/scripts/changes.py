"""The change model shared by collect_changes.py and build_page.py.

Both scripts must agree on which files changed, what each hunk is and how hunks are
numbered, so a `#3` printed by collect_changes.py is the hunk `data-hunks="3"` renders.
Hunks are always zero-context (`git diff -U0`), so each one is exactly one changed region;
build_page.py adds display context from the file on disk.

Scope: the working tree (committed, staged, unstaged and untracked work) against a base.
Renames are detected over the whole tree before scoping, so a scoped renamed file keeps
its real diff. A tracked file deleted and re-created untracked elsewhere (`mv` without
`git mv`) is paired as an untracked rename when the contents are at least 50% similar.
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

# The skill's own output; listing earlier summary pages as "changes" is noise.
DEFAULT_EXCLUDES = (".lavish/",)
STATUS_NAMES = {
    "A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "copied",
    "T": "type changed", "?": "new (untracked)", "U": "unmerged",
}
# Where a change lives relative to the base: on a branch commit, in the index, in the working
# tree only, or not yet known to git. A file can be in several (committed, then edited again).
ORIGINS = ("committed", "staged", "unstaged", "untracked")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
BINARY_SNIFF = 8000
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's well-known empty tree object
RENAME_SIMILARITY = 0.5

SYMBOL_RES = [
    re.compile(r"^\s*(?:async\s+)?def\s+(\w+)"),                                       # python
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)"),     # python/js/ts/java
    re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\"[^\"]*\"\s+)?fn\s+(\w+)"),  # rust
    re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|mod|union)\s+(\w+)"),  # rust
    re.compile(r"^\s*(?:unsafe\s+)?impl(?:<[^>]*>)?\s+(?:[\w:<>, ]+\s+for\s+)?([\w:]+)"),  # rust impl
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\*?\s+(\w+)"),   # js/ts
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>"),  # js arrow
    re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+(\w+)"),                    # ts
    re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)"),                                      # go
    re.compile(r"^\s*(?:(?:public|private|protected|internal)\s+)(?:(?:static|final|abstract|override|async|virtual)\s+)*[\w<>\[\], ?]+\s+(\w+)\s*\("),  # java/c#/kotlin
]
# Class methods in brace languages (`async load(id) {`); keywords that look like calls are excluded.
METHOD_RE = re.compile(r"^\s+(?:static\s+)?(?:async\s+)?(?:get\s+|set\s+)?(?!(?:if|for|while|switch|catch|return|function|else|do|with)\b)([A-Za-z_$][\w$]*)\s*\([^()]*\)\s*(?::\s*[^{=]+)?\{\s*$")
METHOD_EXTS = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".java", ".kt", ".cs", ".swift", ".dart"}
MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
MD_EXTS = {".md", ".markdown", ".mdx"}


# ------------------------------------------------------------------ data
@dataclass
class Hunk:
    index: int                      # 1-based within the file; the number data-hunks uses
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    header: str = ""                # git's function context after the second @@
    lines: list[tuple[str, str]] = field(default_factory=list)  # ("+"|"-", text)

    @property
    def adds(self) -> int:
        return sum(1 for s, _ in self.lines if s == "+")

    @property
    def dels(self) -> int:
        return sum(1 for s, _ in self.lines if s == "-")

    @property
    def new_end(self) -> int:
        return self.new_start + self.new_len - 1

    def new_range(self) -> str:
        if self.new_len == 0:
            return str(self.new_start)
        return f"{self.new_start}-{self.new_end}" if self.new_len > 1 else str(self.new_start)

    def link_line(self) -> int:
        """The new-side line a link should land on (1 for a removal at the top of a file)."""
        return max(self.new_start, 1)


@dataclass
class FileChange:
    path: str                       # current path (the old path for a deletion)
    status: str                     # key of STATUS_NAMES
    old_path: str | None = None     # set for renames and copies
    similarity: int | None = None
    old_mode: str | None = None
    new_mode: str | None = None
    binary: bool = False
    old_size: int | None = None     # bytes, binary files only
    new_size: int | None = None
    symlink: str | None = None      # new symlink target
    old_symlink: str | None = None
    submodule: bool = False
    nested_repo: bool = False       # untracked directory that is its own git repository
    paired: bool = False            # untracked rename found by content similarity
    origins: list[str] = field(default_factory=list)  # subset of ORIGINS, in that order
    hunks: list[Hunk] = field(default_factory=list)
    new_lines: list[str] | None = None   # file on disk; None when deleted or unreadable

    @property
    def adds(self) -> int:
        return sum(h.adds for h in self.hunks)

    @property
    def dels(self) -> int:
        return sum(h.dels for h in self.hunks)

    @property
    def status_name(self) -> str:
        name = STATUS_NAMES.get(self.status, self.status)
        if self.paired:
            name = "renamed (untracked)"
        if self.similarity is not None and self.status in "RC" or self.paired:
            name += f" {self.similarity}%"
        return name

    def link_line(self) -> int | None:
        # no line to land on: deleted, binary, symlink, empty, or a mode-only change
        if self.status == "D" or not self.new_lines or not self.hunks:
            return None
        return min(self.hunks[0].link_line(), len(self.new_lines))

    def notes(self) -> list[str]:
        """Facts a hunk list cannot show: binaries, modes, symlinks, empties."""
        out = []
        if self.old_path:
            out.append(f"from {self.old_path}")
        if self.binary:
            sizes = " -> ".join(f"{s:,} B" for s in (self.old_size, self.new_size) if s is not None)
            out.append(f"binary{', ' + sizes if sizes else ''}")
        modes = {self.old_mode, self.new_mode}
        if self.old_mode and self.new_mode and len(modes) == 2 and self.status not in "AD?" and not modes & {"120000", "160000"}:
            if {self.old_mode, self.new_mode} <= {"100644", "100755"}:
                out.append(f"mode {self.old_mode} -> {self.new_mode} ({'now' if self.new_mode == '100755' else 'no longer'} executable)")
            else:
                out.append(f"mode {self.old_mode} -> {self.new_mode}")
        if self.old_symlink is not None:
            out.append(f"was symlink -> {self.old_symlink}")
        if self.symlink is not None:
            out.append(f"symlink -> {self.symlink}")
        if self.submodule:
            out.append("submodule pointer moved")
        if self.nested_repo:
            out.append("nested git repository, contents not diffed")
        if not self.hunks and not out and self.status != "D":
            out.append("empty file" if self.new_lines == [] else "no content change")
        return out


@dataclass
class ChangeSet:
    repo: Path
    base: str
    base_label: str
    files: list[FileChange]
    scope: list[str]

    @property
    def adds(self) -> int:
        return sum(f.adds for f in self.files)

    @property
    def dels(self) -> int:
        return sum(f.dels for f in self.files)

    def get(self, rel: str) -> FileChange | None:
        rel = rel.strip("/")
        for f in self.files:
            if f.path == rel or f.old_path == rel:
                return f
        return None


# ------------------------------------------------------------------- git
def git(repo: Path, *args: str, ok: tuple[int, ...] = (0,)) -> str:
    out = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=repo, capture_output=True)
    if out.returncode not in ok:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.decode(errors='replace').strip()}")
    return out.stdout.decode("utf-8", errors="surrogateescape")


def repo_root(start: Path) -> Path:
    return Path(git(start, "rev-parse", "--show-toplevel").strip())


def has_head(repo: Path) -> bool:
    """False in a repository with no commits yet (an unborn branch)."""
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=repo, capture_output=True).returncode == 0


def default_base(repo: Path) -> str:
    """Merge-base with main/master on a feature branch, otherwise HEAD; the empty tree before the first commit."""
    if not has_head(repo):
        return EMPTY_TREE
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    for main in ("main", "master"):
        if subprocess.run(["git", "rev-parse", "--verify", "--quiet", main], cwd=repo, capture_output=True).returncode:
            continue
        if branch == main:
            return "HEAD"
        return git(repo, "merge-base", "HEAD", main).strip()
    return "HEAD"


def resolve_base(repo: Path, base: str | None) -> tuple[str, str]:
    """(ref to diff against, human label)."""
    if base:
        git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
        return base, base
    ref = default_base(repo)
    if ref == EMPTY_TREE:
        return ref, "empty tree (no commits yet)"
    if ref == "HEAD":
        return ref, "HEAD (auto)"
    return ref, f"merge-base {ref[:10]} (auto)"


# ----------------------------------------------------------------- scope
def to_repo_rel(repo: Path, p: str) -> str:
    """Accept paths relative to the caller's cwd, absolute paths or repo-relative paths."""
    a = Path(p)
    cand = a if a.is_absolute() else Path.cwd() / a
    try:
        rel = os.path.relpath(os.path.abspath(cand), repo)
    except ValueError:
        rel = p
    if rel.startswith(".."):  # not under the repo from cwd; fall back to repo-relative
        rel = p
    rel = rel.replace(os.sep, "/")
    return "" if rel == "." else rel.rstrip("/")


def in_scope(path: str, scope: list[str]) -> bool:
    if not scope:
        return True
    for s in scope:
        if s == "" or path == s or path.startswith(s + "/") or fnmatch(path, s):
            return True
    return False


def excluded(path: str, excludes: list[str], scope: list[str]) -> bool:
    for e in excludes:
        e = e.rstrip("/")
        hit = path == e or path.startswith(e + "/") or fnmatch(path, e)
        # an explicit --paths entry inside the excluded area wins
        if hit and not any(s and (s == e or s.startswith(e + "/") or path == s) for s in scope):
            return True
    return False


# ----------------------------------------------------------------- parse
def parse_patch(text: str) -> tuple[list[Hunk], bool]:
    """Hunks of a zero-context patch (possibly several `diff --git` sections), and a binary flag."""
    hunks: list[Hunk] = []
    binary = False
    cur: Hunk | None = None
    for line in text.split("\n"):
        m = HUNK_RE.match(line)
        if m:
            cur = Hunk(len(hunks) + 1, int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1),
                       int(m.group(3)), int(m.group(4) if m.group(4) is not None else 1), m.group(5).strip())
            hunks.append(cur)
            continue
        if line.startswith("diff --git") or line.startswith("diff --cc"):
            cur = None
            continue
        if line.startswith("Binary files ") or line == "GIT binary patch":
            binary = True
            continue
        if cur is not None and line and line[0] in "+-" and not line.startswith(("+++ ", "--- ")):
            cur.lines.append((line[0], line[1:].rstrip("\r")))
    return hunks, binary


def read_lines(path: Path) -> list[str] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_bytes().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None


def is_binary_bytes(data: bytes) -> bool:
    return b"\0" in data[:BINARY_SNIFF]


def blob_size(repo: Path, spec: str) -> int | None:
    try:
        return int(git(repo, "cat-file", "-s", spec).strip())
    except (RuntimeError, ValueError):
        return None


def disk_size(path: Path) -> int | None:
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


# --------------------------------------------------------------- collect
def _raw_entries(repo: Path, base: str) -> list[dict]:
    raw = git(repo, "diff", "-z", "--raw", "-M", "--no-ext-diff", base)
    parts = raw.split("\0")
    out, i = [], 0
    while i < len(parts):
        head = parts[i]
        if not head.startswith(":"):
            i += 1
            continue
        old_mode, new_mode, _old_sha, _new_sha, st = head[1:].split(" ")
        code = st[0]
        if code in "RC":
            out.append({"status": code, "sim": int(st[1:] or 0), "old": parts[i + 1], "path": parts[i + 2],
                        "old_mode": old_mode, "new_mode": new_mode})
            i += 3
        else:
            out.append({"status": code, "sim": None, "old": None, "path": parts[i + 1],
                        "old_mode": old_mode, "new_mode": new_mode})
            i += 2
    return out


def _tracked_change(repo: Path, base: str, e: dict) -> FileChange:
    fc = FileChange(e["path"], e["status"], e["old"], e["sim"], e["old_mode"], e["new_mode"])
    paths = [e["old"], e["path"]] if e["old"] else [e["path"]]
    patch = git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", "-M", base, "--", *paths)
    fc.hunks, fc.binary = parse_patch(patch)
    disk = repo / e["path"]
    fc.submodule = "160000" in (e["old_mode"], e["new_mode"])
    if fc.submodule:
        fc.hunks = []
    if e["new_mode"] == "120000" and disk.is_symlink():
        fc.symlink = os.readlink(disk)
    if e["old_mode"] == "120000":
        try:
            fc.old_symlink = git(repo, "cat-file", "-p", f"{base}:{e['old'] or e['path']}")
        except RuntimeError:
            fc.old_symlink = "?"
    if e["status"] != "D":
        fc.new_lines = read_lines(disk)
    if fc.binary:
        fc.old_size = None if e["status"] == "A" else blob_size(repo, f"{base}:{e['old'] or e['path']}")
        fc.new_size = None if e["status"] == "D" else disk_size(disk)
    return fc


def _untracked_change(repo: Path, rel: str) -> FileChange:
    disk = repo / rel
    fc = FileChange(rel.rstrip("/"), "?")
    if rel.endswith("/"):  # ls-files reports an embedded repository as a directory
        fc.nested_repo = True
        return fc
    if disk.is_symlink():
        fc.symlink = os.readlink(disk)
        fc.new_mode = "120000"
        return fc
    try:
        data = disk.read_bytes()
    except OSError:
        return fc
    if is_binary_bytes(data):
        fc.binary, fc.new_size = True, len(data)
        return fc
    lines = data.decode("utf-8", errors="replace").splitlines()
    fc.new_lines = lines
    if lines:
        fc.hunks = [Hunk(1, 0, 0, 1, len(lines), "", [("+", l.rstrip("\r")) for l in lines])]
    return fc


def _pair_untracked_renames(repo: Path, base: str, files: list[FileChange]) -> list[FileChange]:
    """Turn (deleted tracked file, similar untracked file) into one untracked rename."""
    deleted = [f for f in files if f.status == "D" and not f.binary and f.hunks]
    fresh = [f for f in files if f.status == "?" and f.new_lines and not f.binary]
    taken: set[str] = set()
    for d in deleted:
        old = [l for _, l in d.hunks[0].lines] if len(d.hunks) == 1 else [l for h in d.hunks for _, l in h.lines]
        best, best_ratio = None, RENAME_SIMILARITY
        for n in fresh:
            if n.path in taken or Path(n.path).suffix != Path(d.path).suffix:
                continue
            if not 0.5 <= len(n.new_lines) / max(1, len(old)) <= 2:
                continue
            sm = difflib.SequenceMatcher(None, old, n.new_lines, autojunk=False)
            if sm.real_quick_ratio() < best_ratio or sm.quick_ratio() < best_ratio:
                continue
            r = sm.ratio()
            if r >= best_ratio and (best is None or r > best_ratio):
                best, best_ratio = n, r
        if best is None:
            continue
        taken.add(best.path)
        with tempfile.NamedTemporaryFile("w", suffix=Path(d.path).suffix, delete=False, encoding="utf-8") as tmp:
            tmp.write("\n".join(old) + "\n")
        try:
            patch = git(repo, "diff", "--no-index", "-U0", "--no-color", "--no-ext-diff", tmp.name, str(repo / best.path), ok=(0, 1))
        finally:
            os.unlink(tmp.name)
        best.hunks, _ = parse_patch(patch)
        best.old_path, best.paired, best.similarity = d.path, True, round(best_ratio * 100)
        files.remove(d)
    return files


def _origins(repo: Path, ref: str) -> dict[str, set[str]]:
    """Paths (post-rename) changed by branch commits since ref, in the index, and in the working tree."""
    def names(*args: str) -> set[str]:
        return {p for p in git(repo, "diff", "-z", "--name-only", "-M", *args).split("\0") if p}

    committed: set[str] = set()
    if has_head(repo):
        head = git(repo, "rev-parse", "--verify", "HEAD").strip()
        committed = names(ref, "HEAD") if git(repo, "rev-parse", "--verify", ref).strip() != head else set()
    return {"committed": committed, "staged": names("--cached"), "unstaged": names(), "untracked": set()}


def collect(repo: Path, base: str | None = None, paths: list[str] | None = None,
            excludes: list[str] | None = None, pair_renames: bool = True) -> ChangeSet:
    repo = repo_root(repo)
    ref, label = resolve_base(repo, base)
    scope = [to_repo_rel(repo, p) for p in (paths or [])]
    excl = list(DEFAULT_EXCLUDES) + list(excludes or [])

    def keep(*ps: str | None) -> bool:
        live = [p for p in ps if p]
        return any(in_scope(p, scope) for p in live) and not all(excluded(p, excl, scope) for p in live)

    files: list[FileChange] = []
    for e in _raw_entries(repo, ref):
        if keep(e["path"], e["old"]):
            files.append(_tracked_change(repo, ref, e))
    # Every untracked file is listed; an embedded repository comes back as one "dir/" entry.
    for rel in git(repo, "ls-files", "-z", "--others", "--exclude-standard").split("\0"):
        if rel and keep(rel.rstrip("/")):
            files.append(_untracked_change(repo, rel))
    if pair_renames:
        files = _pair_untracked_renames(repo, ref, files)
    where = _origins(repo, ref)
    for f in files:
        f.origins = ["untracked"] if f.status == "?" else [o for o in ORIGINS if f.path in where[o]]
    files.sort(key=lambda f: f.path)
    return ChangeSet(repo, ref, label, files, scope)


# --------------------------------------------------------------- symbols
MD_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def md_headings(lines: list[str]) -> dict[int, str]:
    """1-based line -> heading text, skipping anything inside fenced code blocks."""
    out, fence = {}, None
    for i, line in enumerate(lines, 1):
        m = MD_FENCE_RE.match(line)
        if m:
            mark = m.group(1)
            if fence is None:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence) and not line.strip()[len(mark):].strip():
                fence = None
            continue
        if fence is None and (h := MD_HEADING_RE.match(line)):
            out[i] = h.group(2)
    return out


def symbol_at(line: str, ext: str = "") -> str | None:
    """Definition name on this line (code only; Markdown headings need md_headings for fences)."""
    if ext in MD_EXTS:
        m = MD_HEADING_RE.match(line)
        return m.group(2) if m else None
    for rx in SYMBOL_RES:
        m = rx.match(line)
        if m:
            return m.group(1)
    if ext in METHOD_EXTS:
        m = METHOD_RE.match(line)
        if m:
            return m.group(1)
    return None


def enclosing(lines: list[str], start: int, ext: str = "") -> tuple[str, int] | None:
    """Nearest definition at or above 1-based line `start` that contains it (by indentation)."""
    if not lines or start < 1:
        return None
    i = min(start, len(lines)) - 1
    if ext in MD_EXTS:
        heads = md_headings(lines)
        above = [n for n in heads if n <= i + 1]
        return (heads[max(above)], max(above)) if above else None
    # a blank start line has no indentation of its own: judge it by the next line with text
    probe = next((k for k in range(i, len(lines)) if lines[k].strip()), None)
    target = len(lines[probe]) - len(lines[probe].lstrip()) if probe is not None else 10**6
    for j in range(i, -1, -1):
        s = lines[j]
        if not s.strip():
            continue
        ind = len(s) - len(s.lstrip())
        name = symbol_at(s, ext)
        if name and (ind < target or j == i):
            return name, j + 1
    return None


def hunk_symbols(fc: FileChange, h: Hunk) -> tuple[tuple[str, int] | None, list[tuple[str, int]]]:
    """(enclosing symbol, symbols defined inside the hunk's new lines)."""
    lines, ext = fc.new_lines or [], Path(fc.path).suffix.lower()
    if h.new_len == 0:
        return (enclosing(lines, h.new_start, ext) if h.new_start >= 1 else None), []
    if ext in MD_EXTS:
        heads = md_headings(lines)
        inside = [(heads[i], i) for i in range(h.new_start, h.new_end + 1) if i in heads]
    else:
        inside = [(n, i) for i in range(h.new_start, min(h.new_end, len(lines)) + 1)
                  if (n := symbol_at(lines[i - 1], ext))]
    where = enclosing(lines, h.new_start, ext)
    if where and where in inside:
        where = None
    return where, inside
