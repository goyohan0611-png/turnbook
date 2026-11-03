# One brain, interchangeable agents

Status: local prototype in this checkout; not released or installed. Native-app adapters and
automatic quota failover are not implemented. This document separates the working core from
the experience it is meant to enable.

## Experience

The user's PC holds the continuity. Claude Code, Codex and other connected agents are workers
attached to the same brain. Changing a model, app or session should not require the user to
repeat the goal, decisions, progress, constraints or next action. A request such as "continue"
should lead directly to useful work, without narrating a memory lookup.

The same applies to "Codex implements it; Claude tests it": the agents need a shared work board,
not a second dashboard or a new terminal application the user must manage. Memory remains useful
for ordinary conversation, documents and personal information; work tasks are an additional,
optional layer for delegated work.

## Responsibilities

| Turnbook core | Connected app or adapter |
|---|---|
| Persistent conversations, decisions and evidence | Model inference and tool execution |
| Current goal, progress, next action and references | Detecting the app's usage-limit/exit events |
| Task assignment, dependencies and single-owner transitions | Starting or waking an agent where the app supports it |
| Revision checks against delayed or competing updates | Reporting its own app/session identity and checkpoints |
| A bounded handover on supported session-start hooks | Reading and claiming the task before changing files |

MCP provides tools when a model is running; it does not by itself wake a closed application,
push a message into every chat UI, or pool provider credits. Model capabilities and usage charges
still belong to the chosen provider. No new LLM call is needed to store or read a task record.

## Implemented local protocol

`brain_work_status` and `brain_work` use the connected memory server's **local SQLite store**.
Task keys are `(absolute workspace, task id)`. Windows paths ignore case and slash style;
POSIX paths keep case.
No implicit global project, relative paths or cross-project fallback are accepted.

1. `brain_work_status(workspace, task=None)`: see the folder's tasks, or pass a task id to read
   its packet and revision. The indexed list is bounded to 30; unfinished work is ordered first,
   then most recently updated. Completed history cannot push active tasks out of the list.
   First-prototype records are upgraded without losing packets.
2. `assign`: create a READY task for an app (`codex`) or one session (`claude-code:session-id`).
   `depends_on` names existing tasks in this workspace. Assign implementation first, then its
   dependent test/review task. Independent tasks can run concurrently under different ids.
3. `claim`: become the active owner, using the revision just read. Assigned/paused work keeps
   its progress packet. Empty/completed work requires a new objective. Dependencies must be
   complete. A competing claim or stale revision fails without changing the packet.
4. `checkpoint`: the active owner records progress, next action, references (files/commit/diff)
   and checks. A recorded "passed" is a report, not proof that the current files still pass.
5. `pause`: relinquish the work with a next action; optionally name the next app. By default
   any connected agent may claim it. An old timestamp alone never permits taking active work.
   **A worker that stopped without pausing** (a usage limit, a closed app) still holds its work.
   When the user asks another app to continue it, that app claims with `takeover` set to the
   user's words. It succeeds only after the owner has been quiet for 5 minutes: no checkpoint,
   no captured turn and no write to its transcript file (apps write on every tool call). The
   progress, next action and checks carry over; the packet records who it was taken from and
   why. An agent never takes over on its own initiative or on a timer, and the automatic
   launcher (when built) never does.
   Only owners whose app logs every tool call to its own transcript (Claude Code, Cowork,
   Codex) can be shown quiet; for others, or a session not captured yet, the claim answers
   `OWNER_ACTIVITY_UNKNOWN` and the owner has to pause.
   Five quiet minutes do not prove the owner's process ended (one long tool call writes
   nothing): a takeover is a cooperative switch the user asked for, not a lock. Nothing stops
   a worker that comes back from writing files; it only finds the task is no longer its own.
6. `complete`: the active owner marks it complete. A dependent task cannot complete against a
   dependency revision different from the one it claimed; pause, reclaim and recheck instead.
   This also checks transitive inputs: a completed review becomes stale if its implementation
   changes, and cannot unblock a later delivery task until the review is refreshed.
7. `clear`: erase an inactive task packet while keeping the revision counter, so delayed writes
   cannot resurrect it. This does not erase copies already in conversations or an app's history.

`agent` is an app/session label for **cooperative coordination**, not authentication. This is not
a filesystem lock: an agent that ignores the protocol can still edit files. Split overlapping
file changes deliberately, and test a fixed commit/diff. It is not a cross-PC lock: personal
memory sync and team publication do **not** distribute these task records. Multi-PC task
ownership needs an authoritative hub protocol before it can be supported safely.

### Example: implementation followed by testing

The acting coordinator calls the tools, not the user:

```json
{"action":"assign","workspace":"C:/project","task":"implement","agent":"codex:coordinator","expected_revision":0,"assigned_to":"codex","objective":"Implement the requested change","next_action":"Inspect the relevant code and implement"}
```

```json
{"action":"assign","workspace":"C:/project","task":"verify","agent":"codex:coordinator","expected_revision":0,"assigned_to":"claude-code","depends_on":["implement"],"objective":"Review the diff and run regression checks"}
```

Codex claims `implement`, records file/commit references and test results, then completes it.
Claude Code reads and claims `verify`, checks the referenced changes and records its results.
No agent launch is implied by assignment: an adapter must deliver/wake the recipient, or the
recipient picks up its work the next time it runs.

## Session continuity

Claude Code and Codex installed hooks now handle SessionStart (`startup` / `clear` only). Up to
two unfinished local work packets and a short tail of the previous conversation in the same
workspace are included, within the hook's existing deadline. Task packets are useful even if no
conversation was captured yet. App/session pairs are namespaced; documents and other workspaces
are never used as the previous session. A resumed/compacted session keeps its own history.

This is recorded context, not executable instructions or new user authorization. Before acting,
the new agent reads current status and relevant files; it should not blindly trust a prior
model's claimed success. The user need not be asked to restate context already present.

## Starting a worker

No launcher of its own: an attached agent starts another with its official CLI when the user
asks ("Codex랑 같이 해"; the instructions name the CLIs found on the PC), and recurring work
(keeping the understanding up to date) goes to the apps' own schedulers. A launcher in the
capture service (1.2.0 development) was removed before release: the apps already schedule, and
supervising processes it starts was where most review findings were.

## Privacy and interruption

Task text uses the same masking and explicit `<private>` omission as captured conversations.
Keep source pointers rather than full transcripts in the packet. Task records are local work
notes, not `brain_remember` facts or team checkpoints; use `brain_work clear` for their packet,
and the existing deletion tools separately for conversation/memory copies.

Checkpoint before a long action and after a meaningful result. An abrupt model/app stop can
leave an older checkpoint. The previous conversation and `Did:` records help recover what
actually happened; absent evidence must remain unknown. An adapter may release an owner only
after establishing that it stopped, never merely because a timer expired.

An explicitly empty objective is invalid; an explicitly empty next action cannot be used to
pause, even if an older action exists. Omitting these fields preserves the existing values.

## Next integration gates

- **Approval prompts.** Board reads use `brain_work_status`, marked read-only, non-destructive,
  idempotent and local, so looking at tasks or a packet needs no write approval. `brain_work`
  accepts only writes and remains destructive because it can clear packets.

- Two real installed apps on one fresh fixture store: implement, pause, switch, continue without
  a repeated user explanation; then assign a separate test task and verify the exact diff.
- Quota/exit adapter: detect a stopped worker, checkpoint what is known, pause, notify a permitted
  replacement, and prevent the old worker from resuming the same task unclaimed.
- Notifications must distinguish queued, delivered, running and completed; enqueue is not proof
  the agent has read or executed anything. No busy polling or unsolicited status chatter.
- Publish the package before pointing public plugin hooks at its pinned version. Refresh the
  separately hosted Codex marketplace after release. Test fresh install and upgrade per OS.
- Larger task board: pagination, workspace moves, human-visible repair after an abandoned owner,
  and stronger identity/permissions before any multi-user or cross-PC orchestration.
- Apps launched in different folders need an explicit, verified project/workspace mapping before
  task context can follow them. The startup hook currently uses the app's exact working folder;
  it must not silently pull another workspace's tasks just because the repo names look similar.
