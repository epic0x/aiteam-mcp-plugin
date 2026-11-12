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
| `GET/POST/DELETE /mcp` | 401 + `WWW-Authenticate: Bearer resource_metadata=".../oauth-protected-resource/mcp"` without a valid token (`error="invalid_token"` when one was sent); else exchange and stream-proxy |

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
| `boxes_list` | The boxes in the connection, each with a live health check (the box's `box_health`, 5 s per box). |
| `kanban_list`, `kanban_read`, `memory_read` | Read-only. `box` = one box, or `all` to ask every box at once. The answer is grouped per box with `ok` or `error`. |
| `calendar_post` | Adds a post to a content calendar box in the connection through its `board_add_item` tool. With `date` it waits for review on that day; without one it lands in Unscheduled. It never approves or publishes. |

Annotations: when boxes disagree on a tool's hints, the relay shows the least safe one (read-only only if every box
says so, destructive if any box says so).

### Limits
- 50 boxes per connection. Anything after that is dropped and `boxes_list` reports `truncated`.
- 8 boxes asked at once, 5 s per box for listing tools, health and `all` reads. A box that is down or slow is left
  out of the tool list and shows `ok: false` in `boxes_list`; the other boxes still answer.
- An `all` answer is capped at 256 KB. Boxes past the cap show `truncated` and can be asked on their own.
- Tool lists are cached 60 s per box, the box list 30 s, box tokens until 30 s before they expire.
- Inline calendar media (`media_b64`) up to 2 MB. Use `media_url` for anything larger.

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
`target` as before. With `RELAY_MULTI_BOX=off` (the default) the relay is the plain stream proxy described above.

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
