"""AXI output helpers (https://axi.md): TOON values and tables, truncation hints, help blocks, errors.

Every command prints through these, so the whole CLI shares one format:

  key: value
  name[N]{f1,f2}:
    v1,v2
  help[N]:
    Run `review-desk-axi <cmd> <sid>`

Errors go to stdout as `error: <message>` plus a help block, with exit code 1 (2 for usage).
"""

from __future__ import annotations

import re
import sys

BIN = "review-desk-axi"
TRUNCATE = 400
_NUMBERISH = re.compile(r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$")
_SPECIAL = set(',:"\\[]{}\n\r\t#')


class DeskError(Exception):
    """A failure to report as structured output; `help` holds next-step command templates."""

    def __init__(self, message: str, help: list[str] | None = None, code: int = 1):
        super().__init__(message)
        self.help = help or []
        self.code = code


def value(v) -> str:
    """One TOON scalar: bare when unambiguous, otherwise a double-quoted, escaped string."""
    if v is None or v == "":
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        v = " ".join(str(x) for x in v)
    s = str(v)
    if (s != s.strip() or any(c in _SPECIAL for c in s) or s in ("true", "false", "null")
            or _NUMBERISH.match(s) or s.startswith("-")):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'
    return s


def clip(text: str | None, limit: int = TRUNCATE, hint: str | None = None) -> str:
    """Cut long text and say how to get all of it."""
    text = text or ""
    if len(text) <= limit:
        return text
    more = f" - use {hint}" if hint else ""
    return f"{text[:limit].rstrip()}… (truncated, {len(text)} chars total{more})"


def kv(key: str, v) -> None:
    print(f"{key}: {value(v)}")


def table(name: str, rows: list[dict], fields: list[str], empty: str = "none") -> None:
    """name[N]{fields}: rows, or an explicit `name[0]: none` so empty is never ambiguous."""
    if not rows:
        print(f"{name}[0]: {empty}")
        return
    print(f"{name}[{len(rows)}]{{{','.join(fields)}}}:")
    for r in rows:
        print("  " + ",".join(value(r.get(f)) for f in fields))


def block(name: str, text: str) -> None:
    """A long Markdown text (a briefing) printed as indented lines under its key."""
    lines = text.rstrip("\n").splitlines()
    print(f"{name}[{len(lines)} lines]:")
    for line in lines:
        print("  " + line if line else "")


def help_block(lines: list[str]) -> None:
    lines = [l for l in lines if l]
    if lines:
        print(f"help[{len(lines)}]:")
        for l in lines:
            print(f"  {l}")


def run(cmd: str) -> str:
    return f"Run `{(BIN + ' ' + cmd).strip()}`"


def fail(err: DeskError) -> int:
    print(f"error: {value(str(err))}")
    help_block(err.help)
    sys.stdout.flush()
    return err.code
