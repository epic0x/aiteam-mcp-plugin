"""Who we are: served as an MCP prompt + resource, and as the opening of instructions, guide and skill."""
import pathlib
import re

from mcp.shared.memory import create_connected_server_and_client_session

from aiteam_connector.intro import INTRO, INTRO_PROMPT, INTRO_SHORT, INTRO_URI
from aiteam_connector.server import Connector, SonLink, build_server
from aiteam_connector.usage_guide import GUIDE, INSTRUCTIONS

from .test_connector import stack  # noqa: F401  (fixture)

ROOT = pathlib.Path(__file__).resolve().parents[2]
OPENING = "We are Davis, and we are all Sons of Davis."


async def test_intro_served_as_prompt_and_resource(stack):  # noqa: F811
    _, box_url = stack
    conn = Connector([SonLink("my-son", box_url, "alice-token")], passthrough="none")
    async with create_connected_server_and_client_session(build_server(conn)) as client:
        prompts = (await client.list_prompts()).prompts
        assert [p.name for p in prompts][0] == INTRO_PROMPT  # who_we_are first, then setup_aiteam
        got = await client.get_prompt(INTRO_PROMPT)
        assert OPENING in got.messages[0].content.text
        res = (await client.list_resources()).resources
        assert [str(r.uri) for r in res] == [INTRO_URI] and res[0].mimeType == "text/markdown"
        read = await client.read_resource(INTRO_URI)
        assert read.contents[0].text == INTRO and read.contents[0].mimeType == "text/markdown"
        guide = await client.call_tool("usage_guide", {})
        assert guide.content[0].text.startswith("# Who we are")


def test_intro_opens_instructions_guide_and_skill():
    assert INSTRUCTIONS.startswith(OPENING) and INTRO_SHORT in INSTRUCTIONS
    assert "who_we_are" in INSTRUCTIONS and INTRO_URI in INSTRUCTIONS and "usage_guide" in INSTRUCTIONS
    assert GUIDE.startswith(INTRO)
    skill = (ROOT / "skills" / "aiteam-mcp" / "SKILL.md").read_text()
    body = skill.split("-->", 1)[1].lstrip()
    assert body.startswith("# Who we are\n\n" + OPENING)


def test_intro_says_what_the_customer_gets():
    for s in (INTRO, INTRO_SHORT):
        low = s.lower()
        assert "own computer" in low and "Sons of Davis" in s
        assert "claude code" in low and "own" in low and "subscription" in low     # real sessions, own plan
        assert "mcp" in low and "ssh" in low and "deploy" in low                   # hosted MCP + SSH boxes
        assert "not a chatbot" in low and "employee" in low and "partner" in low and "developer" in low
        for job in ("websites", "videos", "calls", "team", "marketing", "daily posts"):
            assert job in low, job


def test_intro_never_names_the_framework_or_uses_em_dashes():
    for s in (INTRO, INTRO_SHORT, INSTRUCTIONS, GUIDE):
        assert not re.search(r"hermes", s, re.I)
        assert "\u2014" not in s.replace("\u2014 <user> from <app>", "").replace("\u2014 Sam from", "")
