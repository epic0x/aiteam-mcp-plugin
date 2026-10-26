"""Hermes toolbox for ``hermes mcp serve``: every tool the Hermes agent can use, as an MCP tool.

Parity rule (owner 2026-10-13): if the Son's Hermes agent can do it, the MCP connector can do it too.
This module is the Son-side half of that rule. It registers on the Son's own ``hermes mcp serve``
(next to the stock 10 messaging tools and the OpenClaw session tools) and exposes the agent's
whole tool registry: terminal, files, browser, web, vision, cron, kanban, skills, memory,
Discord (full bot control), code execution, TTS, image/video, Home Assistant, Spotify, and any
plugin tool the box has enabled. The connector (``aiteam_connector``, passthrough ``all``)
forwards them to Claude / ChatGPT unchanged.

How it works
  * Tool list = ``model_tools.get_tool_definitions(<every toolset>)``: exactly the set the agent
    itself would get on this box (same ``check_fn`` gates: a tool needing a missing key or binary
    is not listed; ``hermes_toolbox_status`` says which ones and why).
  * A call goes through ``model_tools.handle_function_call``: the same hooks, guardrails,
    approval rules and middleware an agent turn uses. Nothing is re-implemented.
  * The four "agent loop" tools are bound here because they need per-agent state:
      memory          built-in MEMORY.md/USER.md store (+ the configured memory provider's tools,
                      e.g. mnemosyne_*), same files the agent reads
      todo_list       one todo list per MCP server process
      session_search  the agent's session search over state.db
      delegate_task   a real Hermes sub-agent run (headless parent agent built on first use)
  * ``clarify`` is not exposed: over MCP the human is already the caller, so there is nobody
    else to ask.

Config (``hermes config set``): ``mcp.toolbox.enabled`` (default true), ``mcp.toolbox.exclude``
(list of tool names to hide; default empty: full parity). Hooked in the same way as
``mcp_session_tools`` (one ``register(mcp)`` call at the end of ``create_mcp_server``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hermes.mcp_toolbox")

TASK_ID = "mcp-toolbox"  # one terminal/browser sandbox shared by every call from the connector
SESSION_ID = "mcp-toolbox"
NOT_EXPOSED = {"clarify"}  # the MCP caller is the human; there is no one else to ask
SESSION_GATED = ("cronjob_manage",)  # check_fn wants a chat/CLI session marker; the MCP owner is that user
AGENT_LOOP = {"memory", "todo_list", "session_search", "delegate_task"}
READ_ONLY = {
    "read_file", "search_files", "web_search", "web_extract", "vision_analyze", "video_analyze",
    "skills_list", "skill_view", "session_search", "kanban_list", "kanban_show", "kanban_attachments",
    "browser_snapshot", "browser_get_images", "browser_vision", "browser_console", "browser_vault_list",
    "ha_get_state", "ha_list_entities", "ha_list_services", "spotify_search", "x_search", "read_terminal",
    "feishu_doc_read", "feishu_drive_list_comments", "feishu_drive_list_comment_replies",
    "hermes_toolbox_status",
}
DESTRUCTIVE = {"terminal", "execute_code", "write_file", "patch", "process_manage", "cronjob_manage",
               "skill_manage", "discord_admin", "browser_exec", "computer_use", "ha_call_service"}


def _config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config  # type: ignore
        cfg = load_config() or {}
    except Exception:
        try:
            import yaml  # type: ignore
            home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
            cfg = yaml.safe_load(open(os.path.join(home, "config.yaml"))) or {}
        except Exception:
            cfg = {}
    return ((cfg.get("mcp") or {}).get("toolbox") or {}) if isinstance(cfg, dict) else {}


class Toolbox:
    """Lazy, thread-safe wrapper around the Hermes tool registry for one MCP server process."""

    def __init__(self, exclude: Optional[List[str]] = None):
        self.exclude = set(exclude or []) | NOT_EXPOSED
        self._lock = threading.RLock()
        self._defs: Optional[List[Dict[str, Any]]] = None
        self._mem_store = None
        self._mem_manager = None
        self._mem_tools: set = set()
        self._todo = None
        self._parent = None
        self._unavailable: Dict[str, str] = {}

    # ------------------------------------------------------------------ discovery
    def _discover(self) -> List[Dict[str, Any]]:
        import model_tools
        import toolsets
        try:
            from hermes_cli import plugins
            plugins.discover_plugins()
        except Exception:
            logger.debug("plugin discovery failed", exc_info=True)
        names = toolsets.get_toolset_names()
        defs = model_tools.get_tool_definitions(enabled_toolsets=names, quiet_mode=True,
                                                skip_tool_search_assembly=True)
        out, seen = [], set()
        for d in defs:
            fn = d.get("function") or d
            n = fn.get("name")
            if not n or n in seen or n in self.exclude:
                continue
            seen.add(n)
            out.append({"name": n, "description": fn.get("description") or "",
                        "parameters": fn.get("parameters") or {"type": "object", "properties": {}}})
        # memory-provider tools (mnemosyne_* etc.) live on the provider, not the registry
        for s in self._memory_provider_schemas():
            n = s.get("name")
            if n and n not in seen and n not in self.exclude:
                seen.add(n)
                self._mem_tools.add(n)
                out.append({"name": n, "description": s.get("description") or "",
                            "parameters": s.get("parameters") or {"type": "object", "properties": {}}})
        # Tools the registry gates on "is a user session" (env markers a headless MCP server lacks). The MCP caller
        # is the signed-in owner, so they are user-session tools here too.
        try:
            from tools.registry import registry
            for n in SESSION_GATED:
                if n in seen or n in self.exclude:
                    continue
                sch = registry.get_schema(n)
                if sch:
                    seen.add(n)
                    out.append({"name": n, "description": sch.get("description") or "",
                                "parameters": sch.get("parameters") or {"type": "object", "properties": {}}})
        except Exception:
            logger.debug("session-gated tools unavailable", exc_info=True)
        avail = {d["name"] for d in out}
        try:
            for n in sorted(model_tools.get_all_tool_names()):
                if n not in avail and n not in self.exclude:
                    self._unavailable[n] = "not enabled on this box (missing key, binary or platform)"
        except Exception:
            pass
        return out

    def definitions(self) -> List[Dict[str, Any]]:
        with self._lock:
            if self._defs is None:
                self._defs = self._discover() + [_status_def()]
            return self._defs

    # ------------------------------------------------------------------ agent-loop state
    def _memory_provider_schemas(self) -> List[Dict[str, Any]]:
        mgr = self._memory_manager()
        if not mgr:
            return []
        try:
            return [s.get("function", s) for s in mgr.get_all_tool_schemas()]
        except Exception:
            logger.debug("memory provider schemas failed", exc_info=True)
            return []

    def _memory_manager(self):
        if self._mem_manager is not None:
            return self._mem_manager or None
        self._mem_manager = False
        try:
            from hermes_cli.config import load_config
            from agent.memory_provider import is_core_memory_provider
            name = ((load_config() or {}).get("memory") or {}).get("provider", "")
            if name and not is_core_memory_provider(name):
                from agent.memory_manager import MemoryManager
                from plugins.memory import load_memory_provider
                mp = load_memory_provider(name)
                if mp and mp.is_available():
                    mgr = MemoryManager()
                    mgr.add_provider(mp)
                    from hermes_constants import get_hermes_home  # type: ignore
                    mgr.initialize_all(session_id=SESSION_ID, platform="mcp", hermes_home=str(get_hermes_home()),
                                       agent_context="primary")
                    self._mem_manager = mgr
        except Exception:
            logger.warning("memory provider unavailable for MCP toolbox", exc_info=True)
        return self._mem_manager or None

    def _memory_store(self):
        if self._mem_store is None:
            from hermes_cli.config import load_config
            from tools.memory_tool import MemoryStore, get_builtin_memory_config
            mc = get_builtin_memory_config(load_config() or {})
            self._mem_store = MemoryStore(memory_char_limit=mc.get("memory_char_limit", 2200),
                                          user_char_limit=mc.get("user_char_limit", 1375),
                                          memory_enabled=True, user_profile_enabled=True)
        # re-read every call: the agent may have written MEMORY.md since
        self._mem_store.load_from_disk()
        return self._mem_store

    def _todo_store(self):
        if self._todo is None:
            from tools.todo_tool import TodoStore
            self._todo = TodoStore()
        return self._todo

    def _parent_agent(self):
        if self._parent is None:
            from hermes_cli.config import load_config
            from hermes_cli.runtime_provider import resolve_runtime_provider
            from run_agent import AIAgent
            mcfg = (load_config() or {}).get("model") or {}
            rt = resolve_runtime_provider(requested=mcfg.get("provider"))  # same model/auth as the Son's chats
            self._parent = AIAgent(model=str(mcfg.get("default") or rt.get("model") or ""),
                                   api_key=rt.get("api_key"), base_url=rt.get("base_url"),
                                   provider=rt.get("provider"), api_mode=rt.get("api_mode"),
                                   quiet_mode=True, platform="mcp", session_id=SESSION_ID, skip_memory=True,
                                   skip_context_files=True)
        return self._parent

    # ------------------------------------------------------------------ calls
    def call(self, name: str, args: Dict[str, Any]) -> str:
        if name == "hermes_toolbox_status":
            return json.dumps(self.status(), indent=1)
        if name in self.exclude:
            return json.dumps({"error": f"{name} is not exposed over MCP on this box"})
        if name in AGENT_LOOP:
            return self._agent_loop(name, args)
        if name in self._mem_tools:
            return self._memory_manager().handle_tool_call(name, args)
        import model_tools
        return model_tools.handle_function_call(name, args, task_id=TASK_ID, session_id=SESSION_ID,
                                                user_task="MCP connector call")

    def _agent_loop(self, name: str, args: Dict[str, Any]) -> str:
        if name == "memory":
            from tools.memory_tool import memory_tool
            return memory_tool(action=args.get("action"), target=args.get("target", "memory"),
                               content=args.get("content"), old_text=args.get("old_text"),
                               new_text=args.get("new_text"), operations=args.get("operations"),
                               store=self._memory_store())
        if name == "todo_list":
            from tools.todo_tool import todo_tool
            return todo_tool(todos=args.get("todos"), merge=bool(args.get("merge", False)), store=self._todo_store())
        if name == "session_search":
            from tools.session_search_tool import session_search
            keys = ("role_filter", "session_id", "around_message_id", "sort", "profile", "after", "before",
                    "exclude_session_ids")
            return session_search(query=args.get("query") or "", limit=args.get("limit", 3),
                                  window=args.get("window", 5), detail=args.get("detail", "adaptive"),
                                  **{k: args.get(k) for k in keys})
        if name == "delegate_task":
            from tools.delegate_tool import delegate_task
            a = dict(args)
            a["background"] = False  # results must come back in this MCP call
            keys = ("goal", "context", "tasks", "max_iterations", "role", "background", "output_schema", "images",
                    "action", "subagent_id", "message")
            return delegate_task(parent_agent=self._parent_agent(), **{k: a.get(k) for k in keys})
        return json.dumps({"error": f"unknown agent-loop tool {name}"})

    def status(self) -> Dict[str, Any]:
        defs = self.definitions()
        return {"exposed": sorted(d["name"] for d in defs), "exposed_count": len(defs),
                "not_available_on_this_box": self._unavailable, "hidden_by_config": sorted(self.exclude)}


def _status_def() -> Dict[str, Any]:
    return {"name": "hermes_toolbox_status",
            "description": "List every Hermes tool exposed on this Son over MCP, and the ones the agent has but "
                           "this box cannot run yet (missing key, binary or platform), with the reason.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}


def _annotations(name: str):
    import mcp_types as T  # mcp>=2
    ro = name in READ_ONLY
    return T.ToolAnnotations(title=name.replace("_", " ").capitalize(), read_only_hint=ro,
                             destructive_hint=(name in DESTRUCTIVE), open_world_hint=not ro)


def register(mcp, toolbox: Optional[Toolbox] = None) -> List[str]:
    """Add the toolbox to a ``mcp.server.MCPServer`` (Hermes ``create_mcp_server``). Returns tool names."""
    cfg = _config()
    if cfg.get("enabled") is False:
        return []
    tb = toolbox or Toolbox(cfg.get("exclude") or [])
    try:
        defs = tb.definitions()
    except Exception:
        logger.warning("toolbox discovery failed; MCP keeps the messaging + session tools only", exc_info=True)
        return []
    existing = {t.name for t in mcp._tool_manager.list_tools()}
    defs = [d for d in defs if d["name"] not in existing]
    by_name = {d["name"]: d for d in defs}
    orig_list, orig_call = mcp.list_tools, mcp.call_tool

    async def list_tools():
        import mcp_types as T
        tools = await orig_list()
        for d in defs:
            try:
                ann = _annotations(d["name"])
            except Exception:
                ann = None
            tools.append(T.Tool(name=d["name"], description=d["description"][:4000],
                                input_schema=d["parameters"], annotations=ann))
        return tools

    async def call_tool(name, arguments, context=None):
        if name not in by_name:
            return await orig_call(name, arguments, context)
        import mcp_types as T
        try:
            out = await asyncio.to_thread(tb.call, name, dict(arguments or {}))
        except Exception as e:  # never a bare failure: say what broke
            logger.exception("toolbox call %s failed", name)
            return T.CallToolResult(content=[T.TextContent(type="text", text=f"{name} failed: {type(e).__name__}: {e}")],
                                    is_error=True)
        if not isinstance(out, str):
            out = json.dumps(out, default=str)
        is_err = False
        try:
            j = json.loads(out)
            is_err = isinstance(j, dict) and (j.get("success") is False or (bool(j.get("error")) and "output" not in j))
        except Exception:
            pass
        return T.CallToolResult(content=[T.TextContent(type="text", text=out)], is_error=is_err)

    mcp.list_tools, mcp.call_tool = list_tools, call_tool
    logger.info("MCP toolbox: %d Hermes tools exposed", len(defs))
    return list(by_name)
