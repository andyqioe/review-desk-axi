# Particle grammar

The scenes in a summary are drawn once, as plain text, and used twice.
In the terminal they are frozen frames inside a ```` ```text ```` fence.
On the companion page the particle engine reads the same characters and brings them to life: streams run along paths into arrowheads, markers throw sparks, dust twinkles and sheds motes, shaded cells dissolve.
So a glyph is not decoration, it is an instruction to the engine and a word in a small visual language the reader learns in seconds.

## Contents

1. Vocabulary
2. Rules that keep frames readable
3. Scene patterns (title card, pipeline, fan-out, before/after, removal, state machine, constellation, layers)
4. Choosing a scene

## 1. Vocabulary

| Glyph(s) | Meaning in the story | What the page does |
|---|---|---|
| `─ │ ╭ ╮ ╰ ╯ ┌ ┐ └ ┘ ├ ┤ ┬ ┴ ┼ ╱ ╲` | a path: data, control or a request moving | carries particle streams when the path ends in an arrowhead |
| `┄ ┈ ┆ ┊` | a weaker or optional path (fallback, async, best effort) | same as above, drawn dimmer by its own shape |
| `∙` | a free-form dotted path that may bend in any direction | joins anything around it |
| `▶ ◀ ▲ ▼` | where the flow lands | sink: particles arrive and flash |
| `✦` | something new that matters | bursts of sparks every few seconds |
| `✧` `⋆` | something new but minor, or a supporting addition | smaller twinkle and bursts |
| `· ˙ ° ⋅` | dust: atmosphere, or what is left of something removed | twinkles and sheds falling motes |
| `░ ▒` | something dissolving: deleted, deprecated, replaced | flickers in and out |
| `╳` | a break: the bug, the cut, the path that no longer exists | flickers red |
| letters, digits, `.` `_` `/` `:` | labels, node names, file names | rendered bright and static |

Labels are nodes.
A stream stops when it reaches text and starts again on the far side, so `parse ──▶ validate ──▶ save` reads as three nodes and two moving edges.

## 2. Rules that keep frames readable

- **Width 72 columns max** so the frame fits a split terminal without wrapping; 3 to 14 rows is the sweet spot.
- **Single-width glyphs only.** Use the vocabulary above. No emoji, no CJK, no `★` or `●` (ambiguous width breaks alignment in some terminals).
- **Fence it.** Always ```` ```text ```` in the terminal, otherwise Markdown eats the spacing.
- **Leave one space between a box and an arrow's tail** (`│ idle │ ──▶`). A tail that touches a box border joins the box to the stream and particles will crawl around the border. Attach on purpose with `┬ ├ ┤ ┴` only when the box itself should look like it emits.
- **Every arrow you want animated must end in `▶ ◀ ▲ ▼`.** A path with no arrowhead stays still, which is what you want for underlines, box borders and dividers.
- **Restraint with sparks.** One or two `✦` per frame, on the things that are genuinely new. If everything sparkles nothing does.
- **Put file anchors in the caption line under the frame, not inside the art.** The art stays clean and the caption links are clickable (`src/parse.py:41`).
- **The frame must say something a sentence would say worse.** A flow, a fork, a before/after, a lifecycle. If it would just restate the heading with stars around it, cut it.

## 3. Scene patterns

### Title card

Opens the summary.
Feature name plus a one-line thesis floating in dust, one or two markers.

```text
     ·      ˙        ✦          ·       ˙
  ˙     overdue filter for todo lists        ·
     ·    tasks now know when they are late      ˙
        ˙        ·          ✧       ·
```

### Pipeline

The path a request or record takes through the new code, left to right.
Mark the new stage, caption the files.

```text
  csv row ──▶ tokenize ──▶ coerce ──▶ quarantine ✦ ──▶ store
```
caption: `tokenize` src/parse.py:18 · `coerce` src/parse.py:41 · `quarantine` src/quarantine.py:7 (new)

### Fan-out / fork

A decision point.
`┤` lets the gate emit both branches.

```text
                    ╭──▶ retry with backoff
  submit ──▶ gate ──┤
                    ╰──▶ audit log  ✦
```

### Before / after

Two frames side by side, the break on the left, the fix on the right.

```text
  before                          after
  parse ──▶ validate ──▶ save     parse ──▶ validate ──▶ save
                │                               │
                ╳ quoted commas                 ╰──▶ split_quoted ✦
                  silently dropped
```

### Removal

What dissolved and what took its place.
Shade the removed thing, let dust drift off it.

```text
  ░░░░░░░░░░░░░░░░░░░░░░    ˙   ·     ˙      ·
  ░ legacy_retry() 140L ░  ·   ˙   ·    ˙        replaced by
  ░░░░░░░░░░░░░░░░░░░░░░     ·    ˙      ·  ──▶  Backoff policy ✦
```

### State machine / lifecycle

Boxes for states, labelled transitions.
Gaps between boxes and arrow tails keep the boxes calm.

```text
  ╭────────╮  arm   ╭────────╮  post   ╭─────────╮
  │  idle  │ ─────▶ │ armed  │ ──────▶ │ unknown │
  ╰────────╯        ╰────────╯         ╰─────────╯
                                            │ observe
                                            ▼
                                       ╭─────────╮
                                       │ settled │ ✦
                                       ╰─────────╯
```

### Constellation (several features)

One frame that places every feature in this batch relative to the thing they all touch.
Use it at the top of a multi-feature summary, then give each feature its own story.

```text
       ✦ due dates ────────╮
                           ▼
   ✧ overdue filter ───▶ todo list ◀─── ✦ csv export
                           ▲
       ⋅ sort by date ─────╯
```

### Layers

Where the change sits in a stack, with the touched layer marked.

```text
  ╭────────────────────────────────────────╮
  │  cli          todo list --overdue       │
  ├────────────────────────────────────────┤
  │  store        due dates per task      ✦ │
  ├────────────────────────────────────────┤
  │  format       json lines (unchanged)    │
  ╰────────────────────────────────────────╯
```

## 4. Choosing a scene

Ask what the reader is most likely to misunderstand, then draw that.

| The change is mostly... | Draw |
|---|---|
| a new step in an existing flow | pipeline with the new stage marked |
| a new branch, guard or fallback | fan-out |
| a bug fix | before / after with the `╳` at the root cause |
| deletion or replacement | removal |
| new states or a protocol | state machine |
| several features in one batch | constellation first, then one scene per feature |
| a refactor that moved responsibility | layers, or before / after of the ownership |

One feature usually wants a title card plus one or two scenes.
A batch of features wants a title card, a constellation, and one scene per feature, but skip a feature's scene when its story is a three-line change that a code excerpt shows better.
