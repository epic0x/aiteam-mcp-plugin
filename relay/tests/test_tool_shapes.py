"""Every relay tool fits what clients that run their own Code Mode accept, and every result can be read by code.

Checks per tool, on the live tools/list of a real session (native tools, code tools and merged box tools):
  * input and output schemas are valid JSON Schema draft 2020-12, with no anyOf / oneOf / allOf at the root;
  * every property name is 1-64 characters of [A-Za-z0-9_.-];
  * the client-side name (mcp__<server>__<tool>) stays within 128 characters;
  * descriptions stay under 4,096 characters;
  * read-only tools carry readOnlyHint and idempotentHint; big readers carry anthropic/maxResultSizeChars.
Results: JSON in structuredContent plus the same JSON as text, also for box tools that answer text only.
"""
import json
import re

import mcp.types as types
from jsonschema import Draft202012Validator

from .test_codemode import needs_sandbox, relay as code_relay
from .test_multibox import RO, V3, clock, raw, relay, rpc, session, world  # noqa: F401 (fixtures)

PROP = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SERVER_PREFIX = "mcp__aiteam-space__"  # the longest server name a client is likely to give this connection
BIG = {"boxes_list", "kanban_list", "kanban_read", "memory_read", "search", "describe", "execute"}


def prop_names(schema, path="$"):
    """Every property name in a schema, nested ones included."""
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
        for k, v in (schema.get("$defs") or {}).items():
            yield from prop_names(v, f"{path}/$defs/{k}")


def problems(tool: types.Tool) -> list[str]:
    out = []
    if len(SERVER_PREFIX + tool.name) > 128:
        out.append("name too long")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool.name):
        out.append("name characters")
    if len(tool.description or "") >= 4096:
        out.append("description over 4,096 characters")
    for label, schema in (("input", tool.inputSchema), ("output", tool.outputSchema)):
        if schema is None:
            continue
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as e:  # noqa: BLE001
            out.append(f"{label} schema invalid: {e}")
        if schema.get("type") != "object":
            out.append(f"{label} schema root is not an object")
        for key in ("anyOf", "oneOf", "allOf"):
            if key in schema:
                out.append(f"{label} schema has {key} at the root")
        for path, name in prop_names(schema):
            if not PROP.fullmatch(name):
                out.append(f"{label} property {path}.{name!r}")
    return out


async def test_every_listed_tool_passes_the_shape_checks(world, clock, monkeypatch):
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        tools = (await s.list_tools()).tools
    assert len(tools) > 8
    bad = {t.name: p for t in tools if (p := problems(t))}
    assert bad == {}


def test_problems_catches_bad_shapes():
    t = types.Tool(name="x" * 120, description="d" * 5000, inputSchema={
        "type": "object", "anyOf": [{"required": ["a"]}], "properties": {"bad name!": {"type": "string"},
                                                                            "n": {"type": "object", "properties": {
                                                                                "": {"type": "string"}}}}})
    p = problems(t)
    assert "name too long" in p and "description over 4,096 characters" in p
    assert any("anyOf at the root" in x for x in p)
    assert any("'bad name!'" in x for x in p) and any("''" in x for x in p)
    assert problems(types.Tool(name="ok", inputSchema={"type": "object", "properties": {"type": 3}})) != []


async def test_native_tools_hints_and_result_size(world, clock, monkeypatch):
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
    for n in ("boxes_list", "kanban_list", "kanban_read", "memory_read"):
        a = tools[n].annotations
        assert a.readOnlyHint is True and a.idempotentHint is True and a.destructiveHint is False, n
        assert (tools[n].meta or {}).get("anthropic/maxResultSizeChars", 0) >= 100_000, n
    cal = tools["calendar_post"].annotations
    assert cal.readOnlyHint is False and cal.idempotentHint is False  # a retry would add the post twice
    assert tools["boxes_list"].outputSchema is not None


@needs_sandbox
async def test_code_tools_shapes_and_limits(world, clock, monkeypatch):
    from aiteam_relay.codemode import WALL_SECONDS
    async with code_relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
        r = await s.call_tool("execute", {"code": "return (await boxes.list()).map(b => b.box)"})
        q = await s.call_tool("search", {"query": "kanban"})
    for n in ("search", "describe", "execute"):
        t = tools[n]
        assert problems(t) == [], n
        assert t.annotations.readOnlyHint is True and t.annotations.idempotentHint is True
        assert t.outputSchema is not None and (t.meta or {})["anthropic/maxResultSizeChars"] >= 100_000
        assert "box tool" in t.description.lower(), n  # tool search must not mix them up with client tools
    assert "call the box tools directly" in tools["execute"].description
    assert WALL_SECONDS < 60  # a client waits about 60 s for the first byte of an HTTP answer
    for res in (r, q):  # the SDK client already checked structuredContent against the outputSchema
        assert not res.isError and json.loads(res.content[0].text) == res.structuredContent
    assert r.structuredContent["value"] == ["box-a", "box-b"]


async def test_results_are_json_in_structured_content_and_text(world, clock, monkeypatch):
    world.a.tools["text_only"] = (RO, None)
    world.a.tools["plain"] = (RO, None)
    original = world.a.server.request_handlers[types.CallToolRequest]

    async def call(req):
        n = req.params.name
        if n in ("text_only", "plain"):
            body = json.dumps({"rows": [1, 2]}) if n == "text_only" else "just words"
            return types.ServerResult(types.CallToolResult(content=[types.TextContent(type="text", text=body)]))
        return await original(req)
    world.a.server.request_handlers[types.CallToolRequest] = call
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        boxes = await s.call_tool("boxes_list", {})
        fan = await s.call_tool("kanban_list", {"box": "all"})
        txt = await s.call_tool("text_only", {"box": "box-a"})
        plain = await s.call_tool("plain", {"box": "box-a"})
        bad = await s.call_tool("shared", {"box": "box-z"})
    for res in (boxes, fan):
        assert json.loads(res.content[0].text) == res.structuredContent
    assert boxes.structuredContent["answering"] == 2 and fan.structuredContent["answered"] == 2
    assert txt.structuredContent == {"rows": [1, 2]} and txt.content[0].text == json.dumps({"rows": [1, 2]})
    assert plain.structuredContent == {"text": "just words"} and plain.content[0].text == "just words"
    assert bad.isError and bad.structuredContent == {"ok": False, "error": "box not in this connection"}


async def test_unsupported_revision_gets_the_spec_error(world, clock, monkeypatch):
    """A request at 2026-07-28 (not spoken here) gets 400 + UnsupportedProtocolVersionError naming the supported
    revisions, so a client retries with one of them instead of failing on an unreadable 400."""
    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
    async with relay(world, clock, monkeypatch) as app:
        r = await raw(app, V3(), "POST", rpc(5, "tools/list"), headers={"mcp-protocol-version": "2026-07-28"})
        ok = await raw(app, V3(), "POST", rpc(6, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "c", "version": "1"}}),
            headers={"mcp-protocol-version": "2026-07-28"})
    assert r.status_code == 400
    body = r.json()
    assert body["id"] == 5 and body["error"]["code"] == -32022
    assert body["error"]["data"]["requested"] == "2026-07-28"
    assert set(body["error"]["data"]["supported"]) == set(SUPPORTED_PROTOCOL_VERSIONS)
    assert ok.status_code == 200  # initialize negotiates in its body and still works
