# Reviewing Turnbook: is it a bug, or the design?

A finding is fixed only after it is shown to be a bug and not a decision someone made on purpose.
Code that looks wrong is often a trade-off with a reason written down somewhere. Changing it
quietly undoes that reason.

## Before fixing anything

1. **Reproduce it.** A failing test, a measurement or a script run. "Looks wrong" is not enough.
2. **Look for the intent**, in this order:
   - the comment or docstring next to the code (most decisions are written there, with the
     incident that caused them);
   - `git log -S "<the line>"` and `git blame`, then the commit message;
   - `CHANGELOG.md`, `docs/privacy.md`, `docs/install.md`, `docs/design/`;
   - what the user decided (ask if unsure).
3. **Put it in one of four classes:**

   | class | meaning | what to do |
   |---|---|---|
   | bug | the code does not do what its comment, docs or tests say, or it breaks something nobody chose | fix it, with a test that fails before the fix |
   | gap | no one decided either way (the case was never thought of) | fix it, and say in the commit that it was a gap |
   | design | done on purpose, with a reason that still holds | keep it; document it where users will see it |
   | design to revisit | done on purpose, but the reason is weaker than the cost now | ask the user first; change it only on their yes, and say so in the CHANGELOG |

4. **Write it down** in the log below: what was found, which class, and the evidence.

## Log

### 2026-11-01: outside review (WearyMonk8 fork, REVIEW.md) and the review after it

| finding | class | evidence | outcome |
|---|---|---|---|
| A synced-folder brain was plain text in the cloud | design to revisit | `sharing.py` docstring: "kept as plain append-only text" (a text log instead of a database file; encryption never discussed) | user chose to seal it; a5cef6d |
| The installer changes every AI app without asking | design to revisit | `docs/install.md`: "connects every AI app it finds" in one step, on purpose | user chose to ask first; a5cef6d, 0311c24 |
| `irm \| iex` / `curl \| sh` install | design | one-line install is the product's promise; same pattern as uv, rustup, Homebrew | kept; careful install documented |
| Startup shortcut at login | design | capture needs a background process; visible, no admin, removed by uninstall | kept; installer now lists it |
| Memory not encrypted on disk | design | the apps' own transcripts sit unencrypted in the same profile, so encrypting our copy protects nothing | kept; `docs/privacy.md` says so |
| Firewall rule for sharing | design | only on `turnbook folder share`, UAC prompt, LocalSubnet and Tailscale only | kept |
| `turnbook-view` inlined a CDN script unchecked into a page holding the whole memory | gap | added in 0.8.0 to make offline pages work; pinning never considered | pinned by SHA-256 and SRI; 0311c24 |
| Recalled memory could be read as instructions | gap | the header said what the packet is, not that it is not instructions | header says so; 0311c24 |
| Anthropic keys masked under the OpenAI label | bug (label only) | the key was masked; the label was wrong | own label; 0311c24 |
| "Same 60 pilot answers" note in every judge report | bug | copied from the first pilot; the counts were right | removed; 519eb94 |
| Every shared line looked its key up again (20,000 lines: 10 s) | bug (made that day) | introduced by a5cef6d; for a hub PC the config was read per row before too | key looked up once; 2e43e91 |
| A folder whose marker vanished got a new password | bug (made that day) | made in a5cef6d; nothing intended it | known password kept; 2e43e91 |
| A PC that read past sealed lines on an old version never read them | gap | upgrade path not considered in a5cef6d | first password resets the folder cursors; 03b997d |
| A test used `"u"` as a hub URL | bug (test) | the test meant a hub; a bare string is now a folder | test uses a real URL; 0311c24 |

Checked and fine: secret masking (14 formats), the local service's key check, the documents
scan (locked files, encodings, deleted files), the service's load on this PC (1.2% of one core,
382 MB).

### 2026-11-02: bug and optimisation sweep

| finding | class | evidence | outcome |
|---|---|---|---|
| Forgotten text stayed in the memory file's bytes (8 copies after forgetting, in a test) | gap | `forget.py` docstring and `docs/privacy.md` promise the text is gone from the store and its indexes; only transcripts and vectors are named as exceptions | freed space zeroed on every connection, FTS5 secure-delete on forget, WAL emptied; test checks the file bytes |
| An idle capture pass takes 0.3-0.4 s of wall time | design | listing each app's transcript folder (Codex's dated tree: 55 ms); the service runs below normal priority, so it waits behind other work; CPU 1.2% of one core | kept |
| Codex cursor keeps offsets of 214 files while 16 exist | gap, negligible | deleted session files are never dropped from the cursor; 29 KB file, read in under 1 ms | kept for now |
| Code capture kept every past version of every file as a blob (169 MB, one project) | gap | blobs are read only as the "before" of the next change; `before_ref`/`after_ref` are stored but never read; patches hold the history | only current blobs kept |
| Code diffs of a virtual environment and vendored deps (2.7 MB each) in the code history | bug, already fixed | `pyvenv.cfg` skip added in 0.9.31; the leftovers are from the old C:\graph-mind checkout | kept (history); no new ones |
| The pre-0.9.10 model (MiniLM, 479 MB) still on this PC | design | `remove_old_models` runs in `turnbook-install`; this PC runs from a checkout and never re-ran it | removed by hand |
| mpnet (265 MB) and its vectors (63 MB) kept after moving to gemma | design to revisit | kept on purpose for `turnbook model multilingual`; user (11-02): one model now, gemma is better in Korean too, but users must still be able to pick their own | mpnet removed once gemma has read the memory; custom models kept |
| Postgres log only grew; two thirds of it one line per checkpoint (2,424 lines in 17 days) | gap | `pg_ctl -l` appends forever; no setting chosen for it | `log_checkpoints=off` |
| 1,755 repeated `ALTER TABLE ledger_projects RENAME` errors in the hub log (10-16 to 10-19) | bug, already fixed | the migration was removed with the project-brain rewrite; no error since 10-19 | nothing to do |
| A short message ("했어") got four older identical messages as its memory | gap | searching every message is the user's decision (10-30) and stays; returning the message's own words back was never chosen | turns with exactly the query's text dropped from the hook packet |
| With the capture service down, each message starts loading the model in the hook | design | an app's memory server restarts the service within 60 s (ensure_service), so it lasts a minute | kept |
| Claude Code's compaction summaries stored as the user's words (28 in one long session) | bug | the parser's docstring: only typed user text and assistant text are kept; the record carries `isCompactSummary: true`; product promise: no AI summaries | skipped; stored ones erased once by id |
| Codex `<heartbeat>` and `<external_codex_apps_*>` blocks stored as the user's words (150) | gap | `AUTOMATIC_BLOCKS` already strips Codex's `environment_context` and `recommended_plugins`, and `AGENT_INSTRUCTIONS` the AGENTS.md file: same intent, these two kinds came later | added to the list; stored ones erased once by hash |
| (first count said a quarter of Codex user messages were injected) | not a bug | that counted raw transcript lines; most of them were already stripped before storing | corrected after reading `_redact` |
| The same user turn stored twice (source ordinals 1 and 15448, same session, time and text) | bug, already fixed, data left behind | `_identity`'s docstring: before 0.9.2 a re-read gave every turn a new id and stored it twice; uuids since | duplicates dropped once per store, first copy kept; a copy a memory was promoted from is kept |
| Questions and pasted chats saved as "decisions" by the promotion gate | design to revisit | `semantic_memory.py` (v0.3.1, 09-16): Korean keyword regexes, before the user's 10-30 rule that the LLM, not keyword rules, judges language | user (11-02): turn it off and drop what it made; remember requests kept |
| Codex response annotations (selected reply text, JSON) stored in the user turn | gap, kept | the block also carries the user's own comments; 3 messages on one PC | kept until it is worth parsing |
| A memory with source `test` (10-08) and 00:00:00 test-like facts in the real memory | not code | saved through the MCP tool during development | listed for the user to pick (brain_forget rule) |
| After install then uninstall: `hooks/turnbook.json` (Copilot) and `graph-mind.md` (OpenCode) left empty; empty `mcpServers` keys in other apps' files | gap | full before/after file diff of a fake home with every app | the two files of ours are deleted; other apps' files keep empty keys (the user may have added to them) |
| September development notes (TASK_STARTED, SESSION_SUMMARY JSON) answered as curated memories | gap | the writer was removed in a6a8e0d (10-14, "Remove code the product never runs"); the three readers were left | readers removed; the table stays untouched |
| Tool calls (edits, commands) are not stored, so "which file did I change yesterday, and why" has no answer | design-to-revisit | `_said`: "text blocks only: no tool_use, tool_result, thinking" (kept only what someone said); the user chose to change it 11-02 | one `Did:` turn per stretch of work (edits + commands, not reads or output) |
| The hook attaches unrelated memories (a price list, a recorder) to coding questions | design-to-revisit | inject: "every message is searched", always 4 items, no floor; the user asked for stricter 11-02 | floor 0.36 for the hook only, measured (research/hook_floor.py); near-repeats of the message dropped |
