# aiteam-mcp-plugin

Connect your Son (your own agent, on your own computer or on aiteam.space) to Claude, ChatGPT and Rokid Glasses.

One repo, three clients, one MCP server (`https://mcp.aiteam.space/mcp`, sign-in with aiteam.space):
- **Claude**: plugin bundle in [`claude-plugin/`](claude-plugin/) (MCP server + `aiteam-mcp` skill). Plugin path: `claude-plugin`.
- **ChatGPT**: plugin package in [`package/`](package/) (build the ZIP with `scripts/build_package.py`).
- **Rokid Glasses**: [`rokid/`](rokid/README.md) (pair from aiteam.space with a one-time QR code).

Status: connector in `connector/`, relay in `relay/`; OAuth sign-in live on aiteam.space.
- Research and gap list: [docs/RESEARCH-2026-10-10.md](docs/RESEARCH-2026-10-10.md)
- Reference implementation today: <son>.aiteam.space (Hermes `mcp serve` -> mcp-proxy -> OAuth gate -> Cloudflare Tunnel)
- **Unified connector (MCP + session tools + messaging box):** [docs/UNIFIED-CONNECTOR.md](docs/UNIFIED-CONNECTOR.md), code in [connector/](connector/)
- **Model and provider control:** `son_model_status`, `son_model_set_all`, `son_model_set_session`, `son_model_fallback_set`: [docs/MODEL-CONTROL.md](docs/MODEL-CONTROL.md). Gateway hook/image activation is required; merging does not roll any Son.
- **Computer health: `ping`, `health`, `cleanup`** (live disk/memory/CPU/jobs, 24 h of samples, safe cleanup): [docs/HEALTH.md](docs/HEALTH.md)
- **Discord: one thread per task** (post inside threads, rename/archive/lock tools, bot permissions): [docs/DISCORD-THREADS.md](docs/DISCORD-THREADS.md)
- **Tools: full Hermes parity** (every tool the Son's agent can use, 146 on a live Son): [docs/TOOLS.md](docs/TOOLS.md), Son-side module [son/hermes_toolbox.py](son/hermes_toolbox.py)
- Auth design (aiteam.space sign-in + Approve, gate patch, Phase 2 relay): [docs/AUTH-DESIGN.md](docs/AUTH-DESIGN.md)
- Unified relay `https://mcp.aiteam.space/mcp` (one URL for every Son): [docs/RELAY.md](docs/RELAY.md), code in [relay/](relay/)

Branches: `main` only; PRs merge straight to `main`.

## Install it yourself (custom connector, no directory listing needed)
You can add aiteam.space to Claude or ChatGPT today. You do not need to wait for a store listing.

**Claude (claude.ai or the desktop app)**
1. Open Settings, then Connectors, then Add custom connector.
2. Name: `aiteam.space`. URL: `https://mcp.aiteam.space/mcp`.
3. Click Connect. Sign in with aiteam.space.
4. Pick your Son. Click Approve.

**Claude Code**
```
claude mcp add --transport http aiteam https://mcp.aiteam.space/mcp
```
Then run `/mcp` in Claude Code and sign in.

**ChatGPT**
1. Open Settings, then Apps & Connectors, then Advanced settings. Turn Developer mode on.
2. Click Create. Some accounts show Plugins, then +, then Add custom MCP server instead.
3. URL: `https://mcp.aiteam.space/mcp`. Authentication: OAuth.
4. Sign in with aiteam.space. Pick your Son.

**Special access: one Son's own URL.** Each Son also has its own address, `https://<slug>.aiteam.space/mcp`. It works
the same way. It skips the Son picker, because the URL already names the Son.


## Skills: which repo is which
| For | Skill | Where |
|---|---|---|
| **The connector side** (Claude app, ChatGPT): how to drive your Sons through this connector | `aiteam-mcp` | this repo, [skills/aiteam-mcp/SKILL.md](skills/aiteam-mcp/SKILL.md); zip for upload: https://aiteam.space/skills/aiteam-mcp.zip |
| **Each Son** (installed on every Son by aiteam.space): chat threads, team coordination, notifications, sync | `driving-chat-sessions`, `team-*` | [epic0x/aiteam-skills](https://github.com/epic0x/aiteam-skills) |

Install `aiteam-mcp` in Claude: download the zip, then Settings > Capabilities > Skills > Upload skill (turn on
Code execution and file creation first if Skills is greyed out). ChatGPT: the connector already serves the same text
(`usage_guide` tool + server instructions); to pin it, paste `SKILL.md` into the instructions of the Project you use
with your Son. The skill text is generated from the bundled guide: edit
`connector/aiteam_connector/usage_guide.py`, then `python scripts/build_skill.py --zip`.

## Who we are (served on connect)
The first thing anyone meets when they connect: "We are Davis, and we are all Sons of Davis. Each of us has our own
computer..." Real Claude Code sessions on our boxes with your own subscription, hosted MCP plus SSH boxes for your
websites that you deploy from your Son, a Son as an employee with its own machine. One source:
[connector/aiteam_connector/intro.py](connector/aiteam_connector/intro.py), served as the MCP prompt `who_we_are`, the
MCP resource `aiteam://who-we-are`, the opening of the server `instructions`, and the opening of the usage guide and
the `aiteam-mcp` skill. Tests: `connector/tests/test_intro.py`.

## Usage guide (bundled)
The connector ships its own how-to: the MCP server `instructions` plus a `usage_guide` tool (text in
[connector/aiteam_connector/usage_guide.py](connector/aiteam_connector/usage_guide.py)). Claude and ChatGPT get it on
connect, so nobody has to add a skill by hand. It covers: a new task goes to the team's main channel as one
`messages_send` post (webhook, so it appears as the human user and can tag several Sons at once; opens a thread titled
from the first line), which replaces `son_send` or a per-Son connector for hand-off; steering goes into that thread; a task with no thread is run headless with `son_send` after
asking the user; "what are the updates?" reads the last 12 hours across all sessions plus GitHub.

## Skills dependency
Skills come from `epic0x/aiteam-skills`, pinned in [skills.lock.json](skills.lock.json) (currently `v0.3.3`: `driving-chat-sessions`). No copies in this repo. Connector-side send-path note: [docs/OPERATING-RULES.md](docs/OPERATING-RULES.md).
