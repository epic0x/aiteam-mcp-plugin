# Changelog

## Unreleased: team board tools
- Four free tools for the team's shared task list: `team_map` (the teams this Son is in, their nodes and links; metadata only, no messages, memories or task contents), `board_list`, `board_create` and `board_update`. Listed only when the box sets `TEAM_BOARD_URL` and `TEAM_BOARD_TOKEN`; calls go to that URL with the bearer, and the token is never logged or returned.
- `board_update` is compare-and-set: send the version you read. When someone changed the task first, the result is a normal one carrying the latest version, so the caller re-applies its change.
- Strict input schemas match the site's rules (one-line title up to 140 characters, https links only, known statuses and assignee kinds). Usage guide v13 adds section 11 "Team board" and point (10) in the server instructions; the `aiteam-mcp` skill is regenerated.

## Unreleased: one workflow per project
- Usage guide (v12) and the `aiteam-mcp` skill gain "Projects: own workflow, own scope": each project sets its own workflow (who builds, reviews, merges and approves, which branch) and keeps its own repos, threads and memory. Approvals and context never carry across projects; some projects allow no merge by a Son at all; an unknown workflow is asked once. Server instructions get the short form as point (9). No project names in the public plugin.

## Unreleased: guided first run + README
- "Help me set up aiteam": MCP prompt `setup_aiteam`, a trigger in the server instructions and usage guide section 10 (also in the `aiteam-mcp` skill and the ChatGPT `get-started` onboarding skill). Steps: find the Son, connect its brain (Claude, ChatGPT or an API key, several accounts, one-time aiteam.space link, fallback order), offer Telegram and Google Meet (Workspace SSO), first task.
- README rewritten: everything the connector controls (sign-in, accounts, model, fallback, approvals, crons, sessions, commands), the Hermes command map, and links to aiteam.space, sign-up, pricing, help and support.

## Unreleased: model/provider controls
- Four MCP tools read the current model/provider and fallback order, switch all existing gateway sessions or one session, and replace the fallback order. Write tools are annotated as writes and require the JWT gate's verified owner identity.
- A gateway-owned local socket applies persisted session overrides and atomic configuration changes without interrupting running turns. The Son image must install and activate the hook; no release tag or fleet rollout is implied.
- See [MODEL-CONTROL.md](docs/MODEL-CONTROL.md) for authentication, scope limits and deployment requirements.

## v0.1.6 (2026-10-23): store submission build
- Discord paid add-on renamed **Discord thread management — two-way sync**, $5/mo (was $10). Plain Discord chat is free, like Telegram and WhatsApp.
- Store test instructions: reviewers sign up free at aiteam.space, no card, no pre-built reviewer Son.
- Full Discord control: read any channel or thread, raw Discord API, behind the same add-on gate.
- Bundled usage skill regenerated from `usage_guide.py`.

## v0.1.5 (2026-10-16)
- First submission build: bundled skills + MCP, brand and voice-mode assets, Claude plugin bundle.
