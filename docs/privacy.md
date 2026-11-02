# What Turnbook captures, and how it is kept

## What is captured

| app | captured |
|---|---|
| Codex: terminal, VS Code, ChatGPT desktop's work mode | every turn |
| Claude Code: terminal, VS Code, Claude desktop's Code tab | every turn |
| Claude desktop: Cowork | every turn |
| Copilot CLI | every turn (its own `~/.copilot/session-state`) |
| Cursor's CLI agent | every turn (its own `~/.cursor/projects/*/agent-transcripts`) |
| Gemini CLI | every turn, through hooks Turnbook adds (`~/.gemini/settings.json`) |
| OpenCode | every turn, through a plugin Turnbook adds |
| Claude desktop: chat; Windsurf, VS Code, Cline, LM Studio; Cursor's editor where it writes no transcript | what the model saves with `brain_remember` (memory tools connected) |

Only what you actually send is captured: the service reads each app's transcript (or what its
hook or plugin records), written after you press Enter, so a paste you delete before sending
never reaches it. Text only: tool calls, their results and the model's reasoning are not kept.

## Secrets are masked before anything is stored

API keys, tokens from GitHub, AWS, Google, Slack and Stripe, private keys, passwords inside URLs,
values written after `password:` / `api_key=` / `token:`, a password with a digit or symbol said
in a sentence (*"my password is Hunter2!"*, *"와이파이 비번 abcd5678"*), card numbers whose check
digit is right, Korean resident registration numbers, and any long machine-random string even
from a service no rule knows (by its randomness: 91% of random 20-64 character tokens caught; in
246,750 ordinary chat turns it fired 92 times, nearly all on real ids and tokens). Commit ids,
hashes and UUIDs are kept, since you recall those on purpose. A password that is just a word
(*"my password is hunter"*) looks like any other word and is **not** masked.

Keep masking on if you use a cloud model: a recalled memory goes to your model's provider with the
question, so a stored key would leave your PC the first time it is useful. To have Turnbook
remember a key, tell it **where the key is**: *"my OpenAI key is in 1Password, under Dev"*. If your
model runs on your own machines and you want keys remembered as typed, add
`"mask_secrets": false` to `~/.turnbook/config.json`.

## Deleting something

Ask your AI: *"delete the password I typed earlier"*. It lists masked candidates
(`wifi password is ****`), deletes only the ones you pick, and the conversation about deleting is
not saved. Or from a terminal: `turnbook-forget` (asks for the phrase without showing it).

What you pick is deleted from this PC and its search indexes, from the shared memory, and from
your other PCs on their next sync; only ids are shared for that, never the text. Your AI app's own
history (for Claude Code, `~/.claude/projects`) is separate and is not touched.

The text leaves the file itself, not just the tables: the space it took is overwritten with zeros.
A memory file written by a version before this one may still hold leftovers of older edits in
its free space; run `turnbook compact` once to rewrite the file without them.

## On disk

The memory is SQLite under `~/.turnbook/`, not encrypted: anyone who can read your user folder
can read it (minus the masked secrets), just as they can read your AI app's own history. Protect it
the way you protect that folder: a login password and disk encryption (BitLocker or Device
Encryption on Windows, FileVault on macOS).

## Sharing between your PCs

A folder that OneDrive, Google Drive or Dropbox syncs (`turnbook folder <folder>`) holds every line
encrypted the same way, so the cloud service sees only ciphertext. The first PC makes a password
and shows it in its terminal; each other PC types it once when it chooses the folder and keeps it
in its own config, never in the folder. A PC without it neither reads nor writes there.

The memory lives in a Postgres server on one of your PCs, which Turnbook sets up itself. Your
other PCs log in with a generated password, as a role that can reach only the memory database,
over TLS only: the code carries the hub's own certificate, so a PC accepts that hub and nothing in
between. The Windows firewall rule admits only the local network and Tailscale. Every line is also
encrypted (AES with an HMAC) under a key derived from that password before it leaves a PC, so the
hub's database holds only ciphertext. The code contains the password: do not post it.

The own-PC code shares your **whole** personal memory: never give it to a teammate; invite them to
a [project brain](teams.md) instead.

## Seeing your memory

`turnbook-view` opens your memory as a 3D graph in the browser (a local file,
`~/.turnbook/brain.html`; only the 3D library is fetched from a CDN). Use `--no-text` before you
screenshot or share it.

Vulnerabilities: report privately (GitHub **Security → Report a vulnerability**); see
[SECURITY.md](../SECURITY.md).
