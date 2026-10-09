#!/usr/bin/env python3
"""Hooks that make implementation-summary run at the end of every turn that changed files.

  activate.py prompt      UserPromptSubmit: snapshot the session repo (HEAD and a fingerprint of its
                          tracked changes) so the stop hook can see work done through Bash.
  activate.py post-edit   PostToolUse(Edit|Write|MultiEdit|NotebookEdit): once per turn, remind the
                          model to invoke the skill before its final report.
  activate.py post-bash   PostToolUse(Bash): the same reminder after a `git commit`.
  activate.py stop        Stop: if this turn changed files (an edit tool, a `git commit`, or a moved HEAD
                          or changed tracked files in the session repo) and never invoked the skill, block
                          the stop and send the model back to run it. stop_hook_active prevents loops.

The hooks also remember where the work began in each repository it touched (the base: HEAD when
the work started) until the skill runs, and every reminder names it with the commit count since.
Without it a summary on main diffs against HEAD and silently leaves out everything committed, and
with two or more commits the skill writes one summary per commit.

A turn starts at the last real user prompt in the transcript. Edits to scratch locations
(memory, plans, temp dirs outside the session cwd) do not count. Any parsing failure exits 0 so a hook bug never blocks work.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SKILL = "implementation-summary"
HOME = str(Path.home())
# Claude's own bookkeeping never counts as implementation work.
ALWAYS_IGNORED = (f"{HOME}/.claude/projects/", f"{HOME}/.claude/plans/")  # auto-memory, plans
# Scratch locations count only when the session itself works there (a project in /tmp).
TEMP_ROOTS = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/", tempfile.gettempdir() + "/")
STATE_DIR = Path(tempfile.gettempdir()) / "implementation-summary-hook"


def turn_entries(transcript: str) -> tuple[str, list[dict]]:
    """Return (turn id, transcript entries since the last real user prompt)."""
    entries = []
    with open(transcript, encoding="utf-8") as fh:
        for line in fh:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    start, turn_id = 0, "start"
    # Prefer the explicit marker: task notifications, subagent hand-backs and skill expansions
    # arrive as user entries too, but only a human prompt starts a turn.
    if any("origin" in e for e in entries):
        for i, e in enumerate(entries):
            if e.get("type") == "user" and (e.get("origin") or {}).get("kind") == "human":
                start, turn_id = i, e.get("uuid", str(i))
        return turn_id, entries[start:]
    for i, e in enumerate(entries):
        if e.get("type") != "user" or e.get("isMeta"):
            continue
        content = e.get("message", {}).get("content")
        if isinstance(content, str) or (
            isinstance(content, list)
            and any(b.get("type") == "text" for b in content)
            and not any(b.get("type") == "tool_result" for b in content)
        ):
            start, turn_id = i, e.get("uuid", str(i))
    return turn_id, entries[start:]


def tool_uses(entries: list[dict]):
    for e in entries:
        if e.get("type") != "assistant":
            continue
        for b in e.get("message", {}).get("content", []) or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                yield b.get("name"), b.get("input", {}) or {}


def counts_as_change(name: str, inp: dict, cwd: str) -> bool:
    if name not in EDIT_TOOLS:
        return False
    path = os.path.realpath(inp.get("file_path") or inp.get("notebook_path") or "")
    if path.startswith(ALWAYS_IGNORED):
        return False
    if path.startswith(f"{HOME}/.claude/skills/") and "-workspace/" in path:
        return False  # skill-creator eval workspaces hold run outputs, not implementation work
    if path.startswith(TEMP_ROOTS):
        return path.startswith(os.path.realpath(cwd) + "/") if cwd else False
    return True


def skill_invoked(uses, entries=()) -> bool:
    """Via the Skill tool, or typed by the user as /implementation-summary (a command expansion)."""
    if any(n == "Skill" and SKILL in str(i.get("skill", "")) for n, i in uses):
        return True
    tag = f"<command-name>/{SKILL}</command-name>"
    return any(e.get("type") == "user" and tag in json.dumps(e.get("message", {}).get("content", "")) for e in entries)


# ------------------------------------------------------------- git state
def git(d: str, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", d, *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def repo_of(path: str) -> str | None:
    """The repository root holding path (a file that may not exist yet, or a directory)."""
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    return git(d, "rev-parse", "--show-toplevel") if d else None


def fingerprint(repo: str) -> str | None:
    """HEAD plus every tracked change (staged or not) with its size and mtime: moves whenever tracked
    work changes, through any tool. Untracked files are left out so tool output does not count."""
    head = git(repo, "rev-parse", "HEAD")
    if head is None:
        return None
    h = hashlib.sha1(head.encode())
    for rel in sorted((git(repo, "diff", "--name-only", "-z", "HEAD") or "").split("\0")):
        if rel:
            try:
                st = os.stat(os.path.join(repo, rel))
                h.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\0".encode())
            except OSError:
                h.update(f"{rel}\0gone\0".encode())
    return h.hexdigest()


def commit_dirs(command: str, cwd: str) -> list[str]:
    """Directories that `git commit` runs in within a Bash command (`cd x && ...`, `git -C x commit`)."""
    here, out = cwd, []
    for seg in re.split(r"&&|\|\||;|\n", command):
        try:
            words = shlex.split(seg)
        except ValueError:
            words = seg.split()
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):  # GIT_INDEX_FILE=... git commit
            words = words[1:]
        if len(words) > 1 and words[0] == "cd":
            here = os.path.join(here, os.path.expanduser(words[1]))
            continue
        if not words or words[0] != "git":
            continue
        i, where = 1, here
        while i < len(words) and words[i].startswith("-"):
            if words[i] == "-C" and i + 1 < len(words):
                where = os.path.join(where, os.path.expanduser(words[i + 1]))
            i += 2 if words[i] in ("-C", "-c") else 1
        if i < len(words) and words[i] == "commit":
            out.append(where)
    return out


def bash_commits(uses, cwd: str) -> list[str]:
    return [d for n, i in uses if n == "Bash" for d in commit_dirs(str(i.get("command", "")), cwd)]


# ----------------------------------------------------------------- state
# <session>.json: {"bases": {repo: sha where the work began}, "turn": {"repo", "head", "fp"} at the last prompt}
def load(session: str) -> dict:
    try:
        return json.loads((STATE_DIR / f"{session}.json").read_text())
    except (OSError, ValueError):
        return {}


def save(session: str, state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    tmp = STATE_DIR / f".{session}.json.tmp"
    tmp.write_text(json.dumps(state))
    os.replace(tmp, STATE_DIR / f"{session}.json")


def note_base(state: dict, repo: str | None, fallback: str | None) -> None:
    """Remember where the work in repo began, once: HEAD at the start of this turn when it is the
    session repo, otherwise `fallback` (HEAD before the first edit, or the parent of a fresh commit)."""
    if not repo or repo in state.setdefault("bases", {}):
        return
    turn = state.get("turn") or {}
    base = turn.get("head") if turn.get("repo") == repo else fallback
    if base:
        state["bases"][repo] = base


def hints(state: dict) -> str:
    out = []
    for repo, base in (state.get("bases") or {}).items():
        n = git(repo, "rev-list", "--count", "--no-merges", f"{base}..HEAD")
        if n is None:
            continue
        dirty = bool(git(repo, "status", "--porcelain"))
        parts = int(n) + dirty
        what = f"{n} commit{'s' if n != '1' else ''} since" + (", plus uncommitted changes" if dirty else "")
        mode = " That is two or more parts: write one independent summary per commit (per-commit mode)." if parts >= 2 else ""
        out.append(f" Work in {repo} began at {base[:10]} ({what}): run collect_changes.py --repo {repo} "
                   f"--base {base}.{mode}")
    return "".join(out)


# ----------------------------------------------------------------- hooks
def remind(data: dict, state: dict) -> None:
    """Once per turn, before the final report: run the skill, from the base the work began at."""
    turn_id, entries = turn_entries(data["transcript_path"])
    if skill_invoked(list(tool_uses(entries)), entries):
        return
    marker = STATE_DIR / f"{data.get('session_id', 'x')}.turn"
    if marker.exists() and marker.read_text() == turn_id:
        return  # already reminded this turn
    marker.write_text(turn_id)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": f"Files were changed. When the implementation is complete, invoke the "
                             f"{SKILL} skill (Skill tool) and write the final report with it." + hints(state),
    }}))


def prompt(data: dict) -> None:
    session, cwd = data.get("session_id", "x"), data.get("cwd") or os.getcwd()
    state = load(session)
    repo = repo_of(cwd)
    state["turn"] = {"repo": repo, "head": git(repo, "rev-parse", "HEAD"), "fp": fingerprint(repo)} if repo else {}
    save(session, state)


def post_edit(data: dict) -> None:
    inp = data.get("tool_input", {}) or {}
    if not counts_as_change(data.get("tool_name", ""), inp, data.get("cwd", "")):
        return
    session = data.get("session_id", "x")
    state = load(session)
    repo = repo_of(os.path.realpath(inp.get("file_path") or inp.get("notebook_path") or ""))
    note_base(state, repo, git(repo, "rev-parse", "HEAD") if repo else None)
    save(session, state)
    remind(data, state)


def post_bash(data: dict) -> None:
    cwd = data.get("cwd") or os.getcwd()
    dirs = commit_dirs(str((data.get("tool_input") or {}).get("command", "")), cwd)
    if not dirs:
        return
    session = data.get("session_id", "x")
    state = load(session)
    for d in dirs:
        repo = repo_of(d)
        fresh = repo and time.time() - int(git(repo, "log", "-1", "--format=%ct") or 0) < 120
        note_base(state, repo, git(repo, "rev-parse", "--verify", "--quiet", "HEAD^") if fresh else None)
    save(session, state)
    remind(data, state)


def stop(data: dict) -> None:
    if data.get("stop_hook_active"):
        return
    session, cwd = data.get("session_id", "x"), data.get("cwd", "")
    _, entries = turn_entries(data["transcript_path"])
    uses = list(tool_uses(entries))
    state = load(session)
    if skill_invoked(uses, entries):
        state.pop("bases", None)  # summarized: the next piece of work starts from wherever HEAD is then
        save(session, state)
        return
    changed = sorted({i.get("file_path") or i.get("notebook_path") for n, i in uses if counts_as_change(n, i, cwd)})
    commits = bash_commits(uses, cwd or os.getcwd())
    turn = state.get("turn") or {}
    moved = bool(turn.get("repo") and turn.get("fp") and fingerprint(turn["repo"]) != turn["fp"])
    if not (changed or commits or moved):
        return
    if moved:
        note_base(state, turn["repo"], turn.get("head"))
        save(session, state)
    what = [os.path.basename(p) for p in changed[:5]] + (["..."] if len(changed) > 5 else [])
    if commits:
        what.append(f"{len(commits)} git commit{'s' if len(commits) > 1 else ''}")
    if moved and not changed:
        what.append("tracked files changed through Bash")
    print(json.dumps({
        "decision": "block",
        "reason": f"This turn changed files ({', '.join(what)}) but did not run the {SKILL} skill. "
                  f"Invoke it now with the Skill tool and give its summary as the final report." + hints(state),
    }))


def main() -> int:
    try:
        data = json.load(sys.stdin)
        {"prompt": prompt, "post-edit": post_edit, "post-bash": post_bash, "stop": stop}[sys.argv[1]](data)
    except Exception:
        pass  # never block work because of a hook bug
    return 0


if __name__ == "__main__":
    sys.exit(main())
