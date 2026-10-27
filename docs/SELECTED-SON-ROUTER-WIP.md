# Selected-Son relay: bounded experimental increment (not deployable)

The versioned `grant_v:2` path is **off by default**. Do not set
`RELAY_EXPERIMENTAL_MULTI=1` in any deployed environment or enable site-side
multi-select consent on the basis of this branch. The legacy singleton path
keeps the original opaque proxy and exchange request shape.

## Implemented in this branch

- Verify signed relay-audience v2 claims with nonempty, distinct `son_ids`,
  `sub`, `client_id`, `gid`; reject mixed singleton/multi claims. The JWT
  signature, issuer, audience, expiry and Son scope still use the existing
  verifier. This is **not** a membership proof: the authorization server must
  check grant, client, owner and exact target on every exchange.
- Send explicit `son_id` on v2 token exchange; cache by subject-token hash and
  target, cap TTL at 300 seconds, and invalidate only the failing target on
  Son 401. Keep the host allowlist and never send the incoming bearer/cookies
  to the Son.
- Experimental 2026-07-28 JSON-RPC POST: namespace colliding tool names,
  resource URIs and prompt names; paginate all selected Sons' list responses;
  route call/read/get after checking the selected claim and upstream listing.
  Incompatible, ambiguous or unsupported operations fail closed. No
  account/billing handler is present.
- `server/discover` now combines only compatible modern Son capabilities and
  instructions; refuses unknown capability settings, header-annotated tools,
  and nonempty resource templates instead of promising an incomplete surface.
  Discovery is never publicly cacheable (`ttlMs: 0`, `cacheScope: private`).
  Discovery now probes all advertised tools, resources and prompts lists before
  offering them: an SSE-only or malformed list on either selected Son refuses the
  entire discovery instead of advertising a surface the router cannot list.
  `resources/templates/list` returns empty only after checking **every** Son,
  including paginated results. Nonempty templates block discovery and resource
  listing until URI-template routing exists.
- Request-scoped, stateless SSE on `tools/call`, `resources/read`, and
  `prompts/get`: stream progress/message notifications and the final response,
  bound to the request ID; rewrite selected resource URIs. Reject upstream
  sessions and resumable IDs, unrelated events/notifications and malformed or
  unbounded streams. If an error occurs after the first event, emit a JSON-RPC
  SSE error and close; there is no cross-Son resumable event ID. Validate
  incoming Origin on v2, and decode/encode `Mcp-Name` sentinel headers.

## Blocking work before review/deploy

1. **Real client session era is not supported.** Claude/ChatGPT compatibility
   requires testing against actual clients and each Son gate. Implement
   isolated downstream sessions with per-target upstream `Mcp-Session-Id`, GET
   and DELETE streams, `Last-Event-ID` replay ownership and expiration, or
   demonstrate real clients negotiate modern stateless MCP on staging. Current
   v2 rejects these headers and methods; never share or forward them between
   selected Sons.
2. **Modern MCP coverage is incomplete.** Direct lists still reject Son
   responses using SSE; discovery rejects those lists instead of exposing a
   partial capability. Nonempty `resources/templates/list`, tools with
   `x-mcp-header` mirrored parameters, and nontrivial capability settings also
   remain unsupported. Implement complete, target-qualified templates;
   protocol-correct JSON-RPC errors (`HeaderMismatch`,
   `UnsupportedProtocolVersionError`); validate all
   mirrored header fields; and support notification subscriptions
   (`subscriptions/listen`, list changes and resource updates) before claiming
   full discovery. Reject rather than truncate when a Son has an unsupported
   surface. The current request SSE path is covered only by fake transports,
   not a live Son or reverse-proxy streaming test.
3. **Authorization server contract:** align and test the v2 exchange with a
   signed JWT fixture, exact `grant_v`, `son_ids`, `gid`, `client_id`, `sub`
   and relay audience; enforce ownership, live membership, revocation and Son
   URL on every target exchange at the site. A relay fixture is not proof that
   the site actually issues/validates these grants.
4. Test a real Son gate and real Claude and ChatGPT connectors on isolated
   staging; no live selected-Son E2E was run in this branch.

The v2 feature flag is a development gate, **not** a consent rollout switch.
Do not open a full-support review PR or merge/deploy this branch until all
four blockers are resolved and verified.

Protocol references: official MCP 2026-07-28 `server/discover`,
`basic/transports/streamable-http` and `basic/versioning` specifications,
plus 2025-11-25 `basic/transports` for legacy sessions. Modern transport removes
GET, protocol sessions and Last-Event-ID; legacy clients still use them.
For Claude, keep the existing **custom connector** with single-Son consent;
a directory listing is a separate submission/discovery path, not a way to
activate selected-Son consent. Neither custom nor directory connector should
advertise v2 until the above gates pass.
