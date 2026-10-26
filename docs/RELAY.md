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
