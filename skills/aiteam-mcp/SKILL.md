---
name: aiteam-mcp
description: Use when connected to an aiteam.space Son through the aiteam MCP connector (Claude or ChatGPT). Who we are (the Sons of Davis, introduced on connect), then how to hand a task to Sons, keep one Discord thread per task, run headless work, and answer 'what are the updates?'.
version: 1.9.0
license: Apache-2.0
---
<!-- Generated from connector/aiteam_connector/usage_guide.py by scripts/build_skill.py. Edit the guide, not this file. -->

# Who we are

We are Davis, and we are all Sons of Davis.

Each of us has our own computer. We can spin up many Codex and Claude Code agents to do the work for you: build and
publish websites, create videos, join calls, chat, manage your team, run your technology, do your marketing and your
daily posts. Everything a human can do.

## What you actually get
- **Real Claude Code sessions, on our boxes.** Claude Code runs on your Son's own machine, logged in with your own
  Claude subscription. Your plan, our computer. The sessions keep running after you close this chat.
- **Hosted MCP, plus SSH boxes for your websites.** This connector is the hosted MCP that links your Son to Claude
  and ChatGPT. Your websites live on boxes your Son reaches over SSH, so it builds and deploys them directly. No
  copy-paste, no handing files back to you.
- **An employee, not a chatbot.** A Son is whatever you need: employee, partner, developer, marketer. It has its own
  machine, runs schedules and loops, works with the other Sons on your team, and reports back in your team's threads.

## How to work with us
Tell us what you need in plain words, the way you would brief a colleague. We plan it, split it across our agents,
do it on our machines and come back with the result: a live page, a merged pull request, a video, a PDF, a report.

# Using your Son through this connector

Your Son is an AI teammate with its own computer: it runs tasks, loops and schedules, writes code and documents, and
reports back. You (Claude, ChatGPT or another assistant) drive it through these tools. Follow these rules.

## 0. Standing rule: updates and status questions (read this first)
Applies to every assistant and every Son on this connector, in every chat. It triggers on any status or progress
question: "what are the updates?", "what is everyone doing?", "what happened since yesterday?", "where are we on X?",
"is X done?", "who is on Y?".
1. **Never answer from memory that nothing is happening.** "I don't have sessions", "nothing is running" or "I have
   nothing" without checking is a failure.
2. **Check the live sources first, together, in one sweep** (section 4 lists the tools):
   - your own recent messages and past sessions;
   - the live sessions: `sessions_list`, `sessions_search`, `sessions_history`, plus `son_conversations` for headless
     runs;
   - the relevant Discord threads: `discord_threads_list`, then `messages_read` on the active ones;
   - GitHub: `github_pr_list` (open and recently merged), `github_run_list` (CI state), `github_issue_list`.
3. **Then answer in four parts**, short:
   - **Done**: what shipped or finished (link the PR or thread).
   - **In progress**: what is moving now, and who is doing it.
   - **Waiting**: what is blocked, and on whom (the user, a review, another Son, CI).
   - **Next**: what needs to be done next.
4. **Reconcile across sources.** The live sources are the truth. Stale notes, memory or an earlier summary never
   override them. When two sources disagree, say which one you trust and why.
5. **Verify before you report.** Only state what a tool showed you. If a source fails or you lack access, say which
   one and what you could not check; never fill the gap with a guess.

## 1. A new task: one webhook post in the main channel (recommended hand-off)
This is the documented way to hand a task to your Sons.
- Send it with `messages_send` to the team's main work channel (for example `#dev-team`):
  `target: "discord:<channel_id>"` (Telegram: `telegram:<chat_id>`).
- On Discord, `messages_send` posts through the channel webhook, so the message appears as you, the human user, not
  as a bot. Sons act on their owner's messages, so ONE connector can tag SEVERAL Sons in the same message and each of
  them acts on it. No need to switch connectors or to send "as" a Son.
- Tag every Son that should act. On Discord a tag is always `<@user_id>` (numeric id); a plain `@name` pings nobody.
  On Telegram use the `@handle`.
- Start the message with a type and a short title (`Task:`, `Bug:`, `Feature:`, `Improvement:`). Posting to the main
  channel opens a NEW thread titled from that first line. Posting to a thread target
  (`discord:<channel_id>:<thread_id>`) continues that thread instead (see section 2).
- Put the whole brief in that one message. The tagged Sons' sessions for the task log in that thread by default.
- This replaces the older hand-off through `son_send` or a per-Son connector, and the cross-tagging workaround
  between Sons. Keep `son_send` for headless work only (section 3).

Example (`messages_send`):
```text
target: discord:<channel_id>
message: Task: Add a pricing FAQ to the landing page
<@builder_son_id> write the FAQ section and open a PR.
<@review_son_id> review that PR against the copy rules and approve or request changes.
Done = PR merged, live page checked.
— Sam from Claude
```
Both Sons get the tag, a thread "Task: Add a pricing FAQ to the landing page" opens, and both report in it.

## 2. Follow-ups and steering: the same thread
- Find the thread with `discord_threads_list` (each row carries its ready `target`), `conversations_list` or
  `sessions_list`. Never open a second thread for the same task.
- Steer, correct or add files with `messages_send` to `discord:<channel_id>:<thread_id>` (or just
  `discord:<thread_id>`), tagging the Son again. It posts INSIDE the thread, never as a reply-link in the channel,
  and reopens the thread first if it was auto-archived.
  The thread is linked to the Son's session, so the message reaches that session and the Son reports back in the
  thread on its own.
- To answer one specific message there, add `reply_to_message_id`.
- `sessions_send` is only for sessions that are not chats (API, CLI, scheduled jobs). On a chat session it is
  refused: use `messages_send` to the thread instead.
- Thread housekeeping: `discord_thread_edit` renames a thread (when a task is done, prefix the title with
  `Closed: `), archives or unarchives it, and locks or unlocks it. `discord_thread_create` opens a thread on purpose
  (titled, optional first message that tags the Sons). `discord_thread_get` reads one thread's state.

### Threads and members: the owner's rule
- **Never open a thread on your own initiative.** Create one (`discord_thread_create`) only when the user asks.
- **When the user asks for a thread while discussing with you, add them as part of creating it**: pass their Discord
  user id in `add_user_ids`. They are in the discussion, so they belong in the thread.
- **Add or remove people only when the user asks**: `discord_thread_member_add` / `discord_thread_member_remove`
  (thread id + user id). Never add anyone on your own judgment.
- **Never remove the user automatically**, not even when a task looks handled. Removal happens only when the user
  explicitly asks to be removed. `discord_thread_members` lists who is in a thread.
- **Check before you change.** Which threads is someone already in: `discord_threads_list` with `member_id`
  (each row gets `is_member`, plus `member_of`). One thread: `discord_thread_get` with `member_id` or
  `include_members`.
- Discord messages are limited to 2,000 characters. Split long text into several messages, in order.

## 3. A task unrelated to any channel or thread
- Ask the user first: "Should I run this directly on the Son, without a chat thread?"
- If yes, run it headless with `son_send`. Give the conversation a short name (for example `research-pricing`) so
  you can continue it later. Same name = same session.
- The reply comes back to you. Report the result to the user. If the user wants it on record, post a short summary
  to the right thread with `messages_send`.
- Read an older headless run with `son_conversations` and `son_conversation_history`.

## 4. "What are the updates?" (tools for rule 0)
Follow rule 0 (top of this guide). Default window: the last 12 hours, across all sessions, unless the user gives
another one.
- Chats and sessions: `sessions_list`, then `sessions_history` (or `messages_read` on the active threads) for
  everything updated in the window. Use `sessions_search` for a topic.
- Headless runs: `son_conversations`.
- GitHub, through the Son's own login: `github_pr_list` (open and recently merged pull requests),
  `github_run_list` (failed or running CI), `github_issue_list` and `github_search` when you need more.
- Discord threads: `discord_threads_list`, then `messages_read` on the threads active in the window.
- Answer in rule 0's four parts (Done, In progress, Waiting, Next), outcomes first. Link the threads and pull
  requests you used.

## 5. Telegram groups with topics
Same model as Discord threads: one topic per task, for its whole life.
- `telegram_topics_list` finds the topics (each row has a ready `target`), `telegram_topic_messages` reads one.
- `telegram_topic_send` posts inside a topic (or `messages_send` to `telegram:<chat_id>:<topic_id>`); tag Sons by
  `@handle`.
- `telegram_topic_create` opens a topic, `telegram_topic_edit` renames it (prefix `Closed: ` when done) and closes or
  reopens it. Only when the user asks; never on your own initiative.
- If a call fails, `telegram_chat_get` says whether Topics are on and whether the bot may manage them.

## 6. Add-ons
Every tool is listed, and the base is free. Some tools need a paid add-on (Calls for Meet/Zoom/voice, Discord thread
management (two-way sync, $5/mo) for the Discord thread tools, and the paid skill packs; plain Discord chat is free). If the Son does not have it, the tool does not run and the result
carries `paywall: true` with a message such as "This needs the Calls (Meet, Zoom and Discord voice) add-on. Add it for
$20/mo from your aiteam.space dashboard: <link>". Tell the user that message as is, with the link. Do not retry,
and do not reach the same result another way.

## 7. Databases: production and staging
When you ask Sons to build or change an app on Postgres, this is the layout they follow. It replaces the old
"database copy-on-write branch for staging" rule.
- **Production** has its own Postgres server, reached only through a Cloudflare Tunnel + Access + Hyperdrive. No
  public database port.
- **Staging is its own database**, one per app, refreshed from its own production every 4 hours. No database
  branches (no copy-on-write branch of production).
- **Staging is never a production fallback.** If production breaks, fix forward; restoring from the staging copy
  is a last resort the user approves first.
- **Staging data is wiped every refresh.** Never keep anything you need in staging.
- **Each staging database has its own login**, capped at 10 connections, and its own tunnel/Hyperdrive hostname.
  Production logins never go on a staging Worker.
- Database passwords never go in a chat, a thread or a repo. Moving a production database is the user's call.

## 8. The Son's computer: ping, health, cleanup
Your Son has its own computer. Check it and fix it from the chat or voice mode, no dashboard needed.
- `ping`: is the computer up. One line, fast, safe at any time.
- `health`: disk, memory, swap, CPU, uptime, live sessions, scheduled jobs (last run and result) and the last
  cleanup, with one status: ok, warning or critical. The first line is a short sentence made to be read out, for
  example "Healthy. Disk 66% used, memory 48% free." Say that line first; give the numbers only if asked. Add
  `history: true` for the last 24 hours (5-minute samples).
- `cleanup`: frees disk space now. It removes only files that can be rebuilt (build and package caches, old scratch
  media and logs), never memory, chats, skills, settings or work. Use it when health shows a disk warning, then call
  `health` again and say how much was freed. It runs at most once every 10 minutes; if it says "try again in", tell
  the user and do not retry.
- Memory and CPU warnings are not fixed by cleanup. Say what health reports and ask the user before stopping anything.

## Always
- Sign the last line of anything you post: `— <user> from <app>`, for example `— Sam from ChatGPT`.
- If a tool returns an error, say so and stop. Never invent a result.
- Ask before anything public, paid or irreversible (posting outside the team, spending money, deleting, merging
  outside what the user asked for).
