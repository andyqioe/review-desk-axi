# Simplified Technical English (ASD-STE100) for agent writing

This file gives the rules for text that an agent writes for a person.
That text includes implementation summaries and their pages, Review Desk answers and backlog items, and system-redesign proposals, decisions and board text.
The file states the ASD-STE100 rules for sentences and words in our own words.
It is not the specification, and it does not include the STE dictionary.
`scripts/ste_check.py` checks the rules that a script can check, and it prints warnings.

## Why

The reader did not watch the work, and the reader often reads in a second language or at speed.
STE makes each sentence short, direct and unambiguous, so that a reader understands it on the first read.
Short sentences also make a wrong claim easy to see.

## Words

- Use one word for one meaning.
Use the same word for the same thing in all the text.
If the code calls it a "lease", do not call it a "grant" two lines later.
- Use technical names freely: code identifiers, file names, commands, product names and project terms.
Put code identifiers in backticks, so the checker and the reader see them as names.
- Prefer short, common words.
Write "use", not "utilize", and "before", not "prior to".
- Do not use words that end in "-ing" as verbs or as adjectives ("the existing cache", "by restarting the worker").
Write "the cache that exists" or "when the worker restarts".
Rewrite the sentence, and do not only swap the form: "the link must stop to work" means a different thing than "stop working".
Write "the link must stop" or "the link must not redirect after that time".
An "-ing" word is correct when it is a technical name (`logging`, a `string`) or a noun with no verb sense ("during", "nothing").
- Do not use a phrasal verb when a single verb exists.
Write "start", not "set up", and "do", not "carry out".
- Keep noun clusters to three words or fewer.
"the retry budget limit check" becomes "the check of the retry budget".

## Verbs and voice

- Use the active voice, so that every sentence names who or what does the action.
"The worker sends the charge", not "The charge is sent".
Use the passive only when the actor is unknown or does not matter to the reader.
- Use simple tenses: the present, the simple past and the simple future.
"The hook records the base", not "The hook has been recording the base".
- Use direct verbs for rules and ability: "can", "must", "do not".
Do not hedge with "would", "might" or "should" when the text states a fact or a rule.

## Sentences

- Write one idea in each sentence.
- Keep a descriptive sentence to 25 words or fewer.
- Keep an instruction to 20 words or fewer.
- Write an instruction as a command: "Run `npm test`."
- Give one instruction in each sentence.
Two actions can share a sentence only when they happen at the same time.
- Put a condition before its instruction: "If the build fails, read the first error."
- Do not use semicolons to join two sentences.
Write two sentences.
- Keep the articles ("a", "an", "the").
Do not shorten sentences into telegraph style.

## Paragraphs and lists

- Give each paragraph one topic, and state the topic in the first sentence.
- Keep a paragraph to six sentences or fewer.
- Use a vertical list for steps, options and items that are parallel.
A list item is a full sentence, not a fragment.

## Safety text

- Start a warning or a caution with a command: "Do not run the live stage without a valid lease."
- Then give the reason in a separate sentence.

## Before and after

Not STE:

<!-- ste: off -->
> Once the charge has been sent, any failure that might be encountered is treated as unknown by the payment client, ensuring that a retry utilizing the same idempotency key won't double-charge.
<!-- ste: on -->

STE:

> After the client sends a charge, it treats every failure as unknown.
> A retry uses the same idempotency key, so the provider does not charge the customer twice.

## What the checker checks

`python3 <implementation-summary>/scripts/ste_check.py <file>` reads Markdown, HTML or plain text and prints one warning for each problem.
It skips code blocks, inline code, URLs, HTML tags and quoted words.
Put a deliberate bad example between `<!-- ste: off -->` and `<!-- ste: on -->`.

| Rule | Warning |
| --- | --- |
| `length` | a descriptive sentence over 25 words, or an instruction over 20 words |
| `passive` | a form of "be" with a past participle ("is sent", "were removed") |
| `tense` | a perfect tense ("has been", "had sent") |
| `hedge` | "would", "might", "should", "could" in a statement |
| `ing` | an "-ing" word that is not a known noun or technical name |
| `phrasal` | a phrasal verb from a short list ("set up", "carry out") |
| `word` | a long word from a short list, with a shorter replacement |
| `semicolon` | a semicolon between two clauses |
| `instructions` | an instruction with two actions ("Run X, then open Y") |
| `paragraph` | a paragraph with more than six sentences |

The checker cannot judge meaning, so treat each warning as a question.
Fix the sentence, or keep it when the rule does not apply.
A technical name or an unknown actor can be a correct exception.
A clean report does not prove that the text is good STE.
