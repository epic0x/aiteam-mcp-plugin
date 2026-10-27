"""Guided first run ("help me set up aiteam"): MCP prompt + server instructions + usage guide section 10."""
import re

from mcp.shared.memory import create_connected_server_and_client_session

from aiteam_connector.server import AI_TOOLS, Connector, SonLink, build_server, native_tools
from aiteam_connector.setup_flow import (ASK_BRAIN, ASK_CHANNELS, PROMPT_TEXT, SETUP_GUIDE, SETUP_PROMPT,
                                         SETUP_TRIGGER, SIGNUP_URL)
from aiteam_connector.usage_guide import GUIDE, INSTRUCTIONS

from .test_connector import stack  # noqa: F401  (fixture)


async def test_setup_prompt_is_served(stack):  # noqa: F811
    _, box_url = stack
    conn = Connector([SonLink("my-son", box_url, "alice-token")], passthrough="none")
    async with create_connected_server_and_client_session(build_server(conn)) as client:
        prompts = {p.name: p for p in (await client.list_prompts()).prompts}
        assert SETUP_PROMPT in prompts and prompts[SETUP_PROMPT].title == "Help me set up aiteam"
        got = await client.get_prompt(SETUP_PROMPT)
        text = got.messages[0].content.text
        assert got.messages[0].role == "user" and text == PROMPT_TEXT
        assert text.startswith("Help me set up aiteam")


def test_steps_are_in_order_with_the_exact_lines():
    order = [SETUP_GUIDE.index(s) for s in ("**Step 0.", "**Step 1.", "**Step 2.", "**Step 3.")]
    assert order == sorted(order)
    assert ASK_BRAIN.startswith("Your Son is live but has no brain yet.")
    for word in ("Claude", "ChatGPT", "API key", "more than one account"):
        assert word in ASK_BRAIN
    assert ASK_CHANNELS == "Want Telegram? Google Meet?"
    assert SETUP_GUIDE.index(ASK_BRAIN) < SETUP_GUIDE.index(ASK_CHANNELS)
    low = SETUP_GUIDE.lower()
    assert "google workspace account with sso" in low and "one-time" in low and "multiple accounts" in low


def test_instructions_trigger_the_walkthrough():
    assert SETUP_TRIGGER in INSTRUCTIONS and "help me set up aiteam" in INSTRUCTIONS
    assert ASK_BRAIN in INSTRUCTIONS and ASK_CHANNELS in INSTRUCTIONS and SIGNUP_URL in INSTRUCTIONS
    assert "## 10. Guided first run" in GUIDE and GUIDE.index("## 9.") < GUIDE.index("## 10.") < GUIDE.index("## Always")


def test_never_takes_secrets_in_chat():
    for s in (SETUP_GUIDE, SETUP_TRIGGER):
        low = s.lower()
        assert "never" in low and ("in the chat" in low)


def test_setup_names_only_real_tools():
    known = {t.name for t in native_tools(["my-son"])} | set(AI_TOOLS) | {"son_list", "ping", "usage_guide"}
    named = set(re.findall(r"`([a-z]+_[a-z_]+)`", SETUP_GUIDE)) - {"anthropic-key", "openai-key", "setup_aiteam"}
    named |= set(re.findall(r"\b((?:ai|son)_[a-z_]+)\b", SETUP_TRIGGER))
    assert named <= known, sorted(named - known)


def test_no_framework_name_or_em_dash():
    for s in (SETUP_GUIDE, SETUP_TRIGGER, PROMPT_TEXT):
        assert not re.search(r"hermes", s, re.I) and "\u2014" not in s
