# Tools: full Hermes parity

**Rule:** if the Son's Hermes agent can do it, the MCP connector can do it too. No exceptions, no allowlist.
Claude and ChatGPT get the same tools the agent uses in its own chats: shell, files, browser, web, cron,
kanban, skills, memory, sub-agents, Discord (full bot control), GitHub, sessions, and every plugin tool the
Son has enabled.

## How it is wired

```
Claude / ChatGPT
  -> https://<son>.aiteam.space/mcp        OAuth gate (aiteam.space sign-in + Approve)
  -> aiteam-connector (connector/)          connector tools + GitHub + Discord relay, passthrough "all"
  -> hermes mcp serve on the Son            messaging bridge + session tools + hermes_toolbox (son/)
  -> Hermes tool registry                   the same handlers, hooks and guardrails an agent turn uses
```

- `son/hermes_toolbox.py` registers on the Son's own `hermes mcp serve` (one `register(mcp)` call, next to the
  session tools). It lists `model_tools.get_tool_definitions(<every toolset>)`, which is exactly the set the
  agent gets on that box, and runs each call through `model_tools.handle_function_call`. Nothing is
  re-implemented, so a new Hermes tool or plugin shows up in Claude/ChatGPT on the next restart with no
  connector change.
- The four agent-loop tools (`memory`, `todo_list`, `session_search`, `delegate_task`) are bound in the toolbox
  because they need per-agent state. `delegate_task` runs a real Hermes sub-agent on the Son's own model and
  returns its result in the same call.
- The memory provider's tools (for example `mnemosyne_*`) are exposed when the Son uses that provider.
- `clarify` is the only tool left out: over MCP the human is already the caller.
- Annotations: read-only tools carry `readOnlyHint`, shell/file-write/cron/skills/admin tools carry
  `destructiveHint`, so Claude and ChatGPT ask before running them. The connector keeps the Son's annotations.

## Tool list (live Son, 146 tools)

Counts come from a live Son. A tool needs its key, binary or platform on the box; `hermes_toolbox_status` lists
what this Son exposes and what it cannot run yet, with the reason.

**Connector (5):** `usage_guide`, `son_send`, `son_list`, `son_conversations`, `son_conversation_history`

**GitHub, the Son's own `gh` login (27):** `github_api`, `github_auth_status`, `github_search`,
issues (`list/view/create/edit/comment/close/reopen`), PRs (`list/view/diff/checks/create/edit/comment/review/merge/close`),
repos (`repo_list/repo_view`), Actions (`run_list/run_view/run_rerun/workflow_list/workflow_run`)

**Discord:** `discord` (read messages, members, create threads), `discord_admin` (full bot control: guilds,
channels, roles, pins, moderation, per the Son's `discord.allowed_actions`), `messages_send` (webhook relay,
wakes tagged Sons), `discord_threads_list`, `discord_thread_get`, `discord_thread_edit`, `discord_thread_create`

**Telegram groups with topics (6, free):** `telegram_chat_get`, `telegram_topics_list`, `telegram_topic_messages`,
`telegram_topic_send`, `telegram_topic_create`, `telegram_topic_edit` (docs/TELEGRAM-TOPICS.md; listed when the
connector has the Son's Telegram bot token)

**Messaging bridge, every connected platform (10):** `conversations_list`, `conversation_get`, `messages_read`,
`messages_send`, `attachments_fetch`, `channels_list`, `events_poll`, `events_wait`, `permissions_list_open`,
`permissions_respond`

**Sessions (6):** `sessions_list`, `session_status`, `sessions_history`, `sessions_search`, `sessions_send`,
`session_search`

**Shell and files (6):** `terminal`, `process_manage`, `read_file`, `write_file`, `patch`, `search_files`

**Code (1):** `execute_code`

**Browser (6):** `browser_exec`, `browser_vault_list`, `browser_vault_fill`, `browser_vault_save_login`,
`browser_vault_unlock`, `browser_vault_enter_code` (plus `browser_navigate/click/type/snapshot/...` on Sons
with the browser binary installed)

**Web and media (7):** `web_search`, `web_extract`, `vision_analyze`, `video_analyze`, `video_generate`,
`text_to_speech` (plus `image_generate`, `x_search` where keys exist)

**Scheduling and work tracking (16):** `cronjob_manage`, `todo_list`, `kanban_list`, `kanban_show`,
`kanban_create`, `kanban_comment`, `kanban_complete`, `kanban_block`, `kanban_unblock`, `kanban_link`,
`kanban_heartbeat`, `kanban_attach`, `kanban_attach_url`, `kanban_attachments`, `kanban_request_review`,
`kanban_request_changes`

**Skills (3):** `skills_list`, `skill_view`, `skill_manage`

**Memory (1 + provider):** `memory` (MEMORY.md / USER.md) and, with Mnemosyne, 40 `mnemosyne_*` tools
(recall, remember, canonical facts, triples, graph, persona, shared surface, sync, task progress)

**Sub-agents (1):** `delegate_task`

**Desktop UI (11):** `desktop_project`, `desktop_preview`, `drive_preview`, `annotate_preview`, `apply_layout`,
`focus_pane`, `gui_tour`, `show_tip`, `read_terminal`, `read_window_below`, `close_terminal`

**Status (1):** `hermes_toolbox_status`

**Plugin and platform tools:** listed automatically when the Son has them enabled: Home Assistant (`ha_*`),
Spotify (`spotify_*`), A2A (`a2a_*`), computer use, Feishu, connectors (`manage_connections`), add-on tools.

## Install on a Son

1. Copy `son/hermes_toolbox.py` next to the patched `hermes mcp serve` launcher (same folder as
   `mcp_session_tools.py`).
2. In the launcher's `create_mcp_server`, after the session tools:
   ```python
   try:
       import hermes_toolbox
       hermes_toolbox.register(mcp)
   except Exception:
       logger.warning("MCP Hermes toolbox unavailable", exc_info=True)
   ```
3. Restart only the MCP process (never the chat gateway). The connector picks the tools up on its next
   restart (it caches the upstream tool list).
4. Check: `connector/scripts/parity_hermes_live.py https://<son>.aiteam.space/mcp <token file>` calls 12 tools
   through the public URL and prints PASS/FAIL for each.

Config (`hermes config set`): `mcp.toolbox.enabled` (default true), `mcp.toolbox.exclude` (tool names to hide;
default empty = full parity).

## Safety

- Paid add-ons: every tool is listed; a tool from a paid add-on the Son does not own answers with an upsell
  (normal result, `paywall: true`) instead of running. Map and rules: docs/ENTITLEMENTS.md.

- Same code path as the agent: every call runs through Hermes hooks, tool-request middleware and the
  guardrails. Hard blocks and plugin approval rules still apply. There is no chat over MCP to answer an
  approval prompt, so ordinary flagged commands run like a trusted headless session; the client's own confirm
  dialog (driven by `destructiveHint`) is the human step. Hide a tool with `mcp.toolbox.exclude` if a Son
  should not offer it.
- One terminal/browser sandbox (`task_id mcp-toolbox`) is shared by all connector calls, so `cd`, env and
  browser state carry across calls like in one agent session.
- The OAuth token stops at the gate. Only the Son's owner can connect.
