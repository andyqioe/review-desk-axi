"""Oracle tests: collect_changes.py and build_page.py must account for every change exactly.

Git is the oracle. The file set and +/- counts must match `git diff` and `ls-files`;
applying each file's hunks to its base content must reproduce the working tree byte for
byte; every context line the page renders must be the same line in the base and the
working tree at the numbers its gutters show.

  python3 -m unittest discover -s tests -v                     # synthetic repo, ~300 files
  IMPL_SUMMARY_TEST_REPO=~/some/repo python3 -m unittest ...   # also check a real repo
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HERE))

# Every build persists into the summary tree; keep test builds out of the real one.
os.environ["IMPL_SUMMARY_ROOT"] = tempfile.mkdtemp(prefix="impl-summary-root-")

import archive  # noqa: E402
import changes  # noqa: E402
import fixture  # noqa: E402

FIG_RE = re.compile(r'<figure class="code"><div class="code-head">(?P<head>.*?)</div>'
                    r'<div class="code-body[^"]*">(?P<gutters>(?:<div class="gutter"[^>]*>.*?</div>)*)'
                    r'<pre><code[^>]*>(?P<code>.*?)</code></pre></div>', re.S)
SPAN_RE = re.compile(r'<span class="([^"]*)">(.*?)</span>', re.S)
LN_RE = re.compile(r'<span class="ln ?([^"]*)">(.*?)</span>(?=<span class="ln|$)', re.S)
DETAILS_RE = re.compile(r'<details class="cfile"[^>]*><summary>(?P<summary>.*?)</summary>(?P<fig>.*?)</details>', re.S)


def git(repo: Path, *args: str, ok=(0,)) -> str:
    r = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=repo, capture_output=True)
    assert r.returncode in ok, r.stderr.decode()
    return r.stdout.decode("utf-8", errors="surrogateescape")


def run(script: str, *args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], cwd=cwd, capture_output=True, text=True)


def base_lines(repo: Path, base: str, rel: str) -> list[str]:
    data = subprocess.run(["git", "show", f"{base}:{rel}"], cwd=repo, capture_output=True).stdout
    return data.decode("utf-8", errors="replace").splitlines()


def apply(old: list[str], hunks: list[changes.Hunk]) -> list[str]:
    """Rebuild the new file from the old one and zero-context hunks."""
    out, cur = [], 0  # cur = number of old lines consumed
    for h in hunks:
        upto = h.old_start - 1 if h.old_len else h.old_start
        out += old[cur:upto]
        removed = [t for s, t in h.lines if s == "-"]
        assert old[upto:upto + h.old_len] == removed, f"hunk #{h.index} removes lines that are not in the base"
        out += [t for s, t in h.lines if s == "+"]
        cur = upto + h.old_len
    return out + old[cur:]


def unescape(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).replace("\u200b", "")


def parse_figure(fig: str) -> list[tuple[str, str, str, str]]:
    m = FIG_RE.search(fig)
    assert m, "no code figure"
    code = LN_RE.findall(m.group("code"))
    # an empty gutter cell collapses to zero height and shifts every later line number
    assert not re.search(r'<span class="[^"]*"></span>', m.group("gutters")), "empty gutter cell"
    cols = dict(re.findall(r'<div class="gutter" data-col="(old|new)">(.*?)</div>', m.group("gutters"), re.S))
    blank = [("", "")] * len(code)
    gutters = [SPAN_RE.findall(cols[c]) if c in cols else blank for c in ("old", "new")]
    assert len(gutters[0]) == len(gutters[1]) == len(code), "gutter and code rows disagree"
    return [(unescape(o[1]), unescape(n[1]), unescape(t), c.strip()) for o, n, (c, t) in zip(gutters[0], gutters[1], code)]


class OracleMixin:
    """Checks shared by the synthetic and the real repo."""

    repo: Path
    cs: changes.ChangeSet

    def truth_paths(self) -> set[str]:
        raw = git(self.repo, "diff", "-z", "--name-status", "-M", self.cs.base).split("\0")
        paths, i = set(), 0
        while i < len(raw):
            st = raw[i]
            if not st:
                i += 1
                continue
            if st[0] in "RC":
                paths.add(raw[i + 2])
                i += 3
            else:
                paths.add(raw[i + 1])
                i += 2
        for p in git(self.repo, "ls-files", "-z", "--others", "--exclude-standard").split("\0"):
            if p:
                paths.add(p.rstrip("/"))
        paths = {p for p in paths if not p.startswith(".lavish/")}
        for f in self.cs.files:
            if f.paired:
                paths.discard(f.old_path)
        return paths

    def check_file_set(self):
        got = [f.path for f in self.cs.files]
        self.assertEqual(len(got), len(set(got)), "a file is listed twice")
        self.assertEqual(set(got), self.truth_paths())

    def check_counts(self):
        numstat = {}
        raw = git(self.repo, "diff", "-z", "--numstat", "-M", self.cs.base).split("\0")
        i = 0
        while i < len(raw):
            if not raw[i]:
                i += 1
                continue
            a, d, path = raw[i].split("\t")
            if path == "":  # rename: old and new follow
                path = raw[i + 2]
                i += 3
            else:
                i += 1
            numstat[path] = (a, d)
        for f in self.cs.files:
            with self.subTest(path=f.path):
                if f.status == "?" and not f.paired:
                    if f.binary or f.symlink is not None or f.nested_repo:
                        self.assertEqual(f.hunks, [])
                    else:
                        self.assertEqual((f.adds, f.dels), (len(f.new_lines), 0))
                    continue
                if f.paired:
                    continue  # checked by reconstruction
                a, d = numstat[f.path]
                if f.binary:
                    self.assertEqual((a, d), ("-", "-"))
                elif not f.submodule:
                    self.assertEqual((str(f.adds), str(f.dels)), (a, d))

    def check_reconstruction(self):
        for f in self.cs.files:
            if f.binary or f.symlink is not None or f.old_symlink is not None or f.nested_repo or f.submodule:
                continue
            with self.subTest(path=f.path):
                old = [] if f.status in "A?" and not f.paired else base_lines(self.repo, self.cs.base, f.old_path or f.path)
                if f.paired:
                    old = base_lines(self.repo, self.cs.base, f.old_path)
                new = [] if f.status == "D" else (self.repo / f.path).read_bytes().decode("utf-8", "replace").splitlines()
                old = [l.rstrip("\r") for l in old]
                new = [l.rstrip("\r") for l in new]
                self.assertEqual(apply(old, f.hunks), new)
                for h in f.hunks:  # the printed new-side range holds exactly the added lines
                    if h.new_len:
                        self.assertEqual(new[h.new_start - 1:h.new_end], [t for s, t in h.lines if s == "+"])

    def check_inventory_render(self, page: str):
        rows = DETAILS_RE.findall(page)
        self.assertEqual(len(rows), len(self.cs.files), "Every change must hold one row per changed file")
        by_name = {}
        for summary, fig in rows:
            by_name.setdefault(unescape(re.search(r'<span class="cname">(.*?)</span>', summary).group(1)), []).append(fig)
        for f in self.cs.files:
            with self.subTest(path=f.path):
                figs = by_name.get(Path(f.path).name, [])
                fig = next((g for g in figs if html.escape(f.path) in g), None)
                self.assertIsNotNone(fig, "file missing from Every change")
                parsed = parse_figure(fig)
                adds = sum(1 for r in parsed if r[3] == "add")
                dels = sum(1 for r in parsed if r[3] == "del")
                total = sum(len(h.lines) for h in f.hunks)
                if total <= 4000:
                    self.assertEqual((adds, dels), (f.adds, f.dels), "rendered +/- rows differ from the change")
                self.check_context_rows(f, parsed)

    def check_context_rows(self, f: changes.FileChange, parsed):
        """Every unchanged row is the same text in base and working tree at its gutter numbers."""
        ctx = [r for r in parsed if r[3] == ""]
        if not ctx:
            return
        old = [l.rstrip("\r") for l in base_lines(self.repo, self.cs.base, f.old_path or f.path)]
        new = [l.rstrip("\r") for l in (f.new_lines or [])]
        changed = set()
        for h in f.hunks:
            changed.update(range(h.new_start, h.new_start + h.new_len))
        for o, n, text, _ in ctx:
            o, n = int(o), int(n)
            self.assertNotIn(n, changed, f"changed line {n} shown as context")
            self.assertEqual(new[n - 1], text)
            self.assertEqual(old[o - 1], text, f"old line number {o} is wrong")


class SyntheticRepo(OracleMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="impl-summary-test-"))
        cls.repo = cls.tmp / "repo"
        cls.facts = fixture.build(cls.repo, scale=int(os.environ.get("IMPL_SUMMARY_SCALE", "240")))
        cls.cs = changes.collect(cls.repo)
        cls.body = cls.tmp / "body.html"
        cls.body.write_text('<header class="hero"><h1>t</h1></header>\n'
                            '<section class="proof"><div class="section-head"><h2>Proof</h2></div></section>\n')
        r = run("build_page.py", "--body", str(cls.body), "--out", str(cls.tmp / "page.html"), "--editor", "none", cwd=cls.repo)
        assert r.returncode == 0, r.stderr
        cls.build_out = r.stdout
        cls.page = (cls.tmp / "page.html").read_text()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def build(self, body: str, *extra: str) -> tuple[subprocess.CompletedProcess, str]:
        b = self.tmp / "b.html"
        b.write_text(body)
        out = self.tmp / "p.html"
        if out.exists():
            out.unlink()
        r = run("build_page.py", "--body", str(b), "--out", str(out), "--editor", "none", *extra, cwd=self.repo)
        return r, out.read_text() if out.exists() else ""

    # ---------------------------------------------------------- collect
    def test_scale(self):
        scale = int(os.environ.get("IMPL_SUMMARY_SCALE", "240"))
        self.assertGreater(len(self.cs.files), scale // 2)

    def test_file_set_matches_git(self):
        self.check_file_set()

    def test_counts_match_numstat(self):
        self.check_counts()

    def test_hunks_rebuild_working_tree(self):
        self.check_reconstruction()

    def test_default_base_spans_committed_staged_unstaged_untracked(self):
        paths = {f.path for f in self.cs.files}
        for group in ("committed", "staged", "unstaged"):
            self.assertTrue(set(self.facts[group]) <= paths, group)
        self.assertIn("src/new_pkg/deep/fresh.py", paths)
        self.assertTrue(self.cs.base_label.startswith("merge-base"))

    def test_edge_cases(self):
        get = self.cs.get
        r = get("src/util/new_name.py")
        self.assertEqual((r.status, r.old_path, r.adds, r.dels), ("R", "src/util/old_name.py", 1, 1))
        self.assertEqual((get("src/helpers/moved.py").status, get("src/helpers/moved.py").adds), ("R", 0))
        self.assertTrue(get("logo.png").binary)
        self.assertEqual((get("logo.png").old_size, get("logo.png").new_size), (1024, 1024))
        self.assertTrue(get("src/util/gone_binary.bin").binary)
        self.assertIn("now executable", " ".join(get("script.sh").notes()))
        self.assertEqual(get("typechange").symlink, "src/util/new_name.py")
        self.assertEqual(get("spaced dir/naïve file.py").adds, 1)
        self.assertIn("empty file", get("src/new_pkg/empty.py").notes())
        self.assertEqual(get("src/new_pkg/link_to_lib").symlink, "../lib")
        self.assertTrue(get("vendor/nested").nested_repo)
        self.assertIsNone(get(".lavish/impl-summary-old.html"))
        self.assertIsNone(get("ignored.log"))
        app = get("web/app.ts")
        self.assertEqual((app.hunks[0].new_start, app.hunks[0].new_len, app.hunks[0].dels), (0, 0, 8))

    def test_origins(self):
        get = self.cs.get
        for origin in ("committed", "staged", "unstaged"):
            for rel in self.facts[origin]:
                self.assertIn(origin, get(rel).origins, rel)
        self.assertEqual(get("src/util/new_name.py").origins, ["committed"])
        self.assertEqual(get("src/util/to_delete.py").origins, ["staged"])
        self.assertEqual(get("src/util/to_delete_unstaged.py").origins, ["unstaged"])
        self.assertEqual(get("src/new_pkg/deep/fresh.py").origins, ["untracked"])
        self.assertTrue(all(f.origins for f in self.cs.files), "every file needs an origin")
        both = [f for f in self.cs.files if {"committed", "unstaged"} <= set(f.origins)]
        self.assertTrue(both, "fixture edits some committed files again; they must carry both origins")

    def test_page_origin_filters(self):
        for o in ("committed", "staged", "unstaged", "untracked"):
            n = sum(o in f.origins for f in self.cs.files)
            self.assertIn(f'data-origin="{o}" aria-pressed="false" title="show only {o} changes">{n} {o}</button>', self.page)
            self.assertEqual(len(re.findall(rf'<details class="cfile" [^>]*data-origins="[^"]*\b{o}\b', self.page)), n)

    def test_plain_mv_is_paired_as_rename(self):
        old, new = self.facts["paired"]
        f = self.cs.get(new)
        self.assertTrue(f.paired)
        self.assertEqual((f.old_path, f.adds, f.dels), (old, 1, 1))
        self.assertNotIn(old, {g.path for g in self.cs.files})

    def test_scoped_rename_keeps_real_diff(self):
        cs = changes.collect(self.repo, paths=["src/util/new_name.py"])
        self.assertEqual([(f.path, f.status, f.adds) for f in cs.files], [("src/util/new_name.py", "R", 1)])

    def test_paths_relative_to_subdirectory_cwd(self):
        r = run("collect_changes.py", "--format", "json", "--paths", "new_name.py", cwd=self.repo / "src/util")
        self.assertEqual([f["path"] for f in json.loads(r.stdout)["files"]], ["src/util/new_name.py"])

    def test_glob_scope_and_explicit_lavish(self):
        cs = changes.collect(self.repo, paths=["pkg1/*/*.rs"])
        self.assertTrue(cs.files and all(f.path.startswith("pkg1/") and f.path.endswith(".rs") for f in cs.files))
        cs = changes.collect(self.repo, paths=[".lavish"])
        self.assertEqual([f.path for f in cs.files], [".lavish/impl-summary-old.html"])

    def test_map_lists_every_file_and_hunk_once(self):
        out = run("collect_changes.py", cwd=self.repo).stdout
        for f in self.cs.files:
            self.assertEqual(out.count(f"\n## {f.path}  ["), 1, f.path)
            if f.status in "MRT" or f.paired:
                for h in f.hunks:
                    self.assertIn(f"   #{h.index} {f.path}:", out)

    def test_inventory_links_land_inside_the_file(self):
        out = run("collect_changes.py", "--format", "inventory", cwd=self.repo).stdout
        refs = re.findall(r"^- `([^`]+?):(\d+)`", out, re.M)
        self.assertTrue(refs)
        for path, line in refs:
            with self.subTest(path=path):
                n = len((self.repo / path).read_bytes().decode("utf-8", "replace").splitlines())
                self.assertTrue(1 <= int(line) <= n, f"{path}:{line} but the file has {n} lines")
        for bare in ("src/new_pkg/empty.py", "logo.png", "script.sh", "src/util/to_delete.py"):
            self.assertIn(f"- `{bare}` ", out)

    def test_inventory_lists_every_file(self):
        out = run("collect_changes.py", "--format", "inventory", cwd=self.repo).stdout
        items = [l for l in out.splitlines() if l.startswith("- `")]
        self.assertEqual(len(items), len(self.cs.files))
        for f in self.cs.files:
            self.assertEqual(sum(1 for l in items if l.startswith(f"- `{f.path}:") or l.startswith(f"- `{f.path}`")), 1, f.path)
        self.assertTrue(out.startswith("<details>") and out.rstrip().endswith("</details>"))

    # ------------------------------------------------------------ page
    def test_page_renders_every_change(self):
        self.check_inventory_render(self.page)

    def test_inventory_inserted_before_proof(self):
        self.assertLess(self.page.index('id="every-change"'), self.page.index('class="proof"'))
        self.assertIn("stories cite 0/", self.build_out)

    def test_data_hunks_match_collect_numbering(self):
        f = max(self.cs.files, key=lambda f: len(f.hunks) if f.status == "M" else 0)
        self.assertGreaterEqual(len(f.hunks), 3)
        for k in (1, len(f.hunks) // 2, len(f.hunks)):
            with self.subTest(hunk=k):
                r, page = self.build(f'<figure data-diff="{f.path}" data-hunks="{k}"></figure>')
                self.assertEqual(r.returncode, 0, r.stderr)
                story = page[:page.index('id="every-change"')]
                rows = parse_figure(story)
                h = f.hunks[k - 1]
                self.assertEqual([(r[3], r[2]) for r in rows if r[3] in ("add", "del")],
                                 [("add" if s == "+" else "del", t) for s, t in h.lines])
                self.assertTrue(rows[0][2].startswith(f"#{k} "))
                self.check_context_rows(f, rows)

    def test_full_diff_has_no_cut_and_no_empty_headers(self):
        f = max(self.cs.files, key=lambda f: f.adds + f.dels if f.status == "M" else 0)
        r, page = self.build(f'<figure data-diff="{f.path}"></figure>')
        rows = parse_figure(page)
        self.assertEqual(sum(1 for x in rows if x[3].startswith("hunk")), len(f.hunks))
        self.assertFalse(any("cut at" in x[2] for x in rows))
        for i, x in enumerate(rows):
            if x[3].startswith("hunk"):
                self.assertTrue(i + 1 < len(rows) and not rows[i + 1][3].startswith("hunk"), "hunk header with no body")
                joined = x[3] == "hunk join"
                if i:  # a joined header must sit between consecutive new-side line numbers
                    prev_new = next((int(r[1]) for r in reversed(rows[:i]) if r[1]), None)
                    next_new = next((int(r[1]) for r in rows[i + 1:] if r[1]), None)
                    if joined and prev_new and next_new:
                        self.assertEqual(next_new, prev_new + 1, "joined header hides lines")

    def test_data_max_cut_is_labelled(self):
        f = max(self.cs.files, key=lambda f: len(f.hunks) if f.status == "M" else 0)
        r, page = self.build(f'<figure data-diff="{f.path}" data-max="20"></figure>')
        rows = parse_figure(page)
        self.assertLessEqual(len([x for x in rows if x[3] != "meta"]), 21)
        self.assertIn("cut at data-max=20", rows[-1][2])
        self.assertIn(f"#{len(f.hunks)}", rows[-1][2])

    def test_special_files_render(self):
        for path, needle in [("src/util/to_delete.py", 'class="ln del"'), ("logo.png", "binary"),
                             ("src/util/new_name.py", "src/util/old_name.py → src/util/new_name.py"),
                             ("typechange", "symlink -&gt; src/util/new_name.py"),
                             ("src/new_pkg/deep/fresh.py", 'class="ln add"'), ("script.sh", "now executable")]:
            with self.subTest(path=path):
                r, page = self.build(f'<figure data-diff="{path}"></figure>')
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn(needle, page[:page.index('id="every-change"')])

    def test_stale_references_refuse_to_build(self):
        f = self.cs.get("lib/engine.rs")
        for body, err in [(f'<figure data-diff="lib/engine.rs" data-hunks="{len(f.hunks) + 1}"></figure>', "out of range"),
                          ('<figure data-diff="nope.py"></figure>', "missing file"),
                          ('<figure data-diff="pkg0/sub0/m0.py"></figure>' if not self.cs.get("pkg0/sub0/m0.py") else
                           '<figure data-diff=".gitignore-nope"></figure>', "data-diff"),
                          ('<figure data-src="lib/engine.rs:1-99999"></figure>', "out of bounds")]:
            with self.subTest(body=body):
                r, page = self.build(body)
                self.assertEqual(r.returncode, 1)
                self.assertIn(err, r.stderr)

    def test_story_references_are_marked_and_counted(self):
        r, page = self.build('<p><a data-loc="lib/engine.rs:44"></a> <figure data-diff="web/app.ts"></figure></p>')
        self.assertIn(f"stories cite 2/{len(self.cs.files)}", r.stdout)
        self.assertEqual(page.count('class="cref"'), 2)

    def test_every_build_is_persisted(self):
        root = Path(os.environ["IMPL_SUMMARY_ROOT"])
        summary = self.tmp / "summary.md"
        summary.write_text("# recap\n")
        b = self.tmp / "persist-body.html"
        b.write_text("<p>persist</p>")
        r = run("build_page.py", "--body", str(b), "--title", "Persist Check", "--summary", str(summary),
                "--editor", "none", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        folder = root / "repo" / "feature" / archive.component_name(self.cs)
        stems = {p.name.split("-persist-check")[0] for p in folder.glob("*-persist-check*")}
        self.assertEqual(len(stems), 1, list(folder.iterdir()) if folder.exists() else "missing folder")
        names = sorted(p.name.split("-persist-check")[1] for p in folder.glob("*-persist-check*"))
        self.assertEqual(names, sorted(archive.SUFFIXES))
        data = json.loads(next(folder.glob("*-persist-check.changes.json")).read_text())
        self.assertEqual(len(data["files"]), len(self.cs.files))
        self.assertIn("persist-check.html", (root / "repo" / "INDEX.md").read_text())
        self.assertIn(str(folder), r.stdout)

    def test_glossary_becomes_page_data_and_is_checked_both_ways(self):
        body = ('<p>The <span data-term="claim">claim</span> holds.</p>'
                '<dl class="glossary"><dt data-term="claim" data-loc="lib/engine.rs:44">claim</dt>'
                '<dd>Proof of <code>ownership</code>, see <a data-loc="web/app.ts:1">app</a>.</dd>'
                '<dd class="ask">Who takes it over?</dd></dl>')
        r, page = self.build(body)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1 glossary terms", r.stdout)
        self.assertNotIn('<dl class="glossary">', page)  # the list is data, not prose
        raw = re.search(r'<script type="application/json" id="glossary-data">(.*?)</script>', page, re.S).group(1)
        entry = json.loads(raw)["claim"]
        self.assertEqual((entry["name"], entry["loc"], entry["ask"]), ("claim", "lib/engine.rs:44", ["Who takes it over?"]))
        self.assertIn('class=\\"loc\\"', raw)  # links inside a definition are expanded too
        self.assertIn("gloss-card", page)  # glossary.js is in the shell
        for bad, err in [('<span data-term="nope">x</span>', 'data-term "nope" has no entry'),
                         ('<span data-term="a">a</span><dl class="glossary"><dt data-term="a" data-loc="lib/engine.rs:99999">a</dt><dd>d</dd></dl>',
                          "glossary a lib/engine.rs:99999: line out of bounds"),
                         ('<span data-term="a">a</span><dl class="glossary"><dt data-term="a">a</dt><dd class="ask">q?</dd></dl>',
                          "glossary a: no definition")]:
            with self.subTest(bad=bad):
                r, _ = self.build(bad)
                self.assertEqual(r.returncode, 1)
                self.assertIn(err, r.stderr)
        r, _ = self.build('<dl class="glossary"><dt data-term="unused">u</dt><dd>d</dd></dl>')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("glossary unused: defined but no", r.stderr)

    def test_glossary_payload_cannot_close_its_script(self):
        r, page = self.build('<span data-term="t">t</span><dl class="glossary"><dt data-term="t">t</dt><dd>a </script> b</dd></dl>')
        self.assertEqual(r.returncode, 0, r.stderr)
        raw = re.search(r'id="glossary-data">(.*?)</script>', page, re.S).group(1)
        self.assertEqual(json.loads(raw)["t"]["def"], "a </script> b")

    def test_short_inline_code_never_splits(self):
        r, page = self.build("<p>pass <code>--source</code> or <code>" + "x" * 40 + "</code></p>")
        self.assertIn('<code class="nb">--source</code>', page)
        self.assertIn("<code>" + "x" * 40 + "</code>", page)  # long tokens may still wrap

    def test_scoped_page(self):
        r, page = self.build("", "--paths", "lib", "web")
        self.assertEqual(len(DETAILS_RE.findall(page)), 2)


class Symbols(unittest.TestCase):
    def test_markdown_headings_skip_fences(self):
        doc = ["# Top", "", "````markdown", "### Fake", "```text", "## Also fake", "```", "````", "## Real", "text"]
        self.assertEqual(changes.md_headings(doc), {1: "Top", 9: "Real"})
        self.assertEqual(changes.enclosing(doc, 6, ".md"), ("Top", 1))
        self.assertEqual(changes.enclosing(doc, 10, ".md"), ("Real", 9))

    def test_blank_start_line_is_judged_by_the_next_line(self):
        src = ["def first():", "    return 1", "", "", "# section", "def second():", "    pass"]
        self.assertIsNone(changes.enclosing(src, 3, ".py"))           # module level, not inside first()
        self.assertEqual(changes.enclosing(src, 2, ".py"), ("first", 1))

    def test_brace_language_methods(self):
        self.assertEqual(changes.symbol_at("  async load(id: string): Promise<void> {", ".ts"), "load")
        self.assertIsNone(changes.symbol_at("  if (ready) {", ".ts"))
        self.assertEqual(changes.symbol_at("pub(crate) const fn limit() -> u32 {", ".rs"), "limit")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class Highlight(unittest.TestCase):
    def split(self, html_in: str) -> list[str]:
        js = (f"const {{ splitLines }} = require({json.dumps(str(HERE.parent / 'assets' / 'highlight.js'))});"
              "console.log(JSON.stringify(splitLines(process.argv[1])));")
        r = subprocess.run(["node", "-e", js, html_in], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_open_spans_carry_across_lines(self):
        s = '<span class="hljs-string">"""a\nb\nc"""</span>\nx = <span class="hljs-number">1</span>'
        self.assertEqual(self.split(s), ['<span class="hljs-string">"""a</span>', '<span class="hljs-string">b</span>',
                                         '<span class="hljs-string">c"""</span>', 'x = <span class="hljs-number">1</span>'])

    def test_nested_spans_and_blank_lines(self):
        s = '<span class="a">x<span class="b">y\n\nz</span>w</span>'
        self.assertEqual(self.split(s), ['<span class="a">x<span class="b">y</span></span>',
                                         '<span class="a"><span class="b"></span></span>',
                                         '<span class="a"><span class="b">z</span>w</span>'])

    def test_empty_rows_keep_their_height(self):
        js = (f"const {{ keepHeight }} = require({json.dumps(str(HERE.parent / 'assets' / 'highlight.js'))});"
              "console.log(JSON.stringify(['<span class=\"s\"></span>', '', '  ', '<span>x</span>'].map(keepHeight)));")
        r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
        self.assertEqual(json.loads(r.stdout), ['<span class="s"></span>\u200b', "\u200b", "  ", "<span>x</span>"])

    def test_escaped_text_is_untouched(self):
        self.assertEqual(self.split("a &lt; b\nc &amp;&amp; d"), ["a &lt; b", "c &amp;&amp; d"])


class Adopt(unittest.TestCase):
    """Filing pages built before the tree: project, milestone and component come from the page."""

    @staticmethod
    def page(path: Path, title: str, when: str, links: list[str]) -> Path:
        a = "".join(f'<a href="vscode://file{l}:3">x</a>' for l in links)
        path.write_text(f"<html><title>{title}</title><main>{a}<footer><span>generated {when}</span></footer></main></html>")
        return path

    def test_adopt_reads_branch_at_build_time_and_skips_copies(self):
        tmp = Path(tempfile.mkdtemp(prefix="impl-summary-adopt-")).resolve()
        saved = os.environ["IMPL_SUMMARY_ROOT"]
        os.environ["IMPL_SUMMARY_ROOT"] = str(tmp / "root")
        try:
            repo = tmp / "proj"
            repo.mkdir()
            env = dict(os.environ)

            def g(*args, when="2026-01-01T09:00:00"):
                subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                               env={**env, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when})
            g("init", "-q", "-b", "main")
            g("config", "user.email", "t@t")
            g("config", "user.name", "t")
            (repo / "src" / "ui").mkdir(parents=True)
            (repo / "src" / "ui" / "a.ts").write_text("x\n")
            g("add", "-A")
            g("commit", "-qm", "base")
            g("checkout", "-qb", "feat/popover", when="2026-01-01T10:00:00")
            g("checkout", "-q", "main", when="2026-01-01T12:00:00")
            ui = str(repo / "src" / "ui" / "a.ts")
            early = self.page(tmp / "impl-summary-early.html", "Popover &amp; tips", "2026-01-01 11:00", [ui, ui])
            late = self.page(tmp / "impl-summary-late.html", "After merge", "2026-01-01 13:00", [ui])
            self.assertEqual(archive.adopt(early, dry_run=True)[0], "would file")
            self.assertFalse((tmp / "root").exists(), "dry run wrote files")
            action, path = archive.adopt(early)
            self.assertEqual(action, "filed")
            self.assertEqual(path, tmp / "root" / "proj" / "feat-popover" / "src-ui" / "20260101-1100-popover-tips.html")
            self.assertEqual(archive.adopt(late)[1].parent.parent.name, "main")
            self.assertEqual(archive.adopt(early)[0], "already filed")
            self.assertIn("feat-popover/src-ui/20260101-1100-popover-tips.html", (tmp / "root" / "proj" / "INDEX.md").read_text())

            skill = tmp / "skills" / "my-skill"
            (skill / "hooks").mkdir(parents=True)
            (skill / "SKILL.md").write_text("x\n")
            (skill / "hooks" / "h.py").write_text("x\n")
            hooks = self.page(tmp / "impl-summary-hooks.html", "Hooks", "2026-02-03 08:05", [str(skill / "hooks" / "h.py")])
            self.assertEqual(archive.adopt(hooks)[1], tmp / "root" / "my-skill" / "2026-02-03" / "hooks" / "20260203-0805-hooks.html")

            bare = self.page(tmp / "impl-summary-bare.html", "Bare", "2026-02-03 08:05", [])
            with self.assertRaises(RuntimeError):
                archive.adopt(bare)
        finally:
            os.environ["IMPL_SUMMARY_ROOT"] = saved
            shutil.rmtree(tmp, ignore_errors=True)


class SourceAndFallback(unittest.TestCase):
    """Scratch copies name the real project; an unwritable tree falls back into the repo, then sweeps."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="impl-summary-src-")).resolve()
        self.saved = os.environ["IMPL_SUMMARY_ROOT"]
        self.repo = self.tmp / "scratch"
        self.repo.mkdir()
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            git(self.repo, *args)
        (self.repo / "lib").mkdir()
        (self.repo / "lib" / "a.py").write_text("x = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "checkout", "-qb", "scratch-branch")
        (self.repo / "lib" / "a.py").write_text("x = 2\n")
        self.body = self.tmp / "body.html"
        self.body.write_text('<p><a data-loc="lib/a.py:1"></a></p>')

    def tearDown(self):
        os.environ["IMPL_SUMMARY_ROOT"] = self.saved
        for d in self.tmp.rglob("*"):
            if d.is_dir():
                d.chmod(0o755)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, *extra: str) -> subprocess.CompletedProcess:
        return run("build_page.py", "--body", str(self.body), "--title", "T", *extra, cwd=self.repo)

    def test_source_names_project_and_opens_real_files(self):
        real = self.tmp / "real-skill"
        real.mkdir()
        os.environ["IMPL_SUMMARY_ROOT"] = str(self.tmp / "root")
        r = self.build("--source", str(real), "--editor", "vscode")
        self.assertEqual(r.returncode, 0, r.stderr)
        today = dt.date.today().isoformat()
        page = next((self.tmp / "root" / "real-skill" / today / "lib").glob("*-t.html")).read_text()
        self.assertIn(f"vscode://file{real}/lib/a.py:1", page)
        self.assertNotIn(f"vscode://file{self.repo}/", page)

    def test_unwritable_tree_falls_back_then_sweeps(self):
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        os.environ["IMPL_SUMMARY_ROOT"] = str(locked)
        r = self.build("--editor", "none")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not writable", r.stderr)
        fallback = self.repo / ".lavish" / "implementation-summaries"
        files = sorted(p.relative_to(fallback).as_posix() for p in fallback.rglob("*-t.*"))
        self.assertEqual(len(files), 4)
        self.assertTrue(all(f.startswith("scratch/scratch-branch/lib/") for f in files), files)
        os.environ["IMPL_SUMMARY_ROOT"] = str(self.tmp / "central")
        counts = archive.sweep(fallback, move=True)
        self.assertEqual(counts, {"copied": 4, "identical": 0})
        self.assertFalse(fallback.exists(), "move leaves no fallback tree behind")
        self.assertEqual(sorted(p.relative_to(self.tmp / "central").as_posix() for p in (self.tmp / "central").rglob("*-t.*")), files)
        self.assertIn("scratch-branch/lib/", (self.tmp / "central" / "scratch" / "INDEX.md").read_text())


class UnbornRepo(unittest.TestCase):
    """A repository before its first commit: no HEAD, so the base is git's empty tree."""

    def test_collect_and_build_before_first_commit(self):
        tmp = Path(tempfile.mkdtemp(prefix="impl-summary-unborn-"))
        try:
            git(tmp, "init", "-q", "-b", "main")
            (tmp / "staged.txt").write_text("a\nb\n")
            git(tmp, "add", "staged.txt")
            (tmp / "staged.txt").write_text("a\nB\nc\n")
            (tmp / "new.txt").write_text("x\n")
            cs = changes.collect(tmp)
            self.assertEqual(cs.base_label, "empty tree (no commits yet)")
            self.assertEqual([(f.path, f.adds, f.origins) for f in cs.files],
                             [("new.txt", 1, ["untracked"]), ("staged.txt", 3, ["staged", "unstaged"])])
            self.assertEqual(archive.milestone_name(tmp), "main")
            body = Path(tempfile.mkdtemp(prefix="impl-summary-body-")) / "body.html"  # outside the repo
            body.write_text('<figure data-diff="staged.txt"></figure>')
            r = run("build_page.py", "--body", str(body), "--editor", "none", cwd=tmp)
            shutil.rmtree(body.parent, ignore_errors=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("2 files under Every change", r.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Archive(unittest.TestCase):
    """Naming and layout of <root>/<project>/<milestone>/<component>/."""

    @staticmethod
    def cs(weights: dict[str, int]) -> changes.ChangeSet:
        files = [changes.FileChange(p, "M", hunks=[changes.Hunk(1, 1, 0, 1, w, "", [("+", "x")] * w)]) for p, w in weights.items()]
        return changes.ChangeSet(Path("."), "HEAD", "HEAD", files, [])

    def test_component_is_the_most_touched_area(self):
        name = archive.component_name
        self.assertEqual(name(self.cs({"scripts/a/x.py": 80, "scripts/a/y.py": 10, "src/z.rs": 10})), "scripts-a")
        self.assertEqual(name(self.cs({"scripts/a/x.py": 40, "scripts/b/y.py": 40, "src/z.rs": 20})), "scripts")
        self.assertEqual(name(self.cs({"a/b/c/d/e.py": 5})), "a-b-c")           # capped at depth 3
        self.assertEqual(name(self.cs({"README.md": 50, "docs/x.md": 10})), "root")
        self.assertEqual(name(self.cs({"src/x.rs": 10, "README.md": 3})), "src")

    def test_tree_layout_index_and_worktree_project(self):
        tmp = Path(tempfile.mkdtemp(prefix="impl-summary-archive-"))
        try:
            repo = tmp / "my-proj"
            repo.mkdir()
            for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
                git(repo, *args)
            (repo / "lib").mkdir()
            (repo / "lib" / "a.py").write_text("x = 1\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "base")
            git(repo, "checkout", "-qb", "feat/Big_Thing")
            (repo / "lib" / "a.py").write_text("x = 2\n")
            wt = tmp / "wt"
            git(repo, "worktree", "add", "-q", "--detach", str(wt))
            self.assertEqual(archive.project_name(wt), "my-proj")
            self.assertTrue(archive.milestone_name(wt).startswith("detached-"))
            cs = changes.collect(repo)
            saved = os.environ["IMPL_SUMMARY_ROOT"]
            os.environ["IMPL_SUMMARY_ROOT"] = str(tmp / "root")
            try:
                folder = archive.target_dir(cs)
                self.assertEqual(folder, tmp / "root" / "my-proj" / "feat-big_thing" / "lib")
                summary = tmp / "s.md"
                summary.write_text("# s\n")
                noon = dt.datetime(2026, 10, 1, 12, 0)  # fixed, so "a minute later" never crosses midnight
                written = archive.persist(folder, archive.stem("Big Thing!", noon), "<html>", "<b>", "{}", "inv", summary)
                self.assertEqual(sorted(p.name[14:] for p in written),
                                 sorted(f"big-thing{s}" for s in archive.SUFFIXES))
                later = noon + dt.timedelta(minutes=1)
                again = archive.persist(folder, archive.stem("Big Thing!", later), "<html>2", "<b>", "{}", "inv", None)
                self.assertEqual(sorted(p.name for p in folder.iterdir()), sorted(p.name for p in again),
                                 "a same-day rebuild must replace the earlier set")
                other = archive.persist(folder, archive.stem("Big Thing!", later + dt.timedelta(days=1)), "<h>", "", "{}", "", None)
                self.assertEqual(len(list(folder.iterdir())), len(again) + len(other), "a later day keeps both")
                archive.persist(folder, archive.stem("Big Thing Two", later), "<h>", "", "{}", "", None)
                self.assertTrue(all(p.exists() for p in again), "a different title is never replaced")
                index = (tmp / "root" / "my-proj" / "INDEX.md").read_text()
                self.assertIn("## feat-big_thing", index)
                self.assertIn("feat-big_thing/lib/", index)
                self.assertNotIn(".body.html", index)
            finally:
                os.environ["IMPL_SUMMARY_ROOT"] = saved
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(os.environ.get("IMPL_SUMMARY_TEST_REPO"), "set IMPL_SUMMARY_TEST_REPO to check a real repo")
class RealRepo(OracleMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Path(os.environ["IMPL_SUMMARY_TEST_REPO"]).expanduser().resolve()
        cls.cs = changes.collect(cls.repo, os.environ.get("IMPL_SUMMARY_TEST_BASE"))
        cls.tmp = Path(tempfile.mkdtemp(prefix="impl-summary-real-"))
        (cls.tmp / "body.html").write_text("")
        extra = ["--base", os.environ["IMPL_SUMMARY_TEST_BASE"]] if os.environ.get("IMPL_SUMMARY_TEST_BASE") else []
        r = run("build_page.py", "--body", str(cls.tmp / "body.html"), "--out", str(cls.tmp / "page.html"),
                "--editor", "none", *extra, cwd=cls.repo)
        assert r.returncode == 0, r.stderr
        cls.page = (cls.tmp / "page.html").read_text()
        print(f"\nreal repo: {len(cls.cs.files)} files +{cls.cs.adds} -{cls.cs.dels}; {r.stdout.splitlines()[0]}", file=sys.stderr)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_file_set_matches_git(self):
        self.check_file_set()

    def test_counts_match_numstat(self):
        self.check_counts()

    def test_hunks_rebuild_working_tree(self):
        self.check_reconstruction()

    def test_page_renders_every_change(self):
        self.check_inventory_render(self.page)


if __name__ == "__main__":
    unittest.main()
