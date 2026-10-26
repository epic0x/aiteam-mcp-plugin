---
name: get-started
description: Set up aiteam.space in ChatGPT. Sign in, pick your Son, hand it a first task and check its status.
---

# Get started with aiteam.space

Your Son is an AI teammate that runs on its own computer. It keeps working after this chat ends, so use it for
work that needs a real machine: long tasks, scheduled jobs, posts to your team chat, PDFs and deploys.

Walk the user through these steps, one at a time.

## 1. Sign in
- If the plugin is not connected yet, ask the user to connect it and sign in with their aiteam.space account.
- After sign-in they approve access for ChatGPT. Nothing else needs to be installed.

## 2. Pick a Son
- Call `son_list` to see the Sons on the account and which are online.
- If there is more than one, ask the user which Son to use. Use that name in later calls.
- If none is online, say so and suggest trying again in a minute. Do not send work to an offline Son.

## 3. Hand it a first task
- Suggest one small, safe task, for example: "What are you working on right now?" or
  "Post a short hello to our team chat."
- Send it with `son_send`. Keep the user's wording. Use a short conversation name (like `getting-started`) so the
  thread can continue later.
- A reply can take a few minutes. Tell the user the Son is working.

## 4. Check status
- To see what the Son said before, call `son_conversations`, then `son_conversation_history` for the one the user
  wants.
- To ask for a fresh update, send "What's the status?" with `son_send` in the same conversation.

## Limits
- Only Sons on the signed-in account are reachable. Never try to reach someone else's Son.
- Do not ask the Son to make payments or move money.
- Report what the Son actually replied. If a call fails, say so plainly.
