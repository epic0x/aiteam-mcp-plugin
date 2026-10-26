"""Who we are: the first thing anyone meets when they connect to a Son (or to the aiteam.space relay).

One source of truth for the introduction, served four ways so every client sees it on connect:
  * MCP prompt   ``who_we_are``        (prompts/list + prompts/get; Claude shows it in the prompt picker)
  * MCP resource ``aiteam://who-we-are`` (resources/list + resources/read, text/markdown)
  * the opening of the server ``instructions`` (``INTRO_SHORT``, see usage_guide.INSTRUCTIONS)
  * the opening of the full usage guide (``usage_guide`` tool) and of the generated ``aiteam-mcp`` skill

Voice: "we / us" (the Sons). Customer copy: say what the customer gets (Son of Davis, real Claude Code sessions,
our own boxes, deploys). Never name the agent framework underneath; tests/test_intro.py enforces that.
"""
from __future__ import annotations

import mcp.types as types

INTRO_PROMPT = "who_we_are"
INTRO_URI = "aiteam://who-we-are"

INTRO_SHORT = (
    "We are Davis, and we are all Sons of Davis. Each of us has our own computer, and we can spin up many Codex and "
    "Claude Code agents to do the work for you: build and publish websites, create videos, join calls, chat, manage "
    "your team, run your technology, do your marketing and your daily posts. Everything a human can do. You get real "
    "Claude Code sessions on our boxes, logged in with your own subscription, plus hosted MCP and SSH boxes for your "
    "websites that you deploy straight from your Son. A Son is an employee, partner or developer with its own "
    "machine, not a chatbot."
)

INTRO = """# Who we are

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
"""


def intro_prompt() -> types.Prompt:
    return types.Prompt(
        name=INTRO_PROMPT, title="Who we are: Sons of Davis",
        description="Start here. How your Sons introduce themselves: who we are, what we do, and what you get "
                    "(real Claude Code sessions, our own boxes, hosted MCP and SSH deploys).",
        arguments=[])


def intro_prompt_result() -> types.GetPromptResult:
    return types.GetPromptResult(
        description="Who we are: Sons of Davis",
        messages=[types.PromptMessage(role="user", content=types.TextContent(
            type="text",
            text=INTRO + "\nIntroduce yourselves to me in this voice (\"we\", the Sons), in a few short lines, then "
                         "ask what I need done first."))])


def intro_resource() -> types.Resource:
    return types.Resource(
        uri=INTRO_URI, name="who-we-are", title="Who we are: Sons of Davis",
        description="The Sons' introduction: who we are, what we do, what you get.",
        mimeType="text/markdown")
