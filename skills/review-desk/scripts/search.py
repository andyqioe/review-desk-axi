"""Repository search for the desk's Cmd+Shift+F, after the biggrep VS Code extension.

`run()` streams match records from ripgrep (`rg --json`), or from `git grep` when ripgrep is missing.
The query model is biggrep's:
- toggles: regex, case-sensitive (default off) and whole word;
- a path filter of comma- or space-separated tokens: `src/` (substring), `*.rs` (glob), `!test` or `-test` (exclude).

Records are plain dicts the server writes as NDJSON:
- {"t": "m", "path", "line", "text", "ranges": [[a, b]], "badge"} - one matching line; ranges are character offsets
- {"t": "done", "matches", "files", "ms", "truncated"} or {"t": "error", "msg"} - always last
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

MAX_MATCHES = 2000
MAX_PREVIEW = 400       # characters of a long line kept around its first match (biggrep `preview`)
MAX_RANGES = 50
MAX_FILESIZE = "2M"     # minified bundles and data dumps only add noise
TIMEOUT = 20.0


@dataclass
class Spec:
    q: str
    regex: bool = False
    case: bool = False
    word: bool = False
    paths: str = ""


# ----------------------------------------------------------------- path filter
def glob_re(glob: str) -> re.Pattern:
    """biggrep's globToRegExp: `*` stays in a segment, `**` crosses them, a glob with a `/` anchors at the root."""
    dir_only = glob.endswith("/")
    g = glob[:-1] if dir_only else glob
    out, i = "", 0
    while i < len(g):
        c = g[i]
        if c == "*":
            if g[i + 1:i + 2] == "*":
                out += ".*"
                i += 1
                if g[i + 1:i + 2] == "/":
                    i += 1
            else:
                out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        else:
            out += re.escape(c)
        i += 1
    anchor = "^" if "/" in g and not g.startswith("**") else "(?:^|/)"
    return re.compile(anchor + re.sub(r"^/", "", out) + ("/" if dir_only else "(?:/|$)"), re.I)


def path_filter(spec: str) -> Callable[[str], bool]:
    include, exclude = [], []
    for raw in filter(None, re.split(r"[\s,]+", spec or "")):
        neg = raw[0] in "!-"
        body = raw[1:] if neg else raw
        if not body:
            continue
        if re.search(r"[*?]", body):
            rx = glob_re(body)
            test = rx.search
        else:
            needle = body.lower()
            test = lambda p, n=needle: n in p.lower()  # noqa: E731
        (exclude if neg else include).append(test)
    return lambda p: (not include or any(t(p) for t in include)) and not any(t(p) for t in exclude)


# --------------------------------------------------------------------- preview
def preview(line: str, ranges: list[list[int]]) -> tuple[str, list[list[int]]]:
    """A long line keeps 400 characters starting a little before its first match, like biggrep."""
    ranges = ranges[:MAX_RANGES]
    if len(line) <= MAX_PREVIEW:
        return line, ranges
    first = ranges[0][0] if ranges else 0
    off = max(0, min(first - 80, len(line) - MAX_PREVIEW))
    end = off + MAX_PREVIEW
    return line[off:end], [[max(a, off) - off, min(b, end) - off] for a, b in ranges if b > off and a < end]


def python_pattern(spec: Spec) -> re.Pattern:
    """The same match in Python `re`, for the git grep fallback (biggrep's buildPattern)."""
    src = spec.q if spec.regex else re.escape(spec.q)
    if spec.word:
        before = r"(?<!\w)" if spec.regex or re.match(r"\w", spec.q) else ""
        after = r"(?!\w)" if spec.regex or re.search(r"\w$", spec.q) else ""
        src = f"{before}(?:{src}){after}"
    return re.compile(src, 0 if spec.case else re.I)


# ------------------------------------------------------------------------ run
def run(repo: Path, spec: Spec, files: list[str] | None, badges: dict[str, str]) -> Iterator[dict]:
    """Stream matches for `spec` over `files` (repository-relative), or the whole repository when None.
    Closing the generator kills the search process, so an aborted query stops at once."""
    t0 = time.monotonic()
    if not spec.q:
        yield {"t": "done", "matches": 0, "files": 0, "ms": 0, "truncated": False}
        return
    if files is not None and not files:
        yield {"t": "done", "matches": 0, "files": 0, "ms": 0, "truncated": False}
        return
    keep = path_filter(spec.paths)
    rg = shutil.which("rg")
    if rg:
        # the repository walk sees what git sees: hidden files too (not .git), and nothing .gitignore'd
        walk = ["--hidden", "-g", "!.git/"] if files is None else []
        cmd = [rg, "--json", "--no-config", "--sort", "path", "--max-filesize", MAX_FILESIZE, *walk,
               "-s" if spec.case else "-i", *(["-F"] if not spec.regex else []), *(["-w"] if spec.word else []),
               "-e", spec.q, "--", *(files or ["."])]
        parse = parse_rg
    else:
        try:
            pattern = python_pattern(spec)
        except re.error as err:
            yield {"t": "error", "msg": f"regex parse error: {err}"}
            return
        cmd = ["git", "grep", "-n", "-I", "--null", "--untracked", *([] if spec.case else ["-i"]),
               "-F" if not spec.regex else "-P", *(["-w"] if spec.word else []), "-e", spec.q, "--", *(files or ["."])]
        parse = lambda line: parse_git(line, pattern)  # noqa: E731
    # stdin closed: rg given no readable path would otherwise wait on it
    proc = subprocess.Popen(cmd, cwd=repo, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    matches, seen, truncated = 0, set(), False
    try:
        for raw in proc.stdout:
            if time.monotonic() - t0 > TIMEOUT:
                truncated = True
                break
            rec = parse(raw)
            if not rec or not keep(rec["path"]):
                continue
            rec["badge"] = badges.get(rec["path"])
            matches += 1
            seen.add(rec["path"])
            yield rec
            if matches >= MAX_MATCHES:
                truncated = True
                break
        else:
            proc.wait(timeout=5)
            err = proc.stderr.read().decode("utf-8", "replace").strip()
            # rg exits 1 for "no match" and 2 for errors; a regex error is the only one worth showing
            if proc.returncode == 2 and not matches and err:
                # rg: "rg: regex parse error:", the pattern with a caret, then "error: unclosed group"
                detail = next((l for l in reversed(err.splitlines()) if l.strip().startswith("error:")), "")
                head = "regex parse error" if "regex parse error" in err else err.splitlines()[0].removeprefix("rg: ")
                msg = f"{head}: {detail.strip().removeprefix('error:').strip()}" if detail else head
                yield {"t": "error", "msg": " ".join(msg.split())[:300]}
                return
        yield {"t": "done", "matches": matches, "files": len(seen), "ms": int((time.monotonic() - t0) * 1000),
               "truncated": truncated}
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def parse_rg(raw: bytes) -> dict | None:
    msg = json.loads(raw)
    if msg.get("type") != "match":
        return None
    d = msg["data"]
    path = d["path"].get("text")
    lines = d["lines"]
    if path is None or "text" not in lines:  # a non-UTF-8 path or line
        return None
    data = lines["text"].encode("utf-8")
    to_char = lambda b: len(data[:b].decode("utf-8", "replace"))  # noqa: E731  rg reports byte offsets
    text = lines["text"].rstrip("\r\n")
    ranges = [[to_char(m["start"]), to_char(m["end"])] for m in d["submatches"]]
    text, ranges = preview(text, [[a, min(b, len(text))] for a, b in ranges if a < len(text)])
    return {"t": "m", "path": path[2:] if path.startswith("./") else path, "line": d["line_number"], "text": text, "ranges": ranges}


def parse_git(raw: bytes, pattern: re.Pattern) -> dict | None:
    parts = raw.decode("utf-8", "replace").rstrip("\r\n").split("\0")
    if len(parts) < 3:
        return None
    path, line, text = parts[0], parts[1], "\0".join(parts[2:])
    ranges = [[m.start(), m.end()] for m in pattern.finditer(text) if m.end() > m.start()]
    text, ranges = preview(text, ranges)
    return {"t": "m", "path": path, "line": int(line), "text": text, "ranges": ranges}
