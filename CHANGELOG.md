# Changelog

## v0.1.10 (2026-11-20): relay with multiple boxes and Code Mode
Claude plugin 0.1.10. Relay changes need a relay redeploy; the connector change needs a box update.

### relay: multiple boxes per connection
- `RELAY_MULTI_BOX=on` turns the relay into its own MCP server over every box in the connection. Each box tool gets a `box` argument; each call is exchanged for a short token for that one box. Off by default.
- New relay tools: `boxes_list`, `kanban_list`, `kanban_read`, `memory_read` (one box or `all`) and `calendar_post` (adds a post for review, never publishes). See docs/RELAY.md, Multiple boxes.

### relay: Code Mode
- `RELAY_CODE_MODE=on` adds `search`, `describe` and `execute`: one JavaScript snippet runs over every box in the connection and only its result comes back. Snippets run in a separate sandbox process and can only use read-only tools. Needs Node 25 or later: at start the relay checks that the sandbox is refused network, file, process and worker access, and offers the code tools only when it is. Off by default. See docs/RELAY.md, Code Mode.

### relay: "Available on" in tool descriptions
- A box that has not listed its tools yet (cold, slow or failing, with no earlier list) no longer makes every other box's tool say "Available on: <the boxes that answered>". The line is left out until every box's list is known; a box that answered before keeps serving its last list while it reloads.

### relay: token exchange
- Each POST to the token route waits up to `RELAY_EXCHANGE_TIMEOUT` seconds (25), at most `RELAY_EXCHANGE_SLOTS` (2) run at once per relay, and busy or failed answers are retried with a short backoff. Box tool lists are cached per connection; time limits come from the environment (docs/RELAY.md, Environment).
- With `RELAY_MULTI_BOX=on`, a single-box connection keeps working when `/oauth/targets` fails: it falls back to the plain exchange. Scoped connections fail closed.
- Exchanges are paced: one per box at a time; `503`/`429` retried up to 3 times with jitter and a capped `Retry-After`; `500`/`502`/`504` and an unreachable route once; never past the caller's deadline; a box whose busy retries ran out pauses 5 s. `RELAY_EXCHANGE_CONCURRENCY` is accepted as a second name for `RELAY_EXCHANGE_SLOTS` (SLOTS wins when both are set). `RELAY_EXCHANGE_TIMEOUT` is capped at 120 s. Fan-outs ask 4 boxes at once instead of 8.
- Sessions are stateless, so several relay processes can serve one client: an unknown `Mcp-Session-Id` is ignored, `GET /mcp` answers 405, `DELETE` answers 200, and the relay answers `server/discover` itself (no more 400s on that probe).
- `RELAY_ISSUER_HEADERS_FILE` (off by default): extra headers, such as an access gate's service token, sent only on the relay's own calls to the issuer. See docs/RELAY.md, Issuer behind an access gate.

### connector
- `github_search` passes each term of the query to `gh` as its own argument, so `org:acme widget` searches the org for `widget` instead of becoming the invalid `org:"acme widget"`. Double quotes still group a phrase (`"rate limit"`, `label:"good first issue"`). An empty query is refused.
- When `gh` on a Son is not logged in, the answer names the Son (`github.name` in the config, else the Son the connector serves) next to the connect link.
- `son_list` on a box without the `/v1/sons` route (older or hand-built boxes) no longer reports the Son offline: it checks the box's AI status route instead and says so in `via` and `note`.

## v0.1.9 (2026-11-08): coordinator briefs, team board, model controls
Claude plugin 0.1.9 (re-validate in the Claude directory) and ChatGPT package 0.1.9 (new ZIP from the `v0.1.9` tag).

### coordinator briefs
- Bug found briefs end with **Done when**: the steps no longer reproduce it and a test that would have caught it is in place.
- Open questions to experts carry six lines: Question, Who answers, Who decides, Answer by, If no answer, Blocking. Every skeleton includes them (`ask_expert.block` in `core.yaml`).
- Sales handoffs mark each detail **Confirmed**, **Unclear** or **Not discussed**, and the person taking over can send the handoff back when a gap blocks them (`mark_each`, `send_back` in `sales.yaml`). Bad values are reported and the default stays.
- The assistant now acts as the owner's coordinator: every request it sends is labelled **New feature request**, **Improvement** or **Bug found**, and carries four parts: Why, The goal, What needs to be done, What I expect back. The experts decide how. When it needs input it asks the right person one question, says what it will assume meanwhile, and carries on. At the end of a conversation every request and decision becomes a brief, a board task or a follow-up.
- Brief templates per team type (engineering, design, marketing, video, ops, research, sales, general), each following its field's standard (user stories and acceptance criteria, bug reports, creative briefs, video briefs, change requests, research briefs). They live in `connector/aiteam_connector/briefs/*.yaml`, not in code.
- New free, read-only tool `brief_templates`: the core rules and team list, one team's template, or a ready skeleton for a team and label.
- Owners override any template, or add a team, by dropping a YAML file in their briefs folder (`AITEAM_BRIEFS_DIR`, else `<HERMES_HOME>/briefs`). A file that does not parse is skipped and reported; the bundled one stays in use.
- Usage guide v15/v16: "Coordinator: every request is a brief" plus the short form in the server instructions; the hand-off example is a full brief. The `aiteam-mcp` skill is regenerated and the ChatGPT `get-started` skill gets the same rule.

### team board tools
- Four free tools for the team's shared task list: `team_map` (the teams this Son is in, their nodes and links; metadata only, no messages, memories or task contents), `board_list`, `board_create` and `board_update`. Listed only when the box sets `TEAM_BOARD_URL` and `TEAM_BOARD_TOKEN`; calls go to that URL with the bearer, and the token is never logged or returned.
- `board_update` is compare-and-set: send the version you read. When someone changed the task first, the result is a normal one carrying the latest version, so the caller re-applies its change.
- Strict input schemas match the site's rules (one-line title up to 140 characters, https links only, known statuses and assignee kinds). Usage guide v14 adds section 11 "Team board" and point (11) in the server instructions; the `aiteam-mcp` skill is regenerated.

### public repo rule
- Usage guide (v13) and the `aiteam-mcp` skill gain "Public repos: treat every action like publishing a post": check visibility first, never name the assistant or its setup, write as the owner, and get the owner's OK on a draft before pushing, commenting, tagging, opening, closing, merging or reviewing. Server instructions get the short form as point (10).

### one workflow per project
- Usage guide (v12) and the `aiteam-mcp` skill gain "Projects: own workflow, own scope": each project sets its own workflow (who builds, reviews, merges and approves, which branch) and keeps its own repos, threads and memory. Approvals and context never carry across projects; some projects allow no merge by a Son at all; an unknown workflow is asked once. Server instructions get the short form as point (9). No project names in the public plugin.

### guided first run + README
- "Help me set up aiteam": MCP prompt `setup_aiteam`, a trigger in the server instructions and usage guide section 10 (also in the `aiteam-mcp` skill and the ChatGPT `get-started` onboarding skill). Steps: find the Son, connect its brain (Claude, ChatGPT or an API key, several accounts, one-time aiteam.space link, fallback order), offer Telegram and Google Meet (Workspace SSO), first task.
- README rewritten: everything the connector controls (sign-in, accounts, model, fallback, approvals, crons, sessions, commands), the Hermes command map, and links to aiteam.space, sign-up, pricing, help and support.

### model/provider controls
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
