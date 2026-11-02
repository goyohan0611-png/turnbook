# Installing Turnbook

## One step, no Python needed

Windows: download [Turnbook-Setup.cmd](https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/scripts/Turnbook-Setup.cmd)
and double-click it (Windows may ask once whether to run a downloaded file), or in PowerShell:

```powershell
irm https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/scripts/install.ps1 | iex
```

macOS or Linux:

```bash
curl -LsSf https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/scripts/install.sh | sh
```

It installs [uv](https://docs.astral.sh/uv/) for your user (no administrator rights), which brings
its own Python; installs Turnbook with it; and connects every AI app it finds. Restart your AI
apps afterwards.

## With Python already set up

Python 3.10+ (64-bit); Windows, macOS or Linux, on x86-64 or 64-bit ARM:

```bash
pip install turnbook
turnbook-install
```

## Claude Code plugin

If Claude Code is the only app and [uv](https://docs.astral.sh/uv/) is on your PATH:

```
/plugin install turnbook --marketplace goyohan0611-png/turnbook
```

Codex: `codex plugin marketplace add goyohan0611-png/turnbook-plugin`, then `codex plugin add turnbook@turnbook`.

(or `claude plugin marketplace add goyohan0611-png/turnbook` and
`claude plugin install turnbook@turnbook` in a shell). The plugin runs the memory server and the
message hook through `uvx`; the server starts the capture service. It does not add the login item
or connect other apps: `turnbook-install` does. With both, the installer's hook adds the memory
and the plugin's stays quiet.

## Careful install: read first, pin a version

The one-step commands run a script straight from GitHub, and that script runs uv's own installer.
If you would rather not pipe a script into a shell:

1. Read [scripts/install.ps1](../scripts/install.ps1) or [install.sh](../scripts/install.sh)
   first, or skip them and install uv yourself from
   [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/).
2. Install one exact version, so nothing changes until you choose to upgrade:

   ```bash
   uv tool install --python 3.12 turnbook==1.1.0
   turnbook-install
   ```

   Every release is on [PyPI](https://pypi.org/project/turnbook/#history) and in
   [CHANGELOG.md](../CHANGELOG.md).
3. `turnbook-install` lists the settings it will change (see
   [What the installer does](#what-the-installer-does)) and asks before touching any; `--yes`
   skips the question. Back up `~/.claude.json` and
   `~/.claude/settings.json` first if you want to compare. `turnbook-uninstall` removes what it
   added (`--purge` also deletes the memory).

To try it without touching your own setup, run it in Windows Sandbox or a virtual machine first.

Or from source:

```bash
git clone https://github.com/goyohan0611-png/turnbook.git
cd turnbook
pip install -e .
turnbook-install
```

## What the installer does

- installs the packages (no PyTorch: the embedding model runs on ONNX Runtime);
- registers the MCP server with every app it finds: Claude Code, Claude Desktop (including the
  Microsoft Store build), Codex, OpenCode, Cursor, Windsurf, Gemini CLI, Copilot CLI, VS Code
  (Copilot), Cline and LM Studio;
- where an app keeps no transcript to read, adds its own way of saving the conversation: a
  plugin for OpenCode, hooks for Gemini CLI (next to any hooks you already have; uninstall removes
  only ours). Both also add the memory that bears on a message before the model answers;
- starts the capture service at login (Windows Startup folder, a macOS LaunchAgent, or an XDG
  autostart entry on Linux);
- downloads the embedding model (about 290 MB, once) and removes the one 0.9.9 and earlier used.

Running it again is safe; run it again if you move the folder. Options: `--name "Your Name"` (the name
teammates see), `--join <code>` (another PC's memory or a team invite). The server is also listed
in the [MCP Registry](https://registry.modelcontextprotocol.io) as
`io.github.goyohan0611-png/turnbook`.

The first time a machine hosts a memory or a hub, Turnbook downloads Postgres 16 for it once (12
MB, 46 MB on Windows; checked against a pinned hash) and keeps it in the user's cache folder.

## Models tested through OpenCode

OpenCode connects the memory server and hands its tools to whichever model it runs, so any model
that can call tools can use the same memory. How well it uses it is the model's own: whether it
saves a fact without being told to, and looks it up later. `research/opencode_models.py` checks
both in two fresh sessions, with a throwaway memory: the user mentions their dog in passing, then
a new session asks *"What breed is my dog, and what's his name?"*.

<!-- results: research/runs/opencode-models.jsonl -->
Measured 2026-10-16, OpenCode 1.18, its free models:

| model | saved it unasked | found it in a new session |
|---|---|---|
| opencode/big-pickle | yes | yes |
| opencode/deepseek-v4.1-flash-free | yes | yes |
| opencode/mimo-v2.6-flash-free | yes | yes |
| opencode/cat-jam-free | yes | yes |
| opencode/longcat-2.5-preview-free | yes | yes |
| opencode/muse-spark-1.3-contributor-free | yes | not yet re-run (see below) |
| opencode/nemotron-3-ultra-free | yes | not yet re-run |
| opencode/nemotron-3.5-lightning-free | yes | not yet re-run |

Every model saved the fact without being asked and searched for it in the new session. Before
0.9.0 five of them were then told nothing was saved: they searched "dog", which shares no word
with "our golden retriever Kongi", and a fresh server gave the model 0.5 s to load before
skipping search by meaning. Two of those five were re-run after the fix and found it.

Local models (Ollama) connect the same way, but need a PC that can run them: on a laptop with no
GPU, OpenCode's own prompt (~18,000 tokens) took ~27 minutes per turn with qwen3:4b, and even the
memory tools alone timed out. Not yet measured on a GPU.

OpenCode's conversations are now captured by the installed plugin; a manual MCP connection alone
provides the tools, while `turnbook-install` adds capture and per-message memory injection.

## MCP tools

| tool | what it does |
|---|---|
| `brain_context` | a bounded packet of the past turns and memories that answer a request; `recent=true` for "where did we leave off?" |
| `brain_recall` | look memories and captured turns up directly; `entity=` for everything about one thing, in order |
| `brain_remember` | save a sourced memory or decision (secrets masked) |
| `brain_folder` | where the memory lives and which PCs share it; changes (`turnbook folder`, `turnbook export`, `turnbook import`) are run by you in a terminal |
| `brain_forget` | "delete the password I typed": lists masked candidates, deletes only what you pick, everywhere |
| `code_activity` | what changed in a project, file or symbol, and when (when a code folder is watched) |
| `brain_checkpoint` | [project brains](teams.md), for teams that use them |
| `brain_work` (unreleased prototype) | local task assignment, progress and handover between connected agents; [protocol and limits](design/agent-continuity.md) |

Few tools on purpose: every tool's description is read by the model on every turn, and similar
tools get confused with each other.

## Trouble

- **`turnbook-install` is "not recognized"**: pip put it in a Scripts folder that is not on your
  PATH (common with the Windows Python install manager). `python -m install` runs the same thing.
- **A DLL load error**: Windows is missing the Microsoft Visual C++ runtime the embedding runtime
  needs. Install [vc_redist.x64.exe](https://aka.ms/vs/17/release/vc_redist.x64.exe), restart, and
  run the installer again.
- **pip fails with "No such file or directory"**: Windows' 260-character path limit. Clone to a
  short path such as `C:\turnbook`, or enable long paths (the installer prints the command).

## Removing it

`turnbook-uninstall` unregisters it from every app, removes the login item and stops the capture
service. Your memory stays in `~/.turnbook` unless you add `--purge`. Then
`pip uninstall turnbook` (or `uv tool uninstall turnbook`) removes the program.
