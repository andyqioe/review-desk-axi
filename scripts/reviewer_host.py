#!/usr/bin/env python3
"""Reviewer host: one long-lived Claude session per Review Desk session.

  reviewer_host.py <sid> --model M --effort E      (started by `review-desk-axi reviewer start`)

The host owns a `claude -p --input-format stream-json` process. It reads the user's new chat
entries from disk, sends each batch as one turn, and posts the turn's final text back to the
chat. A message sent on another model or effort switches the live session in place
(`set_model` and `apply_flag_settings {effortLevel}` control requests): same process, same
conversation, no respawn. Execute is the main agent's business (`review-desk-axi watch`); an
end event, idle time or a signal shuts the host down. The Claude session id is saved, so a
restarted host resumes the conversation with `--resume`, but only while the system prompt is the one
that conversation started with: `--resume` keeps the original prompt and ignores a new
`--append-system-prompt`, so after a desk upgrade the host starts fresh and briefs the chat instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

SKILL = Path(__file__).resolve().parent.parent
BIN = "review-desk-axi"
HEARTBEAT = 10           # seconds between agent.json refreshes
TIMEOUT = 60             # what the host tells presence its heartbeat window is
TURN_LIMIT = 15 * 60     # longest a single turn may run before it is reported as failed
IDLE_LIMIT = int(os.environ.get("REVIEW_DESK_HOST_IDLE", str(90 * 60)))
CHAT_TAIL = 20


def claude_bin() -> str:
    return os.environ.get("REVIEW_DESK_CLAUDE") or shutil.which("claude") or "claude"


def system_prompt(sid: str, pages: Path) -> str:
    text = (SKILL / "agents" / "host-reviewer.md").read_text(encoding="utf-8")
    return text.replace("{{SID}}", sid).replace("{{BIN}}", BIN).replace("{{PAGES}}", str(pages))


def tool_status(tool: dict) -> str:
    """'Read app/ids.py', 'Bash git diff …': what the reviewer is doing, from the tool call's input."""
    try:
        args = json.loads(tool["json"] or "{}")
    except json.JSONDecodeError:
        args = {}
    name = tool["name"]
    target = args.get("file_path") or args.get("path") or args.get("pattern") or args.get("command") or args.get("skill") or ""
    target = str(target)
    if "/" in target and not target.startswith(("git ", "review-desk")):
        target = target.split("/repo/", 1)[-1]
    return f"{name} {target[:70]}".strip()


def describe(e: dict) -> str:
    """One chat entry as the reviewer reads it."""
    kind = f"skill:{e['skill']}" if e.get("skill") else e["kind"]
    head = f"#{e['seq']} {kind}"
    if e.get("model"):
        head += f" [{e['model']}/{e.get('effort', '?')}]"
    for a in e.get("anchors", []):
        head += f" @{'old:' if a.get('side') == 'old' else ''}{a['path']}" + (f":{a['range']}" if a.get("range") else "")
    if e.get("item"):
        head += f" (backlog {e['item']})"
    lines = [head, e.get("text", "")]
    for a in e.get("anchors", []):
        if a.get("excerpt"):
            lines += ["```", a["excerpt"], "```"]
    if e.get("suggestion"):
        lines += ["suggested replacement:", "```", e["suggestion"], "```"]
    return "\n".join(lines)


def briefing(s: store.Session, batch: list[dict]) -> str:
    """Sent with the first turn of a fresh conversation, so an idle start costs nothing."""
    entries = s.chat()
    waiting = {e["seq"] for e in batch}
    tail = [e for e in entries if e["kind"] != "tier" and e["seq"] not in waiting][-CHAT_TAIL:]
    items = [i for i in s.backlog() if i["status"] not in ("done", "dismissed")]
    parts = ["# Briefing", "", s.context_path.read_text(encoding="utf-8") if s.context_path.exists() else "(no context)", ""]
    if tail:
        parts += ["## Earlier chat", ""]
        for e in tail:
            who = "reviewer" if e.get("role") == "agent" else "user"
            parts.append(f"- #{e['seq']} {who}: {(e.get('text') or '')[:300]}")
        parts.append("")
    parts += ["## Open backlog", ""] + ([f"- {i['id']} [{i['status']}] {i.get('kind')} @{i.get('anchor')}: {i['title']}" for i in items] or ["(empty)"])
    return "\n".join(parts) + "\n\n---\n\n"


class Host:
    def __init__(self, sid: str, model: str, effort: str):
        self.s = store.open_session(sid)
        self.sid, self.model, self.effort = sid, model, effort
        self.events: queue.Queue = queue.Queue()
        self.stopping = False
        self.log = open(self.s.dir / "reviewer.log", "a", buffering=1)
        self.prompt = system_prompt(sid, self.s.pages_dir)
        self.prompt_id = hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()[:16]
        prev = self.s.agent()
        resumable = prev.get("mode") == "host" and prev.get("prompt_id") == self.prompt_id
        self.claude_session = prev.get("claude_session") if resumable else None
        # a resumed conversation would keep its old instructions (pages folder, tools) and act on them
        stale = bool(prev.get("claude_session")) and not resumable
        self.s.set_agent(claude_session=self.claude_session, resumed=bool(self.claude_session),
                         fresh_reason="desk instructions changed" if stale else None)
        self.briefed = bool(self.claude_session)
        unanswered = self.s.unanswered()
        entries = self.s.chat()
        # pick up anything the user asked while no reviewer was attached
        self.cursor = (unanswered[0]["seq"] - 1) if unanswered else (entries[-1]["seq"] if entries else 0)
        self.draft_lock = threading.Lock()
        self.draft: dict | None = None      # the answer being streamed for the current turn
        self.draft_written = 0.0
        self.tool: dict | None = None       # tool_use block being streamed: name + partial input json
        self.proc = self.spawn(resume=self.claude_session)

    # ------------------------------------------------------------ process
    def spawn(self, resume: str | None) -> subprocess.Popen:
        # The one write the reviewer may make: HTML pages in the desk's pages/<sid>/ folder, outside the
        # repository and ~/.claude. Path-scoped allow rules; any other Edit/Write needs approval nobody can give in -p, so it is denied.
        # Page generators run through `review-desk-axi run`, whose OS sandbox confines their writes to pages/ too.
        pages = self.s.pages_dir
        pages.mkdir(parents=True, exist_ok=True)
        cmd = [claude_bin(), "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages",
               "--model", self.model, "--effort", self.effort,
               "--append-system-prompt", self.prompt,
               "--allowedTools", f"Bash({BIN}:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(lavish-axi:*)", "Bash(npx -y lavish-axi:*)",
               "Read", "Grep", "Glob", "Skill", f"Write(/{pages}/**)", f"Edit(/{pages}/**)",
               "--disallowedTools", "NotebookEdit",
               "--settings", json.dumps({"disableAllHooks": True})]
        if resume:
            cmd += ["--resume", resume]
        self.note(f"spawn {' '.join(cmd[:12])} ... resume={resume}")
        env = {**os.environ, "REVIEW_DESK_ROLE": "reviewer"}  # desk commands record pages as opened by the reviewer
        p = subprocess.Popen(cmd, cwd=self.s.meta().get("repo") or None, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=self.log, text=True, bufsize=1)
        threading.Thread(target=self.read, args=(p,), daemon=True).start()
        return p

    def read(self, p: subprocess.Popen) -> None:
        for line in p.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "system" and ev.get("subtype") == "init" and ev.get("session_id"):
                self.claude_session = ev["session_id"]
                self.beat(claude_session=self.claude_session)
            elif ev.get("type") == "stream_event":
                self.on_stream(ev.get("event") or {})
            elif ev.get("type") in ("result", "control_response"):
                self.events.put(ev)
        self.events.put({"type": "eof"})

    # ------------------------------------------------------------ streaming
    def on_stream(self, e: dict) -> None:
        """Keep the current answer's text and a status line; the desk renders them live.
        A status change is written at once: throttling it could drop the finished tool call's label,
        and the desk would show the previous step for as long as the call runs. Streamed tool input
        changes nothing the desk shows, so it writes nothing; only answer text is throttled."""
        with self.draft_lock:
            if self.draft is None:
                return
            kind = e.get("type")
            status = self.draft.get("status")
            if kind == "message_start":
                self.draft["text"] = ""  # only the turn's last message is the answer; earlier text was preamble
            elif kind == "content_block_start":
                block = e.get("content_block") or {}
                if block.get("type") == "tool_use":
                    self.tool = {"name": block.get("name", "tool"), "json": ""}
                    self.draft["status"] = f"using {self.tool['name']}"
                elif block.get("type") == "thinking":
                    self.draft["status"] = "thinking"
                elif block.get("type") == "text":
                    self.draft["status"] = ""
            elif kind == "content_block_delta":
                d = e.get("delta") or {}
                if d.get("type") == "text_delta":
                    self.draft["text"] += d.get("text", "")
                elif d.get("type") == "input_json_delta" and self.tool is not None:
                    self.tool["json"] += d.get("partial_json", "")
                    return
            elif kind == "content_block_stop" and self.tool is not None:
                self.draft["status"] = tool_status(self.tool)
                self.tool = None
            self.write_draft(force=self.draft.get("status") != status)

    def write_draft(self, force: bool = False) -> None:
        if self.draft is None or (not force and time.time() - self.draft_written < 0.08):
            return
        path = self.s.dir / "stream.json"
        tmp = path.with_name(".stream.json.tmp")
        tmp.write_text(json.dumps(self.draft | {"at": store.now()}), encoding="utf-8")
        os.replace(tmp, path)
        self.draft_written = time.time()

    def clear_draft(self) -> None:
        with self.draft_lock:
            self.draft = None
        try:
            (self.s.dir / "stream.json").unlink()
        except FileNotFoundError:
            pass

    def send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def note(self, msg: str) -> None:
        self.log.write(f"{time.strftime('%H:%M:%S')} host: {msg}\n")

    def beat(self, **fields) -> None:
        self.s.set_agent(mode="host", host_pid=os.getpid(), model=self.model, effort=self.effort,
                         timeout=TIMEOUT, cursor=self.cursor, prompt_id=self.prompt_id, **fields)

    # --------------------------------------------------------------- tier
    def set_tier(self, model: str | None, effort: str | None) -> None:
        if model and model != self.model:
            self.send({"type": "control_request", "request_id": f"m{time.time_ns()}", "request": {"subtype": "set_model", "model": model}})
            self.model = model
        if effort and effort != self.effort:
            self.send({"type": "control_request", "request_id": f"e{time.time_ns()}",
                       "request": {"subtype": "apply_flag_settings", "settings": {"effortLevel": effort}}})
            self.effort = effort
        self.note(f"tier -> {self.model}/{self.effort}")
        self.beat(state="waiting", reason=None)

    # --------------------------------------------------------------- turn
    def turn(self, batch: list[dict]) -> None:
        text = ""
        if not self.briefed:
            text, self.briefed = briefing(self.s, batch), True
        text += "New entries (answer each):\n\n" + "\n\n".join(describe(e) for e in batch)
        self.beat(state="thinking", reason=None)
        with self.draft_lock:
            self.draft = {"reply_to": [e["seq"] for e in batch], "text": "", "status": "reading",
                          "model": self.model, "effort": self.effort}
            self.write_draft(force=True)
        self.send({"type": "user", "message": {"role": "user", "content": text}})
        deadline = time.time() + TURN_LIMIT
        result = None
        while time.time() < deadline and result is None:
            try:
                ev = self.events.get(timeout=HEARTBEAT)
            except queue.Empty:
                self.beat(state="thinking")
                continue
            if ev["type"] == "result":
                result = ev
            elif ev["type"] == "eof":
                raise RuntimeError("the Claude process exited mid-turn")
        seqs = [e["seq"] for e in batch]
        if result is None:
            answer = f"Reviewer error: no answer within {TURN_LIMIT // 60} minutes. Ask again, or switch model."
        elif result.get("is_error"):
            answer = f"Reviewer error: {result.get('result') or result.get('subtype') or 'unknown'}"
        else:
            answer = (result.get("result") or "").strip() or "(the reviewer returned no text)"
        self.s.post("msg", "agent", text=answer, reply_to=seqs, model=self.model, effort=self.effort)
        self.clear_draft()  # after the post, so the bubble never blinks out between draft and answer
        self.beat(state="waiting")

    # --------------------------------------------------------------- loop
    def run(self) -> str:
        self.beat(state="waiting", reason=None, started=store.now())
        last_beat = last_activity = time.time()
        while not self.stopping:
            if self.proc.poll() is not None:
                return "crashed"
            if self.s.meta().get("status") == "ended":
                return "end"
            new = [e for e in self.s.chat() if e["seq"] > self.cursor and e.get("role") == "user"]
            batch: list[dict] = []
            for e in new:
                self.cursor = e["seq"]
                if e["kind"] == "tier":
                    if batch:  # answer what came before the switch on the old tier
                        self.turn(batch)
                        batch = []
                    self.set_tier(e.get("model"), e.get("effort"))
                elif e["kind"] == "end":
                    return "end"
                elif e["kind"] in store.USER_KINDS:
                    batch.append(e)
                # execute: the main agent's watcher picks it up; the reviewer keeps its context
            if batch:
                self.turn(batch)
            if new:
                last_activity = time.time()
                self.beat(state="waiting")
            if time.time() - last_beat > HEARTBEAT:
                self.beat(state="waiting")
                last_beat = time.time()
            if time.time() - last_activity > IDLE_LIMIT:
                return "idle"
            time.sleep(0.3)
        return "stopped"

    def shutdown(self, reason: str) -> None:
        self.clear_draft()
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=20)
        except Exception:
            self.proc.kill()
        self.s.set_agent(mode="host", host_pid=None, state="exited", reason=reason, cursor=self.cursor,
                         claude_session=self.claude_session, prompt_id=self.prompt_id, model=self.model, effort=self.effort)
        self.note(f"exit ({reason})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sid")
    ap.add_argument("--model", required=True)
    ap.add_argument("--effort", required=True)
    a = ap.parse_args()
    host = Host(a.sid, a.model, a.effort)

    def stop(*_):
        host.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    reason = "crashed"
    try:
        reason = host.run()
    except Exception as err:  # keep the session consistent even when the host itself fails
        host.note(f"error: {err!r}")
        reason = "crashed"
    finally:
        host.shutdown(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
