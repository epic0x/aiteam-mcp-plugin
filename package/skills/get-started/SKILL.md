---
name: get-started
description: Set up aiteam.space in ChatGPT. Use when the user says "help me set up aiteam". Connect the Son's brain, then Telegram or Google Meet, then a first task.
---

# Get started with aiteam.space

Your Son is an AI teammate that runs on its own computer. It keeps working after this chat ends, so use it for
work that needs a real machine: long tasks, scheduled jobs, posts to your team chat, PDFs and deploys.

When the user says "help me set up aiteam" (or "set up my Son", "get me started"), walk them through these steps.
One step at a time: ask, wait for the answer, act, confirm, then move on. Keep each message short.

## 0. Sign in and pick a Son
- If the plugin is not connected yet, ask the user to connect it and sign in with their aiteam.space account, then
  approve access for ChatGPT. Nothing else needs to be installed.
- Call `son_list`. No Son yet: send the user to https://aiteam.space/new to create one, then stop until they are
  back. Help: https://aiteam.space/help/connect. Support: https://aiteam.space/support.
- More than one Son: ask which one, and use that name in every later call. None online: say so and try again in
  a minute. Do not send work to an offline Son.

## 1. The brain
- Call `ai_status`. If no AI is signed in, say: "Your Son is live but has no brain yet. Connect a Claude
  subscription, ChatGPT, or an API key? You can add more than one account."
- Start the sign-in with `ai_signin_start` (`claude`, `chatgpt`, `anthropic-key` or `openai-key`). Give the user
  the links exactly as returned. The aiteam.space link works once and expires in 15 minutes.
- Never ask for a code, a key or a password in this chat, and never accept one here. It goes on the aiteam.space
  link only.
- When they say they are done, check with `ai_signin_status`.
- More accounts are fine: run `ai_signin_start` again for each one, then set the order with `ai_fallback_set`.

## 2. Where to reach the Son
- Ask: "Want Telegram? Google Meet?"
- Telegram is free: they make a bot with BotFather and paste its token on the Son's page on aiteam.space.
- Google Meet is the Calls add-on and needs a Google Workspace account with SSO. Discord voice needs no setup.
- Skipping is fine. Everything can be added later from https://aiteam.space/my-sons.

## 3. First task
- Suggest one small, safe task, for example "What can you do for me?" or "Post a short hello to our team chat."
- Send it with `son_send`. Keep the user's wording. Use a short conversation name (like `getting-started`) so it
  can continue later.
- A reply can take a few minutes. Tell the user the Son is working.

## 4. Check status
- To see what the Son said before, call `son_conversations`, then `son_conversation_history` for the one the user
  wants.
- To ask for a fresh update, send "What's the status?" with `son_send` in the same conversation.

## Limits
- Only Sons on the signed-in account are reachable. Never try to reach someone else's Son.
- Do not ask the Son to make payments or move money.
- Report what the Son actually replied. If a call fails, say so plainly.
