# Auth design: "aiteam.space sign-in + Approve" for Son MCP connectors

Status: Phase 1 is live on aiteam.space (sign-in + Approve, OAuth 2.1 with PKCE and DCR).
Son gate change below is a spec + reference patch (`patches/gate-aiteam-jwt.py`), **not applied to any box**.
Phase 2 (single relay `mcp.aiteam.space`) is design only. Fixes G1, G5, G6 of [RESEARCH-2026-10-10.md](RESEARCH-2026-10-10.md); prepares G2.

## 0. Sources (read 2026-10-11 from the live docs)
- MCP authorization spec 2025-11-25: https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
  (2025-06-18 version: https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization)
- Claude connector auth: https://claude.com/docs/connectors/building/authentication
- ChatGPT / Apps SDK auth: https://developers.openai.com/apps-sdk/build/auth
- RFC 9728 protected resource metadata https://datatracker.ietf.org/doc/html/rfc9728 · RFC 8414 AS metadata https://www.rfc-editor.org/rfc/rfc8414 ·
  RFC 7591 DCR https://www.rfc-editor.org/rfc/rfc7591 · CIMD draft https://datatracker.ietf.org/doc/html/draft-ietf-oauth-client-id-metadata-document-00 ·
  RFC 7636 PKCE https://www.rfc-editor.org/rfc/rfc7636 · RFC 8707 resource indicators https://www.rfc-editor.org/rfc/rfc8707 ·
  RFC 9207 `iss` https://www.rfc-editor.org/rfc/rfc9207 · RFC 7009 revocation · RFC 8252 loopback redirects

### What the spec and the two clients require (and what we do)
| Requirement | Source | aiteam.space |
|---|---|---|
| MCP server serves PRM (RFC 9728), 401 with `WWW-Authenticate: Bearer resource_metadata=...` | MCP spec | Son gate (unchanged mechanism, new `authorization_servers`) |
| PRM `resource` = exact MCP URL; Claude uses only the **first** `authorization_servers` entry | Claude doc | gate lists `https://aiteam.space` only |
| AS metadata (RFC 8414 or OIDC discovery) | MCP spec | `/.well-known/oauth-authorization-server` |
| OAuth 2.1, PKCE S256 required, advertise `code_challenge_methods_supported:["S256"]` | MCP spec, Claude, ChatGPT | yes, `plain` refused |
| Client registration: CIMD (SHOULD), DCR (MAY), pre-registered | MCP spec | DCR + CIMD |
| Claude uses CIMD only if metadata has `client_id_metadata_document_supported:true` **and** `none` in `token_endpoint_auth_methods_supported`; else DCR | Claude doc | both advertised |
| ChatGPT CIMD client_id `https://chatgpt.com/oauth/client.json` (stable) or `.../oauth/{callback_id}/client.json` | ChatGPT doc | any https CIMD URL; redirects must still pass the allowlist |
| Redirects: `https://claude.ai/api/mcp/auth_callback`; Claude Code loopback `http://localhost|127.0.0.1/callback` any port | Claude doc | allowlisted (+ `claude.com` mirror) |
| Redirects: `https://chatgpt.com/connector_platform_oauth_redirect` if AS returns `iss` on every authorization response and advertises `authorization_response_iss_parameter_supported`; else `https://chatgpt.com/connector/oauth/{callback_id}` | ChatGPT doc | both allowlisted; `iss` always returned |
| `resource` param in authorize **and** token requests; token audience-bound; server MUST reject tokens for other audiences | MCP spec (RFC 8707), ChatGPT "echo resource" | `aud` = canonical resource; gate checks it |
| Token endpoint form-urlencoded; `invalid_grant` for dead refresh; rotate refresh tokens for public clients; < 10 s (refresh < 30 s) | Claude doc | yes (aiteam.space's production database: Postgres; HTTP driver through Cloudflare Tunnel + Access, ~100 ms) |
| Claude appends `offline_access` when AS lists it in `scopes_supported` | Claude doc | listed |
| ChatGPT may send `id_token_hint` on re-auth | ChatGPT doc | ignored (no OIDC); user just re-approves |

## 1. Phase 1 flow (one Son = one MCP URL, e.g. `https://my-son.aiteam.space/mcp`)
```
Claude/ChatGPT ──GET /mcp──▶ Son gate :8701 ──401 + resource_metadata──▶
   ──GET /.well-known/oauth-protected-resource/mcp (gate)──▶ {resource: https://my-son.aiteam.space/mcp, authorization_servers: [https://aiteam.space]}
   ──GET https://aiteam.space/.well-known/oauth-authorization-server──▶ metadata
   ──POST /oauth/register (DCR)  or  client_id = CIMD URL
   ──browser: /oauth/authorize?client_id&redirect_uri&state&code_challenge(S256)&resource=https://my-son.aiteam.space/mcp&scope
        not signed in → /login?next=<this URL> → magic link → back
        consent: "Allow Claude to use <Son name>"  [scopes in plain words]  [Cancel] [Approve]
        (resource matches one of the account's Sons → that Son; resource absent or = relay → Son picker of own Sons;
         resource = a Son the account does NOT own → invalid_target, no token ever)
   ──303 redirect_uri?code&state&iss=https://aiteam.space
   ──POST /oauth/token (code + code_verifier + resource) → {access_token (JWT, 1 h), refresh_token (rotating), scope}
   ──GET /mcp  Authorization: Bearer <JWT> ──▶ gate verifies with JWKS → mcp-proxy :8700 → hermes mcp serve
```

### Token format (aiteam.space → gate contract)
JWS compact, header `{"alg":"ES256","typ":"at+jwt","kid":"..."}`. Claims:
| claim | value |
|---|---|
| `iss` | `https://aiteam.space` (exact) |
| `sub` | account email (owner) |
| `aud` | the Son's MCP resource URL, canonical (lowercase host, no trailing slash, no query) |
| `son_id` | `aiteam.sons.id` (uuid) |
| `scope` | space list: `son:read son:write [offline_access]` |
| `client_id`, `jti`, `gid` (grant id), `iat`, `exp` (= iat + 3600) |

Keys: `GET https://aiteam.space/oauth/jwks` (cache 5 min, refetch on unknown `kid`). Secret on the Worker:
`OAUTH_SIGNING_KEY` (P-256 private JWK with `kid`), optional `OAUTH_SIGNING_KEY_PREV` for rotation.

Revocation: owner presses **Revoke** on the Son page (Connected apps) or the client calls `/oauth/revoke` → grant revoked,
refresh tokens dead immediately; the last access token lives at most 1 h (accepted trade-off; no introspection call on the
hot path). Reused refresh token or replayed code → whole grant revoked.

DB (migration `0015_oauth.sql`, run by the DB owner): `oauth_clients`, `oauth_codes` (hashed, single use, 5 min),
`oauth_grants` (shown as Connected apps), `oauth_refresh_tokens` (hashed, 30 days), `sons.mcp_url`
(explicit MCP URL; default `<origin of sons.url>/mcp`). **For a Son with a hand-built gateway, set `sons.mcp_url = 'https://my-son.aiteam.space/mcp'` on that Son's row**
(owner account must be the aiteam.space account that should be able to approve).

## 2. Son gate change (:8701) — spec
Config (env on the box, names proposed):
| var | default | meaning |
|---|---|---|
| `AITEAM_AUTH` | `on` | accept aiteam.space JWTs |
| `AITEAM_ISSUER` | `https://aiteam.space` | expected `iss`; also the only `authorization_servers` entry |
| `AITEAM_JWKS_URL` | `$AITEAM_ISSUER/oauth/jwks` | key set |
| `SON_RESOURCE` | `https://<host>/mcp` | this gate's canonical resource; must equal `aud` |
| `SON_ID` | (required) | this Son's `aiteam.sons.id`; must equal `son_id` |
| `LEGACY_APPROVAL_CODE` | `off` | `on` keeps the old local AS (`/authorize` approval code, `/token`, `/register`) as a fallback |

Behaviour:
1. `/.well-known/oauth-protected-resource[/mcp]` returns
   `{"resource": SON_RESOURCE, "authorization_servers": ["https://aiteam.space"], "scopes_supported": ["son:read","son:write"], "bearer_methods_supported": ["header"], "resource_documentation": "https://aiteam.space/help/connect"}`.
   With `LEGACY_APPROVAL_CODE=on` the list stays `["https://aiteam.space"]` (Claude only uses the first entry); the legacy AS is
   only reachable by clients that already hold legacy tokens or were configured by hand.
2. `/.well-known/oauth-authorization-server` on the gate: **remove** when legacy is off (clients must discover aiteam.space).
3. Every `/mcp` request: `Authorization: Bearer <t>` →
   - 3 dot-separated parts and header `alg == ES256` → verify signature with the JWKS key matching `kid`
     (refetch JWKS once on unknown kid, at most every 60 s); check `iss == AITEAM_ISSUER`, `aud == SON_RESOURCE`,
     `son_id == SON_ID`, `exp > now - 30`, and `son:read` or `son:write` in `scope`. Pass → proxy to :8700 and
     add `X-Aiteam-Sub`, `X-Aiteam-Scope` headers (Hermes can log who called).
   - else, if `LEGACY_APPROVAL_CODE=on` → existing opaque-token check.
   - else 401 `WWW-Authenticate: Bearer resource_metadata="https://<host>/.well-known/oauth-protected-resource/mcp", error="invalid_token"`
     (insufficient scope → 403 `error="insufficient_scope", scope="son:write"`).
4. Never forward the bearer token to mcp-proxy / Hermes (MCP spec: no token passthrough).
5. Tool gating by scope (follow-up, needs Hermes tool annotations G3): `son:read` only → write tools refused at the gate.

Reference implementation: [`patches/gate-aiteam-jwt.py`](../patches/gate-aiteam-jwt.py) (stdlib + `cryptography`), drop-in
`verify_bearer()` + PRM handler. Rollout per box: set env → restart gate → `curl -sI https://<host>/mcp` shows 401 with
resource_metadata → PRM lists aiteam.space → connect from claude.ai (Settings → Connectors → Add custom connector → URL) →
sign in → Approve → tool call works → Revoke on aiteam.space → next refresh fails (≤ 1 h for the access token).

## 3. Phase 2 (built in relay/, see RELAY.md): one relay `https://mcp.aiteam.space/mcp`
> Built in [relay/](../relay/), see [RELAY.md](RELAY.md). Deviations from the text below: a Cloudflare Tunnel to a
> Python service instead of a Worker + Durable Object, and an RFC 8693 token exchange at aiteam.space instead of a
> relay-held signing key for the relay to Son hop. The tools are the Son connector's own.

Why: store listings take one URL (ChatGPT: one server per plugin; Claude per-user URL patterns review slower) — G2.
- The relay is the **only** protected resource the stores see. PRM: `{resource: "https://mcp.aiteam.space/mcp", authorization_servers: ["https://aiteam.space"]}`.
- Authorize with `resource=https://mcp.aiteam.space/mcp` → consent shows the **Son picker** (already implemented:
  `RELAY_RESOURCE` in `oauth-rules.ts`) → token `aud=https://mcp.aiteam.space/mcp`, `son_id=<picked>`.
- Relay verifies the JWT itself (same rules, `aud` = relay URL), re-checks that the grant is not revoked (cheap DB/KV
  lookup by `gid`, gives instant revocation), then routes to the Son by `son_id`:
  hosted Sons over the fleet's private network; self-hosted Sons over an outbound tunnel the Son opens to the relay
  (WebSocket, paired to the account by a one-time install code). No inbound ports, no per-user hostname.
- The relay exposes a small reviewed tool set with `title` + `readOnlyHint`/`destructiveHint`/`openWorldHint`
  (G3/G4): `ask_son` (write), `get_son_status` (read), `list_son_tasks` (read). Messaging bridge + exec approval off.
- Relay → Son hop uses a separate short-lived relay-signed token (`aud=<son resource>`), never the client's token.
- Hosting: Cloudflare Worker + Durable Object per Son connection (tunnel endpoint), same aiteam.space production database for grants (oauth tables; Postgres, reached through Cloudflare Tunnel + Access).
- Store listing: Claude "Universal URL", auth `oauth_cimd`/`oauth_dcr`; ChatGPT domain verification at
  `https://mcp.aiteam.space/.well-known/openai-apps-challenge`.

## 4. Open decisions
- D1 Phase 2 relay yes/no (from RESEARCH §4) — the AS already supports it.
- D2 Scopes: ship with `son:read` + `son:write` both default (current) or default read-only and ask for write explicitly.
- D3 Access token lifetime 1 h without introspection (revoke takes ≤ 1 h on per-box gates) vs. gate calling an introspection endpoint.
- D4 Keep `LEGACY_APPROVAL_CODE` on during rollout for the existing hand-built gateways, or cut over at once.

## 5. Verification
- The aiteam.space OAuth check script passes locally: PKCE RFC 7636 vector, allowlist, code single use/replay, refresh rotation/reuse, owned-Sons-only, revoke, JWT aud/son_id/expiry/tamper.
- Cross-language: a token minted by `app/lib/oauth-rules.ts signJwt` verifies in `patches/gate-aiteam-jwt.py verify_aiteam_jwt`
  (cryptography 50); wrong `son_id`, wrong `aud` and a tampered signature are refused.
- Live today (unchanged): `https://my-son.aiteam.space/.well-known/oauth-protected-resource` → `authorization_servers: ["https://my-son.aiteam.space"]`, scope `mcp` (local AS with approval code).
- Not done: end-to-end with claude.ai / ChatGPT (needs migration + `OAUTH_SIGNING_KEY` + a gate rollout).
