# Language protocol

This file governs how the summary's text reads, in the terminal and on the page.
SKILL.md governs what the summary contains; this file governs the words and how they are laid out.

The reader is smart but was not in the session.
They do not know the project's private vocabulary, and they should not need to.
Precision stays: exact symbols, counts, status codes and links.
What goes is compression: undefined nouns, nominalized actions, fragments and rules stated without their reason.

## 1. Headings state the guarantee

A story heading says what the system now promises, in plain words.

- Good: "A retried payment never charges twice", "A failed charge is never read as 'nothing happened'".
- Bad: "Durable edges around external calls", "Reproducible limits", "Transport foundation".

If the heading needs a project term to make sense, it is not a guarantee yet.

## 2. Risk, then guard

Most behavior in a summary exists to prevent a failure.
Name the failure first, then what the code now does about it.

- **Risk**: one sentence a reader can picture, with an actor, an event and a consequence.
"The connection drops after the charge is sent, the payment looks failed, it is retried, and the customer is charged twice."
- **Guard**: what the code does now, with its links.
"Once a charge is sent, the payment client treats any failure or malformed reply as unknown."

On the page, write the pair as markup so the risk gets its label:

```html
<div class="pair">
  <p class="risk">The worker sends a charge, crashes, and no one knows what the bank was told.</p>
  <p class="guard">Before sending, the worker saves a <span data-term="intent">v3 intent</span> ...</p>
</div>
<p class="plain">Facts that prevent nothing (a schema version, a moved file) go in a plain line.</p>
```

In the terminal, write the same pair as the story's first bullets: `**Risk**:` with the failure, then the guard under `**Behavior**` or `**Mechanism**`.
Use pairs only for real failure modes; a story with nothing to prevent starts at `**Behavior**`.
Never invent a risk to fill the pattern.

## 3. Lead sentence, then labeled bullets

A story is read in two passes: the heading and its bold lead sentence first, then the bullets the reader cares about.

- Open every story with one bold sentence that states the behavior change.
- Follow it with labeled bullets, in this order and only those the story needs: `**Risk**`, `**Behavior**`, `**Mechanism**`, `**Decision**`, `**Where**`.
- Each bullet is one or two full sentences with an actor and a verb, and its links; the label is a signpost, never a substitute for the sentence.
- Several decisions get several `**Decision**` bullets; never pack two ideas into one bullet with a semicolon chain.
- Parallel items elsewhere are lists too: scene stops, "Also changed" groups, Proof, Loose threads, the overview's Commits.

## 4. Plain sentences

- **Give every action an actor and a verb.**
"The worker saves a record before it sends", not "Dispatch recording precedes transport".
- **No fragments, no slash compounds, no dot lists**, in bullets as much as in prose.
"endpoint and token", not "endpoint/token"; "a runner, its instructions and a results page", not "Runner · instructions · results".
- **Every negative says what it protects.**
"A late webhook can't mark the order PAID, so a stale answer can't ship an order whose charge failed."
- **Process notes say what happened.**
"The sandbox blocked the first network test from opening a local port; the rerun passed", not "the initial TCP test's sandbox denial is retained with passing reruns".
- **Keep a code name only when the reader will search for it.**
Otherwise use the plain word and link the code.
- **Plain hyphens, never em dashes.**

## 5. Terms get cards, not lectures

A term the reader may not know gets a hover card instead of an inline definition, so the sentence stays short.
Mark it with `data-term` and define it once in the page's glossary block (format in `references/page.md`).

**Which terms.**
Project vocabulary a newcomer would not know: domain nouns ("idempotency key", "settlement window"), states in capitals ("PENDING"), version labels ("v3 intent", "schema v4"), internal tools and fixtures ("ledger soak", "golden card set").
Not general engineering words ("TLS", "redirect", "transaction") unless the project uses them in a special sense ("private TLS").
Mark the first use in each story, not every use.
Expect about 8 to 15 terms on a large summary; more means the prose itself is too dense.

**The definition.**
One or two sentences saying what the term means in this project, not a textbook entry.
Say what it is, then what it is for or what it rules out.
Link where it lives (`data-loc` on the `<dt>`), at the definition line.
"A key the payment provider uses to recognize a retry of the same charge. Sending it twice can never charge the customer twice."

**Suggested questions.**
Up to three per term: the questions a reviewer would actually ask next, phrased as they would type them.
"What happens when the deadline passes mid-charge?", not "Learn more about deadlines".
A suggested question must be answerable from the code or the session; never suggest one the summary is hiding from.

The terminal has no hover.
There, use the plain word, or give an essential term a short parenthetical on first use: "the idempotency key (the provider's way to recognize a retry)".

## 6. Length

Clarity first, then brevity.
A rewrite in this style runs about 1 to 1.3 times the length of the compressed version, because definitions moved into cards.
Cut filler, hedges and restatement as before ("properly", "robust", "note that", "this means").
If a sentence needs its words to be understood, keep them.

## 7. Before and after

Compressed:

> The v3 intent carries the immutable amount, idempotency key, card reference and deadline.
> Dispatch recording rechecks order state and fraud holds, then withdraws pendingness atomically.
> Late webhooks remain tied to the original intent. They cannot restore PENDING or settle a refund obligation.

This protocol (terms in brackets carry cards on the page):

> ### A sent charge can't leave the order guessing
>
> **The worker records exactly what it is about to charge before it sends anything, so a crash mid-charge never leaves the order unknown.**
>
> - **Risk**: the worker sends a charge, crashes, and no one knows what the bank was told.
> - **Mechanism**: before sending, the worker saves a [v3 intent] with the fixed amount, its [idempotency key], the exact card and the deadline.
> - **Mechanism**: in the same transaction it rechecks the order and the [fraud holds], and moves the order out of [PENDING] so it can't be charged a second time.
> - **Risk**: a slow webhook arrives after the charge moved on, and reports the order as unpaid or closes the question of whether a [refund] happened.
> - **Behavior**: late webhooks are filed under the original intent and can do neither.

## 8. Check before publishing

- Every story heading states a guarantee in plain words, and a bold lead sentence follows it.
- Every bullet is a full sentence under a label the story needs.
- Every risk is a real failure with an actor, an event and a consequence.
- Every project term in a story is either plain-worded or carries a card; every card has a definition and a code link where one exists.
- No fragments, slash compounds, dot lists or unexplained negatives in prose.
- The build prints no glossary warnings (`build_page.py` checks every `data-term` against the glossary and every link against the files).
