# AI sign-in and model: `ai_*` tools

Set which AI a Son thinks with, from Claude or ChatGPT, without a terminal and without putting credentials in chat. These tools make
no model call on the Son, so they work on a brand-new Son before any provider is signed in (onboarding: `ai_status`,
then `ai_signin_start`). All five are free (never gated by entitlements).

| Tool | Box endpoint | Plugin endpoint | Annotations |
| --- | --- | --- | --- |
| `ai_status` | `GET /v1/ai/status` | `GET /ai/status` | read-only |
| `ai_signin_start` | `POST /v1/ai/signin/start` | `POST /ai/signin/start` | write, not destructive, not idempotent, open world |
| `ai_signin_status` | `GET /v1/ai/signin/{id}` | `GET /ai/signin/{id}` | read-only |
| `ai_model_set` | `POST /v1/ai/model` | `POST /ai/model` | write, not destructive, idempotent |
| `ai_fallback_set` | `PUT /v1/ai/fallback` | `PUT /ai/fallback` | write, not destructive, idempotent |

## Box

The box proxies the five routes to the Son's `aiteam-ai-control` gateway plugin, behind the same caller-token check
as `/v1/sons` (401 without it). Config (all optional, these are the defaults):

```json
"ai_control": {"url": "http://127.0.0.1:8651", "key_env": "API_SERVER_KEY"}
```

The key is read from the env var named by `key_env`, else from `$HERMES_HOME/.env` (default `/opt/data`), on every
call; it never leaves the box. Request bodies are capped at 16 KB and never logged. Plugin down or no key: `503
{"error": "ai control not available on this Son yet"}`. Plugin 4xx: status and JSON passed through (401/403 from the
plugin become a 502: that is the box's own key, not the caller's). Plugin 5xx: `502` with a generic message.

**The box never exposes `/ai/credential`.** A Claude code or an API key reaches the Son only through the one-time
aiteam.space page and the Son gate.

## Sign-in link

`ai_signin_start` turns the plugin's one-time `ticket` into `https://aiteam.space/connect/ai/<son_id>?t=<ticket>`.
`son_id` comes from the connector config `son_id` (or per-Son `son_id`), else `$HERMES_HOME/mcp/son.json`, else env
`SON_ID`; `site_url` overrides `https://aiteam.space`. The ticket appears only inside that link: the structured
result carries `secure_link`, never a `ticket` key. If the Son id is unknown the result still gives the provider link
and says to use the Son's page on aiteam.space.

- Claude: open the provider link, sign in, paste the code Claude shows on the aiteam.space link.
- ChatGPT: open the provider link and type the user code; nothing to paste. Use `ai_signin_status` to check progress.
- API keys: paste the key on the aiteam.space link.

## Gateway plugin installation

Copy `hermes-plugin/aiteam-ai-control/{plugin.yaml,__init__.py}` into
`$HERMES_HOME/plugins/aiteam-ai-control/`, then run `hermes plugins enable aiteam-ai-control`.
The current Hermes gateway supports plugin live reload; confirm the CLI reports that the gateway reloaded it.
Do not restart a gateway carrying active jobs just to apply a model choice. On older Hermes releases without
live reload, schedule installation during the operator's normal upgrade window.

The plugin listens only in the `hermes gateway run` process, on loopback port 8651 (override with
`AITEAM_AI_CONTROL_PORT`). All endpoints require the Son's existing `API_SERVER_KEY`. Standalone chats and cron
workers do not bind the port. A plugin unload shuts down its listener. Missing key fails closed.

In a box with more than one Son target, set `ai_control.son` to the local Son's name. A caller whose Son allow-list
does not include that target receives 403 on every AI-control endpoint. An ambiguous multi-Son box receives 503.

Model changes take effect on the next turn. In-flight jobs retain their clients and finish normally. Session scope
persists just that session's route. All scope writes the global default, clears gateway and API/browser session pins,
and removes channel model/provider overrides while keeping their prompts. Other Hermes profiles are not modified.
A separate, already-running CLI process retains its current runtime until it adopts configuration or resumes.

Fallback order: `PUT /ai/fallback {"chain": [...]}` replaces the whole chain in one config write. `chain: []` turns
fallback off: it writes `fallback_providers: []` and removes the legacy `fallback_model` key (the gateway merges
both, so leaving it would keep a fallback). A step that repeats the main model or an earlier step is refused with
400 and nothing is written. `ai_status` reports the merged chain the gateway will actually run. The gateway re-reads
the chain on every turn, so no restart is needed.

Sign-in tickets are random, stored only as SHA-256 hashes in the Son process, expire after 15 minutes, and cannot
be reused after a successful submission. A gateway restart invalidates pending tickets. Claude exchange requires
the complete `code#state` from the matching PKCE request. Provider errors are mapped to static copy, not echoed.

## Tests

Run connector tests from `connector/` using the project's test extra. Run the plugin test file with a Hermes-enabled
Python environment and an isolated `HERMES_HOME`, never the live profile. The plugin suite covers authentication,
model persistence and session isolation, API/browser locks, no interruption of running jobs, ticket expiry/replay,
mandatory OAuth state, and credential-free responses and logs.

The hosted secure entry page and the Son gate route are companion deployments. Publishing this package alone does
not deploy those components or upgrade existing customer containers. Verify the page, gate, plugin and connector
as a single path before describing sign-in as live.
