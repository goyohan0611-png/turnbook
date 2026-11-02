# A state layer: the brain's workers consolidate while it sleeps

Status: design and experiment plan (2026-11-02). Nothing implemented. Decided by the user after
the "one brain, interchangeable workers" direction; to be kept only if the experiment below
passes its gate.

## Why

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

### Consolidation ("sleep")

Incremental, in date order: for each session not yet consolidated, the worker gets the
current state lines that share its subjects (not the whole state) and the session's turns, and
returns state-line edits (add / replace value / add event or item), each with sources. It never
sees a user question. This is what an idle-time job on the user's PC would do after each
session; in production it is a `brain_work` task the worker runner (opt-in, daily cap) starts.

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
