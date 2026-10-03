# HTML page for an analysis

The page is a second rendering of an analysis you already finished and verified.
It organizes the same findings into sections and replaces the ASCII tree with real diagrams.
It never adds a claim the analysis did not establish: every node, edge, and statement still traces back to a `path:line` you read.

## Before writing HTML

Lavish's current guidance lives in its CLI, so read it fresh each time instead of relying on memory:

- `lavish-axi playbook explanation` - the page explains existing code to a reader who was not there.
- `lavish-axi playbook diagram` - every figure on the page.
- `lavish-axi playbook code` - only if the page quotes code lines as evidence.

Design source: the user's own design system when they have one (for example a `~/.claude/DESIGN.md` their instructions point to), which wins over Lavish's design priority order; otherwise Lavish's current design guidance.
Use its tokens (colors, fonts, radii, spacing) as CSS custom properties and color the SVG figures through them.
When you hand the page over, say in one line which design source you used.

Write the file to `<repo root>/.lavish/analyze-<target-slug>-<YYYYMMDD-HHMM>.html`, where the repo is the one that holds the target.
The slug is the target name lowercased with non-alphanumerics turned into `-` (for example `tokenbucket-take`, `app-ratelimit-py`).

## Build it with a generator, not by hand

Do not hand-type SVG coordinates into the HTML.
Write a short Python generator in the scratchpad that imports `scripts/figures.py` from this skill and writes the page.
The module gives you:

- `Fig.node` / `Fig.edge` / `Fig.divider` / `Fig.raw` with explicit coordinates, so you decide the layout and the output stays static SVG;
- a warning when text is wider than its box, a node leaves the viewBox, two nodes overlap, or an edge names a missing node;
- solid backgrounds behind edge labels, chain highlighting on click, and click-to-copy on every `path:line`;
- `FIG_CSS`, `ARROW_ON` and `PAGE_JS` to paste into the page, and `report()`, whose confirmed and inferred edge counts feed the header chip;
- syntax highlighting: `Fig(..., lang="rust")` sets the figure's language, `node(..., code="pub fn acquire(&self, key: K)")` shows the node's signature or call expression highlighted (its `title` stays as the tooltip), and `edge(..., code="let permit = self.acquire(key).await?")` labels an edge with the highlighted call-site line.
Tokens are rendered as static `<tspan>` elements at generation time (Pygments), so the figure needs no script and still renders offline and in Lavish exports.

`scripts/examples/url_shortener.py` is a complete worked example covering the four figure patterns below.
Copy its structure and page CSS; its facts are a snapshot of one analysis, not reusable content.
Run the generator until `report()` prints no warnings, then fix what only a screenshot shows.

## Page structure

Order the page like the terminal report, so a reader who saw one can navigate the other.
Each claim is its own block with a stable `id`, so the user can annotate exactly the part they doubt.

1. **Header** - the question the page answers ("What is `RateLimiter.check` for, and how does a request reach it?"), then the one-sentence role.
Below it, mono chips for defined-at `path:line`, kind, language, trace mode (`entry to leaves`, `--depth N`, `--callers`), and an edge count split into confirmed and inferred.
2. **Call lineage diagram** - see the next section.
Put the ASCII tree from the terminal report in a collapsed `<details>` under the figure so it stays copyable.
3. **Role** and **Why here** - two short cards, prose only.
4. **Data flow** - for a single function, one figure with inputs on the left, the target in the middle, and on the right the state it mutates, the external effects, and the return value.
For a file or a larger target, use one figure per key mechanism instead (see "Figures for key mechanisms").
Either way, follow it with short cards for owned state, preconditions, and postconditions, each with its `path:line`.
5. **Change impact** - a table with one row per caller: what breaks or changes, which test covers it (test function name and `path:line`), or a visible "untested" badge.
Subtle couplings (ordering, locking, retries, error-as-control-flow, cancellation) go in callout cards below the table.
6. **Design docs** - a table of doc, line, and an agrees / disagrees / silent badge, with one line on each disagreement.
7. **Unconfirmed** - always present, visually distinct, never collapsed.
List inferred edges, searches that came up empty, and assumptions; write "none" when there are none.
8. **Left out and next** - what the trace deliberately did not cover (truncated depth, collapsed fan-out, other repos) and where to look next.

## Call lineage diagram

Hand-authored inline SVG, laid out top to bottom in bands:

- **Entry band** at the top - one node per entry point, labelled with its kind (CLI subcommand, handler, scheduled job, public API, test).
- **Caller bands** - intermediate callers, one band per hop.
Independent caller chains stay as separate columns; do not merge them into one path.
Wrappers (`RateLimiter.check` -> `TokenBucket.take`) appear as their own nodes.
- **Target** - one node in the primary color, visibly heavier than everything else.
- **Callee bands** below the target, ending at **boundary nodes**.
Draw boundaries in a distinct shape (for example a dashed-border pill) with the boundary kind as a label: `filesystem`, `network`, `process`, `shared Mutex`, `clock`, `database`, `third-party`.
- **Test lane** - test callers sit in a separate column or band at the side, labelled "tests", so they never read as production callers.

Edges and markers:

- Confirmed edges are solid; inferred edges are dashed with a `?` marker and a `<title>` saying why they are unconfirmed.
- Indirect reach (trait dispatch, callback, route table, reflection, cross-language call) gets a short edge label naming the mechanism.
- Depth truncation is a faded `…` stub node on the cut edge.
- Fan-out beyond about 6 siblings collapses into one `… N more` node; list the rest in a table under the figure.

Each node shows its code and its `path:line` in mono text, as the full path from the repository root.
`Fig.node` passes every ref through the figure's `resolve`, so a short ref you type (`ratelimit.py:12 · :29`) displays as `app/ratelimit.py:12,29`; when that is wider than the box, the directory prints as a small dim line above `file:line`, inside the same box.
Give `resolve` the mapping for every short name you use, and size boxes so neither line warns.
Give every code node `code=`: the signature for the target and for definitions (`def take(self)`), the call expression for callers (`LIMITER.check(client)`), so names, keywords and types carry the same token colors as an editor.
Label each edge whose call site is the evidence with `code=` and the actual line from the caller (`self.acquire(key).await?`), trimmed to one short expression.
Keep plain `title` text for nodes that are not code: entry kinds, boundaries such as `shared Mutex`, frames and `… N more`.
Size boxes for the code: `CODE_PX` (7.25px per character) is what the width check uses, so long signatures need wider boxes or a shorter excerpt with `…`.
Interaction: clicking a node highlights its full chain from entry point to target (or target to boundary) and dims the rest; clicking empty space clears it.
Clicking a `path:line` chip anywhere on the page copies it to the clipboard.

If the graph would exceed about 25 nodes, follow the diagram playbook: draw a small overview of the chains, then one figure per chain below it.

## Figures for key mechanisms

The data-flow section usually has two or three mechanisms that carry the target, for example a guard, a result classifier, or a write protocol.
Give each one its own single-concept figure instead of describing it in prose:

- **Guard around effects** - a three-box row: check, effect (as a boundary), check again; one row per guard kind.
- **Classifier or state gate** - a decision tree from the input through each condition to colored outcomes (ok / fail / neutral), each with its `path:line`, with the exact condition in the node's `<title>`.
- **Multi-step protocol** - one lane per actor (for example two browser tabs), steps in order joined by elbow edges, and a `divider` at any point of no return (a write's dispatch point), with the outcomes for each phase in a side column.
- **Route or command table** - see the file variant below.

## Variants by target type

- **Several symbols** - one lineage figure per symbol, then a **How they relate** figure that merges them: shared callers drawn once with edges to each symbol, and direct calls between the symbols highlighted.
- **File** - replace the lineage figure with an **Inbound / Outbound** figure: importers and callers on the left grouped by which item they use, the file in the middle with its public items listed inside, dependencies on the right ending in boundaries.
When the file serves routes, CLI subcommands, or a dispatch table, one figure gets too dense.
Draw a small **overview** instead (launchers and the main caller on the left, the file's items in a frame, dependencies on the right, boundaries along the bottom).
Then add a **route map**: one row per route with caller, route pill, and handler, and callers that are tests or tooling styled as `test`.
Put outbound dependencies in a table with a boundary badge per row rather than a second figure.
- **Directory** - lead with a **Layering** figure: one node per file labelled with its one-line role, edges for calls and imports, entry points marked, and a frame around the package's public interface with repo consumers drawn outside the frame.
Follow it with the Entry points, Public interface, Change impact, and Design docs sections for the package as a whole.

## Open, verify, and review

1. Open it with `lavish-axi <file>`.
2. Render-verify before telling the user it is ready: screenshot each figure with `chrome-devtools-axi` at desktop width, then at narrow width.
The Lavish layout audit skips SVG interiors, so this check is yours.
Scroll each figure into view with `eval` and `getBoundingClientRect` before each screenshot; one viewport screenshot of the top proves little.
Look for these, which the generator's checks cannot catch:
   - edges crossing a node's tag (tags sit 7px above the box) or a frame label;
   - labels on near-vertical edges sitting on the line, or on an arrowhead;
   - long shallow diagonals between lanes, which read better as elbows;
   - page-level horizontal overflow (check `document.documentElement.scrollWidth` against `innerWidth`); figures and tables should scroll inside their own cards.
Headless Chrome may refuse widths below about 500px; say so rather than claiming a phone-width check.
Confirm chain highlighting once by dispatching a click on a node and listing the nodes marked `.on`.
3. Tell the user the page is open, in one line, with its path and the design source.
4. Wait for feedback with `lavish-axi poll <file>`.
In Claude Code, run it as a tracked background command (Bash `run_in_background` with the longest allowed `timeout`) so the terminal stays free and this session is notified when feedback arrives; if it is stopped at its time limit, say so and start it again.
On a host without tracked background commands, run it in the foreground.
5. Feedback usually asks to trace a node deeper, resolve a `?` edge, or challenge a claim.
Verify against the code first, the same way as the original trace, then update the page and the Unconfirmed section, and poll again.
Stop when the user ends the session from the browser.
