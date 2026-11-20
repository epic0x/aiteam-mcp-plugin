"""What every listed tool and every result carries, so clients that run code over tools (Claude Code's Code Mode,
our relay's execute) can use them safely. One pass at the end of tools/list and tools/call; tool definitions stay
where they are.

Tools (``finish_tool``):
  * ``readOnlyHint`` true implies ``idempotentHint`` true, unless a tool says otherwise. Read-only calls run in
    parallel and idempotent calls are repeated once after a dropped connection, so a write is never marked either.
  * ``_meta["anthropic/maxResultSizeChars"]`` on tools that return long answers (history, diffs, search, guides).
    Without it a client keeps only 50,000 characters inline and moves the rest to a file.
  * ``_meta["anthropic/requiresUserInteraction"]: true`` on owner controls that change the Son (model, fallback,
    sessions, approvals, slash commands) and on tools a paid add-on unlocks: a person answers every call.
Results (``structured``): JSON in ``structuredContent`` and the same JSON as text. A result that already has
structured content passes through unchanged.
"""
from __future__ import annotations

import json
from typing import Iterable

import mcp.types as types

from .entitlements import TOOL_ADDON

MAX_RESULT_CHARS = 300_000
# Long answers: conversations and history, search, diffs and logs, raw API reads, guides, board and memory reads.
BIG_RESULT = frozenset({
    "usage_guide", "brief_templates", "health", "son_conversation_history", "son_conversations",
    "messages_read", "sessions_history", "sessions_search", "session_search", "conversation_get", "events_poll",
    "github_search", "github_pr_diff", "github_pr_view", "github_issue_view", "github_run_view", "github_api",
    "github_issue_list", "github_pr_list", "github_repo_list",
    "discord_read", "discord_threads", "discord_threads_list", "telegram_topic_messages",
    "kanban_list", "kanban_show", "board_list", "team_map", "memory", "read_file", "search_files", "web_extract",
})
# Owner controls that change the Son: a person confirms each call (reads like son_model_status stay open).
OWNER_CONFIRM = frozenset({
    "son_model_set_all", "son_model_set_session", "son_model_fallback_set", "ai_model_set", "ai_fallback_set",
    "son_session_stop", "son_session_interrupt", "son_session_reset", "son_slash", "son_approvals_set",
})


def needs_person(name: str) -> bool:
    return name in OWNER_CONFIRM or name in TOOL_ADDON


def finish_tool(t: types.Tool) -> types.Tool:
    update: dict = {}
    a = t.annotations
    if a is not None and a.readOnlyHint is True and a.idempotentHint is None:
        update["annotations"] = a.model_copy(update={"idempotentHint": True})
    meta = dict(t.meta or {})
    if t.name in BIG_RESULT:
        meta.setdefault("anthropic/maxResultSizeChars", MAX_RESULT_CHARS)
    if needs_person(t.name):
        meta["anthropic/requiresUserInteraction"] = True
    if meta != (t.meta or {}):
        update["meta"] = meta
    return t.model_copy(update=update) if update else t


def finish_tools(tools: Iterable[types.Tool]) -> list[types.Tool]:
    return [finish_tool(t) for t in tools]


def structured(r: types.CallToolResult, tool: types.Tool | None = None) -> types.CallToolResult:
    """The result with structured content: a JSON object as is, other JSON as {"value": ...}, plain text as
    {"text": ...}; an error as {"ok": false, "error": ...}. Text content is never changed. Left as is: a result that
    already has structured content, one from a tool with an outputSchema, and one with images or files."""
    if r.structuredContent is not None or (tool is not None and tool.outputSchema is not None):
        return r
    texts = [c.text for c in r.content if isinstance(c, types.TextContent)]
    if not texts or len(texts) != len(r.content):
        return r
    joined = "\n".join(texts)
    try:
        val = json.loads(joined)
    except ValueError:
        val, data = None, {"text": joined}
    else:
        data = val if isinstance(val, dict) else {"value": val}
    if r.isError and "error" not in data:
        data = {"ok": False, "error": joined[:2000] if val is None else val}
    return r.model_copy(update={"structuredContent": data})


__all__ = ["BIG_RESULT", "MAX_RESULT_CHARS", "OWNER_CONFIRM", "finish_tool", "finish_tools", "needs_person",
           "structured"]
