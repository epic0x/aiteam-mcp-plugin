"""End-to-end over real sockets: MCP client -> connector (in-memory MCP) -> messaging box (HTTP) -> fake Hermes."""
import json
import socket

import pytest
from aiohttp import web
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

from aiteam_connector.box import Box, BoxConfig, Caller, SonTarget, session_id_for, sha
from aiteam_connector.bridge import HermesBridge, parse_sse
from aiteam_connector.github_gh import GITHUB_TOOLS
from aiteam_connector.server import Connector, SonLink, build_server

from .fake_hermes import KEY, FakeHermes

ALICE, BOB = "box_alice_token_xxxxxxxxxxxxxxxxxxxx", "box_bob_token_yyyyyyyyyyyyyyyyyyyyyy"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def start(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, f"http://127.0.0.1:{port}"


@pytest.fixture
async def stack():
    fake = FakeHermes()
    r1, hermes_url = await start(fake.app())
    cfg = BoxConfig("test-box", "Test box", {"my-son": SonTarget("my-son", hermes_url, "X")},
                    [Caller("alice", "Alice", sha(ALICE), ["my-son"]), Caller("bob", "Bob", sha(BOB), ["other"])])
    box = Box(cfg, bridges={"my-son": HermesBridge(hermes_url, KEY)})
    r2, box_url = await start(box.app())
    yield fake, box_url
    await r2.cleanup()
    await r1.cleanup()


def test_parse_sse():
    assert parse_sse(["event: assistant.delta", 'data: {"delta": "hi"}']) == ("assistant.delta", {"delta": "hi"})
    assert parse_sse([": keepalive"]) is None
    assert parse_sse(['data: {"event": "x"}']) == ("x", {"event": "x"})


def test_session_id_is_stable_and_scoped():
    a = session_id_for("box", "alice", "default")
    assert a == session_id_for("box", "alice", "default")
    assert a != session_id_for("box", "bob", "default") != session_id_for("box2", "alice", "default")
    assert a.startswith("box_") and len(a) == 28


async def test_box_auth_and_allowlist(stack):
    fake, box_url = stack
    import httpx
    async with httpx.AsyncClient() as c:
        assert (await c.get(f"{box_url}/v1/whoami")).status_code == 401
        assert (await c.get(f"{box_url}/v1/whoami", headers={"Authorization": "Bearer wrong"})).status_code == 401
        r = await c.post(f"{box_url}/v1/sons/my-son/messages", json={"text": "hi"}, headers={"Authorization": f"Bearer {BOB}"})
        assert r.status_code == 403
        r = await c.post(f"{box_url}/v1/sons/nope/messages", json={"text": "hi"}, headers={"Authorization": f"Bearer {ALICE}"})
        assert r.status_code == 404
        r = await c.post(f"{box_url}/v1/sons/my-son/messages", json={"text": "hi", "conversation": "../x"},
                         headers={"Authorization": f"Bearer {ALICE}"})
        assert r.status_code == 400
        w = (await c.get(f"{box_url}/v1/whoami", headers={"Authorization": f"Bearer {ALICE}"})).json()
        assert w["caller"]["id"] == "alice" and w["sons"] == ["my-son"]


async def test_box_relays_with_own_identity_and_memory(stack):
    fake, box_url = stack
    import httpx
    h = {"Authorization": f"Bearer {ALICE}"}
    async with httpx.AsyncClient() as c:
        r1 = (await c.post(f"{box_url}/v1/sons/my-son/messages", json={"text": "first"}, headers=h)).json()
        r2 = (await c.post(f"{box_url}/v1/sons/my-son/messages", json={"text": "second"}, headers=h)).json()
        assert r1["reply"] == "echo #1: first" and r2["reply"] == "echo #2: second", "same conversation = same session"
        assert r1["session_id"] == r2["session_id"] and r1["tools"] == ["terminal"]
        r3 = (await c.post(f"{box_url}/v1/sons/my-son/messages", json={"text": "x", "conversation": "other"}, headers=h)).json()
        assert r3["reply"] == "echo #1: x" and r3["session_id"] != r1["session_id"]
        # the Son sees the box identity, never the owner's
        assert fake.authors[0] == {"id": "box:test-box/alice", "name": "Alice", "is_bot": True}
        convs = (await c.get(f"{box_url}/v1/sons/my-son/conversations", headers=h)).json()["conversations"]
        assert sorted(x["conversation"] for x in convs) == ["default", "other"]
        hist = (await c.get(f"{box_url}/v1/sons/my-son/conversations/default/messages", headers=h)).json()
        assert [m["content"] for m in hist["messages"]][-1] == "echo #2: second"
        # SSE relay
        async with c.stream("POST", f"{box_url}/v1/sons/my-son/messages", json={"text": "s", "stream": True}, headers=h) as r:
            body = (await r.aread()).decode()
        assert "event: assistant.completed" in body and "event: done" in body


async def test_mcp_connector_end_to_end(stack):
    fake, box_url = stack
    conn = Connector([SonLink("my-son", box_url, ALICE)], passthrough="none")
    async with create_connected_server_and_client_session(build_server(conn)) as client:
        client: ClientSession
        tools = {t.name: t for t in (await client.list_tools()).tools}
        native = {"usage_guide", "son_send", "son_list", "son_conversations", "son_conversation_history",
                  "ping", "health", "cleanup", "son_model_status", "son_model_set_all",
                  "son_model_set_session", "son_model_fallback_set", "ai_status", "ai_signin_start", "ai_signin_status",
                  "ai_model_set", "ai_fallback_set"}
        assert set(tools) - native == set(GITHUB_TOOLS) and native <= set(tools)  # GitHub (gh) is on by default
        assert tools["son_send"].annotations.readOnlyHint is False and tools["son_list"].annotations.readOnlyHint is True
        assert all(t.title for t in tools.values()), "store review needs titles"
        r = await client.call_tool("son_send", {"message": "hello from Claude", "conversation": "c1"})
        assert not r.isError and r.content[0].text.startswith("echo #1: hello from Claude")
        r = await client.call_tool("son_send", {"message": "again", "conversation": "c1"})
        assert r.content[0].text.startswith("echo #2: again")
        r = await client.call_tool("son_list", {})
        assert json.loads(r.content[0].text)["sons"][0]["online"] is True
        r = await client.call_tool("son_conversation_history", {"conversation": "c1", "limit": 2})
        assert json.loads(r.content[0].text)["messages"][-1]["content"] == "echo #2: again"
        r = await client.call_tool("son_send", {"message": "x", "son": "ghost"})
        assert r.isError


async def test_connector_reports_box_refusal(stack):
    fake, box_url = stack
    conn = Connector([SonLink("my-son", box_url, BOB)], passthrough="none")
    async with create_connected_server_and_client_session(build_server(conn)) as client:
        r = await client.call_tool("son_send", {"message": "hi"})
        assert r.isError and "403" in r.content[0].text


def test_full_surface_is_default_and_annotated():
    import mcp.types as types
    from aiteam_connector.server import Connector, annotate_upstream
    assert Connector([SonLink("my-son", "http://x", "t")]).passthrough == "all"
    t = types.Tool(name="messages_send", description="Send.\n\nlong", inputSchema={"type": "object"})
    a = annotate_upstream(t, ["my-son"])
    assert a.annotations.readOnlyHint is False and a.annotations.openWorldHint is True
    assert a.description == "Send.\n\nlong"  # full description kept: Hermes tool docs carry the usage rules
    r = annotate_upstream(types.Tool(name="messages_read", inputSchema={"type": "object"}), ["my-son", "x"])
    assert r.annotations.readOnlyHint is True and "son" in r.inputSchema["properties"]


def test_session_title_capped_for_hermes():
    """Hermes refuses titles > 100 chars with a 500 (live 2026-10-13: 'Title too long (104 chars, max 100)' broke
    son_send for a long caller name + conversation id)."""
    from aiteam_connector.bridge import TITLE_MAX, fit_title
    long = "Box son-a: Owner connector (Claude/ChatGPT via aiteam.space sign-in) / " + "c" * 64 + " [abc123]"
    out = fit_title(long)
    assert len(out) <= TITLE_MAX and out.endswith(" [abc123]") and "\u2026" in out
    assert fit_title("  short   title ") == "short title"
    assert len(fit_title("x" * 300)) == TITLE_MAX


def test_upstream_annotations_from_the_son_toolbox_are_kept():
    """The Son's hermes_toolbox annotates every Hermes tool (terminal = destructive, read_file = read-only)."""
    import mcp.types as types
    from aiteam_connector.server import annotate_upstream
    t = types.Tool(name="terminal", inputSchema={"type": "object"},
                   annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
    a = annotate_upstream(t, ["my-son"])
    assert a.annotations.destructiveHint is True and a.annotations.readOnlyHint is False
    r = annotate_upstream(types.Tool(name="read_file", inputSchema={"type": "object"},
                                     annotations=types.ToolAnnotations(readOnlyHint=True)), ["my-son"])
    assert r.annotations.readOnlyHint is True
