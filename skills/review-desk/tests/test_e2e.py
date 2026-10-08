"""End-to-end tests: a real server, the real CLI, a real git repo, and kill -9.

Run from the skill directory: python3 -m unittest discover -s tests
Each test class gets its own REVIEW_DESK_HOME and port, so nothing touches ~/.review-desk.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
CLI = [sys.executable, str(SKILL / "scripts" / "review_desk.py")]
# the companion skill: beside this one in the bundle, else where Claude Code installs skills
IMPL = next((p for p in (SKILL.parent / "implementation-summary" / "scripts" / "build_page.py",
                        Path.home() / ".claude" / "skills" / "implementation-summary" / "scripts" / "build_page.py") if p.exists()),
            SKILL.parent / "implementation-summary" / "scripts" / "build_page.py")
sys.path.insert(0, str(SKILL / "scripts"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "src").mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "src" / "parse.py").write_text("def split_row(line):\n    return line.split(',')\n\n\ndef legacy():\n    return 1\n")
    run("add", "-A")
    run("commit", "-qm", "init")
    (repo / "src" / "parse.py").write_text(
        "import csv\n\n\ndef split_row(line):\n    return next(csv.reader([line]))\n")
    (repo / "src" / "new.py").write_text("X = 1\nY = 2\n")
    return repo


class Desk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.repo = make_repo(root)
        cls.port = free_port()
        cls.env = {**os.environ, "REVIEW_DESK_HOME": str(root / "home"), "REVIEW_DESK_PORT": str(cls.port),
                   "REVIEW_DESK_NO_OPEN": "1", "IMPL_SUMMARY_ROOT": str(root / "archive")}
        os.environ["REVIEW_DESK_HOME"] = cls.env["REVIEW_DESK_HOME"]
        out = cls.rd("open", "--title", "Test review", "--repo", str(cls.repo))
        cls.sid = out.split("sid: ")[1].split()[0].strip('"')
        cls.url = out.split("url: ")[1].split()[0].strip('"')
        cls.token = cls.url.split("t=")[1]

    @classmethod
    def tearDownClass(cls):
        h = cls.health()
        if h:
            os.kill(h["pid"], signal.SIGTERM)
        cls.tmp.cleanup()

    # ---------------------------------------------------------- helpers
    @classmethod
    def rd(cls, *args, input=None, check=True) -> str:
        return cls.rd_full(*args, input=input, check=check)[1]

    @classmethod
    def rd_full(cls, *args, input=None, check=True, cwd=None) -> tuple[int, str, str]:
        p = subprocess.run([*CLI, *args], env=cls.env, capture_output=True, text=True, input=input, timeout=60, cwd=cwd)
        if check and p.returncode != 0:
            raise AssertionError(f"review-desk-axi {' '.join(args)} failed ({p.returncode}): {p.stdout}{p.stderr}")
        return p.returncode, p.stdout, p.stderr

    @classmethod
    def health(cls):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/health", timeout=1) as r:
                return json.loads(r.read())
        except Exception:
            return None

    def http(self, method: str, path: str, body=None, token="ok", origin=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Desk-Token"] = self.token if token == "ok" else token
        if origin:
            headers["Origin"] = origin
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def say(self, text, **kw):
        code, out = self.http("POST", f"/api/{self.sid}/message", {"text": text, **kw})
        self.assertEqual(code, 200, out)
        return out["seq"]


class ProtocolTest(Desk):
    def test_0_main_agent_answering_ignores_tier_chips(self):
        self.rd("attach", self.sid, "--main", "--model", "gpt-5", "--effort", "medium")
        self.rd("wait", self.sid, "--timeout", "1")
        q = self.say("quick one", model="opus", effort="high")
        out = self.rd("wait", self.sid, "--timeout", "5")
        self.assertIn(f"\n  {q},msg,", out)
        self.assertNotIn("tier-change", out)
        self.rd("reply", self.sid, "--to", str(q), "--text", "answered by main")
        self.rd("detach", self.sid, "--reason", "end")
        self.assertIn("must be one of", self.rd("attach", self.sid, "--model", "gpt-5", "--effort", "low", check=False))

    def test_1_question_answer_and_tier_handoff(self):
        q = self.say("why csv?", anchors=[{"path": "src/parse.py", "range": "4-5"}], model="haiku", effort="low")
        brief = self.rd("attach", self.sid, "--model", "haiku", "--effort", "low")
        self.assertIn("pending[1]{seq,kind,tier,anchors,item,ids,text,excerpt,suggestion}:", brief)
        self.assertIn("4  def split_row(line):", brief, "anchor excerpt comes from disk")
        self.rd("reply", self.sid, "--to", str(q), "--file", "-", input="csv keeps quoted commas: `src/parse.py:5`")
        code, st = self.http("GET", f"/api/{self.sid}/state")
        self.assertEqual(st["unanswered"], [])
        self.assertEqual(st["agent"]["state"], "thinking")
        # a different tier while a reviewer is live asks it to hand over; the message waits for the next one
        self.rd("wait", self.sid, "--timeout", "1")
        self.say("deeper question", model="opus", effort="high")
        self.say("and another", model="opus", effort="high")
        out = self.rd("wait", self.sid, "--timeout", "5")
        self.assertRegex(out, r"\n  \d+,tier-change,opus/high,")
        self.assertIn("HANDOFF tier=opus/high", out, "help[] names the exact final keyword")
        self.assertNotIn("deeper question", out, "a control event ends the batch")
        self.rd("detach", self.sid, "--reason", "handoff")
        self.assertIn("respawning", self.rd("status", self.sid))
        brief = self.rd("attach", self.sid, "--model", "opus", "--effort", "high")
        self.assertIn("pending[2]", brief)
        self.assertIn("deeper question", brief)

    def test_2_flag_suggest_execute_handoff_ack(self):
        code, f = self.http("POST", f"/api/{self.sid}/flag", {"anchor": {"path": "src/parse.py", "range": "5"}, "note": "needs a test"})
        self.assertEqual(code, 200, f)
        code, sgg = self.http("POST", f"/api/{self.sid}/suggest", {"anchor": {"path": "src/parse.py", "range": "5"},
                                                                    "replacement": "    return next(csv.reader([line], strict=True))", "note": "strict"})
        self.assertEqual(code, 200, sgg)
        item = next(i for i in self.http("GET", f"/api/{self.sid}/state")[1]["backlog"] if i["id"] == sgg["item"])
        self.assertIn("+    return next(csv.reader([line], strict=True))", item["patch"])
        self.assertIn("-    return next(csv.reader([line]))", item["patch"])
        self.rd("backlog", self.sid, "add", "--title", "reviewer found this", "--anchor", "src/parse.py:1", "--from", "1")
        code, ex = self.http("POST", f"/api/{self.sid}/execute", {"ids": [f["item"], sgg["item"]]})
        self.assertEqual(code, 200, ex)
        out = self.rd("wait", self.sid, "--timeout", "5")
        self.assertIn(f",execute,,,,{f['item']} {sgg['item']},,,", out)
        h1 = self.rd("handoff", self.sid)
        h2 = self.rd("handoff", self.sid)
        self.assertEqual(h1.replace(",open,", ",handed-off,"), h2, "handoff is repeatable until acked")
        self.assertIn(f"execute_requested: {f['item']} {sgg['item']}", h1)
        self.rd("detach", self.sid, "--reason", "execute")
        inbox = json.loads(self.rd("inbox", "--cwd", str(self.repo), "--json"))
        self.assertEqual(len(inbox), 1)
        self.assertEqual(set(inbox[0]["execute"]), {f["item"], sgg["item"]})
        ids = [i["id"] for i in inbox[0]["items"]]
        self.rd("backlog", self.sid, "ack", *ids)
        self.assertEqual(json.loads(self.rd("inbox", "--cwd", str(self.repo), "--json")), [])
        self.rd("backlog", self.sid, "done", f["item"], "--note", "added test")
        self.assertIn(f"\n  {f['item']},done,", self.rd("backlog", self.sid, "list", "--all"))

    def test_3_security(self):
        self.assertEqual(self.http("GET", f"/api/{self.sid}/state", token=None)[0], 403)
        self.assertEqual(self.http("GET", f"/api/{self.sid}/state", token="wrong")[0], 403)
        self.assertEqual(self.http("POST", f"/api/{self.sid}/message", {"text": "x"}, origin="http://evil.test")[0], 403)
        self.assertEqual(self.http("GET", f"/api/{self.sid}/file?path=../../etc/passwd")[0], 404)
        self.assertEqual(self.http("GET", f"/api/{self.sid}/file?path=/etc/passwd")[0], 404)
        # a symlink the repository holds (a skill linked in from its checkout) opens like any other file
        outside = Path(self.tmp.name) / "checkout"
        outside.mkdir(exist_ok=True)
        (outside / "linked.py").write_text("X = 1\n")
        if not (self.repo / "vendored").exists():
            (self.repo / "vendored").symlink_to(outside)
        code, out = self.http("GET", f"/api/{self.sid}/file?path=vendored/linked.py")
        self.assertEqual((code, out.get("lines")), (200, ["X = 1"]))
        self.assertEqual(self.http("GET", f"/api/{self.sid}/file?path=vendored/../../checkout/linked.py")[0], 404)
        self.assertEqual(self.http("GET", f"/s/{self.sid}?t=wrong", token=None)[0], 403)
        with urllib.request.urlopen(self.url, timeout=5) as r:
            page = r.read().decode()
        self.assertIn('"sid": "%s"' % self.sid, page)

    def test_3b_skills_and_full_selection_excerpt(self):
        code, out = self.http("GET", f"/api/{self.sid}/skills")
        self.assertEqual(code, 200)
        self.assertTrue(all("name" in k and "description" in k for k in out["skills"]))
        q = self.say("whole selection please", anchors=[{"path": "src/parse.py", "range": "1-5"}])
        e = next(x for x in self.http("GET", f"/api/{self.sid}/state")[1]["chat"] if x["seq"] == q)
        self.assertEqual(len(e["anchors"][0]["excerpt"].splitlines()), 5, "the reviewer gets the whole selection, not 3 lines")

    def test_3c_pages_open_close_serve_and_sandbox(self):
        import http.client
        import shutil as sh
        work = Path(self.tmp.name) / "pagework"
        work.mkdir(exist_ok=True)
        (work / "a.html").write_text("<!doctype html><html><head><link rel=stylesheet href=a.css></head><body>"
                                     "<h1>A</h1></body></html>")
        (work / "a.css").write_text("h1{color:red}")
        (work / ".env").write_text("SECRET=1")
        # agent opens: one id per path, a reopen without focus is a no-op, non-HTML and missing files are errors
        out = self.rd("page", self.sid, "open", str(work / "a.html"), "--title", "Page A")
        self.assertIn("page: P1", out)
        self.assertIn("status: already open", self.rd("page", self.sid, "open", str(work / "a.html"), "--background"))
        code, out, _ = self.rd_full("page", self.sid, "open", str(work / "a.css"), check=False)
        self.assertEqual(code, 1)
        self.assertIn("error:", out)
        code, out, _ = self.rd_full("page", self.sid, "open", str(work / "missing.html"), check=False)
        self.assertEqual(code, 1)
        # the browser sees it, and fetches it (and its assets) with the view key, never with the token
        st = self.http("GET", f"/api/{self.sid}/state")[1]
        pg = next(x for x in st["pages"] if x["id"] == "P1")
        self.assertTrue(pg["open"])
        self.assertEqual(pg["title"], "Page A")
        self.assertNotIn("view_key", st["session"])
        base = f"http://127.0.0.1:{self.port}{st['view']}/P1"
        with urllib.request.urlopen(f"{base}/a.html", timeout=5) as r:
            body = r.read().decode()
            self.assertIn("sandbox allow-scripts", r.headers["Content-Security-Policy"])
        self.assertIn("data-review-desk", body, "the link bridge is injected")
        with urllib.request.urlopen(f"{base}/a.css", timeout=5) as r:
            self.assertEqual(r.read().decode(), "h1{color:red}")
        view = f"{st['view']}/P1"
        for bad, want in ((f"{view}/.env", 404), (f"{view}/../../session.json", 403),
                          (f"/s/{self.sid}/view/wrongkey/P1/a.html", 403), (f"{view[:-1]}9/a.html", 404)):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)  # sends ../ as is
            conn.request("GET", bad)
            resp = conn.getresponse()
            resp.read()
            conn.close()
            self.assertEqual(resp.status, want, bad)
        # the user closes and reopens; the agent sees who did it
        self.assertEqual(self.http("POST", f"/api/{self.sid}/pages", {"op": "close", "id": "P1"})[0], 200)
        self.assertIn("P1,closed,Page A", self.rd("page", self.sid, "list"))
        self.assertEqual(self.http("POST", f"/api/{self.sid}/pages", {"op": "open", "path": str(work / "a.css")})[0], 400)
        self.assertEqual(self.http("POST", f"/api/{self.sid}/pages", {"op": "open", "id": "P1"})[0], 200)
        self.assertIn("P1,open,Page A", self.rd("page", self.sid, "list"))
        self.assertIn("status: closed", self.rd("page", self.sid, "close", "P1"))
        self.assertIn("status: already closed", self.rd("page", self.sid, "close", str(work / "a.html")))
        # a page named by file name alone (how the reviewer cites its pages) is found in the session's pages folder
        import store
        pages = store.open_session(self.sid).pages_dir
        pages.mkdir(parents=True, exist_ok=True)
        (pages / "by-name.html").write_text("<!doctype html><p>by name</p>")
        code, body = self.http("POST", f"/api/{self.sid}/pages", {"op": "open", "path": "by-name.html"})
        self.assertEqual(code, 200, body)
        self.assertIn(f"{body['id']},open", self.rd("page", self.sid, "list"))
        # generators run with writes confined to pages/
        if sh.which("sandbox-exec") or sh.which("bwrap"):
            escape = work / "escaped.txt"
            code, out, _ = self.rd_full("run", self.sid, "--", sys.executable, "-c",
                                        "from pathlib import Path; Path('gen.html').write_text('<p>x</p>');"
                                        f"Path({str(escape)!r}).write_text('x')", check=False)
            self.assertEqual(code, 1, "the write outside pages/ fails")
            self.assertFalse(escape.exists())
            self.assertIn("gen.html", out)

    def test_3d_search_streams_matches(self):
        def search(**params):
            qs = urllib.parse.urlencode({"scope": "changed", **params})
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/{self.sid}/search?{qs}", headers={"X-Desk-Token": self.token})
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertIn("ndjson", r.headers["Content-Type"])
                recs = [json.loads(l) for l in r.read().decode().splitlines() if l.strip()]
            self.assertIn(recs[-1]["t"], ("done", "error"), "every stream ends with done or error")
            return [x for x in recs if x["t"] == "m"], recs[-1]
        run = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)  # noqa: E731
        (self.repo / "lib").mkdir(exist_ok=True)
        (self.repo / "lib" / "other.py").write_text("def split_row(x):\n    return SPLIT_ROW\n")
        (self.repo / "big.txt").write_text("needle\n" * 2100)
        run("add", "lib/other.py", "big.txt")
        run("commit", "-qm", "unchanged files for search")
        # the change set: src/parse.py (modified) and src/new.py (untracked); committed files only in repo scope
        hits, done = search(q="split_row")
        self.assertEqual({h["path"] for h in hits}, {"src/parse.py"})
        self.assertEqual(hits[0]["badge"], "M")
        self.assertEqual(done["matches"], len(hits))
        line = (self.repo / "src" / "parse.py").read_text().splitlines()[hits[0]["line"] - 1]
        a, b = hits[0]["ranges"][0]
        self.assertEqual(line[a:b], "split_row", "ranges are character offsets into the line")
        hits, _ = search(q="split_row", scope="repo")
        self.assertEqual({h["path"] for h in hits}, {"src/parse.py", "lib/other.py"})
        self.assertEqual({h["path"] for h in search(q="split_row", scope="repo", paths="lib/")[0]}, {"lib/other.py"})
        self.assertEqual({h["path"] for h in search(q="split_row", scope="repo", paths="*.py !lib")[0]}, {"src/parse.py"})
        # toggles: case-insensitive by default, then case, word and regex
        self.assertEqual(len(search(q="SPLIT_ROW", scope="repo", paths="lib/")[0]), 2)
        self.assertEqual(len(search(q="SPLIT_ROW", scope="repo", paths="lib/", case=1)[0]), 1)
        self.assertEqual(len(search(q="split", scope="repo", paths="lib/", word=1)[0]), 0)
        self.assertEqual(len(search(q=r"def \w+_row", scope="repo", regex=1)[0]), 2)
        # a regex error is a structured record, the cap is reported, and the token is required
        hits, done = search(q="ses(sion", regex=1)
        self.assertEqual((hits, done["t"]), ([], "error"))
        self.assertIn("unclosed group", done["msg"])
        hits, done = search(q="needle", scope="repo")
        self.assertEqual((len(hits), done["truncated"]), (2000, True))
        self.assertEqual(self.http("GET", f"/api/{self.sid}/search?q=x", token=None)[0], 403)

    def test_4_cli_works_with_server_down(self):
        h = self.health()
        os.kill(h["pid"], signal.SIGKILL)
        time.sleep(0.3)
        self.assertIsNone(self.health())
        self.rd("backlog", self.sid, "add", "--title", "logged while the server was down")
        self.assertIn("logged while the server was down", self.rd("handoff", self.sid))
        self.rd("url", self.sid)  # restarts it
        self.assertIsNotNone(self.health())

    def test_5_server_from_older_code_is_replaced(self):
        # a server started before an upgrade keeps running its old code and lacks new routes;
        # the next CLI command must notice the code changed and restart it
        import shutil as sh
        import store
        old = Path(self.tmp.name) / "old-scripts"
        sh.copytree(SKILL / "scripts", old, dirs_exist_ok=True, ignore=sh.ignore_patterns("__pycache__"))
        with open(old / "server.py", "a") as fh:
            fh.write("\n# an older desk\n")
        os.kill(self.health()["pid"], signal.SIGTERM)
        self.assertTrue(any(time.sleep(0.1) or self.health() is None for _ in range(50)))
        older = subprocess.Popen([sys.executable, str(old / "server.py"), "--port", str(self.port)], env=self.env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.assertTrue(any(time.sleep(0.1) or self.health() for _ in range(50)))
        stale = self.health()
        self.assertNotEqual(stale["code"], store.code_id())
        self.rd("url", self.sid)
        fresh = self.health()
        self.assertEqual(fresh["code"], store.code_id())
        self.assertNotEqual(fresh["pid"], stale["pid"])
        older.wait(timeout=10)  # the CLI stopped it


class AxiContractTest(Desk):
    """https://axi.md: content-first home, TOON, help[], stdout errors, exit codes, idempotent writes."""

    def test_home_view_is_content_first(self):
        code, out, err = self.rd_full(cwd=str(self.repo))
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("bin: "))
        self.assertTrue(lines[1].startswith("description: Review Desk"))
        self.assertIn("count: 1 session(s) in this repo", out)
        self.assertIn("sessions[1]{sid,title,status,reviewer,unanswered,open}:", out)
        self.assertIn(f"\n  {self.sid},Test review,open,", out)
        self.assertRegex(out, r"help\[\d+\]:\n  Run `review-desk-axi ")
        other = Path(self.tmp.name) / "elsewhere"
        other.mkdir(exist_ok=True)
        self.assertIn("sessions[0]: none in this repo", self.rd_full(cwd=str(other))[1])

    def test_errors_are_structured_on_stdout(self):
        code, out, err = self.rd_full("status", "ffffffff", check=False)
        self.assertEqual((code, err), (1, ""))
        self.assertTrue(out.startswith("error: no review-desk session ffffffff\nhelp[1]:"))
        code, out, err = self.rd_full("status", self.sid, "--bogus", check=False)
        self.assertEqual((code, err), (2, ""), "unknown flags fail loud")
        self.assertIn('error: "unrecognized arguments: --bogus"', out)
        code, out, _ = self.rd_full("backlog", self.sid, "list", "--fields", "id,nope", check=False)
        self.assertEqual(code, 2)
        self.assertIn("unknown field(s) nope", out)

    def test_writes_are_idempotent(self):
        args = ("backlog", self.sid, "add", "--title", "Same thing", "--anchor", "src/parse.py:2", "--from", "1")
        first = self.rd(*args)
        again = self.rd(*args)
        iid = first.split("created: ")[1].split()[0]
        self.assertIn(f"existing: {iid}", again)
        q = self.say("idempotent reply?")
        r1 = self.rd("reply", self.sid, "--to", str(q), "--text", "yes")
        r2 = self.rd("reply", self.sid, "--to", str(q), "--text", "yes")
        self.assertEqual(r1.split("posted: ")[1].split()[0], r2.split("posted: ")[1].split()[0])
        self.assertIn("duplicate: ", r2)
        self.assertIn(",pinned,", self.rd("add", self.sid, "src/new.py"))
        self.assertIn(",already pinned,", self.rd("add", self.sid, "src/new.py"))

    def test_minimal_schema_and_truncation(self):
        self.say("x" * 900)
        out = self.rd("chat", self.sid, "--last", "1")
        self.assertIn("(truncated, 900 chars total - use review-desk-axi chat", out)
        seq = out.split("\n  ")[1].split(",")[0]
        self.assertIn("x" * 900, self.rd("chat", self.sid, seq))
        out = self.rd("backlog", self.sid, "list")
        self.assertRegex(out, r"count: \d+ shown of \d+")
        self.assertIn("{id,status,title,anchor}:", out)

    def test_hooks(self):
        settings = Path(self.tmp.name) / "settings.json"
        settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [
            {"type": "command", "command": "python3 ~/.claude/skills/review-desk/hooks/inbox.py"}]}]},
            "permissions": {"allow": ["Bash(~/.claude/skills/review-desk/bin/review-desk:*)"]}}))
        first = self.rd("setup", "hooks", "--settings", str(settings))
        self.assertIn("legacy inbox.py hook\",removed", first)
        self.assertIn("UserPromptSubmit: review-desk-axi hook prompt\",added", first)
        self.assertIn("PostToolUse: review-desk-axi hook tool\",added", first)
        self.assertIn("Stop: review-desk-axi hook stop\",added", first)
        written = settings.read_text()
        second = self.rd("setup", "hooks", "--settings", str(settings))
        self.assertNotIn("added", second)
        self.assertNotIn("removed", second)
        self.assertEqual(written, settings.read_text(), "a second setup changes nothing")
        d = json.loads(written)
        import review_desk
        summary = review_desk.summary_hooks()  # implementation-summary's, when that skill is installed beside
        self.assertEqual([h["command"] for g in d["hooks"]["UserPromptSubmit"] for h in g["hooks"]],
                         ["review-desk-axi hook prompt", *[c for e, c, _ in summary if e == "UserPromptSubmit"]])
        for event, cmd, matcher in summary:
            self.assertIn(f"{event}: {cmd}\",added", first)
            group = next(g for g in d["hooks"][event] if g["hooks"][0]["command"] == cmd)
            self.assertEqual(group.get("matcher"), matcher)
        self.assertEqual(d["permissions"]["allow"], ["Bash(review-desk-axi:*)"])
        # the prompt hook injects undelivered items once per session, and never fails
        self.rd("backlog", self.sid, "add", "--title", "hook item")
        self.rd("detach", self.sid, "--reason", "idle")
        stdin = json.dumps({"cwd": str(self.repo), "session_id": "hook-test"})
        out1 = self.rd("hook", "prompt", input=stdin)
        self.assertIn("hook item", out1)
        self.assertEqual(self.rd("hook", "prompt", input=stdin), "")
        self.assertEqual(self.rd_full("hook", "prompt", input="not json")[0], 0)


class DeliveryTest(Desk):
    """Execute reaches the agent that drives the desk while it is busy: its PostToolUse and Stop hooks
    deliver it once, and the desk says when nobody is listening."""

    def hook(self, event, session="agent-1", **data):
        out = self.rd("hook", event, input=json.dumps({"session_id": session, "cwd": str(self.repo), **data}))
        return json.loads(out) if out.strip() else None

    def bash(self, command, session="agent-1", stdout=""):
        return self.hook("tool", session, tool_name="Bash", tool_input={"command": command}, tool_response={"stdout": stdout})

    def delivery(self):
        return self.http("GET", f"/api/{self.sid}/state")[1]["delivery"]

    def test_busy_owner_gets_execute_once_and_the_desk_says_where_it_went(self):
        import store
        self.assertEqual(self.delivery()["state"], "idle")
        # driving the desk claims it; a reviewer's own commands never do
        self.assertIsNone(self.bash(f"review-desk-axi wait {self.sid}", session="a-reviewer"))
        self.assertNotIn(self.sid, store.owned("a-reviewer"))
        self.assertIsNone(self.bash(f"review-desk-axi add {self.sid} a.py:1"))
        self.assertIn(self.sid, store.owned("agent-1"))
        self.rd("backlog", self.sid, "add", "--title", "rename the helper", "--detail", "split_row reads better as parse_row")
        item = "B1"  # the class's desk is fresh
        # Execute while the owner works on something else: queued, then delivered by its next tool call
        self.assertEqual(self.http("POST", f"/api/{self.sid}/execute", {"ids": [item]})[0], 200)
        self.assertEqual(self.delivery()["state"], "queued")
        out = self.hook("tool", tool_name="Read", tool_input={"file_path": "/x"})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn(f"pressed Execute for {item}", ctx)
        self.assertIn("rename the helper", ctx)
        self.assertIsNone(self.hook("tool", tool_name="Read", tool_input={"file_path": "/x"}), "delivered once")
        d = self.delivery()
        self.assertEqual((d["state"], d["delivered"]["via"], d["delivered"]["to"]), ("delivered", "hook:tool", "agent-1"))
        self.assertIn(f"{item},handed-off", self.rd("backlog", self.sid, "list", "--fields", "id,status"))
        # a watch started now does not fire again for it
        self.assertIn("event: TIMEOUT", self.rd("watch", self.sid, "--timeout", "1"))
        # an End that lands while the agent writes its final reply stops the stop, once
        self.rd("backlog", self.sid, "add", "--title", "second item")
        self.http("POST", f"/api/{self.sid}/execute", {"ids": []})
        stop = self.hook("stop")
        self.assertEqual(stop["decision"], "block")
        self.assertIn("second item", stop["reason"])
        self.assertIsNone(self.hook("stop", stop_hook_active=True))
        # the latest driver owns it; the previous one stops receiving
        self.bash(f"review-desk-axi reload {self.sid}", session="agent-2")
        self.assertIsNone(self.hook("tool", tool_name="Read"))  # agent-1's next hook prunes the desk it lost
        self.assertNotIn(self.sid, store.owned("agent-1"))
        self.assertEqual(store.open_session(self.sid).meta()["owner"]["session"], "agent-2")
        # nobody working and nobody watching: the desk says the Execute was not heard
        self.rd("backlog", self.sid, "add", "--title", "third item")
        s = store.open_session(self.sid)
        s.update_meta(owner=s.meta()["owner"] | {"seen": 0}, watcher=None)
        self.http("POST", f"/api/{self.sid}/execute", {"ids": []})
        self.assertEqual(self.delivery()["state"], "unheard")
        # a desk opened in this session is claimed from the open command's output
        out = self.bash("review-desk-axi open --title other", session="agent-3", stdout=f"sid: {self.sid}\nurl: x\n")
        self.assertEqual(store.open_session(self.sid).meta()["owner"]["session"], "agent-3")
        self.assertIn("third item", out["hookSpecificOutput"]["additionalContext"], "the new owner gets what was waiting")

    def test_hooks_never_fail(self):
        for event in ("tool", "stop"):
            self.assertEqual(self.rd_full("hook", event, input="not json")[0], 0)


class AckLifecycleTest(Desk):
    """Ack means "accepted, implementing now". Items the user did not execute stay executable; accepted work the
    agent never closes is listed until it is (Stop once per acceptance, the prompt hook, status, the home view)."""

    def hook(self, event, session="agent-1", **data):
        out = self.rd("hook", event, input=json.dumps({"session_id": session, "cwd": str(self.repo), **data}))
        return json.loads(out) if out.strip() and out.lstrip().startswith("{") else (out or None)

    def status_of(self, iid):
        import store
        return next(i["status"] for i in store.open_session(self.sid).backlog() if i["id"] == iid)

    def test_only_executed_items_are_acked_and_unfinished_work_is_listed(self):
        self.hook("tool", tool_name="Bash", tool_input={"command": f"review-desk-axi add {self.sid} a.py:1"}, tool_response={"stdout": ""})
        for t in ("fix one", "fix two", "fix three"):
            self.rd("backlog", self.sid, "add", "--title", t, "--anchor", "a.py:1", "--from", "1")
        # the user executes B1 only: the help acks B1 and leaves B2 and B3 for the user
        self.assertEqual(self.http("POST", f"/api/{self.sid}/execute", {"ids": ["B1"]})[0], 200)
        handoff = self.rd("handoff", self.sid)
        self.assertIn(f"backlog {self.sid} ack B1` to accept", handoff)
        self.assertIn("Leave B2 B3 open", handoff)
        self.rd("backlog", self.sid, "ack", "B1")
        self.rd("backlog", self.sid, "done", "B1", "--note", "fixed at a.py:1")
        # later the user executes B2: it is delivered, with the ack step
        self.assertEqual(self.http("POST", f"/api/{self.sid}/execute", {"ids": ["B2"]})[0], 200)
        ctx = self.hook("tool", tool_name="Read", tool_input={"file_path": "/x"})["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"backlog {self.sid} ack B2", ctx)
        # accepted and never closed: Stop reminds once per acceptance, then lets the agent stop
        self.rd("backlog", self.sid, "ack", "B2")
        stop = self.hook("stop")
        self.assertEqual(stop["decision"], "block")
        self.assertIn("B2 (fix two) but never closed it", stop["reason"])
        self.assertIsNone(self.hook("stop"), "once per acceptance")
        self.assertIsNone(self.hook("stop", stop_hook_active=True))
        # a fresh session (after a context reset) and status both list it
        self.assertIn("unfinished[1]", self.hook("prompt", session="agent-2"))
        self.assertIsNone(self.hook("prompt", session="agent-2"), "once per session")
        self.assertIn("B2 accepted, not done", self.rd("status", self.sid))
        # partly done goes back to the user, and leaves the unfinished list
        self.rd("backlog", self.sid, "reopen", "B2", "--note", "the test is still missing")
        self.assertEqual(self.status_of("B2"), "open")
        self.assertNotIn("accepted, not done", self.rd("status", self.sid))
        # an item acked without an Execute (the old help) can still be executed from the desk
        self.rd("backlog", self.sid, "ack", "B3")
        self.assertEqual(self.http("POST", f"/api/{self.sid}/execute", {"ids": []})[1]["ids"], ["B2"], "Execute all leaves acked work alone")
        self.assertEqual(self.http("POST", f"/api/{self.sid}/execute", {"ids": ["B3"]})[0], 200)
        self.assertEqual(self.status_of("B3"), "handed-off")
        import store
        self.assertIn("B3", store.open_session(self.sid).execute_requested())


class StructuredDetailTest(Desk):
    """A backlog detail is multi-line Markdown (Context, Issue, Suggested fix, Reasoning, Tests): it goes in on
    stdin untouched by the shell, and reaches the main agent whole, in handoff and in the Execute delivery."""

    DETAIL = "\n".join([
        "- **Context:** `split_row` (`src/parse.py:12`) now uses `csv.reader`.",
        "- **Issue:** an unterminated quote raises `csv.Error` and aborts the whole ingest.",
        "- **Suggested fix:** catch `csv.Error` in `parse` (`src/parse.py:24`) and collect the raw line.",
        "  - return the rejects beside the rows",
        "- **Reasoning:** one bad row should not lose the file; " + "returning rejects keeps them visible. " * 8,
        "- **Tests:** add `test_unterminated_quote_is_rejected` to `tests/test_parse.py`.",
    ])

    def hook(self, event, session="agent-1", **data):
        out = self.rd("hook", event, input=json.dumps({"session_id": session, "cwd": str(self.repo), **data}))
        return json.loads(out) if out.strip() else None

    def test_detail_from_stdin_reaches_the_main_agent_whole(self):
        self.hook("tool", tool_name="Bash", tool_input={"command": f"review-desk-axi add {self.sid} a.py:1"}, tool_response={"stdout": ""})
        out = self.rd("backlog", self.sid, "add", "--title", "Quarantine bad rows", "--anchor", "src/parse.py:12-18",
                      "--from", "1", "--detail-file", "-", input=self.DETAIL + "\n")
        self.assertIn("created: B1", out)
        self.assertGreater(len(self.DETAIL), 400, "longer than the old delivery cut")
        import store
        self.assertEqual(store.open_session(self.sid).backlog()[0]["detail"], self.DETAIL, "stored verbatim, backticks intact")
        # --detail and --detail-file together is a usage error, and changes nothing
        code, stdout, _ = self.rd_full("backlog", self.sid, "add", "--title", "x", "--detail", "a", "--detail-file", "-", input="b", check=False)
        self.assertEqual(code, 2)
        self.assertIn("not both", stdout)
        self.assertEqual(len(store.open_session(self.sid).backlog()), 1)
        # handoff prints each detail as an indented block, not one escaped table cell
        handoff = self.rd("handoff", self.sid)
        self.assertIn(f"detail_B1[{len(self.DETAIL.splitlines())} lines]:", handoff)
        self.assertIn("\n    - return the rejects beside the rows\n", handoff)
        self.assertNotIn("\\n", handoff)
        # Execute delivers the whole detail, nested under its item
        self.http("POST", f"/api/{self.sid}/execute", {"ids": ["B1"]})
        ctx = self.hook("tool", tool_name="Read", tool_input={"file_path": "/x"})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("- B1 [fix] Quarantine bad rows @ src/parse.py:12-18", ctx)
        self.assertIn("    - **Tests:** add `test_unterminated_quote_is_rejected` to `tests/test_parse.py`.", ctx)
        # update replaces the detail the same way
        self.rd("backlog", self.sid, "update", "B1", "--detail-file", "-", input="- **Context:** new\n")
        self.assertEqual(store.open_session(self.sid).backlog()[0]["detail"], "- **Context:** new")


class FirstWatchTest(Desk):
    """A watch started after the user pressed Execute still delivers it: watch and the hooks read the same
    pending events, so an Execute is never stranded because no watch was running at the time."""

    def test_execute_pressed_before_the_first_watch_is_delivered(self):
        import store
        self.assertIsNone(store.open_session(self.sid).meta().get("main_cursor"), "a desk nobody has watched yet")
        self.rd("backlog", self.sid, "add", "--title", "pressed before any watch")
        self.assertEqual(self.http("POST", f"/api/{self.sid}/execute", {"ids": ["B1"]})[0], 200)
        out = self.rd("watch", self.sid, "--timeout", "5")
        self.assertIn("event: EXECUTE", out)
        self.assertIn("pressed before any watch", out)
        d = store.open_session(self.sid).meta()["delivered"]
        self.assertEqual(d["via"], "watch")
        self.assertIn("event: TIMEOUT", self.rd("watch", self.sid, "--timeout", "1"), "delivered once")


class ReviewerHostTest(Desk):
    """One long-lived reviewer: tier changes apply in place, watch wakes the main agent, restarts resume."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.claude_log = Path(cls.tmp.name) / "fake-claude.jsonl"
        cls.env.update({"REVIEW_DESK_CLAUDE": str(SKILL / "tests" / "fake_claude.py"), "FAKE_CLAUDE_LOG": str(cls.claude_log)})

    def until(self, pred, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.2)
        return False

    def agent_msgs(self):
        return [e for e in self.http("GET", f"/api/{self.sid}/state")[1]["chat"] if e.get("role") == "agent"]

    def test_host_lifecycle(self):
        import store
        q1 = self.say("queued before the reviewer starts", model="haiku", effort="low")
        out = self.rd("reviewer", self.sid, "start", "--model", "haiku", "--effort", "low")
        self.assertIn("reviewer: running haiku/low", out)
        self.assertIn(f"watch {self.sid}", out)
        self.assertTrue(self.until(lambda: any(q1 in m.get("reply_to", []) for m in self.agent_msgs())))
        first = next(m for m in self.agent_msgs() if q1 in m["reply_to"])
        self.assertIn("answer[haiku/low]", first["text"])
        self.assertIn("briefed=True", first["text"])
        pid = first["text"].split("pid=")[1].split()[0]

        # the answer streams: a draft with growing text appears, then the posted message replaces it
        q_s = self.say("stream this", model="haiku", effort="low")
        seen = []
        def draft_text():
            d = self.http("GET", f"/api/{self.sid}/state")[1].get("draft")
            if d and d.get("text"):
                seen.append(d["text"])
            return any(q_s in m.get("reply_to", []) for m in self.agent_msgs())
        self.assertTrue(self.until(draft_text))
        self.assertTrue(len(seen) >= 2 and len(seen[-1]) > len(seen[0]), f"draft did not grow: {seen}")
        self.assertIsNone(self.http("GET", f"/api/{self.sid}/state")[1].get("draft"), "the draft clears once the answer is posted")

        # a different tier from the chat switches the same process in place
        q2 = self.say("now on sonnet", model="sonnet", effort="medium")
        self.assertTrue(self.until(lambda: any(q2 in m.get("reply_to", []) for m in self.agent_msgs())))
        second = next(m for m in self.agent_msgs() if q2 in m["reply_to"])
        self.assertIn("answer[sonnet/medium]", second["text"])
        self.assertIn(f"pid={pid}", second["text"], "no respawn")
        self.assertIn("briefed=False", second["text"])
        self.assertEqual(len(self.claude_log.read_text().splitlines()), 1, "one Claude launch so far")
        self.assertEqual((second["model"], second["effort"]), ("sonnet", "medium"))

        # Execute wakes the watcher; the reviewer keeps running
        w = subprocess.Popen([*CLI, "watch", self.sid, "--timeout", "30"], env=self.env, stdout=subprocess.PIPE, text=True)
        time.sleep(1)
        self.rd("backlog", self.sid, "add", "--title", "host item", "--from", str(q2))
        self.http("POST", f"/api/{self.sid}/execute", {"ids": []})
        out = w.communicate(timeout=30)[0]
        self.assertIn("event: EXECUTE", out)
        self.assertIn("host item", out)
        self.assertIn("host: running", self.rd("reviewer", self.sid, "status"))

        # a stopped reviewer resumes its conversation on restart
        self.rd("reviewer", self.sid, "stop")
        self.assertIn("host: not running", self.rd("reviewer", self.sid, "status"))
        out = self.rd("reviewer", self.sid, "start", "--model", "opus", "--effort", "high")
        self.assertIn("resumes: previous conversation", out)
        launches = [json.loads(l) for l in self.claude_log.read_text().splitlines()]
        self.assertEqual(len(launches), 2)
        self.assertIn("--resume", launches[1]["args"])
        self.assertEqual(launches[1]["args"][launches[1]["args"].index("--resume") + 1], f"fake-{pid}")

        # a conversation started under other desk instructions is not resumed: --resume would keep
        # its old system prompt (an old pages folder), so the host starts fresh and briefs again
        self.rd("reviewer", self.sid, "stop")
        store.open_session(self.sid).set_agent(prompt_id="from-an-older-desk")
        out = self.rd("reviewer", self.sid, "start", "--model", "opus", "--effort", "high")
        self.assertIn("fresh conversation (desk instructions changed)", out)
        launches = [json.loads(l) for l in self.claude_log.read_text().splitlines()]
        self.assertEqual(len(launches), 3)
        self.assertNotIn("--resume", launches[2]["args"])
        q3 = self.say("after the upgrade", model="opus", effort="high")
        self.assertTrue(self.until(lambda: any(q3 in m.get("reply_to", []) for m in self.agent_msgs())))
        self.assertIn("briefed=True", next(m for m in self.agent_msgs() if q3 in m["reply_to"])["text"])

        # End stops the reviewer and wakes the watcher
        w = subprocess.Popen([*CLI, "watch", self.sid, "--timeout", "30"], env=self.env, stdout=subprocess.PIPE, text=True)
        time.sleep(1)
        self.http("POST", f"/api/{self.sid}/end", {})
        self.assertIn("event: END", w.communicate(timeout=30)[0])
        self.assertTrue(self.until(lambda: "not running" in self.rd("reviewer", self.sid, "status")))
        self.assertEqual(store.open_session(self.sid).agent().get("reason"), "end")


class DraftStatusTest(Desk):
    """The status line names the tool call that is running, even when events arrive faster than the throttle."""

    def test_finished_call_label_is_never_throttled_away(self):
        import threading
        import store
        import reviewer_host
        h = reviewer_host.Host.__new__(reviewer_host.Host)
        h.s = store.open_session(self.sid)
        h.draft, h.draft_lock, h.draft_written, h.tool = {"text": "", "status": "", "reply_to": [1]}, threading.Lock(), 0.0, None
        stream = h.s.dir / "stream.json"
        h.on_stream({"type": "content_block_start", "content_block": {"type": "tool_use", "name": "Bash"}})
        self.assertEqual(json.loads(stream.read_text())["status"], "using Bash")
        before = stream.stat().st_mtime_ns
        h.on_stream({"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": '{"command": "git diff --stat"}'}})
        self.assertEqual(stream.stat().st_mtime_ns, before, "streamed tool input changes nothing shown, so nothing is written")
        h.on_stream({"type": "content_block_stop"})  # well inside the 80 ms text throttle
        self.assertEqual(json.loads(stream.read_text())["status"], "Bash git diff --stat")


def git_in(repo: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


class PullRequestTest(Desk):
    """`open --pr <link>`: a GitHub pull request as a session. "GitHub" is a local bare repository with
    refs/pull/N/head (url.insteadOf sends https://github.com/ there) and a fake gh (tests/fake_gh.py)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.port = free_port()
        cls.fake = root / "gh"
        cls.fake.mkdir()
        bindir = root / "bin"
        bindir.mkdir()
        (bindir / "gh").write_text(f"#!/bin/sh\nexec {sys.executable} {SKILL / 'tests' / 'fake_gh.py'} \"$@\"\n")
        (bindir / "gh").chmod(0o755)
        gitconfig = root / "gitconfig"
        gitconfig.write_text(f'[url "{root / "remote"}/"]\n\tinsteadOf = https://github.com/\n'
                             '[user]\n\temail = t@t\n\tname = t\n[init]\n\tdefaultBranch = main\n'
                             '[protocol "file"]\n\tallow = always\n[uploadpack]\n\tallowAnySHA1InWant = true\n')
        cls.env = {**os.environ, "REVIEW_DESK_HOME": str(root / "home"), "REVIEW_DESK_PORT": str(cls.port),
                   "REVIEW_DESK_NO_OPEN": "1", "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_GH_DIR": str(cls.fake),
                   "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}
        os.environ["REVIEW_DESK_HOME"] = cls.env["REVIEW_DESK_HOME"]
        g = lambda repo, *a: git_in(repo, *a, env=cls.env)  # noqa: E731
        # upstream: main, a PR branch off it, then main moves on (the PR must not show that commit)
        seed = root / "seed"
        (seed / "src").mkdir(parents=True)
        g(root, "init", "-q", str(seed))
        (seed / "src" / "app.py").write_text("".join(f"line {i}\n" for i in range(1, 41)))
        (seed / "README.md").write_text("widgets\n")
        g(seed, "add", "-A"); g(seed, "commit", "-qm", "init")
        g(seed, "switch", "-qc", "feature")
        (seed / "src" / "app.py").write_text("".join(f"line {i}\n" if i != 20 else "line 20 changed by the PR\n" for i in range(1, 41)))
        (seed / "src" / "new.py").write_text("NEW = 1\n")
        g(seed, "add", "-A"); g(seed, "commit", "-qm", "the PR")
        cls.head = g(seed, "rev-parse", "HEAD")
        g(seed, "switch", "-q", "main")
        (seed / "README.md").write_text("widgets, moved on\n")
        g(seed, "commit", "-qam", "main moves on")
        cls.base_tip = g(seed, "rev-parse", "HEAD")
        cls.bare = root / "remote" / "acme" / "widgets.git"
        cls.bare.parent.mkdir(parents=True)
        g(root, "clone", "-q", "--bare", str(seed), str(cls.bare))
        g(cls.bare, "update-ref", "refs/pull/7/head", cls.head)
        cls.seed = seed
        # the user's own clone, on another branch with uncommitted work: a PR review must never touch it
        cls.repo = root / "work"
        g(root, "clone", "-q", "https://github.com/acme/widgets.git", str(cls.repo))
        g(cls.repo, "switch", "-qc", "my-branch")
        (cls.repo / "README.md").write_text("my uncommitted edit\n")
        cls.write_pr(7, state="OPEN", base=cls.base_tip, head=cls.head)
        (cls.fake / "comments-7.json").write_text(json.dumps([
            {"path": "src/app.py", "line": 20, "body": "Why change line 20?", "user": {"login": "carol"}, "created_at": "2026-10-02T00:00:00Z"}]))
        out = cls.rd("open", "--pr", "https://github.com/acme/widgets/pull/7/files", cwd=str(cls.repo))
        cls.open_out = out
        cls.sid = out.split("sid: ")[1].split()[0].strip('"')
        cls.url = out.split("url: ")[1].split()[0].strip('"')
        cls.token = cls.url.split("t=")[1]

    @classmethod
    def write_pr(cls, n, state, base, head):
        (cls.fake / f"pr-{n}.json").write_text(json.dumps({
            "number": n, "title": "Change line 20", "body": "Changes line 20.\n\n<script>alert(1)</script>", "url": f"https://github.com/acme/widgets/pull/{n}",
            "state": state, "isDraft": False, "author": {"login": "bob"}, "baseRefName": "main", "baseRefOid": base,
            "headRefName": "feature", "headRefOid": head, "headRepository": {"name": "widgets"}, "headRepositoryOwner": {"login": "acme"},
            "additions": 2, "deletions": 1, "changedFiles": 2, "url_": None,
            "comments": [{"author": {"login": "alice"}, "body": "Looks **good**", "createdAt": "2026-10-01T00:00:00Z"}],
            "reviews": [{"author": {"login": "dave"}, "state": "CHANGES_REQUESTED", "body": "", "submittedAt": "2026-10-03T00:00:00Z"}]}))

    @classmethod
    def rd(cls, *args, input=None, check=True, cwd=None) -> str:
        return cls.rd_full(*args, input=input, check=check, cwd=cwd)[1]

    def session(self):
        import store
        return store.open_session(self.sid)

    def files(self):
        return {f["path"]: f for f in self.session().manifest()["files"]}

    def test_1_opens_the_pr_head_in_its_own_worktree_without_touching_the_users_clone(self):
        m = self.session().meta()
        wt = Path(m["repo"])
        self.assertEqual(wt.resolve().parent, Path(self.env["REVIEW_DESK_HOME"]).resolve() / "worktrees")
        self.assertEqual(git_in(wt, "rev-parse", "HEAD"), self.head)
        self.assertEqual(set(self.files()), {"src/app.py", "src/new.py"}, "vs the merge-base: main's later README commit is not in the PR")
        self.assertEqual(self.session().manifest()["base_label"], "main")
        # the commit picker lists the PR's own commit, counted from the merge-base like the diff
        idx = json.loads((self.session().dir / "commits" / "index.json").read_text())
        self.assertEqual([(e["id"], e["subject"], e["files"]) for e in idx["entries"]], [(self.head, "the PR", 2)])
        # the user's clone: same branch, edit intact, no worktree checkout in it
        self.assertEqual(git_in(self.repo, "branch", "--show-current"), "my-branch")
        self.assertEqual((self.repo / "README.md").read_text(), "my uncommitted edit\n")
        self.assertEqual(m["pr"]["home_repo"], str(self.repo.resolve()))
        self.assertEqual(m["title"], "PR #7: Change line 20")
        self.assertIn('pr: "https://github.com/acme/widgets/pull/7"', self.open_out)
        # the reviewer's context carries the description and the whole discussion, inline comments included
        ctx = self.session().context_path.read_text()
        for want in ("## Pull request acme/widgets#7: Change line 20", "Changes line 20.", "@alice commented",
                     "@dave reviewed: changes requested", "@carol on src/app.py:20", "Why change line 20?"):
            self.assertIn(want, ctx)
        # the home view and the prompt hook find the desk from the user's clone
        _, home, _ = self.rd_full(cwd=str(self.repo))
        self.assertIn(self.sid, home)
        # the PR page: a sandboxed page tab, no scripts from the PR text
        pages = self.http("GET", f"/api/{self.sid}/state")[1]["pages"]
        self.assertEqual([(p["id"], p["title"], p["open"]) for p in pages], [("P1", "PR #7", True)])
        page = Path(pages[0]["path"]).read_text()
        self.assertIn("default-src 'none'", page)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("Why change line 20?", page)

    def test_2_reopen_follows_new_pushes_but_never_discards_local_work(self):
        g = lambda repo, *a: git_in(repo, *a, env=self.env)  # noqa: E731
        g(self.seed, "switch", "-q", "feature")
        (self.seed / "src" / "more.py").write_text("MORE = 2\n")
        g(self.seed, "add", "-A"); g(self.seed, "commit", "-qm", "push 2")
        head2 = g(self.seed, "rev-parse", "HEAD")
        g(self.seed, "push", "-q", str(self.bare), f"{head2}:refs/pull/7/head")
        g(self.seed, "switch", "-q", "main")
        self.write_pr(7, state="OPEN", base=self.base_tip, head=head2)
        out = self.rd("open", "--pr", "acme/widgets#7", cwd=str(self.repo))
        self.assertIn(f"sid: {self.sid}", out, "one session per PR")
        self.assertIn(f"moved to {head2[:10]}", out)
        self.assertIn("src/more.py", self.files())
        wt = Path(self.session().meta()["repo"])
        # an Execute implemented in the worktree: a later push does not move it
        (wt / "src" / "new.py").write_text("NEW = 2\n")
        g(self.seed, "switch", "-q", "feature")
        (self.seed / "src" / "third.py").write_text("T = 3\n")
        g(self.seed, "add", "-A"); g(self.seed, "commit", "-qm", "push 3")
        head3 = g(self.seed, "rev-parse", "HEAD")
        g(self.seed, "push", "-q", str(self.bare), f"{head3}:refs/pull/7/head")
        g(self.seed, "switch", "-q", "main")
        self.write_pr(7, state="OPEN", base=self.base_tip, head=head3)
        out = self.rd("open", "--pr", "7", cwd=str(self.repo))
        self.assertIn("the worktree has local work", out)
        self.assertEqual((wt / "src" / "new.py").read_text(), "NEW = 2\n")
        self.assertEqual(git_in(wt, "rev-parse", "HEAD"), head2)
        pr = self.session().meta()["pr"]
        self.assertEqual((pr["head_sha"], pr["latest_head"]), (head2, head3), "the reviewed commit is what the worktree holds")
        self.assertIn("src/new.py", self.files(), "the diff shows the local edit")
        self.assertNotIn("src/third.py", self.files())
        # once the local work is gone, the next open catches up
        g(wt, "checkout", "-q", "--", ".")
        self.assertIn(f"moved to {head3[:10]}", self.rd("open", "--pr", "7", cwd=str(self.repo)))
        self.assertEqual(self.session().meta()["pr"]["head_sha"], head3)

    def test_3_merged_pr_diffs_against_the_base_it_merged_onto(self):
        g = lambda repo, *a: git_in(repo, *a, env=self.env)  # noqa: E731
        # PR 8 is merged with a merge commit: main now contains its head, so a merge-base with main's tip is the head
        g(self.seed, "switch", "-qc", "pr8", self.base_tip)
        (self.seed / "src" / "eight.py").write_text("E = 8\n")
        g(self.seed, "add", "-A"); g(self.seed, "commit", "-qm", "pr 8")
        head8 = g(self.seed, "rev-parse", "HEAD")
        g(self.seed, "switch", "-q", "main")
        g(self.seed, "merge", "-q", "--no-ff", "-m", "merge pr 8", "pr8")
        g(self.seed, "push", "-q", str(self.bare), "main", f"{head8}:refs/pull/8/head")
        self.write_pr(8, state="MERGED", base=self.base_tip, head=head8)
        out = self.rd("open", "--pr", "https://github.com/acme/widgets/pull/8", cwd=tempfile.gettempdir())
        sid8 = out.split("sid: ")[1].split()[0].strip('"')
        import store
        s8 = store.open_session(sid8)
        self.assertEqual([f["path"] for f in s8.manifest()["files"]], ["src/eight.py"])
        # opened outside any clone of acme/widgets: a cached clone, and no home repo
        self.assertTrue((Path(self.env["REVIEW_DESK_HOME"]) / "clones" / "github.com" / "acme" / "widgets" / ".git").exists())
        self.assertIsNone(s8.meta()["pr"]["home_repo"])

    def test_4_execute_on_a_pr_asks_post_or_implement_and_post_makes_one_review(self):
        sid = self.sid
        head = git_in(Path(self.session().meta()["repo"]), "rev-parse", "HEAD")
        self.rd("backlog", sid, "add", "--title", "Inline one", "--anchor", "src/app.py:19-21", "--from", "1", "--detail", "- **Issue:** x")
        self.rd("backlog", sid, "add", "--title", "Outside the diff", "--anchor", "src/app.py:2", "--from", "1")
        self.rd("backlog", sid, "add", "--title", "No anchor", "--kind", "question", "--from", "1")
        self.assertEqual(self.http("POST", f"/api/{sid}/execute", {"ids": ["B1", "B2", "B3"]})[0], 200)
        handoff = self.rd("handoff", sid)
        self.assertIn("ask the user (AskUserQuestion) whether to post them as a GitHub review or implement them", handoff)
        self.assertIn(f"pr {sid} post B1 B2 B3 --dry-run", handoff)
        # the agent driving the desk gets the same choice from its PostToolUse hook
        ctx = self.rd("hook", "tool", input=json.dumps({"session_id": "a1", "cwd": str(self.repo), "tool_name": "Bash",
                                                      "tool_input": {"command": f"review-desk-axi status {sid}"}, "tool_response": {"stdout": ""}}))
        self.assertIn("post them as a GitHub review", ctx)
        dry = self.rd("pr", sid, "post", "B1", "B2", "B3", "--dry-run")
        self.assertIn('B1,"src/app.py:19-21",Inline one', dry)
        self.assertIn("src/app.py:2 is outside the PR's diff", dry)
        self.assertFalse((self.fake / "reviews-7.jsonl").exists(), "a dry run posts nothing")
        self.rd("backlog", sid, "ack", "B1", "B2", "B3")
        out = self.rd("pr", sid, "post", "B1", "B2", "B3")
        self.assertIn("review: \"https://github.com/acme/widgets/pull/7#pullrequestreview-1\"", out)
        [payload] = [json.loads(l) for l in (self.fake / "reviews-7.jsonl").read_text().splitlines()]
        self.assertEqual((payload["commit_id"], payload["event"]), (head, "COMMENT"))
        self.assertEqual([(c["path"], c.get("start_line"), c["line"], c["side"]) for c in payload["comments"]], [("src/app.py", 19, 21, "RIGHT")])
        self.assertIn("**Inline one**\n\n- **Issue:** x", payload["comments"][0]["body"])
        self.assertIn("`src/app.py:2`", payload["body"])
        self.assertIn("**No anchor**", payload["body"])
        items = {i["id"]: i for i in self.session().backlog()}
        self.assertEqual({i["status"] for i in items.values()}, {"done"})
        self.assertIn("pullrequestreview-1", items["B1"]["note"])
        # a retry never posts twice
        again = self.rd("pr", sid, "post", "B1")
        self.assertIn("already_posted[1]", again)
        self.assertEqual(len((self.fake / "reviews-7.jsonl").read_text().splitlines()), 1)

    def test_5_bad_links_and_non_pr_sessions_fail_cleanly(self):
        code, out, _ = self.rd_full("open", "--pr", "https://example.com/nope", check=False)
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("error: "))
        code, out, _ = self.rd_full("open", "--pr", "acme/widgets#7", "--base", "main", check=False)
        self.assertEqual(code, 2)
        self.assertIn("--pr takes the code from the pull request", out)
        plain = self.rd("open", "--title", "plain", "--repo", str(self.repo)).split("sid: ")[1].split()[0]
        code, out, _ = self.rd_full("pr", plain, "post", "B1", check=False)
        self.assertEqual(code, 1)
        self.assertIn("does not review a pull request", out)


class CommitPickerTest(Desk):
    """Work committed while a desk is open is appended as commits; it never folds into one diff or vanishes."""

    def git(self, *a) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True, text=True).stdout.strip()

    def session(self):
        import store
        return store.open_session(self.sid)

    def index(self) -> dict:
        return json.loads((self.session().dir / "commits" / "index.json").read_text())

    def test_1_commits_append_and_all_changes_keeps_the_pinned_base(self):
        base = self.git("rev-parse", "HEAD")
        self.assertEqual(self.session().meta()["commit_base"], base)
        self.assertEqual(self.index()["entries"], [])  # nothing committed yet: "All changes" is the uncommitted work
        self.git("add", "-A")
        self.git("commit", "-qm", "Parse rows with csv")
        first = self.git("rev-parse", "HEAD")
        out = self.rd("reload", self.sid)
        self.assertIn("1 since", out)
        (self.repo / "src" / "new.py").write_text("X = 1\nY = 3\nZ = 4\n")
        self.git("commit", "-qam", "Bump Y, add Z")
        second = self.git("rev-parse", "HEAD")
        (self.repo / "src" / "later.py").write_text("L = 1\n")
        out = self.rd("reload", self.sid)
        self.assertIn(f"appended {second[:7]}", out)
        idx = self.index()
        self.assertEqual([e["id"] for e in idx["entries"]], [first, second])
        self.assertEqual([e["subject"] for e in idx["entries"]], ["Parse rows with csv", "Bump Y, add Z"])
        self.assertEqual(idx["uncommitted"]["files"], 1)
        # "All changes" is still everything since the base the desk pinned at open, not just the new HEAD's diff
        self.assertEqual({f["path"] for f in self.session().manifest()["files"]}, {"src/parse.py", "src/new.py", "src/later.py"})
        code, m = self.http("GET", f"/api/{self.sid}/manifest?c={second}")
        self.assertEqual((code, [f["path"] for f in m["files"]]), (200, ["src/new.py"]))
        code, d = self.http("GET", f"/api/{self.sid}/diff/1?c={second}")
        self.assertEqual([r[2] for r in d["rows"] if r[3] == "add"], ["Y = 3", "Z = 4"])
        code, m = self.http("GET", f"/api/{self.sid}/manifest?c=uncommitted")
        self.assertEqual([f["path"] for f in m["files"]], ["src/later.py"])
        code, idx2 = self.http("GET", f"/api/{self.sid}/commits")
        self.assertEqual(idx2["entries"], idx["entries"])
        # a commit's folder is written once: reloading again reuses it
        folder = self.session().dir / "commits" / first / "manifest.json"
        before = folder.stat().st_mtime_ns
        self.rd("reload", self.sid)
        self.assertEqual(folder.stat().st_mtime_ns, before)
        self.assertIn("2 since", self.rd("status", self.sid))

    def test_2_file_view_and_anchors_are_as_of_the_picked_commit(self):
        first = self.index()["entries"][0]["id"]
        code, f = self.http("GET", f"/api/{self.sid}/file?path=src/new.py&c={first}")
        self.assertEqual((code, f["lines"]), (200, ["X = 1", "Y = 2"]))  # the working tree says Y = 3
        for bad in (f"file?path=../x&c={first}", "file?path=src/new.py&c=" + "0" * 40, "manifest?c=HEAD", "diff/1?c=..%2F..%2Fx"):
            self.assertEqual(self.http("GET", f"/api/{self.sid}/{bad}")[0], 404, bad)
        code, r = self.http("POST", f"/api/{self.sid}/flag", {"anchor": {"path": "src/new.py", "range": "2", "side": "new", "commit": first}, "note": "Y was 2 here"})
        self.assertEqual(code, 200, r)
        e = self.session().chat()[-1]
        self.assertEqual((e["anchors"][0]["commit"], e["anchors"][0]["excerpt"]), (first, "2  Y = 2"))
        item = next(i for i in self.session().backlog() if i["id"] == r["item"])
        self.assertEqual(item["anchor"], f"src/new.py:2@{first[:10]}")
        self.assertIn(f"src/new.py:2@{first[:10]}", self.rd("chat", self.sid, str(e["seq"])))
        # a suggested edit on a commit's lines is a patch against that commit's version
        code, r = self.http("POST", f"/api/{self.sid}/suggest", {"anchor": {"path": "src/new.py", "range": "2", "side": "new", "commit": first}, "replacement": "Y = 20"})
        self.assertEqual(code, 200, r)
        patch = next(i for i in self.session().backlog() if i["id"] == r["item"])["patch"]
        self.assertIn("-Y = 2\n+Y = 20", patch)
        # an anchor naming a commit the desk never listed is the working tree's
        code, r = self.http("POST", f"/api/{self.sid}/flag", {"anchor": {"path": "src/new.py", "range": "2", "side": "new", "commit": "f" * 40}})
        e = self.session().chat()[-1]
        self.assertNotIn("commit", e["anchors"][0])
        self.assertEqual(e["anchors"][0]["excerpt"], "2  Y = 3")

    def test_3_desk_carries_the_picker(self):
        page = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/s/{self.sid}?t={self.token}", timeout=5).read().decode()
        for needle in ('id="commit-pick"', 'id="cp-btn"', 'id="cp-menu"', 'id="cp-prev"', 'id="cp-next"'):
            self.assertIn(needle, page)
        js = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/assets/desk.js", timeout=5).read().decode()
        for needle in ("function setCommit(", "function openRef(", "manifest?c=", "withCommit("):
            self.assertIn(needle, js)

    def test_4_each_commit_shows_its_own_story(self):
        first, second = [e["id"] for e in self.index()["entries"]]
        tmp = Path(self.tmp.name)
        for name, text in (("one", "Story of the first commit"), ("rest", "Story of the uncommitted rest")):
            (tmp / f"{name}.html").write_text(f"<!doctype html><title>{name}</title><body><p>{text}</p></body>")
            (tmp / f"{name}.md").write_text(f"## {text}\n\nDetail for {name}.\n\n### Every change\n<details>files</details>\n")
        out = self.rd("story", self.sid, "set", first[:7], "--page", str(tmp / "one.html"), "--summary", str(tmp / "one.md"))
        self.assertIn(f"{first[:10]},{first[:7]} Parse rows with csv", out)
        self.rd("story", self.sid, "set", "uncommitted", "--page", str(tmp / "rest.html"), "--summary", str(tmp / "rest.md"))
        code, out, _ = self.rd_full("story", self.sid, "set", "0" * 7, "--page", str(tmp / "one.html"), check=False)
        self.assertEqual(code, 1)
        self.assertIn("is not a commit this desk lists", out)
        page = lambda q: urllib.request.urlopen(f"http://127.0.0.1:{self.port}/s/{self.sid}/story?{q}t={self.token}", timeout=5).read().decode()
        self.assertIn("Story of the first commit", page(f"c={first}&"))
        self.assertIn("Story of the uncommitted rest", page("c=uncommitted&"))
        for other in (f"c={second}&", "", "c=..%2F..%2Fx&"):  # no story of its own: the overview
            self.assertIn("No summary page", page(other))
        code, st = self.http("GET", f"/api/{self.sid}/state")
        self.assertEqual(sorted(st["stories"]), sorted([first, "uncommitted"]))
        self.assertEqual(st["stories"][first]["label"], f"{first[:7]} Parse rows with csv")
        ctx = (self.session().dir / "context.md").read_text()
        self.assertIn("## Per-commit summaries", ctx)
        self.assertLess(ctx.index("Story of the first commit"), ctx.index("Story of the uncommitted rest"))
        self.assertNotIn("<details>files</details>", ctx)  # the inventory gives way to the file map
        self.assertIn("2 commit stories", self.rd("status", self.sid))
        self.rd("story", self.sid, "remove", first[:7])
        self.assertNotIn(first, self.http("GET", f"/api/{self.sid}/state")[1]["stories"])
        self.assertNotIn("Story of the first commit", (self.session().dir / "context.md").read_text())
        js = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/assets/desk.js", timeout=5).read().decode()
        for needle in ("function showStory(", "story?${key === \"all\"", "Story · "):
            self.assertIn(needle, js)


class SteTest(Desk):
    """Text a person reads in the desk is checked against Simplified Technical English; warnings never block."""

    def test_ste_command_and_warnings_on_reply_and_backlog(self):
        out = self.rd("ste", "-", input="The charge is sent; it might fail.\n")
        self.assertIn("ste: 3 warnings in 1 sentences (stdin)", out)
        for rule in ("passive", "hedge", "semicolon"):
            self.assertIn(f" {rule}:", out)
        self.assertIn("no warnings", self.rd("ste", "-", input="The bank gets the charge.\n"))
        seq = self.say("why does it retry?")
        out = self.rd("reply", self.sid, "--to", str(seq), "--file", "-", input="The retry is triggered by the worker.\n")
        self.assertIn("posted:", out)
        self.assertIn("passive", out)
        self.assertEqual(self.session_chat()[-1]["text"], "The retry is triggered by the worker.")  # posted as written
        clean = self.rd("reply", self.sid, "--to", str(seq), "--file", "-", input="The worker retries the charge once.\n")
        self.assertNotIn("warning", clean)
        out = self.rd("backlog", self.sid, "add", "--title", "Retry is not bounded", "--detail-file", "-",
                      input="- **Issue**: the loop would run forever.\n")
        self.assertIn("created:", out)
        self.assertIn("hedge", out)

    def session_chat(self):
        import store
        return store.open_session(self.sid).chat()


class TabPresenceTest(Desk):
    """A hidden desk tab closes its event stream (a browser keeps 6 connections per host) and checks in instead;
    the server still counts it as open, so `open` does not open a duplicate tab and the idle watchdog waits."""

    @classmethod
    def setUpClass(cls):
        os.environ["REVIEW_DESK_SEEN_SECONDS"] = "3"
        try:
            super().setUpClass()
        finally:
            del os.environ["REVIEW_DESK_SEEN_SECONDS"]

    def tabs(self) -> int:
        return self.health()["clients"].get(self.sid, 0)

    def stream(self, tab: str):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", f"/api/{self.sid}/events?tab={tab}&t={self.token}")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        r.readline()  # the first event: the stream is registered
        return c

    def test_streaming_and_hidden_tabs_count_until_their_check_ins_lapse(self):
        self.assertEqual(self.tabs(), 0)
        a = self.stream("tabA")
        self.assertEqual(self.tabs(), 1)
        self.assertEqual(self.http("GET", f"/api/{self.sid}/seen?tab=tabB")[0], 200)
        self.assertEqual(self.tabs(), 2)  # one streaming, one hidden
        self.assertEqual(self.http("GET", f"/api/{self.sid}/seen?tab=tabC", token=None)[0], 403)
        self.http("GET", f"/api/{self.sid}/seen?tab=" + "x" * 65)  # not a tab id: ignored
        self.assertEqual(self.tabs(), 2)
        a.close()  # tab A goes hidden: it counts until its first check-in is due
        time.sleep(0.5)
        self.assertEqual(self.tabs(), 2)
        time.sleep(3.2)  # neither tab checked in again
        self.assertEqual(self.tabs(), 0)

    def test_desk_closes_its_stream_while_hidden(self):
        js = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/assets/desk.js", timeout=5).read().decode()
        for needle in ("function park(", "function resume(", '"visibilitychange"', "seen?tab=", "events?tab="):
            self.assertIn(needle, js)


class GitDiffTest(unittest.TestCase):
    def test_untracked_symlinks_and_binaries_open(self):
        import gitdiff
        with tempfile.TemporaryDirectory() as t:
            repo = make_repo(Path(t))
            (Path(t) / "elsewhere").mkdir()
            (repo / "linked").symlink_to(Path(t) / "elsewhere")  # a symlink to a directory
            (repo / "blob.bin").write_bytes(b"\x89PNG\0\0data")
            m = gitdiff.write(repo, Path(t) / "out")
            by = {f["path"]: f for f in m["files"]}
            self.assertEqual(by["linked"]["notes"], [f"symlink -> {Path(t) / 'elsewhere'}"])
            self.assertEqual((by["linked"]["adds"], by["linked"]["binary"]), (1, False))
            self.assertEqual((by["blob.bin"]["binary"], by["blob.bin"]["adds"], by["blob.bin"]["hunks"]), (True, 0, []))
            rows = json.loads((Path(t) / "out" / "diffs" / f"{by['blob.bin']['n']}.json").read_text())["rows"]
            self.assertEqual(rows, [])


class SearchFallbackTest(unittest.TestCase):
    def test_git_grep_fallback_matches_ripgrep(self):
        import search
        with tempfile.TemporaryDirectory() as t:
            repo = make_repo(Path(t))
            rows = lambda spec, files: [(r["path"], r["line"], r["ranges"]) for r in search.run(repo, spec, files, {})  # noqa: E731
                                        if r["t"] == "m"]
            want = rows(search.Spec("split_row"), None) if shutil.which("rg") else None
            real = search.shutil.which
            search.shutil.which = lambda name: None if name == "rg" else real(name)  # as on a machine without ripgrep
            try:
                got = rows(search.Spec("split_row"), None)
                changed = rows(search.Spec("SPLIT_ROW"), ["src/parse.py"])
                bad = list(search.run(repo, search.Spec("ses(sion", regex=True), None, {}))[-1]
            finally:
                search.shutil.which = real
            self.assertEqual([g[0] for g in got], ["src/parse.py"])
            self.assertEqual(changed, got, "case-insensitive by default, and an explicit file list works")
            if want is not None:
                self.assertEqual(got, want, "git grep finds the same lines and ranges as ripgrep")
            self.assertEqual(bad["t"], "error")


class DurabilityTest(unittest.TestCase):
    def test_kill9_mid_write_loses_nothing_committed(self):
        import store
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["REVIEW_DESK_HOME"] = tmp
            s = store.create(None, title="t", repo=tmp)
            writer = (f"import sys; sys.path.insert(0, {str(SKILL / 'scripts')!r}); import store\n"
                      f"s = store.open_session({s.sid!r})\n"
                      "for n in range(100000):\n"
                      "    s.backlog_add(f'item {n}')\n"
                      "    print(n, flush=True)\n")
            p = subprocess.Popen([sys.executable, "-c", writer], stdout=subprocess.PIPE, text=True,
                                 env={**os.environ, "REVIEW_DESK_HOME": tmp})
            acked = -1
            while acked < 40:
                acked = int(p.stdout.readline())
            p.send_signal(signal.SIGKILL)
            p.wait()
            p.stdout.close()
            items = s.backlog()
            self.assertGreaterEqual(len(items), acked + 1, "every add that returned is on disk")
            self.assertEqual([i["id"] for i in items], [f"B{n}" for n in range(1, len(items) + 1)], "ids stay dense")
            # a torn final line (killed inside write) is skipped, and the next append still works
            with open(s.backlog_path, "a") as fh:
                fh.write('{"op":"create","id":"B9999","ti')
            self.assertEqual(len(s.backlog()), len(items))
            first = s.handoff()
            self.assertEqual(len(first), len(items))
            self.assertEqual(len(s.handoff()), len(items), "delivered again until acked")
            s.backlog_status([i["id"] for i in items], "acked", by="main")
            self.assertEqual(s.handoff(), [])


@unittest.skipUnless(IMPL.exists(), "implementation-summary is not installed")
class ImplementationSummaryTest(unittest.TestCase):
    def test_build_page_review_data_feeds_the_desk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = make_repo(root)
            body = root / "body.html"
            body.write_text('<header class="hero"><h1>x</h1></header><p><a data-loc="src/parse.py:4"></a></p>')
            env = {**os.environ, "IMPL_SUMMARY_ROOT": str(root / "archive"), "REVIEW_DESK_HOME": str(root / "home")}
            out = subprocess.run([sys.executable, str(IMPL), "--repo", str(repo), "--body", str(body), "--title", "T",
                                  "--review-data", "--editor", "none"], env=env, capture_output=True, text=True, check=True).stdout
            rd = Path(out.split("review data: ")[1].split(" (")[0])
            m = json.loads((rd / "manifest.json").read_text())
            self.assertEqual({f["path"] for f in m["files"]}, {"src/parse.py", "src/new.py"})
            cited = {f["path"]: f["cited"] for f in m["files"]}
            self.assertTrue(cited["src/parse.py"])
            rows = json.loads((rd / f"diffs/{next(f['n'] for f in m['files'] if f['path'] == 'src/parse.py')}.json").read_text())["rows"]
            self.assertTrue(any(r[3] == "add" and "csv.reader" in r[2] for r in rows))
            # a same-day rebuild under the same title must keep the session folder
            (rd / "session.json").write_text("{}")
            subprocess.run([sys.executable, str(IMPL), "--repo", str(repo), "--body", str(body), "--title", "T",
                            "--review-data", str(rd), "--editor", "none"], env=env, capture_output=True, text=True, check=True)
            self.assertTrue((rd / "session.json").exists())

    def test_desk_lists_a_feature_branchs_commits_from_the_summarys_base(self):
        """build_page diffs a feature branch against its merge-base with main; the desk's picker counts from there."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = make_repo(root)
            g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
            g("branch", "-M", "main")
            g("checkout", "-qb", "feature")
            g("add", "-A")
            g("commit", "-qm", "one")
            (repo / "src" / "new.py").write_text("X = 1\nY = 2\nZ = 3\n")
            g("commit", "-qam", "two")
            body = root / "body.html"
            body.write_text('<header class="hero"><h1>x</h1></header>')
            port = free_port()
            env = {**os.environ, "IMPL_SUMMARY_ROOT": str(root / "archive"), "REVIEW_DESK_HOME": str(root / "home"),
                   "REVIEW_DESK_PORT": str(port), "REVIEW_DESK_NO_OPEN": "1"}
            out = subprocess.run([sys.executable, str(IMPL), "--repo", str(repo), "--body", str(body), "--title", "T",
                                  "--review-data", "--editor", "none"], env=env, capture_output=True, text=True, check=True).stdout
            rd = Path(out.split("review data: ")[1].split(" (")[0])
            self.assertEqual(json.loads((rd / "manifest.json").read_text())["base"], g("merge-base", "HEAD", "main"))
            try:
                out = subprocess.run([*CLI, "open", "--dir", str(rd), "--title", "T", "--repo", str(repo)], env=env,
                                     capture_output=True, text=True, check=True).stdout
                self.assertIn("2 since", out)
                idx = json.loads((rd / "commits" / "index.json").read_text())
                self.assertEqual([e["subject"] for e in idx["entries"]], ["one", "two"])
            finally:
                # no server came up: nothing to stop
                with contextlib.suppress(OSError, ValueError, KeyError), urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                    os.kill(json.loads(r.read())["pid"], signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
