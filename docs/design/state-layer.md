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
session, or started by the worker runner (opt-in, daily cap).

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

### Storage: the memories Turnbook already has

No new store. A state line is a saved memory (`local_brain_memories`) that already has most of
what it needs:

- `about` + `attribute`: a later value replaces the earlier one and recall shows both (up to
  five earlier values) - that is the `value` kind as it stands.
- `attributes.from_turns`: the turns it was worked out from; a packet that holds the memory
  leaves those turns out.
- `effective_at`: when it became true (an event's date).

Added: `attributes.kind` = value | event | item, and `memory_type = "state"` so they can be
listed, counted and replaced as a layer. Content is one short line written for reading:
`old sneakers: shoe rack in the closet (since 2023-05-29; before: under the bed, 2023-05-25)`.

### Forgetting

Forgetting a turn must take every memory derived from it. Today `erase` follows
`semantic_ingestion_audit` (promoted copies) but not `attributes.from_turns`, so a note written
from a turn the user deleted survives. Fixed first (it applies to existing notes too).

### Updating: one tool, `brain_understand`

- `next(scope?)`: up to N turns not yet folded in (oldest first, a cursor per scope), each with
  a short ref (t1..tN), plus the current state lines for the subjects those turns touch. Text
  is the stored (masked) text, `<private>` already gone.
- `save(batch, lines)`: state lines citing refs; written as memories in one transaction
  (values through about/attribute), and the cursor moves past the batch. A batch saved twice is
  refused (the cursor already moved).

Any attached agent can do it when asked; the worker runner gets a second role, `understand`
(no git tree involved, same opt-in, grant per scope, daily cap). Nothing runs on its own.

### Reading

Unchanged paths: memories already go first in a packet (a quarter of the budget), the hook's
stand-out rule applies to them, `brain_context` finds them by meaning and words. A state line
is one short line, so several fit where one raw turn did.

### Order of work

1. forget follows `from_turns` (bug, independent)
2. `brain_understand` next/save + cursor, tests
3. compact line format, budget check on the pilot haystacks
4. runner role `understand`
5. the accuracy pilot (later; the user asked for features and speed first)
