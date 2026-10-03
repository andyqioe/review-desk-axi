#!/usr/bin/env python3
"""Hooks that make implementation-summary run at the end of every turn that changed files.

  activate.py post-edit   PostToolUse(Edit|Write|MultiEdit|NotebookEdit): once per turn, remind the
                          model to invoke the skill before its final report.
  activate.py stop        Stop: if this turn changed files and never invoked the skill, block the
                          stop and send the model back to run it. stop_hook_active prevents loops.

A turn starts at the last real user prompt in the transcript. Edits to scratch locations
(memory, plans, temp dirs outside the session cwd) do not count. Any parsing failure exits 0 so a hook bug never blocks work.
"""
import json
import os
import sys
import tempfile
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


def post_edit(data: dict) -> None:
    if not counts_as_change(data.get("tool_name", ""), data.get("tool_input", {}) or {}, data.get("cwd", "")):
        return
    turn_id, entries = turn_entries(data["transcript_path"])
    if skill_invoked(list(tool_uses(entries)), entries):
        return
    STATE_DIR.mkdir(exist_ok=True)
    marker = STATE_DIR / f"{data.get('session_id', 'x')}.turn"
    if marker.exists() and marker.read_text() == turn_id:
        return  # already reminded this turn
    marker.write_text(turn_id)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": f"Files were changed. When the implementation is complete, invoke the "
                             f"{SKILL} skill (Skill tool) and write the final report with it.",
    }}))


def stop(data: dict) -> None:
    if data.get("stop_hook_active"):
        return
    _, entries = turn_entries(data["transcript_path"])
    uses = list(tool_uses(entries))
    changed = sorted({i.get("file_path") or i.get("notebook_path") for n, i in uses if counts_as_change(n, i, data.get("cwd", ""))})
    if not changed or skill_invoked(uses, entries):
        return
    shown = ", ".join(os.path.basename(p) for p in changed[:5]) + (" ..." if len(changed) > 5 else "")
    print(json.dumps({
        "decision": "block",
        "reason": f"This turn changed files ({shown}) but did not run the {SKILL} skill. "
                  f"Invoke it now with the Skill tool and give its summary as the final report.",
    }))


def main() -> int:
    try:
        data = json.load(sys.stdin)
        {"post-edit": post_edit, "stop": stop}[sys.argv[1]](data)
    except Exception:
        pass  # never block work because of a hook bug
    return 0


if __name__ == "__main__":
    sys.exit(main())
