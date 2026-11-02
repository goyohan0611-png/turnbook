# Work bundles: keep, back up, hide and delete one piece of work

Status: proposal, not built. For review.

## Why

Memory only grows. The LoCoMo study (research/runs/graph-v9.20-learning) shows recall in the first
30 turns falling from 83.5% to 80.4% as one memory grows tenfold, even with the learned re-ranker.
Users need to take finished or unwanted work out of the way, and to do it in one step: everything
said about one piece of work, whichever app or model it was said in.

## What already exists

- Every conversation turn stores `client` (the app), `client_session_id`, `scope`, `cwd`,
  `happened_at`. Every curated memory stores `scope`.
- `scope` is the project of the folder the app was working in (`workspace_scopes`), else `global`.
  Claude Code and Codex in the same folder already share a scope.
- `brain_forget` deletes chosen ids everywhere: this PC, the search indexes, the shared folder or
  hub, the user's other PCs (tombstones and retried notices, `forget.py`). It is built for secrets:
  search, pick up to 10, delete.
- Not stored: which model answered.

## Proposal

A **bundle** is a scope (project). One new MCP tool, `brain_bundle`, with actions:

| action | what it does |
|---|---|
| `list` | bundles with turn and memory counts, apps, first and last date |
| `show` | one bundle by app and by month, a few masked previews |
| `assign` | put a session (or a date range of one app) into a bundle; for chats with no folder |
| `export` | write the bundle to one file (below) |
| `import` | read such a file back; merging by id, so importing twice changes nothing |
| `hide` / `unhide` | leave it out of recall, keep it stored; reversible |
| `delete` | delete it everywhere through `forget_ids`, after an automatic export |

Every action that changes something takes the same filters, all optional except the bundle:
`bundle`, `app`, `session`, `since`, `until`. It is always two calls, as `brain_forget` is: the
first returns counts and masked previews, the second, with the token the first returned, acts.

### Ideas taken from other projects

- **Mem0** (`delete_all` with `user_id` / `agent_id` / `run_id`): bulk delete by scope, and a call
  with no filter is refused. Here: `delete` without a bundle is refused; deleting everything needs
  the explicit bundle `"*"`.
- **Letta** (Agent File `.af`): a whole agent, memory included, as one portable file. Here: the
  export file below.
- **MCP Agent Memory**: an automatic backup before clearing, the last 10 kept. Here: `delete`
  exports first to `~/.turnbook/backups/`, keeping the last 10.

### Export file

A zip with `manifest.json` (bundle, filters, counts, date range, Turnbook version, sha256 of each
part) and `turns.jsonl`, `memories.jsonl`, `code_changes.jsonl`, one record per line, exactly as
stored. Plain by default; with a passphrase, encrypted with the same `cryptography` primitives the
hub already uses. Never uploaded anywhere by Turnbook.

### Hiding

A `hidden_bundles` table (bundle, since, until, app). `gather` adds the condition to both searches,
so hidden turns never enter the pool. The semantic index keeps their vectors, so `unhide` is
instant.

### Assigning chats without a folder

Desktop chat apps have no `cwd`, so their turns land in `global`. `assign` records
`session -> bundle` in a small table; capture applies it to later turns of that session, and the
action rewrites `scope` for turns already stored. The model can call it when the user says "this
chat is about Turnbook".

### Later

- Store the answering model per turn where the transcript has it (Claude Code: `message.model`;
  Codex: the session's model), so a filter by model becomes possible.
- A weekly suggestion: bundles untouched for N months, offered for hiding.

## Tests

One per action; a round trip `export -> delete -> import` that restores the same ids and the same
recall result; a hidden bundle never in `gather` output; `delete` with no bundle refused; the
deletion reaches a second store through the shared folder (as `test_forget_retry.py` does).
