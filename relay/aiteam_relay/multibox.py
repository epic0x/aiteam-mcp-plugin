"""Multi-box relay (RELAY_MULTI_BOX=on): the relay is its own MCP server over every box in the caller's grant.

Per request: the relay JWT is verified (auth.py), the grant's boxes come from `GET <issuer>/oauth/targets` (targets.py,
30 s cache), and each tool call is exchanged for a short box token for exactly the chosen box (exchange.py) and sent to
that box's own /mcp as an MCP client (boxes.py). The MCP SDK owns the client-facing transport (Streamable HTTP,
stateless), so no box session id, stream or event id ever reaches the client or another box.

Tools offered:
  * every box tool once by name, with an added `box` argument (enum = the grant's boxes; required when the grant has
    more than one box). Annotations: the least safe hint across boxes. Results come back exactly as the box sent them.
  * boxes_list, kanban_list, kanban_read, memory_read (one box or box="all"), calendar_post.
Box prompts and resources are not merged; only the relay's own introduction is offered when available.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from typing import Any, Callable

import mcp.types as types
from mcp import ClientSession
from mcp.server.lowlevel import Server

from . import __version__
from .boxes import BoxError, BoxPool, raw_call
from .exchange import Exchanger, InvalidGrant
from .targets import Target, Targets, TargetsClient, TargetsMissing

log = logging.getLogger("aiteam_relay.multibox")

SCOPE_KEY = "aiteam_relay.auth"
NATIVE = ("boxes_list", "kanban_list", "kanban_read", "memory_read", "calendar_post")
FANOUT_TOOLS = {"kanban_list": "kanban_list", "kanban_read": "kanban_read", "memory_read": "memory_read"}
MAX_FANOUT_BYTES = 256 * 1024  # whole fan-out answer
MAX_MEDIA_BYTES = 2 * 1024 * 1024  # inline media; the MCP request body itself is capped at 4 MB
NOT_IN_CONNECTION = "box not in this connection"
INSTRUCTIONS = (
    "This connection reaches one or more aiteam.space boxes. Call boxes_list to see them and whether each one is up. "
    "Every tool takes a `box` argument (the box name from boxes_list); with a single box it can be left out. "
    "kanban_list, kanban_read and memory_read also take box=\"all\" to read every box at once. calendar_post adds a "
    "post to a content calendar for review; it never publishes."
)


@dataclass(frozen=True)
class Caller:
    token: str
    claims: dict

    @property
    def exp(self) -> float | None:
        e = self.claims.get("exp")
        return float(e) if isinstance(e, (int, float)) else None

    @property
    def legacy(self) -> bool:
        """A single-box grant from before scoped grants: exchange without `target`, exactly as today."""
        v = self.claims.get("grant_v")
        return v in (None, 1, "1") and bool(self.claims.get("son_id") or self.claims.get("node_id"))


def _text(msg: str, *, error: bool = True, data: dict | None = None) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], structuredContent=data,
                                isError=error)


def _json_result(data: dict, *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
                                structuredContent=data, isError=error)


def _result_data(r: types.CallToolResult) -> Any:
    if r.structuredContent is not None:
        return r.structuredContent
    texts = [c.text for c in r.content if isinstance(c, types.TextContent)]
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except ValueError:
        return joined


def _least_safe(hints: list[types.ToolAnnotations | None]) -> types.ToolAnnotations | None:
    """Merge one tool's hints across boxes: read-only only if every box says so; destructive if any box says so.
    A box with no hints at all counts as unknown, so the merged tool is not read-only."""
    if not any(h is not None for h in hints):
        return None
    hs = [h if h is not None else types.ToolAnnotations() for h in hints]

    def all_true(name):  # a "safe when true" hint: true only if every box says true
        vals = [getattr(h, name) for h in hs]
        return True if all(v is True for v in vals) else (False if any(v is False for v in vals) else None)

    def any_true(name):  # an "unsafe when true" hint: true if any box says true
        vals = [getattr(h, name) for h in hs]
        return True if any(v is True for v in vals) else (None if any(v is None for v in vals) else False)

    ro = all_true("readOnlyHint")
    return types.ToolAnnotations(title=next((h.title for h in hs if h.title), None), readOnlyHint=ro,
                                 destructiveHint=None if ro else any_true("destructiveHint"),
                                 idempotentHint=all_true("idempotentHint"), openWorldHint=any_true("openWorldHint"))


def box_prop(keys: list[str], *, allow_all: bool = False, what: str = "box") -> dict:
    enum = list(keys) + (["all"] if allow_all else [])
    desc = f"Which {what} (see boxes_list)." + (' "all" = every box at once.' if allow_all else "")
    return {"type": "string", "enum": enum, "description": desc}


def with_box(tool: types.Tool, keys: list[str], have: list[str]) -> types.Tool:
    schema = dict(tool.inputSchema or {"type": "object"})
    props = dict(schema.get("properties") or {})
    props["box"] = box_prop(keys)
    schema["properties"] = props
    req = [r for r in (schema.get("required") or []) if r != "box"]
    if len(keys) > 1:
        req.append("box")
    if req:
        schema["required"] = req
    else:
        schema.pop("required", None)
    desc = tool.description or ""
    if len(keys) > 1 and set(have) != set(keys):
        desc = (desc + "\n\n" if desc else "") + "Available on: " + ", ".join(have)
    return tool.model_copy(update={"inputSchema": schema, "description": desc})


def native_tools(t: Targets) -> list[types.Tool]:
    keys = t.keys
    cals = [x.key for x in t.targets if x.calendar]
    ro = lambda title: types.ToolAnnotations(title=title, readOnlyHint=True, destructiveHint=False,  # noqa: E731
                                             idempotentHint=True, openWorldHint=False)
    multi = len(keys) > 1
    fan = box_prop(keys, allow_all=True)
    lim = lambda d, hi: {"type": "integer", "minimum": 1, "maximum": hi, "default": d}  # noqa: E731
    cal_prop = {"type": "string", "description": "The content calendar box (see boxes_list, calendar=true)."}
    if cals:
        cal_prop["enum"] = cals
    return [
        types.Tool(name="boxes_list", title="Boxes in this connection",
                   description="Every box this connection reaches (name, kind, project, whether it is a content "
                               "calendar) and a live health check of each, 5 s per box. A box that does not answer "
                               "shows ok=false with the reason; the others still answer.",
                   inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
                   annotations=ro("Boxes in this connection")),
        types.Tool(name="kanban_list", title="List task boards",
                   description="Cards on a box's task board, read-only. box=\"all\" reads every box at once and "
                               "groups the answer per box (ok/error per box, 5 s each).",
                   inputSchema={"type": "object", "additionalProperties": False,
                                "required": ["box"] if multi else [],
                                "properties": {"box": fan, "status": {"type": "string", "pattern": "^[a-z_]{1,32}$"},
                                               "assignee": {"type": "string",
                                                            "pattern": "^[A-Za-z0-9_.@:-]{1,64}$"},
                                               "limit": lim(50, 200)}},
                   annotations=ro("List task boards")),
        types.Tool(name="kanban_read", title="Read a task card",
                   description="One card from a box's task board, read-only. box=\"all\" asks every box for that "
                               "card id and groups the answers per box.",
                   inputSchema={"type": "object", "additionalProperties": False,
                                "required": ["id", "box"] if multi else ["id"],
                                "properties": {"box": fan, "id": {"type": "string",
                                                                  "pattern": "^[A-Za-z0-9_.:-]{1,64}$"}}},
                   annotations=ro("Read a task card")),
        types.Tool(name="memory_read", title="Search box memory",
                   description="Read-only search of a box's shared memory. box=\"all\" searches every box and groups "
                               "the answers per box.",
                   inputSchema={"type": "object", "additionalProperties": False,
                                "required": ["box"] if multi else [],
                                "properties": {"box": fan, "query": {"type": "string", "maxLength": 200},
                                               "limit": lim(20, 100)}},
                   annotations=ro("Search box memory")),
        types.Tool(name="calendar_post", title="Add a post to a content calendar",
                   description="Adds a post to a content calendar in this connection for review. With date "
                               "(YYYY-MM-DD, board time zone) it waits for review on that day; without, it lands in "
                               "Unscheduled. Never approves or publishes. Media: media_url (https) or media_b64 "
                               "(one image or short video, up to 2 MB; larger files: media_url).",
                   inputSchema={"type": "object", "additionalProperties": False, "required": ["calendar", "caption"],
                                "properties": {
                                    "calendar": cal_prop,
                                    "caption": {"type": "string", "maxLength": 5000},
                                    "title": {"type": "string", "maxLength": 200,
                                              "description": "Default: the start of the caption."},
                                    "type": {"type": "string", "enum": ["post", "reel", "video", "image", "story",
                                                                        "carousel", "other"], "default": "post"},
                                    "media_url": {"type": "string", "pattern": "^https://"},
                                    "media_b64": {"type": "string"},
                                    "date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}}},
                   annotations=types.ToolAnnotations(title="Add a post to a content calendar", readOnlyHint=False,
                                                     destructiveHint=False, idempotentHint=False,
                                                     openWorldHint=False)),
    ]


_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png", "png"), (b"\xff\xd8\xff", "image/jpeg", "jpg"),
          (b"GIF8", "image/gif", "gif"), (b"\x1a\x45\xdf\xa3", "video/webm", "webm"))


def sniff(data: bytes) -> tuple[str, str] | None:
    for magic, ctype, ext in _MAGIC:
        if data.startswith(magic):
            return ctype, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    if data[4:8] == b"ftyp":
        return ("video/quicktime", "mov") if data[8:10] == b"qt" else ("video/mp4", "mp4")
    return None


def cap_rows(rows: list[dict], limit: int = MAX_FANOUT_BYTES) -> tuple[list[dict], bool]:
    """Keep whole rows until the answer would pass `limit` bytes; later rows keep only box + a truncated mark."""
    out, used, cut = [], 0, False
    for r in rows:
        size = len(json.dumps(r, ensure_ascii=False).encode())
        if used + size > limit:
            cut = True
            out.append({"box": r["box"], "ok": r["ok"], "truncated": True,
                        "error": "answer too large: ask this box alone"})
            continue
        used += size
        out.append(r)
    return out, cut


class Router:
    def __init__(self, targets: TargetsClient, exchanger: Exchanger, pool: BoxPool, *,
                 clock: Callable[[], float] = time.time) -> None:
        self.targets_client, self.exchanger, self.pool, self.clock = targets, exchanger, pool, clock

    async def targets(self, c: Caller) -> Targets:
        try:
            return await self.targets_client.get(c.token, c.exp)
        except TargetsMissing:
            if not c.legacy:
                raise
        # aiteam.space without /oauth/targets yet: a single-box grant still works through today's exchange.
        ex = await self.exchanger.get(c.token, c.exp)
        label = (urlsplit(ex.son_mcp_url).hostname or "box").split(".")[0]
        kind = "node" if c.claims.get("node_id") and not c.claims.get("son_id") else "son"
        tid = str(c.claims.get("son_id") or c.claims.get("node_id"))
        return Targets([Target(kind=kind, id=tid, key=label, name=label, mcp_url=ex.son_mcp_url,
                               calendar=label.startswith("calendar-"))], expires_at=0)

    # --------------------------------------------------------------- tools/list
    async def list_tools(self, c: Caller) -> list[types.Tool]:
        t = await self.targets(c)
        got = await self.pool.fan_out(t.targets, lambda x: self.pool.tools(c.token, c.exp, x, c.legacy))
        by_name: dict[str, list[tuple[Target, types.Tool]]] = {}
        for target, res in got:
            if isinstance(res, BaseException):
                continue
            for tool in res:
                if tool.name in NATIVE:
                    continue  # the relay's own tool of that name covers every box
                by_name.setdefault(tool.name, []).append((target, tool))
        keys = t.keys
        out = native_tools(t)
        for name in sorted(by_name):
            rows = by_name[name]
            base = rows[0][1]
            have = [x.key for x, _ in rows]
            merged = with_box(base, keys, have)
            merged = merged.model_copy(update={"annotations": _least_safe([tl.annotations for _, tl in rows])})
            out.append(merged)
        return out

    # --------------------------------------------------------------- tools/call
    def pick(self, t: Targets, args: dict) -> Target | str:
        box = args.get("box")
        if box is None:
            if len(t.targets) == 1:
                return t.targets[0]
            return "box is required: this connection has more than one box (see boxes_list)" if t.targets \
                else "this connection has no boxes right now"
        if not isinstance(box, str):
            return NOT_IN_CONNECTION
        return t.find(box) or NOT_IN_CONNECTION

    async def call(self, c: Caller, name: str, args: dict, progress=None) -> types.CallToolResult:
        t = await self.targets(c)
        if name == "boxes_list":
            return await self.boxes_list(c, t)
        if name in FANOUT_TOOLS:
            return await self.fan_tool(c, t, name, args)
        if name == "calendar_post":
            return await self.calendar_post(c, t, args)
        target = self.pick(t, args)
        if isinstance(target, str):
            return _text(target)
        fwd = {k: v for k, v in args.items() if k != "box"}
        try:
            return await self.pool.call(c.token, c.exp, target, c.legacy, name, fwd, progress=progress)
        except BoxError as e:
            return _text(f"{target.key}: {e}")

    async def _call_data(self, c: Caller, target: Target, name: str, args: dict) -> dict:
        r = await self.pool.call(c.token, c.exp, target, c.legacy, name, args, timeout=self.pool.box_timeout)
        if r.isError:
            data = _result_data(r)
            raise BoxError(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)[:500])
        return _result_data(r)

    async def boxes_list(self, c: Caller, t: Targets) -> types.CallToolResult:
        async def health(x: Target) -> dict:
            tools = await self.pool.tools(c.token, c.exp, x, c.legacy)
            if any(tl.name == "box_health" for tl in tools):
                return await self._call_data(c, x, "box_health", {})
            # an older box without box_health: a live MCP handshake + ping is the check
            start = time.monotonic()
            await self.pool.run(c.token, c.exp, x, c.legacy, lambda s: s.send_ping(), timeout=self.pool.box_timeout)
            return {"ok": True, "gateway_alive": True, "mcp_handshake_ms": int((time.monotonic() - start) * 1000),
                    "note": "this box has no box_health tool yet"}

        got = await self.pool.fan_out(t.targets, health)
        rows = []
        for x, res in got:
            row = x.row()
            if isinstance(res, BaseException):
                row.update(ok=False, error=str(res))
            else:
                row.update(ok=bool(res.get("ok", True)) if isinstance(res, dict) else True, health=res)
            rows.append(row)
        rows += [{"box": r["box"], "ok": False, "error": r["error"]} for r in t.rejected]
        rows, cut = cap_rows(rows)
        data = {"scope_rule": t.scope_rule, "project_name": t.project_name, "count": len(t.targets),
                "boxes": rows, "truncated": cut or t.truncated}
        up = sum(1 for r in rows if r.get("ok"))
        head = f"{len(t.targets)} boxes, {up} answering."
        return types.CallToolResult(content=[types.TextContent(type="text", text=head + "\n" + json.dumps(
            data, ensure_ascii=False))], structuredContent=data, isError=False)

    async def fan_tool(self, c: Caller, t: Targets, name: str, args: dict) -> types.CallToolResult:
        fwd = {k: v for k, v in args.items() if k != "box"}
        if args.get("box") == "all":
            chosen = t.targets
        else:
            one = self.pick(t, args)
            if isinstance(one, str):
                return _text(one)
            try:
                r = await self.pool.call(c.token, c.exp, one, c.legacy, FANOUT_TOOLS[name], fwd)
            except BoxError as e:
                return _text(f"{one.key}: {e}")
            return r
        async def leg(x: Target) -> dict:
            tools = await self.pool.tools(c.token, c.exp, x, c.legacy)
            if not any(tl.name == FANOUT_TOOLS[name] for tl in tools):
                raise BoxError(f"{FANOUT_TOOLS[name]} is not available on this box")
            return await self._call_data(c, x, FANOUT_TOOLS[name], fwd)

        got = await self.pool.fan_out(chosen, leg)
        rows = [({"box": x.key, "ok": False, "error": str(res)} if isinstance(res, BaseException)
                 else {"box": x.key, "ok": True, "data": res}) for x, res in got]
        rows, cut = cap_rows(rows)
        ok = sum(1 for r in rows if r["ok"])
        data = {"box": "all", "results": rows, "truncated": cut}
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"{ok} of {len(rows)} boxes "
                                                               "answered.\n" + json.dumps(data, ensure_ascii=False))],
                                    structuredContent=data, isError=ok == 0 and bool(rows))

    async def calendar_post(self, c: Caller, t: Targets, args: dict) -> types.CallToolResult:
        key = args.get("calendar")
        cal = t.find(key) if isinstance(key, str) else None
        if cal is None and isinstance(key, str):
            cal = next((x for x in t.targets if x.id == key), None)
        if cal is None:
            return _text("calendar not in this connection")
        if not cal.calendar:
            return _text(f"{cal.key} is not a content calendar")
        caption = args.get("caption")
        if not isinstance(caption, str) or not caption.strip():
            return _text("caption is required")
        title = args.get("title") if isinstance(args.get("title"), str) and args["title"].strip() else \
            caption.strip().splitlines()[0][:80]
        item: dict[str, Any] = {"type": args.get("type") or "post", "title": title, "caption": caption}
        date = args.get("date")
        if date:
            if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                return _text("date must be YYYY-MM-DD")
            item["planned_date"] = date
        media_url, media_b64 = args.get("media_url"), args.get("media_b64")
        if media_url and media_b64:
            return _text("send media_url or media_b64, not both")
        if media_url:
            if not isinstance(media_url, str) or not media_url.startswith("https://"):
                return _text("media_url must be an https link")
            item["media_url"] = media_url
        upload = None
        if media_b64:
            if not isinstance(media_b64, str) or len(media_b64) > (MAX_MEDIA_BYTES // 3 + 1) * 4 + 4:
                return _text("media_b64 is over 2 MB: use media_url")
            try:
                raw = base64.b64decode(media_b64, validate=True)
            except (binascii.Error, ValueError):
                return _text("media_b64 is not valid base64")
            kind = sniff(raw)
            if kind is None:
                return _text("media_b64 must be a png, jpeg, gif, webp, mp4, mov or webm file")
            upload = {"filename": f"post.{kind[1]}", "content_type": kind[0], "chunk_b64": media_b64, "final": True}

        async def go(s: ClientSession) -> types.CallToolResult:
            if upload is not None:
                up = await raw_call(s, "board_upload", upload)
                if up.isError:
                    return up
                data = _result_data(up)
                uid = data.get("upload_id") if isinstance(data, dict) else None
                if not isinstance(uid, str):
                    return _text("the calendar did not accept the upload")
                item["upload_id"] = uid
            return await raw_call(s, "board_add_item", item)

        try:
            return await self.pool.run(c.token, c.exp, cal, c.legacy, go)
        except BoxError as e:
            return _text(f"{cal.key}: {e}")


def _intro():
    try:
        from aiteam_connector import intro  # optional: present when the connector package is installed alongside
    except Exception:
        return None
    return intro


def build_server(router: Router) -> Server:
    server = Server("aiteam-relay", version=__version__, instructions=INSTRUCTIONS)

    def caller() -> Caller:
        req = server.request_context.request
        c = req.scope.get(SCOPE_KEY) if req is not None else None
        if not isinstance(c, Caller):  # never reached: the HTTP layer refuses unauthenticated requests
            raise PermissionError("not signed in")
        return c

    async def list_tools(_req: types.ListToolsRequest) -> types.ServerResult:
        c = caller()
        try:
            tools = await router.list_tools(c)
        except InvalidGrant:
            tools = []
        except Exception as e:
            log.warning("tools/list failed: %s", type(e).__name__)
            tools = []
        return types.ServerResult(types.ListToolsResult(tools=tools))

    async def call_tool(req: types.CallToolRequest) -> types.ServerResult:
        c = caller()
        name, args = req.params.name, dict(req.params.arguments or {})
        ctx = server.request_context
        ptoken = getattr(ctx.meta, "progressToken", None) if ctx.meta else None

        async def progress(p: float, total: float | None, message: str | None) -> None:
            if ptoken is not None:
                try:
                    await ctx.session.send_progress_notification(ptoken, p, total, message=message,
                                                                 related_request_id=ctx.request_id)
                except Exception:
                    pass
        try:
            res = await router.call(c, name, args, progress=progress if ptoken is not None else None)
        except InvalidGrant:
            res = _text("this connection was revoked or expired: sign in again")
        except Exception as e:
            log.warning("tools/call %s failed: %s", name, type(e).__name__)
            res = _text("aiteam.space could not list the boxes of this connection right now; try again shortly")
        return types.ServerResult(res)

    server.request_handlers[types.ListToolsRequest] = list_tools
    server.request_handlers[types.CallToolRequest] = call_tool

    intro = _intro()
    if intro is not None:
        @server.list_prompts()
        async def _prompts() -> list[types.Prompt]:
            return [intro.intro_prompt()]

        @server.get_prompt()
        async def _get_prompt(name: str, arguments: dict | None) -> types.GetPromptResult:
            if name == intro.intro_prompt().name:
                return intro.intro_prompt_result()
            raise ValueError(f"unknown prompt {name!r}")

        @server.list_resources()
        async def _resources() -> list[types.Resource]:
            return [intro.intro_resource()]

        @server.read_resource()
        async def _read(uri):
            from mcp.server.lowlevel.helper_types import ReadResourceContents
            if str(uri) != intro.INTRO_URI:
                raise ValueError(f"unknown resource {uri}")
            return [ReadResourceContents(content=intro.INTRO, mime_type="text/markdown")]

    return server


async def prefetch(router: Router, c: Caller) -> None:
    """Called by the HTTP layer before the MCP layer, so a revoked or expired grant is a 401 the client acts on and an
    unreachable aiteam.space is a 502, the same answers as the single-box proxy. Raises InvalidGrant / ExchangeError."""
    await router.targets(c)


__all__ = ["Caller", "Router", "build_server", "prefetch", "SCOPE_KEY"]
