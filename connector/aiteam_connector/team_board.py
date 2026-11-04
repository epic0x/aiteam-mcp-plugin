"""Team board + team map: the team's shared task list and who is in which team, read and changed from the chat.

Free (part of the coordination base, never paywalled). On only when the box sets both env vars:
  TEAM_BOARD_URL    where the Son's board calls go (the box forwards them, signed, to aiteam.space)
  TEAM_BOARD_TOKEN  bearer for that endpoint
With either missing the four tools are not listed, and a call answers "not enabled".

Transport: ``POST $TEAM_BOARD_URL`` with ``Authorization: Bearer $TEAM_BOARD_TOKEN`` and a JSON body
``{op, team_id?, task?}``; the site's JSON comes back as is. The Son's identity is added by the box, never by the
caller, so no Son id is sent from here. The token stays in memory: never logged, never in a result.

A task holds only title, status, assignee, an optional https link and a version. Updates are compare-and-set: send
the version you read; if someone changed the task first the site answers 409 with the current row, and the tool
returns that row as a normal result so the caller can re-apply its change.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx
import mcp.types as types
from jsonschema import ValidationError, validate

TEAM_BOARD_TOOLS = ("team_map", "board_list", "board_create", "board_update")
OPS = {"team_map": "map", "board_list": "list", "board_create": "create", "board_update": "update"}

# Same rules as the site (aiteam.space app/lib/team-board-rules.ts).
TEAM_ID = {"type": "string", "pattern": "^[A-Za-z0-9_-]{2,64}$", "description": "Team id, from team_map."}
TASK_ID = {"type": "string", "pattern": "^tt_[0-9a-f]{24}$", "description": "Task id, from board_list."}
TITLE = {"type": "string", "minLength": 1, "maxLength": 140, "pattern": "^[^\\x00-\\x1f\\x7f]+$",
         "description": "One line, up to 140 characters."}
STATUS = {"type": "string", "enum": ["todo", "doing", "blocked", "done"]}
ASSIGNEE = {"type": "object", "required": ["kind", "id"], "additionalProperties": False,
            "properties": {"kind": {"type": "string", "enum": ["son", "box", "external"]},
                           "id": {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,120}$"}},
            "description": "A Son, box or external node of this team (ids from team_map)."}
LINK = {"type": "string", "maxLength": 500, "pattern": "^https://[^\\s]+$",
        "description": "Optional https:// link (a PR, a thread, a doc)."}

NOT_ENABLED = "The team board is not enabled on this connector."
NOT_IN_TEAM = "This Son is not in that team (or its link was removed)."
UNKNOWN_TEAM = "Unknown team."
CONFLICT = ("Someone changed this task first. Here is the latest version; apply your change to it and try again.")
UNREACHABLE = "The team board is not reachable right now. Nothing was changed that we know of; try again in a minute."


def team_board_tools() -> list[types.Tool]:
    ro = lambda t: types.ToolAnnotations(title=t, readOnlyHint=True, destructiveHint=False,  # noqa: E731
                                         idempotentHint=True, openWorldHint=True)
    rw = lambda t, idem: types.ToolAnnotations(title=t, readOnlyHint=False, destructiveHint=False,  # noqa: E731
                                               idempotentHint=idem, openWorldHint=True)
    return [
        types.Tool(
            name="team_map", title="Show my teams",
            description="The teams this Son is in, their nodes (Sons, boxes and external nodes: id, kind, name, home "
                        "team, teams, address) and the links between them (direction, read or read_write). Metadata "
                        "only: no messages, memories or task contents. Start here to find team ids and assignee ids.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            annotations=ro("Show my teams")),
        types.Tool(
            name="board_list", title="Read a team board",
            description="All tasks on one team's board: id, title, status (todo, doing, blocked, done), assignee, "
                        "link and version. Pass the version to board_update when you change a task.",
            inputSchema={"type": "object", "required": ["team_id"], "additionalProperties": False,
                         "properties": {"team_id": TEAM_ID}},
            annotations=ro("Read a team board")),
        types.Tool(
            name="board_create", title="Add a task to a team board",
            description="Add a task to a team's board. Title only, plus optional status (default todo), assignee "
                        "and https link. Never put secrets, message text or customer data in a title.",
            inputSchema={"type": "object", "required": ["team_id", "title"], "additionalProperties": False,
                         "properties": {"team_id": TEAM_ID, "title": TITLE, "status": STATUS,
                                        "assignee": ASSIGNEE, "link": LINK}},
            annotations=rw("Add a task", False)),
        types.Tool(
            name="board_update", title="Change a task on a team board",
            description="Change a task's title, status, assignee or link. Pass the version you last read; null "
                        "clears the assignee or the link. If someone changed it first, you get the latest version "
                        "back: apply your change to it and call again with its version.",
            inputSchema={"type": "object", "required": ["team_id", "task_id", "version"], "minProperties": 4,
                         "additionalProperties": False,
                         "properties": {"team_id": TEAM_ID, "task_id": TASK_ID,
                                        "version": {"type": "integer", "minimum": 1, "maximum": 999999999},
                                        "title": TITLE, "status": STATUS,
                                        "assignee": {**ASSIGNEE, "type": ["object", "null"]},
                                        "link": {**LINK, "type": ["string", "null"]}}},
            annotations=rw("Change a task", True)),
    ]


def _schema(name: str) -> dict:
    return next(t.inputSchema for t in team_board_tools() if t.name == name)


def build_body(name: str, args: dict) -> dict:
    """The request body for a tool call. Raises ValueError (plain text) when the arguments break the schema."""
    try:
        validate(args, _schema(name))
    except ValidationError as e:
        field = ".".join(str(p) for p in e.absolute_path) or "arguments"
        raise ValueError(f"Invalid {field}: {e.message}") from None
    op = OPS[name]
    if op == "map":
        return {"op": "map"}
    if op == "list":
        return {"op": "list", "team_id": args["team_id"]}
    if op == "create":
        return {"op": "create", "team_id": args["team_id"],
                "task": {k: args[k] for k in ("title", "status", "assignee", "link") if k in args}}
    patch = {k: args[k] for k in ("title", "status", "assignee", "link") if k in args}
    return {"op": "update", "team_id": args["team_id"],
            "task": {"id": args["task_id"], "version": args["version"], **patch}}


def _result(message: str, data: Optional[dict] = None, is_error: bool = False) -> types.CallToolResult:
    body = message if data is None else message + "\n" + json.dumps(data, ensure_ascii=False, indent=1)
    return types.CallToolResult(content=[types.TextContent(type="text", text=body)],
                                structuredContent=data, isError=is_error)


class TeamBoard:
    def __init__(self, url: str, token: str, transport: Optional[httpx.AsyncBaseTransport] = None,
                 timeout: float = 20):
        self.url, self._token = url, token
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10), transport=transport,
                                      headers={"User-Agent": "aiteam-connector/team-board"})

    def __repr__(self) -> str:  # keep the token out of any repr / log line
        return f"TeamBoard(url={self.url!r})"

    @classmethod
    def from_env(cls, transport: Optional[httpx.AsyncBaseTransport] = None) -> Optional["TeamBoard"]:
        url = os.environ.get("TEAM_BOARD_URL", "").strip()
        token = os.environ.get("TEAM_BOARD_TOKEN", "").strip()
        if not url or not token or not url.startswith(("https://", "http://")):
            return None
        return cls(url, token, transport)

    async def call(self, name: str, args: dict) -> types.CallToolResult:
        args = {k: v for k, v in (args or {}).items() if k != "son"}
        try:
            body = build_body(name, args)
        except ValueError as e:
            return _result(str(e), is_error=True)
        try:
            r = await self.http.post(self.url, json=body, headers={"Authorization": f"Bearer {self._token}"})
        except httpx.HTTPError as e:  # the message can carry the URL; keep only the kind of failure
            return _result(f"{UNREACHABLE} ({type(e).__name__})", is_error=True)
        try:
            data = r.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            data = None
        code = r.status_code
        if code < 300 and data is not None:
            return _result(_ok_line(name, data), data)
        word = str((data or {}).get("error") or "")
        if code == 409 and data is not None and isinstance(data.get("task"), dict):
            return _result(CONFLICT, {"conflict": True, "task": data["task"]})
        if code in (401, 403):
            return _result(NOT_IN_TEAM, {"ok": False, "status": code, "error": word or "forbidden"}, True)
        if code == 404:
            line = UNKNOWN_TEAM + (" Or that task is not on its board." if name == "board_update" else "")
            return _result(line, {"ok": False, "status": code, "error": word or "not_found"}, True)
        if code in (400, 409, 413, 422, 428):
            msg = str((data or {}).get("message") or "")
            line = f"The team board refused this: {word or 'bad_request'}." + (f" {msg}" if msg else "")
            return _result(line, {"ok": False, "status": code, "error": word or "bad_request"}, True)
        return _result(f"{UNREACHABLE} (HTTP {code})", is_error=True)


def _ok_line(name: str, data: dict) -> str:
    if name == "board_create":
        return "Task added."
    if name == "board_update":
        return "Task changed."
    if name == "board_list" and data.get("ready") is False:
        return "This team's board is not set up yet."
    return "OK."
