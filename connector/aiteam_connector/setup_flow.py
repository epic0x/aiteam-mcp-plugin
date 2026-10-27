"""Guided first run: what Claude or ChatGPT does when a buyer says "help me set up aiteam".

One source, served three ways so every client gets it:
  * MCP prompt ``setup_aiteam`` (prompts/list + prompts/get; Claude shows it in the prompt picker as
    "Help me set up aiteam"). Getting it starts the walk-through.
  * a trigger sentence inside the server ``instructions`` (``SETUP_TRIGGER``, see usage_guide.INSTRUCTIONS), so
    a client that never opens the prompt picker still runs the same steps when the user asks in plain words.
  * section 10 of the usage guide (``usage_guide`` tool) and of the generated ``aiteam-mcp`` skill.

The same steps run in the aiteam.space setup chat when the buyer skips connecting the MCP (site repo).
Tool names used here must exist on the connector; tests/test_setup_flow.py checks them.
"""
from __future__ import annotations

import mcp.types as types

SETUP_PROMPT = "setup_aiteam"
SITE = "https://aiteam.space"
SIGNUP_URL = f"{SITE}/new"
SUPPORT_URL = f"{SITE}/support"
CONNECT_HELP_URL = f"{SITE}/help/connect"
DASHBOARD_URL = f"{SITE}/my-sons"

# The exact lines the client says at each step (short, plain, read-aloud friendly for voice mode).
ASK_BRAIN = ("Your Son is live but has no brain yet. Connect a Claude subscription, ChatGPT, or an API key? "
             "You can add more than one account.")
ASK_CHANNELS = "Want Telegram? Google Meet?"

SETUP_TRIGGER = (
    "GUIDED SETUP: when the user says 'help me set up aiteam' (or 'set up my Son', 'get me started', 'onboard me'), "
    "run the setup_aiteam prompt's steps one at a time, waiting for the user after each: (0) son_list; no Son = "
    f"send them to {SIGNUP_URL} to create one, then stop. (1) ai_status; if no AI is signed in say: '{ASK_BRAIN}' "
    "then ai_signin_start with their pick and hand over the one-time aiteam.space link exactly as returned; never "
    "take a code or key in the chat; confirm with ai_signin_status; offer another account and ai_fallback_set. "
    f"(2) Ask: '{ASK_CHANNELS}' Telegram is free (BotFather token, added on the Son's page on aiteam.space); "
    "Google Meet is the Calls add-on and needs a Google Workspace account with SSO; Discord voice needs no setup. "
    "(3) Suggest one small first task. "
)

SETUP_GUIDE = f"""
## 10. Guided first run: "help me set up aiteam"
When the user says "help me set up aiteam" (or "set up my Son", "get me started"), walk them through these steps.
One step at a time. Ask, wait for the answer, act, confirm, then move on. Keep each message short: the user may be
in voice mode. The prompt `setup_aiteam` starts the same walk-through.

**Step 0. Is there a Son?** Call `son_list`.
- No Son, or not signed in: send them to {SIGNUP_URL} (sign in with Google, create a Son), then stop until they
  are back. Help: {CONNECT_HELP_URL}. Support: {SUPPORT_URL}.
- More than one Son: ask which one, and use that name in every later call.
- A Son that is still starting: say it is coming online and try `ping` again in a minute.

**Step 1. The brain.** Call `ai_status`.
- If no AI is signed in, say: "{ASK_BRAIN}"
- Their pick goes to `ai_signin_start`: `claude` (Claude Pro or Max), `chatgpt` (ChatGPT Plus or Pro),
  `anthropic-key` or `openai-key` (an API key). Pass a short `label` when they name the account (for example
  "work").
- Give them the links exactly as returned. Claude: sign in, then paste the code Claude shows on the one-time
  aiteam.space link. ChatGPT: open the link and type the short code; nothing to paste. API key: paste it on the
  aiteam.space link. The link works once and expires in 15 minutes.
- Never ask for a code, a key or a password in the chat, and never accept one here. If they paste one anyway, tell
  them to use the link instead and to rotate that key.
- When they say they are done, check with `ai_signin_status`. Expired or failed: start again.
- Multiple accounts are fine: each `ai_signin_start` adds one more account (another Claude login, ChatGPT, a key).
  Then offer a fallback order with `ai_fallback_set` (for example Claude first, ChatGPT next) and pick the main
  model with `ai_model_set` if they want a different one.
- Already signed in: read the first line of `ai_status` out as is and go to step 2.

**Step 2. Where to reach the Son.** Ask: "{ASK_CHANNELS}"
- Telegram: free. They make a bot with BotFather and paste its token on the Son's page on aiteam.space
  ({DASHBOARD_URL}), never in this chat. Then the Son answers in Telegram, including groups with topics.
- Google Meet: the Calls add-on. It needs a Google Workspace account with SSO (a personal Gmail does not work).
  They add it from the Son's page. Discord voice works with no extra setup.
- Discord: plain chat is free; thread management (two-way sync) is an add-on.
- Skipping is fine. Everything can be added later from the Son's page.

**Step 3. First task.** Suggest one small, safe task (for example "What can you do for me?" or "Post a hello in
our team chat"), send it the way section 1 or 3 says, and report the reply. Close with: you can change the model,
the accounts, the fallback order and the channels at any time, from this chat or from aiteam.space.
"""

PROMPT_TEXT = (
    "Help me set up aiteam. Walk me through it one step at a time, and wait for my answer after each question.\n"
    + SETUP_GUIDE
    + "\nStart with step 0 now."
)


def setup_prompt() -> types.Prompt:
    return types.Prompt(
        name=SETUP_PROMPT, title="Help me set up aiteam",
        description="Guided first run: connect your Son's brain (Claude, ChatGPT or an API key; several accounts "
                    "allowed), then Telegram and Google Meet, then a first task.",
        arguments=[])


def setup_prompt_result() -> types.GetPromptResult:
    return types.GetPromptResult(
        description="Help me set up aiteam",
        messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=PROMPT_TEXT))])
