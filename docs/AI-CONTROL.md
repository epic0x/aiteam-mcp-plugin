# AI sign-in and model: `ai_*` tools

Set which AI a Son thinks with, from Claude or ChatGPT, with no dashboard and no secret in the chat. These tools make
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
- ChatGPT: open the provider link and type the user code; nothing to paste (the link is optional).
- API keys: paste the key on the aiteam.space link.
