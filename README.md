<!-- mcp-name: io.github.goyohan0611-png/turnbook -->
<div align="center">

<img src="https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/demo/logo.png" alt="Turnbook" width="112">

# Turnbook

### Your AI forgets everything when the session ends. This one remembers, and it never calls an LLM to do it.

**No AI summaries: every conversation is kept word for word, so nothing is lost or made up on the way in, and saving costs zero model tokens. 91.8% on LongMemEval from 3.1k tokens of memory per question ([details](#does-it-work)). Runs on your own PC.**

[![][license-shield]][license-link]
[![][python-shield]][python-link]
[![][platform-shield]][platform-link]
[![][longmemeval-shield]][benchmarks-link]
[![][ci-shield]][ci-link]
[![][pypi-shield]][pypi-link]
[![goyohan0611-png/turnbook MCP server](https://glama.ai/mcp/servers/goyohan0611-png/turnbook/badges/score.svg)](https://glama.ai/mcp/servers/goyohan0611-png/turnbook)
[![Listed on mcpservers.org](https://mcpservers.org/badge.svg)](https://mcpservers.org/servers/goyohan0611-png/turnbook)
[![Mentioned in Awesome MCP Servers](https://awesome.re/mentioned-badge.svg)](https://github.com/punkpeye/awesome-mcp-servers#knowledge--memory)

*Decide something with one agent on Monday. Ask another on Wednesday. It knows.*

| | Typical memory layers (Mem0, Mastra, Supermemory, ...) | **Turnbook** |
|---|---|---|
| How a conversation is saved | an LLM rewrites it into facts or notes | **word for word** |
| What saving costs | LLM API calls for every conversation, for as long as you use it | **nothing** |
| API key or cloud plan | needed | **none** |
| Where your conversations go | to the LLM provider, and their cloud if hosted | **stay on your PC** |
| What gets saved | what the model or your code chooses to add | **every turn, automatically** |
| Which apps share it | the app you build it into | **Claude Code, Codex, Cursor, Copilot CLI, Gemini CLI and OpenCode, as they are** |
| LongMemEval, gpt-5-mini answering | up to 94.9% (Mastra) | 91.8% from 3.1k tokens |

<sub>All 500 LongMemEval_S questions, official gpt-4o judge. Rewriting every conversation with an LLM buys a few points on questions that combine sessions or dates; Turnbook keeps the original words instead and spends nothing to save them. <a href="#how-it-compares">How it compares</a></sub>

**English** · [한국어](README.ko.md)

<img src="https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/demo/demo.gif" alt="Recorded live: Claude Code saves a decision to Turnbook, and Codex, a different app, recalls it" width="900">

<sub>Recorded live on one PC: Claude Code (Opus 5.5, effort high) and Codex (GPT-6.1-Sol, effort high) sharing one Turnbook memory. Model thinking time is fast-forwarded.</sub>

If Turnbook saves you tokens or repeated explanations, a ⭐ helps other people find it.

</div>

## Install

Windows: download [Turnbook-Setup.cmd](https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/scripts/Turnbook-Setup.cmd)
and double-click it, or in PowerShell:

```powershell
irm https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/scripts/install.ps1 | iex
```

macOS / Linux:

```bash
curl -LsSf https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/scripts/install.sh | sh
```

No Python needed; nothing else to set up. Restart your AI apps afterwards. Already have Python?
`pip install turnbook && turnbook-install`. Rather read the script and pin a version first?
[Careful install](docs/install.md#careful-install-read-first-pin-a-version).

Only Claude Code, and [uv](https://docs.astral.sh/uv/) installed? It is also a plugin:

```
/plugin install turnbook --marketplace goyohan0611-png/turnbook
```

<details>
<summary>Connecting an MCP client by hand</summary>

After `pip install turnbook`:

```json
{
  "mcpServers": {
    "turnbook": {
      "command": "python",
      "args": ["-m", "turnbook.mcp_server"]
    }
  }
}
```

Claude Code: `claude mcp add turnbook -- python -m turnbook.mcp_server`. This gives the
tools only; `turnbook-install` also saves every conversation automatically.
</details>

## Try it

Work as usual for a while, then open a new session, another app, or another model and ask:

- *"Where did we leave off yesterday?"*
- *"Which database did we pick for the billing service, and why?"*
- *"What did you change in the auth middleware last week?"*
- *"What's my dog's name?"* (any fact you mentioned once, in passing)

Nothing to tag or save by hand: every turn is already stored, and the agent pulls back the few
that answer the question.

## What it does

- **Every agent, one memory.** Claude Code, Codex, Cursor, Copilot CLI, Gemini CLI, OpenCode and
  any MCP app read and write the same memory. Decide with one, ask another; switch models, the
  memory stays.
- **The right memory in every message.** In Claude Code, Codex, Copilot CLI, Gemini CLI and
  OpenCode, Turnbook looks up what your message needs from the past and adds it before the model
  answers. Nothing to ask for.
- **Free to save, on your PC.** Every turn is stored word for word in one SQLite file: no model
  call, no tokens, no account, no cloud.

<details>
<summary>Everything else</summary>

- **Made for orchestration.** When an orchestrator runs several agents at once, they all write
  to the same memory as they work, so what one agent decided is there for the next. Every
  memory keeps which app and session it came from.
- **Holds up as memory piles up.** Grow a memory tenfold and the answer is still among the first
  30 found for 92.7% of questions (92.6% before); a search takes about 0.1 s.
  [As the memory grows](#as-the-memory-grows)
- **Any language, across languages.** The search model reads Korean almost as well as English:
  ask in Korean about something said in English, or the other way round. [In Korean](#in-korean)
- **Your project's documents too.** `turnbook docs add FOLDER` keeps its Markdown and text files
  searchable beside the conversations, section by section, and up to date as they change.
- **One memory across your PCs.** Run `turnbook folder share`, then `turnbook folder <code>` on
  the other PC (the AI tells you the command; it never moves your memory by itself, so a web page
  cannot either). Office network or [Tailscale](https://tailscale.com), over TLS, every line
  encrypted before it leaves a PC.
- **Hand a memory to someone else as a file.** `turnbook export FILE` writes all of it;
  `--project FOLDER` writes only the conversations held in that folder. They run
  `turnbook import FILE`.
- **Yours.** No account, no cloud of its own, no telemetry. Secrets are masked before anything is
  stored, and *"delete the password I typed earlier"* deletes it everywhere. [docs/privacy.md](docs/privacy.md)
- **Teams, if you want them.** Share a project's decisions and progress with teammates' AIs, never
  your personal memory; files stay in Slack and GitHub. [docs/teams.md](docs/teams.md)

</details>

## Works with

The installer finds these and connects them; run it again after installing a new one.

| app | conversations saved | how |
|---|---|---|
| Claude Code | every turn | reads its transcripts; a hook adds relevant memory to each message |
| Codex CLI | every turn | reads its transcripts; a hook adds relevant memory to each message (trust it once in `/hooks`) |
| Cursor | every turn | reads its transcripts |
| Copilot CLI | every turn | reads its transcripts; a hook adds relevant memory to each message |
| Claude Desktop (Cowork) | every turn | reads its session files |
| Gemini CLI | every turn | hooks; also adds relevant memory before each answer |
| OpenCode | every turn | plugin; also adds relevant memory before each answer, for any model |
| Claude Desktop chat, Windsurf, VS Code Copilot, Cline, LM Studio | what the model saves | MCP tools |

Any other MCP client works through the tools: see the hand-written config above.

## How it works

```mermaid
flowchart LR
    A["Claude Code · Codex · Cursor<br/>Gemini CLI · OpenCode · ..."] -- "transcripts, hooks, plugin" --> C["capture service<br/>(runs in the background)"]
    C -- "verbatim turns,<br/>secrets masked" --> S[("your memory<br/>on this PC")]
    S --> I["local search<br/>words + meaning + time"]
    A -- "a question that needs the past" --> I
    I -- "the few turns that answer it<br/>(a few thousand tokens)" --> A
```

- **Saving costs nothing.** Turns are stored as they were said. No model summarises or extracts
  anything at write time, so nothing is lost or invented on the way in, and no tokens are spent.
- **Search runs on your PC.** Word search, a local embedding model (Google's EmbeddingGemma,
  4-bit; ONNX, no GPU) and when things were said are combined, then ordered by a small learned
  score; the agent gets a short packet with the source turns, not a dump of history.
- **The model you already use does the thinking.** Turnbook hands it evidence; it answers.
  When models get better, the answers get better without changing the memory.

## Beyond agent memory

Underneath, Turnbook is RAG over chat history with everything before the answer built in. A
typical RAG stack is assembled from parts; Turnbook runs every step up to the answer on its own
and leaves only the answer to the model you already use:

| Step | Typical RAG stack | Turnbook |
|---|---|---|
| Collect | an ingestion script you write | every turn saved automatically from your agents |
| Chunk | splitting rules to tune | one turn per entry, word for word |
| Embed | an embedding API, paid per token | a local model, free |
| Store | a vector database to run | one SQLite file |
| Retrieve | search code to write | words, meaning and time, re-ranked by a small learned score |
| Hand over | prompt-assembly code | a bounded evidence packet over MCP |
| Answer | your LLM | your LLM (Claude, Codex, ...) |

The same engine could serve a chatbot that remembers each of its users, a support bot that
recalls past tickets, or a team's searchable decision log. Today it ships as a personal, local
memory; a Python API and multi-user server mode are not built yet. Interested? Open an issue.

## Does it work?

[LongMemEval](https://github.com/xiaowu0162/LongMemEval) is the public benchmark for long-term
chat memory: 500 questions over about 50 past sessions each. Turnbook's scores use gpt-5-mini as
the answer model with the benchmark's own prompt and official gpt-4o judge; a question left
unanswered counts as wrong.

| text the answer model reads (median) | accuracy | model tokens spent **saving** memory |
|---|---|---|
| **3.1k tokens** (the default) | **91.8%** (459/500) | **0** |
| 25k tokens | 93.0% (465/500) | 0 |

### How it compares

Other memories publish their own LongMemEval scores, each with its own answer model, judge and
setup, so they are not one ranking. The closest to like-for-like:
[Mastra's Observational Memory](https://mastra.ai/research/observational-memory) reports 94.9%
with the same answer model, gpt-5-mini, which reads about 30k tokens of observations that
gemini-2.5-flash wrote while the conversations were saved. It and others (Mem0, Supermemory) run
an LLM over every conversation when saving it, writing dated facts and replacing outdated ones; that is
where Turnbook loses points: 33 of its 41 misses are questions that combine several sessions,
reason about dates, or need the newest value of something that changed. Turnbook keeps the
original words, spends no model tokens saving them, and runs on your own PC.

### What makes the difference

**The learned re-ranker.** Without it (plain rank fusion, no weight fitted on any benchmark):
87.0% of all 500, and 86.8% of the 380 questions never used for tuning, where the shipped search
scores 91.1%. It helps most where answers are spread over several sessions.
[norerank](research/runs/graph-v9.14-full500-mpnet8-norerank)

**The memory added to each message** (4 items, ~300 tokens) is a hint, not the full search: alone
it answers 61.4% of the same 500; `brain_context`, which the model calls for more, answers 91.8%.
It does not get in the way of unrelated work: on 200 GSM8K math problems with an unrelated
person's memory attached, gpt-5-mini solved 191, against 193 without it (p = 0.69).
[hook](research/runs/graph-v9.14-full500-mpnet8-hook) ·
[interference](research/runs/graph-v9.23-hook-interference)

### As the memory grows

More past conversations means more that looks like the one you mean. On LoCoMo (1,536 questions),
with each person's memory grown tenfold by storing other people's conversations beside it, the
answer turn stays among the first 30 found for 92.7% of questions (92.6% before it grew);
Turnbook with its re-ranker turned off drops from 90.2% to 88.4%. A search takes 30 ms at 5,882 turns and 116 ms at 55,882.
Larger memories are being measured.
[scale](research/runs/graph-v9.20-learning/scale_10y-embeddinggemma-300m-ONNX.json)

<p align="center"><img src="https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/demo/scale.png" alt="Answer turn among the first 30 found: 92.6 percent at 5,882 turns, 92.7 percent at 55,882; among the first 10: 81.4 to 81.9 percent" width="720"></p>

### In Korean

Four LoCoMo conversations translated into Korean (582 questions). Search by meaning alone, share
of questions whose answer turn is among the first 30 found: English 90.2%, Korean 86.9%, a Korean
question about an English memory 87.8%. Word search finds "콩이" in "콩이를" (Korean particles).
[research/runs/graph-v9.21-korean](research/runs/graph-v9.21-korean)

Every question, answer and verdict is in [`research/runs/`](research/runs); method, failures and
how to rerun it: [docs/benchmarks.md](docs/benchmarks.md).

## FAQ

**Will it slow my PC down?** The background service uses about 2% of one CPU core when nothing new
is coming in. An app's memory server holds about 525 MB. A memory search takes about 40 ms
(46 ms at the 95th percentile on a GitHub runner);
the first one after an app starts, about 1.4 s. Each new turn takes about 0.1 s to embed; the
service runs below normal CPU priority and embeds backlogs at half pace. On a first install, the
conversations already on the PC are read in the background while word search answers at once.

**Which search model?** [EmbeddingGemma](https://huggingface.co/google/embeddinggemma-300m)
(about 200 MB, downloaded from Hugging Face on first use). A memory from an older version is
read again with it in the background and moves over by itself. `turnbook model` shows which
model is in use; `turnbook model
REPO@FILE` switches to any ONNX embedding model on Hugging Face (memory is read again with it in
the background). A model you picked is never deleted.
EmbeddingGemma is provided under the
[Gemma Terms of Use](https://ai.google.dev/gemma/terms); it is not part of Turnbook's own code
or license.

**What does it cost?** Nothing. No API key and no model calls of its own; your AI app reads a
packet of a few thousand tokens when a question needs the past.

**Does anything leave my PC?** No, unless you share the memory with your own other PCs or a team.
No telemetry. Secrets (API keys, passwords, card numbers...) are masked before storing.

**How is this different from CLAUDE.md or an app's built-in memory?** Those keep notes a model
wrote, one app at a time. Turnbook keeps every turn as it was said and shares it across every
app and model on the PC.

**Korean?** Yes: Korean and English conversations, and questions in either language. With the
default model, search by meaning trails English by about 3 points in Korean, and a Korean
question finds an English memory about as often: [In Korean](#in-korean).

**How do I remove it?** `turnbook-uninstall` disconnects every app and stops the service; the
memory stays in `~/.turnbook` unless you add `--purge`.

> [!NOTE]
> Used daily on Windows. macOS and Linux pass every test in CI on every change, including an
> upgrade from the previous release, but have seen less daily use: issues are welcome.

## More

[MCP tools](docs/install.md#mcp-tools) · [See your memory in 3D](docs/privacy.md#seeing-your-memory)
(`turnbook-view`) · [Changelog](CHANGELOG.md) · [Security](SECURITY.md) ·
Development: `pip install -r requirements.txt`, then `python -m unittest discover -s tests`

Contributions are welcome. License: [Apache-2.0](LICENSE).

<!-- Links -->
[license-shield]: https://img.shields.io/badge/license-Apache--2.0-4dc9f6?style=flat-square
[license-link]: LICENSE
[python-shield]: https://img.shields.io/badge/python-3.10+-7dd8f8?style=flat-square&logo=python&logoColor=white
[python-link]: https://www.python.org/
[platform-shield]: https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-b0e8ff?style=flat-square
[platform-link]: docs/benchmarks.md#known-limits
[longmemeval-shield]: https://img.shields.io/badge/LongMemEval-91.8%25-2ea44f?style=flat-square
[benchmarks-link]: docs/benchmarks.md
[ci-shield]: https://img.shields.io/github/actions/workflow/status/goyohan0611-png/turnbook/tests.yml?style=flat-square&label=tests
[ci-link]: https://github.com/goyohan0611-png/turnbook/actions/workflows/tests.yml
[pypi-shield]: https://img.shields.io/pypi/v/turnbook?style=flat-square&label=pypi&cacheSeconds=3600
[pypi-link]: https://pypi.org/project/turnbook/
