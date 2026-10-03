---
name: review-desk
description: >-
  Open a Review Desk: a fast, local code review editor (changed-file tree, diff and file views,
  line selection, chat panel, backlog) staffed by one long-lived reviewer session whose model and
  effort the user picks and can switch from the chat in place, without a respawn. The reviewer answers questions and never
  edits code; anything to implement goes into a durable backlog that reaches the main agent when
  the user presses Execute, the reviewer exits, or a hook finds it undelivered. HTML pages (a
  reviewer's Lavish page, a skill's --html output, an implementation summary) open as read-only
  editor tabs that the agents and the user open and close. Used by
  /implementation-summary after every change; also use it directly when the user wants to
  "review the changes with me", "open the review desk", "let me ask questions about this diff",
  or "resume the review". Prefer it over Lavish for reviewing code changes.
---

# Review Desk

A Review Desk session is a directory of append-only files (see `scripts/store.py`).
The browser, the reviewer subagent and the main agent all read and write those files, so nothing is lost when a process dies.
Agents drive it with `review-desk-axi`, an [AXI](https://axi.md) CLI: run it with no arguments for this repo's sessions and any undelivered backlog, and follow the `help[]` block every command ends with.
Output is TOON; errors print `error:` on stdout (exit 1, usage 2); every write is safe to retry.

## Who does what

| Role | Does | Never |
|---|---|---|
| User (browser) | asks, flags lines, suggests edits, picks the reviewer's model/effort, presses Execute or End | |
| Reviewer (one long-lived Claude session) | answers in the chat, logs real issues to the backlog, runs skills; switches model and effort in place | edits code |
| Main agent | opens the desk, pins files, spawns reviewers, implements backlog items, acks and closes them | answers chat questions while a reviewer is live |

## Setup (once per machine)

`review-desk-axi setup` links the binary into `/opt/homebrew/bin`, writes the reviewer agents (`~/.claude/agents/review-desk-{low,medium,high}.md`, loaded at the next session start) and installs the session hooks and permission in `~/.claude/settings.json`.
It is idempotent and reports each change; run `setup hooks`, `setup bin` or `setup agents` for one part.
If the binary is not on PATH yet (a fresh install, for example from `npx skills add`), call `<this skill's base directory>/bin/review-desk-axi setup` first.

## Open a session

From `/implementation-summary`, follow its step 6 (it passes the page, summary and review data).
Directly, on any git repository:

```bash
review-desk-axi open --title "<what is under review>" --repo . [--base main] [--paths src/] [--notes notes.md]
review-desk-axi add <sid> src/x.py:40-72 tests/test_x.py --note "start here"   # pin files into the editor
```

`open` prints the session id, the URL (it opens the browser once) and the last model/effort the user chose.
`add` is how you put files in front of the user: each ref appears under "pinned by agent" and opens as a tab; `--focus` jumps to it.

## Show pages

Any HTML page can open as a read-only tab in the editor, beside the code: a reviewer's Lavish-style page, a skill's `--html` output (`/analyze-code --html`), an earlier implementation summary.

```bash
review-desk-axi page <sid> open <file.html> --title "Call graph" [--background]   # P1, P2, ...; reopening reuses the id
review-desk-axi page <sid> close P1
review-desk-axi page <sid> list                                                  # open and closed, and who opened them
review-desk-axi run <sid> -- python3 make_page.py   # a page generator; its writes are confined to ~/.review-desk/pages/<sid>/
```

- **Who opens and closes:** the reviewer, the main agent and the user all can.
  The user closes a tab with its ×, reopens one from the "pages" list in the files panel, opens any `.html` path named in the chat by clicking it, and renders an HTML file from the repository with "render as page".
  Open and closed state is durable (`pages.jsonl`), so the agents see the user's choices and a browser reload keeps them.
- **Live:** editing the file reloads its open tab.
- **Sandboxed:** pages render in a sandboxed iframe with an opaque origin, also enforced by a CSP header.
  Their scripts run, but cannot reach the desk's token, API or storage (`localStorage` throws).
  Relative assets next to the page load; dotfiles and paths outside the page's folder do not.
- **Links:** `vscode://file/<abs path>:<line>` links (the summary's and analyze-code's code links) open in the desk's editor; links to other `.html` files open as pages.
- **Generators:** `run` uses `sandbox-exec` on macOS and `bwrap` on Linux, and refuses to run without either.
  Its working directory is the pages folder, and it reports which pages it wrote.

## Search

**Opening it:** Cmd/Ctrl+Shift+F opens a repository search from anywhere in the desk, including the chat and page tabs.
It follows the biggrep VS Code extension.

**Engine:** the server streams `rg --json` matches (`scripts/search.py`, falling back to `git grep`), capped at 2000 results.
- **Scope:** the change set by default, or the whole repository (Alt+S).
  The repository scope sees what git sees, hidden files included.
- **Toggles:** match case, whole word and regex (Alt+C/W/R).
- **Path filter:** `src/` is a substring, `*.py` is a glob, and `!test` or `-test` excludes.

**Results:** grouped by file and syntax-highlighted, with matches marked.
Keys:
- ↓ enters the list, and j/k, PgUp/PgDn and Home/End move.
- ←/→ fold a file.
- Enter opens the match and carries the query into the file's find bar (Cmd+F).
- Space previews, Cmd+C copies `path:line:text`, and Esc closes.

## Start a reviewer

The reviewer is one long-lived Claude session per review (`scripts/reviewer_host.py`), not a subagent.
When the user picks another model or effort in the chat, the same session switches in place (`set_model`, `effortLevel`): no respawn, and the conversation is kept.

1. Ask the user which reviewer to run with one AskUserQuestion holding two questions:
   - **Model**: Haiku (fast, cheap, easy questions), Sonnet, Opus, Fable. Put `last_tier` from `open` first, labelled "(Recommended)".
   - **Effort**: low, medium, high, with the last choice first.
   Add a "No reviewer" option to the model question for a desk without chat.
2. `review-desk-axi reviewer <sid> start --model <m> --effort <e>`. It starts the host in the background (log: `<session dir>/reviewer.log`) and resumes the previous conversation only if the reviewer's instructions are unchanged; after a desk upgrade it starts fresh and briefs the chat (`resumes: fresh conversation (desk instructions changed)`).
3. Run `review-desk-axi watch <sid>` with Bash `run_in_background: true` and `timeout: 7200000`. It blocks until the main agent has work (Execute, End, or the reviewer exiting); its completion wakes you while you are idle. While you work on something else, your hooks deliver Execute and End instead (see "Delivery").
4. End your turn with the desk URL alone on its last line. Do not poll.

`reviewer <sid> status` and `reviewer <sid> stop` manage the host.
The reviewer never writes to the repository, with one exception enforced by permission rules: it may write HTML pages into `~/.review-desk/pages/<sid>/` (outside the repository and `~/.claude`, where Claude Code refuses writes; a call graph, a comparison, a skill's `--html` output) and show them as editor tabs (see "Show pages").
If `claude` cannot run headless on this machine, fall back to a background subagent: `subagent_type: "review-desk-<effort>"`, `model: "<m>"`, prompt `Review Desk session SID=<sid>. You are model <m>.` (or `general-purpose` with `review-desk-axi prompt <sid> --model <m> --effort <e>` before the agent types load). A subagent cannot change model, so it hands over (`HANDOFF tier=M/E`) and you respawn the new tier.

## When the reviewer hands back

`watch` prints one `event:` line and the `handoff` output (every backlog item you have not accepted yet).
Track every listed item in your task list and run `review-desk-axi backlog <sid> ack <ids>`; that stops the prompt hook from re-sending them.

- `EXECUTE`: implement exactly the `execute_requested` items.
  For a `suggested-edit` item, apply its patch (check it still applies; if the code moved, apply the intent).
  For a `question` item, ask the user.
  After each item, run `backlog <sid> done <id> --note "<what changed, path:line>"`; drop one with `dismiss --note "<why>"`.
  Then refresh the desk (`/implementation-summary` "Backlog follow-up" rebuilds the page and diffs; otherwise `review-desk-axi reload <sid>` recomputes them from git) and start `watch` again in the background. The reviewer is still running with its context; do not restart it.
- `END`: report the open items in one or two lines and leave them in your task list.
- `REVIEWER_EXITED (idle|crashed|stopped)`: report the open items; if the user is still reviewing, `reviewer <sid> start` resumes the same conversation (or starts fresh and briefs it after a desk upgrade), then `watch` again.
- `TIMEOUT`: start `watch` again; nothing happened.

From a subagent reviewer (fallback), the final message carries the keyword instead: `HANDOFF tier=M/E` (spawn `review-desk-E` with model M, no question), `EXECUTE`, `IDLE`, `END`, handled as above.

The SessionStart hook prints the home view; the UserPromptSubmit hook injects backlog a dead reviewer never delivered, once per item per session.
When either lists `undelivered` items, run `handoff <sid>` and handle the result as above.

## Delivery

An Execute or End must reach the main agent even while it is busy with another task or watching several desks, so delivery does not rest on `watch` alone:
- **Ownership.** The Claude session that drives a desk (`open`, `add`, `page`, `watch`, `reload`, `backlog`, ...) owns it; the PostToolUse hook records this, and the latest driver wins. Reviewer commands (`attach`, `wait`, `reply`, `chat`, `detach`) never claim.
- **PostToolUse hook** (`hook tool`): after every tool call of the owning session, a pending Execute or End is injected into its context with the items, once. Implement them as in "When the reviewer hands back" as soon as your current step allows.
- **Stop hook** (`hook stop`): an Execute or End that arrives while you write a final reply blocks the stop with the same text, once.
- **UserPromptSubmit hook**: delivers it with the user's next message, for a session that was idle without a watch.
- **watch**: still wakes an idle session. It writes a heartbeat, and it skips an event a hook already delivered (one shared cursor, `main_cursor`).

The desk shows where the latest Execute stands under its buttons: delivered (and how), waking the agent, queued behind a busy agent, or not delivered because no agent is listening, with the `handoff` command to paste into any agent.
Hooks exist only in Claude Code; a desk driven from another harness (Codex) gets Execute through `watch` while that harness can be woken, otherwise the desk says nobody is listening.
`setup hooks` installs all four hooks.

## Without a reviewer

On Codex, or when the user picked "No reviewer", the main agent answers in the desk itself with the same loop as a reviewer: `review-desk-axi attach <sid> --main --model <your model name> --effort <e>`, then `wait` / `reply` / `backlog add` (see `agents/reviewer.md.tmpl`).
`--main` keeps the chat's model/effort chips from triggering a handover.
Ask the model/effort question in plain text where AskUserQuestion does not exist.

## Files

- `bin/review-desk-axi` - the CLI entry (`setup bin` links it onto PATH); `scripts/review_desk.py` implements it, `scripts/axi.py` holds the TOON, help and error formatting.
- `scripts/reviewer_host.py` - the reviewer: one `claude -p --input-format stream-json` session per review, fed from the chat files, with in-place model and effort switches; prompt in `agents/host-reviewer.md`.
- `scripts/store.py` - session files, fsync'd appends under an flock, backlog fold, page open/close fold, reviewer presence.
- `scripts/server.py` - loopback server for the editor (`127.0.0.1:4388`, `REVIEW_DESK_PORT`); token-gated, refuses cross-origin POSTs, exits after 30 idle minutes.
  Pages are served from `/s/<sid>/view/<key>/<page id>/<file>` with a separate read-only key.
- `scripts/gitdiff.py` - review data from plain `git diff` when no `manifest.json` exists.
- `scripts/search.py` - the Cmd+Shift+F engine: ripgrep (or git grep) runs, biggrep's path filter and previews, streamed records.
- `scripts/install_agents.py` - writes the reviewer agents from `agents/reviewer.md.tmpl`; rerun after editing the template (`--check` reports drift).
- `assets/desk.{html,css,js}` - the editor template; no build step.
- `evals/` - eight task evals for both roles plus trigger queries, with a harness that seeds sessions, plays the user and grades from the session files (see `evals/README.md`).
- `tests/` - `python3 -m unittest discover -s tests` from this directory: real server, real CLI, AXI output contract, kill -9 durability, token and origin checks.
