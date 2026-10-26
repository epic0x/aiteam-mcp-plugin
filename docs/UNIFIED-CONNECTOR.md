# AI Teamspace Plugin MCP Connector: design note

Status: **v0.1 built** in `connector/` (this PR). Phase 1 = per-box connector behind the existing aiteam.space
OAuth gate. Phase 2 = the same code behind the single relay `mcp.aiteam.space` ([AUTH-DESIGN §3](AUTH-DESIGN.md)).
Labels: [fact] = checked in code or on a live box, [built] = in this PR, [proposal] = not built.

## 1. The problem it solves: "one Son, no coordinator"
A customer may run **one Son and nothing else**: no coordinator above it, nobody to cross-tag it.
- [fact] Chat platforms (Telegram, Discord) ignore a bot's own messages, and Hermes only answers a bot in a
  group when tagged. On a team, the coordinator does the tagging. A lone Son has no coordinator, so nothing
  outside its owner's chat can drive it.
- Goal: **whoever holds the connector drives that single Son directly**: send a task, get the reply streamed,
  list and read its sessions. No chat platform, no coordinator, no Hermes CLI in anyone's container.

## 2. Three pieces folded into one connector
```
Claude / ChatGPT / any MCP client
   │  MCP (streamable HTTP), OAuth token from aiteam.space (sign-in + Approve)
   ▼
OAuth gate (:8701, existing)  ──▶  aiteam-connector (:8720)  ← ONE MCP server, one tool list
                                     │                     │
            (3) messaging bridge     │                     │  (2) session tools passthrough
                 REST + SSE          ▼                     ▼
                         messaging box (:8710)      Son's `hermes mcp serve` (:8700)
                         own identity, holds         sessions_list / status / history /
                         the Son's API key           search / send (OpenClaw session-tools shapes)
                                     │
                                     ▼  POST /api/sessions/{id}/chat/stream   author = box:<box>/<caller>
                         (1) Son's Hermes api_server (:8642) = the agent runtime
```
1. **Hermes is the runtime** [fact]. The connector never runs a model. Every `son_send` is a real Hermes turn on
   the Son, with its memory, skills and tools.
2. **OpenClaw-style session tools** [built]. The connector lists the Son's own `hermes mcp serve` tools and
   forwards `sessions_list / session_status / sessions_history / sessions_search / sessions_send` unchanged, so the
   shapes stay the OpenClaw canonical ones. It adds titles and
   `readOnlyHint`/`openWorldHint` annotations (store gap G3). `passthrough: "all"` also forwards the other 10
   upstream tools. The default is `all`: the full surface for owner connectors AND the public Claude/ChatGPT
   listing (owner 2026-10-12). Write tools are labelled (`readOnlyHint: false`) and described in the listing.
   Plus 27 GitHub tools (14 write), on by default, that run the Son's own `gh` CLI login: same permissions and the
   same output as `gh` on that Son (connector/aiteam_connector/github_gh.py). No GitHub App and no separate scope
   (owner 2026-10-12, supersedes the read-only `github:read` design). Plus `usage_guide` (the how-to-drive-your-Son guide, also sent as the MCP server instructions, so no client
   needs a hand-installed skill). Total public surface: 50 tools (`ping`, `health`, `cleanup` added: docs/HEALTH.md).
3. **Paperclip-style HTTP messaging bridge** [built]. The model is `felipefontoura/paperclip-adapter-hermes-gateway`:
   an outside caller talks to Hermes over HTTP with its own bearer, with no CLI and no bind mounts. We use the
   session API, not `/v1/chat/completions`, so every turn lands in a named Hermes session with history, FTS
   search and an author label. `NousResearch/hermes-paperclip-adapter` is the opposite model (it spawns the
   CLI per heartbeat), which is why we don't use it: it needs Hermes inside the caller's container.

## 3. The messaging box (sidecar)
`aiteam-box`, about 250 lines, aiohttp. It runs on the Son's network next to the api_server.
- **Own identity.** It has a `box_id`. Every turn it relays carries `author = {id: "box:<box_id>/<caller>", is_bot: true}`,
  so the Son's memory records who really asked and never confuses it with the owner or itself.
- **Holds the Son's `API_SERVER_KEY`.** Callers never see it. Each caller gets its own token (`aiteam-box new-caller`;
  only the SHA-256 is stored) and an allowlist of Sons. Revoking a caller = deleting one config line.
- **One session per (caller, conversation).** The id is `box_<sha256(box|caller|conversation)[:24]>`, created
  on first use. Same conversation name = same thread, so the Son remembers. A caller can never write into the
  owner's Telegram or Discord sessions. Hermes `sessions_send` refuses those too.
- **Relays out.** `stream: true` forwards the Son's SSE frames as they arrive (`assistant.delta`, `tool.started`,
  `assistant.completed`...). The connector turns `tool.started` into MCP progress notifications.
- **HTTP API.** `GET /v1/whoami`, `GET /v1/sons`, `POST /v1/sons/{son}/messages {text, conversation, stream}`,
  `GET /v1/sons/{son}/conversations`, `GET /v1/sons/{son}/conversations/{conv}/messages`, `GET /healthz`.

Why a sidecar and not a plugin inside the gateway: it keeps a separate identity and key boundary, it restarts
without touching the chat gateway, and the same binary serves a coordinator that fans out to many Sons
(one box config with several `sons`).

## 4. The two deployment cases (the test matrix)
| Case | Box | Who drives | Box config |
|---|---|---|---|
| **Standalone single Son** | any host (one Son) | connector holder → box → that Son | `sons: [my-son]`, caller = the connector |
| **Coordinator** | coordinator host | connector holder → the coordinator's box → Sons (and the coordinator itself) | `sons: [coordinator, son-a, ...]`; remote Sons reached over their own box URL |

The tool surface is the same in both. Only the box config differs (one Son vs many).

## 5. Tools (the store-reviewed surface)
| Tool | Hint | What |
|---|---|---|
| `son_send` | write, open world | message a Son, return its reply; `conversation` keeps a thread |
| `son_list` | read | Sons this connector can reach + online |
| `son_conversations` | read | your conversations with a Son |
| `son_conversation_history` | read | messages of one of them |
| `sessions_list`, `session_status`, `sessions_history`, `sessions_search` | read | upstream Hermes (OpenClaw shapes) |
| `sessions_send` | write | upstream Hermes; API/CLI/cron sessions only |
| `discord_threads_list`, `discord_thread_get` | read | Discord threads of a channel / one thread (relay only) |
| `discord_thread_edit`, `discord_thread_create` | write | rename, archive/unarchive, lock, open a thread (relay only); see [DISCORD-THREADS.md](DISCORD-THREADS.md) |
Multi-Son connectors add a `son` enum argument to every tool.

## 6. Security
- The client's OAuth token stops at the gate (no token passthrough, MCP spec). The gate → connector → box hops
  use box-local tokens. The Son key lives only in the box env.
- Default bind is `127.0.0.1`. Only the gate is exposed, through the existing Cloudflare tunnel.
- Caller allowlist per Son. Conversation names are restricted (`[A-Za-z0-9._-]{1,64}`). Text is capped at 32 KB.
- [proposal] Scope mapping: `son:read` tokens get the read tools only (gate filters `tools/call`). Needs the
  gate to pass `X-Aiteam-Scope` to the connector. It already sets the header.

## 7. Rollout
1. [built] `connector/` package (`aiteam-connector`, `aiteam-box`), unit tests against a fake api_server with the
   real 0.21.5 SSE frames, `scripts/e2e_live.py`.
2. Standalone single-Son test: results in the PR body.
3. Coordinator test: the coordinator runs `scripts/e2e_live.py` against his box. Results go in the PR body.
4. [proposal] Point `my-son.aiteam.space/mcp` at the connector (gate upstream :8700 → :8720), keeping the old chain
   one env flip away. Then the same for the hosted Son image, then the Phase 2 relay.

## 8. Open questions
- Q1 Store listing: `passthrough: session` (9 tools, store-safe) or `all`? Owner connectors default to `all` (19 tools).
- Q2 Should a coordinator box also expose the *other* Sons' session tools (needs their upstream URLs), or only `son_send`?
- Q3 Public listing name: "AI Teamspace" (this note) vs "aiteam.space".
