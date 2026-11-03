# Changelog

Upgrade: `python -m pip install --upgrade turnbook`, then `turnbook-install`.
Memory is kept across upgrades (checked in CI on Windows, Linux and macOS).

## 1.2.0 — 2026-11-03

- **Your rules, for every agent.** Tell any agent a standing rule ("항상 한국어로 답해"): it is
  saved in the brain, and Claude, Codex, Gemini and OpenCode all start their next session with
  it. A changed rule replaces the old one.
- **Forgetting a turn also forgets what was built from it** (memories citing it in
  `from_turns`).
- **Reading the work board asks for no approval.** `brain_work_status` reads; `brain_work`
  keeps the writes.
- **Agents know which other agents are on this PC.** The instructions name the agent CLIs found
  (`codex exec`, `claude -p`, `gemini -p`, `opencode run`, `copilot -p`), so "Codex랑 같이 해"
  needs no explanation; the caller puts the task in the prompt and records the result.
- **One brain, interchangeable workers.** A worker that stopped (a usage limit, a closed app)
  can be continued in another app when the user asks: `brain_work` claim with `takeover` set to
  the user's words, once the owner has been quiet 5 minutes by its own transcript. "Continue"
  now looks at the folder's work board first. Completing drops a stale next action. Checked with
  the real Claude Code and Codex CLIs on a throwaway store: Claude paused half-way, Codex
  finished from "이어서 해줘" alone, a new Claude session verified from "맡은 작업 있으면 해줘".
- **turnbook-view** shows the brain's work in progress at the top of Sessions & work.
- **`<private>` written as code** (`` `<private>` ``) is a mention, not a tag: an unclosed one
  dropped the rest of a message that only talked about the feature.

- **Follow-up continuity checks.** Empty edits cannot erase a task's objective or its required
  handover action. Open tasks stay ahead of completed history in the indexed work queue (the
  first prototype's rows are upgraded in place). Dependency freshness is checked through the
  whole chain, so a completed review of an older implementation cannot unblock later work.
  Added independent-process contention, killed-transaction recovery, upgrade and stdio-MCP
  integration tests.
- **Close shared-memory read connections cleanly.** Cached Postgres readers are closed at process
  exit and when replaced after a dropped connection. A connection that fails during initial
  table setup is closed too. Test fixtures also close their ledgers and reap their dummy capture
  processes, so shutdown warnings identify actual leaks rather than test leftovers.
- **Continue across agents, and split implementation from verification (local prototype).**
  `brain_work` keeps a task's goal, owner, progress, next action, file/commit references and checks
  in this PC's memory store. Assign separate tasks to apps or sessions, optionally depending on
  completed tasks. Revision checks reject competing claims and delayed writes; verification
  cannot complete against a dependency changed since it started. No automatic app launch,
  quota failover or cross-PC task synchronization is included.
- **New sessions start with the last work.** Claude Code/Codex installer hooks add SessionStart;
  the Claude plugin has it too. Startup and clear receive bounded work packets and a short tail
  from the previous conversation in the same folder. App/session ids stay separate; documents
  and other folders are excluded. The hook exits within its deadline even if work is still slow.
- **Explicit private blocks.** `<private>...</private>` is removed before capture, Gemini and
  OpenCode staging, explicit saves, indexing and publication. An unclosed block hides the rest;
  all-private messages are skipped, even with secret masking off. Original app histories and
  text saved by an older version are not changed.
- **Sessions & work in the local memory view.** A read-only timeline, project/text filters and
  work-only `Did:` view alongside the graph, including turns not indexed yet. `--no-text` omits
  the timeline. It remains usable without the 3D library. Browser visual QA is pending; data,
  no-text isolation and JavaScript syntax checks pass.
- **Comparable metrics, explicitly labeled.** Frozen Gemma packet session hit@5: 466/470;
  hit@10: 470/470, excluding 30 abstention questions. Offline replay with input hashes, not a
  current-checkout retrieval run, answer accuracy or a new blind result. The older retrieval
  baseline is now dated so the two cannot be mistaken for the same run.
- **Saved memories on each message: only those that stand out.** Saved memories are short, and
  on short Korean text every one scores 0.3-0.45 against any message, so the 0.36 floor (measured
  on conversation turns) let a kiosk price list through, and memories sharing only everyday
  words ("그러면", "이제") skipped it. A memory now goes in when it stands 0.2 above the mean of
  all memories for that message (with fewer than 10 memories: the floor). On this PC's 32
  memories: the answering memory stood 0.28+ above, the best unrelated one 0.17 at most.

## 1.1.0 — 2026-11-02
What the agent did is remembered, the memory added to each message is held to a measured floor,
and Turnbook is a Claude Code plugin. Upgrading: `pip install -U turnbook && turnbook-install`,
then restart your AI apps.

- **What the agent did is remembered.** Claude Code's and Codex's tool calls were dropped; now
  each stretch of work between two replies is kept as one turn: `Did:` with the files edited
  and the commands run (first line, and the agent's own description of it in Claude Code).
  Reads, searches and tool output are still left out. Kept once the transcript has been quiet
  for two minutes, so a long run is one line, not one per scan (on this PC: 8,499 tool calls,
  3,247 lines, next to 6,163 replies).
- **Less unrelated memory on each message.** The hook added the 4 nearest items to every
  message, related or not. It now keeps only those at least 0.36 similar (EmbeddingGemma; a
  turn found by its words is kept) and drops past messages that repeat this one ("얼마나 걸려"
  for "얼마나 걸리나"). Measured on LongMemEval_S 500 (`research/hook_floor.py`): the answer
  is in 74.8% of packets (78.8% before), and asked of someone else's history, 65.8% of
  packets are now empty (0% before). `brain_context` is unchanged.
- **Claude Code plugin.** `/plugin install turnbook --marketplace goyohan0611-png/turnbook`
  runs the memory server and the message hook through `uvx` (new command `turnbook-hook`).
  With `turnbook-install` too, the plugin's hook stays quiet so the memory is added once.

## 1.0.0 — 2026-11-02
Turnbook 1.0: the name everywhere, the memory in `~/.turnbook`, and the fixes of the review
that led up to it (0.10.5). Upgrading: `pip install -U turnbook && turnbook-install`, then
restart your AI apps.

- **Everything is called Turnbook now.** The Python package is `turnbook` (`graph_mind` stays as
  an alias, so apps, hooks and Startup entries written before keep working until
  `turnbook-install` rewrites them); settings are `TURNBOOK_*` (`GRAPH_MIND_*` still read); the
  memory server apps show is `turnbook` (was `graph-mind-memory`); the Startup shortcut is
  `Turnbook.lnk` (macOS: `com.turnbook.capture`); OpenCode's files are `turnbook.md` and
  `plugins/turnbook.js`. The installer replaces each old entry instead of adding beside it.
- **The memory folder is `~/.turnbook`.** An existing `~/.graph-mind` is moved, with the paths
  written inside it, when nothing holds a file in it: by `turnbook-install` (it stops the
  capture service and this PC's hub first) or at the next login, when the capture service
  starts before the apps. Until then it is used where it is. Once moved, `~/.graph-mind` is a
  link to `~/.turnbook`, so an app's memory server still on old code until the app restarts
  writes into the same memory instead of starting an empty one.
- **A new kind of app block shows up in the capture status.** A user turn that still begins
  with a `<tag>` after the known blocks are taken out is stored as before and counted per tag
  (`unknown_blocks` in the status), so the next thing an app starts putting into its "user"
  messages is seen the first day. Tests now use transcripts shaped like the real ones.
- The README shows this version's results only; the memory-growth chart is redrawn with
  EmbeddingGemma (to 55,882 turns; larger sizes are being measured).
- `.github/smoke_install.py` run by hand uses a temp folder, never the real memory.
- **Development notes from before 0.8 are no longer answered as memories.** The feature that
  wrote them went in 0.8, but search still read its table, so an agent's task summaries from
  then took the curated-memory slots of hook packets. The notes stay in the file, unread.

## 0.10.5 — 2026-11-02
- **A synced folder holds only ciphertext.** A memory kept in a OneDrive, Google Drive or Dropbox
  folder was written there as plain text; its lines are now sealed like a hub's. The first PC
  makes a password and shows it in its terminal; each other PC types it once at
  `turnbook folder <folder>` (kept in its own config, never in the folder). Lines a PC wrote
  before are sealed when it next chooses the folder; a PC without the password neither reads nor
  writes there, and its turns wait on that PC. Found by an outside review (WearyMonk8).
- **The installer asks first.** At a terminal, `turnbook-install` lists the settings it will
  change (every AI app's memory server, the per-message hook, the login item) and waits for a
  yes; `--yes` skips it, and scripts without a terminal are not asked. `curl ... | sh` asks too
  (through the terminal, as stdin is the script).
- **`turnbook-view` runs only the 3D library it was checked against.** The page holds the whole
  memory, and the library came from a CDN unchecked: a CDN serving other code could have read
  every memory in it. The download is now pinned by SHA-256, and the browser fallback carries an
  integrity hash.
- **The per-message memory says it is a record, not instructions,** so a model does not act on a
  request that happens to sit in an old conversation or a shared memory.
- Anthropic keys (`sk-ant-…`) are masked as `[REDACTED_ANTHROPIC_KEY]` (they were masked before,
  under the OpenAI label).
- **A compaction summary is no longer stored as the user's words.** When Claude Code (or Cowork)
  compacts a long conversation it writes the model's summary as a "user" record; Turnbook
  stored it as something the user said, thousands of characters of AI summary each time,
  against its own rule of keeping only what was said. Those records are skipped now, and
  the ones stored before are erased once (by id, or by the text's hash for turns stored before
  0.9.2). Codex automations' heartbeats and its apps' page state, also sent as "user"
  messages, are kept out the same way (150 were stored on one PC).
- **No more memories made by keyword.** The capture service turned a turn into a "decision",
  "preference" or "project principle" when it held Korean words such as "앞으로" or "하기로":
  a question without a question mark, or a pasted group chat, became a decision, and no
  English sentence ever did. It no longer runs; the model judges what to keep and saves it
  with `brain_remember`. The memories it made are erased once (the turns stay); the ones
  made because the user said "기억해" stay.
- **Copies of the same turn stored before 0.9.2 are dropped once.** Back then a turn's id came
  from its row number in one read, so a transcript read again stored every turn a second
  time; two of a hook packet's four slots could be the same sentence. The same app,
  conversation, speaker, time and text is one turn; the first stays. A copy a memory was
  promoted from is kept, so no memory goes with it.
- **Forgotten text leaves the file, not just the tables.** SQLite only marks a deleted row's space
  free, and the word index kept a deleted word until a merge: after `turnbook-forget`, the phrase
  was still in the memory file's bytes (8 copies in a test). Freed space is now zeroed on every
  connection (no measurable cost), the word index drops a forgotten word at once, and the
  write-ahead log is emptied after forgetting. Run `turnbook compact` once to clear leftovers
  from before.
- **Code capture keeps only the current version of each file.** Every past version was kept
  as a snapshot (169 MB for a 182-file project in three weeks), though only the latest is
  read; the patches keep the history. Old ones go at the next change.
- **One search model.** EmbeddingGemma reads English and Korean better than mpnet, so mpnet is no
  longer offered (`turnbook model multilingual` is gone from the help). Once a memory has been
  read again with gemma, the capture service deletes the models Turnbook downloaded that
  nothing reads (mpnet ~290 MB and its vectors, the 0.9.9 model ~450 MB); before, only
  `turnbook-install` did, and an upgrade by pip alone kept them. A model you chose with
  `turnbook model REPO@FILE` is never deleted.
- **The per-message memory leaves out earlier messages with exactly the same words.** A short
  reply such as "했어" or "ok" brought back four older copies of itself (~1,500 characters of
  nothing) with every message. Text equality only; every message is still searched.
- `turnbook-uninstall` also deletes the two files that are Turnbook's by name and were left
  empty: Copilot CLI's `hooks/turnbook.json` and OpenCode's `graph-mind.md`.
- A hub's Postgres log no longer gets a line per checkpoint (two thirds of it, ~60 KB a day).
- Docs: a careful install (read the script, pin a version), and what is not encrypted on disk.

## 0.10.4 — 2026-10-30
- **The hook's memory is never stored again as the user's words.** Copilot CLI keeps a prompt its
  hook extended as the user's message; read back, every packet would have come back as something
  the user said. User text is now cut at Turnbook's own packet header, whatever the app.
- **Other PCs' conversations reach the per-message memory within a minute.** Only a tool call
  brought them in once the hook stopped waiting on the shared hub (0.10.2); the capture service
  now syncs the shared folder or hub every 60 s (about 2% of a CPU core on a hub PC).
- OpenCode gets the same 3 s memory deadline as the other agents (it could wait up to 15 s).
- Project documents: a locked file, or a cloud file not downloaded yet, no longer stops every
  document after it; a text file saved in the PC's own encoding (ANSI, e.g. CP949 on a Korean
  Windows) keeps its letters instead of turning into replacement characters.

## 0.10.3 — 2026-10-30
- **Memory added to every message, a little.** The per-message hook (Claude Code, Codex, Copilot
  CLI, Gemini CLI, OpenCode) used to search only when the message had a word like "remember" or
  "last time", and missed questions such as "what did I have for lunch today?". Whether a message
  needs the past is now left to the model: every message gets a small packet (4 items, 1,200
  characters, about 300 tokens), and brain_context gives more. About 0.85 s per message with the
  capture service running.
- The packet starts with the current date and time, so "today" can be told from the dated lines;
  a saved fact is no longer printed twice (title and text), and the hook's sources show a file
  name instead of a full path: six items fit where four did.
- **Code capture no longer stops when the watched folder changes.** Changing the folder, the file
  kinds or the exclusions left the old manifest in place, every pass failed and nothing more was
  recorded; it now starts over from the folder as it is.

## 0.10.2 — 2026-10-30
- **A message never waits more than 3 s for its memory.** With the capture service down, a hook
  that searched by itself loaded the model first and held the message 7-22 s; now the message
  goes on without memory after 3 s. A hook no longer tries a service that has died (2 s each).
- The capture service reads the search index when it starts and keeps it (about 170 MB on an
  11k-turn memory), so a message right after a quiet half hour gets its memory in time too.
- A long pasted message is searched by its first and last 500 characters, where the question
  usually is: 4,000 characters took 2.2 s, now about 0.8 s.

## 0.10.1 — 2026-10-30
- **Memory added to each message, faster.** The capture service now answers the per-message hook
  from its own loaded model and search index, so a message that asks about the past waits about
  0.4 s for its memory instead of 1.5-2.5 s (measured on a real memory on Windows). Without a
  running service, or with an older one, the hook searches by itself as before. The service lets
  go of the index after 30 minutes without a request.
- The hook no longer connects to a shared Postgres hub on every message; the memory servers
  already bring in other devices' lines.

## 0.10.0 — 2026-10-30
- **EmbeddingGemma for every memory.** 0.9.48 gave it to new installs; a memory from 0.9.47 or
  before now moves to it too. It keeps searching with mpnet while the capture service reads it
  again with EmbeddingGemma, 64 pieces at a time when nothing else is due, then switches by
  itself, so search never gets worse on the way. `turnbook model multilingual` keeps mpnet.
- The model preset is called `gemma` (`english`, its name in 0.9.48, still works).
- **Project documents.** `turnbook docs add FOLDER` keeps the folder's Markdown and text files
  searchable beside your conversations, split at headings and updated as they change; a replaced
  section is marked with the date it was replaced. `turnbook docs remove FOLDER` stops it.
- **Memory added to each message** in Claude Code, Codex and Copilot CLI: a hook looks up what
  the message needs from the past and adds it before the model sees it, as Gemini CLI and
  OpenCode already did. Codex asks you to trust the hook once in `/hooks`.
- The capture service runs below normal CPU priority.
- Benchmarks: the 40-question comparisons with MemPalace and Mem0 are removed; 40 questions are
  too few to rank systems.

## 0.9.48 — 2026-10-30
- **A more accurate search model for new installs: EmbeddingGemma** (Google, 300M, 4-bit ONNX,
  about 200 MB). On LongMemEval_S it answers 91.8% of questions (459 of 500) from a
  3.1k-token packet; the same packet with mpnet scored 86.6%, and mpnet needed 26.3k tokens to
  reach 91.8%. On LoCoMo, scored on conversations the re-ranker never saw, the answer turn is among
  the first 10 found for 83.7% of questions (mpnet 70.2%). In Korean it finds the answer by
  meaning almost as well as in English (86.9% against 90.2% in the first 30; mpnet 67.5%).
  EmbeddingGemma is provided under the Gemma Terms of Use (https://ai.google.dev/gemma/terms).
- **Existing memories keep mpnet**, so the update reads nothing again. `turnbook model` shows the
  model in use; `turnbook model english` moves to EmbeddingGemma, `turnbook model multilingual` to
  mpnet (the memory is read again once, in the background, at half pace).
- Each search model has its own re-ranker weights, trained on LoCoMo and LongMemEval together.
- An app's memory server holds about 250 MB less with EmbeddingGemma (525 MB against 772 MB); a
  search takes about 40 ms (26 ms with mpnet).
- `brain_context` hands over up to 60 turns and 30,000 characters by default (was 30 and 15,000).
  With mpnet that took LongMemEval_S from 86.6% to 89.6%; set `max_chars` for smaller packets.
- Memory tools now warn when automatic capture has not run for 15 minutes, so you learn when
  conversations stop being saved.

## 0.9.47 — 2026-10-28
- **Recall holds up better as memory grows.** The learned score that orders what search finds
  now also weighs how rare the shared words are and how much of one past conversation was found,
  and each search brings 100 candidates instead of 50. In a memory of 205,000 turns (LoCoMo inside
  other conversations) the answer turn is among the first 30 for
  79.7% of questions (73.0% with 0.9.45's fixed blend); a search there takes about 0.4 s. The
  weights were trained on LoCoMo and LongMemEval together.
- **Korean word search sees past particles:** "콩이를", "콩이는" and "콩이가" find "콩이". On LoCoMo
  translated into Korean, the answer turn is among the first 30 for 77.5% of questions (was 74.4%).
- The search model is unchanged, so nothing is re-read after the update.
- Sharing a memory between PCs: a hub that was still starting no longer fails the first connect.

## 0.9.46 — 2026-10-27
- **Recall puts the right conversation nearer the top.** The turns found by word search and by
  meaning are now ordered by a small learned score (how high each search ranked a turn, how
  close its meaning is, and how many of the question's words it contains) instead of a fixed
  blend. On LongMemEval_S the answer now reaches the model for 98.3% of questions (was 97.5%)
  and 94.2% of answer turns (was 92.4%), with packets the same size. The weights were learned on
  a different benchmark (LoCoMo), so this is not tuned to the test. Questions with a date range
  are ordered as before.

## 0.9.45 — 2026-10-27
- **Turnbook is now under the Apache-2.0 license** (was AGPL-3.0). You can use, change and ship it
  in any product, open or closed, including a hosted service, without publishing your changes.
  The CLA and the separate commercial license are gone; they are no longer needed. Versions up to
  0.9.44 stay under the AGPL-3.0.
- The demo in the README is the live recording again, and the README images now show on PyPI.

## 0.9.44 — 2026-10-27
- **Graph-MIND is now Turnbook.** The package is `turnbook` and the commands are `turnbook`,
  `turnbook-install` and so on; the repository moved to github.com/goyohan0611-png/turnbook (old
  links redirect). To move over: `pip uninstall graph-mind-memory`, `pip install turnbook`, then
  `turnbook-install`. Your memory stays where it is (`~/.graph-mind`), and the old `graph-mind*`
  commands keep working, so apps, hooks and startup entries set up before need no change.
- A new logo.

## 0.9.43 — 2026-10-24
- **A deletion reaches your other PCs even if Graph-MIND stops halfway.** Forgetting something
  now records, in the same step, that the other PCs still have to be told (ids only, never the
  deleted text). If the app or PC stops before the notice goes out, it is sent on the next start.
- **A shared-folder deletion that cannot finish no longer repeats every 5 seconds.** The notice
  is sent once; if a log file stays locked (OneDrive) or offline, cleaning it up is retried after
  1 minute, then less often, at most hourly.
- The package description no longer lists app names.

## 0.9.42 — 2026-10-23
- **The vector cache takes half the memory.** Vectors are held as float16 in memory (the file
  stays float32, so existing caches read unchanged). At a million pieces that is 1.5 GB instead
  of 3 GB for the capture service and for each app's memory server. Search scores move by less
  than 0.001, which leaves the ranking as it was. The search matrix stays float32: as float16,
  search would be about ten times slower.

## 0.9.41 — 2026-10-23
The last two findings of the stability review: small, but they only ever grew.
- **Search questions are no longer kept in the vector cache.** Every distinct question stayed
  in it for good, in memory and on disk, though a question is rarely asked twice. Stored
  conversations are cached as before.
- **The OpenCode plugin keeps only its newest 1,000 message ids** instead of every one since
  OpenCode started. Run `graph-mind-install` to rewrite the plugin.

## 0.9.40 — 2026-10-23
The two stability findings 0.9.39 left open.
- **A turn that could not be written to a shared folder is no longer lost to the other PCs.**
  When OneDrive (or any sync app) held the log, the turn stayed on this PC only, and nothing sent
  it later. It now waits in the same outbox a downed hub uses and goes out with the next turn.
- **A code-change manifest that could not be saved no longer records every change again on
  each poll.** A change's id now comes from the state it starts from, not the scan's clock, so
  the next scan finds the same id already kept and skips it.

## 0.9.39 — 2026-10-22
Running all day without trouble: fixes from a stability review (Codex gpt-6.1-sol).
- **A hub that stops answering no longer stalls capture.** Only connecting had a time limit;
  a call cut off by Wi-Fi or VPN, or waiting on another writer, could hold every app's capture.
  Calls now give up (2 minutes per statement, 30 s for a lock, dead connections noticed in
  about a minute) and the lines wait in the outbox.
- **While the hub is down, each turn no longer re-reads the whole outbox** and waits out
  another connection attempt: it is tried again after a minute.
- **A line cut off in the outbox (crash, full disk) no longer swallows the next one.**
- **Moving from the old hub read position no longer locks itself** and repeats on every sync.
- **Saved code is written whole or not at all.** A copy cut off mid-write was trusted forever.
- **Indexing an imported history reads 1,000 rows at a time**, not every unindexed row at once.
- **One failed pass, or a status file that cannot be written, no longer stops the capture
  service.** It tries again a little later.
- **The status file is written once a minute when nothing changed** (every 5 s before).
- **Memory servers idle for 30 minutes also close their hub connection**: Codex keeps old
  sessions' servers running, and each held one.

## 0.9.38 — 2026-10-22
- **Each memory server takes about 230 MB less memory.** numpy's math library reserved a
  buffer for every CPU core when it loaded (233 MB on an 8-core PC), in every server an app
  starts, and in the capture service. It now runs on one thread: a search over 60,000 pieces
  still takes about 20 ms. Codex starts a new server per session and keeps the old ones, so
  this added up to gigabytes.
- **A memory server unused for 30 minutes lets go of its search index and vectors** (and the
  model, if it had to load its own). The next question reads them back, about a second.
- **Publishing to a team project no longer fails on text with a card-like number.** The
  secret check before sharing crashed on the card-number rule instead of checking it.

## 0.9.37 — 2026-10-22
Fixes for the rest of Codex review round 10 (sharing across your PCs).
- **A hub with a key refuses lines that are not sealed.** Lines from before sharing are
  sealed when sharing starts, so only a line added without the key was refused: such a line
  could plant a memory or a forget notice.
- **A shared-folder log swapped for another file is read again from the start.** The read
  position now carries a fingerprint of the 4 KB before it. A copy of equal or greater length
  (from the sync client, or another folder's log of the same name) used to be read on from the
  old position or skipped. Imports are idempotent, so reading again is harmless.
- **A memory that replaced a forgotten one now reaches a new PC.** It waited forever for the
  memory it replaced, which was gone.
- **A forget notice received later also clears the hub.** If the PC that forgot could not
  reach the hub then, the text stayed in the hub's rows; every PC that receives the notice now
  tries again.
- **A large backlog is read in steps** (8 MB or 2,000 rows at a time), and forgetting reads
  the shared log line by line, so a big history no longer has to fit in memory at once.

## 0.9.36 — 2026-10-22
- **Wrong "deleted" code records from 0.9.31 are cleaned up.** Files of a newly skipped
  virtual environment were recorded as deleted while still on disk (7,394 on one PC). At
  start, the service drops each such record whose file is still there, with its diff.
- **A removed teammate cannot keep a project's rights through SET ROLE** (Codex round 9
  re-check). Members now get project rights without SET ROLE (Postgres 16), so removing them
  ends the rights in every session at once. Ending their sessions instead had a gap a
  reconnect could use, and an owner could cut the host's own memory sessions with it.
- **The installer recognises Graph-MIND's Codex table however its key is written**
  (`"graph-mind-memory"`), and leaves other people's tables with similar names alone.

## 0.9.35 — 2026-10-21
- **Sharing your existing memories works after moving to a new folder or hub.** What had
  been sent before was remembered without where it went, so choosing a new, empty folder and
  sharing existing memories sent nothing. The new destination's own contents now decide.

## 0.9.34 — 2026-10-21
- **Moving to another hub no longer skips its first memories.** The read position was kept
  for "the hub", not for each hub: after reading hub A up to its 100th line, joining hub B
  skipped B's first 100. Each hub now carries an id and the position is kept per hub.

## 0.9.33 — 2026-10-21
Fixes from Codex review round 10 (sharing across your PCs, the shared model).
- **A crash in the middle of writing a shared log line no longer costs the next record.** The
  next record was glued to the half line, and both were skipped.
- **One malformed record no longer stops all syncing.** A record with the wrong shape raised
  on every sync, and nothing after it was ever read.
- **A malformed connection code is refused** before it replaces your folder setting and the
  queue of lines waiting for the hub.
- Lines queued while the hub is unreachable are flushed to disk at once.
- The capture service lends its model to every app again even while some other local program
  holds a connection open without answering; a cache of broken (NaN) vectors is now noticed.

## 0.9.32 — 2026-10-21
- **A newly skipped folder is not recorded as deleted.** With 0.9.31, a workspace whose
  virtual environment had been watched recorded every file in it as a deleted file on the
  first scan (7,394 changes and five minutes of work on one PC). A file that still exists but
  is no longer watched is now simply dropped.

## 0.9.31 — 2026-10-21
- **Code capture skips Python virtual environments by any name.** A folder holding
  `pyvenv.cfg` is not walked: a `.venv-rivals` with 16,000 files was walked every two minutes
  (2 s each time).
- The capture service's status file (`~/.graph-mind/automatic-capture/status.json`) records how
  long each step of a pass took (`seconds`), so a busy service can be explained.

## 0.9.30 — 2026-10-21
Security fixes for team project hubs (Codex review round 9). Upgrade the hub's PC first.
- **A teammate could become the hub's database superuser.** The hub's privileged functions
  looked up their tables with the caller's temporary tables first; a teammate's temporary
  table with the right name ran its commands with full rights. Temporary tables come last now,
  and teammates cannot create them on the hub at all.
- **A crafted team invite could take the hub's password.** The login and address in an invite
  went into the connection unchecked, so extra connection options could send this PC's hub
  password to another server. Invites with anything but a plain login and address are refused.
- **Removed or downgraded teammates are cut off at once.** A session that had switched to a
  project's group kept its rights after the teammate lost them; their open sessions now end.
- **The hosting person's account can no longer be removed** (that deleted their personal
  memory tables), and a leaving owner's projects cannot be handed to themselves.
- **The audit log shows who really wrote each entry**, not the name the writer typed.
- **Keys in a checkpoint's references or attached link are caught** like keys in its text.
- **Leaving a project yourself works** (it was rolled back every time).
- Installer: re-installing no longer breaks a Codex config that writes Graph-MIND's table with
  quotes, and the Windows hook command keeps a `$` in the Python path literal.
- **The capture service is quiet again while you chat.** Since 0.9.27 it looked through every
  process on the PC each time a turn arrived (4 s of work on Windows, 8% of a core while
  chatting); it now looks at most every 10 minutes, at Python processes only (0.5 s).

## 0.9.29 — 2026-10-21
- **`graph-mind compact` shrinks the memory file.** Compressing conversations frees space
  inside the database, which new conversations reuse, but the file keeps its size. Run this
  once the capture service has compressed them (after restarting your AI apps) to give that
  space back to the disk. It needs about the database's size in free disk and refuses
  otherwise; an interruption leaves the old file intact.
- **Project export no longer picks up conversations from another kind of PC.** A memory
  imported from Windows and exported on a Mac or Linux (or the other way round) could count
  a conversation held in an unrelated folder as part of the project, and put its text in the
  file. A folder that is not a full path on this PC now never matches.
- `brain_recall(source=...)` keeps to `scopes` when they are given.

## 0.9.28 — 2026-10-21
- **Export one project's conversations.** `graph-mind export FILE --project FOLDER` writes only
  the conversations held in that folder or below it, with the memories drawn from them, for
  handing one project's history to someone without the rest of your memory. Facts saved on
  request ("remember that ...") carry no folder and are not included. Asking your AI ("export
  just this project's conversations") gives you the command.

## 0.9.27 — 2026-10-21
- **Conversations take about half the disk space.** A turn of 200 bytes or more is stored
  zlib-compressed; reading the 30 turns of one question unpacks in under a millisecond, and
  word search, forgetting, sharing and export work as before. Existing turns are compressed by
  the capture service, a little at a time. It starts only once no Graph-MIND process from an
  older version is running (restart your AI apps after upgrading), because older code would
  read a compressed turn as empty. The database file keeps its size; the space freed is reused
  for new conversations, so it grows about half as fast.

## 0.9.26 — 2026-10-21
- **Your AI can open the conversation behind a memory.** A context packet shows one excerpt
  of each conversation turn, so an answer in the rest of that turn, or in the reply right after
  it ("which jobs did you list for my dad?"), was out of reach and the AI said it didn't know.
  Now `brain_recall(source=...)` returns the cited turn in full with the turns around it, and
  the AI is told to look there before saying it doesn't know. Measured on LongMemEval: on par
  overall (29 better, 23 worse over two runs), better on date questions (13 / 3) and on what
  the AI itself said earlier (5 / 2).

## 0.9.25 — 2026-10-21
- **Project instructions are confirmed in a terminal.** Every teammate's AI follows them, so a
  chat no longer sets them: your AI proposes the text, and `graph-mind team instructions
  "<project>"` shows it and applies it when you answer y.

## 0.9.24 — 2026-10-21
- **Giving a teammate access is no longer done from a chat.** Asking your AI to invite someone
  (or to host projects on this PC) now gives you a command to run in a terminal,
  `graph-mind team invite NAME --role ROLE --project P` or `graph-mind team host`, and the
  invite code appears only there. Before, text your AI read on a web page or in a file could
  have made a stranger an owner of your project.

## 0.9.23 — 2026-10-21
- **Every install now writes its own log file** in a synced brain folder (the computer's name
  plus a suffix drawn once). 0.9.22's check missed PCs that chose the folder before the other
  PC's log had synced, and PCs already sharing a folder. Older log files are still read; new
  lines go to the new file.

## 0.9.22 — 2026-10-21
- **Two PCs with the same computer name no longer share one log file** in a synced brain
  folder. Both appended to the same file, and the sync client could keep only one PC's copy,
  losing the other's memories. A PC that joins a folder already holding a log under its name
  now gets its own name with a short suffix.

## 0.9.21 — 2026-10-21
- **Code changes no longer keep secrets.** A password in a config file under a watched
  project was stored in the file's copy and its diff as typed; both are masked now. Files over
  2 MB and binary files are skipped, and a Windows junction out of the project is not followed.
- **Code capture no longer stops** when a file is edited back and forth (A, B, A, B).
- **A fact saved with the same date on two PCs** now shows the same current value on both.
- **A memory promotion interrupted at the wrong moment** no longer blocks every later one.
- The entity timeline no longer lists an entry twice or misses a change after a forget; one
  oversized label no longer empties the context packet; the memory view (`brain.html`) can no
  longer be broken by a stored message.

## 0.9.20 — 2026-10-21
- **Far less disk writing.** Saving new search vectors used to rewrite the whole vector file
  (about 32 MB for 11k vectors) every time, even for one new vector. Now the new vectors are
  appended; the file is rewritten only when two processes saved in a different order. Vectors
  already published are never changed, and a half-written tail after a crash is ignored.

## 0.9.19 — 2026-10-21
- **Less work while you are idle.** The capture service no longer re-reads every stored turn
  every five seconds to find unembedded ones, and skips the memory-indexing passes while the
  store has not changed. Cursor, health and code-manifest files are written only when they
  change; shared sync skips logs it has already read.
- The embedding model's worker threads no longer spin after a batch, and each memory server
  reuses its own embedder for background index refreshes instead of loading a second cache.

## 0.9.18 — 2026-10-21
- **No more hour of re-embedding after every upgrade or restart.** The startup check on the
  cached search vectors threw the whole cache away whenever two of its 16 samples moved a
  little, which the int8 model (0.9.10 and later) does by design: the same text comes out
  slightly different in different batches. The capture service then re-embedded everything,
  about an hour at a fifth of the CPU. It now clears the cache only when most samples are far
  off, as a truly corrupt cache is.

## 0.9.17 — 2026-10-20
- **Switching to another shared brain** no longer sends what was queued for the old one to the
  new one. It is all in this PC's store; `share_existing` sends it on purpose.
- **The settings file** (share password, join codes) is readable by you only on macOS and Linux,
  and two apps saving settings at once no longer lose one of them.
- **An unreachable hub** gives up after 5 seconds instead of holding a recall for the system's
  network timeout.
- **Python 3.10:** the server, hooks and OpenCode plugin no longer import a `graph_mind` folder
  from the project they are started in (3.10 ignores PYTHONSAFEPATH).
- An apostrophe in a path no longer breaks the Codex config or the startup shortcut; uninstall
  goes on when `claude` is blocked; an entity's history is capped at 500 entries.

## 0.9.16 — 2026-10-20
- **Forgetting reaches every copy.** A memory the capture service made from a forgotten turn
  now also leaves the outbox and the shared log, and other PCs are told about it. A forget
  notice from another PC also clears this PC's outbox and the hooks' staged lines. Two forgets at
  once no longer write back each other's text.
- **Forgotten items are never sent again**, even when a transcript is read a second time.
- **Masking:** quoted passwords that contain the other quote mark (`"don't tell"`) or an escaped
  quote; fields saved by `brain_remember` whose name says credential (`{"password": "..."}`).
- **Sync** retries a line when the database was briefly locked, instead of skipping it.
- **Questions about a past moment** no longer come back empty when newer conversations exist.
- Cursor replies keep their question's date across scans; on macOS and Linux, staging files made
  before 0.9.14 are narrowed to this user only.

## 0.9.15 — 2026-10-20
- **What you forget stays forgotten.** A forgotten turn or memory is refused if it arrives again:
  from another PC's log, from an outbox sent after the hub came back, or from a transcript read
  again. Before, any of these brought it back.
- **Forgetting while the shared hub is down** still tells the other PCs: the notice waits in the
  outbox (a failed clean-up lost it before), and lines for the forgotten turn still waiting to
  be sent are dropped.

## 0.9.14 — 2026-10-20
Fixes from a second code review (Codex, gpt-6.1-sol).
- **Security:** a folder or file path holding shell characters (`" ; & | $` ...) is refused
  instead of being put into the command the AI asks you to paste: a crafted path ran whatever
  followed it.
- **More secrets masked:** quoted JSON/YAML/.env values, AWS secret access keys, signed JWTs,
  DigitalOcean, GitLab, npm and Hugging Face tokens. `brain_remember` now masks tags, entities,
  source and attributes too. Type annotations (`password: str`) are no longer changed.
- **Forgetting reaches every copy:** the memory a "remember this" turn was promoted to, and the
  Gemini hooks' and OpenCode plugin's own transcripts (unmasked until the session is idle an hour).
  Those transcripts are now readable by your account only.
- **Capture:** one malformed record or a stray Unicode surrogate no longer stops every later line
  of a transcript; a line still being written when capture started is no longer lost.
- The Gemini hook reads only the end of its transcript (it read all of it for every reply).
- Uninstall removes the OpenCode plugin even when `opencode.jsonc` has comments.

## 0.9.13 — 2026-10-20
Fixes from a code review (Codex, gpt-6.1-sol).
- **Security:** `~/.graph-mind/embedder.json`, whose key lets a program use the capture service's
  model, is now readable by your account only (on macOS and Linux another account could read it);
  the service accepts nothing but lists of texts.
- **Shared memory:** a PC saved how far it had read the other PCs' logs before importing them, so a
  crash in between skipped those records for good; now after. A malformed line no longer stops
  every later sync.
- **A question no longer waits behind another request of the same app** to the capture service.
- Concurrent requests for the same backlog embed each text once; restarting an old-model service
  no longer makes each app load its own model meanwhile; only one capture service runs per PC.
- Asking about a past moment (`as_of`, `knowledge_cutoff`) now also leaves out later conversations.

## 0.9.12 — 2026-10-20
- **Sharing and teams work on Ubuntu 26.04.** The Postgres build used on Linux needed the system's
  libxml2.so.2 and ICU 74, which 26.04 no longer ships. Linux now uses zonky's build, which
  carries its own libraries; a PC whose earlier build still runs keeps it.

## 0.9.11 — 2026-10-20
- **A question no longer waits while 0.9.10 embeds the memory again.** It was embedded after the
  chunk being worked on (up to 5 s at the 95th percentile on a GitHub runner); now beside it.
  Measured on CI once the memory is embedded: search 20 ms median, 41 ms at the 95th percentile
  (63 ms with the previous model), and a Korean question finds the English-only memory.

## 0.9.10 — 2026-10-20
- **Better search by meaning in Korean, with less memory.** The embedding model is now the int8
  export of multilingual mpnet (was MiniLM). Korean questions found among the first 10: 19/20
  (was 17/20); formal Korean 17/20 (was 13/20); a Korean question about an English-only memory
  4/5 (was 3/5). English evidence on 100 LongMemEval questions: same recall, answer session in
  the first 5 for 99.0% (was 97.9%). Loaded it takes 699 MB (was 869 MB) and a 266 MB download
  (was 449 MB, which the installer now removes). It is 2.4x slower to embed and a query takes
  10 ms. Results: `research/runs/graph-v9.13-int8-models`. The 88.8% LongMemEval score was
  measured with MiniLM; the full run on this model is still to come.
- **After the upgrade, existing memory is embedded again once, at half pace** (about an hour for
  11,000 pieces on an 8-thread PC, the service at ~1.4 cores meanwhile); until then older turns
  are found by words. Every app's server asking for the same backlog is answered once. A capture
  service still running the old model is restarted by the first app that starts on the new
  version, so apps do not each load their own model.

## 0.9.9 — 2026-10-20
- **Security: a chat can no longer move your memory.** Choosing the memory folder, sharing it with
  your other PCs, joining with a code and exporting or importing a memory file are now terminal
  commands you run (`graph-mind folder share`, `graph-mind folder <code>`, `graph-mind export
  <file>`): the AI shows the command. Before, a web page or file the AI read could ask it to send
  every future conversation to someone else's server, when tool calls were auto-approved.
- **Security: this PC's Postgres needs a password.** It was created with `trust` for connections
  from the same PC, so any program or other Windows account there could read the memory. Now
  every login needs a password, kept in `~/.graph-mind/pgpass`; an existing one is changed the
  first time it is used after the upgrade (restart open AI apps once afterwards).
- **The first memory question after an app starts is answered in about 1.3 s** (was 3.4 s, once
  24 s, on a PC whose 400 MB memory is shared with another PC). The server's warm-up now also
  syncs the shared memory and loads the search index, not only the model.
- **More secrets masked before storing:** a password said in a sentence ("my password is
  Hunter2!", "내 비밀번호는 qwer1234!", "와이파이 비번 abcd5678": only with a digit or a symbol, so
  "the password is stored in the vault" stays), Stripe keys, card numbers with a valid check
  digit, Korean resident registration numbers. On LongMemEval's 189,515 chat turns these rules
  fire once.
- **The OpenCode plugin's and Gemini CLI hooks' own transcripts are deleted once stored.** They
  hold what was typed before masking, so a masked or forgotten password stayed on disk there.
- **The capture service idles at about 2% of one core** (was 5%): transcripts with nothing new
  are not opened, and Cowork's deep session folders are searched for new sessions once a minute.
- Full LongMemEval run on this code: 88.5% (440/497; 88.8% before, within noise).

## 0.9.8 — 2026-10-19
- **The capture service no longer slows the PC.** Measured on a 400 MB memory with nothing new
  coming in, it used half a CPU core and about 1 GB all day; now about 9% of one core while a
  conversation is being saved, and about 830 MB (the search model itself).
  - It looked for turns without a search vector by reading every stored turn every 5 seconds;
    now only after new turns arrive (and every 10 minutes).
  - The code-change scan walked into excluded folders (node_modules...) and re-read every file
    every 2 minutes; files whose size and time are unchanged are no longer read.
  - The model runtime kept the memory of its largest batch (up to 2.9 GB); it now gives it back
    (4% slower), and embeds on half the cores so the PC stays usable.
- A hub preparation step on a PC hosting project brains failed and was retried every 10-20 s
  when an old table had been made again after the move; it is now kept under another name.

## 0.9.7 — 2026-10-19
- README: retrieval R@5 (98.5% vs 95.2%) beside answer accuracy in the agentmemory table.
- recall_any@5 on LongMemEval_S, measured clean in CI: 98.5% (479 questions; @10 99.2%),
  beside agentmemory's published 95.2%; in docs/benchmarks.md. The bench baseline is replaced:
  the earlier one was measured on corrupted cached vectors.
- bench workflow: the summary step failed on every run without a research script (a `[ -f ] &&`
  as the loop's last command); the table's R@5 / R@10 columns now have headers.
- Tool parameters the tool text did not explain (`as_of`, `knowledge_cutoff`, `scopes`, `limit`,
  `max_chars`, `change_kind`...) carry a one-line description: 29 of 74 now, the rest are
  explained in their tool's text (Glama had flagged 0% coverage). About 400 more tokens in the
  tool list each request.

## 0.9.6 — 2026-10-19
- **"Delete the password I typed" is no longer kept when asked in Gemini CLI or OpenCode.** The
  conversation about deleting is meant to go too (docs/privacy.md), but their hook and plugin
  recorded only text, so the brain_forget call was invisible and the request, secret and all,
  was stored. A Gemini CLI AfterTool hook (brain_forget only) and an OpenCode tool hook now mark
  the call as Claude Code's transcript does. Checked with Gemini CLI 0.64.0: remember, then
  forget; the forget exchange is gone from what is captured.
- **Gemini CLI is marked as saved only while its hooks are actually in place.** The installer
  connected the memory (telling the model its chats are saved) before adding the hooks; when the
  hooks could not be written, nothing saved Gemini's chats and the model was told not to. Hooks
  now go first, the mark depends on them, and a stale mark from an earlier install is removed.
- A message with no sign of the past (most of them) is let through in about half the time:
  the memory lookup decided to skip it only after loading the search code (Gemini CLI waits for
  this hook before answering; 1.2 s to 0.5 s here).
- docs/privacy.md and docs/install.md list what each app now has captured (Copilot CLI, Cursor's
  CLI agent, Gemini CLI, OpenCode); the privacy page still said their transcripts were not read.

## 0.9.5 — 2026-10-19
- **Gemini CLI hooks work when the Windows profile folder has a space** ("C:\Users\Jane Doe"):
  Gemini runs hooks through PowerShell, where the quoted path alone was a string and the hook
  failed silently, saving nothing. Now `& "path" -m graph_mind.hook ...`.
- **A project with its own `graph_mind.py` or `graph_mind/` no longer breaks the memory there.**
  Apps start the server and hooks in the user's project folder, and `python -m` looked there
  first. `PYTHONSAFEPATH=1` (Python 3.11+) is now set for the server, the hooks and the
  OpenCode plugin.
- **One app's settings can no longer stop the whole install.** A Gemini CLI settings file with
  `"hooks": {"enabled": true}` crashed it, and so would a file an app holds open; that app is now
  skipped with a message and the rest are connected.

## 0.9.4 — 2026-10-19
- Gemini CLI and Copilot CLI are told their conversations are already saved, so the model no
  longer stores the same thing again with brain_remember. (Cursor was too in 0.9.4; 0.9.5 takes
  it back until its editor is seen writing the transcript the CLI does.)
- Cursor turns are dated by the time in the transcript, not by when the capture service read
  them.
- A Gemini CLI reply reported twice by its AfterAgent hook is stored once.
- Hook transcripts keep their read position in their own file, apart from any transcript source
  of the same agent.

## 0.9.3 — 2026-10-18
- Package and registry descriptions name the agents it works with, so a search for "Claude Code
  memory" or "Gemini CLI memory" can find it. No code changes.
- README and docs/benchmarks.md: agentmemory on the same 499 LongMemEval questions, its own
  published retrieval through the same answer model and judge: 80.4% against Graph-MIND's 89.0%,
  reading about 5x more text per question.

## 0.9.2 — 2026-10-18
- **Gemini CLI, Copilot CLI and Cursor conversations are saved too**, so what is said there
  reaches the other agents without the model calling brain_remember. Each was tried for real
  on Windows (Gemini CLI 0.64.0, Copilot CLI 1.0.98, Cursor's CLI agent): a fact said in one
  session was stored, both sides, and a new Gemini or Copilot session answered it. (Cursor's
  new session could not call any MCP tool on the test PC: another tool's broken hook blocked
  them.)
  - Copilot CLI and Cursor: their own transcripts are read (`~/.copilot/session-state`,
    `~/.cursor/projects/*/agent-transcripts`). Cursor's prompt and reply hooks do not run in
    its CLI, and Copilot's never carry the reply.
  - Gemini CLI keeps no transcript to read: `graph-mind-install` adds hooks
    (`python -m graph_mind.hook`) beside the user's own, and `graph-mind-uninstall` removes
    only ours. Its BeforeAgent hook also adds the memory that bears on each message before the
    model answers (as OpenCode's plugin does): the new session answered without a tool call.
- Copilot CLI is connected to the memory server (`~/.copilot/mcp-config.json`).

## 0.9.1 — 2026-10-18
- **OpenCode conversations are saved, and every message brings its memory.** The installer adds
  a plugin: it records what is said for the capture service (stored like Claude Code's turns,
  searchable by meaning, shared with the other agents), and before the model answers a message
  it adds the memory that bears on it (`python -m graph_mind.inject`: 8 items, 4,000
  characters, nothing when the message shows no sign of the past). The model no longer has to
  think of calling brain_context: a free model that never saved the fact and answered "I don't
  have any information about your dog" without the plugin answered "Kongi, a golden retriever"
  with it.
- Looking up the memory for a message takes about 7 s on a cold PC instead of 17.5 s: the MCP
  SDK (6-10 s to import) is now loaded only when the server starts, not by everything that
  borrows its search. Past the plugin's 15 s limit the memory had never reached the model.
- `bench/evidence.py` also reports recall_any@5 and @10, the retrieval number other memory
  projects publish for LongMemEval, so the two can be compared on the same terms.

## 0.9.0 — 2026-10-16
- **OpenCode.** `graph-mind-install` now connects OpenCode too, so any model OpenCode runs that
  can call tools shares the same memory (OpenCode's free models were tested: see
  research/opencode_models.py). OpenCode does not pass a server's instructions to the model, so
  they go in an instructions file its config lists. Its conversations are not captured yet: the
  model saves what it hears with brain_remember.
- **Search by meaning could be quietly wrong; now it checks and repairs itself.** The vectors
  cache shared by the capture service and every app's memory server could pair texts with other
  texts' vectors when two processes saved at the same moment (seen on Linux; on Windows the save
  crashed instead). The capture service now re-embeds a few cached pieces when it starts and,
  if they differ, clears the cache for every process and fills it again. The benchmark's own
  cache had this, so the evidence numbers measured in CI since 0.8.0 were too low.
- **Saved memories are found by meaning too.** "What breed is my dog?" now finds "our golden
  retriever Kongi", and a Korean question an English memory; before, a memory needed a shared
  word. Meaning is used when no word matches (the local model scores short Korean memories as
  all alike, so mixed into word matches it only added noise), and then a fresh server waits up
  to 10 s for the model instead of 0.5 s. This matters most in apps whose conversations are not
  captured (OpenCode): there, what the model saved is all there is.
- **Move a whole memory as one file.** Ask "export my memory to a file" on one PC and "import
  this memory file" on another: `brain_folder(export_to=...)` / `brain_folder(import_from=...)`.
  Importing twice adds nothing. Sharing an existing memory (`share_existing`) also stopped
  dropping a memory's profile, so a fact stays a fact on the other PCs.
- **Tools that are easier to tell apart.** `brain_context` now says it is the default way to
  answer from the past and `brain_recall` that it is for raw rows, ids and one thing's history;
  `brain_folder` (your own memory and PCs) and `brain_checkpoint` (team projects) point at each
  other. A Glama review found the old descriptions near-identical.
- **Facts that change.** `brain_remember` takes `about` and `attribute` ("Kongi" + "breed", "me" +
  "home_city"). A later value of the same fact replaces the earlier one in recall, with no memory
  id needed, and the packet shows the value now and what it was before. Order is when each became
  true, so a fact recorded late from an old conversation stays history. The models are asked to
  record such facts as they hear them, and to record the fact behind an answer that came from old
  conversation turns. Memories take at most a quarter of the packet before conversation turns
  (uncapped, many short facts pushed out the turns that answered), and an `attribute` never
  holds the value ("last_trip", not "last_trip_paris"). Measured on 108 knowledge-update and
  preference questions: 96 with facts, 96 without, so facts do not cost accuracy.
- **The capture service comes back by itself.** It started only at login, so a crash or a kill
  meant nothing was saved until the next login. Now the memory server of every app that relies on
  it checks once a minute and starts it again; a second one never starts beside it.
- **One model for every app.** The capture service, running since login with the embedding model
  loaded, lends it to the memory servers (127.0.0.1, keyed). An app's server no longer loads its
  own: search by meaning works from its first question and each app's server holds ~90 MB instead
  of ~830 MB (`bench/latency.py`, 2,000 turns: first answer 9.7 s -> 0.8 s). Without the service
  running, a server loads the model itself as before.
- `bench/evidence.py` embeds each haystack before asking, as the capture service does: a backlog
  sent to the background left some questions to word search alone, so two runs of the same code
  disagreed (91.6% / 91.4%). It also no longer keeps every question's index in memory.
- **Measurements** (`bench/`), the yardstick for the structure work ahead: `evidence.py` (does
  the answer reach the packet, all of LongMemEval_S, offline), `latency.py` (server start, first
  answer, when search by meaning works, steady p50/p95, memory), `recovery.py` (every fault meant
  to heal, run and timed, and the ones not handled yet). Weekly in CI (`bench` workflow).
- A hub Postgres that was killed comes back on the next use: its log now lives outside its data
  folder (the crash recovery's disk sync waited 30 s on it) and a start may take up to 5 minutes
  (that sync alone took 25-55 s on a laptop with antivirus).

## 0.8.0 — 2026-10-14
Run `graph-mind-install` after upgrading: it registers the server at its new place. Apps set up
by an earlier version keep working until then. Your memory is kept.
- **One package.** The code is the `graph_mind` package (`python -m graph_mind.mcp_server`)
  instead of twenty top-level modules, which had put names like `install` and `forget` into
  every Python environment it was installed in. Modules that were named for their history now
  say what they do: `sharing`, `project_brains`, `paths`, `context_packet`, `warmup`,
  `memory_view`, `capture_cli`. From a source checkout: `pip install -e .`.
- **Removed what never ran**: the opt-in OpenAI extraction in the capture service (off by
  default, and at odds with saving memory never calling a model), the old development-event
  writer (its records in older memories are still read), and unused recall helpers. Research
  code and its tests live under `research/`.
- **Team hubs**: one account per name, so adding "Mina" can never pick someone who took her
  name; an owner can take a teammate's checkpoint back but not rewrite it; `remove_member`
  without a project no longer closes the whole account (`graph-mind-hub remove-user` does).
- SECURITY.md and the teams page say plainly what the hub machine's own users and its operator
  can read.
- A hub whose first start failed right after it was created (Windows refusing cmd.exe) no
  longer stays without the memory's database: it is created on the next start.
- `ruff check .` passes and runs in CI.

## 0.7.0 — 2026-10-13
Run `graph-mind-install` after upgrading. Every PC that shares a hub should upgrade: the hub moves
to its own Postgres build with TLS, and team logins reach a new hub database. Your memory is kept.
- **Project brains (optional, for teams).** `brain_checkpoint`: shared projects apart from
  personal memory, which stays the product, holding only what someone publishes: checkpoints
  (decision, progress, blocker, next action, artifact, release) with their context (what changed, where, why, verification, what is left, what to
  check; "not recorded" where nothing was written), supersession and retraction; per-project AI
  instructions; a working folder bound to a project. Files and the talk about them stay in
  Slack, drives and GitHub: a git folder is linked as its commit at the stage it really reached
  (committed, pushed, PR open, merged, released) and a Slack or drive address as a link.
- **Postgres hubs** (office network or Tailscale): every project its own schema, groups and
  key in a database apart from personal memory; teammates' logins cannot reach anyone's
  personal memory; row-level security per project; an audit log. Whoever creates
  a project owns it; only owners add members (owner, editor, viewer), set instructions or retract
  others' checkpoints; whoever runs the hub runs the server and sees a project only when added.
  `graph-mind-hub` for a server or NAS with no AI on it: `host`, `add-user`, `users`,
  `remove-user --transfer-to`, `delete-project`, `audit`.
- **One-step install, no Python needed**: `scripts/install.ps1` (or double-click
  `Graph-MIND-Setup.cmd`) on Windows, `scripts/install.sh` on macOS and Linux. They install uv,
  which brings its own Python, then Graph-MIND, then connect the AI apps. Checked in CI on all
  three systems against each release.
- **One kind of code for the user**: a code pasted to the AI works whichever tool receives it,
  whether it is for the user's own other PC or a team invite; `graph-mind-install --join` takes
  either too.
- **Names people choose**: teammates see a chosen name, not a PC's; the assistant asks for it
  before creating or sharing a project and when joining (`set_name`, `install --name`).
- An unreachable hub is reported as "latest state unknown", never as an empty project.
- **Hub connections are TLS**: every code carries the hub's own certificate and PCs check it.
- **Postgres from its own builds instead of pgserver** (no TLS, no ARM): Postgres 16.14 fetched
  once per machine on first hosting, checked against pinned hashes. Hosting works on any Python
  and on 64-bit ARM Linux; `pip install` no longer carries a Postgres for people who never host.
  A hub on the old programs is restarted on the new ones once, data kept. Hubs from before this
  branch are converted on first start (each project into its own schema, sealed again).
- Postgres no longer opens console windows on Windows (a hidden console instead of none). The
  random start failures: on the PC where they happened, Windows refused to start cmd.exe in
  10-30% of plain `cmd /c ver` runs; Postgres needs cmd.exe, so the retries stay.

## 0.6.4 — 2026-10-12
- Mitigates intermittent Postgres initialization and start failures on Windows with a bounded
  retry (up to ten tries each). On the PC where it was seen, initdb succeeded in 3-6 of 10
  identical runs; with the retry, three fresh hubs out of three came up. Interference from
  endpoint security software is suspected but the cause is not confirmed, and inside the test
  harness all ten tries can still fail. To be closed before a multi-user team hub ships.

## 0.6.3 — 2026-10-12
- `graph-mind-view`: regions show from the first frame (nodes start near their region), instead of
  depending on how long the browser runs the layout.

## 0.6.2 — 2026-10-12
- `graph-mind-view`: fixed a black page when opened from disk ("reading 'tick'" on every frame:
  the layout was restarted before it existed). Forces are set first, the data last.

## 0.6.1 — 2026-10-12
- `graph-mind-view` keeps the 3D library inside the page (fetched once by Python), so the page
  opens from disk even where the browser would not load it from the CDN; it stayed black there.
- Region names leave out ids and hashes (words with digits).

## 0.6.0 — 2026-10-12
- `graph-mind-view`: your memory as an interactive 3D graph in the browser. Turns and memories
  are points, joined by the associations recall already keeps; related ones gather into coloured
  regions named by their topic words. `--no-text` writes a copy with no conversation text, safe
  to screenshot or share.

## 0.5.5 — 2026-10-12
- Recall stays fast as memory grows. Meaning search keeps its pieces and vectors in memory and
  adds only the turns captured since the previous question, instead of rebuilding everything per
  question. Measured on synthetic stores: 100,000 turns 6.3 s → 1.3 s per question, 20,000 turns
  1.5 s → 0.3 s; on this PC's real memory 165 ms → 37 ms. Same rankings (44 real queries; only
  the order of identical messages with identical scores can differ).

## 0.5.4 — 2026-10-12
- Faster recall: `brain_context` no longer repeats every item next to the context text it
  already contains, so the model reads about 60% less (43K → 16K characters on a real memory).
  The result lists the item `ids` instead.
- Recall itself takes about half the time (0.9 s → 0.5 s on a real memory): one open connection
  to the shared hub instead of a new one per question, and memory titles are normalized once.

## 0.5.3 — 2026-10-12
Run `graph-mind-install` after upgrading: it rewrites the app registrations.
- A memory server that fails to start no longer keeps Codex from starting a chat (the Codex entry
  is no longer `required`).
- Faster replies in Claude Code and Codex: their conversations are already saved by the capture
  service, so the model is told not to call `brain_remember` for ordinary chat (only when asked
  to remember something). Apps without capture keep saving proactively.
- The installer no longer stops when Windows refuses to launch the `claude` CLI; it writes
  Claude Code's settings file instead.
- Repository layout: the product is in `src/`, tests in `tests/`, benchmarks and REPORT.md in
  `research/`. The installed package is unchanged.

## 0.5.2 — 2026-10-12
- Secret masking can be turned off with `"mask_secrets": false` in `~/.graph-mind/config.json`,
  for people running their own models who want keys remembered verbatim. On by default.

## 0.5.1 — 2026-10-12
- If an app keeps writing transcripts but no conversation comes out of them (its format
  changed), the capture service warns instead of silently saving nothing. The assistant sees the
  warning in `brain_context` / `brain_folder`; `graph-mind-capture status` shows it too.
- CI: upgrading from the previous release keeps the saved memory.

## 0.5.0 — 2026-10-12
- Shared memory is encrypted at rest: each line on the hub is sealed (Fernet, key derived from
  the share password). Existing lines are sealed when sharing starts. **PCs on 0.4 or older
  cannot read sealed lines — upgrade every PC.**
- The installer connects every MCP client it finds: Claude Code, Claude Desktop, Codex, Cursor,
  Windsurf, Gemini CLI, LM Studio, VS Code (Copilot) and Cline.

## 0.4.0 — 2026-10-12
- Embeddings run on ONNX Runtime instead of PyTorch: identical vectors, about 8x faster model
  load, no 2 GB torch install.

## 0.3.4 — 2026-10-12
- An app's first question no longer waits for the embedding model to load (38.8 s → about 2.5 s).

## 0.3.3 — 2026-10-12
- Re-sharing sends only what the shared memory does not have yet.

## 0.3.2 — 2026-10-12
- The shared hub keeps running after the app that started it closes; it restarts when found down,
  and lines that cannot be sent wait in a local outbox.
- `graph-mind-uninstall` (`--purge` also deletes the memory). SECURITY.md.
- Dockerfile for MCP directories, checked in CI.

## 0.3.1 — 2026-10-08
- Masks passwords written after their name (`password:`, `비밀번호:` …) and unknown
  machine-random tokens.

## 0.3.0 — 2026-10-08
- `brain_forget`: ask the assistant to delete something; it lists matches, deletes what you pick,
  and the deletion exchange itself is not remembered.
- Scheduled checks: fresh PyPI install daily, test suite weekly.

## 0.2.1 — 2026-10-08
- `graph-mind-forget`: remove a phrase from every PC's memory.

## 0.2.0 — 2026-10-08
- Five MCP tools instead of thirteen. Tests never touch the developer's own memory.

## 0.1.1 – 0.1.5 — 2026-10-08
- Installs on Python 3.13/3.14; pip installs capture conversations only.
- Clear message for a missing Visual C++ runtime; model cache falls back to `~/.graph-mind`
  when `~/.cache` is not writable.

## 0.1.0 — 2026-10-08
- First release: local memory engine for AI coding assistants, on PyPI and the MCP Registry.
  Automatic conversation capture, context packets, sharing between PCs, login item on
  Windows, macOS and Linux.
