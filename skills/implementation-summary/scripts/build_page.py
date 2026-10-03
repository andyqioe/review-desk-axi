#!/usr/bin/env python3
"""Build the animated implementation-summary page from a hand-written body fragment.

The body is free-form HTML (see references/page.md). This script wraps it in the page
shell, inlines the CSS and particle engine, and expands four directives so code and
links always come from the files on disk and git, never from memory:

  <figure data-src="src/x.rs:40-72" [data-focus="51-55"] [data-lang="rust"] [data-title="..."]>
      [optional caption HTML]
  </figure>
      -> real lines 40-72 of src/x.rs with a line-number gutter and an editor link.

  <figure data-diff="src/x.rs" [data-hunks="1,3"] [data-max="80"] [data-title="..."]></figure>
      -> the diff of that file against --base, with old/new line numbers. Hunks are the
         `#n` numbers collect_changes.py prints. Deleted, renamed, binary and untracked
         files render too. Nothing is cut unless data-max is set, and a cut is labelled.

  <section data-changes [data-paths="src/ docs/"]></section>
      -> "Every change": every changed file in scope, grouped by directory, each with its
         full diff in a collapsed row. Inserted before the proof section when the body
         has none, so the page always accounts for the whole change.

  <a data-loc="src/x.rs:51">optional text</a>
      -> editor link; empty text becomes "src/x.rs:51".

  <span data-term="claim">claim</span>  ...  <dl class="glossary">
    <dt data-term="claim" [data-loc="src/q.rs:44"]>claim</dt><dd>definition</dd>[<dd class="ask">question</dd>...]
  </dl>
      -> a hover card per term (definition, editor link, "Ask Claude", suggested questions);
         the list itself is not shown. Every data-term needs an entry, every entry a definition.

<pre class="scene"> contents are dedented and HTML-escaped, so write raw ASCII art.

Exit status is 1 (and nothing is written) if any directive points at a missing file, an
out-of-range line or a hunk that does not exist, so a stale reference cannot ship silently.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import textwrap
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive  # noqa: E402
from changes import ORIGINS, ChangeSet, FileChange, Hunk, collect, hunk_symbols  # noqa: E402
from collect_changes import inventory_md, to_json  # noqa: E402

SKILL = Path(__file__).resolve().parent.parent
ASSETS = SKILL / "assets"
CONTEXT = 3
INVENTORY_MAX_ROWS = 4000  # per file; guards generated blobs, and the cut is labelled

EXT_LANG = {
    ".py": "python", ".rs": "rust", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript", ".go": "go", ".java": "java",
    ".kt": "kotlin", ".rb": "ruby", ".sh": "bash", ".zsh": "bash", ".c": "c", ".h": "c", ".cc": "cpp",
    ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".swift": "swift", ".sql": "sql", ".json": "json",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "ini", ".md": "markdown", ".html": "xml", ".css": "css",
    ".scss": "scss", ".php": "php", ".lua": "lua", ".dockerfile": "dockerfile",
}
STATUS_BADGE = {"A": "A", "?": "N", "M": "M", "D": "D", "R": "R", "C": "C", "T": "T", "U": "U"}

ATTR_RE = re.compile(r'([\w-]+)(?:\s*=\s*"([^"]*)")?')
FIGURE_RE = re.compile(r"<figure\b(?P<attrs>[^>]*\bdata-(?:src|diff)=\"[^\"]*\"[^>]*)>(?P<inner>.*?)</figure>", re.S)
CHANGES_RE = re.compile(r"<section\b(?P<attrs>[^>]*\bdata-changes\b[^>]*)>(?P<inner>.*?)</section>", re.S)
LOC_RE = re.compile(r"<a\b(?P<attrs>[^>]*\bdata-loc=\"[^\"]*\"[^>]*)>(?P<inner>.*?)</a>", re.S)
SCENE_RE = re.compile(r"(<pre\b[^>]*\bclass=\"[^\"]*\bscene\b[^\"]*\"[^>]*>)(?P<art>.*?)(</pre>)", re.S)
REF_RE = re.compile(r'\bdata-(?:src|diff|loc)="([^"]+)"')
GLOSSARY_RE = re.compile(r'<dl\b[^>]*\bclass="[^"]*\bglossary\b[^"]*"[^>]*>(?P<inner>.*?)</dl>', re.S)
ENTRY_RE = re.compile(r"<dt\b(?P<attrs>[^>]*)>(?P<name>.*?)</dt>(?P<dds>(?:\s*<dd\b[^>]*>.*?</dd>)*)", re.S)
DD_RE = re.compile(r"<dd\b(?P<attrs>[^>]*)>(?P<inner>.*?)</dd>", re.S)
TERM_RE = re.compile(r'\bdata-term="([^"]+)"')
TAG_RE = re.compile(r"<[^>]+>")
ASK_MAX = 3
SHORT_CODE_RE = re.compile(r"<code>([^<]{1,24})</code>")
PROOF_RE = re.compile(r'<section\b[^>]*\bclass="[^"]*\bproof\b')

Row = tuple[str, str, str, str]  # (old no, new no, text, class)


def attrs_of(s: str) -> dict:
    return {k: v for k, v in ATTR_RE.findall(s)}


def strip_spec(spec: str) -> str:
    """'src/x.rs:10-20' -> 'src/x.rs'."""
    return re.sub(r":\d+(?:-\d+)?$", "", spec)


# ------------------------------------------------------------- diff rows
def anchors(h: Hunk) -> tuple[int, int]:
    """(last unchanged new line before the change, first unchanged new line after it)."""
    if h.new_len == 0:
        return h.new_start, h.new_start + 1
    return h.new_start - 1, h.new_end + 1


def diff_rows(fc: FileChange, selected: list[Hunk], cap: int | None) -> tuple[list[Row], str | None]:
    """Rows for the chosen hunks with up to CONTEXT unchanged lines around each.

    Context comes from the file on disk and stops at any neighbouring hunk, so a line that
    another hunk changed is never shown as unchanged. Returns (rows, truncation note).
    """
    new = fc.new_lines or []
    hunks = fc.hunks
    # old = new + shift for an unchanged line, shift accumulated over hunks above it
    shifts, acc = [], 0
    for h in hunks:
        acc += h.old_len - h.new_len
        shifts.append((anchors(h)[1], acc))

    def old_no(line: int) -> int:
        s = 0
        for first_after, total in shifts:
            if first_after <= line:
                s = total
        return line + s

    rows: list[Row] = []
    shown_to = 0
    for pos, h in enumerate(selected):
        if cap is not None and len(rows) >= cap:
            rest = selected[pos:]
            return rows, (f"cut at data-max={cap}: hunk{'s' if len(rest) > 1 else ''} "
                          f"{', '.join('#' + str(r.index) for r in rest)} not shown "
                          f"(+{sum(r.adds for r in rest)} -{sum(r.dels for r in rest)})")
        before, after = anchors(h)
        prev = hunks[h.index - 2] if h.index > 1 else None
        nxt = hunks[h.index] if h.index < len(hunks) else None
        where, _ = hunk_symbols(fc, h)
        sym = f"  in {where[0]}" if where else ""
        lo = max(1, before - CONTEXT + 1, anchors(prev)[1] if prev else 1, shown_to + 1)
        # "join": no hidden lines since the previous hunk, so highlighting may carry across the header
        joined = pos > 0 and lo <= shown_to + 1
        rows.append(("", "", f"#{h.index}  @@ -{h.old_start},{h.old_len} +{h.new_start},{h.new_len} @@{sym}",
                     "hunk join" if joined else "hunk"))
        for ln in range(lo, min(before, len(new)) + 1):
            rows.append((str(old_no(ln)), str(ln), new[ln - 1], ""))
        o, n = h.old_start, h.new_start
        for sign, text in h.lines:
            if sign == "-":
                rows.append((str(o), "", text, "del"))
                o += 1
            else:
                rows.append(("", str(n), text, "add"))
                n += 1
        hi = min(len(new), after + CONTEXT - 1, anchors(nxt)[0] if nxt else len(new))
        for ln in range(after, hi + 1):
            rows.append((str(old_no(ln)), str(ln), new[ln - 1], ""))
        shown_to = max(shown_to, hi, before)
        if cap is not None and len(rows) > cap:
            dropped = len(rows) - cap
            rows = rows[:cap]
            rest = selected[pos + 1:]
            more = f"; hunk{'s' if len(rest) > 1 else ''} {', '.join('#' + str(r.index) for r in rest)} not shown" if rest else ""
            return rows, f"cut at data-max={cap}: last {dropped} line(s) of #{h.index}{more}"
    return rows, None


def meta_rows(fc: FileChange) -> list[Row]:
    return [("", "", note, "meta") for note in fc.notes() if not note.startswith("from ")]


# ----------------------------------------------------------- review data
def write_review_data(b: "Builder", out: Path) -> int:
    """manifest.json + diffs/<n>.json for review-desk, from the same change model as the page.

    Rows are the page's diff rows (old no, new no, text, class), so the desk and the page never
    disagree about a line number. Returns the number of files written.
    """
    diffs = out / "diffs"
    diffs.mkdir(parents=True, exist_ok=True)
    for stale in diffs.glob("*.json"):
        stale.unlink()
    files = []
    for n, fc in enumerate(b.cs.files, 1):
        total = sum(1 for h in fc.hunks for _ in h.lines)
        rows, cut = diff_rows(fc, fc.hunks, INVENTORY_MAX_ROWS if total > INVENTORY_MAX_ROWS else None)
        rows = meta_rows(fc) + rows + ([("", "", cut, "meta")] if cut else [])
        (diffs / f"{n}.json").write_text(json.dumps({"rows": rows}, ensure_ascii=False), encoding="utf-8")
        hunks = []
        for h in fc.hunks:
            where, inside = hunk_symbols(fc, h)
            sym = where or (inside[0] if inside else None)
            hunks.append({"n": h.index, "old_start": h.old_start, "old_len": h.old_len, "new_start": h.new_start,
                          "new_len": h.new_len, "symbol": sym[0] if sym else h.header})
        files.append({"n": n, "path": fc.path, "old_path": fc.old_path, "status": fc.status_name,
                      "badge": STATUS_BADGE.get(fc.status, fc.status), "origins": fc.origins, "adds": fc.adds,
                      "dels": fc.dels, "binary": fc.binary, "notes": [x for x in fc.notes() if not x.startswith("from ")],
                      "hunks": hunks, "cited": fc.path in b.referenced or (fc.old_path or "") in b.referenced})
    manifest = {"repo": str(b.repo), "link_root": str(b.link_root), "base_label": b.cs.base_label,
                "scope": b.cs.scope, "files": files}
    tmp = out / ".manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out / "manifest.json")
    return len(files)


# --------------------------------------------------------------- builder
class Builder:
    def __init__(self, repo: Path, base: str | None, editor: str, paths: list[str] | None, excludes: list[str],
                 source: Path | None = None):
        self.repo = repo
        self.link_root = source or repo  # editor links open the real project, not a scratch copy
        self.editor = editor
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.stats = {"code": 0, "diff": 0, "loc": 0, "scene": 0, "inventory": 0, "terms": 0}
        self.cs: ChangeSet = collect(repo, base, paths, excludes)
        self.full: ChangeSet | None = None  # unscoped, for data-diff on files outside --paths
        self.base_arg, self.excludes = base, excludes
        self.referenced: set[str] = set()

    def change(self, rel: str) -> FileChange | None:
        fc = self.cs.get(rel)
        if fc is None:
            if self.full is None:
                self.full = collect(self.repo, self.base_arg, None, self.excludes)
            fc = self.full.get(rel)
        return fc

    # ------------------------------------------------------------ helpers
    def href(self, rel: str, line: int | None) -> str:
        path = (self.link_root / rel).resolve()
        if self.editor == "none":
            return ""
        if self.editor == "file":
            return f"file://{path}"
        return f"{self.editor}://file{path}{f':{line}' if line else ''}"

    def read_lines(self, rel: str) -> list[str] | None:
        path = self.repo / rel
        if not path.is_file():
            self.errors.append(f"missing file: {rel}")
            return None
        return path.read_text(encoding="utf-8", errors="replace").splitlines()

    @staticmethod
    def parse_range(spec: str) -> tuple[int, int] | None:
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", spec.strip())
        if not m:
            return None
        a = int(m.group(1))
        return a, int(m.group(2) or a)

    @staticmethod
    def lang_for(rel: str, attrs: dict | None = None) -> str:
        if attrs and attrs.get("data-lang"):
            return attrs["data-lang"]
        name = Path(rel).name.lower()
        if name == "dockerfile" or name.startswith("dockerfile."):
            return "dockerfile"
        return EXT_LANG.get(Path(rel).suffix.lower(), "plaintext")

    def head(self, rel: str, line: int | None, tag: str, label: str | None = None) -> str:
        label = label or (f"{rel}:{line}" if line else rel)
        href = self.href(rel, line) if (self.repo / rel).exists() else ""
        link = (f'<a class="path" href="{html.escape(href)}" title="Open in editor">{html.escape(label)}</a>'
                if href else f'<span class="path">{html.escape(label)}</span>')
        return f'<div class="code-head">{link}<span class="tag">{html.escape(tag)}</span></div>'

    @staticmethod
    def body(rows: list[Row], lang: str, diff: bool) -> str:
        """rows are (old, new, text, cls); a plain excerpt leaves `old` empty and uses one gutter."""
        # an added file has no old numbers and a deleted one no new numbers: drop the empty column
        cols = [c for c in (0, 1) if any(r[c] for r in rows)] if diff else [1]
        diff = diff and len(cols) == 2
        gutters = "".join(
            f'<div class="gutter" data-col="{("old", "new")[c]}">' + "".join(f'<span class="{r[3]}">{html.escape(r[c]) or "&#8203;"}</span>' for r in rows) + "</div>"
            for c in cols)
        code = "".join(f'<span class="ln {r[3]}">{html.escape(r[2]) or "&#8203;"}</span>' for r in rows)
        kind = " diff" if diff else ("" if cols else " bare")
        return f'<div class="code-body{kind}">{gutters}<pre><code class="language-{lang}">{code}</code></pre></div>'

    def diff_tag(self, fc: FileChange, attrs: dict | None = None) -> str:
        if attrs and attrs.get("data-title"):
            return attrs["data-title"]
        parts = [fc.status_name]
        if fc.old_path:
            parts.append(f"from {fc.old_path}")
        parts.append(f"vs {self.cs.base_label.replace(' (auto)', '')}")
        return " · ".join(parts)

    # -------------------------------------------------------- directives
    def code_figure(self, attrs: dict, inner: str) -> str:
        spec = attrs["data-src"]
        rel, _, rng = spec.rpartition(":")
        rng_t = self.parse_range(rng) if rel else None
        if not rel or not rng_t:
            self.errors.append(f"bad data-src (want path:start-end): {spec}")
            return ""
        lines = self.read_lines(rel)
        if lines is None:
            return ""
        a, b = rng_t
        if a < 1 or b > len(lines) or a > b:
            self.errors.append(f"line range {a}-{b} out of bounds for {rel} ({len(lines)} lines)")
            return ""
        focus = set()
        for part in filter(None, attrs.get("data-focus", "").split(",")):
            fr = self.parse_range(part)
            if fr:
                focus.update(range(fr[0], fr[1] + 1))
        chunk = textwrap.dedent("\n".join(lines[a - 1: b])).split("\n")
        rows = [("", str(n), t, "focus" if n in focus else "") for n, t in zip(range(a, b + 1), chunk)]
        tag = attrs.get("data-title") or (f"lines {a}-{b}" if a != b else f"line {a}")
        caption = f"<figcaption>{inner.strip()}</figcaption>" if inner.strip() else ""
        self.stats["code"] += 1
        return f'<figure class="code">{self.head(rel, a, tag)}{self.body(rows, self.lang_for(rel, attrs), False)}{caption}</figure>'

    def diff_figure(self, attrs: dict, inner: str) -> str:
        rel = attrs["data-diff"].strip("/")
        fc = self.change(rel)
        if fc is None:
            what = "missing file" if not (self.repo / rel).exists() else "no change"
            self.errors.append(f"data-diff {rel}: {what} vs {self.cs.base_label}")
            return ""
        want = [int(x) for x in re.split(r"[,\s]+", attrs.get("data-hunks", "").strip()) if x.isdigit()]
        bad = [w for w in want if w < 1 or w > len(fc.hunks)]
        if bad:
            self.errors.append(f"data-diff {rel}: hunk(s) {bad} out of range (file has {len(fc.hunks)}; see #n in collect_changes.py)")
            return ""
        selected = [fc.hunks[w - 1] for w in sorted(set(want))] if want else fc.hunks
        cap = int(attrs["data-max"]) if attrs.get("data-max", "").isdigit() else None
        caption = f"<figcaption>{inner.strip()}</figcaption>" if inner.strip() else ""
        self.stats["diff"] += 1
        return self.diff_block(fc, selected, cap, self.diff_tag(fc, attrs), caption, self.lang_for(fc.path, attrs))

    def diff_block(self, fc: FileChange, selected: list[Hunk], cap: int | None, tag: str, caption: str = "", lang: str | None = None) -> str:
        rows, cut = diff_rows(fc, selected, cap)
        rows = meta_rows(fc) + rows
        if cut:
            rows.append(("", "", cut, "meta"))
        if not rows:
            rows = [("", "", "no line changes", "meta")]
        first = fc.link_line() if not selected or selected == fc.hunks else max(selected[0].new_start, 1)
        label = f"{fc.old_path} → {fc.path}" if fc.old_path else None
        line = first if fc.status != "D" else None
        return (f'<figure class="code">{self.head(fc.path, line, tag, label and (label + (f":{line}" if line else "")))}'
                f'{self.body(rows, lang or self.lang_for(fc.path), True)}{caption}</figure>')

    def inventory(self, attrs: dict) -> str:
        cs = self.cs
        if attrs.get("data-paths"):
            cs = collect(self.repo, self.base_arg, attrs["data-paths"].split(), self.excludes)
        groups: OrderedDict[str, list[FileChange]] = OrderedDict()
        for f in cs.files:
            parent = str(Path(f.path).parent)
            groups.setdefault("./" if parent == "." else parent + "/", []).append(f)
        out = [
            '<section class="changes" id="every-change">',
            '<div class="section-head"><span class="index">Δ</span><h2>Every change</h2></div>',
            '<div class="changes-bar">',
            f'<span class="chip tally-files"><b>{len(cs.files)}</b> file{"s" if len(cs.files) != 1 else ""}</span>',
            f'<span class="chip tally-lines"><span class="add">+{cs.adds}</span> <span class="del">−{cs.dels}</span></span>',
            f'<span class="chip">vs {html.escape(cs.base_label)}</span>',
        ]
        if cs.scope:
            out.append(f'<span class="chip">in {html.escape(" ".join(s or "." for s in cs.scope))}</span>')
        out.append('<span class="ctl-group"><button type="button" class="ctl" data-act="open">expand all</button>'
                   '<button type="button" class="ctl" data-act="close">collapse all</button></span></div>')
        present = [(o, sum(o in f.origins for f in cs.files)) for o in ORIGINS]
        out.append('<div class="changes-bar origins"><span class="bar-label">show only</span>' + "".join(
            f'<button type="button" class="ctl origin" data-origin="{o}" aria-pressed="false" '
            f'title="show only {o} changes">{n} {o}</button>' for o, n in present if n) + "</div>")
        if not cs.files:
            out.append('<p class="empty">No changes in scope.</p>')
        for g, fs in groups.items():
            a, d = sum(f.adds for f in fs), sum(f.dels for f in fs)
            out.append(f'<div class="cgroup"><div class="cgroup-head"><span class="dir">{html.escape(g)}</span>'
                       f'<span class="meta">{len(fs)} file{"s" if len(fs) != 1 else ""} · +{a} −{d}</span></div><div class="cfiles">')
            for f in fs:
                notes = [n for n in f.notes() if not n.startswith("from ")]
                note = f'<span class="cnote">{html.escape("; ".join(notes))}</span>' if notes else ""
                old = f'<span class="cold">{html.escape(f.old_path)} →</span>' if f.old_path else ""
                stat = ('<span class="cstat">binary</span>' if f.binary else
                        f'<span class="cstat"><span class="add">+{f.adds}</span> <span class="del">−{f.dels}</span></span>' if f.hunks else "")
                story = '<span class="cref" title="also shown in a story above">story</span>' if f.path in self.referenced or f.old_path in self.referenced else ""
                badge = STATUS_BADGE.get(f.status, f.status)
                rows = sum(1 for h in f.hunks for _ in h.lines)
                cap = INVENTORY_MAX_ROWS if rows > INVENTORY_MAX_ROWS else None
                out.append(
                    f'<details class="cfile" data-status="{badge}" data-origins="{" ".join(f.origins)}" data-adds="{f.adds}" data-dels="{f.dels}"><summary>'
                    f'<span class="st" data-status="{badge}" title="{html.escape(f.status_name)}">{badge}</span>'
                    f'<span class="cpath">{old}<span class="cname">{html.escape(Path(f.path).name)}</span></span>'
                    f'{note}{story}<span class="corg">{"+".join(f.origins)}</span>{stat}</summary>'
                    f'{self.diff_block(f, f.hunks, cap, self.diff_tag(f))}</details>')
                self.stats["inventory"] += 1
            out.append("</div></div>")
        out.append("</section>")
        return "\n".join(out)

    def checked_href(self, spec: str, what: str) -> str:
        """Editor link for `path[:a[-b]]`; a missing file or out-of-range line is a build error."""
        m = re.fullmatch(r"(.+?)(?::(\d+)(?:-(\d+))?)?", spec)
        rel, a, b = m.group(1), m.group(2), m.group(3)
        lines = self.read_lines(rel)
        if lines is not None and a:
            hi = int(b or a)
            if int(a) < 1 or hi > len(lines):
                self.errors.append(f"{what} {spec}: line out of bounds ({len(lines)} lines)")
        return self.href(rel, int(a) if a else None)

    def loc(self, attrs: dict, inner: str) -> str:
        spec = attrs["data-loc"]
        href = self.checked_href(spec, "data-loc")
        text = inner.strip() or html.escape(spec)
        extra = " ".join(f'{k}="{v}"' for k, v in attrs.items() if k not in ("data-loc", "href", "class"))
        cls = ("loc " + attrs.get("class", "")).strip()
        self.stats["loc"] += 1
        h = f' href="{html.escape(href)}"' if href else ""
        return f'<a class="{cls}"{h} title="{html.escape(spec)}" {extra}>{text}</a>'

    def glossary(self, body: str) -> str:
        """Lift every <dl class="glossary"> into page data for glossary.js; check both directions."""
        entries: dict[str, dict] = {}

        def take(m: re.Match) -> str:
            for e in ENTRY_RE.finditer(m.group("inner")):
                attrs = attrs_of(e.group("attrs"))
                key = attrs.get("data-term")
                name = html.unescape(TAG_RE.sub("", e.group("name"))).strip()
                if not key:
                    self.errors.append(f"glossary <dt>{name}</dt> has no data-term")
                    continue
                if key in entries:
                    self.errors.append(f"glossary {key}: defined twice")
                defs, asks = [], []
                for d in DD_RE.finditer(e.group("dds")):
                    target = asks if "ask" in attrs_of(d.group("attrs")).get("class", "").split() else defs
                    target.append(d.group("inner").strip())
                if not defs:
                    self.errors.append(f"glossary {key}: no definition (<dd> without class=\"ask\")")
                if len(asks) > ASK_MAX:
                    self.warnings.append(f"glossary {key}: {len(asks)} suggested questions, only {ASK_MAX} are shown")
                loc = attrs.get("data-loc")
                # a definition may link code too: expand its data-loc links like the body's
                definition = LOC_RE.sub(lambda x: self.loc(attrs_of(x.group("attrs")), x.group("inner")), " ".join(defs))
                entries[key] = {"name": name or key, "def": definition, "loc": loc,
                                "href": self.checked_href(loc, f"glossary {key}") if loc else None,
                                "ask": [html.unescape(TAG_RE.sub("", q)) for q in asks[:ASK_MAX]]}
            return ""

        body = GLOSSARY_RE.sub(take, body)
        used = set(TERM_RE.findall(body))
        for key in sorted(used - entries.keys()):
            self.errors.append(f'data-term "{key}" has no entry in <dl class="glossary">')
        for key in sorted(entries.keys() - used):
            self.warnings.append(f'glossary {key}: defined but no data-term="{key}" uses it')
        self.stats["terms"] = len(entries)
        if not entries:
            return body
        payload = json.dumps(entries, ensure_ascii=False).replace("</", "<\\/")
        return body + f'\n<script type="application/json" id="glossary-data">{payload}</script>\n'

    def scene(self, m: re.Match) -> str:
        art = textwrap.dedent(html.unescape(m.group("art")).strip("\n"))
        width = max((len(l) for l in art.splitlines()), default=0)
        if width > 72:
            self.warnings.append(f"scene is {width} columns (max 72); narrow it here and in the terminal reply")
        self.stats["scene"] += 1
        return f"{m.group(1)}\n{html.escape(art, quote=False)}{m.group(3)}"

    # -------------------------------------------------------------- build
    def expand(self, body: str) -> str:
        self.referenced = {strip_spec(s).strip("/") for s in REF_RE.findall(body)}
        if not CHANGES_RE.search(body):
            slot = PROOF_RE.search(body)
            tag = "<section data-changes></section>\n"
            body = body[: slot.start()] + tag + body[slot.start():] if slot else body + "\n" + tag
        body = self.glossary(body)
        body = SCENE_RE.sub(self.scene, body)
        body = CHANGES_RE.sub(lambda m: self.inventory(attrs_of(m.group("attrs"))), body)

        def fig(m: re.Match) -> str:
            attrs = attrs_of(m.group("attrs"))
            return self.code_figure(attrs, m.group("inner")) if "data-src" in attrs else self.diff_figure(attrs, m.group("inner"))

        body = FIGURE_RE.sub(fig, body)
        body = LOC_RE.sub(lambda m: self.loc(attrs_of(m.group("attrs")), m.group("inner")), body)
        # browsers break inline code after any hyphen (`--` / `source`); keep short tokens whole
        body = SHORT_CODE_RE.sub(r'<code class="nb">\1</code>', body)
        return body

    def coverage(self) -> str:
        changed = {f.path for f in self.cs.files}
        hit = {p for p in changed if p in self.referenced or (self.cs.get(p) and self.cs.get(p).old_path in self.referenced)}
        miss = sorted(changed - hit)
        line = f"stories cite {len(hit)}/{len(changed)} changed files"
        if not miss:
            return line
        by: OrderedDict[str, list[str]] = OrderedDict()
        for m in miss:
            parent = str(Path(m).parent)
            by.setdefault("./" if parent == "." else parent + "/", []).append(Path(m).name)
        rows = [f"  {d} {', '.join(names)}" for d, names in by.items()]
        return line + "; only under Every change:\n" + "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--body", required=True, help="HTML body fragment you wrote")
    ap.add_argument("--out", default=None, help="extra copy of the page; it is always persisted into the summary tree")
    ap.add_argument("--title", default="Implementation summary", help="page title; also names the files (<stamp>-<slug>)")
    ap.add_argument("--summary", default=None, help="the terminal summary (Markdown file) to persist next to the page")
    ap.add_argument("--project", default=None, help="override the project folder (default: repository name)")
    ap.add_argument("--milestone", default=None, help="override the milestone folder (default: current branch)")
    ap.add_argument("--component", default=None, help="override the component folder (default: most-touched area)")
    ap.add_argument("--source", default=None, help="the real project directory when --repo is a scratch git copy of it: "
                                                    "editor links open it, and project/milestone are named from it "
                                                    "(its branch, or today's date outside git)")
    ap.add_argument("--repo", default=".", help="repo root that directive paths are relative to")
    ap.add_argument("--base", default=None, help="git ref to diff against (default: same auto base as collect_changes.py)")
    ap.add_argument("--paths", nargs="*", default=None, help="scope of Every change; pass the same --paths as collect_changes.py")
    ap.add_argument("--exclude", nargs="*", default=[], help="extra files/dirs/globs to leave out of Every change")
    ap.add_argument("--editor", default=os.environ.get("IMPL_SUMMARY_EDITOR", "vscode"),
                    choices=["vscode", "cursor", "windsurf", "zed", "file", "none"],
                    help="link scheme for file:line links (env IMPL_SUMMARY_EDITOR)")
    ap.add_argument("--meta", default="", help="short footer note, e.g. 'branch feat/x vs main'")
    ap.add_argument("--review-data", nargs="?", const="auto", default=None, metavar="DIR",
                    help="also write review-desk data (manifest.json, diffs/); bare flag = <stem>.review/ next to the page, "
                         "or pass the existing session folder when rebuilding after a review")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    summary = Path(args.summary) if args.summary else None
    source = Path(args.source).expanduser().resolve() if args.source else None
    if source and not source.is_dir():
        print(f"build_page: --source {source} is not a directory", file=sys.stderr)
        return 1
    if summary and not summary.is_file():
        print(f"build_page: --summary {summary} does not exist", file=sys.stderr)
        return 1
    try:
        b = Builder(repo, args.base, args.editor, args.paths, args.exclude, source)
        body = b.expand(Path(args.body).read_text(encoding="utf-8"))
    except RuntimeError as e:
        print(f"build_page: {e}", file=sys.stderr)
        return 2
    for w in b.warnings:
        print(f"build_page: warning: {w}", file=sys.stderr)
    if b.errors:
        print("build_page: refusing to write, fix these references:", file=sys.stderr)
        for e in b.errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    shell = (ASSETS / "shell.html").read_text(encoding="utf-8")
    page = (shell
            .replace("{{TITLE}}", html.escape(args.title))
            .replace("{{META}}", html.escape(args.meta or repo.name))
            .replace("{{GENERATED}}", dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
            .replace("{{CSS}}", (ASSETS / "page.css").read_text(encoding="utf-8"))
            .replace("{{HIGHLIGHT}}", (ASSETS / "highlight.js").read_text(encoding="utf-8"))
            .replace("{{JS}}", (ASSETS / "particles.js").read_text(encoding="utf-8"))
            .replace("{{GLOSSARY}}", (ASSETS / "glossary.js").read_text(encoding="utf-8"))
            .replace("{{BODY}}", body))
    raw_body = Path(args.body).read_text(encoding="utf-8")
    folder = archive.target_dir(b.cs, args.project, args.milestone, args.component, source)
    written, fallback = archive.persist_or_fallback(folder, repo, archive.stem(args.title), page, raw_body,
                                                    json.dumps(to_json(b.cs), indent=1) + "\n", inventory_md(b.cs), summary)
    if fallback:
        folder = written[0].parent
        print(f"build_page: warning: {archive.root_dir()} is not writable (sandbox?); persisted to {fallback} instead. "
              f"From an unsandboxed session run: archive.py sweep {fallback} --move", file=sys.stderr)
    out = written[0]
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page, encoding="utf-8")
    s = b.stats
    print(f"persisted {len(written)} files to {folder}/{written[0].stem}.*"
          + ("" if summary else " (no --summary given: the terminal summary was not saved)"))
    print(f"wrote {out} ({s['scene']} scenes, {s['code']} code, {s['diff']} diffs, {s['loc']} links, "
          f"{s['inventory']} files under Every change, {s['terms']} glossary terms, {len(page) // 1024} KB)")
    print(b.coverage())
    if args.review_data:
        rd = folder / f"{written[0].stem}.review" if args.review_data == "auto" else Path(args.review_data).expanduser()
        n = write_review_data(b, rd)
        print(f"review data: {rd} ({n} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
