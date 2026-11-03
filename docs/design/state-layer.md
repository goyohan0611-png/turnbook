# The brain's current understanding: one state for every agent

Status: design and experiment plan (2026-11-02). Nothing implemented. Kept only if the
experiment below passes its gate.

Turnbook is one brain that makes several agents work as one: Claude Code, Codex and the others
attached to it share the same memory, the same current understanding and the same work in
progress, so whichever one the user talks to behaves like the same AI. The memory (every turn,
verbatim) and the work (`brain_work`) exist. What is missing is the middle: an understanding
the brain already holds, so that every agent sees the same current value, the same decision and
the same state of a project, instead of each one piecing it together from raw turns on its own
(and two agents ending up with two different answers).

This document adds that layer. Who writes it does not matter: any worker attached to the brain
can, whenever there is something new to fold in. Better answers to "what is it now / when /
everything about X" are the measurable side effect, and the experiment measures them.

## Why it is needed

Turnbook stores every turn verbatim and finds it again: no model tokens to save, nothing lost or
made up on the way in. What it does not have is anything *already worked out*: "what is the
current value", "what happened when", "everything about X across sessions" is pieced together
by the reader from up to 3.1k tokens of raw turns, every time.

That is where the score goes. LongMemEval_S, shipped search, gpt-5-mini reader, official judge
(`research/runs/graph-v9.14-full500-mpnet8-gemma`): 41 of 500 wrong, and 30 of them are the
three types consolidation is for.

| type | wrong |
|---|---|
| multi-session | 14 / 121 |
| temporal-reasoning | 11 / 126 |
| knowledge-update | 5 / 72 |
| everything else | 11 / 181 |

Tools that consolidate (Mastra, Mem0, Supermemory) do it with an LLM on every write and score
94-95%. Turnbook's position (zero model tokens to save, raw history kept) stays; the question is
whether consolidation *after* the fact, by the user's own workers, closes the gap.

## Three layers

| layer | holds | written by | changes |
|---|---|---|---|
| records (exists) | every turn verbatim, `Did:` lines | capture service, no model | never (append only) |
| **state (new)** | per subject: current values with history, dated events, lists that span sessions; every line cites the turns it came from | a worker, while the user is idle | rewritten as new records arrive |
| work (exists, 1.2) | current task, next action, owner | the workers | claim/checkpoint/complete |

Records stay the evidence. A state line is a claim with sources; if it is wrong, the turns it
cites show it, and the next consolidation can correct it. Deleting a turn (`brain_forget`)
drops the state lines that cite it.

### A state line

```json
{"subject": "Kongi (dog)", "kind": "value", "attribute": "food",
 "value": "salmon kibble", "as_of": "2023-05-20", "replaced": [["chicken kibble", "2023-03-02"]],
 "sources": ["s12:4", "s31:2"]}
{"subject": "trips", "kind": "event", "value": "Jeju, 3 nights", "date": "2023-04-11",
 "sources": ["s19:0"]}
{"subject": "bikes owned", "kind": "item", "value": "road bike (bought 2023-02)",
 "sources": ["s07:3"]}
```

`value` lines answer knowledge updates, `event` lines with dates answer temporal questions,
`item` lines collect what a multi-session question counts or compares.

### Updating the understanding

Incremental, in date order: for each session not yet consolidated, the worker gets the
current state lines that share its subjects (not the whole state) and the session's turns, and
returns state-line edits (add / replace value / add event or item), each with sources. It never
sees a user question. In production any attached worker can do it, as a `brain_work` task: in its own
session, or run on a schedule by the app (a daily Claude Code or Codex task).

### Reading

The hook and `brain_context` put the state lines that match the request first (small: one line
per fact), then raw turns as today. Retrieval of state lines uses the same hybrid search; the
per-message hook floor applies.

## Experiment (before any product code)

LongMemEval_S, the stored haystacks, the shipped search, gpt-5-mini reader, official gpt-4o
judge: everything as in the 91.8% run except the state layer.

- **Arms.** `records` (today's packet, existing answers reused) and `state+records` (state lines
  first, then turns, same 3.1k-token budget).
- **Consolidator.** gpt-5-mini, incremental per session as above, question-blind. Its prompt and
  outputs are saved per haystack (`state.jsonl`), so the state can be inspected.
- **Pilot.** 120 questions: the 30 current misses of the three types plus 90 currently right
  ones of the same types (to see what the state breaks). Cost estimate: ~115k history tokens per
  haystack, incremental ~1.5x with state context: ~20M input tokens, about $5 with gpt-5-mini.
- **Full.** All 500 if the pilot passes.
- **Gate.** On the 500: +2.0 points or more overall, no type down by more than 1 question in 50,
  median packet tokens no higher than +20%. Also report: how many state lines are wrong
  against their own sources (sampled 50, checked by hand).
- Vectors and packets in CI (cache-merge, as before); model calls run locally with the user's
  key, which never leaves the PC (as in the earlier evaluations).

Not decided by this experiment: which worker consolidates in production (Claude or Codex, the
user's subscription), how often, and the user-facing view of state pages.

## Product design (2026-11-02, after a first look at real output)

One consolidation of a real haystack (07741c45, a knowledge-update question the shipped search
misses) gave 158 lines in 182 s, and the answer as a value with its history: old sneakers
"under the bed" (05-25) -> "shoe rack in the closet" (05-29). Lines averaged 254 characters:
too long to put 30 in a 1,200-character hook packet. So: compact lines, and few of them.

### As built (2026-11-02, after Codex's design review)

`src/turnbook/understanding.py`, MCP tool `brain_understand` (next / save).

- **Progress is this PC's**, per scope (`global` when not given; a project scope is separate),
  over turns in the order they arrived here (`conversation_turns.revision`), so a past
  conversation imported late is not skipped. Tables `understand_progress` (last revision,
  generation) and `understand_batches` live in the same SQLite file. One PC should understand a
  scope; others get its lines through sync. Two PCs writing one scope at once is not supported
  (there is no shared lock across PCs).
- **A batch is fixed when handed out**: its turns' ids and hashes (refs t1..tN), the generation
  it started from; turns shown in the order they happened, capped by count and characters.
  Saving no lines marks the turns done.
- **save is all or nothing**: lines checked first, then one `BEGIN IMMEDIATE` transaction writes
  every memory and moves the progress. The model thinks outside it. Two agents from one point:
  the first save wins, the other gets `STALE_BATCH`. The same batch saved again with the same
  lines answers the first result (a lost reply); with other lines, a conflict.
- **Understood facts are grouped apart** from facts the user saved (`attributes.fact` =
  `understood:<state_key>`), so an inference never pushes a user's own fact into "before".
- **Identity**: a server-issued `state_key` per line; a later version is a new memory with the
  same key. History comes from versions (and from `replaced` on a new value line, written as
  older versions), never from text inside the line. No `supersedes` link: recall drops
  superseded memories, which would drop the history.
- **Honest lines**: status fact | plan | uncertain; `date` is when it happened or became true
  and null when not said (never the day it was mentioned); a date still ahead becomes
  `planned_for`. The guide given with each batch says so.
- **Forgetting**: erase takes every memory citing a forgotten turn in `attributes.from_turns`,
  and `remember` refuses one that cites a forgotten turn, whichever way it arrives (a save, an
  import from another PC, brain_remember). A turn forgotten between next and save: the lines
  citing it are skipped.
- **Packets**: a state line shows as `STATE` (confidence 0.7), never stands in for its turns (the
  note rule that drops cited turns does not apply to it), and only value lines show earlier
  values.

Kept up to date by asking an agent or by an app's scheduled task (no launcher of Turnbook's own).
Not yet: ranking state against turns per question kind, stable
attribute names across batches beyond showing existing lines, a measured accuracy effect.

### First real runs (2026-11-03)

Codex 0.160 (gpt-6.1-sol, medium) kept one LongMemEval_S history (07741c45: "Where do I
currently keep my old sneakers?", never shown to it) through `brain_understand` alone
(`research/understand_e2e.py`):

| run | time | batches | lines | median line | notes |
|---|---|---|---|---|---|
| guide 1 | 165 s | 17 | 108 | 85 chars | many `user:` heads; "last Saturday" left undated |
| guide 2 | 168 s | 17 | 116 | 79 chars | subjects name things; "last Saturday" -> 2023-05-27 |

About 170k tokens of the user's Codex subscription per 527 turns.

Per-message hook on the rebuilt store (527 turns + 108 lines, `research/understand_packets.py`):
0.12-0.21 s a message; the question got 1 state line and its turns, four unrelated messages got
no state line (three got nothing at all). Indexing the 527 turns' vectors at low priority took
7.7 min, the capture service's usual cost.

Known limit: a change filed under another subject stays a separate line ("storage: under my bed"
and "organization: store old sneakers in shoe rack (plan)"); the guide asking to update the same
line did not fix it. Both lines carry the day they were said, and the turns come with them, so
the reader can tell which is newer; ranking state against turns per question kind and grouping
by subject are next.
