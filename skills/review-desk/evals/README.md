# review-desk evals

Eight task evals (`evals.json`) and twenty trigger queries (`trigger_evals.json`).
The skill has two roles, so the evals cover both:

| id | role | what it tests |
|---|---|---|
| 1 | main | open a session, pin the code moments, set prefs, spawn the chosen tier without asking |
| 2 | main | EXECUTE hand-back: ack, implement a fix and a suggested patch, mark done with notes, reload, respawn at the wanted tier |
| 3 | main | HANDOFF hand-back: no ack (nothing was executed), no code, respawn the new tier without asking |
| 4 | main | the prompt hook's injected `undelivered` context: handoff, ack and fix only the item the user asks for, leave the question open and ask it |
| 5 | reviewer | answer two questions, log exactly the real gap with a five-bullet detail (Context, Issue, Suggested fix, Reasoning, Tests), cite `path:line`, exit END |
| 6 | reviewer | tier change mid-session: hand over, leave the next question for the new reviewer, exit HANDOFF |
| 7 | reviewer | asked to edit code itself: refuse, log it with a five-bullet detail, exit EXECUTE when the user executes |
| 8 | main | no subagents (Codex-style): `attach --main`, answer across a tier change without a handover |
| 9 | host | asked for a diagram: write a page into the pages folder, open it as a tab, name it, repo untouched |
| 10 | host | `/analyze-code split_row --html`: the skill's page lands in the pages folder (generator via `run`) and opens as a tab |
| 11 | host | close the open summary page and open an existing older one, without writing a new page |

`/implementation-summary` keeps its own integration eval (`implementation-summary/evals/evals.json`, id 5).

## How a run works

The desk is interactive, so a run has three parts:

```bash
H=~/.claude/skills/review-desk/evals/harness.py
python3 $H setup <id> --run <run-dir>    # fixture repo, isolated home, seeded session; writes prompt.txt + executor_note.txt
python3 $H drive <id> --run <run-dir> &  # the scripted user (only when the eval has driver steps)
# executor: an agent gets prompt.txt (+ the skill path for with_skill runs) and executor_note.txt,
#           and saves its final message to <run-dir>/final.md
python3 $H check <id> --run <run-dir>    # grading.json in the skill-creator schema
```

Host evals (9-11) need no executor: `harness.py host <id> --run <dir> [--skill <copy>] [--model sonnet --effort low]` starts the real reviewer host (`claude -p`) of that skill copy on the seeded session, plays the driver steps once everything is answered, and stops it.
Put their run directories outside `~/.claude` (the reviewer's page writes are refused there); `setup` enforces it.

For skill-creator iterations, put each run at `review-desk-workspace/iteration-N/<eval-name>/{with_skill,without_skill}/` and aggregate with `python -m scripts.aggregate_benchmark` from the skill-creator directory.
Reviewer evals (5-7) inline the reviewer instructions from `review-desk-axi prompt`, so they run on any agent; the baseline for those is the same prompt with the instructions removed.

Every check is programmatic (`harness.py check_one`): it reads the session files, the fixture repo and `final.md`, never a transcript.
`python3 -m unittest evals/test_harness.py` proves the set discriminates: a scripted golden run passes every check, and a run that does nothing scores 1/8 to 2/7 (only guards like "repo unchanged" pass).
