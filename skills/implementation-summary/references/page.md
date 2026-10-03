# Companion page

The page is the same story as the terminal summary, given room: scenes animate, code excerpts get a line-number gutter with the key lines lit, and every file reference opens in the editor at the right line.
Room is for scenes, code, links and glossary cards, not more words: page prose follows `LANGUAGE.md` and the terminal's budgets (at most about 1.5x).
You write only the body fragment; `scripts/build_page.py` adds the shell, the styles (near-black, indigo, Inter and JetBrains Mono), the particle engine and the glossary cards.

## Build and open

```bash
SKILL=<this skill's directory>
python3 "$SKILL/scripts/build_page.py" \
  --body "$SCRATCH/impl-summary-body.html" \
  --summary "$SCRATCH/summary.md" \
  --title "<feature name>" \
  --paths <same --paths you gave collect_changes> \
  --base <same --base, if you passed one> \
  --meta "<branch> vs <base>" \
  --review-data
```

Then open the Review Desk on it (SKILL.md step 6); the desk's Story tab shows this page and its file links open in the desk.

- Write the body and the terminal summary to the session scratchpad; the build persists both.
- The build writes everything into `~/.claude/implementation-summaries/<project>/<milestone>/<component>/` as files sharing one `<YYYYMMDD-HHMM>-<title-slug>` stem: `.html` (the page), `.md` (the terminal summary), `.body.html`, `.changes.json` (every file, hunk and origin) and `.inventory.md`, then rebuilds `<project>/INDEX.md`. It prints the folder and the page path.
- Project is the repository (a worktree files under its main checkout), milestone is the branch, component is the most-touched area. Pass `--project`, `--milestone` or `--component` when those defaults are wrong. `--out` writes an extra copy of the page; `IMPL_SUMMARY_ROOT` moves the whole tree (tests use it).
- Summarizing a project that is not in git (a skill under `~/.claude/skills`, say) means building from a scratch git copy of it. Pass `--source <the real directory>`: editor links then open the real files, the project is named after it, and the milestone is its branch, or the date outside git, the same rule `adopt` uses.
- If the central tree is not writable (a sandboxed agent), the build persists into `<repo>/.lavish/implementation-summaries/` and says so. Leave it; an unsandboxed session merges it with `python3 "$SKILL/scripts/archive.py" sweep <that folder> --move`.
- Pages built before the tree existed: `python3 "$SKILL/scripts/archive.py" adopt <repo>/.lavish/impl-summary-*.html` copies each into place (project from the files it links, milestone from the reflog at its build time, outside git the build date) and skips any already filed.
- The build refuses to write if any `data-src`, `data-diff` or `data-loc` points at a missing file or out-of-range line, or `data-hunks` names a hunk that does not exist. Fix the reference and rebuild; never delete the reference to make the error go away.
- The build prints `stories cite N/M changed files` and names the files that appear only under "Every change". Read it: a file that changed behavior should be in a story or the "Also changed" sentence, not only in the inventory.
- `--review-data` (bare) writes `<stem>.review/` next to the page and prints it; pass that folder back (`--review-data <folder>`) when rebuilding after a review, so the desk session, its chat and its backlog stay attached.
- Editor links default to `vscode://`. Pass `--editor cursor|zed|windsurf|file|none` or set `IMPL_SUMMARY_EDITOR` when you know the user's editor.

## Body building blocks

Everything is plain HTML; use the classes below and add your own `<style>` block if a story needs a layout the defaults do not cover.

### Hero

```html
<header class="hero">
  <p class="eyebrow">implementation summary · 2 features</p>
  <h1>Todos that know they are late</h1>
  <p class="thesis">One sentence on what the system can do now that it could not before.</p>
  <div class="chips">
    <span class="chip"><b>6</b> files</span>
    <span class="chip"><span class="add">+214</span> <span class="del">-37</span></span>
    <span class="chip"><b>18/18</b> tests pass</span>
  </div>
</header>
```

The engine puts a live ASCII field behind the hero automatically (it follows the pointer).

### Scene

Paste the same frame you used in the terminal.
Write it raw: the build dedents and escapes it.

```html
<div class="scene-wrap">
  <pre class="scene">
    csv row ──▶ tokenize ──▶ coerce ──▶ quarantine ✦ ──▶ store
  </pre>
  <p class="caption"><a data-loc="src/parse.py:18">tokenize</a> · <a data-loc="src/quarantine.py:7">quarantine</a> (new)</p>
</div>
```

Add `data-calm` to a `pre.scene` to keep only the assembly and twinkle, without streams or bursts (good for a dense layer diagram).

### Story section

One per feature (or per major part of one feature).
Prose on the left stays sticky while the visuals scroll on the right; it stacks on narrow screens.

```html
<section class="story">
  <div class="story-head"><span class="index">01</span><h2>Rows with quoted commas survive</h2></div>
  <div class="story-grid">
    <div class="prose">
      <p>What it does now, in behavior terms...</p>
      <p>How it works, walking the path with links like <a data-loc="src/parse.py:41"></a>...</p>
      <p class="callout"><strong>Decision:</strong> why it is built this way, and what was traded.</p>
    </div>
    <div class="visual">
      <!-- scene-wrap, code figures, diff figures -->
    </div>
  </div>
</section>
```

Use `<div class="wide">` inside `.story-grid` for something that should span both columns.

### Code excerpt (pulled from disk)

```html
<figure data-src="src/parse.py:38-61" data-focus="45-49" data-title="split_quoted">
  Optional caption: what to notice in the lit lines.
</figure>
```

- `data-src` is `path:start-end`, repo-relative. The build reads those exact lines, so line numbers in the gutter are true.
- `data-focus` lights the lines that carry the idea (comma-separated ranges allowed).
- `data-lang` overrides the language guessed from the extension.

### Diff excerpt

```html
<figure data-diff="src/parse.py" data-hunks="2" data-title="the fix"></figure>
```

Diff of the working tree against the base, with old and new line-number gutters and 3 lines of context that never cross into another hunk.
Highlighting runs per block, old and new sides separately, so docstrings, block comments and template strings that span lines keep their colour.
`data-hunks` takes the `#n` numbers `collect_changes.py` prints (comma-separated); omit it to show every hunk.
Nothing is cut by default; `data-max="N"` caps the rows and labels exactly which hunks were left out.
Untracked files show whole as added, deleted files whole as removed, renames show `old → new`, and binary, mode and symlink changes show as notes.
Prefer a focused `data-src` excerpt for new code and a diff for a surgical change where the removed lines matter.

### Every change

```html
<section data-changes></section>                       <!-- scope = build --paths -->
<section data-changes data-paths="src/ tests/"></section> <!-- narrower scope -->
```

Expands to every changed file in scope, grouped by directory, each a collapsed row (status badge, rename source, notes, origin, +/-) holding its full diff, with expand-all and collapse-all controls and "show only" chips that filter rows by origin (committed, staged, unstaged, untracked).
Rows for files a story cites carry a `story` tag.
If the body has no `data-changes`, the build inserts one before the proof section, so you normally do not write it.
A file whose diff exceeds 4000 lines renders its first 4000 with a labelled cut.

### Risk and guard

`LANGUAGE.md` section 2: a story's failure modes as pairs, then any plain facts.

```html
<div class="pair">
  <p class="risk">The connection drops after the charge is sent, the payment looks failed, it is retried, and the customer is charged twice.</p>
  <p class="guard">Once a charge is sent, the <span data-term="client">payment client</span> treats any failure as unknown (<a data-loc="src/payments/client.rs:105"></a>).</p>
</div>
<p class="plain">The Python package has no server yet.</p>
```

The page labels `.risk` and rules `.guard`; write no label text yourself.

### Glossary cards

Mark a term with `data-term` on any inline element, then define every key once in a `<dl class="glossary">` anywhere in the body (the end is tidiest).
The build lifts the list out of the page into data; `glossary.js` shows a card on hover, focus or tap.

```html
<p class="guard">Late webhooks are filed under the original <span data-term="intent">payment intent</span>.</p>

<dl class="glossary">
  <dt data-term="intent" data-loc="src/payments/intent.rs:44">payment intent</dt>
  <dd>The saved record of one charge before it is sent: amount, card, idempotency key and deadline. A retry reuses it instead of starting a new charge.</dd>
  <dd class="ask">What happens to an intent whose deadline passes?</dd>
  <dd class="ask">Why do late webhooks stay tied to it?</dd>
</dl>
```

- `<dt>`: `data-term` is the key, the text is the card's title, `data-loc` (optional) is the card's code link, checked like any `data-loc`.
- `<dd>`: the definition; inline HTML and `<a data-loc>` links work. Required.
- `<dd class="ask">`: a suggested question, up to three. The card always has an "Ask Claude" button as well.
- The build fails on a `data-term` with no entry, an entry with no definition, or a link out of range, and warns on an unused entry. It prints `N glossary terms`.
- Selecting any text on the page offers "Ask Claude" too.
In the Review Desk, a question lands in the chat box with the quote and section, for the user to edit and send; opened on its own, the page copies a ready-to-paste prompt.

Which terms to mark and how to write definitions: `LANGUAGE.md` section 4.

### File link

```html
<a data-loc="src/parse.py:41"></a>                <!-- text becomes src/parse.py:41 -->
<a data-loc="src/parse.py:38-61">split_row()</a> <!-- custom text -->
```

### Proof

```html
<section class="proof">
  <div class="section-head"><span class="index">✓</span><h2>Proof</h2></div>
  <ul class="checks">
    <li data-status="pass"><div><code>pytest -q</code><span>18 passed in 0.41s</span></div></li>
    <li data-status="fail"><div><code>ruff check</code><span>2 pre-existing warnings in legacy.py, untouched</span></div></li>
    <li data-status="unverified"><div><code>live export of 10k rows</code><span>not run; only the 20-row fixture was exercised</span></div></li>
  </ul>
</section>
```

Statuses: `pass`, `fail`, `skipped`, `unverified`.

### Loose threads

```html
<section class="threads">
  <div class="section-head"><span class="index">→</span><h2>Loose threads</h2></div>
  <ol>
    <li>What is not done, what is risky, what the user should decide.</li>
  </ol>
</section>
```

### Overflow

Long secondary material (a second excerpt, a data table) goes in `<details class="more"><summary>second excerpt</summary><div>...</div></details>` so the main path stays short.

## Page order

hero → constellation (multi-feature only) → one story per feature → every change (automatic) → proof → loose threads.
That mirrors the terminal summary, so a reader can switch between them without getting lost.
