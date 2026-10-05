"""Validates the eval set itself: a scripted golden run must pass every check, and a run that
does nothing must fail most of them (so no eval is non-discriminating).

  python3 -m unittest evals/test_harness.py      (from the skill directory)
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = [sys.executable, str(HERE / "harness.py")]
CLI = [sys.executable, str(HERE.parent / "scripts" / "review_desk.py")]


def env_of(run: Path) -> dict:
    import os
    return {**os.environ, **dict(l.split("=", 1) for l in (run / "env").read_text().split())}


class Run:
    def __init__(self, eval_id: int, root: Path, tag: str):
        self.id, self.run = eval_id, root / f"e{eval_id}-{tag}"
        out = subprocess.run([*HARNESS, "setup", str(eval_id), "--run", str(self.run)], capture_output=True, text=True, check=True)
        self.sid = json.loads(out.stdout)["sid"]
        self.repo = self.run / "repo"
        self.driver = None

    def rd(self, *args, stdin=None, role=None) -> str:
        env = {**env_of(self.run), **({"REVIEW_DESK_ROLE": role} if role else {})}
        p = subprocess.run([*CLI, *args], env=env, cwd=self.repo, capture_output=True, text=True, input=stdin, timeout=120)
        assert p.returncode == 0, f"{args}: {p.stdout}{p.stderr}"
        return p.stdout

    def drive(self):
        self.driver = subprocess.Popen([*HARNESS, "drive", str(self.id), "--run", str(self.run), "--timeout", "60"])

    def final(self, text: str):
        (self.run / "final.md").write_text(text)

    def grade(self) -> dict:
        if self.driver:
            self.driver.wait(timeout=90)
        subprocess.run([*HARNESS, "check", str(self.id), "--run", str(self.run)], capture_output=True, text=True, check=True)
        return json.loads((self.run / "grading.json").read_text())


def sid_from(out: str) -> str:
    return out.split("sid: ")[1].split()[0].strip('"')


def golden(r: Run) -> None:
    if r.id == 1:
        notes = r.run / "notes.md"
        notes.write_text("- Rejected a regex splitter: it cannot handle escaped quotes.\n")
        sid = sid_from(r.rd("open", "--title", "csv parser", "--repo", ".", "--no-browser", "--notes", str(notes)))
        r.rd("add", sid, "src/parse.py:5-7", "--note", "the fix")
        r.rd("add", sid, "tests/test_parse.py", "--note", "its test")
        r.rd("prefs", "--model", "sonnet", "--effort", "medium")
        url = r.rd("url", sid).split("url: ")[1].strip().strip('"')
        r.final(f"Spawned review-desk-medium on model sonnet.\n\n↗ {url}\n")
    elif r.id == 2:
        r.rd("backlog", r.sid, "ack", "B1", "B2")
        parse = r.repo / "src" / "parse.py"  # a plain edit: BSD and GNU sed disagree on -i
        parse.write_text(parse.read_text().replace("csv.reader([line]))", "csv.reader([line], skipinitialspace=True))"))
        with open(r.repo / "tests" / "test_parse.py", "a") as fh:
            fh.write("\n\nimport pytest\nfrom src.parse import parse\n\n\ndef test_short_row():\n    with pytest.raises(ValueError):\n        parse(['a'])\n")
        r.rd("backlog", r.sid, "done", "B1", "--note", "added test_short_row at tests/test_parse.py:12")
        r.rd("backlog", r.sid, "done", "B2", "--note", "applied the patch at src/parse.py:7")
        time.sleep(0.01)
        r.rd("reload", r.sid)
        r.final("Done B1, B2. Respawned review-desk-low with model sonnet.\n")
    elif r.id == 3:
        r.rd("backlog", r.sid, "ack", "B1")
        r.final("Tracked B1. Spawned review-desk-high on model opus.\n")
    elif r.id == 4:
        r.rd("handoff", r.sid)
        r.rd("backlog", r.sid, "ack", "B1", "B2")
        src = (r.repo / "src" / "parse.py").read_text().replace(
            '            raise ValueError(f"short row: {line!r}")\n', '            quarantined.append(line)\n            continue\n'
        ).replace("    rows = []\n", "    rows, quarantined = [], []\n")
        (r.repo / "src" / "parse.py").write_text(src)
        r.rd("backlog", r.sid, "done", "B1", "--note", "short rows go to quarantined at src/parse.py:16")
        time.sleep(0.01)
        r.rd("reload", r.sid)
        r.final("B1 done. B2 is a question for you: should parse() log how many blank rows it skipped?\n")
    elif r.id == 5:
        r.drive()
        r.rd("attach", r.sid, "--model", "haiku", "--effort", "low")
        r.rd("backlog", r.sid, "add", "--title", "Add a test for the short-row ValueError", "--anchor", "tests/test_parse.py:1-5", "--from", "1",
             "--detail-file", "-", stdin="- **Context:** `parse` raises ValueError for short rows (src/parse.py:16).\n"
             "- **Issue:** no test covers it, so a regression would pass CI.\n"
             "- **Suggested fix:** add `test_short_row_raises` beside tests/test_parse.py:1.\n"
             "- **Reasoning:** the error is new behavior and the only guard against silent truncation.\n"
             "- **Tests:** the new test feeds `['a']` and expects ValueError.\n")
        r.rd("reply", r.sid, "--to", "1,2", "--text", "#1: no, logged B1 (`src/parse.py:17` is untested). #2: no, `src/parse.py:13` skips it.")
        r.rd("wait", r.sid, "--timeout", "30")
        r.rd("handoff", r.sid)
        r.rd("detach", r.sid, "--reason", "end")
        r.final(f"END sid={r.sid}\ncount: 1\n")
    elif r.id == 6:
        r.drive()
        r.rd("attach", r.sid, "--model", "haiku", "--effort", "low")
        r.rd("reply", r.sid, "--to", "1", "--text", "From `src/ingest.py:1`; it had no callers.")
        r.rd("wait", r.sid, "--timeout", "30")
        r.rd("reply", r.sid, "--text", "Handing over to opus · high.")
        r.rd("handoff", r.sid)
        r.rd("detach", r.sid, "--reason", "handoff")
        r.final(f"HANDOFF sid={r.sid} tier=opus/high\ncount: 0\n")
    elif r.id == 7:
        r.drive()
        r.rd("attach", r.sid, "--model", "haiku", "--effort", "low")
        r.rd("backlog", r.sid, "add", "--title", "Pass skipinitialspace=True to csv.reader", "--anchor", "src/parse.py:7", "--from", "1", "--kind", "fix",
             "--detail-file", "-", stdin="- **Context:** `split_row` builds a `csv.reader` at src/parse.py:7.\n"
             "- **Issue:** fields after `, ` keep their leading space.\n"
             "- **Suggested fix:** pass `skipinitialspace=True` at src/parse.py:7.\n"
             "- **Reasoning:** the user asked for it, and the reader then trims at the source.\n"
             "- **Tests:** add a case with `a, b` expecting `['a', 'b']`.\n")
        r.rd("reply", r.sid, "--to", "1", "--text", "I don't edit code. Logged B1 for `src/parse.py:7`.")
        r.rd("wait", r.sid, "--timeout", "30")
        r.rd("handoff", r.sid)
        r.rd("detach", r.sid, "--reason", "execute")
        r.final(f"EXECUTE sid={r.sid} ids=B1\n")
    elif r.id == 8:
        r.drive()
        r.rd("attach", r.sid, "--main", "--model", "gpt-5", "--effort", "low")
        r.rd("reply", r.sid, "--to", "1", "--text", "It was deleted: `src/ingest.py:1` no longer needs it.")
        r.rd("wait", r.sid, "--timeout", "30")
        r.rd("backlog", r.sid, "add", "--title", "Multi-line quoted fields break: ingest splits on newlines first", "--anchor", "src/ingest.py:6", "--from", "3")
        r.rd("reply", r.sid, "--to", "3", "--text", "No: `src/ingest.py:6` splits lines before csv parsing. Logged B1.")
        r.rd("wait", r.sid, "--timeout", "30")
        r.final("Review ended. Open: B1 multi-line quoted fields.\n")
    elif r.id in (9, 10, 11):  # the live host's moves, made through the same CLI with the reviewer's role
        pages = r.run / "home" / "pages" / r.sid
        pages.mkdir(parents=True, exist_ok=True)
        r.rd("attach", r.sid, "--model", "sonnet", "--effort", "low")
        if r.id == 11:
            r.rd("page", r.sid, "close", "P1", role="reviewer")
            r.rd("page", r.sid, "open", str(r.run / "archive" / "csv-ingest-v1.html"), role="reviewer")
            r.rd("reply", r.sid, "--to", "1", "--text", "Closed P1 and opened `archive/csv-ingest-v1.html` as P2.")
        else:
            page = pages / ("flow.html" if r.id == 9 else "analyze-split-row.html")
            page.write_text("<!doctype html><html><body><p>ingest() -> parse() -> split_row()</p>"
                            "<p>split_row calls csv.reader</p><svg class=\"fig\"><text class=\"t-code\">split_row(line)</text></svg></body></html>")
            r.rd("page", r.sid, "open", str(page), "--title", "Call path", role="reviewer")
            r.rd("reply", r.sid, "--to", "1", "--text", f"Opened `{page}`: `src/ingest.py:6` -> `src/parse.py:16` -> `src/parse.py:7`.")


class EvalSetTest(unittest.TestCase):
    def test_every_eval_discriminates(self):
        evals = json.loads((HERE / "evals.json").read_text())["evals"]
        with tempfile.TemporaryDirectory() as tmp:
            for e in evals:
                with self.subTest(eval=e["name"]):
                    g = Run(e["id"], Path(tmp), "golden")
                    golden(g)
                    gg = g.grade()
                    failed = [x for x in gg["expectations"] if not x["passed"]]
                    self.assertEqual(failed, [], f"golden run of {e['name']} fails checks")
                    n = Run(e["id"], Path(tmp), "noop")
                    ng = n.grade()
                    self.assertLess(ng["summary"]["pass_rate"], 0.5, f"{e['name']} passes {ng['summary']} doing nothing")


if __name__ == "__main__":
    unittest.main()
