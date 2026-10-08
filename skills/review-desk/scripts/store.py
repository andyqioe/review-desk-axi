"""Durable state for Review Desk sessions. Disk is the only source of truth.

The CLI (agents) and the server (browser) both read and write these files directly, so a
dead server never blocks a reviewer and a killed reviewer never loses what it logged.

  <home>/sessions/<sid>        symlink to the session directory (or the directory itself)
  <home>/prefs.json            last model/effort the user picked

  <session dir>/
    session.json   sid, title, repo, page, token, status          (atomic rewrite)
    context.md     the reviewer's compact briefing                 (written once by `open`)
    manifest.json  changed files + hunk map; diffs/<n>.json rows  (written by build_page --review-data)
    commits/       one change set per commit, and the uncommitted rest, for the commit picker (gitdiff.py)
    chat.jsonl     every message and control event, with a seq    (append + fsync)
    backlog.jsonl  backlog events folded into items                (append + fsync)
    tray.jsonl     files/ranges the agent pinned into the editor   (append + fsync)
    pages.jsonl    HTML pages shown as read-only editor tabs       (append + fsync: open/close events)

  <home>/pages/<sid>/          pages the reviewer writes, its only writable folder; <home>/tmp/<sid>/ is scratch
                               for page generators. Both stay out of the repository and ~/.claude, where a
                               session directory may live (implementation-summary's .review folders) and where
                               Claude Code refuses writes.
    agent.json     reviewer heartbeat: run, tier, state, cursor    (atomic rewrite)

Appends happen under an flock on <dir>/.lock and end with fsync; readers skip a torn last
line, so a kill at any instant loses at most the line being written.
"""

from __future__ import annotations

import fcntl
import html
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

MODELS = ("haiku", "sonnet", "opus", "fable")
EFFORTS = ("low", "medium", "high")
USER_KINDS = ("msg", "flag", "suggest")          # user entries a reviewer must answer
CONTROL_KINDS = ("tier", "execute", "end")        # user entries that end a reviewer's run
OPEN_STATUSES = ("open", "handed-off")            # not yet accepted by the main agent
ALL_STATUSES = ("open", "handed-off", "acked", "done", "dismissed")
WAIT_GRACE = 90           # seconds past a wait timeout before a waiting reviewer counts as gone
THINK_LIMIT = 20 * 60     # seconds a reviewer may spend on one batch before it counts as gone


def home() -> Path:
    return Path(os.environ.get("REVIEW_DESK_HOME") or Path.home() / ".review-desk").expanduser()


SERVER_CODE = ("server.py", "store.py", "gitdiff.py")  # the Python a running server has loaded; assets are read per request


def code_id() -> str:
    """Fingerprint of the server's code on disk. A server reports the one it started with, so the CLI
    can tell a server that predates an upgrade (and lacks its routes) from a current one."""
    import hashlib
    h = hashlib.sha256()
    for name in SERVER_CODE:
        h.update((Path(__file__).resolve().parent / name).read_bytes())
    return h.hexdigest()[:16]


def anchor_ref(a: dict) -> str:
    """An anchor as agents read it: [old:]path[:a-b][@<commit>]. With @<commit> the lines are as of that
    commit (on the old side, as of its parent), because the user flagged them in that commit's view;
    without it they are the working tree's (the base's, on the old side)."""
    ref = f"{'old:' if a.get('side') == 'old' else ''}{a['path']}" + (f":{a['range']}" if a.get("range") else "")
    return ref + (f"@{a['commit'][:10]}" if a.get("commit") else "")


def now() -> float:
    return round(time.time(), 3)


# ------------------------------------------------------------------ files
def append_jsonl(path: Path, obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def read_jsonl(path: Path) -> list[dict]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in raw.splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # torn tail from a kill mid-write
    return out


def write_json(path: Path, obj: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------- session
class Session:
    def __init__(self, sid: str, directory: Path):
        self.sid = sid
        self.dir = directory

    # paths
    @property
    def meta_path(self) -> Path: return self.dir / "session.json"
    @property
    def chat_path(self) -> Path: return self.dir / "chat.jsonl"
    @property
    def backlog_path(self) -> Path: return self.dir / "backlog.jsonl"
    @property
    def tray_path(self) -> Path: return self.dir / "tray.jsonl"
    @property
    def pages_path(self) -> Path: return self.dir / "pages.jsonl"
    @property
    def agent_path(self) -> Path: return self.dir / "agent.json"
    @property
    def pages_dir(self) -> Path: return (home() / "pages" / self.sid).resolve()
    @property
    def scratch_dir(self) -> Path: return (home() / "tmp" / self.sid).resolve()
    @property
    def context_path(self) -> Path: return self.dir / "context.md"
    @property
    def manifest_path(self) -> Path: return self.dir / "manifest.json"

    @contextmanager
    def lock(self):
        fd = os.open(self.dir / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def meta(self) -> dict:
        return read_json(self.meta_path, {}) or {}

    def update_meta(self, **fields) -> dict:
        with self.lock():
            m = self.meta()
            m.update(fields)
            write_json(self.meta_path, m)
        return m

    def manifest(self) -> dict:
        return read_json(self.manifest_path, {"files": []}) or {"files": []}

    def revision(self) -> tuple:
        """Changes whenever anything the browser shows changes."""
        out = []
        shown = [Path(pg["path"]) for pg in self.pages() if pg["open"]]  # an edited page reloads its tab
        for p in (self.meta_path, self.chat_path, self.backlog_path, self.tray_path, self.pages_path, self.agent_path,
                  self.manifest_path, *shown):
            try:
                st = p.stat()
                out.append((st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                out.append(None)
        page = self.meta().get("page")
        if page:
            try:
                out.append(Path(page).stat().st_mtime_ns)
            except OSError:
                out.append(None)
        return tuple(out)

    # ------------------------------------------------------------- chat
    def chat(self) -> list[dict]:
        return read_jsonl(self.chat_path)

    def post(self, kind: str, role: str, **fields) -> dict:
        with self.lock():
            entries = self.chat()
            seq = (entries[-1]["seq"] + 1) if entries else 1
            entry = {"seq": seq, "kind": kind, "role": role, "at": now(), **{k: v for k, v in fields.items() if v is not None}}
            append_jsonl(self.chat_path, entry)
        return entry

    def unanswered(self, entries: list[dict] | None = None) -> list[dict]:
        entries = self.chat() if entries is None else entries
        answered = {i for e in entries if e.get("role") == "agent" for i in e.get("reply_to", [])}
        return [e for e in entries if e.get("role") == "user" and e["kind"] in USER_KINDS and e["seq"] not in answered]

    def wanted_tier(self) -> dict | None:
        """The tier the user last asked for: from the newest user entry that names one."""
        for e in reversed(self.chat()):
            if e.get("role") == "user" and e.get("model"):
                return {"model": e["model"], "effort": e.get("effort", "low")}
        return None

    # ---------------------------------------------------------- backlog
    def backlog_events(self) -> list[dict]:
        return read_jsonl(self.backlog_path)

    def backlog(self) -> list[dict]:
        items: dict[str, dict] = {}
        for ev in self.backlog_events():
            op, iid = ev.get("op"), ev.get("id")
            if op == "create":
                items[iid] = {k: v for k, v in ev.items() if k != "op"} | {"status": "open", "history": []}
            elif iid in items and op == "status":
                items[iid]["status"] = ev["status"]
                items[iid]["history"].append({"status": ev["status"], "by": ev.get("by"), "note": ev.get("note"), "at": ev.get("at")})
                if ev.get("note"):
                    items[iid]["note"] = ev["note"]
            elif iid in items and op == "update":
                items[iid].update(ev.get("fields", {}))
        return list(items.values())

    def backlog_add(self, title: str, detail: str = "", kind: str = "fix", anchor: str | None = None,
                    from_seq: int | None = None, patch: str | None = None, by: str = "reviewer",
                    dedupe: bool = False) -> dict:
        """Create an item. With dedupe, an item with the same title, anchor and source message is
        returned instead (marked `existing`), so a retried command never logs the work twice."""
        with self.lock():
            if dedupe:
                for i in self.backlog():
                    if (i["title"], i.get("anchor"), i.get("from")) == (title, anchor or None, from_seq):
                        return i | {"existing": True}
            n = sum(1 for ev in self.backlog_events() if ev.get("op") == "create") + 1
            ev = {"op": "create", "id": f"B{n}", "title": title, "detail": detail, "kind": kind, "anchor": anchor,
                  "from": from_seq, "patch": patch, "by": by, "at": now()}
            append_jsonl(self.backlog_path, {k: v for k, v in ev.items() if v not in (None, "")})
        return self.item(f"B{n}")

    def item(self, iid: str) -> dict | None:
        return next((i for i in self.backlog() if i["id"] == iid), None)

    def backlog_status(self, ids: list[str], status: str, by: str, note: str | None = None) -> list[dict]:
        if status not in ALL_STATUSES:
            raise ValueError(f"unknown status {status}")
        with self.lock():
            known = {i["id"]: i for i in self.backlog()}
            missing = [i for i in ids if i not in known]
            if missing:
                raise KeyError(f"no backlog item {', '.join(missing)}")
            for iid in ids:
                if known[iid]["status"] != status:
                    append_jsonl(self.backlog_path, {"op": "status", "id": iid, "status": status, "by": by, "note": note, "at": now()})
        return [self.item(i) for i in ids]

    def backlog_update(self, iid: str, **fields) -> dict:
        with self.lock():
            if not any(i["id"] == iid for i in self.backlog()):
                raise KeyError(f"no backlog item {iid}")
            append_jsonl(self.backlog_path, {"op": "update", "id": iid, "fields": {k: v for k, v in fields.items() if v is not None}, "at": now()})
        return self.item(iid)

    def handoff(self) -> list[dict]:
        """Items the main agent has not accepted yet; marks open ones handed-off. Safe to repeat."""
        with self.lock():
            items = [i for i in self.backlog() if i["status"] in OPEN_STATUSES]
            for i in items:
                if i["status"] == "open":
                    append_jsonl(self.backlog_path, {"op": "status", "id": i["id"], "status": "handed-off", "by": "handoff", "at": now()})
        return [self.item(i["id"]) for i in items]

    def unfinished(self) -> list[dict]:
        """Items the main agent accepted (acked) but has not closed with done, dismiss or reopen. Each carries
        `acked_at`, so a reminder can fire once per acceptance instead of once per item forever."""
        out = []
        for i in self.backlog():
            if i["status"] == "acked":
                at = next((h.get("at") for h in reversed(i.get("history", [])) if h["status"] == "acked"), None)
                out.append(i | {"acked_at": at})
        return out

    def execute_requested(self) -> list[str]:
        """Item ids the user asked to execute that the main agent has not accepted yet."""
        status = {i["id"]: i["status"] for i in self.backlog()}
        want: list[str] = []
        for e in self.chat():
            if e.get("kind") == "execute":
                want += [i for i in e.get("ids", []) if i not in want]
        return [i for i in want if status.get(i) in OPEN_STATUSES]

    # ------------------------------------------------------------- tray
    def tray(self) -> list[dict]:
        seen: dict[str, dict] = {}
        for t in read_jsonl(self.tray_path):
            seen[t["ref"]] = t
        return list(seen.values())

    def tray_add(self, ref: str, focus: bool = False, note: str | None = None) -> tuple[dict, bool]:
        """Pin a ref; re-pinning the same ref with the same note is a no-op. Returns (entry, added)."""
        entry = {"ref": ref, "focus": focus, "note": note, "at": now()}
        with self.lock():
            old = next((t for t in self.tray() if t["ref"] == ref), None)
            if old and old.get("note") == note and not focus:
                return old, False
            append_jsonl(self.tray_path, {k: v for k, v in entry.items() if v is not None})
        return entry, True

    # ------------------------------------------------------------ pages
    # An HTML page (a reviewer's Lavish page, a skill's --html output, an implementation summary) shown
    # as a read-only editor tab. One id per path (P1, P2, ...); open and close events fold to the current set.
    def pages(self) -> list[dict]:
        out: dict[str, dict] = {}
        for e in read_jsonl(self.pages_path):
            pg = out.setdefault(e["id"], {"id": e["id"], "path": e["path"], "title": None, "open": False, "focus": 0})
            if e["op"] == "open":
                pg.update(open=True, by=e.get("by"), at=e.get("at"))
                if e.get("title"):
                    pg["title"] = e["title"]
                if e.get("focus"):
                    pg["focus"] = e.get("at", 0)
            elif e["op"] == "close":
                pg.update(open=False, closed_by=e.get("by"), at=e.get("at"))
        for pg in out.values():
            try:
                pg["mtime"] = Path(pg["path"]).stat().st_mtime_ns
            except OSError:
                pg["mtime"] = None  # deleted since it was opened; the tab says so
        return sorted(out.values(), key=lambda pg: int(pg["id"][1:]))

    def page(self, ref: str) -> dict | None:
        """A page by id (P2) or by path."""
        ref = ref.strip()
        key = str(Path(ref).expanduser().resolve()) if not re.fullmatch(r"[Pp]\d+", ref) else None
        return next((pg for pg in self.pages() if (pg["path"] == key if key else pg["id"] == ref.upper())), None)

    def page_open(self, path: str, title: str | None = None, focus: bool = True, by: str = "main") -> tuple[dict, bool]:
        """Open (or re-show) a page; returns (page, changed). Re-opening an open page without focus is a no-op.
        Without a title, a new page is named by its <title>."""
        path = str(Path(path).expanduser().resolve())
        if not title and not any(x["path"] == path and x["title"] for x in self.pages()):
            title = html_title(Path(path))
        with self.lock():
            pages = self.pages()
            pg = next((x for x in pages if x["path"] == path), None)
            if pg and pg["open"] and not focus and (not title or title == pg["title"]):
                return pg, False
            pid = pg["id"] if pg else f"P{len(pages) + 1}"
            append_jsonl(self.pages_path, {k: v for k, v in {"op": "open", "id": pid, "path": path, "title": title,
                                                             "focus": focus, "by": by, "at": now()}.items() if v is not None})
        return next(x for x in self.pages() if x["id"] == pid), True

    def page_close(self, pid: str, by: str = "main") -> bool:
        with self.lock():
            pg = next((x for x in self.pages() if x["id"] == pid), None)
            if not pg or not pg["open"]:
                return False
            append_jsonl(self.pages_path, {"op": "close", "id": pid, "path": pg["path"], "by": by, "at": now()})
        return True

    def view_key(self) -> str:
        """Capability for the page routes: reads registered pages and their folders, nothing else."""
        key = self.meta().get("view_key")
        if not key:
            key = secrets.token_urlsafe(18)
            self.update_meta(view_key=key)
        return key

    # ------------------------------------------------------------ agent
    def agent(self) -> dict:
        return read_json(self.agent_path, {}) or {}

    def set_agent(self, **fields) -> dict:
        with self.lock():
            a = self.agent()
            a.update(fields, ts=now())
            write_json(self.agent_path, a)
        return a

    def presence(self) -> dict:
        """The reviewer as the browser and the inbox should see it."""
        a = self.agent()
        state = a.get("state")
        age = time.time() - a.get("ts", 0)
        live = (state == "waiting" and age < a.get("timeout", 540) + WAIT_GRACE) or (state == "thinking" and age < THINK_LIMIT)
        wanted = self.wanted_tier()
        out = {"live": bool(live), "state": state if live else ("exited" if state == "exited" else "offline"),
               "model": a.get("model"), "effort": a.get("effort"), "reason": a.get("reason"), "ts": a.get("ts"),
               "wanted": wanted, "main": bool(a.get("main"))}
        if not live and self.meta().get("status") != "ended":
            if a.get("reason") == "handoff":
                out["state"] = "respawning"   # main starts the tier the user asked for
            elif a.get("reason") == "execute":
                out["state"] = "executing"    # main implements, then restarts the reviewer
        return out

    # ------------------------------------------------------------- delivery
    # Execute and End reach the main agent two ways: its harness hooks (every tool call and every stop,
    # so a busy agent gets them too) and `watch` (wakes an idle one). Both advance main_cursor, so an
    # event is delivered once whichever path gets there first.
    def pending_events(self) -> list[dict]:
        """User Execute/End entries past the main agent's cursor. An Execute whose items were all
        accepted since then is not pending: it was handled by hand (handoff, ack)."""
        m = self.meta()
        cursor = m.get("main_cursor") or 0
        entries = [e for e in self.chat() if e["seq"] > cursor and e.get("role") == "user" and e.get("kind") in ("execute", "end")]
        if any(e["kind"] == "execute" for e in entries) and not self.execute_requested():
            entries = [e for e in entries if e["kind"] != "execute"]
        return entries

    def mark_delivered(self, seq: int, via: str, to: str | None = None) -> None:
        with self.lock():
            m = self.meta()
            m["main_cursor"] = max(m.get("main_cursor") or 0, seq)
            m["delivered"] = {"seq": seq, "via": via, "to": to, "at": now()}
            write_json(self.meta_path, m)

    def claim(self, agent_session: str, harness: str = "claude") -> None:
        """The agent session driving this desk receives its Execute and End. The latest driver wins."""
        owner = self.meta().get("owner") or {}
        if owner.get("session") != agent_session:
            if owner.get("session"):
                owned_update(owner["session"], remove=self.sid)
            self.update_meta(owner={"session": agent_session, "harness": harness, "seen": now()})
        owned_update(agent_session, add=self.sid)

    def owner_seen(self) -> None:
        """Heartbeat from the owner's hooks, at most every 20 s: it is working and will see the next event."""
        m = self.meta()
        owner = m.get("owner") or {}
        if owner and time.time() - owner.get("seen", 0) > 20:
            self.update_meta(owner=owner | {"seen": now()})

    def delivery(self) -> dict:
        """What happened to the latest Execute/End, as the desk should tell the user."""
        m = self.meta()
        owner, watcher, last = m.get("owner") or {}, m.get("watcher") or {}, m.get("delivered")
        watching = bool(watcher) and time.time() - watcher.get("at", 0) < 30 and pid_alive(watcher.get("pid"))
        busy = bool(owner) and time.time() - owner.get("seen", 0) < 120
        pending = self.pending_events()
        if pending:
            state = "waking" if watching else "queued" if busy else "unheard"
        else:
            state = "delivered" if last else "idle"
        return {"state": state, "pending": [e["seq"] for e in pending], "execute": self.execute_requested() if pending else [],
                "owner": owner or None, "watching": watching, "delivered": last}


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, OSError):
        return False


def owned(agent_session: str) -> list[str]:
    """Desks an agent session owns (its hooks check only these, so they stay cheap)."""
    return read_json(home() / "owners" / f"{agent_session}.json", []) or []


def owned_update(agent_session: str, add: str | None = None, remove: str | None = None) -> None:
    if not re.fullmatch(r"[\w-]{1,80}", agent_session or ""):
        return
    path = home() / "owners" / f"{agent_session}.json"
    sids = [x for x in owned(agent_session) if x != remove]
    if add and add not in sids:
        sids.append(add)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, sids)


# --------------------------------------------------------------- registry
def html_title(path: Path) -> str | None:
    try:
        head = path.read_bytes()[:65536].decode("utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"<title[^>]*>(.*?)</title>", head, re.S | re.I)
    title = " ".join(html.unescape(m.group(1)).split()) if m else ""
    return title[:80] or None


def new_sid() -> str:
    """8 hex chars that never read as a number (`4180e859` is a float), so TOON prints ids bare."""
    while True:
        sid = secrets.token_hex(4)
        if not re.fullmatch(r"\d+(e\d+)?", sid):
            return sid


def create(directory: Path | None, **meta) -> Session:
    """Create a session in `directory` (reusing it if it already holds one), or under <home>/sessions."""
    reg = home() / "sessions"
    reg.mkdir(parents=True, exist_ok=True)
    meta = {k: v for k, v in meta.items() if v is not None}
    existing = read_json(directory.expanduser() / "session.json") if directory else None
    if existing and existing.get("sid"):
        sid = existing["sid"]
    else:
        sid = new_sid()
        while (reg / sid).exists() or (reg / sid).is_symlink():
            sid = new_sid()
    directory = (directory.expanduser() if directory else reg / sid).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    s = Session(sid, directory)
    if existing and existing.get("sid"):
        s.update_meta(**meta, status="open")
    else:
        write_json(s.meta_path, {"sid": sid, "token": secrets.token_urlsafe(18), "created": now(), "status": "open", **meta})
    link = reg / sid
    if link.resolve() != directory:
        if link.is_symlink():
            link.unlink()
        link.symlink_to(directory)
    return s


def open_session(sid: str) -> Session:
    p = home() / "sessions" / sid
    if not p.exists():
        raise KeyError(f"no review-desk session {sid} (looked in {home() / 'sessions'})")
    return Session(sid, p.resolve())


def sessions() -> list[Session]:
    reg = home() / "sessions"
    out = []
    for p in sorted(reg.iterdir()) if reg.is_dir() else []:
        if p.exists() and (p / "session.json").exists():
            out.append(Session(p.name, p.resolve()))
    return out


def prefs() -> dict:
    return read_json(home() / "prefs.json", {}) or {}


def save_prefs(**fields) -> None:
    home().mkdir(parents=True, exist_ok=True)
    p = prefs()
    p.update(fields)
    write_json(home() / "prefs.json", p)
