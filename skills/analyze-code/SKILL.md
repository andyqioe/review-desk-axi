---
name: analyze-code
description: >-
  Explain the lineage and role of code: trace the call stack around one function or method, several
  methods, a whole file, or a directory, then analyze what the target is for - who reaches it and
  from which entry points, what it calls down to, what data and side effects flow through it, what
  breaks if it changes, and which design docs describe it. Output is terminal Markdown with an ASCII
  call tree and clickable file:line references, optionally rendered as an HTML page (Lavish) that
  organizes the findings and draws the call lineage, data flow, and layering as diagrams. Invoked as
  `/analyze-code <targets> [--depth N] [--callers] [--html]` (for example `/analyze-code
  TokenBucket.take`, `/analyze-code RateLimiter.check LinkStore.get --callers`,
  `/analyze-code app/ratelimit.py`, `/analyze-code app --depth 2 --html`). Use it
  whenever the user says "analyze-code", "trace this function", "where is X called from", "what
  calls this", "walk me through the call stack", "what is this method's role", "how does this file
  fit in", "explain the lineage of", "what depends on this", "what breaks if I change X", "diagram
  how X fits in", "draw the call graph of X", or wants to understand how a piece of code fits into the system before modifying,
  deleting, or reviewing it - even if they do not name the skill. Do NOT use it for reviewing a diff
  for bugs (`/code-review`), for explaining an abstract CS concept (`/explain-this-concept`), or for
  a one-line "where is X defined" lookup that a single grep answers.
---

# Analyze code

Explain where a piece of code sits in the system and why it exists.
The reader is usually about to change, delete, debug, or review the target and needs a trustworthy map, not a paraphrase of the source.
So every claim in the output must be backed by a `path:line` reference the reader can click, and anything you could not confirm is labelled as such.

State the plan in one line before starting, for example: "Tracing `TokenBucket.take` from entry points to side effects (default depth)."

## 1. Parse the request

Targets can be mixed in one invocation:

- **Symbol** - `acquire`, `Engine::acquire`, `engine.acquire`, `AuthFlow.run`. Resolve to a definition first. If the name is ambiguous (several definitions), list the candidates with `path:line` and analyze the one the user most likely means, saying which you picked; analyze all of them only if they are few and genuinely related (for example trait method plus its impls).
- **Several symbols** - analyze each, then add a section on how they relate (shared callers, one calls the other, alternate paths to the same effect).
- **File** - the unit is the file's public surface: exported / `pub` items, the module's inbound importers, and its outbound dependencies.
- **Directory** - the unit is the package/module: its entry points, its public interface to the rest of the repo, and the internal layering between its files.

Trace-mode flags:

| Flag | Meaning |
|---|---|
| (none) | **Entry to leaves.** Walk callers upward until a real entry point, callees downward until the side-effect boundary. |
| `--depth N` | Stop after N hops in each direction; mark truncation with `...` in the tree. |
| `--callers` | Callers only. Summarize callees in one line. Use when the question is "who reaches this and why". |
| `--html` | Also render the analysis as an HTML page with diagrams (section 6) without asking. |

Flags combine (`--callers --depth 3 --html`).
Phrases like "as a page", "with diagrams", or "render it" in the request mean `--html`.

## 2. Orient before tracing

Spend a minute on context, because the role of a function is defined by the system around it, not by its body.

- Read the repo's agent notes and top-level docs (`CLAUDE.md`, `AGENTS.md`, `README.md`) for ownership statements like "`app/ratelimit.py` owns rate limiting".
- Find the language(s) and the build/entry layout: `main`, CLI dispatch, HTTP routes, `__main__.py`, `bin/`, exported package roots, test directories.
- Find design docs that mention the target: `grep -rl '<symbol or file stem>' --include='*.md'`. Note them for section 6; do not trust them over the code, and flag any disagreement.

## 3. Trace the call graph

There is usually no language server, so the trace is text search plus reading. That makes it easy to be confidently wrong, so be deliberate:

**Finding callers**
- Search for the bare name with word boundaries (`rg -n '\bacquire\b'`), then read each hit and discard definitions, comments, strings, and unrelated same-named symbols (a different type's `acquire`).
- Account for indirect reach, which plain grep misses and which is where the interesting lineage often hides:
  - trait / interface / abstract methods: find the trait, then every call through the trait and every impl;
  - methods called through a wrapper (`RateLimiter.check` -> `TokenBucket.take`): trace the wrapper's callers too, and show the wrapper in the tree;
  - function values passed as callbacks, registered handlers, decorators, route tables, CLI subcommand maps, `getattr`/reflection, cross-language boundaries (a Python script invoking a Rust binary's subcommand, JS injected into a page);
  - re-exports (`pub use`, `from x import *`, `index.ts` barrels) that change the name at the call site.
- Test callers count, but group them separately from production callers; they show intended behaviour.

**Stopping upward** at an entry point: `main`, a CLI subcommand, a request handler, a scheduled job, a public API a different package consumes, or a test. Name which kind it is.

**Finding callees**
- Read the target body; list what it calls. Collapse trivial helpers (formatting, getters, logging) into one line.
- Keep descending while the callee is project code that does real work. Stop at the side-effect boundary: filesystem, network, process spawn, locks/atomics shared across tasks, clocks, randomness, database, or a third-party library call. Name the boundary.

**Confidence**
- Mark each edge as confirmed (you read both ends) or inferred (name match you could not disambiguate, dynamic dispatch you could not resolve). Put `(?)` on inferred edges in the tree.
- If a search returns nothing for something that obviously must be called, say so rather than concluding it is dead code: the caller may be dynamic, generated, or in another repo.

For a directory or a large file, delegating the raw caller/callee sweep to an Explore subagent keeps your context clean; ask it for `path:line` edges, not prose.

## 4. Analyze the role

With the graph in hand, answer the questions the reader actually has:

- **Role in one sentence** - what responsibility would be missing if this code were deleted.
- **Why here** - why this responsibility lives at this layer rather than in its caller or callee (the design decision the code encodes).
- **Data flow and side effects** - inputs it reads, state it mutates (fields, shared maps, files, global/static), external effects, and what it returns. Name invariants it relies on (preconditions) and invariants it enforces (postconditions), each with the line that shows it.
- **Change impact** - which callers break or change behaviour if the signature or semantics change; which paths are covered by tests (name the test functions) and which are not; subtle couplings (ordering, locking, retries, error-as-control-flow, cancellation).
- **Design-doc linkage** - which docs describe this code, the relevant line, and whether code and doc agree.

## 5. Output format

Terminal Markdown, in this order, followed by the HTML offer from section 6. Keep prose tight; the tree and references carry most of the weight.

~~~markdown
## <target> - <one-sentence role>

**Defined at** `path:line` · **Kind** method on `Type` / free function / module · **Language** Python

### Call lineage
```python
main()                                      # app/server.py:49   entry: script main
└─ ThreadingHTTPServer(..).serve_forever()  # app/server.py:50   thread per request
   └─ Handler.do_POST(self)                 # app/server.py:15   POST /shorten
      └─ RateLimiter.check(client)          # app/ratelimit.py:29  key: app/server.py:18
         ├─ TokenBucket(capacity, ..)       # app/ratelimit.py:32  first call per client
         └─ ▶ TokenBucket.take(self)        # app/ratelimit.py:12  <- target
            ├─ time.time()                  # app/ratelimit.py:14  boundary: wall clock
            └─ # no lock: read, refill and spend at app/ratelimit.py:15-18
# tests: tests/test_ratelimit.py:4,10
```

### Role
### Data flow and side effects
### Change impact
### Design docs
### Unconfirmed
<edges marked (?), searches that came up empty, anything you assumed>
~~~

The tree is a code block in the target's language, so the reader's highlighter colors it (the terminal, the Review Desk chat, any Markdown viewer):
- Fence it with the language (` ```rust `, ` ```python `, ` ```ts `, ` ```go `); for a directory or a mixed trace, use the dominant language.
- Write each node as the code a reader would grep for: `Type::method(args)` / `obj.method(args)` / `function(args)`.
  Plain prose labels ("worker loop") lose the coloring, so name the actual function.
  Keep at most the one or two arguments that explain the edge and write the rest as `..` (`TokenBucket(capacity, ..)`, `RateLimiter.check(client)`); a call with nothing worth showing is `serve_forever()`.
- Keep every line of the block within about 90 characters, comment included, because the terminal and the desk chat are narrow and a long node pushes every reference off screen.
  Indent each level by 3 columns (a child's `└─` sits under its parent's first character, as in the example), so depth stays cheap.
  When a line would still run over, trim arguments first, then shorten the comment to the `path:line` and the one label that matters; put any longer explanation in the prose below the tree.
  A chain deeper than about 8 levels becomes two trees: callers down to the target, then the target down to its boundaries.
- Write every reference as the full path from the repository root, `app/ratelimit.py:12`, never a bare file name (`ratelimit.py:12`) or a bare line (`:12`), so each one resolves on its own, opens from any viewer and survives being copied out of the tree.
  That includes asides inside a comment: `key: app/server.py:18`, not `key: :18`.
  Several lines in one file stay on one path: `tests/test_ratelimit.py:4,10` or a range `app/ratelimit.py:15-18`.
- Put everything that is not code in a trailing line comment in that language's syntax (`//` for Rust, TS, JS, Go, Java, C; `#` for Python, Ruby, shell; `--` for SQL, Lua): the `path:line`, `entry: <kind>`, `boundary: <kind>`, `<- target`, `(?)`, `... N more`.
  Comments render dimmed, so the code stands out and the references stay readable.
- Every line inside the fence is a code node or a comment, nothing else.
  Side notes that are not a call (a missing lock, "same path as above", "two more callers") become comment lines in the block's own language, placed in the tree where they apply (`└─ # no lock: read, refill and spend at app/ratelimit.py:15-18`, as in the example), or move to the prose.
  Test callers get one closing comment line of paths only (`# tests: tests/test_ratelimit.py:4,10`); their function names, which are long, go in Change impact.
  A bare prose line, or a comment in another language's syntax, breaks the highlighting.
- Keep the tree glyphs (`└─ ├─ │ ▶`) and align the comments in one column, two spaces after the longest node.
- Highlighters do not turn text inside a code block into links, so the prose sections still cite each node they discuss as a clickable `path:line`.

Draw the tree with callers above and callees below the target (marked `▶`). When there are several independent caller chains, show each chain as its own branch rather than merging them. Collapse fan-out beyond ~6 siblings into `… N more (list below)` and list them after the tree.

Adapt the skeleton by target type:
- **Several symbols** - one block per symbol, then `### How they relate`.
- **File** - replace "Call lineage" with `### Inbound` (who imports / calls this file's items, grouped by item) and `### Outbound` (what it depends on); keep the rest.
- **Directory** - lead with `### Layout` (one line per file: its role), then `### Entry points`, `### Public interface` (what the rest of the repo uses), `### Internal layering` (a small tree of which file calls which), then Change impact and Design docs for the package as a whole.

Use `path:line` references for every definition, call site, and invariant, so the reader can click through, always as the full path from the repository root (`app/ratelimit.py:12`, not `ratelimit.py:12`).
Quote code only when a specific line is the evidence for a claim, and keep quotes to a few lines.

## 6. Offer the HTML page

The terminal report always comes first and stands on its own.
After it, give the user the option of an HTML page that organizes the same analysis into sections and draws it as diagrams:

- With `--html`, skip the question and build the page.
- Without it, the report is not finished until you ask: "Render this analysis as an HTML page with diagrams?"
Options: "Render page" and "Terminal is enough".
Put "Render page" first and mark it "(Recommended)" when the analysis is a file, a directory, or several symbols, or when the call tree has more than about 12 nodes; otherwise put "Terminal is enough" first.
Use the host's structured question tool when it has one (AskUserQuestion in Claude Code).
Without one (for example in Codex), end the reply with the question and both options as its last lines, then stop and wait for the answer.
- Skip the offer entirely when this analysis runs inside another response rather than as the user's own request, for example the condensed per-snippet analysis of verbose code mode, or a call from another skill or subagent.

To build the page, read `references/html-page.md` and follow it.

## Pitfalls

- **Paraphrasing the body is not analysis.** "It refills the bucket, checks for a token, and spends it" restates the code; "it is the only place a `POST /shorten` is admitted or refused, so every client's burst limit lives here" is the role.
- **Same name, different symbol.** Before adding an edge, confirm the receiver type or import path.
- **Stopping at the first caller.** One hop up is rarely the reason the code exists; keep going to the entry point unless `--depth` says otherwise.
- **Trusting docs over code.** Docs drift. Report disagreements under Design docs.
- **Silence on uncertainty.** A missing `Unconfirmed` section implies certainty you probably do not have; include it, even if it says "none".
- **Drawing more than you traced.** A diagram looks authoritative, so an edge on the page that the terminal report did not establish is worse than one in prose. Draw inferred edges dashed with `?`, and never add a node to make the figure look complete.
