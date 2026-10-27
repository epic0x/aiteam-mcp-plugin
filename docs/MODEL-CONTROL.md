# Model and provider control

Four connector tools, on by default:

| Tool | Does | Annotation |
|---|---|---|
| `son_model_status` | Global primary/fallback order, configured provider/model ids, and effective route, override, live cached agent and fallback order for one session or a paginated inventory | read-only |
| `son_model_set_all` | Set the Son default and replace existing gateway sessions' model/provider selections | write, idempotent, non-destructive |
| `son_model_set_session` | Set or clear one existing gateway session's selection | write, idempotent, non-destructive |
| `son_model_fallback_set` | Replace the Son-wide fallback order; optional `primary` changes its first route too | write, idempotent, non-destructive |

Examples (use the provider/model ids returned by **this Son**, not a universal model catalogue):

```json
{"tool":"son_model_status","arguments":{"limit":50,"offset":0}}
{"tool":"son_model_set_all","arguments":{"provider":"anthropic-secondary","model":"claude-opus-5-5"}}
{"tool":"son_model_set_session","arguments":{"sessionId":"<from sessions_list>","provider":"anthropic-secondary","model":"claude-opus-5-5"}}
{"tool":"son_model_set_session","arguments":{"sessionKey":"<from sessions_list>","clear":true}}
{"tool":"son_model_fallback_set","arguments":{"primary":{"provider":"anthropic-primary","model":"claude-opus-5-5"},"fallback":[{"provider":"anthropic-secondary","model":"claude-opus-5-5"},{"provider":"openai-codex","model":"gpt-6.1-sol"}]}}
```

`sessionKey` or `sessionId` is accepted. If both are supplied, they must identify the same current routing entry;
a reset/reused key is refused, not silently preferred over a different id. Read inventory returns `hasMore` and
`nextOffset`. The model/provider shown as `effective` is the **next-turn selection**, not a claim that a turn already
running has changed; `live_agent` and `running` show the cached runtime separately.

## Authentication and boundaries

The Son's existing public gate validates the aiteam.space JWT (signature, issuer, audience, Son binding, scope),
strips all incoming `X-Aiteam-*`, then forwards the verified subject as `X-Aiteam-Sub`. These tools require that
verified caller even for reads. They have no anonymous/stdio exemption. The connector remains loopback-only.
`AITEAM_SON_OWNERS` (comma-separated owner emails) optionally narrows callers; when that list is configured, its
owners plus the existing `AITEAM_GATE_EXEMPT` operator exemptions are accepted, and other callers are refused.
The legacy exemption list alone does not exclude legitimate Son owners. Without an explicit owners list the
gate's Son-bound owner token is the authority. Do not expose the connector port without the JWT gate, or trust a client-supplied subject header.

Provider names and model ids must already be declared in `providers` / `custom_providers`, the current primary or
fallback config. Configured model lists/defaults and current routes form the allowlist; an arbitrary provider,
unknown model, credential, or caller-supplied endpoint is not accepted. To allow a new model, configure it on the Son
first through the normal Hermes setup. Credential resolution is checked before a write; credentials themselves,
commands, account labels and base URLs never appear in results.

Only **gateway-managed routing sessions** can receive a session override. Historical/unrouted CLI, cron, API or
child sessions are not rewritten, and multiplex-profile gateways fail closed. A global default is available to
future processes that inherit it, but this tool does not interrupt running CLI/cron/subagent processes or rewrite
their explicit model pins. It controls one Son, not the whole fleet.

## What changes and when

- An in-flight turn finishes on its existing model. No process or customer Son is restarted by a tool call.
- A session switch uses `SessionStore.set_model_override` (only model/provider persisted). The running gateway
  drops the old in-memory override, then rehydrates the chosen provider's credentials on the next turn. Idle cache
  entries are evicted; running agents are left alone. Pending older `/model --once` restores are cancelled so they
  cannot overwrite a newer owner selection.
- Set-all saves `model.default` and `model.provider` using Hermes' route-cleanup rules, removing stale endpoint,
  key and wire settings. By default it clears existing session pins. Existing sessions with a channel override
  receive a fresh session pin so the channel's old route cannot defeat the all-session switch. Future, newly
  created chats still obey any separately configured channel override. `clear_session_overrides=false` changes
  only the default and deliberately leaves existing pins in place.
- Fallback remains Son-wide, not per-session. A session's displayed order starts at its own effective primary and
  then shows the common configured fallback list. `fallback=[]` disables fallback. Duplicate entries in a new order
  are rejected. Optional `primary` does not clear existing session/channel pins: use set-all for that.
- Primary route fields are committed in one atomic config-file replacement. An optional primary + fallback update
  is also one replacement. The legacy `fallback_model` key is removed when replacing the order. Other config and
  comments are retained. The gateway's existing model-switch lock serializes tool changes with `/model` commands.
- Set-all is not a multi-session database transaction: storage failures return `ok=false, partial=true`, the new
  global primary and failed session keys; retry instead of claiming everything changed.

## Gateway-owned endpoint

`son/model_control/HOOK.yaml` + `handler.py` install as:

```
<HERMES_HOME>/hooks/aiteam-model-control/{HOOK.yaml,handler.py}
```

On `gateway:startup` the hook starts a local UNIX-domain endpoint:

```
<HERMES_HOME>/mcp/model-control.sock    # 0600; same uid as Hermes/connector, never TCP
```

One JSON line per connection, one response line. Protocol:

```json
{"verb":"status","args":{"session_id":"..."}}
{"verb":"set_global","args":{"provider":"...","model":"...","clear_session_overrides":true}}
{"verb":"set_session","args":{"session_key":"...","provider":"...","model":"..."}}
{"verb":"set_session","args":{"session_id":"...","clear":true}}
{"verb":"set_fallback","args":{"fallback":[{"provider":"...","model":"..."}]}}
```

The first/local Son uses that connector's UNIX endpoint. Explicitly configured remote destinations now route
through their own owner-bound box `/v1/control`, never through the local socket; see `GATEWAY-CONTROLS.md`.
Automatic selected-Son routing and fleet discovery remain the separate relay's job. No credentials traverse the socket. A missing socket returns
an actionable error, never a pretend successful switch. Disable all four tools with connector config
`"model_control": false`; override the local socket path with `"model_control":{"socket":"..."}`.

## Install and verification

The aiteam-army consumer must ship both the connector module/registration **and** the hook/boot installer. Updating
only the relay at `mcp.aiteam.space` does not add tools: that service transparently forwards the selected Son's
catalogue. No relay code change or restart is required for these four tools.

The new hook needs a planned gateway start to activate. Do not roll customer Sons or restart active gateways just
to expose it. Build the updated Son image separately, then schedule an owner-approved idle canary. A hand-built
Son must also forward the verified subject header; older gates without `X-Aiteam-Sub` fail closed.

Focused tests:

```sh
uv run --project connector --extra test pytest -q connector/tests/test_model_control.py
uv run --project connector --extra test pytest -q connector/tests
uv run --project relay --extra test pytest -q relay/tests
```

These tests use fake credentials and a fake gateway runner over a **real local UNIX socket**; they do not claim a
real provider completion or production connector test. The separate isolated Hermes probe in the task evidence
exercises real SessionStore persistence, GatewayRunner rehydration and config writes, with credential resolution
stubbed and zero provider calls.
