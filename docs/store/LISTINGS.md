# Store listings: Claude directory + ChatGPT plugin store

Release: tag `v0.1.6` (Discord chat free, thread management add-on from #17). Store images re-rendered for v0.1.6. Assets: `package/assets/*.png`, screenshots in
`docs/store/screenshots/`, all rendered by `scripts/render_store_assets.py`.

## What ships (one install)
- **ChatGPT**: `aiteam-chatgpt-plugin-<version>.zip` = `plugin.json` + `mcp.json` (server `aiteam`, OAuth DCR) +
  `skills/get-started` + `skills/aiteam-mcp` (bundled through `listing.json` `bundleSkills`) + brand PNGs.
- **Claude**: two listings from the same org, paired: the **MCP connector** (`https://mcp.aiteam.space/mcp`) and the
  **plugin bundle** `claude-plugin/` (`.claude-plugin/plugin.json`, `.mcp.json` -> same server, `skills/aiteam-mcp`).
  Claude reads a plugin bundle from a GitHub repo that must be **public before the listing goes live**.

## Billing (server-enforced, `connector/aiteam_connector/entitlements.py`)
- The connection is never refused. Voice mode (Claude, ChatGPT), Telegram + topics, WhatsApp, chat, GitHub read,
  sessions/status: free.
- Discord thread management — two-way sync ($5/mo): control and sync Discord threads both ways (`discord`, `discord_admin`, `discord_thread*` tools). Upsell result, tool does not run.
- Free: a Discord bot for normal chat with your Son, alongside free Telegram and WhatsApp chat.
- Live only with `FLEET_ENFORCE_PLUGS` on (plugs.json `enforce: true`). Card on file, one monthly bill.

## Listing text (both stores)
- **Name**: aiteam.space
- **One-liner / short description**: Your AI teammate, own computer
- **Voice presentation**: "Claude \u2014 voice/live mode" and "ChatGPT \u2014 voice mode", with
  `package/assets/voice-claude.png` and `voice-chatgpt.png` (brand mark + waveform).
- **Long description**: `package/listing.json` `interface.longDescription` (ChatGPT wording; for Claude, replace
  "ChatGPT" with "Claude").
- **Category**: Productivity
- **Example prompts**: the three `interface.defaultPrompt` values.
- **Website** https://aiteam.space · **Privacy** https://aiteam.space/privacy · **Terms** https://aiteam.space/terms
- **Support**: https://aiteam.space/support
- **Auth**: OAuth 2.1 at https://aiteam.space (DCR + client metadata documents, PKCE S256), scopes `son:read son:write`.

## Permission justifications
- `son:read`: list the user's Sons, read their conversations, sessions and status, read GitHub PRs/CI/issues
  through the Son's own login. Read-only tools carry `readOnlyHint: true`.
- `son:write`: send a task or message to the user's own Son, post to the user's own team chat, manage Discord
  threads when the user asks (paid add-on). Write tools carry `destructiveHint` where they change state.
- No payments, no money movement, no access to other users' Sons; the relay only reaches Sons on the signed-in account.

## Test instructions (both stores, paste as is)
Sign up at aiteam.space — free tier, no card. Use any email (or Continue with Google), enter the code we email you,
then Create a Son: pick Empty Bot + Starter, name it, Deploy. Checkout shows $0.00 (the welcome credit pays the
first bill; no card field). The Son is running in about 3 minutes. Then add the connector URL
https://mcp.aiteam.space/mcp in Claude or ChatGPT and sign in with the same email.
No pre-built reviewer Son is needed.

## Submission URLs
- ChatGPT: https://platform.openai.com > Plugins > Upload new or existing plugin (org must be verified).
- Claude: https://claude.ai/directory/manage > Submit new > MCP connector, then Submit new > Plugin bundle.
