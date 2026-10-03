#!/usr/bin/env python3
"""Review Desk server: serves the editor template and the browser's half of the protocol.

Loopback only. Every session page and API call needs the session's token, and POSTs from
another origin are refused: chat text reaches a reviewer agent as the user's words, so no
other local page may be able to write it. Reads and writes go straight to the session's
files (store.py); the server holds no state of its own beyond SSE connections.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

ASSETS = Path(__file__).resolve().parent.parent / "assets"
CODE_ID = store.code_id()  # fixed at start: what this process runs, even after the files change
IDLE_SECONDS = int(os.environ.get("REVIEW_DESK_IDLE", str(30 * 60)))
MAX_FILE = 2 * 1024 * 1024
MAX_BODY = 1024 * 1024
ROUTE = re.compile(r"^/(?:api|s)/(?P<sid>[0-9a-f]{8})(?:/(?P<rest>.*))?$")
CLIENTS: dict[str, int] = {}
CLIENTS_LOCK = threading.Lock()
LAST_ACTIVITY = [time.time()]


def excerpt(s: store.Session, path: str, rng: str | None, side: str = "new", limit: int = 40) -> str:
    """First lines of an anchor, from the file on disk (new side) or the diff rows (old side)."""
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", rng or "")
    if not m:
        return ""
    a, b = int(m[1]), int(m[2] or m[1])
    if side == "old":
        f = next((f for f in s.manifest().get("files", []) if f["path"] == path), None)
        rows = (store.read_json(s.dir / "diffs" / f"{f['n']}.json", {}) or {}).get("rows", []) if f else []
        lines = [f"{r[0]}  {r[2]}" for r in rows if r[0] and a <= int(r[0]) <= b]
        return "\n".join(lines[:limit])
    lines = repo_lines(s, path)
    if lines is None:
        return ""
    return "\n".join(f"{n}  {lines[n - 1]}" for n in range(a, min(b, len(lines), a + limit - 1) + 1))


def repo_file(s: store.Session, rel: str) -> Path | None:
    """A file in the repository, by its repository-relative path. Containment is judged on the path as
    written (no `..`, not absolute), so a symlink the repository itself holds opens the way git and editors
    open it, while a crafted path still cannot climb out of the repository."""
    rp = Path(rel)
    if not rel or rp.is_absolute() or ".." in rp.parts:
        return None
    p = Path(s.meta().get("repo", "/")) / rp
    return p if p.is_file() else None


def repo_lines(s: store.Session, rel: str) -> list[str] | None:
    p = repo_file(s, rel)
    if p is None or p.stat().st_size > MAX_FILE:
        return None
    return p.read_text(encoding="utf-8", errors="replace").splitlines()


def tier_guard(s: store.Session, model: str | None, effort: str | None) -> None:
    """A user entry for a different tier than the live reviewer's first asks that reviewer to hand over."""
    if not model:
        return
    store.save_prefs(model=model, effort=effort or "low")
    p = s.presence()
    if p["live"] and not p.get("main") and (p.get("model"), p.get("effort")) != (model, effort):
        s.post("tier", "user", model=model, effort=effort)


def clean_anchors(s: store.Session, anchors) -> list[dict]:
    out = []
    for a in anchors or []:
        if not isinstance(a, dict) or not a.get("path"):
            continue
        side = "old" if a.get("side") == "old" else "new"
        rng = str(a.get("range") or "")
        out.append({"path": str(a["path"]), "range": rng or None, "side": side,
                    "excerpt": excerpt(s, a["path"], rng, side)})
    return out


def make_patch(s: store.Session, path: str, rng: str, replacement: str) -> str | None:
    lines = repo_lines(s, path)
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", rng or "")
    if lines is None or not m:
        return None
    a, b = int(m[1]), int(m[2] or m[1])
    new = lines[: a - 1] + replacement.split("\n") + lines[b:]
    return "".join(difflib.unified_diff([l + "\n" for l in lines], [l + "\n" for l in new], f"a/{path}", f"b/{path}", n=2))


def page_path(s: store.Session, ref: str) -> str | None:
    """An HTML file the user asked to open: absolute, ~/..., or relative. A relative name is looked up in
    the session's pages folder first (the reviewer names its pages by file name alone), then the repository
    and the summary's link root."""
    if not ref:
        return None
    p = Path(ref).expanduser()
    if p.is_absolute():
        candidates = [p]
    else:
        roots = [s.pages_dir, Path(s.meta().get("repo", ".")), Path(s.manifest().get("link_root") or ".")]
        candidates = [r / p for r in roots]
    for c in candidates:
        c = c.resolve()
        if c.suffix.lower() in PAGE_EXT and c.is_file():
            return str(c)
    return None


def skill_list() -> list[dict]:
    """User skills for the composer's slash autocomplete: name + first sentence of the description."""
    out = []
    root = Path.home() / ".claude" / "skills"
    for md in sorted(root.glob("*/SKILL.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.match(r"---\n(.*?)\n---", text, re.S)
        if not m:
            continue
        front = m.group(1)
        name = re.search(r"^name:\s*(.+)$", front, re.M)
        desc = re.search(r"^description:\s*(?:>-?|\|)?\s*\n?((?:.*\n?)*?)(?=^\S|\Z)", front, re.M)
        if not name or "workspace" in md.parent.name:
            continue
        d = " ".join((desc.group(1) if desc else "").split())
        d = re.split(r"(?<=[.!?])\s", d, 1)[0][:160]
        out.append({"name": name.group(1).strip().strip('"'), "description": d})
    return out


def state(s: store.Session) -> dict:
    s.view_key()
    m = s.meta()
    home = str(Path.home())
    pages = [{**pg, "shown": "~" + pg["path"][len(home):] if pg["path"].startswith(home + "/") else pg["path"]} for pg in s.pages()]
    return {"session": {k: v for k, v in m.items() if k not in ("token", "view_key")}, "chat": s.chat(), "backlog": s.backlog(),
            "tray": s.tray(), "pages": pages, "view": f"/s/{s.sid}/view/{m['view_key']}",
            "agent": s.presence(), "unanswered": [e["seq"] for e in s.unanswered()],
            "prefs": store.prefs(), "models": store.MODELS, "efforts": store.EFFORTS, "draft": read_draft(s),
            "delivery": s.delivery()}


def read_draft(s: store.Session) -> dict | None:
    """The reviewer's streaming answer (reviewer_host writes stream.json), or None."""
    return store.read_json(s.dir / "stream.json")


# Pages render in a sandboxed, opaque origin: their scripts run but cannot reach the desk's token or API.
# The CSP header sandboxes the page even when it is opened outside the desk's iframe.
PAGE_SANDBOX = "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals"
# Injected into every page: editor links (vscode://file/..., file://...) open in the desk instead.
# A plain click on a [data-ref] path:line chip (analyze-code's figures and prose) opens it too; a modifier
# click keeps the page's own action (copy). Cmd/Ctrl+Shift+F inside a page opens the desk's search.
PAGE_BRIDGE = (b"<script data-review-desk>(()=>{if(window.parent===window)return;"
               b"window.addEventListener('click',e=>{if(e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||!e.target.closest)return;"
               b"const r=e.target.closest('[data-ref]'),ref=r&&r.getAttribute('data-ref');"
               b"if(ref&&/^[\\w.\\/@+-]+(:\\d+(-\\d+)?)?$/.test(ref)){e.preventDefault();e.stopImmediatePropagation();"
               b"window.parent.postMessage({desk:'ref',ref},'*');return;}"
               b"const a=e.target.closest('a[href]');if(!a)return;const h=a.getAttribute('href')||'';"
               b"if(/^(vscode|cursor|windsurf|zed):\\/\\/file\\//.test(h)||/^file:\\/\\//.test(h)){e.preventDefault();"
               b"window.parent.postMessage({desk:'open',href:h,title:a.title||''},'*');}},true);"
               b"window.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.shiftKey&&!e.altKey&&e.key.toLowerCase()==='f')"
               b"{e.preventDefault();window.parent.postMessage({desk:'search'},'*');}},true);})();</script>")
PAGE_EXT = (".html", ".htm")


def inject_bridge(page: bytes) -> bytes:
    i = page.lower().rfind(b"</body>")
    return page[:i] + PAGE_BRIDGE + page[i:] if i >= 0 else page + PAGE_BRIDGE


class Handler(BaseHTTPRequestHandler):
    server_version = "review-desk"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet; errors still go to stderr via send_error paths
        if os.environ.get("REVIEW_DESK_DEBUG"):
            super().log_message(fmt, *args)

    # ----------------------------------------------------------- replies
    def send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code: int = 200) -> None:
        self.send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def fail(self, code: int, msg: str) -> None:
        self.json({"error": msg}, code)

    # ------------------------------------------------------------ gating
    def session_for(self, sid: str, token: str | None) -> store.Session | None:
        try:
            s = store.open_session(sid)
        except KeyError:
            self.fail(404, "no such session")
            return None
        if not token or not secrets.compare_digest(token, s.meta().get("token", "")):
            self.fail(403, "bad token")
            return None
        return s

    def same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        return origin in (None, f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    # --------------------------------------------------------------- GET
    def do_GET(self):  # noqa: N802
        LAST_ACTIVITY[0] = time.time()
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/health":
            with CLIENTS_LOCK:
                clients = dict(CLIENTS)
            return self.json({"app": "review-desk", "home": str(store.home().resolve()), "pid": os.getpid(), "clients": clients,
                              "code": CODE_ID})
        if u.path.startswith("/assets/"):
            name = u.path[len("/assets/"):]
            p = (ASSETS / name).resolve()
            if ASSETS.resolve() not in p.parents or not p.is_file():
                return self.fail(404, "no asset")
            ctype = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}.get(p.suffix, "application/octet-stream")
            return self.send(200, p.read_bytes(), ctype + "; charset=utf-8")
        m = ROUTE.match(u.path)
        if not m:
            return self.fail(404, "not found")
        sid, rest = m["sid"], m["rest"] or ""
        api = u.path.startswith("/api/")
        if not api and rest.startswith("view/"):
            return self.view(sid, rest[5:])
        token = self.headers.get("X-Desk-Token") or q.get("t")
        s = self.session_for(sid, token)
        if s is None:
            return
        if not api:
            if rest == "":
                page = (ASSETS / "desk.html").read_text(encoding="utf-8")
                cfg = json.dumps({"sid": sid, "token": token, "title": s.meta().get("title", "Review")})
                page = page.replace("{{CONFIG}}", cfg.replace("</", "<\\/")).replace("{{TITLE}}", html.escape(s.meta().get("title", "Review")))
                return self.send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if rest == "story":
                page = s.meta().get("page")
                if not page or not Path(page).is_file():
                    return self.send(200, b"<!doctype html><body style='background:#050505;color:#a1a1aa;font:14px Inter,sans-serif;padding:40px'>No summary page for this session.</body>",
                                     "text/html; charset=utf-8")
                return self.send(200, Path(page).read_bytes(), "text/html; charset=utf-8")
            return self.fail(404, "not found")
        if rest == "state":
            return self.json(state(s))
        if rest == "skills":
            return self.json({"skills": skill_list()})
        if rest == "manifest":
            return self.json(s.manifest())
        if rest.startswith("diff/"):
            n = rest[5:]
            data = store.read_json(s.dir / "diffs" / f"{n}.json") if n.isdigit() else None
            return self.json(data) if data is not None else self.fail(404, "no diff")
        if rest == "file":
            lines = repo_lines(s, q.get("path", ""))
            if lines is None:
                return self.fail(404, "file not readable in repo")
            return self.json({"path": q["path"], "lines": lines})
        if rest == "events":
            return self.events(s)
        if rest == "search":
            return self.search(s, q)
        return self.fail(404, "not found")

    def search(self, s: store.Session, q: dict) -> None:
        """Cmd+Shift+F: stream matches as NDJSON, flushed in small batches. A client that aborts a stale query
        closes the socket; the next write fails and closing the generator kills ripgrep."""
        import search as sr
        flag = lambda k: q.get(k) in ("1", "true")  # noqa: E731
        spec = sr.Spec(q=q.get("q", "")[:500], regex=flag("regex"), case=flag("case"), word=flag("word"), paths=q.get("paths", "")[:500])
        m = s.manifest()
        repo = Path(m.get("repo") or s.meta().get("repo", "."))
        badges = {f["path"]: f.get("badge") for f in m.get("files", [])}
        files = None
        if q.get("scope") != "repo":
            files = [p for p, b in badges.items() if b != "D" and (repo / p).is_file()]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        gen = sr.run(repo, spec, files, badges)
        buf, flushed = [], time.monotonic()
        try:
            for rec in gen:
                buf.append(json.dumps(rec, ensure_ascii=False))
                if rec["t"] != "m" or len(buf) >= 100 or time.monotonic() - flushed > 0.03:
                    self.wfile.write(("\n".join(buf) + "\n").encode("utf-8"))
                    self.wfile.flush()
                    buf, flushed = [], time.monotonic()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            gen.close()

    def view(self, sid: str, rest: str) -> None:
        """/s/<sid>/view/<key>/<page id>/<file>: a registered page, or a file in its folder (its assets).
        The key only reads pages; relative links keep working because they resolve under the same prefix."""
        parts = rest.split("/", 2)
        if len(parts) < 3:
            return self.fail(404, "not found")
        key, pid, rel = parts
        try:
            s = store.open_session(sid)
        except KeyError:
            return self.fail(404, "no such session")
        if not s.meta().get("view_key") or not secrets.compare_digest(key, s.meta()["view_key"]):
            return self.fail(403, "bad key")
        pg = next((x for x in s.pages() if x["id"] == pid), None)
        if not pg:
            return self.fail(404, "no such page")
        root = Path(pg["path"]).parent.resolve()
        target = (root / rel).resolve()
        if root != target.parent and root not in target.parents:
            return self.fail(403, "outside the page folder")
        if any(part.startswith(".") for part in target.relative_to(root).parts) or not target.is_file():
            return self.fail(404, "no such file")
        body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        extra = {"Access-Control-Allow-Origin": "*"}  # the sandboxed page has an opaque origin; fonts need CORS
        if target.suffix.lower() in PAGE_EXT:
            body = inject_bridge(body)
            extra["Content-Security-Policy"] = PAGE_SANDBOX
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        return self.send(200, body, ctype, extra)

    def events(self, s: store.Session) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        with CLIENTS_LOCK:
            CLIENTS[s.sid] = CLIENTS.get(s.sid, 0) + 1
        try:
            last, beat, tick = None, time.time(), time.time()
            draft_path, draft_rev = s.dir / "stream.json", None
            while True:
                try:
                    dr = draft_path.stat().st_mtime_ns
                except FileNotFoundError:
                    dr = None
                if dr != draft_rev:  # streamed text goes out on its own channel, without a full state refetch
                    payload = json.dumps(read_draft(s) or {}, ensure_ascii=False)
                    self.wfile.write(f"event: stream\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    draft_rev = dr
                rev = s.revision()
                if rev != last or time.time() - tick > 20:  # periodic sync keeps presence ages fresh
                    self.wfile.write(b"event: sync\ndata: {}\n\n")
                    self.wfile.flush()
                    last, tick = rev, time.time()
                elif time.time() - beat > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    beat = time.time()
                LAST_ACTIVITY[0] = time.time()
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with CLIENTS_LOCK:
                CLIENTS[s.sid] -= 1
                if CLIENTS[s.sid] <= 0:
                    del CLIENTS[s.sid]

    # -------------------------------------------------------------- POST
    def do_POST(self):  # noqa: N802
        LAST_ACTIVITY[0] = time.time()
        u = urlparse(self.path)
        m = ROUTE.match(u.path)
        if not m or not u.path.startswith("/api/"):
            return self.fail(404, "not found")
        if not self.same_origin():
            return self.fail(403, "cross-origin request refused")
        s = self.session_for(m["sid"], self.headers.get("X-Desk-Token"))
        if s is None:
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self.fail(413, "too large")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self.fail(400, "bad json")
        rest = m["rest"] or ""
        model = body.get("model") if body.get("model") in store.MODELS else None
        effort = body.get("effort") if body.get("effort") in store.EFFORTS else None
        if s.meta().get("status") == "ended" and rest not in ("reopen",):
            return self.fail(409, "review ended")

        if rest == "message":
            text = str(body.get("text", "")).strip()
            if not text:
                return self.fail(400, "empty message")
            tier_guard(s, model, effort)
            cmd = re.match(r"^/([\w:-]+)\s*(.*)$", text, re.S)
            skill = cmd.group(1) if cmd and cmd.group(1) in {k["name"] for k in skill_list()} else None
            e = s.post("msg", "user", text=text, anchors=clean_anchors(s, body.get("anchors")) or None, model=model, effort=effort,
                       skill=skill, args=cmd.group(2).strip() if skill else None)
            return self.json({"seq": e["seq"]})
        if rest in ("flag", "suggest"):
            anchors = clean_anchors(s, [body.get("anchor")])
            if not anchors:
                return self.fail(400, "anchor required")
            a = anchors[0]
            note = str(body.get("note", "")).strip()
            spec = f"{a['path']}:{a['range']}" if a.get("range") else a["path"]
            patch = None
            if rest == "suggest":
                patch = make_patch(s, a["path"], a.get("range") or "", str(body.get("replacement", "")))
                if patch is None:
                    return self.fail(400, "cannot build a patch for that range")
                if not patch:
                    return self.fail(400, "suggestion is identical to the current lines")
            title = (note.splitlines()[0] if note else ("Suggested edit" if patch else "Flagged")) [:100]
            tier_guard(s, model, effort)
            item = s.backlog_add(title, note, "suggested-edit" if patch else "fix", spec, None, patch, by="user")
            e = s.post(rest, "user", text=note or title, anchors=[a], item=item["id"], model=model, effort=effort,
                       suggestion=str(body.get("replacement")) if patch else None)
            s.backlog_update(item["id"], **{"from": e["seq"]})
            return self.json({"seq": e["seq"], "item": item["id"]})
        if rest == "execute":
            ids = [str(i) for i in body.get("ids") or []]
            open_ids = [i["id"] for i in s.backlog() if i["status"] in store.OPEN_STATUSES]
            ids = [i for i in ids if i in open_ids] if ids else open_ids
            if not ids:
                return self.fail(400, "nothing to execute")
            e = s.post("execute", "user", ids=ids)
            return self.json({"seq": e["seq"], "ids": ids})
        if rest == "backlog":
            status = body.get("status")
            if status not in ("dismissed", "open"):
                return self.fail(400, "users may only dismiss or reopen")
            try:
                s.backlog_status([str(body.get("id"))], status, by="user")
            except KeyError as err:
                return self.fail(404, str(err))
            return self.json({"ok": True})
        if rest == "pages":
            op = body.get("op")
            if op == "close":
                pg = s.page(str(body.get("id", "")))
                if not pg:
                    return self.fail(404, "no such page")
                s.page_close(pg["id"], by="user")
                return self.json({"ok": True, "id": pg["id"]})
            if op == "open":
                ref = str(body.get("id") or body.get("path") or "")
                pg = s.page(ref) if re.fullmatch(r"[Pp]\d+", ref) else None
                path = pg["path"] if pg else page_path(s, ref)
                if not path:
                    return self.fail(400, "not an HTML file")
                pg, _ = s.page_open(path, focus=True, by="user")
                return self.json({"ok": True, "id": pg["id"]})
            return self.fail(400, "op must be open or close")
        if rest == "end":
            s.post("end", "user")
            s.update_meta(status="ended")
            return self.json({"ok": True})
        return self.fail(404, "not found")


def watchdog(httpd: ThreadingHTTPServer) -> None:
    while True:
        time.sleep(30)
        with CLIENTS_LOCK:
            busy = bool(CLIENTS)
        if not busy and time.time() - LAST_ACTIVITY[0] > IDLE_SECONDS:
            httpd.shutdown()
            return


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("REVIEW_DESK_PORT", "4388")))
    a = ap.parse_args()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    threading.Thread(target=watchdog, args=(httpd,), daemon=True).start()
    print(f"review-desk serving {store.home()} on http://127.0.0.1:{a.port} (pid {os.getpid()})", flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
