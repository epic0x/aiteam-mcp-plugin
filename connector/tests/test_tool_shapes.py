"""Every connector tool fits what clients that run their own Code Mode accept, and every result can be read by code.

Runs the full tool list (every optional surface on) through the checks clients apply: JSON Schema draft 2020-12,
no anyOf / oneOf / allOf at the root, property names 1-64 of [A-Za-z0-9_.-], client-side tool name within 128
characters, descriptions under 4,096 characters. Then the hints: read-only implies idempotent, writes are neither,
big readers raise the inline result size, owner controls and paid tools need a person on every call.
"""
import json
import re

import httpx
import mcp.types as types
import pytest
from jsonschema import Draft202012Validator
from mcp.shared.memory import create_connected_server_and_client_session

from aiteam_connector.discord_relay import DiscordRelay
from aiteam_connector.entitlements import TOOL_ADDON
from aiteam_connector.server import Connector, SonLink, build_server
from aiteam_connector.team_board import TeamBoard
from aiteam_connector.telegram_control import TelegramControl
from aiteam_connector.tool_meta import BIG_RESULT, OWNER_CONFIRM, finish_tool, structured

PROP = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SERVER_PREFIX = "mcp__aiteam-space__"
NO_HTTP = httpx.MockTransport(lambda r: httpx.Response(503))


def full_connector(tmp_path) -> Connector:
    sons = [SonLink("my-son", "http://box.invalid", "t"), SonLink("other-son", "http://box2.invalid", "t2")]
    return Connector(sons, passthrough="none", transport=NO_HTTP,
                     discord=DiscordRelay("bot", http=httpx.AsyncClient(transport=NO_HTTP)),
                     telegram=TelegramControl("123:abc", registry=tmp_path / "t.json",
                                              http=httpx.AsyncClient(transport=NO_HTTP)),
                     team_board=TeamBoard("https://board.invalid", "tok", transport=NO_HTTP))


def prop_names(schema, path="$"):
    if isinstance(schema, dict):
        for k, v in (schema.get("properties") or {}).items():
            yield path, k
            yield from prop_names(v, f"{path}.{k}")
        for key in ("items", "additionalProperties", "not", "if", "then", "else"):
            if isinstance(schema.get(key), dict):
                yield from prop_names(schema[key], f"{path}/{key}")
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for i, sub in enumerate(schema.get(key) or []):
                yield from prop_names(sub, f"{path}/{key}[{i}]")


def problems(t: types.Tool) -> list[str]:
    out = []
    if len(SERVER_PREFIX + t.name) > 128 or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", t.name):
        out.append("name")
    if len(t.description or "") >= 4096:
        out.append("description over 4,096 characters")
    for label, schema in (("input", t.inputSchema), ("output", t.outputSchema)):
        if schema is None:
            continue
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as e:  # noqa: BLE001
            out.append(f"{label} schema invalid: {str(e)[:120]}")
        if schema.get("type") != "object":
            out.append(f"{label} root is not an object")
        out += [f"{label} has {k} at the root" for k in ("anyOf", "oneOf", "allOf") if k in schema]
        out += [f"{label} property {p}.{n!r}" for p, n in prop_names(schema) if not PROP.fullmatch(n)]
    return out


async def listed(conn: Connector) -> dict[str, types.Tool]:
    async with create_connected_server_and_client_session(build_server(conn)) as client:
        return {t.name: t for t in (await client.list_tools()).tools}


async def test_every_tool_passes_the_shape_checks(tmp_path):
    tools = await listed(full_connector(tmp_path))
    assert len(tools) >= 70
    assert {n: p for n, t in tools.items() if (p := problems(t))} == {}


async def test_hints_are_consistent(tmp_path):
    tools = await listed(full_connector(tmp_path))
    for n, t in tools.items():
        a = t.annotations
        assert a is not None, n
        if a.readOnlyHint:
            assert a.idempotentHint is True, f"{n}: read-only tools are safe to repeat"
            assert not a.destructiveHint, n
    # writes that would act twice on a retry are never marked repeatable
    for n in ("son_send", "github_issue_create", "github_pr_merge", "telegram_topic_send", "discord_thread_create",
              "board_create", "son_session_reset", "son_slash", "cleanup", "ai_signin_start"):
        assert tools[n].annotations.readOnlyHint is False and tools[n].annotations.idempotentHint is not True, n


async def test_result_size_and_person_flags(tmp_path):
    tools = await listed(full_connector(tmp_path))
    for n in ("usage_guide", "github_search", "github_pr_diff", "son_conversation_history", "discord_read",
              "telegram_topic_messages", "board_list", "health"):
        assert tools[n].meta["anthropic/maxResultSizeChars"] >= 100_000, n
    for n, t in tools.items():
        person = (t.meta or {}).get("anthropic/requiresUserInteraction")
        if n in OWNER_CONFIRM or n in TOOL_ADDON:
            assert person is True, n
        else:
            assert person is None, n  # reads and free tools keep the client's normal permission rules
    assert {"son_model_set_all", "son_slash", "son_approvals_set", "discord_thread_edit", "discord_api"} <= {
        n for n, t in tools.items() if (t.meta or {}).get("anthropic/requiresUserInteraction")}
    assert "son_model_status" in tools and not (tools["son_model_status"].meta or {})
    assert BIG_RESULT  # the set is not empty by accident


def test_problems_catches_bad_shapes():
    bad = types.Tool(name="x" * 120, description="d" * 5000, inputSchema={
        "type": "object", "anyOf": [{"required": ["a"]}], "properties": {"bad name": {"type": "string"}}})
    p = problems(bad)
    assert "name" in p and "description over 4,096 characters" in p
    assert any("anyOf" in x for x in p) and any("'bad name'" in x for x in p)


def test_control_tools_still_need_a_session_selector():
    from aiteam_connector.gateway_control import build_control_request
    with pytest.raises(ValueError):
        build_control_request("son_session_stop", {})
    assert build_control_request("son_session_stop", {"sessionKey": "k"})[0] == "session_stop"


@pytest.mark.parametrize("content,is_error,want", [
    ('{"a": 1}', False, {"a": 1}),
    ("[1, 2]", False, {"value": [1, 2]}),
    ("plain words", False, {"text": "plain words"}),
    ("it broke", True, {"ok": False, "error": "it broke"}),
    ('{"error": "x"}', True, {"error": "x"}),
])
def test_structured_results(content, is_error, want):
    r = types.CallToolResult(content=[types.TextContent(type="text", text=content)], isError=is_error)
    out = structured(r)
    assert out.structuredContent == want and out.content[0].text == content and out.isError is is_error


def test_structured_leaves_typed_and_media_results_alone():
    typed = types.Tool(name="t", inputSchema={"type": "object"}, outputSchema={"type": "object"})
    r = types.CallToolResult(content=[types.TextContent(type="text", text='{"a": 1}')])
    assert structured(r, typed).structuredContent is None
    img = types.CallToolResult(content=[types.ImageContent(type="image", data="AA==", mimeType="image/png")])
    assert structured(img).structuredContent is None
    already = types.CallToolResult(content=[types.TextContent(type="text", text="x")], structuredContent={"b": 2})
    assert structured(already).structuredContent == {"b": 2}


async def test_results_come_back_structured_over_mcp(tmp_path):
    conn = full_connector(tmp_path)
    async with create_connected_server_and_client_session(build_server(conn)) as client:
        await client.list_tools()
        guide = await client.call_tool("usage_guide", {})
        briefs = await client.call_tool("brief_templates", {})
    assert guide.structuredContent["text"].startswith(guide.content[0].text[:40])
    assert json.loads(briefs.content[0].text) == briefs.structuredContent


def test_finish_tool_keeps_explicit_hints():
    t = types.Tool(name="x", inputSchema={"type": "object"},
                   annotations=types.ToolAnnotations(readOnlyHint=True, idempotentHint=False))
    assert finish_tool(t).annotations.idempotentHint is False  # a tool that says otherwise keeps its word
