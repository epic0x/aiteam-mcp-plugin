# Telegram groups with topics: control from Claude or ChatGPT (free)

The Telegram counterpart of [DISCORD-THREADS.md](./DISCORD-THREADS.md): one topic per task, for its whole life.
Part of the free base, for everyone with the connector.

## Setup

- Connector config: `"telegram": {"bot_token_env": "TELEGRAM_BOT_TOKEN"}` (the Son's own bot; the token stays in
  memory, is never logged and never returned). Without it the `telegram_*` tools are not listed.
- In the group: turn **Topics** on (group settings), add the Son's bot, and make it an admin with **Manage topics**
  (and **Pin messages** if wanted). To read every message (not just mentions), turn the bot's privacy mode off in
  BotFather or keep it admin.
- `telegram_chat_get` says whether Topics are on and what the bot may do.

## Tools

| Tool | Hint | What |
|---|---|---|
| `telegram_chat_get` | read | title, `is_forum`, bot status, `bot_can_manage_topics`, `bot_can_pin` |
| `telegram_topics_list` | read | topics the Son is in + topics created through the connector, each with its `target` |
| `telegram_topic_messages` | read | latest messages of one topic, as the Son saw them |
| `telegram_topic_send` | write | post in a topic (optional `reply_to_message_id`); `delivered: true` with the message ids |
| `telegram_topic_create` | write | open a topic (`name`, optional `icon_color`, optional first `message`) |
| `telegram_topic_edit` | write | rename and/or close or reopen a topic (`topic_id` 1 = General) |

`messages_send` to `telegram:<chat_id>:<topic_id>` also posts inside a topic (the Son's Hermes send path).

## Telegram limits (Bot API)

- No "list topics" and no history call for bots. The list merges the topics the Son has a session in with the ones
  created here; reading returns what the Son saw since it joined.
- Topic names: 1-128 characters. Messages: 4,096 characters, longer text is split in order.
- General (`topic_id` 1) takes no thread id when posting; the tools handle that.

## Life of a task

1. `telegram_topic_create` with `name: "Task: <title>"` and the brief as `message` (tag the Son by `@handle`).
2. Every follow-up: `telegram_topic_send` (or `messages_send`) to that topic.
3. Done: `telegram_topic_edit` with `name: "Closed: Task: <title>"` and `closed: true`.

Never open, rename or close a topic on your own initiative: only when the owner asks.
