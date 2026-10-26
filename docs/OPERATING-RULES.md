# Operating rules for assistants driving a Son through this connector

The rules ship as an installable skill from **`epic0x/aiteam-skills@v0.3.3`**, skill `driving-chat-sessions`
(https://github.com/epic0x/aiteam-skills/releases/tag/v0.3.3). This connector depends on that tag and keeps no copy
of the skill text. To update the rules, change the skill in aiteam-skills, tag a new release, then bump the pin in
[`skills.lock.json`](../skills.lock.json).

## Connector-side note: the send path
`messages_send` runs inside the Son's `hermes mcp serve` process (behind this connector), not inside the chat
gateway. The launcher that starts `hermes mcp serve` must load `TELEGRAM_BOT_TOKEN` and `DISCORD_BOT_TOKEN` from
`$HERMES_HOME/.env`, and it must never log their values. Without them, `messages_send` to Telegram fails with "You must
pass the token you received from BotFather". The skill covers the other errors ("Unauthorized", "Chat not found").
