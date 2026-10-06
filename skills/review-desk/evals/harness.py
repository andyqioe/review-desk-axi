#!/usr/bin/env python3
"""Eval harness for review-desk: seed a session, play the user, grade from the session files.

  harness.py setup <id> --run <dir> [--baseline]   build the fixture repo + isolated home, seed the session,
                                       write <dir>/prompt.txt (the task) and <dir>/executor_note.txt
  harness.py host <id> --run <dir> [--skill D] [--model M --effort E]   run the live reviewer host (role "host")
  harness.py drive <id> --run <dir>    the scripted user: posts the eval's driver steps (end, tier,
                                       execute, msg) when their condition holds; run it in the
                                       background while the executor works; exits when done
  harness.py check <id> --run <dir>    grade: writes <dir>/grading.json (skill-creator schema);
                                       the executor's final message must be in <dir>/final.md

Every run gets its own REVIEW_DESK_HOME and port (in <dir>/env), so runs never share state.
Checks read the session files, the fixture repo and final.md, so grading is deterministic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(SKILL / "scripts"))

EVALS = json.loads((HERE / "evals.json").read_text())["evals"]
LINE_REF = re.compile(r"[\w./-]+\.\w+:\d+")


def ev(eval_id: int) -> dict:
    return next(e for e in EVALS if e["id"] == eval_id)


def use_home(run: Path) -> None:
    env = dict(line.split("=", 1) for line in (run / "env").read_text().split())
    os.environ.update(env)


# ---------------------------------------------------------------- fixture
CSV_BASE = {
    "src/__init__.py": "",
    "src/parse.py": '"""Row parsing for the CSV ingest."""\n\n\ndef split_row(line):\n    return line.split(",")\n\n\n'
                    "def parse(lines):\n    rows = []\n    for line in lines:\n        if not line.strip():\n            continue\n"
                    "        rows.append(split_row(line))\n    return rows\n",
    "src/ingest.py": "from src.parse import parse\n\n\ndef legacy_retry(fn):\n    return fn()\n\n\n"
                     "def ingest(path):\n    with open(path) as fh:\n        return parse(fh.read().splitlines())\n",
    "sample.csv": 'name,city\n"Smith, John",Oslo\nAda,London\n',
}
CSV_CHANGE = {
    "src/parse.py": '"""Row parsing for the CSV ingest."""\nimport csv\n\n\ndef split_row(line):\n'
                    '    """Split one CSV line, keeping commas inside quoted fields."""\n    return next(csv.reader([line]))\n\n\n'
                    "def parse(lines):\n    rows = []\n    for line in lines:\n        if not line.strip():\n            continue\n"
                    "        row = split_row(line)\n        if len(row) < 2:\n            raise ValueError(f\"short row: {line!r}\")\n"
                    "        rows.append(row)\n    return rows\n",
    "src/ingest.py": "from src.parse import parse\n\n\ndef ingest(path):\n    with open(path) as fh:\n        return parse(fh.read().splitlines())\n",
    "tests/__init__.py": "",
    "tests/test_parse.py": "from src.parse import split_row\n\n\ndef test_quoted_comma():\n"
                           "    assert split_row('\"Smith, John\",Oslo') == [\"Smith, John\", \"Oslo\"]\n",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def build_fixture(repo: Path) -> None:
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "eval@example.com")
    git(repo, "config", "user.name", "eval")
    for files in (CSV_BASE,):
        for rel, text in files.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "csv ingest")
    for rel, text in CSV_CHANGE.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)


def snapshot(repo: Path) -> dict[str, str]:
    out = {}
    for p in sorted(repo.rglob("*")):
        if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts:
            out[str(p.relative_to(repo))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ setup
def seed(spec: dict, repo: Path):
    import store
    import gitdiff
    from review_desk import build_context

    s = store.create(None, title="Quoted commas survive", repo=str(repo))
    gitdiff.write(repo, s.dir, "HEAD", None)
    summary = repo.parent / "summary.md"
    summary.write_text("# Quoted commas survive\n\nRows like \"Smith, John\" are kept: `split_row` uses `csv.reader` "
                       "(`src/parse.py:7`); rows with fewer than 2 fields raise `ValueError` (`src/parse.py:16-17`); "
                       "`legacy_retry` had no callers and was deleted from `src/ingest.py`.\n\n### Proof\n- `pytest -q` - 1 passed\n")
    build_context(s, summary, None)
    run = repo.parent
    for pg in spec.get("pages", []):  # HTML pages already shown as tabs (an implementation summary, an older one)
        path = run / pg["file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(pg["html"])
        if pg.get("registered", True):
            made, _ = s.page_open(str(path), title=pg.get("title"), focus=False, by=pg.get("by", "main"))
            if not pg.get("open", True):
                s.page_close(made["id"], by=pg.get("by", "main"))
    for e in spec.get("chat", []):
        fields = {k: (v.replace("{RUN}", str(run)) if isinstance(v, str) else v) for k, v in e.items() if k not in ("kind", "role")}
        s.post(e["kind"], e["role"], **fields)
    for b in spec.get("backlog", []):
        item = s.backlog_add(b["title"], b.get("detail", ""), b.get("kind", "fix"), b.get("anchor"), b.get("from"),
                             b.get("patch"), by=b.get("by", "reviewer"))
        if b.get("status", "open") != "open":
            s.backlog_status([item["id"]], b["status"], by="seed")
    if spec.get("agent"):
        a = dict(spec["agent"])
        age = a.pop("ts_age", 0)
        s.set_agent(**a, cursor=s.chat()[-1]["seq"] if s.chat() else 0, timeout=540)
        if age:
            data = json.loads(s.agent_path.read_text())
            data["ts"] -= age
            s.agent_path.write_text(json.dumps(data))
    return s


def cli(run: Path, *args: str, stdin: str | None = None) -> str:
    env = {**os.environ, **dict(line.split("=", 1) for line in (run / "env").read_text().split())}
    return subprocess.run([sys.executable, str(SKILL / "scripts" / "review_desk.py"), *args], env=env, input=stdin,
                          capture_output=True, text=True, cwd=run / "repo").stdout


def cmd_setup(e: dict, run: Path, baseline: bool = False) -> None:
    if run.exists() and any(run.iterdir()):
        sys.exit(f"{run} is not empty")
    if e["role"] == "host" and (Path.home() / ".claude") in run.resolve().parents:
        sys.exit(f"{run}: put host runs outside ~/.claude; Claude Code refuses the reviewer's writes there")
    run.mkdir(parents=True, exist_ok=True)
    repo = run / "repo"
    build_fixture(repo)
    port = free_port()
    (run / "env").write_text(f"REVIEW_DESK_HOME={run / 'home'} REVIEW_DESK_PORT={port} REVIEW_DESK_NO_OPEN=1\n")
    (run / "home").mkdir()
    (run / "home" / "config.json").write_text(json.dumps({"port": port, "no_open": True}))
    use_home(run)
    sid = ""
    if e["setup"]["session"] is not None:
        sid = seed(e["setup"]["session"], repo).sid
    (run / "snapshot.json").write_text(json.dumps(snapshot(repo), indent=1))
    prompt = e["prompt"].replace("{SID}", sid)
    if "{HANDOFF}" in prompt:
        prompt = prompt.replace("{HANDOFF}", cli(run, "handoff", sid).strip())
    if "{INBOX}" in prompt:
        injected = cli(run, "hook", "prompt", stdin=json.dumps({"cwd": str(repo), "session_id": "eval"})).strip()
        prompt = prompt.replace("{INBOX}", f"<system-reminder>\nUserPromptSubmit hook additional context:\n{injected}\n</system-reminder>")
    if e["role"] == "host":  # the live reviewer host runs it (`harness.py host`); no executor agent
        (run / "prompt.txt").write_text("\n".join(m.get("text", "") for m in e["setup"]["session"].get("chat", [])) + "\n")
        print(json.dumps({"run": str(run), "sid": sid, "next": f"harness.py host {e['id']} --run {run}"}))
        return
    if e["role"] == "reviewer" and not baseline:  # the baseline gets the bare prompt, no reviewer protocol
        effort = "low"
        reviewer = subprocess.run([sys.executable, str(SKILL / "scripts" / "review_desk.py"), "prompt", sid or "x",
                                   "--model", "haiku", "--effort", effort], capture_output=True, text=True).stdout
        prompt = f"{reviewer.strip()}\n\n---\n{prompt}"
    (run / "prompt.txt").write_text(prompt + "\n")
    (run / "executor_note.txt").write_text(
        f"Eval environment. The repository is {repo}; run commands from there.\n"
        f"Pass `--home {run / 'home'}` as the first argument of every `review-desk-axi` command "
        f"(`review-desk-axi --home {run / 'home'} <command> ...`) so it uses this eval's isolated session store.\n"
        "Do not ask the user questions: nobody will answer. Where the skill says to spawn a subagent and you cannot, "
        "write the exact Agent call (subagent_type, model, prompt) in your final message instead.\n"
        f"Save your final message, verbatim, to {run / 'final.md'}.\n")
    print(json.dumps({"run": str(run), "sid": sid, "prompt": str(run / "prompt.txt"),
                      "executor_note": str(run / "executor_note.txt"), "needs_driver": bool(e["driver"])}))


# ------------------------------------------------------------------ drive
def session_for(run: Path):
    import store
    use_home(run)
    repo = (run / "repo").resolve()
    for s in store.sessions():
        if Path(s.meta().get("repo", "/")).resolve() == repo:
            return s
    return None


def cmd_drive(e: dict, run: Path, timeout: int) -> None:
    import store
    log = open(run / "driver.log", "a")
    steps = list(e["driver"])
    deadline = time.time() + timeout
    prev_done = False
    while steps and time.time() < deadline:
        s = session_for(run)
        step = steps[0]
        ready = False
        if s is not None:
            a = s.agent()
            if step["when"] == "after_previous":
                ready = prev_done
            elif step["when"] == "answered_all":
                ready = a.get("state") == "waiting" and not s.unanswered()
        if not ready:
            prev_done = False
            time.sleep(0.5)
            continue
        kind = step["do"]
        if kind == "end":
            s.post("end", "user")
            s.update_meta(status="ended")
        elif kind == "tier":
            s.post("tier", "user", model=step["model"], effort=step["effort"])
        elif kind == "execute":
            ids = [i["id"] for i in s.backlog() if i["status"] in store.OPEN_STATUSES]
            s.post("execute", "user", ids=ids)
        elif kind == "msg":
            p = s.presence()
            if p["live"] and not p.get("main") and (p.get("model"), p.get("effort")) != (step.get("model"), step.get("effort")):
                s.post("tier", "user", model=step.get("model"), effort=step.get("effort"))  # what the server's tier_guard does
            s.post("msg", "user", text=step["text"], model=step.get("model"), effort=step.get("effort"))
        log.write(f"{time.strftime('%H:%M:%S')} {step}\n")
        log.flush()
        steps.pop(0)
        prev_done = True
        time.sleep(1.0)  # let the executor's wait pick it up before the next condition is evaluated
    log.write(f"{time.strftime('%H:%M:%S')} done, {len(steps)} step(s) left\n")


# ------------------------------------------------------------------ host
def cmd_host(e: dict, run: Path, skill: Path, model: str, effort: str, timeout: int) -> None:
    """Run the real reviewer host (one `claude -p` session) of `skill` on the seeded session, play the driver
    steps once everything is answered, stop the host, and save the chat answers to final.md."""
    s = session_for(run)
    env = {**os.environ, **dict(line.split("=", 1) for line in (run / "env").read_text().split())}
    rd = [sys.executable, str(skill / "scripts" / "review_desk.py")]
    log = open(run / "driver.log", "a")
    out = subprocess.run([*rd, "reviewer", s.sid, "start", "--model", model, "--effort", effort], env=env, cwd=run / "repo",
                         capture_output=True, text=True)
    log.write(f"{time.strftime('%H:%M:%S')} start: {out.stdout.strip()} {out.stderr.strip()}\n")
    deadline = time.time() + timeout
    t0 = time.time()

    def settle() -> bool:
        while time.time() < deadline:
            a = s.agent()
            if a.get("state") == "exited":
                return False
            if a.get("state") == "waiting" and not s.unanswered() and time.time() - t0 > 5:
                return True
            time.sleep(2)
        return False

    try:
        for step in [*e["driver"], None]:
            if not settle() or step is None:
                break
            if step["do"] == "msg":
                s.post("msg", "user", text=step["text"].replace("{RUN}", str(run)), model=model, effort=effort)
            elif step["do"] == "end":
                s.post("end", "user")
                s.update_meta(status="ended")
            log.write(f"{time.strftime('%H:%M:%S')} {step}\n")
            log.flush()
            time.sleep(3)
    finally:
        subprocess.run([*rd, "reviewer", s.sid, "stop"], env=env, capture_output=True, text=True)
    (run / "timing.json").write_text(json.dumps({"duration_ms": int((time.time() - t0) * 1000),
                                                 "total_duration_seconds": round(time.time() - t0, 1)}))
    answers = [f"### #{e2['seq']} reviewer\n\n{e2.get('text', '')}" for e2 in agent_msgs(s)]
    pages = [f"- {pg['id']} {'open' if pg['open'] else 'closed'} by {pg.get('by')}{', closed by ' + pg['closed_by'] if pg.get('closed_by') else ''}: {pg['path']}"
             for pg in s.pages()]
    (run / "final.md").write_text("\n\n".join(answers) + "\n\n### Pages\n\n" + ("\n".join(pages) or "none") + "\n")
    log.write(f"{time.strftime('%H:%M:%S')} host done\n")


# ------------------------------------------------------------------ check
def final_text(run: Path) -> str:
    p = run / "final.md"
    return p.read_text() if p.exists() else ""


def items(s) -> dict[str, dict]:
    return {i["id"]: i for i in s.backlog()} if s else {}


def agent_msgs(s) -> list[dict]:
    return [e for e in s.chat() if e.get("role") == "agent"] if s else []


def check_one(c: dict, run: Path, s) -> tuple[bool, str]:
    fn = c["fn"]
    repo = run / "repo"
    final = final_text(run)
    if fn == "session_exists":
        return s is not None, f"session {s.sid}" if s else "no session for the fixture repo"
    if s is None and fn not in ("repo_unchanged", "file_contains", "file_matches", "fixture_tests_pass", "final_mentions",
                                "final_starts", "final_ends_with_url"):
        return False, "no session for the fixture repo"
    if fn == "manifest_files":
        n = len(s.manifest().get("files", []))
        return n == c["count"], f"{n} files"
    if fn in ("tray_covers", "tray_has"):
        tray = s.tray()
        for t in tray:
            r = t["ref"]
            m = re.fullmatch(r"(.*?)(?::(\d+)(?:-(\d+))?)?", r)
            if m[1] != c["path"] or (c.get("note") and not t.get("note")):
                continue
            if fn == "tray_has":
                return True, f"{r} note={t.get('note')!r}"
            if m[2] and int(m[2]) <= c["line"] <= int(m[3] or m[2]):
                return True, f"{r} note={t.get('note')!r}"
        return False, f"tray: {[(t['ref'], t.get('note')) for t in tray]}"
    if fn == "context_contains":
        text = s.context_path.read_text() if s.context_path.exists() else ""
        m = re.search(c["regex"], text)
        return bool(m), f"context.md {'contains' if m else 'lacks'} {c['regex']}"
    if fn == "prefs":
        import store
        p = store.prefs()
        return (p.get("model"), p.get("effort")) == (c["model"], c["effort"]), str(p)
    if fn == "final_mentions":
        low = final.lower()
        miss = [t for t in c.get("all", []) if t.lower() not in low]
        bad = [t for t in c.get("none", []) if t.lower() in low]
        start_ok = final.lstrip().startswith(c["starts"]) if c.get("starts") else True
        ok = not miss and not bad and start_ok and bool(final)
        return ok, ("final.md missing" if not final else f"missing {miss}; forbidden present {bad}; starts ok {start_ok}")
    if fn == "final_starts":
        return final.lstrip().startswith(c["text"]), final.strip()[:80] or "final.md missing"
    if fn == "final_ends_with_url":
        last = [l for l in final.strip().splitlines() if l.strip()][-1:] or [""]
        return "http://127.0.0.1:" in last[0], last[0][:120]
    if fn == "repo_unchanged":
        before = json.loads((run / "snapshot.json").read_text())
        after = snapshot(repo)
        diff = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        return not diff, f"changed: {diff}" if diff else "identical to the setup snapshot"
    if fn == "items_status":
        got = {i: items(s).get(i, {}).get("status") for i in c["ids"]}
        return all(v == c["status"] for v in got.values()), str(got)
    if fn == "all_items_status":
        got = {i: x["status"] for i, x in items(s).items()}
        return bool(got) and all(v == c["status"] for v in got.values()), str(got)
    if fn == "items_have_note":
        got = {i: items(s).get(i, {}).get("note") for i in c["ids"]}
        return all(v and len(v) > 8 for v in got.values()), str(got)
    if fn == "file_contains":
        text = (repo / c["path"]).read_text() if (repo / c["path"]).exists() else ""
        return c["text"] in text, f"{c['text']!r} {'found' if c['text'] in text else 'not found'} in {c['path']}"
    if fn == "file_lacks":
        text = (repo / c["path"]).read_text() if (repo / c["path"]).exists() else ""
        return c["text"] not in text, f"{c['text']!r} {'still in' if c['text'] in text else 'gone from'} {c['path']}"
    if fn == "final_question_about":
        qs = [l.strip() for l in re.split(r"(?<=[?])\s+|\n", final) if l.strip().endswith("?")]
        para = [q for q in qs if c["text"] in q] or ([q for q in qs] if c["text"] in final else [])
        return bool(para), f"questions: {qs[:3]}" if qs else "no question in final.md"
    if fn == "file_matches":
        text = (repo / c["path"]).read_text() if (repo / c["path"]).exists() else ""
        m = re.search(c["regex"], text)
        return bool(m), m.group(0) if m else f"no match for {c['regex']} in {c['path']}"
    if fn == "fixture_tests_pass":
        out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=repo, capture_output=True, text=True)
        tail = (out.stdout.strip().splitlines() or out.stderr.strip().splitlines() or [""])[-1]
        return out.returncode == 0, tail
    if fn == "reloaded_after_done":
        done_at = [ev.get("at", 0) for ev in s.backlog_events() if ev.get("op") == "status" and ev.get("status") == "done"]
        rel = s.meta().get("reloaded")
        return bool(rel and done_at and rel >= max(done_at)), f"reloaded={rel} last done={max(done_at) if done_at else None}"
    if fn == "answered":
        open_ = {e["seq"] for e in s.unanswered()}
        miss = [q for q in c["seqs"] if q in open_]
        return not miss, f"unanswered: {sorted(open_)}"
    if fn == "unanswered_text":
        hit = [e["seq"] for e in s.unanswered() if c["text"] in e.get("text", "")]
        return bool(hit), f"unanswered seqs containing it: {hit}"
    if fn == "reviewer_items":
        mine = [i for i in items(s).values() if i.get("by") == "reviewer"]
        ok = len(mine) == c["count"] and all((i.get("anchor") or "").startswith(c.get("anchor_prefix", ""))
                                             and re.search(c.get("regex", ""), f"{i['title']} {i.get('detail', '')}") for i in mine)
        return ok, str([(i["id"], i.get("anchor"), i["title"]) for i in mine])
    if fn == "details_structured":
        # Every item the reviewer logged has the five bullets in order, each with text, and cites code as path:line.
        labels = ["Context", "Issue", "Suggested fix", "Reasoning", "Tests"]
        mine = [i for i in items(s).values() if i.get("by") == "reviewer"]
        bad = []
        for i in mine:
            d = i.get("detail") or ""
            pos = [re.search(rf"(?m)^- \*\*{re.escape(l)}:?\*\*:?\s*\S", d) for l in labels]
            if not all(pos) or [m.start() for m in pos] != sorted(m.start() for m in pos):
                bad.append(f"{i['id']}: sections {[l for l, m in zip(labels, pos) if m]}")
            elif not LINE_REF.search(d):
                bad.append(f"{i['id']}: no path:line")
        return bool(mine) and not bad, "; ".join(bad) or f"{len(mine)} structured"
    if fn == "no_item_matching":
        hit = [i["id"] for i in items(s).values() if re.search(c["regex"], f"{i['title']} {i.get('detail', '')}")]
        return not hit, f"matching items: {hit}"
    if fn == "item_matching":
        hit = [i for i in items(s).values() if re.search(c["regex"], f"{i['title']} {i.get('detail', '')} {i.get('patch', '')}")
               and (i.get("anchor") or "").startswith(c["anchor_prefix"])]
        return bool(hit), str([(i["id"], i.get("anchor"), i["title"]) for i in items(s).values()])
    if fn == "replies_cite_lines":
        hit = [m for e in agent_msgs(s) for m in LINE_REF.findall(e.get("text", ""))]
        return bool(hit), f"citations: {hit[:5]}"
    if fn == "agent_said":
        hit = [e["seq"] for e in agent_msgs(s) if re.search(c["regex"], e.get("text", ""))]
        return bool(hit), f"agent messages matching: {hit}"
    if fn == "agent_reason":
        a = s.agent()
        return a.get("state") == "exited" and a.get("reason") == c["reason"], f"state={a.get('state')} reason={a.get('reason')}"
    if fn == "agent_main":
        return bool(s.agent().get("main")), f"agent.json main={s.agent().get('main')}"
    if fn in ("page_open", "page_closed"):
        pages_dir = s.pages_dir
        hits = []
        for pg in s.pages():
            path = Path(pg["path"])
            if fn == "page_open" and not (pg["open"] and pg.get("by") == c.get("by", pg.get("by"))):
                continue
            if fn == "page_closed" and not (not pg["open"] and pg.get("closed_by") == c.get("by", pg.get("closed_by"))):
                continue
            if c.get("under_pages") and pages_dir not in path.parents:
                continue
            if c.get("path_regex") and not re.search(c["path_regex"], str(path)):
                continue
            if c.get("file_regex"):
                text = path.read_text(errors="replace") if path.is_file() else ""
                if not re.search(c["file_regex"], text):
                    continue
            hits.append(pg["id"])
        return bool(hits), f"matching: {hits}; all: {[(pg['id'], pg['open'], pg.get('by'), pg.get('closed_by'), Path(pg['path']).name) for pg in s.pages()]}"
    if fn == "no_new_pages":
        made = sorted(q.name for q in s.pages_dir.glob("*.htm*")) if s.pages_dir.exists() else []
        return not made, f"pages/: {made}"
    if fn == "no_repo_path":
        return not (repo / c["path"]).exists(), f"{c['path']} {'exists' if (repo / c['path']).exists() else 'absent'}"
    if fn == "answered_all_no_tier":
        tiers = [e["seq"] for e in s.chat() if e["kind"] == "tier"]
        open_ = [e["seq"] for e in s.unanswered()]
        return not tiers and not open_ and len([e for e in s.chat() if e.get("role") == "user" and e["kind"] == "msg"]) >= 2, \
            f"tier events {tiers}, unanswered {open_}"
    return False, f"unknown check {fn}"


def cmd_check(e: dict, run: Path) -> None:
    s = session_for(run)
    rows = []
    for text, c in zip(e["expectations"], e["checks"]):
        try:
            ok, evidence = check_one(c, run, s)
        except Exception as err:  # a crashing check is a failed check, with the reason
            ok, evidence = False, f"check error: {err!r}"
        rows.append({"text": text, "passed": bool(ok), "evidence": evidence})
    passed = sum(r["passed"] for r in rows)
    out = {"expectations": rows, "summary": {"passed": passed, "failed": len(rows) - passed, "total": len(rows),
                                              "pass_rate": round(passed / len(rows), 2) if rows else 0.0}}
    (run / "grading.json").write_text(json.dumps(out, indent=2))
    print(f"{e['name']}: {passed}/{len(rows)}")
    for r in rows:
        print(f"  {'PASS' if r['passed'] else 'FAIL'} {r['text']} - {r['evidence']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["setup", "drive", "host", "check", "list"])
    ap.add_argument("id", type=int, nargs="?")
    ap.add_argument("--run")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--baseline", action="store_true", help="setup: omit the reviewer instructions (without_skill runs)")
    ap.add_argument("--skill", default=str(SKILL), help="host: the review-desk copy whose reviewer host runs (a snapshot for old_skill)")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="low")
    a = ap.parse_args()
    if a.action == "list":
        for e in EVALS:
            print(f"{e['id']}  {e['role']:8}  {e['name']}  ({len(e['expectations'])} expectations, driver steps: {len(e['driver'])})")
        return 0
    if a.id is None or not a.run:
        ap.error("setup/drive/check need an eval id and --run")
    if len(ev(a.id)["expectations"]) != len(ev(a.id)["checks"]):
        sys.exit(f"eval {a.id}: expectations and checks differ in length")
    run = Path(a.run).expanduser().resolve()
    {"setup": lambda: cmd_setup(ev(a.id), run, a.baseline), "drive": lambda: cmd_drive(ev(a.id), run, a.timeout),
     "host": lambda: cmd_host(ev(a.id), run, Path(a.skill).expanduser().resolve(), a.model, a.effort, a.timeout),
     "check": lambda: cmd_check(ev(a.id), run)}[a.action]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
