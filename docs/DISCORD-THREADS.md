# Discord threads: one thread per task

Owner rule: every task lives in ONE Discord thread for its whole life. The first post opens it, and every follow-up,
steer, result and the final "Closed: " rename happen inside that same thread. This page is what the connector needs
for that, and how Claude or ChatGPT should use it.

## Bot permissions
The connector's Discord bot (`discord.bot_token_env` in the connector config) posts through a webhook on the thread's
PARENT channel and manages threads with its own token. In the server, or on the parent channel, it needs:

| Permission | Why |
|---|---|
| View Channel, Read Message History | resolve the thread, read messages back to prove delivery |
| Send Messages, Send Messages in Threads | native replies (`reply_to_message_id`) and posts inside threads |
| Manage Webhooks | find or create the `aiteam connector` webhook on the parent channel |
| Manage Threads | rename, archive/unarchive, lock/unlock, reopen an auto-archived thread |
| Create Public Threads | `discord_thread_create` |

Permission integer for exactly these: `326954454016` (or grant them through a role). Invite or re-invite with:
`https://discord.com/oauth2/authorize?client_id=<app_id>&scope=bot+applications.commands&permissions=326954454016`.
Discord gives webhooks no per-thread permission: a webhook on the parent posts into any of its threads with
`?thread_id=`, so nothing extra is needed per thread.

## Posting inside an existing thread
`messages_send` with any of these targets posts INSIDE the thread (webhook author, tagged Sons wake, read back):
- `discord:<channel_id>:<thread_id>` (preferred)
- `discord:<thread_id>`
- a thread id put in the channel slot by mistake

Every id is looked up with Discord, and the webhook always lives on the real parent. Before this fix, a thread id in
the channel slot made the connector list webhooks on the thread itself, and Discord answered
`list webhooks on <thread_id> (needs Manage Webhooks): 404 Unknown Channel`. That was a target-resolution bug, not
a missing permission. An auto-archived (not locked) thread is reopened before the post. A thread id that does not
exist under the given channel returns a plain 404 error and nothing is posted.

## Thread tools
| Tool | Hint | What |
|---|---|---|
| `discord_threads_list` | read | threads of a channel (`include_archived` for archived ones), each with its `target` |
| `discord_thread_get` | read | name, parent, archived/locked, auto-archive, `target`, `url` |
| `discord_thread_edit` | write | `name` (1-100 chars), `archived`, `locked`, `auto_archive_duration` (60/1440/4320/10080), `slowmode_seconds` |
| `discord_thread_create` | write | open a public thread (`name`, optional `message` posted via webhook, optional `from_message_id`, optional `add_user_ids`) |
| `discord_thread_member_add` | write | add one user (`thread_id`, `user_id`) to the thread; read back, `member: true` |
| `discord_thread_member_remove` | write (destructive) | remove one user from the thread; read back, `member: false` |
| `discord_thread_members` | read | user ids in a thread |

They appear only when the connector has the Discord relay configured. Renaming an archived thread reopens it,
renames it, and archives it again. Errors come back as `{"error", "status"}`; nothing is retried silently. Discord rate-limits thread renames to
about 2 per 10 minutes per thread: a third rename returns status 429 with the wait time, and the name is unchanged.

## Life of a task
1. `messages_send` to `discord:<channel_id>` with the brief (first line = title), tagging the Sons. Or
   `discord_thread_create` with a `name` and the brief as `message`.
2. Every follow-up: `messages_send` to the `target` of that thread. Use `discord_threads_list` to find it again.
3. Done: `discord_thread_edit` with `name: "Closed: <title>"`, then `archived: true` if the owner wants it out of view.

## Members: the owner's rule (2026-10-13)
1. When the owner and the connector (Claude/ChatGPT) are discussing something and the owner asks for a thread for it,
   the connector adds the owner as part of creating it: `discord_thread_create` with `add_user_ids: ["<owner id>"]`.
2. Never create a thread on the connector's or a Son's own initiative. Only when the owner asks.
3. Never remove the owner from a thread automatically, not even when a task looks done. `discord_thread_member_remove`
   runs only when the owner explicitly asks to be removed. Add/remove of anyone else: also only on the owner's ask.

Mechanics: add is `PUT /channels/{thread}/thread-members/{user}`, remove is `DELETE` on the same path. Both are
idempotent (no write when already in/out: `changed: false`) and are read back before the tool returns
(`verified: true`). An archived, unlocked thread is reopened for the change; after a remove it is archived again,
after an add it stays open. Archived and locked: refused with 409 (unlock first).

Permissions the bot needs: add = Send Messages in Threads (and the added user must be able to see the parent
channel); remove = Manage Threads. Errors name the missing permission.
