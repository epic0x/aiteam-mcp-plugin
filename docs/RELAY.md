# Unified relay: `https://mcp.aiteam.space/mcp`

One MCP URL for every Son. A user adds `https://mcp.aiteam.space/mcp` in Claude or ChatGPT, signs in with
aiteam.space, picks a Son on the consent page, and the relay forwards every MCP request to that Son.
Code: [relay/](../relay/) (package `aiteam-relay`, CLI `aiteam-relay`). Spec base: [AUTH-DESIGN.md §3](AUTH-DESIGN.md).

## Flow

```
Claude / ChatGPT
   |  HTTPS, Authorization: Bearer <relay token, aud=https://mcp.aiteam.space/mcp, son_id=<picked>>
   v
Cloudflare (mcp.aiteam.space) --tunnel--> relay 127.0.0.1:8790
   |  1. verify the JWT locally (aiteam.space JWKS, ES256): iss, aud = relay, exp, son:* scope
   |  2. RFC 8693 token exchange: POST https://aiteam.space/oauth/token
   |       -> short-lived Son token (aud=https://<slug>.aiteam.space/mcp, <= 5 min) + son_mcp_url
   |     (cached by sha256(relay token) until exp - 30 s)
   v
https://<slug>.aiteam.space/mcp   (the Son's existing public gate, unchanged)
```

- The client token never reaches the Son. The Son only sees the exchanged token, which its gate already accepts.
- Revoking the grant on aiteam.space takes effect on the relay within 5 minutes (the exchanged token lifetime).
- Sign-in discovery: the relay serves the protected-resource metadata; the authorization server is aiteam.space.

## Deviations from AUTH-DESIGN §3 (on purpose)
- **Hosting:** a Python ASGI service behind a Cloudflare Tunnel on the fleet host, not a Cloudflare Worker +
  Durable Object.
- **Relay to Son hop:** RFC 8693 token exchange at aiteam.space, not a relay-held signing key. The relay holds no
  secret at all. Son gates stay untouched.
- **Tool surface:** the relay forwards to the Son's public `/mcp`, so the tools are the Son connector's own tools, not
  a separate reduced set.

## Routes
| Route | Answer |
|---|---|
| `GET /.well-known/oauth-protected-resource` and `/.well-known/oauth-protected-resource/mcp` | PRM JSON: resource `https://mcp.aiteam.space/mcp`, authorization server `https://aiteam.space`, scopes `son:read son:write` |
| `GET /.well-known/openai-apps-challenge` | the OpenAI domain challenge token, `text/plain`, no newline; 404 when unset |
| `GET /healthz` | `ok` |
| `GET/POST/DELETE /mcp` | 401 + `WWW-Authenticate: Bearer resource_metadata=".../oauth-protected-resource/mcp"` without a valid token (`error="invalid_token"` when one was sent); else exchange and stream-proxy (with `RELAY_MULTI_BOX=on`: see Sessions and streams) |

Header rules on `/mcp`: only `content-type`, `accept`, `mcp-session-id`, `mcp-protocol-version` and `last-event-id`
go upstream, plus `Authorization: Bearer <exchanged token>`. Inbound `Authorization`, `Cookie`, `X-Aiteam-*` and
everything else are dropped. Status, `content-type` and `mcp-session-id` come back as-is. SSE is streamed chunk by
chunk. If the Son answers 401, the relay drops the cached token and exchanges once more. If aiteam.space answers
`invalid_grant` (revoked grant, Son gone, expired token), the client gets 401 `invalid_token` and signs in again.

SSRF guard: the relay only forwards to `https://<one label>.aiteam.space/mcp` (no port, no user info, no query).
`mcp.aiteam.space` itself is refused, so the relay can never call itself. Anything else is 502 and no request is sent.

Logs: no tokens, no full upstream URLs (only the Son host name). The `httpx` logger is set to WARNING.

## Environment
| Variable | Default | Meaning |
|---|---|---|
| `RELAY_PUBLIC_URL` | `https://mcp.aiteam.space` | public origin; the resource is `<this>/mcp` |
| `AITEAM_ISSUER` | `https://aiteam.space` | token issuer, JWKS at `/oauth/jwks`, exchange at `/oauth/token` |
| `RELAY_HOST` | `127.0.0.1` | bind address |
| `RELAY_PORT` | `8790` | bind port |
| `RELAY_SON_HOST_SUFFIX` | `.aiteam.space` | allowed Son host suffix (SSRF guard) |
| `OPENAI_APPS_CHALLENGE` | unset | OpenAI domain verification token |
| `OPENAI_APPS_CHALLENGE_FILE` | unset | or read the token from this file (used when the variable is unset) |
| `RELAY_MULTI_BOX` | `off` | `on` = the relay is its own MCP server over every box in the connection (see Multiple boxes) |
| `RELAY_BOX_TIMEOUT` | `30` | multi-box: seconds to load one box's tool list (token exchange, MCP initialize, tools/list) |
| `RELAY_TARGETS_TIMEOUT` | `15` | multi-box: seconds for one `GET /oauth/targets` |
| `RELAY_FANOUT_TIMEOUT` | `10` | multi-box: seconds per box in `boxes_list` and in `box="all"` reads |
| `RELAY_LIST_TIMEOUT` | `25` | multi-box: the longest one `tools/list` answer waits for boxes whose list is not cached yet |
| `RELAY_TOOLS_TTL` | `300` | multi-box: seconds a box's cached tool list counts as fresh |
| `RELAY_EXCHANGE_SLOTS` | `2` | token exchanges in flight at once per process, all connections and boxes together (1 to 64). Wins over `RELAY_EXCHANGE_CONCURRENCY` when both are set |
| `RELAY_EXCHANGE_CONCURRENCY` | unset | the same setting under its newer name; read only when `RELAY_EXCHANGE_SLOTS` is unset |
| `RELAY_EXCHANGE_TIMEOUT` | `25` | seconds one POST to the issuer's token route may take (at most 120); never past the caller's own limit |
| `RELAY_CODE_MODE` | `off` | `on` = add `search`, `describe` and `execute` (needs `RELAY_MULTI_BOX=on`; see Code Mode) |
| `RELAY_NODE` | `node` on `PATH` | Code Mode: the Node 25+ binary that runs the sandbox |
| `RELAY_SANDBOX_DIR` | `relay/sandbox` | Code Mode: the sandbox folder (after `npm ci`); the only folder the sandbox may read |
| `RELAY_ISSUER_HEADERS_FILE` | unset (off) | file of extra headers for the relay's own calls to the issuer only, for an issuer behind an access gate (see Issuer behind an access gate) |

Time values are seconds (decimals allowed), above 0 and at most 3600. Anything else keeps the default and logs a
warning. The same goes for an exchange slot count outside 1 to 64 and `RELAY_EXCHANGE_TIMEOUT` above 120.

### Issuer behind an access gate
For an issuer that sits behind an access gate wanting a service token on every request (a staging site, for
example), the relay can add the gate's headers to its own calls to the issuer. Off unless
`RELAY_ISSUER_HEADERS_FILE` is set; production runs without it.

1. Put the service token in a file only the relay's user can read:

   ```
   CF-Access-Client-Id: <client id>
   CF-Access-Client-Secret: <client secret>
   ```

   `chmod 600` it. Blank lines and lines starting with `#` are skipped.
2. Set `RELAY_ISSUER_HEADERS_FILE` to its path and restart the relay. The log says
   `issuer headers on for <issuer> (CF-Access-Client-Id, CF-Access-Client-Secret)`, with names only.

Where the headers go:
- Only the issuer's own origin (`AITEAM_ISSUER`: same scheme, host and port), only `/oauth/token`, `/oauth/targets`
  and `/oauth/jwks`.
- Never to a box, never to any other path or host, never on a redirect (the relay follows none). A header with one of
  those names on any other request is removed, even one a client sent.

The relay refuses to start, and says why without the value, when the file is missing, is a symlink or not a plain file, can be
read by group or others, names any other header, or has an empty value. Use a service token made for the relay
alone, so it can be revoked on its own.

## Multiple boxes

With `RELAY_MULTI_BOX=on` one connection reaches every box the owner picked on aiteam.space: all boxes, all boxes in
one project, or a hand-picked list. The relay stops forwarding raw MCP traffic and becomes an MCP server itself
(Streamable HTTP, stateless). It talks to each box as an MCP client.

```
client --relay token--> relay --GET /oauth/targets (relay token)--> aiteam.space   boxes in the connection, 30 s cache
                              --POST /oauth/token target=<box id>--> aiteam.space   box token, max 5 min, per box
                              --MCP, box token--> https://<box>.aiteam.space/mcp
```

1. The relay verifies the client token as before.
2. It asks aiteam.space which boxes the connection reaches. aiteam.space resolves this live, so a box the owner
   removes, or a project they leave, drops out within 30 seconds.
3. For each tool call it exchanges the token for that one box (`target=<son or node id>`). aiteam.space checks
   ownership again on every exchange. A box answering 401 loses only its own cached token, and the call is tried once
   more.

### Tools
| Tool | What it does |
|---|---|
| every box tool | Listed once by name with an extra `box` argument (the box names in this connection). `box` is required when the connection has more than one box. A tool that only some boxes have says `Available on: a, b`. The result comes back exactly as the box sent it. |
| `boxes_list` | The boxes in the connection, each with a live health check (the box's `box_health`, `RELAY_FANOUT_TIMEOUT` per box). |
| `kanban_list`, `kanban_read`, `memory_read` | Read-only. `box` = one box, or `all` to ask every box at once. The answer is grouped per box with `ok` or `error`. |
| `calendar_post` | Adds a post to a content calendar box in the connection through its `board_add_item` tool. With `date` it waits for review on that day; without one it lands in Unscheduled. It never approves or publishes. |

Annotations: when boxes disagree on a tool's hints, the relay shows the least safe one (read-only only if every box
says so, destructive if any box says so).

Box tools pass through by their own names (for example `messages_send`, `messages_read`, `discord_read`,
`sessions_list`, `permissions_list_open`, `permissions_respond`, `cronjob_manage`), each with the `box` argument.

### Tool list cache
Listing a box's tools costs a token exchange, an MCP handshake and `tools/list`, often a few seconds per box. So the
relay keeps each box's tool list in memory, per process:

- Key: the box address and id, plus a hash of the account and granted scope. Only tool definitions are stored: no
  tokens and no tool results.
- Fresh for `RELAY_TOOLS_TTL` (5 minutes). After that the cached list is still served at once and refreshed in the
  background.
- Warm start: as soon as a request's boxes are known, the relay starts loading every box that is not cached, so the
  `tools/list` after `initialize` usually finds them ready. Those loads take their box tokens through the same
  exchange limit as everything else (see Token exchange), so a warm start never sends a burst to aiteam.space.
- Cold box: `tools/list` waits for it, up to `RELAY_LIST_TIMEOUT` in total for all such boxes, and then answers with
  what is ready. The log names the boxes left out. Their loads keep running (up to `RELAY_BOX_TIMEOUT` each) and show
  up in the next `tools/list`.
- A box whose refresh fails keeps its last good list (served as stale) and is tried again after 30 s.
- A `tools/call` with `box` for a tool that box's cached list does not show reloads that one list once (at most every
  10 s per box), then routes the call. The box answers for its own tools.
- At most 2,000 lists per process; the least recently used goes first.

### Limits
- 50 boxes per connection. Anything after that is dropped and `boxes_list` reports `truncated`.
- 4 boxes asked at once per fan-out, `RELAY_FANOUT_TIMEOUT` (10 s) per box for `boxes_list` and `all` reads, so one
  dead box never holds a fan-out answer for long. A box that is down shows `ok: false`; the other boxes still answer.
- Loading a tool list: `RELAY_BOX_TIMEOUT` (30 s) per box, 32 loads at once per process, their token exchanges at
  most `RELAY_EXCHANGE_SLOTS` (2) at once (see Token exchange). A box never listed before
  is left out of `tools/list` until its first list arrives; a box listed before stays listed (see Tool list cache).
- An `all` answer is capped at 256 KB. Boxes past the cap show `truncated` and can be asked on their own.
- Tool lists are fresh for 5 minutes per box (then served while they refresh), the box list is cached 30 s
  (`GET /oauth/targets`, `RELAY_TARGETS_TIMEOUT` 15 s), box tokens until 30 s before they expire.
- Inline calendar media (`media_b64`) up to 2 MB. Use `media_url` for anything larger.

### Token exchange
Every box needs its own exchanged token, and a cold `tools/list` over many boxes needs all of them at once. The
token route on aiteam.space answers `503 temporarily_unavailable` when it gets too many at once, so the relay paces
itself (per process):

- One exchange per box at a time. Requests that need the same box token while an exchange for it is running wait
  for that one (prefetch, warm start, `tools/list` and calls of one connection, and of other connections with the
  same token).
- At most `RELAY_EXCHANGE_SLOTS` (2) exchanges in flight at once, all boxes and connections together. The
  others queue.
- `503` and `429` are retried up to 3 times, waiting 0.5 s, 1 s, 2 s (each 50 to 100 % of that, at random, so
  boxes do not retry in step), or the `Retry-After` the route sent, capped at 10 s. While one answer says "busy",
  every new exchange of the process waits the same pause first.
- `500`, `502`, `504` and an unreachable route are retried once, after the same short wait. A server error is often
  one bad instance behind a load balancer; a second one is reported.
- Every other answer is final, never retried: `invalid_grant` (a 401 to the client), `invalid_target` (that box is
  not in the connection), any other 4xx.
- No retry, and no wait in the queue, past the time the caller has left (the box or fan-out limit of that request).
- A box whose retries ran out on `503` or `429` is not sent to the route again for 5 s; requests in that time get
  "aiteam.space is busy; try again in a few seconds" for that box. A box whose tool list failed to load is tried
  again after 30 s (Tool list cache), so a busy route is not asked again by the next `tools/list` either.
- `GET /oauth/targets` is fetched once per token at a time too: concurrent requests of one connection share it.

Logs name the box and the status (`token exchange for box-a answered 503 temporarily_unavailable; retry 1 of 3 in
0.4 s`), never a token or a token hash. Box tokens stay cached until 30 s before they expire, as before.

### Sessions and streams
The multi-box relay keeps no MCP session: every request is authenticated and scoped on its own, and nothing a later
request depends on is stored in the process. One connection's requests can land on different relay processes (several
instances, or a fail-over to a second host) and work the same.

- `POST /mcp`: every JSON-RPC request is answered on its own. `initialize` is answered without an `Mcp-Session-Id`,
  and an `Mcp-Session-Id` the client sends anyway (one another process gave, or one from an older answer) is ignored,
  never refused. Each answer is a short SSE stream that ends with the result; progress notifications for that request
  come first on the same stream. Nothing is held open after the result.
- `GET /mcp`: `405 Method Not Allowed`, `Allow: POST, DELETE`. The relay offers no standalone stream (the spec lets a
  server decline it; clients then use POST only), so no connection is held open between requests.
- `DELETE /mcp`: `200`, with or without `Mcp-Session-Id`: there is no session to end.
- `server/discover` (the probe a client that knows the stateless protocol revision sends first): answered `200` with
  the revisions the relay speaks (all `initialize` based). The client reads that as "use `initialize`" and does,
  with no error status. A request at a revision the relay does not speak is still a `400`, as the spec requires.
- GET, DELETE and `server/discover` never fetch the box list and never exchange a token.

Before this, a client that probes with `server/discover` first got a `400` on that probe at the start of every
connection (the SDK refuses the unknown revision), then fell back to `initialize`. The SDK logs
`Terminating session: None` at the end of every stateless request, `200`s included, so that line alone is not an
error. And `GET /mcp` opened a stream that stayed open with nothing on it.

SSE versus JSON answers: POST answers stay SSE (`json_response` off) because a long box call sends progress
notifications before its result, and those need a stream. The stream still ends with the result, so it costs no
more connection time than a JSON answer.

### Box addresses
Every box address from aiteam.space passes the same check as today: `https://<one label>.aiteam.space/mcp`, no port,
no user info, never `mcp.aiteam.space`. External nodes must be published under that suffix too. Any other address is
never called and shows in `boxes_list` as `address not allowed`. The relay also refuses an exchange whose box address
differs from the one in the box list.

### Not merged
Box prompts and resources are not merged into the relay; only box tools are. The relay offers its own introduction
prompt and resource when the connector package is installed next to it.

### Single-box connections
Connections made before this change keep working unchanged: one box, `box` optional, and the exchange is sent without
`target` as before. If `GET /oauth/targets` fails for such a connection, it still works through that plain exchange.

An owner can widen a single-box connection on aiteam.space (Settings, Connected apps). Tokens issued before that keep
their single-box claims for up to an hour, but aiteam.space now needs `target` on every exchange for that connection.
The relay follows the `grant_v` that `GET /oauth/targets` reports, not the token's claims, so those tokens get
`target=<box id>` and reach every box of the widened connection. With `RELAY_MULTI_BOX=off` (the default) the relay is the plain stream proxy described above.

## Code Mode
`RELAY_CODE_MODE=on` (needs `RELAY_MULTI_BOX=on`, off by default) adds three tools, so a client can do many steps in
one call instead of loading every box tool:

- `search(query)`: API methods and box tools that match the words, one line each.
- `describe(names)`: the full input of the methods (`kanban.list`) or box tools (`box-a/only_a`) named.
- `execute(code)`: runs one JavaScript snippet, the body of an async function, and returns only what it returns.

API inside a snippet (one object argument each): `boxes.list`, `boxes.health`, `tools.list`, `tools.call`,
`kanban.list`, `kanban.read`, `memory.read`, plus `console.log`.

### How a snippet runs
- In `relay/sandbox/runner.mjs`: a QuickJS interpreter (WebAssembly) inside a Node process started with
  `--permission`, reading only its own folder, with no string `eval` in the host and an empty environment. No tokens
  or settings ever enter it.
- Code Mode turns on only after a start-up check on the installed Node: the sandbox process, started with the same
  flags, tries to open a socket to a local port, fetch it, read a file outside its folder, write, start a process and
  start a worker. Node must refuse every one and nothing may reach the port. Node's permission model covers network
  access from Node 25 on, so older Node is refused outright. Until the check passes the code tools are not offered,
  `execute` answers "Code Mode is not set up on this relay yet" with the reason, and the log says the same. While
  off, the check runs again every 5 minutes.
- Every API call goes back to the relay and runs like a plain tool call: the connection's boxes are read live, the
  box must be one of them, and the call uses that box's own exchanged token.
- `tools.call` runs read-only tools only. Anything that changes something answers "needs approval" and has to be
  called as a plain tool, where the box's own approval rules apply. A loop can't get past this.

### Limits per run
50 API calls, 10 s of CPU, 50 s in total (waiting on boxes included), 32 MB of memory, 256 KB result, 200 log lines,
20,000 characters of code, 4 runs at once per relay. The 50 s keeps a run inside the 60 s that clients such as Claude
Code give an HTTP MCP request to its first answer byte. Error messages are cut at 10,000 characters.

### What clients read
- Every relay tool is annotated: `search`, `describe`, `execute`, `boxes_list` and the `box="all"` reads are
  read-only, non-destructive and idempotent; box tools keep the hints their box gives them.
- They declare an `outputSchema` and answer with `structuredContent` of that shape, plus the same JSON as text
  after a one-line summary, so a script reads fields and an older client reads text. A one-box read gets an object
  too: the box's own JSON, a list as `{items: [...]}`, plain text as `{text: ...}`.
- `_meta["anthropic/maxResultSizeChars"]` on the big answers (300,000 characters for `execute`, `boxes_list` and
  the fan-outs; 200,000 for `describe`; 100,000 for `search`), so Claude Code keeps them inline instead of saving
  them to a file past its 50,000-character default. `search` also has `anthropic/alwaysLoad`, so a client that
  defers tools behind tool search still sees it.
- Tool descriptions are cut at 4,096 characters (an `Available on:` note always stays) and the server
  instructions stay under 4,096, the size Claude Code reads.

### Native Code Mode (Claude Code)
Claude Code v2.1.323 and later has its own Code Mode (`CLAUDE_CODE_EXPERIMENTAL_CODE_MODE=1`): Claude writes a script
that calls MCP tools by name, on the user's machine, through the user's permission rules. It works with this relay
as is: the script can call `boxes_list`, the `box="all"` reads, box tools, and `execute` itself. Both have a place:

- Claude Code's script runs on the client: each tool call is one round trip to the relay, and writes go through
  the user's own prompts and rules.
- `execute` runs on the relay: one round trip for many box calls, read-only tools only. Clients without Code Mode
  (Claude.ai, Claude Desktop, ChatGPT) get the same one-step reads this way.

The server instructions say this when Code Mode is on here.

### Setup
1. Install Node 25 or later on the relay host.
2. `cd relay/sandbox && npm ci`.
3. Set `RELAY_CODE_MODE=on` (with `RELAY_MULTI_BOX=on`) and restart the relay. The log shows "Code Mode is on" or why
   it is off.

`RELAY_NODE` points at a `node` other than the one on `PATH`; `RELAY_SANDBOX_DIR` at another copy of the folder.

## Run
```
uv venv -p 3.12 .venv && . .venv/bin/activate
uv pip install -e ./relay
aiteam-relay            # listens on 127.0.0.1:8790
```
Tests: `uv pip install -e "./relay[test]" && cd relay && python -m pytest -q`.

### systemd unit (example)
```
[Unit]
Description=aiteam.space unified MCP relay
After=network-online.target
Wants=network-online.target

[Service]
User=aiteam-relay
WorkingDirectory=/srv/aiteam-relay
Environment=RELAY_HOST=127.0.0.1
Environment=RELAY_PORT=8790
EnvironmentFile=-/etc/aiteam-relay.env
ExecStart=/srv/aiteam-relay/.venv/bin/aiteam-relay
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### Cloudflare Tunnel ingress
Add one line to the tunnel's `ingress:` list, above the catch-all, then restart `cloudflared`:
```
  - hostname: mcp.aiteam.space
    service: http://127.0.0.1:8790
```
And a DNS route for the host: `cloudflared tunnel route dns <tunnel> mcp.aiteam.space`.

### OpenAI domain challenge
At app submit time OpenAI shows a token. Put it in `/etc/aiteam-relay.env` as `OPENAI_APPS_CHALLENGE=<token>` (or
write it to a file and set `OPENAI_APPS_CHALLENGE_FILE`), restart the service, then check that
`https://mcp.aiteam.space/.well-known/openai-apps-challenge` returns exactly the token.

### Anthropic egress
Claude connects to MCP servers from Anthropic's published egress range `160.79.104.0/21`. No Cloudflare allowlist is
needed. Only if a WAF rule, Bot Fight Mode or Browser Integrity Check blocks those requests, add a skip rule for
`mcp.aiteam.space` (or that range).

## Rollback
Remove the `mcp.aiteam.space` ingress line (and its DNS route) and restart `cloudflared`. Stop the service.
Per-Son URLs (`https://<slug>.aiteam.space/mcp`) keep working, because they never depended on the relay.

## Depends on
The token-exchange grant on aiteam.space (`grant_type=urn:ietf:params:oauth:grant-type:token-exchange`). Until that
is deployed, every `/mcp` call with a valid token gets 401 (`invalid_grant` from the token endpoint).
