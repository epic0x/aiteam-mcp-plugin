"""hermes_toolbox (son/) without a Hermes install: stub the Hermes modules it imports and check the contract.

The real thing is exercised live by connector/scripts/parity_hermes_live.py against a Son.
"""
import asyncio
import importlib
import json
import sys
import types as pytypes
from pathlib import Path

import pytest

SON = Path(__file__).resolve().parents[2] / "son"


class _Model:  # tiny pydantic stand-in for mcp_types
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def toolbox(monkeypatch):
    calls = []
    mt = pytypes.ModuleType("model_tools")
    mt.get_tool_definitions = lambda **kw: [
        {"type": "function", "function": {"name": "terminal", "description": "Run a shell command.",
                                          "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}},
        {"type": "function", "function": {"name": "read_file", "description": "Read.", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "clarify", "description": "Ask.", "parameters": {"type": "object"}}},
    ]
    mt.get_all_tool_names = lambda: ["terminal", "read_file", "clarify", "spotify_search"]

    def handle(name, args, **kw):
        calls.append((name, args, kw))
        return json.dumps({"output": f"ran {name}", "exit_code": 0, "error": None})
    mt.handle_function_call = handle
    ts = pytypes.ModuleType("toolsets")
    ts.get_toolset_names = lambda: ["terminal", "file"]
    reg_mod = pytypes.ModuleType("tools.registry")
    reg_mod.registry = pytypes.SimpleNamespace(get_schema=lambda n: {"name": n, "description": "Cron.",
                                                                     "parameters": {"type": "object"}})
    tools_pkg = pytypes.ModuleType("tools")
    mtypes = pytypes.ModuleType("mcp_types")
    for cls in ("Tool", "ToolAnnotations", "CallToolResult", "TextContent"):
        setattr(mtypes, cls, type(cls, (_Model,), {}))
    cfg = pytypes.ModuleType("hermes_cli.config")
    cfg.load_config = lambda: {}
    hc = pytypes.ModuleType("hermes_cli")
    for name, mod in {"model_tools": mt, "toolsets": ts, "tools": tools_pkg, "tools.registry": reg_mod,
                      "mcp_types": mtypes, "hermes_cli": hc, "hermes_cli.config": cfg}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.syspath_prepend(str(SON))
    sys.modules.pop("hermes_toolbox", None)
    tb = importlib.import_module("hermes_toolbox")
    return tb, calls, mtypes


class FakeServer:
    def __init__(self):
        self._tool_manager = pytypes.SimpleNamespace(list_tools=lambda: [pytypes.SimpleNamespace(name="messages_send")])

    async def list_tools(self):
        return []

    async def call_tool(self, name, arguments, context=None):
        return f"orig:{name}"


def test_every_available_hermes_tool_is_listed_except_clarify(toolbox):
    tb, calls, _ = toolbox
    srv = FakeServer()
    names = tb.register(srv)
    assert {"terminal", "read_file", "cronjob_manage", "hermes_toolbox_status"} <= set(names)
    assert "clarify" not in names  # the MCP caller is the human
    listed = asyncio.run(srv.list_tools())
    term = next(t for t in listed if t.name == "terminal")
    assert term.annotations.destructive_hint is True and term.annotations.read_only_hint is False
    rf = next(t for t in listed if t.name == "read_file")
    assert rf.annotations.read_only_hint is True


def test_calls_go_through_hermes_dispatch_and_others_fall_through(toolbox):
    tb, calls, _ = toolbox
    srv = FakeServer()
    tb.register(srv)
    r = asyncio.run(srv.call_tool("terminal", {"command": "echo hi"}))
    assert r.is_error is False and "ran terminal" in r.content[0].text
    assert calls[0][0] == "terminal" and calls[0][2]["task_id"] == tb.TASK_ID
    assert asyncio.run(srv.call_tool("messages_send", {})) == "orig:messages_send"
    st = json.loads(asyncio.run(srv.call_tool("hermes_toolbox_status", {})).content[0].text)
    assert "spotify_search" in st["not_available_on_this_box"]


def test_exclude_config_hides_tools(toolbox, monkeypatch):
    tb, _, _ = toolbox
    monkeypatch.setattr(tb, "_config", lambda: {"exclude": ["terminal"]})
    names = tb.register(FakeServer())
    assert "terminal" not in names and "read_file" in names


def test_disabled_registers_nothing(toolbox, monkeypatch):
    tb, _, _ = toolbox
    monkeypatch.setattr(tb, "_config", lambda: {"enabled": False})
    assert tb.register(FakeServer()) == []
