---
name: implementation-summary
description: >-
  Write the closing summary after implementing any code change (feature, features, fix, refactor):
  a narrative, human-readable walkthrough of what changed and why, structured with ASCII
  particle-art scenes (streams through the new code path, sparks on what is new, dust where code was
  removed), real code excerpts, and clickable path:line links, plus an animated HTML companion page
  where the same scenes come alive. Use this automatically, without being asked, every time you finish
  implementing anything - a feature, several features, a bug fix, a refactor, a rename, even a small
  change - and are about to write the final report of the turn. It applies whenever files were
  changed, scaling down to a compact form for small edits. Also use it when the user asks to
  "summarize what you built", "walk me through the changes", "recap", "wrap up", or "what did you
  change". Skip it only when no code changed (answers, research, reviews) and for mid-task updates.
---

# Implementation summary

The reader was not watching you work.
They want to understand the change well enough to trust it, review it, and pick it up tomorrow.
A checklist of ticked tasks tells them what you were busy with; a good summary shows them the *shape* of the change: what the system does now, which path through the code makes that happen, which few lines carry the idea, what you decided on their behalf, and what is still unproven.

ASCII particle art is how this skill gets room to show shape instead of listing items.
A small text frame can draw a flow, a fork, a before/after, a lifecycle, or what dissolved, and the same frame is reused on an animated companion page where streams run along the paths, new pieces throw sparks and removed code drifts away as dust.
Treat the art as structure that carries meaning, not ornament.

## Workflow

1. **Gather ground truth.** Run `python3 <skill-dir>/scripts/collect_changes.py --paths <files and dirs you touched>` (add `--base <ref>` if your work spans commits on a branch).
It covers committed, staged, unstaged and untracked work, and prints a per-directory rollup, then every file with its status, origin (`committed`, `staged`, `unstaged`, `untracked`, or several) and notes (rename source, binary, mode, symlink), then every hunk as `#n path:range` with its enclosing symbol.
When the reader will commit or review next, say which work is still uncommitted; the origin labels give it directly.
Those ranges anchor your links and excerpts; `#n` is the hunk number the page's `data-hunks` uses.
Scope with `--paths` whenever the tree has changes that are not yours, so you never narrate someone else's work; use the same scope for every later command.
Then run it again with `--format inventory`: that prints the "Every change" block you paste verbatim into the terminal summary.
Also collect, from this session: the verification you actually ran and its real output, the decisions you made that the user did not dictate, and anything you found but did not fix.
When recapping work you did not watch (an earlier session, a branch handed to you), take decisions from commit messages, code comments and handoff docs, attribute them ("per `HANDOFF.md`"), and say in Proof that you ran nothing unless you did.
2. **Find the story** (next section) before writing a word of output.
3. **Draw the scenes.** Read `references/particle-grammar.md`; it defines the glyph vocabulary the page engine understands and gives patterns for each kind of change.
4. **Write the terminal summary** in the shape below, in the language of `LANGUAGE.md`, and save it to a scratch file (`$SCRATCH/summary.md`) exactly as you will reply.
5. **Build the companion page.** Read `references/page.md`, write the body fragment, run `scripts/build_page.py --summary $SCRATCH/summary.md --review-data`.
The page reuses your scenes verbatim and pulls code excerpts from disk by line range.
Every build persists the page, the terminal summary, the body, the change model and the inventory into `~/.claude/implementation-summaries/<project>/<milestone>/<component>/` (project = repository, milestone = branch, component = the most-touched area; override with `--project`, `--milestone`, `--component`) and refreshes that project's `INDEX.md`.
`--review-data` also writes `<stem>.review/` next to the page: the Review Desk's file list and diffs, from the same change model as the page.
When the project is not in git and you built from a scratch git copy, pass `--source <real directory>` so links and naming come from the real project.
6. **Open the Review Desk** (the `review-desk` skill; `RD` = `review-desk-axi`, an AXI CLI whose `help[]` blocks name each next command) instead of Lavish:
   - Write `$SCRATCH/review-notes.md`: what only this session knows and the reviewer will need (rejected alternatives, why a decision went the way it did, checks that were flaky or skipped, files you read but did not change). At most 40 lines; the summary already covers the rest.
   - `RD open --dir <the .review folder the build printed> --title "<feature name>" --page <the .html it printed> --summary $SCRATCH/summary.md --notes $SCRATCH/review-notes.md --repo <repo>`
   - `RD add <sid> <the 1-3 code moments as path:start-end> --note "<one line each>"` so the files the reviewer should read first open as tabs.
   - Ask which reviewer to run and start it, following "Start a reviewer" in the review-desk skill (one AskUserQuestion: model and effort, last choice first, plus "No reviewer"; then `RD reviewer <sid> start` and `RD watch <sid>` in the background).
7. **End the reply with the desk URL** on its own line (`↗ <url>`), then stop. Do not poll; the background `watch` wakes you.

Steps 4 and 5 tell the same story; write the terminal version first because it forces the tight version of every sentence, then expand on the page.

## When the reviewer hands back

Follow "When the reviewer hands back" in the review-desk skill: track and `ack` every item, then branch on the keyword.
For `EXECUTE`, implement the items, then run this skill again in its **Backlog follow-up** form (Scaling table) and rebuild into the same session: `build_page.py ... --review-data <existing .review folder>`, then `RD reload <sid> --page <new .html>`, and start `RD watch <sid>` again in the background (the reviewer keeps running).

## Find the story

Answer these for yourself first.
The summary is the answers, arranged for a reader.

- **What can the system or user do now that they could not before?** Say it in behavior terms ("rows with quoted commas are kept instead of silently dropped"), not activity terms ("updated the parser").
This becomes the thesis line.
- **What path does that behavior take through the code?** Entry point to effect, naming the new or changed stops.
This becomes the main scene.
- **Which one to three places in the code carry the idea?** Not every changed file: the lines a reviewer should read first.
These become the code moments.
- **What did you decide that the reader might not expect?** Tradeoffs, rejected alternatives, deviations from the request, things you deliberately did not do.
- **What is proven and what is not?** Exactly what ran, with the real result.
- **What is left?** Follow-ups, risks, questions only the user can answer.

If several features shipped, answer per feature, then ask how they relate: shared data, one enabling another, or simply batched.
That relation is the constellation scene.

**Account for everything.** Before writing, walk the "By directory" rollup and place every group: in a story, in the scene, or in one "Also changed" sentence (tests, docs, config, generated files).
A large change is where summaries silently drop work; the inventory lists every file, but the prose must not leave a whole area unexplained.

## Terminal summary shape

The terminal summary is the dense version; the page holds the detail.
Budgets (prose only, excluding art, code and the collapsed "Every change" block): about 15 lines for one feature, about 30 for several, about 45 for a large change spanning many areas.

````markdown
```text
     ·      ˙        ✦          ·       ˙
  ˙     <feature name>                       ·
     ·    <one-line thesis: the behavior change>   ˙
        ˙        ·          ✧       ·
```

<One sentence: the behavior change.>

### The shape of it

```text
  <main scene: pipeline / fork / before-after / lifecycle / constellation>
```
<caption line: each node that maps to code, as `path:line`>

<Up to 3 sentences walking the scene, each stop linked: `src/parse.py:41`.>

### <Story heading per feature, or per key part of one feature>

<Up to 4 sentences: behavior, mechanism, decision. Each claim linked.>

<One sentence: where this code sits in the flow.>
<details>
<summary>`src/parse.py:38-61` - split_quoted keeps commas inside quotes</summary>

```python
...real lines from the file...
```

</details>

<One "Also changed" sentence for groups no story covers, each linked.>

### Every change

<the `collect_changes.py --format inventory` output, pasted verbatim: a collapsed <details> listing every file>

### Proof

- `<command>` - <result, verbatim counts>
- unverified: <what was not run, and why>

### Loose threads

- <Up to 3 items, one line each: not done, risky, or needs the user's decision.>

↗ <review desk URL>
````

### Code moments

Code earns a place when it is the clearest way to show the idea: the guard that fixes the bug, the new type's shape, the line where the two features meet.
Pick one to three per feature; a summary that pastes every hunk is a diff with extra steps.
Completeness is the inventory's job (terminal block and the page's "Every change" section), so stories can stay selective without hiding anything.

- Excerpt real lines from the file as it is now (read it after your final edit), 5 to 25 lines, eliding with `// ...` or `# ...` where it helps.
- Directly above each excerpt write one always-visible sentence placing the code in the flow.
Then wrap the code in a collapsed `<details>` block whose summary is `` `path:start-end` - what this is ``, with blank lines around the fenced code so the Markdown renders.
If the user's own instructions prescribe a different snippet format, theirs wins.
- For a surgical fix, a small ```` ```diff ```` block often says more than the new code alone.

### Links

Every claim about code gets a clickable `path:line` (or `path:start-end`), repo-relative, in backticks or bare.
Take line numbers from `collect_changes.py` output or from reading the file after your last edit, never from memory: an edit earlier in the session shifts everything below it, and a link that lands on the wrong line costs more trust than no link.
Link a symbol at its definition line; link a behavior at the line that decides it.

## Writing well

- **Read `LANGUAGE.md` before writing a word.** It is the language protocol for both the terminal and the page: headings that state a guarantee, risk then guard, plain sentences with an actor, and glossary cards for project terms instead of inline jargon.
- **Precise, not compressed.** Name the exact symbol, status code, type, flag or count (`429` with `Retry-After`, `csv.reader`, `9 passed`) instead of describing it.
Cut hedges, intensifiers and filler ("properly", "robust", "note that", "this means", "I also"), and do not restate what the excerpt or scene already shows.
Never cut the words a sentence needs to be understood: an undefined noun or a fragment costs the reader more than a few extra words.
- **Prose carries the narrative; lists only hold genuinely parallel items** (loose threads, checks run).
If you notice three bullets in a row that each start with a past-tense verb, that is the checklist this skill exists to replace; rewrite them as a paragraph about the change.
- **Describe the result, not your process.** "First I read X, then I tried Y" is noise unless the journey itself matters, for example a root cause you only found mid-way.
- **Be honest in Proof.** Exactly what ran and what it printed; failures and skips stated as such; unverified work labelled.
- **Follow the user's style rules** from their instructions (for example, a plain hyphen when they ban em dashes).
- Headings are short and specific to the content ("Rows with quoted commas survive"), not generic ("Changes made").

## Scaling

| Work | Terminal | Page |
|---|---|---|
| One small feature or fix (a few files) | title card, one scene, 1-2 code moments, proof, threads | hero, one story, proof, threads |
| One large feature | title card, main scene, 2-3 stories for its parts | hero, a story per part |
| Several features | title card, constellation, a short story per feature (scene only where it helps) | hero, constellation, a story per feature |
| Large change (dozens of files or more) | as above, plus the rollup decides the stories: one per area that changed behavior, one "Also changed" sentence for the rest | stories plus "Every change"; check the build's coverage line |
| Tiny change (a rename, a one-line fix, a config tweak) | one-line scene, 1-3 sentences with links, one proof line; skip the inventory when the links already name every file | skip the page |
| Backlog follow-up (executing items from a Review Desk) | one line per item: `Bn` - what changed, with links; proof for what you ran; no title card or scene unless an item changed behavior | rebuild into the same `.review` folder and `RD reload`; skip AskUserQuestion; restart `watch` (the reviewer keeps running) |

Every size except tiny includes the "Every change" block in the terminal; the page always renders it.
Every size except tiny opens the Review Desk; a tiny change skips it unless the user asks to review.

## Bundled files

- `scripts/collect_changes.py` - line-accurate map of every changed file and hunk (`--format map|inventory|json`). Run first.
- `scripts/build_page.py` - wraps your body fragment into the animated page; expands `data-src`, `data-diff`, `data-loc` and the "Every change" inventory from disk and git, refuses to build on a stale reference, and prints which changed files the stories cite. `--review-data` writes the Review Desk's manifest and diffs.
- `scripts/changes.py` - the change model both scripts share, so hunk numbers and file lists always agree.
- `scripts/archive.py` - names and writes the `<project>/<milestone>/<component>/` tree every build persists into (`$IMPL_SUMMARY_ROOT` relocates it); `archive.py adopt <pages> [--dry-run]` files pages built before the tree, and `archive.py sweep <fallback tree> --move` merges what a sandboxed build saved inside its repo (for example a repo's `.lavish/impl-summary-*.html`), reading project, branch-at-build-time and component from each page.
- `tests/` - oracle tests against git on a generated ~300-file change set: `python3 -m unittest discover -s tests` from the skill directory; set `IMPL_SUMMARY_TEST_REPO=<repo>` to also check a real working tree.
- `references/particle-grammar.md` - glyph vocabulary, frame rules, scene patterns. Read before drawing.
- `LANGUAGE.md` - the language protocol: how every sentence of the summary reads. Read before step 4.
- `references/page.md` - page building blocks (risk and guard pairs, glossary cards), build and open commands. Read before step 5.
- the `review-desk` skill, installed beside this one - the Review Desk (step 6): editor, reviewer subagents, durable backlog. Its SKILL.md owns the reviewer protocol.
- `assets/` - page shell, styles, the particle engine and the glossary cards (`glossary.js`); used by the build, no need to read.
