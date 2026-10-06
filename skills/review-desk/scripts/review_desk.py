#!/usr/bin/env python3
"""review-desk-axi: Review Desk, a local code review editor staffed by a reviewer subagent.

Run with no arguments for the home view (this repo's sessions and any undelivered backlog).

Main agent
  open --title T [--dir D] [--page P] [--summary S] [--notes N] [--repo R]   create or reuse a session
  add <sid> <path[:a-b]>... [--note N] [--focus]   pin files or ranges into the editor
  page <sid> open <file.html> [--title T] [--background] | close <P#|path> | list
                                                   show an HTML page as a read-only editor tab
  run <sid> -- <command...>                        run a page generator; writes confined to pages/
  handoff <sid>                                    backlog the main agent has not accepted yet
  backlog <sid> ack|done|dismiss|reopen <ids> [--note N]
  status <sid> | url <sid> | reload <sid> [--page P] | end <sid> | prefs [--model M --effort E]
  prompt <sid> --model M --effort E               reviewer instructions for a general-purpose spawn
  setup [hooks|bin|agents|all]                     install session hooks, the PATH link, reviewer agents

Reviewer agent
  attach <sid> --model M --effort E [--main]       briefing: context, chat, backlog, pending messages
  wait <sid> [--timeout 540]                       block for the next user events
  reply <sid> --to 4,5 --file - [--then-wait]      answer in the chat (Markdown on stdin)
  backlog <sid> add --title T --anchor path:a-b --from SEQ --detail-file - [--kind fix|question]
                                                   log an issue (Context/Issue/Suggested fix/Reasoning/Tests on stdin)
  backlog <sid> list [--fields a,b] [--full] [--all] | update <id> ...
  chat <sid> [seq...] [--last N] [--full]          read messages in full
  detach <sid> --reason handoff|execute|idle|end

Output is TOON with a help[] block (https://axi.md). Errors print `error:` on stdout and exit 1
(usage errors 2). Writes are idempotent. Disk is the source of truth (store.py), so every
command except open/url works with the server down.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import axi  # noqa: E402
import store  # noqa: E402
from axi import BIN, DeskError, block, clip, help_block, kv, run, table  # noqa: E402

SKILL = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("REVIEW_DESK_PORT", "4388"))
HOST = "127.0.0.1"
CHAT_TAIL = 20
MAP_LIMIT = 120
CLIP_CHAT = 200
DESCRIPTION = ("Review Desk - a local code review editor whose reviewer subagent answers the user's questions "
               "about a change and hands implementation work back to the main agent as a durable backlog.")
BACKLOG_FIELDS = ["id", "status", "kind", "anchor", "from", "by", "title", "detail", "note", "patch"]


def tilde(p) -> str:
    s = str(p)
    h = str(Path.home())
    return "~" + s[len(h):] if s == h or s.startswith(h + "/") else s


def bin_path() -> str:
    return tilde(shutil.which(BIN) or SKILL / "bin" / BIN)


def spawn_hint(sid: str, model: str = "<model>", effort: str = "<effort>") -> str:
    return run(f"reviewer {sid} start --model {model} --effort {effort}") + ", then " + watch_hint(sid)


# ----------------------------------------------------------------- server
def health() -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=1.5) as r:
            return json.loads(r.read())
    except Exception:
        return None


def stop_server(h: dict) -> None:
    """Stop a running desk server and wait until its port is free. Sessions live on disk; browsers reconnect."""
    try:
        os.kill(h["pid"], signal.SIGTERM)
    except (KeyError, OSError):
        return
    for _ in range(50):
        time.sleep(0.1)
        if health() is None:
            return


def ensure_server() -> dict:
    h = health()
    if h and h.get("app") == "review-desk" and Path(h.get("home", "")) == store.home().resolve() \
            and h.get("code") != store.code_id():
        stop_server(h)  # started before the desk was upgraded: it lacks the new routes
        h = health()
    if h is None:
        log = store.home() / "server.log"
        store.home().mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as fh:
            subprocess.Popen([sys.executable, str(SKILL / "scripts" / "server.py"), "--port", str(PORT)],
                             stdout=fh, stderr=fh, stdin=subprocess.DEVNULL, start_new_session=True,
                             env={**os.environ, "REVIEW_DESK_HOME": str(store.home())})
        for _ in range(60):
            time.sleep(0.1)
            h = health()
            if h:
                break
        if h is None:
            raise DeskError(f"server did not start on {HOST}:{PORT}", [f"Read `{tilde(log)}`"])
    if h.get("app") != "review-desk":
        raise DeskError(f"port {PORT} is taken by another program", ["Set REVIEW_DESK_PORT to a free port and retry"])
    if Path(h.get("home", "")) != store.home().resolve():
        raise DeskError(f"the server on {PORT} serves {h.get('home')}, not {store.home()}",
                        ["Set REVIEW_DESK_PORT to a free port for this REVIEW_DESK_HOME"])
    return h


def url_for(s: store.Session) -> str:
    return f"http://{HOST}:{PORT}/s/{s.sid}?t={s.meta()['token']}"


def session(sid: str) -> store.Session:
    try:
        return store.open_session(sid)
    except KeyError:
        raise DeskError(f"no review-desk session {sid}", [run("") + " to list this repo's sessions"])


# ------------------------------------------------------------------- rows
def tier_of(e: dict) -> str:
    return f"{e['model']}/{e.get('effort', '?')}" if e.get("model") else ""


def anchors_of(e: dict) -> str:
    return " ".join(f"{'old:' if a.get('side') == 'old' else ''}{a['path']}" + (f":{a['range']}" if a.get("range") else "")
                    for a in e.get("anchors", []))


def event_row(e: dict, sid: str, full: bool = True) -> dict:
    kind = {"tier": "tier-change"}.get(e["kind"], e["kind"])
    if e.get("skill"):
        kind = f"skill:{e['skill']}"
    text = e.get("text", "")
    if not full:
        text = clip(text, CLIP_CHAT, f"{BIN} chat {sid} {e['seq']}")
    return {"seq": e["seq"], "role": "reviewer" if e.get("role") == "agent" else e.get("role"), "kind": kind,
            "tier": tier_of(e), "reply_to": " ".join(f"#{i}" for i in e.get("reply_to", [])), "anchors": anchors_of(e),
            "item": e.get("item"), "ids": " ".join(e.get("ids", [])), "text": text,
            "excerpt": "\n".join(a.get("excerpt") or "" for a in e.get("anchors", [])).strip() if full else "",
            "suggestion": e.get("suggestion") if full else ""}


EVENT_FIELDS = ["seq", "kind", "tier", "anchors", "item", "ids", "text", "excerpt", "suggestion"]
CHAT_FIELDS = ["seq", "role", "kind", "tier", "reply_to", "anchors", "text"]


def item_row(i: dict, full: bool, sid: str) -> dict:
    row = {k: i.get(k) for k in BACKLOG_FIELDS}
    if not full:
        for k in ("detail", "patch"):
            row[k] = clip(row[k], CLIP_CHAT, f"{BIN} backlog {sid} list --fields id,{k} --full")
    return row


# ----------------------------------------------------------------- context
def strip_inventory(md: str) -> str:
    """Drop the "Every change" <details> block: the manifest map below replaces it."""
    return re.sub(r"### Every change\s*\n<details>.*?</details>\s*", "### Every change\n(see the file map below)\n\n", md, flags=re.S)


def file_map(manifest: dict) -> list[str]:
    files = manifest.get("files", [])
    out = [f"{len(files)} changed files vs {manifest.get('base_label', '?')} (#n = hunk number; story = cited by the summary):"]
    for f in files[:MAP_LIMIT]:
        hunks = " ".join(f"#{h['n']}:{h['new_start']}+{h['new_len']}" + (f"({h['symbol'][:30]})" if h.get("symbol") else "")
                         for h in f.get("hunks", [])[:8])
        more = f" +{len(f['hunks']) - 8} hunks" if len(f.get("hunks", [])) > 8 else ""
        old = f" (from {f['old_path']})" if f.get("old_path") else ""
        cited = " story" if f.get("cited") else ""
        out.append(f"- {f['badge']} {f['path']}{old} +{f['adds']} -{f['dels']}{cited}  {hunks}{more}")
    if len(files) > MAP_LIMIT:
        out.append(f"- ... {len(files) - MAP_LIMIT} more in manifest.json")
    return out


def build_context(s: store.Session, summary: Path | None, notes: Path | None) -> None:
    m = s.meta()
    parts = [f"# Review context: {m.get('title', '')}", "", f"repo: {m.get('repo')}   page: {m.get('page') or '-'}", ""]
    if summary and summary.is_file():
        parts += ["## Implementation summary (what the user is reviewing)", "", strip_inventory(summary.read_text(encoding="utf-8")).strip(), ""]
    if notes and notes.is_file():
        parts += ["## Session notes from the implementing agent", "", notes.read_text(encoding="utf-8").strip(), ""]
    parts += ["## File map", "", *file_map(s.manifest()), ""]
    s.context_path.write_text("\n".join(parts), encoding="utf-8")


# -------------------------------------------------------------- home/inbox
def repo_of(cwd: Path) -> Path:
    try:
        out = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return Path(out.stdout.strip()).resolve()
    except Exception:
        pass
    return cwd.resolve()


def repo_sessions(cwd: Path | None) -> list[store.Session]:
    root = repo_of(cwd) if cwd else None
    out = [s for s in store.sessions() if root is None or Path(s.meta().get("repo", "/")).resolve() == root]
    return sorted(out, key=lambda s: s.meta().get("created", 0), reverse=True)


def inbox(cwd: Path | None) -> list[dict]:
    """Sessions with backlog the main agent has not accepted, whose reviewer is gone."""
    out = []
    for s in repo_sessions(cwd):
        execute = s.execute_requested()
        # open = never handed to an agent; handed-off and not executed waits on the user, not on the agent
        items = [i for i in s.backlog() if i["status"] == "open" or (i["status"] == "handed-off" and i["id"] in execute)]
        p = s.presence()
        if p["live"] or not (items or execute):
            continue
        out.append({"sid": s.sid, "title": s.meta().get("title"), "items": items, "execute": execute,
                    "unanswered": len(s.unanswered()), "reviewer": p})
    return out


UNFINISHED_HINT = (f"Close each unfinished item: `{BIN} backlog <sid> done <id> --note \"<what changed, path:line>\"`, "
                   f"`reopen <id> --note \"<what remains>\"`, or `dismiss <id> --note \"<why>\"`")


def unfinished_rows(sessions: list) -> list[dict]:
    """Items an agent acked (accepted) and never closed: the work that silently stalls after a context reset."""
    return [{"sid": s.sid, "id": i["id"], "acked_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(i["acked_at"])) if i.get("acked_at") else "?",
             "title": i["title"], "key": f"{s.sid}:{i['id']}:acked:{i.get('acked_at')}"} for s in sessions for i in s.unfinished()]


def undelivered_rows(rows: list[dict]) -> list[dict]:
    return [{"sid": r["sid"], "id": i["id"], "status": i["status"], "kind": i.get("kind"),
             "execute": i["id"] in r["execute"], "title": i["title"]} for r in rows for i in r["items"]]


def home(cwd: Path, limit: int = 5) -> list[dict]:
    kv("bin", bin_path())
    kv("description", DESCRIPTION)
    h = health()
    kv("server", f"up {HOST}:{PORT}" if h and h.get("app") == "review-desk" else "down (open or url starts it)")
    root = repo_of(cwd)
    kv("repo", tilde(root))
    ss = repo_sessions(cwd)
    rows = []
    for s in ss[:limit]:
        m, p = s.meta(), s.presence()
        items = s.backlog()
        rows.append({"sid": s.sid, "title": m.get("title"), "status": m.get("status"),
                     "reviewer": f"{p['state']} {p.get('model') or ''}/{p.get('effort') or ''}".replace(" /", ""),
                     "unanswered": len(s.unanswered()),
                     "open": sum(i["status"] in store.OPEN_STATUSES for i in items)})
    kv("count", f"{len(ss)} session(s) in this repo" + (f", newest {limit} shown" if len(ss) > limit else ""))
    table("sessions", rows, ["sid", "title", "status", "reviewer", "unanswered", "open"], "none in this repo")
    pending = inbox(cwd)
    und = undelivered_rows(pending)
    if und or any(r["execute"] for r in pending):
        table("undelivered", und, ["sid", "id", "status", "kind", "execute", "title"])
    unf = unfinished_rows(ss)
    if unf:
        table("unfinished", unf, ["sid", "id", "acked_at", "title"])
    hints = []
    for r in pending:
        hints.append(run(f"handoff {r['sid']}") + " then handle it (review-desk skill: When the reviewer hands back)")
    if unf:
        hints.append(UNFINISHED_HINT)
    if rows:
        hints.append(run("status <sid>"))
    hints.append(run('open --title "<what to review>" --repo .') + " to review the working tree")
    help_block(hints)
    return pending


# ----------------------------------------------------------------- commands
def cmd_home(a) -> int:
    home(Path(os.getcwd()))
    return 0


def cmd_open(a) -> int:
    repo = Path(a.repo).resolve()
    directory = Path(a.dir) if a.dir else None
    page = str(Path(a.page).resolve()) if a.page else None
    s = store.create(directory, title=a.title, repo=str(repo), page=page, editor=a.editor)
    if not s.manifest_path.exists():
        import gitdiff
        try:
            gitdiff.write(repo, s.dir, a.base or "HEAD", a.paths)
        except subprocess.CalledProcessError as e:
            raise DeskError(f"git diff failed in {repo}: {e.stderr.strip() if e.stderr else e}", ["Pass --repo <git checkout>"])
    build_context(s, Path(a.summary) if a.summary else None, Path(a.notes) if a.notes else None)
    h = ensure_server()
    url = url_for(s)
    clients = h.get("clients", {}).get(s.sid, 0)
    if clients:
        browser = f"already open ({clients} tab)"
    elif a.no_browser or os.environ.get("REVIEW_DESK_NO_OPEN"):
        browser = "not opened (--no-browser)"
    else:
        webbrowser.open(url)
        browser = "opened"
    p = store.prefs()
    kv("sid", s.sid)
    kv("url", url)
    kv("browser", browser)
    kv("dir", tilde(s.dir))
    kv("files", len(s.manifest().get("files", [])))
    kv("context", f"{tilde(s.context_path)} ({len(s.context_path.read_text().splitlines())} lines)")
    kv("last_tier", f"{p.get('model', 'haiku')}/{p.get('effort', 'low')}")
    help_block([run(f'add {s.sid} <path:a-b> --note "<why read this first>"') + " to pin the 1-3 code moments",
                f"Ask the user for model and effort (last: {p.get('model', 'haiku')}/{p.get('effort', 'low')}), then "
                + run(f"prefs --model <model> --effort <effort>"),
                spawn_hint(s.sid),
                f"End your reply with the url alone on its last line (`↗ <url>`); the reviewer's exit notifies you"])
    return 0


def cmd_url(a) -> int:
    s = session(a.sid)
    ensure_server()
    kv("url", url_for(s))
    return 0


def cmd_add(a) -> int:
    s = session(a.sid)
    paths = {f["path"] for f in s.manifest().get("files", [])}
    repo = Path(s.meta().get("repo", "."))
    rows = []
    for ref in a.refs:
        path = re.sub(r":\d+(?:-\d+)?$", "", ref)
        if path not in paths and not (repo / path).is_file():
            rows.append({"ref": ref, "status": "skipped: not a changed file or a file in the repo"})
            continue
        _, added = s.tray_add(ref, focus=a.focus, note=a.note)
        rows.append({"ref": ref, "status": "pinned" if added else "already pinned", "note": a.note})
    table("pinned", rows, ["ref", "status", "note"])
    help_block([run(f"url {s.sid}") + " to show the desk again"])
    return 1 if all(r["status"].startswith("skipped") for r in rows) else 0


PAGE_FIELDS = ["id", "status", "title", "path", "by"]


def page_rows(s: store.Session) -> list[dict]:
    return [{"id": pg["id"], "status": ("open" if pg["open"] else "closed") + ("" if pg["mtime"] else ", file missing"),
             "title": pg["title"] or Path(pg["path"]).name, "path": tilde(pg["path"]), "by": pg.get("by") or ""}
            for pg in s.pages()]


def cmd_page(a) -> int:
    """HTML pages as read-only editor tabs: a reviewer's Lavish page, a skill's --html output, an implementation summary."""
    s = session(a.sid)
    by = "reviewer" if os.environ.get("REVIEW_DESK_ROLE") == "reviewer" else "main"
    if a.action == "list":
        rows = page_rows(s)
        kv("count", len(rows))
        table("pages", rows, PAGE_FIELDS)
        help_block([run(f"page {s.sid} open <file.html> --title \"<what it shows>\"") + " to show a page",
                    run(f"page {s.sid} close <id>") + " to close one"])
        return 0
    if not a.target:
        raise DeskError(f"page {a.action} needs a target", [run(f"page {s.sid} list")], 2)
    if a.action == "close":
        pg = s.page(a.target)
        if not pg:
            raise DeskError(f"no page {a.target} in session {s.sid}", [run(f"page {s.sid} list")])
        changed = s.page_close(pg["id"], by=by)
        kv("page", pg["id"])
        kv("status", "closed" if changed else "already closed")
        help_block([run(f"page {s.sid} open {pg['id']}") + " to show it again"])
        return 0
    # open: by id (re-show) or by file
    pg = s.page(a.target) if re.fullmatch(r"[Pp]\d+", a.target) else None
    path = Path(pg["path"]) if pg else Path(a.target).expanduser()
    if not pg and not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if path.suffix.lower() not in (".html", ".htm"):
        raise DeskError(f"{tilde(path)} is not an .html file", ["Write the page as one self-contained .html file, then open it"])
    if not path.is_file():
        raise DeskError(f"{tilde(path)} does not exist", [run(f"page {s.sid} list") + " for pages already registered"])
    pg, changed = s.page_open(str(path), title=a.title, focus=not a.background, by=by)
    kv("page", pg["id"])
    kv("title", pg["title"] or path.name)
    kv("status", ("shown" if not a.background else "opened in the background") if changed else "already open")
    kv("path", tilde(path))
    help_block([run(f"page {s.sid} close {pg['id']}") + " when it is no longer needed",
                "Edits to the file reload the open tab by themselves"])
    return 0


def sandbox_argv(writable: list[Path], argv: list[str]) -> list[str] | None:
    """Wrap argv so it may write only inside `writable` (macOS sandbox-exec, Linux bwrap); None if neither exists."""
    if shutil.which("sandbox-exec"):
        allow = " ".join(f'(subpath "{p}")' for p in writable)
        profile = f'(version 1)(allow default)(deny file-write*)(allow file-write* {allow} (subpath "/dev"))'
        return ["sandbox-exec", "-p", profile, *argv]
    if shutil.which("bwrap"):
        binds = [x for p in writable for x in ("--bind", str(p), str(p))]
        return ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", *binds, *argv]
    return None


def cmd_run(a) -> int:
    """Run a command (a skill's page generator) with writes confined to the session's pages/ folder and a scratch dir."""
    s = session(a.sid)
    argv = a.argv[1:] if a.argv[:1] == ["--"] else a.argv
    if not argv:
        raise DeskError("run needs a command after --", [run(f"run {s.sid} -- python3 <pages>/make_page.py")], 2)
    pages, tmp = s.pages_dir, s.scratch_dir
    pages.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    # a reviewer started before pages moved to <home>/pages/<sid> still writes into the session's own pages/
    legacy = (s.dir / "pages").resolve()
    wrapped = sandbox_argv([pages, tmp] + ([legacy] if legacy.is_dir() and legacy != pages else []), argv)
    if wrapped is None:
        raise DeskError("no write sandbox on this machine (sandbox-exec or bwrap)",
                        [f"Write the page directly into {tilde(pages)} with the Write tool"])
    roots = [pages] + ([legacy] if legacy.is_dir() and legacy != pages else [])
    before = {q: q.stat().st_mtime_ns for r in roots for q in r.rglob("*.htm*")}
    env = {**os.environ, "TMPDIR": str(tmp), "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.run(wrapped, cwd=pages, env=env, capture_output=True, text=True, timeout=a.timeout)
    out = (p.stdout + p.stderr).strip()
    kv("exit", p.returncode)
    kv("output", clip(out, 4000) if out else "(none)")
    pages_now = [q for r in roots for q in r.rglob("*.htm*") if before.get(q) != q.stat().st_mtime_ns]
    table("written", [{"path": tilde(q)} for q in sorted(pages_now)], ["path"])
    help_block([run(f"page {s.sid} open {tilde(q)} --title \"<what it shows>\"") for q in sorted(pages_now)[:3]]
               or [f"Only writes inside {tilde(pages)} succeed; write the page there"])
    return 0 if p.returncode == 0 else 1


def cmd_attach(a) -> int:
    s = session(a.sid)
    if not a.main and a.model not in store.MODELS:
        raise DeskError(f"attach --model must be one of {', '.join(store.MODELS)}", ["Pass --main when the main agent answers itself"], 2)
    entries = s.chat()
    run_id = store.new_sid()
    s.set_agent(run=run_id, model=a.model, effort=a.effort, state="thinking", reason=None, main=a.main,
                cursor=entries[-1]["seq"] if entries else 0, timeout=a.timeout)
    if a.model in store.MODELS and not a.main:
        store.save_prefs(model=a.model, effort=a.effort)
    m = s.meta()
    kv("session", s.sid)
    kv("title", m.get("title"))
    kv("repo", m.get("repo"))
    kv("you", f"{'main agent ' if a.main else ''}{a.model}/{a.effort} (run {run_id})")
    axi.block("context", s.context_path.read_text(encoding="utf-8") if s.context_path.exists() else "(no context.md)")
    pending = s.unanswered(entries)
    waiting = {e["seq"] for e in pending}
    tail = [e for e in entries if e["kind"] != "tier" and e["seq"] not in waiting][-CHAT_TAIL:]
    table("chat", [event_row(e, s.sid, full=False) for e in tail], CHAT_FIELDS)
    items = [i for i in s.backlog() if i["status"] not in ("done", "dismissed")]
    table("backlog", [item_row(i, False, s.sid) for i in items], ["id", "status", "title", "anchor"], "empty")
    table("pending", [event_row(e, s.sid) for e in pending], EVENT_FIELDS, "none")
    if pending:
        help_block(skill_hints(s, pending) + [run(f"reply {s.sid} --to <seq,...> --file - --then-wait") + " with the Markdown answer on stdin",
                    run(f'backlog {s.sid} add --title "<title>" --anchor <path:a-b> --from <seq> --detail-file -') + " first, for a confirmed issue, with the Context / Issue / Suggested fix / Reasoning / Tests bullets on stdin"])
    else:
        help_block([run(f"wait {s.sid}") + " (Bash timeout 600000)"])
    return 0


def take_events(s: store.Session, cursor: int) -> tuple[list[dict], int]:
    new = [e for e in s.chat() if e["seq"] > cursor and e.get("role") == "user"]
    out: list[dict] = []
    for e in new:
        out.append(e)
        cursor = e["seq"]
        if e["kind"] in store.CONTROL_KINDS:
            break  # a control event ends the batch; what follows belongs to the next reviewer
    return out, cursor


def skill_hints(s: store.Session, events: list[dict]) -> list[str]:
    """An exact Skill call for every slash-command message, so no reviewer answers it by hand."""
    out = []
    for e in events:
        if e.get("skill"):
            target = " ".join(x for x in [e.get("args") or "", anchors_of(e)] if x).strip()
            out.append(f'Invoke the Skill tool: skill: "{e["skill"]}", args: "{target}"; then '
                       + run(f"reply {s.sid} --to {e['seq']} --file -") + " with its condensed result")
    return out


def exit_help(s: store.Session, reason: str, keyword: str) -> list[str]:
    return [run(f"handoff {s.sid}"), run(f"detach {s.sid} --reason {reason}"),
            f"Finish with `{keyword} sid={s.sid}` followed by the handoff output"]


def cmd_wait(a) -> int:
    s = session(a.sid)
    cursor = s.agent().get("cursor", 0)
    s.set_agent(state="waiting", timeout=a.timeout)
    deadline = time.time() + a.timeout
    beat = time.time()
    while True:
        if s.meta().get("status") == "ended" and not any(e["kind"] == "end" and e["seq"] > cursor for e in s.chat()):
            s.set_agent(state="thinking", cursor=cursor)
            table("events", [], EVENT_FIELDS, "none (the session was ended)")
            help_block(exit_help(s, "end", "END"))
            return 0
        events, new_cursor = take_events(s, cursor)
        if events:
            s.set_agent(state="thinking", cursor=new_cursor)
            table("events", [event_row(e, s.sid) for e in events], EVENT_FIELDS)
            last = events[-1]
            if last["kind"] == "tier":
                hints = [run(f'reply {s.sid} --text "Handing over to {last["model"]} · {last["effort"]}."')] + \
                        exit_help(s, "handoff", f"HANDOFF tier={last['model']}/{last['effort']}")
            elif last["kind"] == "execute":
                hints = exit_help(s, "execute", f"EXECUTE ids={','.join(last.get('ids', []))}")
            elif last["kind"] == "end":
                hints = exit_help(s, "end", "END")
            else:
                hints = skill_hints(s, events) + [run(f"reply {s.sid} --to <seq,...> --file - --then-wait") + " with the Markdown answer on stdin",
                         run(f'backlog {s.sid} add --title "<title>" --anchor <path:a-b> --from <seq> --detail-file -') + " first, for a confirmed issue, with the Context / Issue / Suggested fix / Reasoning / Tests bullets on stdin"]
            help_block(hints)
            return 0
        if time.time() >= deadline:
            s.set_agent(state="waiting", timeout=a.timeout)
            table("events", [], EVENT_FIELDS, f"none (timed out after {a.timeout}s)")
            help_block([run(f"wait {s.sid}") + " again; after 6 timeouts in a row: " + ", ".join(exit_help(s, "idle", "IDLE"))])
            return 0
        if time.time() - beat > 15:
            s.set_agent(state="waiting", timeout=a.timeout)
            beat = time.time()
        time.sleep(0.25)


def read_text_arg(a) -> str:
    if a.text is not None:
        return a.text
    if a.file == "-":
        return sys.stdin.read()
    if a.file:
        return Path(a.file).read_text(encoding="utf-8")
    raise DeskError("reply needs --text or --file", [run(f"reply {a.sid} --to <seq> --file -") + " with Markdown on stdin"], 2)


def cmd_reply(a) -> int:
    s = session(a.sid)
    ag = s.agent()
    to = [int(x.lstrip("#")) for x in re.split(r"[,\s]+", a.to or "") if x.strip("#").isdigit()]
    text = read_text_arg(a).strip()
    if not text:
        raise DeskError("empty reply", [run(f"reply {a.sid} --to <seq> --file -") + " with Markdown on stdin"], 2)
    dup = next((e for e in s.chat() if e.get("role") == "agent" and e.get("text") == text and e.get("reply_to", []) == to), None)
    e = dup or s.post("msg", "agent", text=text, reply_to=to, model=ag.get("model"), effort=ag.get("effort"))
    kv("posted", f"#{e['seq']}")
    kv("answers", " ".join(f"#{i}" for i in to) or "none")
    if dup:
        kv("duplicate", "identical reply already posted; nothing written")
    if a.then_wait:
        sys.stdout.flush()
        return cmd_wait(a)
    help_block([run(f"wait {s.sid}") + " (Bash timeout 600000)"])
    return 0


def cmd_chat(a) -> int:
    s = session(a.sid)
    entries = s.chat()
    seqs = {int(x.lstrip("#")) for x in a.seqs if x.lstrip("#").isdigit()}
    rows = [e for e in entries if e["seq"] in seqs] if seqs else entries[-a.last:]
    full = a.full or bool(seqs)
    kv("count", f"{len(rows)} of {len(entries)} entries")
    table("chat", [event_row(e, s.sid, full=full) for e in rows], CHAT_FIELDS + (["excerpt", "suggestion"] if full else []))
    if not full:
        help_block([run(f"chat {s.sid} <seq> ...") + " to read entries in full"])
    return 0


def read_detail(a) -> str | None:
    """--detail inline, or --detail-file (- for stdin): a structured detail is multi-line Markdown full of
    backticks, which a double-quoted shell argument would run as command substitutions."""
    if a.detail is not None and a.detail_file:
        raise DeskError("pass --detail or --detail-file, not both", [run(f"backlog {a.sid} {a.action} ... --detail-file -") + " with the Markdown on stdin"], 2)
    if a.detail_file == "-":
        return sys.stdin.read().strip("\n")
    if a.detail_file:
        return Path(a.detail_file).read_text(encoding="utf-8").strip("\n")
    return a.detail


def cmd_backlog(a) -> int:
    s = session(a.sid)
    if a.action in ("add", "update"):
        a.detail = read_detail(a)
    if a.action == "add":
        if not a.title:
            raise DeskError("backlog add needs --title", [run(f'backlog {a.sid} add --title "<title>" --anchor <path:a-b> --from <seq> --detail-file -') + " with the detail on stdin"], 2)
        patch = Path(a.patch).read_text(encoding="utf-8") if a.patch else None
        i = s.backlog_add(a.title, a.detail or "", a.kind, a.anchor, a.from_seq, patch, by=a.by, dedupe=True)
        kv("existing" if i.get("existing") else "created", i["id"])
        kv("title", i["title"])
        help_block([f'Reply with "Logged {i["id"]}: <one line>": ' + run(f"reply {a.sid} --to <seq> --file - --then-wait")])
    elif a.action == "list":
        items = s.backlog()
        shown = items if a.all else [i for i in items if i["status"] not in ("done", "dismissed")]
        fields = BACKLOG_FIELDS if a.full and not a.fields else (a.fields.split(",") if a.fields else ["id", "status", "title", "anchor"])
        bad = [f for f in fields if f not in BACKLOG_FIELDS]
        if bad:
            raise DeskError(f"unknown field(s) {', '.join(bad)}", [f"Use --fields from: {','.join(BACKLOG_FIELDS)}"], 2)
        kv("count", f"{len(shown)} shown of {len(items)}" + ("" if a.all else " (done/dismissed hidden; --all)"))
        table("backlog", [item_row(i, a.full, a.sid) for i in shown], fields, "empty")
        if not a.full:
            help_block([run(f"backlog {a.sid} list --fields {','.join(BACKLOG_FIELDS)} --full") + " for every field in full"])
    elif a.action == "update":
        if not a.ids:
            raise DeskError("backlog update needs an id", [run(f'backlog {a.sid} update <id> --detail-file -') + " with the detail on stdin"], 2)
        i = s.backlog_update(a.ids[0].upper(), title=a.title, detail=a.detail, anchor=a.anchor)
        kv("updated", i["id"])
    else:
        status = {"ack": "acked", "done": "done", "dismiss": "dismissed", "reopen": "open"}[a.action]
        if not a.ids:
            raise DeskError(f"backlog {a.action} needs ids", [run(f"backlog {a.sid} {a.action} <id> ...")], 2)
        items = s.backlog_status([x.upper() for x in a.ids], status, by=a.by, note=a.note)
        table("updated", [{"id": i["id"], "status": i["status"]} for i in items], ["id", "status"])
        if status == "acked":
            help_block([run(f'backlog {a.sid} done <id> --note "<what changed, path:line>"') + " after implementing",
                        run(f'backlog {a.sid} dismiss <id> --note "<why>"') + " to drop one"])
    return 0


def cmd_handoff(a) -> int:
    s = session(a.sid)
    items = s.handoff()
    execute = s.execute_requested()
    kv("sid", s.sid)
    kv("count", len(items))
    kv("execute_requested", " ".join(execute) or "none")
    rows = [item_row(i, True, s.sid) | {"patch": clip(i.get("patch"), 1200, f"{BIN} backlog {s.sid} list --fields id,patch --full")} for i in items]
    table("items", rows, ["id", "status", "kind", "anchor", "from", "by", "title", "patch"], "none awaiting the main agent")
    # Details are structured Markdown (Context, Issue, Suggested fix, Reasoning, Tests); a table cell would flatten
    # them into one escaped line, so each prints whole as an indented block the main agent can read and follow.
    for i in items:
        if i.get("detail") and i["detail"] != i["title"]:
            block(f"detail_{i['id']}", i["detail"])
    if items:
        help_block(handoff_hints(s.sid, execute, [i for i in items if i["id"] not in execute]))
    return 0


def handoff_hints(sid: str, execute: list[str], rest: list[dict]) -> list[str]:
    """Ack means "accepted, implementing now", so only executed items are acked. An item acked without an
    Execute used to drop out of every list and could never be executed: it stayed acked forever."""
    hints = []
    if execute:
        hints += [run(f"backlog {sid} ack {' '.join(execute)}") + " to accept the items the user executed, then implement them",
                  run(f'backlog {sid} done <id> --note "<what changed, path:line>"') + " after each",
                  run(f'backlog {sid} reopen <id> --note "<what remains>"') + " for one that is only partly done"]
    questions = [i["id"] for i in rest if i.get("kind") == "question"]
    if rest:
        hints.append(f"Leave {' '.join(i['id'] for i in rest)} open and do not ack them: report them; the user executes them from the desk when ready")
    if questions:
        hints.append(f"Put {' '.join(questions)} to the user now: a question item needs their answer, not an Execute")
    return hints


def cmd_detach(a) -> int:
    s = session(a.sid)
    s.set_agent(state="exited", reason=a.reason)
    kv("reviewer", f"exited ({a.reason})")
    return 0


def cmd_status(a) -> int:
    s = session(a.sid)
    p = s.presence()
    m = s.meta()
    items = s.backlog()
    counts = {st: sum(i["status"] == st for i in items) for st in store.ALL_STATUSES}
    w = p.get("wanted") or {}
    kv("session", s.sid)
    kv("status", m.get("status"))
    kv("title", m.get("title"))
    kv("reviewer", f"{p['state']} {p.get('model') or '-'}/{p.get('effort') or '-'}" + (f" (exited: {p['reason']})" if p.get("reason") else ""))
    kv("wanted_tier", f"{w.get('model', '-')}/{w.get('effort', '-')}")
    kv("unanswered", len(s.unanswered()))
    kv("backlog", " ".join(f"{k}={v}" for k, v in counts.items() if v) or "empty")
    kv("execute_requested", " ".join(s.execute_requested()) or "none")
    hints = []
    if p["state"] == "respawning" and w:
        hints.append(spawn_hint(s.sid, w["model"], w["effort"]))
    elif not p["live"] and m.get("status") != "ended" and len(s.unanswered()):
        hints.append(spawn_hint(s.sid, w.get("model", "<model>"), w.get("effort", "<effort>")) + " (messages are queued)")
    if counts["open"] or counts["handed-off"]:
        hints.append(run(f"handoff {s.sid}"))
    if counts["acked"]:
        hints.append(UNFINISHED_HINT.replace("<sid>", s.sid) + f" ({' '.join(i['id'] for i in s.unfinished())} accepted, not done)")
    help_block(hints)
    return 0


def cmd_inbox(a) -> int:
    rows = inbox(Path(a.cwd) if a.cwd else None)
    if a.json:
        print(json.dumps(rows))
        return 0
    und = undelivered_rows(rows)
    kv("count", len(und))
    table("undelivered", und, ["sid", "id", "status", "kind", "execute", "title"])
    help_block([run(f"handoff {r['sid']}") for r in rows])
    return 0


def cmd_reload(a) -> int:
    s = session(a.sid)
    fields = {"reloaded": store.now()}
    if a.page:
        fields["page"] = str(Path(a.page).resolve())
    m = s.manifest()
    if m.get("source") == "git":  # opened without implementation-summary: recompute the diffs ourselves
        import gitdiff
        n = len(gitdiff.write(Path(m["repo"]), s.dir, m.get("base") or "HEAD", m.get("paths") or None)["files"])
        kv("diffs", f"recomputed from git ({n} files)")
    s.update_meta(**fields)
    kv("reloaded", s.sid)
    return 0


def cmd_end(a) -> int:
    s = session(a.sid)
    s.update_meta(status="ended")
    kv("ended", s.sid)
    return 0


def cmd_prefs(a) -> int:
    if a.model or a.effort:
        store.save_prefs(**{k: v for k, v in (("model", a.model), ("effort", a.effort)) if v})
    p = store.prefs()
    kv("model", p.get("model", "haiku"))
    kv("effort", p.get("effort", "low"))
    return 0


def cmd_prompt(a) -> int:
    """The reviewer instructions without frontmatter, for spawning a general-purpose agent when
    the review-desk-<effort> definitions are not loaded yet (they load at session start)."""
    text = (SKILL / "agents" / "reviewer.md.tmpl").read_text(encoding="utf-8").replace("{{EFFORT}}", a.effort)
    body = text.split("\n---\n", 1)[1]
    body = "\n".join(l for l in body.splitlines() if not l.startswith("<!-- GENERATED"))
    print(f"You are a Review Desk reviewer. Answer briefly and stop reasoning early ({a.effort} effort).\n" + body.strip())
    print(f"\nSID={a.sid}. Your model: {a.model}.")
    return 0


# --------------------------------------------------------- reviewer host
def host_alive(s: store.Session) -> bool:
    a = s.agent()
    pid = a.get("host_pid")
    if a.get("mode") != "host" or not pid or a.get("state") == "exited":
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def watch_hint(sid: str) -> str:
    return (f"Run `{BIN} watch {sid}` with Bash run_in_background (timeout 7200000); "
            "it returns, and wakes you, on Execute, End or the reviewer exiting")


def cmd_reviewer(a) -> int:
    s = session(a.sid)
    ag = s.agent()
    if a.action == "status":
        kv("host", "running" if host_alive(s) else "not running")
        kv("tier", f"{ag.get('model') or '-'}/{ag.get('effort') or '-'}")
        kv("state", ag.get("state") or "-")
        kv("log", tilde(s.dir / "reviewer.log"))
        return 0
    if a.action == "stop":
        if not host_alive(s):
            kv("host", "not running")
            return 0
        os.kill(ag["host_pid"], signal.SIGTERM)
        for _ in range(100):
            time.sleep(0.2)
            if not host_alive(s):
                break
        kv("host", "stopped")
        return 0
    if not a.model or not a.effort:
        raise DeskError("reviewer start needs --model and --effort", [run(f"reviewer {a.sid} start --model haiku --effort low")], 2)
    if host_alive(s):
        raise DeskError(f"a reviewer is already running for {s.sid} ({ag.get('model')}/{ag.get('effort')})",
                        ["Switch model or effort from the chat's chips; the running reviewer changes in place",
                         run(f"reviewer {s.sid} stop") + " to stop it"])
    store.save_prefs(model=a.model, effort=a.effort)
    log = open(s.dir / "reviewer.log", "a")
    subprocess.Popen([sys.executable, str(SKILL / "scripts" / "reviewer_host.py"), s.sid, "--model", a.model, "--effort", a.effort],
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
                     env={**os.environ, "REVIEW_DESK_HOME": str(store.home())})
    for _ in range(100):
        time.sleep(0.1)
        if host_alive(s):
            break
    if not host_alive(s):
        raise DeskError("the reviewer did not start", [f"Read `{tilde(s.dir / 'reviewer.log')}`"])
    kv("reviewer", f"running {a.model}/{a.effort} (pid {s.agent().get('host_pid')})")
    ag = s.agent()
    # the host decides; a fresh conversation has a new session id within moments, so that cannot tell
    kv("resumes", "previous conversation" if ag.get("resumed")
       else "fresh conversation" + (f" ({ag['fresh_reason']})" if ag.get("fresh_reason") else ""))
    help_block([watch_hint(s.sid), "End your turn with the desk url; tier changes from the chat apply in place, no respawn"])
    return 0


def cmd_watch(a) -> int:
    """Block until the main agent has something to do: Execute, End, or the reviewer exiting."""
    s = session(a.sid)
    deadline = time.time() + a.timeout
    event = None
    seq = None
    beat = 0.0
    while time.time() < deadline and event is None:
        if time.time() - beat > 10:  # the desk shows "waking the main agent" only while this is fresh
            s.update_meta(watcher={"pid": os.getpid(), "at": store.now()})
            beat = time.time()
        # the same pending list the hooks deliver from: an Execute pressed before this watch started is still
        # waiting, and one a hook already delivered is not
        pending = s.pending_events()
        if pending:
            event = "END" if any(e["kind"] == "end" for e in pending) else "EXECUTE"
            seq = max(e["seq"] for e in pending)
        if event is None and s.meta().get("status") == "ended":
            event = "END"
        ag = s.agent()
        if event is None and ag.get("mode") == "host" and not host_alive(s):
            event = f"REVIEWER_EXITED ({ag.get('reason') or 'unknown'})"
        if event is None:
            time.sleep(0.5)
    s.update_meta(watcher=None)
    if event is None:
        kv("event", "TIMEOUT")
        help_block([watch_hint(s.sid) + " again"])
        return 0
    kv("event", event)
    if seq is not None:
        s.mark_delivered(seq, "watch")
    return_code = cmd_handoff(a)
    if event == "EXECUTE":
        help_block(["Implement exactly the execute_requested items; done <id> --note after each",
                    run(f"reload {s.sid}") + " (or rebuild with implementation-summary), then " + watch_hint(s.sid)])
    elif event.startswith("REVIEWER_EXITED"):
        help_block([run(f"reviewer {s.sid} start --model <model> --effort <effort>") + " if the user is still reviewing (it resumes the conversation unless the desk was upgraded)",
                    "Otherwise report the open items"])
    else:
        help_block(["Report the open items in one or two lines; do not restart the reviewer"])
    return return_code


# ------------------------------------------------------------------ hooks
def hook_input() -> dict:
    try:
        if sys.stdin.isatty():
            return {}
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


# Commands only a reviewer runs: they never make the calling session a desk's owner.
REVIEWER_CMDS = {"attach", "wait", "reply", "chat", "detach"}
DESK_CMD_RE = re.compile(r"(?:review-desk-axi|review_desk\.py)\s+([a-z]+)\s+([0-9a-f]{8})\b")


def claim_from_tool(data: dict) -> None:
    """The session that drives a desk (open, add, page, watch, reload, backlog...) owns it."""
    agent = data.get("session_id")
    if not agent or data.get("tool_name") != "Bash":
        return
    cmd = str((data.get("tool_input") or {}).get("command") or "")
    if "review-desk-axi" not in cmd and "review_desk.py" not in cmd:
        return
    sids = {sid for verb, sid in DESK_CMD_RE.findall(cmd) if verb not in REVIEWER_CMDS}
    resp = data.get("tool_response")
    out = resp.get("stdout", "") if isinstance(resp, dict) else str(resp or "")
    if re.search(r"review-desk-axi\s+open\b", cmd):
        sids |= set(re.findall(r"^sid: ([0-9a-f]{8})$", out, re.M))
    for sid in sids:
        try:
            store.open_session(sid).claim(agent)
        except KeyError:
            pass


def delivery_text(s: store.Session, events: list[dict], via: str, to: str | None) -> str:
    """Hand the main agent what the user pressed, in the words `watch` would use, and mark it delivered."""
    title = s.meta().get("title") or s.sid
    if any(e["kind"] == "end" for e in events):
        s.mark_delivered(max(e["seq"] for e in events), via, to)
        return (f"Review Desk {s.sid} ({title}): the user ended the review. Report its open items in one or two lines; "
                f"do not restart the reviewer.")
    ids = s.execute_requested()
    items = {i["id"]: i for i in s.handoff()}
    lines = [f"Review Desk {s.sid} ({title}): the user pressed Execute for {' '.join(ids)}. Run `{BIN} backlog {s.sid} ack {' '.join(ids)}`, "
             f"implement exactly these items now (finish or park your current step first), then run "
             f"`{BIN} backlog {s.sid} done <id> --note \"<what changed, path:line>\"` for each (or `reopen <id> --note \"<what remains>\"` "
             f"if one is only partly done), rebuild the summary (implementation-summary, Backlog follow-up form) and `{BIN} reload {s.sid}`."]
    for i in (items[x] for x in ids if x in items):
        anchor = f" @ {i['anchor']}" if i.get("anchor") else ""
        lines.append(f"- {i['id']} [{i.get('kind') or 'fix'}] {i['title']}{anchor}")
        # The whole detail, indented under its item: the Suggested fix and Tests sections are the instructions.
        if i.get("detail") and i.get("detail") != i.get("title"):
            lines.extend("    " + l if l.strip() else "" for l in i["detail"].splitlines())
    lines.append(f"Every field, including patches: `{BIN} handoff {s.sid}`.")
    s.mark_delivered(max(e["seq"] for e in events), via, to)
    return "\n".join(lines)


def deliver_owned(agent: str | None, via: str) -> list[str]:
    """Deliver pending Execute/End events of every desk this agent session owns."""
    out = []
    for sid in store.owned(agent or ""):
        try:
            s = store.open_session(sid)
        except KeyError:
            store.owned_update(agent, remove=sid)
            continue
        if (s.meta().get("owner") or {}).get("session") != agent:
            store.owned_update(agent, remove=sid)  # another session drives it now
            continue
        s.owner_seen()
        events = s.pending_events()
        if events:
            out.append(delivery_text(s, events, via, agent))
    return out


def hook_seen(session: str | None, add: set[str] | None = None) -> set[str]:
    """Keys this agent session was already told about, so each hook says a thing once per session."""
    f = store.home() / "hook-seen" / f"{session or 'x'}.json"
    seen = set(json.loads(f.read_text())) if f.exists() else set()
    if add and add - seen:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(sorted(seen | add)))
    return seen


def unfinished_nudge(agent: str | None) -> list[str]:
    """At Stop, remind the owning agent once per acceptance about items it acked and never closed."""
    seen = hook_seen(agent)
    rows = []
    for sid in store.owned(agent or ""):
        try:
            rows += [r for r in unfinished_rows([store.open_session(sid)]) if r["key"] not in seen]
        except KeyError:
            continue
    if not rows:
        return []
    hook_seen(agent, {r["key"] for r in rows})
    listed = "; ".join(f"{r['sid']} {r['id']} ({r['title']})" for r in rows)
    return [f"Review Desk: you accepted {listed} but never closed {'it' if len(rows) == 1 else 'them'}. If an item is finished, run "
            f"`{BIN} backlog <sid> done <id> --note \"<what changed, path:line>\"`; if it is partly done, "
            f"`reopen <id> --note \"<what remains>\"`; if you will not do it, `dismiss <id> --note \"<why>\"`. "
            f"If you are still working on it or waiting on the user, say so in your reply."]


def cmd_hook(a) -> int:
    """Session hooks. Never fail: a hook bug must not block a session or a prompt.
    tool (PostToolUse) and stop (Stop) deliver Execute/End to the owning agent even while it is busy."""
    try:
        data = hook_input()
        if a.event in ("tool", "stop"):
            if a.event == "tool":
                claim_from_tool(data)
            texts = deliver_owned(data.get("session_id"), f"hook:{a.event}")
            if a.event == "stop" and not texts and not data.get("stop_hook_active"):
                texts = unfinished_nudge(data.get("session_id"))
            if texts and a.event == "tool":
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n\n".join(texts)}}))
            elif texts:
                print(json.dumps({"decision": "block", "reason": "\n\n".join(texts)}))
            return 0
        cwd = Path(data.get("cwd") or os.getcwd())
        if a.event == "prompt":
            for text in deliver_owned(data.get("session_id"), "hook:prompt"):
                print(text)
        seen = hook_seen(data.get("session_id"))
        if a.event == "session":
            rows = home(cwd)
        else:
            rows = inbox(cwd)
        keys = {f"{r['sid']}:{i['id']}:{i['status']}" for r in rows for i in r["items"]} | \
               {f"{r['sid']}:execute:{','.join(r['execute'])}" for r in rows if r["execute"]}
        if a.event == "prompt":
            fresh = [r | {"items": [i for i in r["items"] if f"{r['sid']}:{i['id']}:{i['status']}" not in seen]} for r in rows]
            fresh = [r for r in fresh if r["items"] or (r["execute"] and f"{r['sid']}:execute:{','.join(r['execute'])}" not in seen)]
            if fresh:
                kv("review_desk", "a reviewer is gone and these backlog items never reached you")
                table("undelivered", undelivered_rows(fresh), ["sid", "id", "status", "kind", "execute", "title"])
                help_block([run(f"handoff {r['sid']}") + " then handle it (review-desk skill: When the reviewer hands back)" for r in fresh])
            # accepted work nobody closed, once per acceptance per session: it survives a context reset this way
            unf = [r for r in unfinished_rows(repo_sessions(cwd)) if r["key"] not in seen]
            if unf:
                kv("review_desk_unfinished", "backlog items an agent accepted (acked) but never closed")
                table("unfinished", unf, ["sid", "id", "acked_at", "title"])
                help_block([UNFINISHED_HINT])
                keys |= {r["key"] for r in unf}
        if a.event == "session":
            keys |= {r["key"] for r in unfinished_rows(repo_sessions(cwd))}  # the home view just listed them
        hook_seen(data.get("session_id"), keys)
    except Exception:
        pass
    return 0


# ------------------------------------------------------------------ setup
def setup_hooks(settings: Path) -> list[dict]:
    d = json.loads(settings.read_text()) if settings.exists() else {}
    before = json.dumps(d, sort_keys=True)
    rows = []
    hooks = d.setdefault("hooks", {})
    legacy = ("review-desk/hooks/inbox.py",)
    # tool and stop deliver Execute/End to the agent that owns a desk while it works on something else
    for event, cmd, matcher in (("SessionStart", f"{BIN} hook session", True), ("UserPromptSubmit", f"{BIN} hook prompt", False),
                                ("PostToolUse", f"{BIN} hook tool", True), ("Stop", f"{BIN} hook stop", False)):
        groups = hooks.setdefault(event, [])
        for g in groups:
            kept = [h for h in g.get("hooks", []) if not any(x in h.get("command", "") for x in legacy)]
            if len(kept) != len(g.get("hooks", [])):
                rows.append({"what": f"{event}: legacy inbox.py hook", "status": "removed"})
                g["hooks"] = kept
        groups[:] = [g for g in groups if g.get("hooks")]
        if any(h.get("command") == cmd for g in groups for h in g.get("hooks", [])):
            rows.append({"what": f"{event}: {cmd}", "status": "present"})
        else:
            groups.append(({"matcher": ""} if matcher else {}) | {"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
            rows.append({"what": f"{event}: {cmd}", "status": "added"})
    allow = d.setdefault("permissions", {}).setdefault("allow", [])
    for old in [r for r in allow if "review-desk/bin/review-desk:" in r]:
        allow.remove(old)
        rows.append({"what": f"permission {old}", "status": "removed"})
    rule = f"Bash({BIN}:*)"
    rows.append({"what": f"permission {rule}", "status": "present" if rule in allow else "added"})
    if rule not in allow:
        allow.append(rule)
    if json.dumps(d, sort_keys=True) != before:
        if settings.exists():
            shutil.copyfile(settings, settings.with_name(settings.name + ".bak-review-desk"))
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
    return rows


def setup_bin(dest: Path) -> list[dict]:
    target = SKILL / "bin" / BIN
    link = dest / BIN
    if link.is_symlink() and link.resolve() == target.resolve():
        return [{"what": f"link {tilde(link)}", "status": "present"}]
    if link.exists() or link.is_symlink():
        raise DeskError(f"{link} exists and is not a link to {target}", [f"Remove {link} yourself, then " + run("setup bin")])
    link.symlink_to(target)
    return [{"what": f"link {tilde(link)} -> {tilde(target)}", "status": "added"}]


def cmd_setup(a) -> int:
    rows = []
    if a.target in ("bin", "all"):
        rows += setup_bin(Path(a.bin_dir).expanduser())
    if a.target in ("agents", "all"):
        out = subprocess.run([sys.executable, str(SKILL / "scripts" / "install_agents.py"), "--check"], capture_output=True, text=True)
        if out.returncode:
            subprocess.run([sys.executable, str(SKILL / "scripts" / "install_agents.py")], check=True, capture_output=True)
            rows.append({"what": "agents review-desk-{low,medium,high}", "status": "written (load at next session start)"})
        else:
            rows.append({"what": "agents review-desk-{low,medium,high}", "status": "present"})
    if a.target in ("hooks", "all"):
        settings = Path(a.settings).expanduser()
        kv("settings", tilde(settings))
        rows += setup_hooks(settings)
    table("changes", rows, ["what", "status"])
    if any(r["status"] == "added" for r in rows if "hook" in r["what"] or "Start" in r["what"] or "Submit" in r["what"]):
        help_block(["Hooks apply from the next prompt; restart the session for SessionStart"])
    return 0


# ------------------------------------------------------------------- main
class Parser(argparse.ArgumentParser):
    """Usage errors on stdout in the same `error:` + help[] shape, exit 2 (unknown flags fail loud)."""

    def error(self, message):
        print(f"error: {axi.value(message)}")
        help_block([run(f"{self.prog.split(' ', 1)[1] if ' ' in self.prog else ''} --help".strip())])
        sys.exit(2)


def build_parser() -> Parser:
    ap = Parser(prog=BIN, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", help="session store to use (default ~/.review-desk or $REVIEW_DESK_HOME); "
                                    "<home>/config.json may set {\"port\": N, \"no_open\": true}")
    sub = ap.add_subparsers(dest="cmd", parser_class=Parser)

    def cmd(name, fn, desc, sid=True):
        p = sub.add_parser(name, help=desc, description=desc)
        if sid:
            p.add_argument("sid", help="session id (from open, or the home view)")
        p.set_defaults(fn=fn)
        return p

    p = cmd("open", cmd_open, "Create or reuse a session and open the desk. Example: open --title 'Fix ingest' --repo .", sid=False)
    p.add_argument("--dir", help="session folder (implementation-summary: the <stem>.review folder build_page printed)")
    p.add_argument("--title", default="Review")
    p.add_argument("--page", help="HTML page for the Story tab")
    p.add_argument("--summary", help="terminal summary Markdown, folded into context.md")
    p.add_argument("--notes", help="session-only notes for the reviewer, folded into context.md")
    p.add_argument("--repo", default=".")
    p.add_argument("--base", help="git base when no manifest exists (default HEAD)")
    p.add_argument("--paths", nargs="*", help="scope when no manifest exists")
    p.add_argument("--editor", default=os.environ.get("IMPL_SUMMARY_EDITOR", "vscode"))
    p.add_argument("--no-browser", action="store_true")

    cmd("url", cmd_url, "Print the desk URL (starts the server if needed). Example: url d9534008")
    cmd("handoff", cmd_handoff, "List backlog the main agent has not accepted; marks open items handed-off. Safe to repeat.")
    cmd("status", cmd_status, "Reviewer presence, wanted tier, queue and backlog counts, with the next step.")
    cmd("end", cmd_end, "Close the session (the browser shows it ended).")

    p = cmd("add", cmd_add, "Pin files or ranges into the editor's tray. Example: add d9534008 src/x.py:40-72 --note 'the fix'")
    p.add_argument("refs", nargs="+", help="path or path:start-end")
    p.add_argument("--focus", action="store_true", help="also jump the browser to it")
    p.add_argument("--note")

    p = cmd("page", cmd_page, "Show an HTML page as a read-only editor tab, close it, or list pages. Example: page d9534008 open ~/.review-desk/pages/d9534008/flow.html --title 'Call graph'")
    p.add_argument("action", choices=["open", "close", "list"])
    p.add_argument("target", nargs="?", help="open: an .html file or a page id; close: a page id or path")
    p.add_argument("--title", help="tab title (default: the file name)")
    p.add_argument("--background", action="store_true", help="open the tab without switching to it")

    p = cmd("run", cmd_run, "Run a page generator with writes confined to the session's pages/ folder. Example: run d9534008 -- python3 make_page.py")
    p.add_argument("argv", nargs=argparse.REMAINDER, help="-- then the command")
    p.add_argument("--timeout", type=int, default=300)

    p = cmd("attach", cmd_attach, "Start a reviewer run: prints the briefing and pending messages. Example: attach d9534008 --model haiku --effort low")
    p.add_argument("--model", required=True, help=f"one of {', '.join(store.MODELS)}; with --main, any name")
    p.add_argument("--effort", required=True, choices=store.EFFORTS)
    p.add_argument("--main", action="store_true", help="the main agent answers itself; the chat's tier chips do not trigger a handover")
    p.add_argument("--timeout", type=int, default=540)

    p = cmd("wait", cmd_wait, "Block until the user sends something (run the Bash call with timeout 600000). Example: wait d9534008")
    p.add_argument("--timeout", type=int, default=540)

    p = cmd("reply", cmd_reply, "Post an answer in the chat; identical retries are not duplicated. Example: reply d9534008 --to 4,5 --file - --then-wait")
    p.add_argument("--to", default="", help="comma-separated seq numbers this answers")
    p.add_argument("--text")
    p.add_argument("--file", help="Markdown file, or - for stdin")
    p.add_argument("--then-wait", action="store_true")
    p.add_argument("--timeout", type=int, default=540)

    p = cmd("chat", cmd_chat, "Read chat entries; named seqs print in full. Example: chat d9534008 12 14")
    p.add_argument("seqs", nargs="*")
    p.add_argument("--last", type=int, default=20)
    p.add_argument("--full", action="store_true")

    p = cmd("backlog", cmd_backlog, "Manage backlog items. Example: backlog d9534008 add --title 'Test short rows' --anchor tests/t.py:1 --from 15")
    p.add_argument("action", choices=["add", "list", "update", "ack", "done", "dismiss", "reopen"])
    p.add_argument("ids", nargs="*")
    p.add_argument("--title")
    p.add_argument("--detail")
    p.add_argument("--detail-file", help="Markdown detail from a file, or - for stdin (use for multi-line details)")
    p.add_argument("--kind", default="fix", choices=["fix", "suggested-edit", "question"])
    p.add_argument("--anchor")
    p.add_argument("--from", dest="from_seq", type=int)
    p.add_argument("--patch")
    p.add_argument("--note")
    p.add_argument("--by", default=None)
    p.add_argument("--all", action="store_true", help="list: include done and dismissed")
    p.add_argument("--full", action="store_true", help="list: every field, untruncated")
    p.add_argument("--fields", help=f"list: comma-separated subset of {','.join(BACKLOG_FIELDS)}")

    p = cmd("detach", cmd_detach, "End a reviewer run before its final message. Example: detach d9534008 --reason handoff")
    p.add_argument("--reason", required=True, choices=["handoff", "execute", "idle", "end"])

    p = cmd("inbox", cmd_inbox, "Undelivered backlog for a repository (what the prompt hook injects).", sid=False)
    p.add_argument("--cwd")
    p.add_argument("--json", action="store_true")

    p = cmd("reload", cmd_reload, "Refresh the browser after a rebuild; recomputes git-sourced diffs.")
    p.add_argument("--page")

    p = cmd("prefs", cmd_prefs, "Show or set the model/effort the chat starts on.", sid=False)
    p.add_argument("--model", choices=store.MODELS)
    p.add_argument("--effort", choices=store.EFFORTS)

    p = cmd("prompt", cmd_prompt, "Reviewer instructions for a general-purpose spawn (before the agent types load).")
    p.add_argument("--model", required=True, choices=store.MODELS)
    p.add_argument("--effort", required=True, choices=store.EFFORTS)

    p = cmd("reviewer", cmd_reviewer, "Run the session's reviewer: one long-lived Claude session whose model and effort switch in place. Example: reviewer d9534008 start --model haiku --effort low")
    p.add_argument("action", choices=["start", "stop", "status"])
    p.add_argument("--model", choices=store.MODELS)
    p.add_argument("--effort", choices=store.EFFORTS)

    p = cmd("watch", cmd_watch, "Block until Execute, End or the reviewer exits (run in the background; its exit wakes the main agent).")
    p.add_argument("--timeout", type=int, default=7000)

    p = cmd("hook", cmd_hook, "Session hook entry points: session (SessionStart home view), prompt (UserPromptSubmit inbox), "
                                   "tool and stop (PostToolUse/Stop: deliver Execute/End to the owning agent while it is busy).", sid=False)
    p.add_argument("event", choices=["session", "prompt", "tool", "stop"])

    p = cmd("setup", cmd_setup, "Install ambient context: hooks + permission in settings.json, the PATH link, reviewer agents. Idempotent.", sid=False)
    p.add_argument("target", nargs="?", default="all", choices=["hooks", "bin", "agents", "all"])
    p.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))
    p.add_argument("--bin-dir", default="/opt/homebrew/bin")
    return ap


def apply_home(home: str | None) -> None:
    """--home selects an isolated session store; its config.json may pin a port and disable the browser."""
    global PORT
    if home:
        os.environ["REVIEW_DESK_HOME"] = str(Path(home).expanduser().resolve())
    cfg = store.read_json(store.home() / "config.json", {}) or {}
    if "REVIEW_DESK_PORT" not in os.environ and cfg.get("port"):
        PORT = int(cfg["port"])
    if cfg.get("no_open"):
        os.environ["REVIEW_DESK_NO_OPEN"] = "1"


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    apply_home(a.home)
    if not getattr(a, "fn", None):
        a.fn = cmd_home
    if getattr(a, "by", "unset") is None:
        a.by = "reviewer" if a.action == "add" else "main"
    try:
        return a.fn(a)
    except DeskError as e:
        return axi.fail(e)
    except (KeyError, ValueError) as e:
        return axi.fail(DeskError(str(e.args[0] if e.args else e), [run("--help")]))


if __name__ == "__main__":
    sys.exit(main())
