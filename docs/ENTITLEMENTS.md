# Add-on entitlements: every tool listed, paid tools answer with an upsell

Rules:

- Every tool stays listed. Claude and ChatGPT can discover and call all of them.
- The free base is the MCP control surface: connecting (Claude or ChatGPT, voice mode included), reading,
  coordinating the Son, Telegram chat and Telegram topic control, GitHub, sessions, files, terminal, browser. The
  connection itself is never refused for a missing add-on.
- A call to a tool that needs a paid add-on the Son does not own returns a normal tool result (`isError: false`)
  with the add-on, its price and the dashboard link. The tool does not run. It is not an error and not a silent no-op.
- The owner is never gated.
- Shared-memory tools (`mnemosyne_shared_*`) are not gated yet.

## Where it runs

`connector/aiteam_connector/entitlements.py`, called first thing in `Connector.call()`, the one path every tool
call takes (connector tools, GitHub, Discord relay, Telegram control, the Son's Hermes tools).

## Source of truth

Stripe -> aiteam.space `aiteam.sons.addons` -> fleet `PUT /tenants/{id}/addons` -> `<HERMES_HOME>/plugs.json`:

```json
{"version": 1, "enforce": true, "comp": false, "addons": ["calls", "telegram", "mcp"], "plugs": {}, "clients": {}}
```

The connector re-reads the file when its mtime or size changes, so a purchase applies on the next call after the
fleet sync. Nothing is gated when:

- there is no `plugs.json` (dev boxes, hand-built gateways), or it has no `addons` list (older fleet),
- `enforce` is not `true` (the fleet switch `FLEET_ENFORCE_PLUGS` is off: the default),
- `comp` is `true` (complimentary Son),
- the caller's verified email is listed in `AITEAM_GATE_EXEMPT` (comma list, set on the owner's boxes, never in
  this repo). The OAuth gate forwards it as `X-Aiteam-Sub` after stripping every inbound `X-Aiteam-*` header
  (`patches/gate-aiteam-jwt.py` `forward_headers`), so a client cannot set it.

## Tool -> add-on map

| Add-on | Slug | Price | Tools |
|---|---|---|---|
| Calls (Meet, Zoom and Discord voice) | `calls` | $20/mo | `voice_meet_join`, `voice_meet_leave`, `voice_meet_mode`, `voice_meet_status`, `meet_join`, `meet_leave`, `meet_say`, `meet_status`, `meet_transcript`, `meet_command` |
| Discord thread management — two-way sync | `discord` | $5/mo | `discord`, `discord_admin`, `discord_thread_create`, `discord_thread_edit`, `discord_thread_member_add`, `discord_thread_member_remove` (thread control), `discord_api` (any Discord REST call). Free: `discord_threads_list`, `discord_thread_get`, `discord_thread_members`, `discord_read`, `discord_threads`, `discord_channels` (reading status, rule 0) and `messages_send` to a `discord:` target (plain chat) |

Paid add-ons that ship skills only are gated on `skill_view` of a skill that only they ship:

| Add-on | Slug | Price | Skills |
|---|---|---|---|
| SEO growth agent | `seo-growth` | $5/mo | seo-growth, seo-geo, blog-playbook, content-creator |
| Social photo posts and content machine | `social-content` | $5/mo | social-content, social-creative, social-media-manager |
| Cold email | `cold-email` | $5/mo | cold-email, lead-research, sales-agent |
| Cinematic video ads and reels | `cinematic-ads` | $20/mo | cinematic-ads, creative-direction, logo-ident, video-reels |
| Explainer and infographic video ads | `explainer-videos` | $10/mo | explainer-videos, video-reels, infographic |
| Trading signals: crypto | `trading-signals` | $15/mo | trading-signals, hyperliquid-trading |
| Trading signals: stocks | `trading-signals-stocks` | $15/mo | trading-signals-stocks |
| Security audit loop | `security-audit-loop` | $20/mo | security-audit-loop, security-audit |
| Calls | `calls` | $20/mo | calls, google-meet, aiteam-google-meet, zoom-calls, zoom-voice, discord-voice |

Shared skills only paid add-ons ship: x-posting (SEO, social, cinematic, explainer) and content-calendar (social,
cinematic, explainer). A skill that several paid add-ons ship opens with any of them; the upsell names the cheapest.
Skills that a free add-on or every Son also ships stay open (`FREE_ELSEWHERE` in entitlements.py, with the reason):
claims-check, meeting-notes (free Calendar add-on), humanizer, manim-video, competitor-news-monitor (base image). Generic tools (terminal, browser, web, video generation) are
never gated: that would break free use.

Never gated: Telegram (`messages_send` to `telegram:`, every `telegram_*` tool), GitHub, sessions, son_*, memory,
`mnemosyne_shared_*`, and the free bundles (starter, technology, CMO pack, uptime monitor).

## The response

```json
{
  "isError": false,
  "content": [{"type": "text", "text": "This needs the Calls (Meet, Zoom and Discord voice) add-on. Add it for $20/mo from your aiteam.space dashboard: https://aiteam.space/my-sons/<son_id>?add=calls"}],
  "structuredContent": {
    "paywall": true, "tool": "voice_meet_join",
    "addon": {"slug": "calls", "name": "Calls (Meet, Zoom and Discord voice)", "price_usd": 20, "period": "month"},
    "url": "https://aiteam.space/my-sons/<son_id>?add=calls",
    "message": "<same text>"
  }
}
```

The usage guide tells the assistant to relay the message as is, and not to retry or work around it. `?add=<slug>`
opens that add-on on the Son's page (aiteam.space companion PR).

## Keeping the map current

Names and prices mirror aiteam.space `app/lib/addons.ts` and `plug-rules.ts`. `tests/test_entitlements.py` pins this
page against the map, and `tests/fixtures/catalogue.json` (a copy of the catalogue's slug, price and skills) against
`SKILL_ADDONS`: every skill only paid add-ons list must be gated, so catalogue drift fails CI. Refresh the fixture
when the catalogue changes.
