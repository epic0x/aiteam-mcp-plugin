"""What Claude Code (and other clients) read from tools/list: hints, description and instruction limits, result sizes."""
import asyncio

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from aiteam_connector.server import (BIG_RESULTS, MAX_DESCRIPTION, Connector, SonLink, build_server, for_clients)
from aiteam_connector.usage_guide import GUIDE, INSTRUCTIONS


def _tools() -> list[types.Tool]:
    async def go():
        conn = Connector([SonLink("my-son", "http://127.0.0.1:9", "t")], passthrough="none")
        async with create_connected_server_and_client_session(build_server(conn)) as c:
            return (await c.list_tools()).tools
    return asyncio.run(go())


def test_instructions_fit_claude_code_limit():
    assert len(INSTRUCTIONS) <= 4096, len(INSTRUCTIONS)


def test_every_tool_has_hints_and_read_only_tools_say_so_fully():
    tools = _tools()
    assert tools
    for t in tools:
        a = t.annotations
        assert a is not None and a.readOnlyHint is not None, t.name
        if a.readOnlyHint:
            assert a.destructiveHint is False and a.idempotentHint is True, t.name
        assert len(t.description or "") <= MAX_DESCRIPTION, t.name


def test_writes_keep_their_own_hints():
    w = types.Tool(name="son_send", inputSchema={"type": "object"},
                   annotations=types.ToolAnnotations(readOnlyHint=False))
    out = for_clients(w)
    assert out.annotations.destructiveHint is None and out.annotations.idempotentHint is None


def test_long_description_is_cut_to_the_limit():
    t = types.Tool(name="x", description="word " * 2000, inputSchema={"type": "object"})
    d = for_clients(t).description
    assert len(d) == MAX_DESCRIPTION and d.endswith("...")


def test_big_readers_raise_result_size_within_ceiling():
    tools = {t.name: t for t in _tools()}
    assert "usage_guide" in tools
    for name, size in BIG_RESULTS.items():
        assert size <= 500_000
        if name in tools:
            assert (tools[name].meta or {}).get("anthropic/maxResultSizeChars") == size, name
    assert len(GUIDE) < BIG_RESULTS["usage_guide"]


def test_guide_explains_code_mode():
    sec = GUIDE[GUIDE.index("## Code Mode"):GUIDE.index("## Public repos")]
    for s in ("CLAUDE_CODE_EXPERIMENTAL_CODE_MODE", "v2.1.323", "`execute`", "read-only", "permission rules",
              "anthropic/maxResultSizeChars", "Claude.ai, Claude Desktop, ChatGPT"):
        assert s in sec, s
    assert "CODE MODE" in INSTRUCTIONS
