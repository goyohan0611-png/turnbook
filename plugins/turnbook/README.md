# Turnbook for Claude Code

Local memory for Claude Code. Every conversation is saved on your PC, verbatim, with no LLM
calls to save it. Before Claude answers, the past turns that bear on your message are added
(a small packet, held to a similarity floor so unrelated memory stays out). What the agent did,
the files it edited and the commands it ran, is kept too.

## Install

Needs [uv](https://docs.astral.sh/uv/) on your PATH.

```
/plugin install turnbook --marketplace goyohan0611-png/turnbook
```

The plugin starts the Turnbook memory server (`uvx --from turnbook turnbook`), which starts a
background service that reads Claude Code's transcripts in `~/.claude/projects` and stores them
in `~/.turnbook`. A `UserPromptSubmit` hook adds the related memory to each message.

## Tools

`brain_context`, `brain_recall`, `brain_remember`, `brain_forget`, `brain_folder`,
`code_activity`, `brain_checkpoint`. See the
[main README](https://github.com/goyohan0611-png/turnbook#readme).

## Data

Everything stays on your machine (`~/.turnbook`). Nothing is sent anywhere unless you set up
sharing between your own PCs yourself. Secrets are masked before storing; `brain_forget`
deletes what you pick. Details: [docs/privacy.md](https://github.com/goyohan0611-png/turnbook/blob/main/docs/privacy.md).

Uses the [EmbeddingGemma](https://huggingface.co/onnx-community/embeddinggemma-300m-ONNX)
model, downloaded once from Hugging Face.

## License

Apache-2.0
