# aiteam.space connector for Claude and ChatGPT

Run your Son from Claude or ChatGPT. A Son is your AI teammate on its own computer: it builds and deploys sites,
runs scheduled jobs, joins calls, posts in your team chat and reports back. This connector lets Claude or ChatGPT
sign it in, switch its model, steer its sessions, set its schedules and hand it work, by text or by voice.

- **Website:** https://aiteam.space
- **Create a Son (free sign-up):** https://aiteam.space/new
- **Pricing and add-ons:** https://aiteam.space/pricing
- **Connect help (short videos):** https://aiteam.space/help/connect
- **Support:** https://aiteam.space/support
- **Legal:** [Terms](https://aiteam.space/legal/terms) · [Privacy](https://aiteam.space/legal/privacy) · [Refunds](https://aiteam.space/refunds)
- **MCP server URL:** `https://mcp.aiteam.space/mcp` (OAuth: sign in with aiteam.space)

## Contents
1. [Get started in 3 minutes](#get-started-in-3-minutes)
2. [Guided first run: "Help me set up aiteam"](#guided-first-run-help-me-set-up-aiteam)
3. [What the connector controls](#what-the-connector-controls)
4. [Every Hermes command, and how to run it here](#every-hermes-command-and-how-to-run-it-here)
5. [Security](#security)
6. [Free and paid](#free-and-paid)
7. [For developers](#for-developers)

## Get started in 3 minutes
1. **Create a Son** at https://aiteam.space/new (sign in with Google).
2. **Add the connector** to Claude or ChatGPT (below). You do not need to wait for a store listing.
3. **Say "Help me set up aiteam".** Claude or ChatGPT walks you through the rest.

**Claude (claude.ai, desktop or mobile app)**
1. Open Settings, then Connectors, then Add custom connector.
2. Name: `aiteam.space`. URL: `https://mcp.aiteam.space/mcp`.
3. Click Connect. Sign in with aiteam.space.
4. Pick your Son. Click Approve.

**Claude Code**
```
claude mcp add --transport http aiteam https://mcp.aiteam.space/mcp
```
Then run `/mcp` in Claude Code and sign in.

**Claude plugin (connector + skill in one install):** plugin path [`claude-plugin/`](claude-plugin/).

**ChatGPT**
1. Open Settings, then Apps & Connectors, then Advanced settings. Turn Developer mode on.
2. Click Create. Some accounts show Plugins, then +, then Add custom MCP server instead.
3. URL: `https://mcp.aiteam.space/mcp`. Authentication: OAuth.
4. Sign in with aiteam.space. Pick your Son.

The ChatGPT plugin package lives in [`package/`](package/) (build the ZIP with `scripts/build_package.py`).

**Rokid Glasses:** pair from aiteam.space with a one-time QR code, see [`rokid/`](rokid/README.md).

**One Son's own URL.** Each Son also has its own address, `https://<slug>.aiteam.space/mcp`. It works the same way
and skips the Son picker, because the URL already names the Son.

## Guided first run: "Help me set up aiteam"
Say **"Help me set up aiteam"** in Claude or ChatGPT (or pick the `setup_aiteam` prompt in Claude's prompt menu).
The assistant goes one step at a time and waits for you after each question.

| Step | The assistant says or does | Tools |
|---|---|---|
| 0. Your Son | Finds your Son. No Son yet: sends you to https://aiteam.space/new. Several Sons: asks which one. | `son_list`, `ping` |
| 1. Brain | "Your Son is live but has no brain yet. Connect a Claude subscription, ChatGPT, or an API key? You can add more than one account." You get a one-time aiteam.space link (15 minutes) for the code or key. Add as many accounts as you like, then set the fallback order. | `ai_status`, `ai_signin_start`, `ai_signin_status`, `ai_fallback_set`, `ai_model_set` |
| 2. Channels | "Want Telegram? Google Meet?" Telegram is free (BotFather token, pasted on your Son's page). Google Meet is the Calls add-on and needs a Google Workspace account with SSO. Discord voice needs no setup. | links to https://aiteam.space/my-sons |
| 3. First task | Suggests one small task, sends it, reports the reply. | `son_send`, `messages_send` |

How it ships: the MCP server `instructions` carry a trigger for the phrase, the MCP prompt `setup_aiteam` carries
the full script, and section 10 of the `usage_guide` tool (and of the `aiteam-mcp` skill) has the same steps.
ChatGPT also gets it as the package's onboarding skill, [package/skills/get-started](package/skills/get-started/SKILL.md).
Source: [connector/aiteam_connector/setup_flow.py](connector/aiteam_connector/setup_flow.py), tests:
`connector/tests/test_setup_flow.py`.

The same steps run in the aiteam.space setup chat. Step 1 there is "connect me to Claude or ChatGPT" (this
connector). If you skip it, the setup chat walks you through the brain and the channels itself.

## What the connector controls
Everything below works from a Claude or ChatGPT chat, including voice mode. Ask in plain words ("switch my Son to
Opus", "stop that session", "every Monday at 9 send me a report"); the assistant picks the tool.

### Sign-in and multiple accounts
- **`ai_status`**: which AI accounts your Son is signed in to, the default model, the model of every session and the
  fallback order. Works on a brand-new Son with no AI yet.
- **`ai_signin_start`**: connect a **Claude subscription** (Pro or Max), **ChatGPT** (Plus or Pro), an **Anthropic
  API key** or an **OpenAI API key**. Returns the provider's sign-in link and a one-time aiteam.space link (expires in
  15 minutes) where you paste the code or key. Claude: paste the code Claude shows. ChatGPT: type the short code on
  the OpenAI page, nothing to paste.
- **Multiple accounts:** run `ai_signin_start` once per account, with an optional `label` ("work", "personal").
  Each one is added to your Son's account pool; nothing is replaced.
- **`ai_signin_status`**: waiting, connected, failed or expired.
- Codes, keys and passwords never go in the chat. See [Security](#security).

Details: [docs/AI-CONTROL.md](docs/AI-CONTROL.md).

### Model and provider switch
- **`ai_model_set`**: switch provider and model for **every session** (default) or **one session**
  (`scope: "session"` + `session_key` or `session_id`).
- **`son_model_status` / `son_model_set_all` / `son_model_set_session`**: the same switch at gateway level, with the
  full per-session inventory (effective route, overrides, what is running now).
- The change applies from the next turn. A turn already running finishes on its current model. Nothing restarts.
- Only models already configured on your Son are accepted, never an arbitrary endpoint.

Details: [docs/MODEL-CONTROL.md](docs/MODEL-CONTROL.md).

### Fallback order
- **`ai_fallback_set`** (or **`son_model_fallback_set`**, which can also set the primary): the ordered list your Son
  falls back to when the main model fails or hits a limit, up to 6 entries. Example: Claude account 1, then Claude
  account 2, then ChatGPT. An empty list clears it. Every entry must be signed in first.

### Approvals
- **`son_approvals_set`**: `smart` (risky commands ask first, safe ones run) or `yolo` (no approval prompts) for the
  Son's profile. Needs your owner binding on the Son.
- **`permissions_list_open` / `permissions_respond`**: see the commands waiting for approval, then approve or deny one.
- Claude and ChatGPT also ask you before any tool marked destructive runs (shell, file writes, schedules).

Details: [docs/GATEWAY-CONTROLS.md](docs/GATEWAY-CONTROLS.md).

### Scheduled jobs (crons): set and stop
- **`cronjob_manage`** with `action`:
  - `create`: `schedule` + `prompt` (+ optional `name`). Schedules: `30m`, `every 2h`, `every monday 9am`,
    `weekdays at 9am`, `0 9 * * *`, `in 30m` (once), or an ISO date (once).
  - `list`: every job, next run, last result.
  - `pause`, `resume`, `remove` (stop for good), `update`, `run` (fire now): with the `job_id` from `list`.
- Jobs run on the Son's main model unless you pin one. `health` shows each job's last run and failure streak.

### Sessions: list, read, steer, stop
- **List and read:** `sessions_list`, `session_status`, `sessions_history`, `sessions_search`.
- **Steer a chat session:** `messages_send` into its Discord thread or Telegram topic. The Son picks it up in that
  session. `sessions_send` steers API, CLI and scheduled sessions.
- **Stop:** `son_session_stop` (hard stop, clears queued work for that one session), `son_session_interrupt` (same
  stop, explicit name), `son_session_reset` (stop and start that conversation fresh).
- **Headless work with no chat:** `son_send` (name the conversation to continue it later), then
  `son_conversations` and `son_conversation_history`.

### Team chat: Discord and Telegram
- **New task:** `messages_send` to the team channel. It posts as you (webhook), tags several Sons at once and opens a
  thread titled from the first line.
- **Discord threads:** `discord_threads_list`, `discord_thread_get`, `discord_thread_create`, `discord_thread_edit`
  (rename, archive, lock), `discord_thread_member_add`, `discord_thread_member_remove`, `discord_thread_members`.
  Docs: [docs/DISCORD-THREADS.md](docs/DISCORD-THREADS.md).
- **Telegram topics:** `telegram_chat_get`, `telegram_topics_list`, `telegram_topic_messages`, `telegram_topic_send`,
  `telegram_topic_create`, `telegram_topic_edit`. Docs: [docs/TELEGRAM-TOPICS.md](docs/TELEGRAM-TOPICS.md).
- **Status questions** ("what are the updates?"): the assistant checks live sessions, threads and GitHub first, then
  answers Done / In progress / Waiting / Next. Docs: [docs/OPERATING-RULES.md](docs/OPERATING-RULES.md).

### The Son's computer
- **`ping`**: is it up. **`health`**: disk, memory, CPU, live sessions, scheduled jobs, last cleanup, 24 h history.
  **`cleanup`**: frees rebuildable files only, at most once per 10 minutes. Docs: [docs/HEALTH.md](docs/HEALTH.md).

### GitHub
- **`github_*`** (27 tools): issues, pull requests (view, diff, checks, review, merge), Actions runs and workflows,
  repo search, through the Son's own `gh` login and permissions.

### Everything else the Son can do
Full parity: shell, files, browser, web search, memory, skills, sub-agents, kanban, media and every plugin tool the
Son has enabled are listed too (about 146 tools on a live Son). List: [docs/TOOLS.md](docs/TOOLS.md), Son-side
module [son/hermes_toolbox.py](son/hermes_toolbox.py).

## Every Hermes command, and how to run it here
Your Son runs on the Hermes agent. Every chat command it has maps to a connector tool. Commands that take a secret
or open a terminal screen are replaced by a safer tool.

| Command | From Claude / ChatGPT |
|---|---|
| `/status`, `/context`, `/help`, `/commands`, `/profile`, `/version`, `/whoami`, `/agents` | `son_slash` |
| `/reasoning`, `/fast`, `/footer` | `son_slash` |
| `/stop` | `son_session_stop` (or `son_slash` with `/stop`) |
| `/new`, `/reset` | `son_session_reset` (or `son_slash`) |
| `/model` | `ai_model_set`, `son_model_set_all`, `son_model_set_session` |
| fallback order (config) | `ai_fallback_set`, `son_model_fallback_set` |
| `/login`, provider sign-in | `ai_signin_start` + `ai_signin_status` (one-time aiteam.space link) |
| `/approvals`, `/yolo` | `son_approvals_set` (smart or yolo) |
| `/approve`, `/deny` | `permissions_list_open`, `permissions_respond` |
| `/cron` | `cronjob_manage` (create, list, update, pause, resume, remove, run) |
| `/steer`, `/queue`, `/btw` | `messages_send` to the session's thread or topic; `sessions_send` for API, CLI and cron sessions |
| `/sessions`, `/history`, `/resume` | `sessions_list`, `sessions_history`, `sessions_search` |
| `/bg` | `son_send` (headless conversation) |
| `/kanban` | `kanban_*` tools |
| `/skills`, `/learn` | `skills_list`, `skill_view`, `skill_manage` |
| `/memory` | `memory` (and the memory provider's tools) |
| `/usage`, `/insights` | `ai_status`, `health` |
| `/platforms`, `/platform` | `channels_list`, `telegram_chat_get`, `discord_threads_list` |
| `/title`, `/branch` | `discord_thread_edit`, `discord_thread_create`, `telegram_topic_edit` |

`son_slash` runs only the commands in its list: no shell, no plugins, no credential commands, nothing typed through
to the model. Session stop, reset, approvals and `son_slash` need the latest Son update; if one of them is not in
your tool list yet, your Son has not received it.

## Security
- **Sign-in:** OAuth with your aiteam.space account. You approve each client. The connector only reaches Sons on
  your account. Design: [docs/AUTH-DESIGN.md](docs/AUTH-DESIGN.md), relay: [docs/RELAY.md](docs/RELAY.md).
- **Secrets never in the chat.** Codes and API keys go on a one-time aiteam.space page (owner only, 15 minutes).
  The assistant is told never to ask for one and never to accept one. If you paste a key by mistake, rotate it.
- **Owner only.** Model, sign-in, approvals and session controls require the verified owner of that Son.
- **Destructive tools are marked**, so Claude and ChatGPT ask you before they run.

## Free and paid
Connecting is free: voice mode, Telegram, WhatsApp, plain Discord chat, GitHub, sign-in, model switch, schedules and
health. Paid add-ons: Calls (Google Meet, Zoom, Discord voice), Discord thread management (two-way sync) and skill
packs. A paid tool you have not added answers with a short message and a link instead of running. Prices:
https://aiteam.space/pricing. Map: [docs/ENTITLEMENTS.md](docs/ENTITLEMENTS.md).

## For developers
- **Layout:** `connector/` (the MCP server), `relay/` (`mcp.aiteam.space`, one URL for every Son),
  `claude-plugin/` (Claude bundle), `package/` (ChatGPT package), `hermes-plugin/` (AI sign-in plugin on the Son),
  `son/` (Son-side tool parity and model control), `rokid/`.
- **Usage guide and skill:** the connector serves its own how-to (server `instructions` + `usage_guide` tool), from
  [connector/aiteam_connector/usage_guide.py](connector/aiteam_connector/usage_guide.py). The `aiteam-mcp` skill
  ([skills/aiteam-mcp/SKILL.md](skills/aiteam-mcp/SKILL.md), zip: https://aiteam.space/skills/aiteam-mcp.zip) is
  generated from it: edit the guide, then `python scripts/build_skill.py --zip`. Install in Claude: Settings >
  Capabilities > Skills > Upload skill. ChatGPT: the connector already serves the same text.
- **Introduction on connect:** prompt `who_we_are` and resource `aiteam://who-we-are`, from
  [connector/aiteam_connector/intro.py](connector/aiteam_connector/intro.py).
- **Son-side skills** (chat threads, team coordination, notifications, sync) come from
  [epic0x/aiteam-skills](https://github.com/epic0x/aiteam-skills), pinned in [skills.lock.json](skills.lock.json).
- **Tests:** `cd connector && pip install -e ".[test]" && python -m pytest -q`; package and plugin checks in
  `tests/`. Local runner: `scripts/box-test.sh`.
- **Docs:** [UNIFIED-CONNECTOR](docs/UNIFIED-CONNECTOR.md) · [AI-CONTROL](docs/AI-CONTROL.md) ·
  [MODEL-CONTROL](docs/MODEL-CONTROL.md) · [GATEWAY-CONTROLS](docs/GATEWAY-CONTROLS.md) · [TOOLS](docs/TOOLS.md) ·
  [HEALTH](docs/HEALTH.md) · [DISCORD-THREADS](docs/DISCORD-THREADS.md) · [TELEGRAM-TOPICS](docs/TELEGRAM-TOPICS.md) ·
  [ENTITLEMENTS](docs/ENTITLEMENTS.md) · [AUTH-DESIGN](docs/AUTH-DESIGN.md) · [RELAY](docs/RELAY.md) ·
  [OPERATING-RULES](docs/OPERATING-RULES.md) · [SUBMISSION](docs/SUBMISSION.md) · [CHANGELOG](CHANGELOG.md)

License: Apache-2.0 ([LICENSE](LICENSE)).
