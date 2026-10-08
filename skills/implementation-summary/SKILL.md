---
name: implementation-summary
description: >-
  Write the closing summary after implementing any code change (feature, features, fix, refactor):
  a structured, human-readable walkthrough of what changed and why, built from ASCII particle-art
  scenes (streams through the new code path, sparks on what is new, dust where code was removed),
  labeled bullets, real code excerpts and clickable path:line links, plus an animated HTML companion
  page where the same scenes come alive. When the work spans several commits (stepwise or
  multi-commit implementations), it writes one independent summary per commit and shows each in the
  Review Desk's Story tab for that commit. Use this automatically, without being asked, every time
  you finish implementing anything - a feature, several features, a series of commits, a bug fix,
  a refactor, a rename, even a small change - and are about to write the final report of the turn,
  then ask whether to open a Review Desk. It applies whenever files were changed or commits were made,
  scaling down to a compact form for small edits. Also use it when the user asks to "summarize what
  you built", "walk me through the changes", "summarize each commit", "recap", "wrap up", or "what did
  you change". Skip it only when no code changed (answers, research, reviews) and for mid-task updates.
---

# Implementation summary

The reader was not watching you work.
They want to understand the change well enough to trust it, review it, and pick it up tomorrow.
A checklist of ticked tasks tells them what you were busy with; a good summary shows them the *shape* of the change: what the system does now, which path through the code makes that happen, which lines carry the idea, what you decided on their behalf, and what is still unproven.

ASCII particle art is how this skill shows shape instead of listing items.
A small text frame can draw a flow, a fork, a before/after, a lifecycle, or what dissolved, and the same frame is reused on an animated companion page where streams run along the paths, new pieces throw sparks and removed code drifts away as dust.
Treat the art as structure that carries meaning, not ornament.
Around the art, the text is structured for scanning: short headings, one bold lead sentence per story, then labeled bullets.

## Workflow

1. **Gather ground truth.** Run `python3 <skill-dir>/scripts/collect_changes.py --paths <files and dirs you touched>`, adding `--base <ref>` when your work includes commits.
   - **The base.** The activation hook's reminder names where the work began ("Work in <repo> began at <sha> ... run collect_changes.py --base <sha>"); pass that `--base`.
   Without it, on `main` the base is HEAD and every commit you made is silently left out (the map warns "Base is HEAD").
   On a feature branch the default (merge-base with main) usually covers it.
   - **What it prints.** A per-directory rollup, then every file with its status, origin (`committed`, `staged`, `unstaged`, `untracked`, or several) and notes (rename source, binary, mode, symlink), then every hunk as `#n path:range` with its enclosing symbol.
   Those ranges anchor your links and excerpts; `#n` is the hunk number the page's `data-hunks` uses.
   - **Commits block.** When the work has two or more parts (commits since the base, plus any uncommitted rest), the map opens with `# Commits: ... PER-COMMIT MODE` and lists them: follow "Per-commit mode" below.
   - **Scope.** Use `--paths` whenever the tree has changes that are not yours, so you never narrate someone else's work, and use the same scope for every later command.
   When the reader will commit or review next, say which work is still uncommitted; the origin labels give it directly.
   - **Inventory.** Run it again with `--format inventory`: that prints the "Every change" block you paste verbatim into the terminal summary.
   - **From the session.** Also collect the verification you actually ran and its real output, the decisions you made that the user did not dictate, and anything you found but did not fix.
   When recapping work you did not watch (an earlier session, a branch handed to you), take decisions from commit messages, code comments and handoff docs, attribute them ("per `HANDOFF.md`"), and say in Proof that you ran nothing unless you did.
2. **Find the story** (next section) before writing a word of output.
3. **Draw the scenes.** Read `references/particle-grammar.md`; it defines the glyph vocabulary the page engine understands and gives patterns for each kind of change.
4. **Write the terminal summary** in the shape below, in the language of `LANGUAGE.md`, and save it to a scratch file (`$SCRATCH/summary.md`) exactly as you will reply.
5. **Build the companion page.** Read `references/page.md`, write the body fragment, run `scripts/build_page.py --summary $SCRATCH/summary.md --review-data`.
   - The page reuses your scenes verbatim and pulls code excerpts from disk by line range.
   - Every build persists the page, the terminal summary, the body, the change model and the inventory into `~/.claude/implementation-summaries/<project>/<milestone>/<component>/` (project = repository, milestone = branch, component = the most-touched area; override with `--project`, `--milestone`, `--component`) and refreshes that project's `INDEX.md`.
   - `--review-data` also writes `<stem>.review/` next to the page: the Review Desk's file list and diffs, from the same change model as the page.
   - When the project is not in git and you built from a scratch git copy, pass `--source <real directory>` so links and naming come from the real project.
6. **Ask whether to open a Review Desk, every time.** Do this for every size of change, tiny ones included, and never open a desk unasked.
   - Read the last reviewer choice with `RD prefs` (`RD` = `review-desk-axi`, an AXI CLI whose `help[]` blocks name each next command).
   - Ask one AskUserQuestion holding three questions:
     **Desk**: "Open a Review Desk for this change?" with "Yes (Recommended)" and "No".
     **Model**: Haiku, Sonnet, Opus, Fable, with the last choice first and labelled "(Recommended)", plus "No reviewer".
     **Effort**: low, medium, high, with the last choice first.
   - On "No", skip the rest of this step and step 7: the reply ends with the summary, and the page path stays in the build output for later.
   - On "Yes", open the desk (the `review-desk` skill owns the details) instead of Lavish:
     - Write `$SCRATCH/review-notes.md`: what only this session knows and the reviewer will need (rejected alternatives, why a decision went the way it did, checks that were flaky or skipped, files you read but did not change). At most 40 lines; the summary already covers the rest.
     - `RD open --dir <the .review folder the build printed> --title "<feature name>" --page <the .html it printed> --summary $SCRATCH/summary.md --notes $SCRATCH/review-notes.md --repo <repo>`
     - In per-commit mode, attach each commit's own page: `RD story <sid> set <sha> --page <commit .html> --summary <commit .md>` (and `uncommitted` for the uncommitted part).
     - `RD add <sid> <the code moments as path:start-end> --note "<one line each>"`, up to 10, so the files the reviewer should read first open as tabs.
     - Unless the answer was "No reviewer", `RD prefs --model <m> --effort <e>`, then start the reviewer and its watch as "Start a reviewer" in the review-desk skill says (`RD reviewer <sid> start ...`, then `RD watch <sid>` in the background); that skill's own model and effort question is already answered.
7. **End the reply with the desk URL** on its own line (`↗ <url>`) when a desk was opened, then stop. Do not poll; the background `watch` wakes you.

Steps 4 and 5 tell the same story; write the terminal version first because it forces the tight version of every sentence, then expand on the page.

## Per-commit mode

When the work is two or more commits, or commits plus uncommitted changes, the reader reviews it commit by commit, so each part gets its own independent summary instead of one summary of the squashed diff.

- **One summary per part.** For each commit, oldest first, run the whole workflow (steps 1 to 5) on that commit alone:
  - `collect_changes.py --commit <sha> [--paths ...]` for its map and `--format inventory` for its "Every change" block.
  - Find that commit's own story, scene, code moments, Proof and Loose threads; scale it with the Scaling table by that commit's size, not the whole review's.
  - Write it to `$SCRATCH/summary-<short sha>.md` and build its page with `build_page.py --commit <sha> --summary $SCRATCH/summary-<short sha>.md` (no `--review-data`). Its excerpts and diffs show the files as that commit left them.
  - Links in a commit's summary may carry the commit (`src/x.py:40-42@a1b2c3d`), so the desk opens that commit's lines.
  - The uncommitted part gets the same treatment with `--base HEAD` and `$SCRATCH/summary-uncommitted.md`.
- **Independent means complete.** Each commit's summary stands on its own: a reader who opens only that commit gets the behavior change, the mechanism, the decisions and the proof for it, without "see commit 2".
Proof lists what you ran against that commit (stepwise work usually tests each commit); say plainly when a check only ran on the final state.
- **The overview.** One short summary for the whole review, built with `--review-data` and the same `--base` as step 1, becomes "All changes" in the desk:
  - The title card and one thesis line for the whole review.
  - A "Commits" list: one bullet per part, `` `a1b2c3d` `` subject, then its thesis in one sentence.
  - The whole review's "Every change" block, overall Proof and Loose threads.
  - No stories of its own; the commits carry them.
- **The terminal reply** is the overview, then each part's summary in order under `## Commit n/N - <short sha> <subject>` (or `## Uncommitted`), then the desk URL.
- **The desk** opens on the overview page; step 6 attaches every commit's page with `RD story`, so picking a commit in the desk shows its own story.
- A single commit with nothing uncommitted is one ordinary summary: the Commits block does not appear and nothing changes.

## When the reviewer hands back

Follow "When the reviewer hands back" in the review-desk skill: `ack` only the items the user executed (leave the rest open), then branch on the keyword.
For `EXECUTE`, implement the items and close each with `RD backlog <sid> done <id> --note "<what changed, path:line>"` (or `reopen <id> --note "<what remains>"` when only partly done), then run this skill again in its **Backlog follow-up** form (Scaling table) and rebuild into the same session: `build_page.py ... --review-data <existing .review folder>`, then `RD reload <sid> --page <new .html>`, and start `RD watch <sid>` again in the background (the reviewer keeps running).
When the follow-up adds commits to a per-commit review, give each new commit its own summary and `RD story <sid> set` it.

## Find the story

Answer these for yourself first.
The summary is the answers, arranged for a reader.

- **What can the system or user do now that they could not before?** Say it in behavior terms ("rows with quoted commas are kept instead of silently dropped"), not activity terms ("updated the parser").
This becomes the thesis line.
- **What path does that behavior take through the code?** Entry point to effect, naming the new or changed stops.
This becomes the main scene.
- **Which places in the code carry the idea?** Not every changed file: the lines a reviewer should read first, as many as the change needs, up to 10.
These become the code moments.
- **What did you decide that the reader might not expect?** Tradeoffs, rejected alternatives, deviations from the request, things you deliberately did not do.
- **What is proven and what is not?** Exactly what ran, with the real result.
- **What is left?** Follow-ups, risks, questions only the user can answer.

If several features shipped, answer per feature, then ask how they relate: shared data, one enabling another, or simply batched.
That relation is the constellation scene.

**Account for everything.** Before writing, walk the "By directory" rollup and place every group: in a story, in the scene, or in one "Also changed" bullet (tests, docs, config, generated files).
A large change is where summaries silently drop work; the inventory lists every file, but the text must not leave a whole area unexplained.

## Terminal summary shape

The terminal summary is the dense version; the page holds the detail.
Budgets (text only, excluding art, code and the collapsed "Every change" block): about 25 lines for one feature, about 45 for several, about 70 for a large change spanning many areas.

````markdown
```text
     ·      ˙        ✦          ·       ˙
  ˙     <feature name>                       ·
     ·    <one-line thesis: the behavior change>   ˙
        ˙        ·          ✧       ·
```

**<One sentence: the behavior change.>**

### The shape of it

```text
  <main scene: pipeline / fork / before-after / lifecycle / constellation>
```
<caption line: each node that maps to code, as `path:line`>

- <One bullet per stop on the scene, in flow order, each a full sentence with its link: `src/parse.py:41`.>

### <Story heading: the guarantee, per feature or per key part of one feature>

**<Lead sentence: what the system now does, in behavior terms.>**

- **Risk**: <the failure this prevents, with an actor, an event and a consequence; only when there is one>
- **Behavior**: <what a user or caller sees now>
- **Mechanism**: <how the code does it, each claim linked>
- **Decision**: <a choice the reader might not expect, and why; one bullet per decision>
- **Where**: <where this code sits in the flow, linked>

<One always-visible sentence placing the excerpt in the flow.>
<details>
<summary>`src/parse.py:38-61` - split_quoted keeps commas inside quotes</summary>

```python
...real lines from the file...
```

</details>

### Also changed

- <One bullet per group no story covers (tests, docs, config), each linked.>

### Every change

<the `collect_changes.py --format inventory` output, pasted verbatim: a collapsed <details> listing every file>

### Proof

- `<command>` - <result, verbatim counts>
- unverified: <what was not run, and why>

### Loose threads

- <Up to 10 items, one line each: not done, risky, or needs the user's decision.>

↗ <review desk URL, when one was opened>
````

Use only the labels a story needs: a refactor may have no Risk, a one-line fix no Decision.
Each bullet is a full sentence with an actor and a verb; a label never stands in for the sentence.

### Code moments

Code earns a place when it is the clearest way to show the idea: the guard that fixes the bug, the new type's shape, the line where two features meet.
Pick as many as the change needs, up to 10 per summary; a summary that pastes every hunk is a diff with extra steps.
Completeness is the inventory's job (terminal block and the page's "Every change" section), so stories can stay selective without hiding anything.

- Excerpt real lines from the file as it is now (read it after your final edit), or as the commit left it in per-commit mode, 5 to 25 lines, eliding with `// ...` or `# ...` where it helps.
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
- **Structure for scanning.** Every story is a bold lead sentence followed by labeled bullets (`**Risk**`, `**Behavior**`, `**Mechanism**`, `**Decision**`, `**Where**`), and lists hold everything parallel: scene stops, also-changed groups, checks run, loose threads.
A reader should get the change from the headings and lead sentences alone, then drop into the bullets they care about.
- **Bullets describe the system, not your activity.** "The parser keeps commas inside quotes (`src/parse.py:41`)", not "Updated the parser".
If three bullets in a row start with a past-tense verb about what you did, that is the checklist this skill exists to replace; rewrite them as what the code now does.
- **Precise, not compressed.** Name the exact symbol, status code, type, flag or count (`429` with `Retry-After`, `csv.reader`, `9 passed`) instead of describing it.
Cut hedges, intensifiers and filler ("properly", "robust", "note that", "this means", "I also"), and do not restate what the excerpt or scene already shows.
Never cut the words a sentence needs to be understood: an undefined noun or a fragment costs the reader more than a few extra words.
- **Describe the result, not your process.** "First I read X, then I tried Y" is noise unless the journey itself matters, for example a root cause you only found mid-way.
- **Be honest in Proof.** Exactly what ran and what it printed; failures and skips stated as such; unverified work labelled.
- **Follow the user's style rules** from their instructions (for example, a plain hyphen when they ban em dashes).
- Headings are short and specific to the content ("Rows with quoted commas survive"), not generic ("Changes made").

## Scaling

| Work | Terminal | Page |
|---|---|---|
| One small feature or fix (a few files) | title card, one scene, 1-3 code moments, proof, threads | hero, one story, proof, threads |
| One large feature | title card, main scene, a story per part (up to 10), code moments as needed (up to 10) | hero, a story per part |
| Several features | title card, constellation, a story per feature (up to 10; scene only where it helps) | hero, constellation, a story per feature |
| Large change (dozens of files or more) | as above, plus the rollup decides the stories: one per area that changed behavior, one "Also changed" bullet per remaining group | stories plus "Every change"; check the build's coverage line |
| Several commits, or commits plus uncommitted work | overview, then one independent summary per commit, each scaled by this table (Per-commit mode) | overview page plus one `--commit` page per commit, attached with `RD story` |
| Tiny change (a rename, a one-line fix, a config tweak) | one-line scene, 1-3 bullets with links, one proof line; skip the inventory when the links already name every file | skip the page unless the user wants a desk |
| Backlog follow-up (executing items from a Review Desk) | one bullet per item: `Bn` - what changed, with links; proof for what you ran; no title card or scene unless an item changed behavior | `RD backlog <sid> done <id>` per finished item first, then rebuild into the same `.review` folder and `RD reload`; skip AskUserQuestion; restart `watch` (the reviewer keeps running) |

Every size except tiny includes the "Every change" block in the terminal; the page always renders it.
Every size asks whether to open a Review Desk (step 6); for a tiny change, build the page only when the user says yes.

## Bundled files

- `scripts/collect_changes.py` - line-accurate map of every changed file and hunk (`--format map|inventory|json`, `--commit <sha>` for one commit); its Commits block switches on per-commit mode. Run first.
- `scripts/build_page.py` - wraps your body fragment into the animated page; expands `data-src`, `data-diff`, `data-loc` and the "Every change" inventory from disk and git (from the commit with `--commit`), refuses to build on a stale reference, and prints which changed files the stories cite. `--review-data` writes the Review Desk's manifest and diffs.
- `scripts/changes.py` - the change model both scripts share, so hunk numbers and file lists always agree.
- `scripts/archive.py` - names and writes the `<project>/<milestone>/<component>/` tree every build persists into (`$IMPL_SUMMARY_ROOT` relocates it); `archive.py adopt <pages> [--dry-run]` files pages built before the tree, and `archive.py sweep <fallback tree> --move` merges what a sandboxed build saved inside its repo (for example a repo's `.lavish/impl-summary-*.html`), reading project, branch-at-build-time and component from each page.
- `hooks/activate.py` - the activation hooks (`review-desk-axi setup hooks` installs them): a turn that changed files, through any tool or a `git commit`, must end with this skill, and every reminder names the base the work began at and when per-commit mode applies.
- `tests/` - `python3 -m unittest discover -s tests` from the skill directory: oracle tests against git on a generated ~300-file change set and on a multi-commit repo, and the hook against synthetic transcripts; set `IMPL_SUMMARY_TEST_REPO=<repo>` to also check a real working tree.
- `references/particle-grammar.md` - glyph vocabulary, frame rules, scene patterns. Read before drawing.
- `LANGUAGE.md` - the language protocol: how every sentence of the summary reads. Read before step 4.
- `references/page.md` - page building blocks (story bullets, risk and guard pairs, glossary cards), build and open commands. Read before step 5.
- the `review-desk` skill, installed beside this one - the Review Desk (step 6): editor, reviewer subagents, durable backlog, per-commit stories. Its SKILL.md owns the reviewer protocol.
- `assets/` - page shell, styles, the particle engine and the glossary cards (`glossary.js`); used by the build, no need to read.
