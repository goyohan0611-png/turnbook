# Project brains (optional, for teams)

Turnbook is a personal memory first. Teams that want it can add project brains: everyone keeps a
private brain, and every project they choose to share has a shared one. Nothing personal moves into
a project by itself, only what someone publishes. Files and the talk about them stay in Slack, a
drive or GitHub; the project brain is what lets another person's AI pick the work up.

> A: *"I fixed the Postgres issue. Share it."* → a checkpoint: what changed and why, files, tests,
> the commit at the stage it reached, what is left.
> A: *"I posted the proposal in Slack. Note it in the project."* → a checkpoint with the Slack link.
> B, next day, in another app: *"What happened with the Postgres issue, and what is left?"*

- **Checkpoints**: decision, progress, blocker, next action, artifact, release, with their context
  (what changed, where, why, how it was verified, what is left). Later ones can supersede them; a
  mistaken one can be retracted.
- **Links, not files**: a git folder is linked as its commit with the stage it really reached
  (committed, pushed, PR open, merged, released), so a commit on one PC is never reported as done;
  a Slack message or a drive document is kept as its link.
- **Instructions**: standing guidance for every AI that works on the project. Your AI proposes
  the text; you see it and apply it in a terminal (`turnbook team instructions "<project>"`),
  so a page your AI read cannot rewrite what every teammate's AI follows.
- **Members, as on GitHub**: whoever creates a project owns it. Owners add people (owner, editor or
  viewer), set the instructions and can retract anyone's checkpoint; editors publish; viewers read.
  *"Invite Mina to the turnbook project as an editor"* gives you a command to run in a
  terminal (`turnbook team invite "Mina" --role editor --project "turnbook"`), which prints a
  code for her; her AI joins with it. Giving access and hosting are never done from a chat, so a
  page or file your AI reads cannot add someone. Every action, reads included, is audited.
- When a project's hub cannot be reached, the assistant says the latest state is unknown instead of
  guessing.

## Where projects live

**On a Postgres hub**: a PC, server or NAS. Each project is its own schema with its own groups and
key, in a database apart from anyone's personal memory, so it can be backed up, moved or removed
whole; the database itself enforces each project's permissions, and teammates' logins cannot reach
anyone's personal memory. Whoever runs the hub runs the server and sees a project only when its
owner adds them through the tools; as the machine's administrator they could still read the
database itself, so host it on a machine only the operator logs in to. Teammates reach it on the office network, or from anywhere over
[Tailscale](https://tailscale.com) (install it on the hub and on each PC, same tailnet; invite
codes carry the hub's Tailscale address and use it when the office network is not there).

A server needs no AI and no API key, only Turnbook (any Python from 3.10; Windows, macOS, or
Linux on x86-64 or 64-bit ARM, such as a Raspberry Pi or an ARM NAS):

```bash
turnbook-hub host                # once: this machine becomes a hub (it stays one)
turnbook-hub add-user Mina       # an account and the code to give her (no projects yet)
turnbook-hub users               # accounts and their roles; never what is in a project
turnbook-hub remove-user Mina --transfer-to Jun   # projects only she owned go to Jun
turnbook-hub delete-project NAME --confirm NAME
turnbook-hub audit
```

**On this PC alone**: without a hub, projects stay in `~/.turnbook/ledger.sqlite`.
