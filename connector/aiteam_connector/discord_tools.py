"""Full Discord control over MCP.

Before this, every *read* tool (messages_read, conversations_list, events_*) only saw the Son's own Hermes
sessions, so reading any other Discord channel/thread failed with "Conversation not found". Only the webhook
send path talked to Discord directly. These tools use the same bot token as the relay:

  discord_read      latest messages of any channel or thread (before/after/around paging)
  discord_threads   active threads of a server (optionally one channel), plus archived ones of a channel
  discord_channels  servers the bot is in and their channels
  discord_api       any Discord REST call (GET/POST/PATCH/PUT/DELETE): edit, delete, react, pin, create
                    threads/channels, members, roles... Full control, bot permissions are the only limit.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

import httpx
from mcp import types

API = "https://discord.com/api/v10"
ID = {"type": "string", "pattern": "^[0-9]{15,22}$"}
TARGET_RX = re.compile(r"^(?:discord:)?(?P<chat>\d{15,22})(?::(?P<thread>\d{15,22}))?$")
DISCORD_TOOLS = {"discord_read", "discord_threads", "discord_channels", "discord_api"}


def discord_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="discord_read",
            description="Read messages from ANY Discord channel or thread the bot can see (not only the Son's own "
                        "sessions). target: discord:<channel_id>, discord:<channel_id>:<thread_id> or a bare id. "
                        "Newest first unless after is set. Returns author, content, attachments, embeds, reply ref.",
            inputSchema={"type": "object", "required": ["target"], "properties": {
                "target": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
                "before": ID, "after": ID, "around": ID}},
            annotations=types.ToolAnnotations(readOnlyHint=True)),
        types.Tool(
            name="discord_threads",
            description="List Discord threads: all active threads in a server (guild_id), optionally only those under "
                        "channel_id; with channel_id + archived=true, the archived public threads of that channel.",
            inputSchema={"type": "object", "properties": {
                "guild_id": ID, "channel_id": ID, "archived": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}}},
            annotations=types.ToolAnnotations(readOnlyHint=True)),
        types.Tool(
            name="discord_channels",
            description="List the Discord servers the bot is in, or the channels of one server (guild_id).",
            inputSchema={"type": "object", "properties": {"guild_id": ID}},
            annotations=types.ToolAnnotations(readOnlyHint=True)),
        types.Tool(
            name="discord_api",
            description="Call any Discord REST endpoint as the bot (full control): method + path relative to "
                        "/api/v10, e.g. GET /channels/{id}/messages/{mid}, PATCH /channels/{id}/messages/{mid} "
                        "{content}, DELETE /channels/{id}/messages/{mid}, PUT /channels/{id}/messages/{mid}/reactions/"
                        "%F0%9F%91%8D/@me, PUT /channels/{id}/pins/{mid}, POST /channels/{id}/threads {name}, "
                        "GET /guilds/{id}/members?limit=100. Returns status + JSON body.",
            inputSchema={"type": "object", "required": ["method", "path"], "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"]},
                "path": {"type": "string", "description": "Starts with /, e.g. /channels/123/messages"},
                "query": {"type": "object", "description": "Query string parameters"},
                "body": {"type": "object", "description": "JSON body for POST/PATCH/PUT"},
                "reason": {"type": "string", "description": "Audit-log reason (X-Audit-Log-Reason)"}}}),
    ]


def _slim(m: dict) -> dict:
    a = m.get("author") or {}
    out = {"id": m.get("id"), "at": m.get("timestamp"), "edited": m.get("edited_timestamp"),
           "author": a.get("global_name") or a.get("username"), "author_id": a.get("id"),
           "bot": bool(a.get("bot")), "webhook": bool(m.get("webhook_id")), "content": m.get("content", "")}
    if m.get("attachments"):
        out["attachments"] = [{"name": x.get("filename"), "url": x.get("url"), "type": x.get("content_type")}
                              for x in m["attachments"]]
    if m.get("embeds"):
        out["embeds"] = [{k: e.get(k) for k in ("title", "description", "url") if e.get(k)} for e in m["embeds"]]
    ref = m.get("referenced_message") or {}
    if ref:
        out["reply_to"] = {"id": ref.get("id"), "author": (ref.get("author") or {}).get("username"),
                           "content": (ref.get("content") or "")[:200]}
    if m.get("thread"):
        out["thread"] = {"id": m["thread"].get("id"), "name": m["thread"].get("name")}
    return out


class DiscordTools:
    def __init__(self, bot_token: str, http: Optional[httpx.AsyncClient] = None):
        self.token = bot_token
        self.http = http or httpx.AsyncClient(timeout=30, headers={"User-Agent": "DiscordBot (aiteam-connector, 1)"})

    async def _req(self, method: str, path: str, query: Optional[dict] = None, body: Any = None,
                   reason: Optional[str] = None) -> httpx.Response:
        h = {"Authorization": f"Bot {self.token}"}
        if reason:
            h["X-Audit-Log-Reason"] = reason
        if not path.startswith("/"):
            path = "/" + path
        path = path.removeprefix("/api/v10")
        return await self.http.request(method, API + path, params=query or None, headers=h,
                                       json=body if body is not None and method in ("POST", "PATCH", "PUT") else None)

    async def _json(self, method: str, path: str, **kw) -> Any:
        r = await self._req(method, path, **kw)
        if r.status_code >= 400:
            raise ValueError(f"Discord {r.status_code} on {method} {path}: {r.text[:300]}")
        return r.json() if r.content else {}

    async def call(self, name: str, a: dict) -> tuple[Any, bool]:
        if name == "discord_read":
            m = TARGET_RX.match(str(a.get("target", "")).strip())
            if not m:
                return {"error": "target must be discord:<channel_id>[:<thread_id>] or a numeric id"}, True
            ch = m["thread"] or m["chat"]
            q = {"limit": int(a.get("limit") or 30)}
            for k in ("before", "after", "around"):
                if a.get(k):
                    q[k] = str(a[k])
            msgs = await self._json("GET", f"/channels/{ch}/messages", query=q)
            return {"channel": ch, "count": len(msgs), "messages": [_slim(x) for x in msgs]}, False
        if name == "discord_threads":
            g, c, lim = a.get("guild_id"), a.get("channel_id"), int(a.get("limit") or 50)
            if c and a.get("archived"):
                d = await self._json("GET", f"/channels/{c}/threads/archived/public", query={"limit": lim})
                th = d.get("threads", [])
            else:
                if not g and c:
                    g = (await self._json("GET", f"/channels/{c}")).get("guild_id")
                if not g:
                    return {"error": "pass guild_id (or channel_id)"}, True
                th = (await self._json("GET", f"/guilds/{g}/threads/active")).get("threads", [])
                if c:
                    th = [t for t in th if str(t.get("parent_id")) == str(c)]
            th = th[:lim]
            return {"count": len(th), "threads": [
                {"id": t["id"], "name": t.get("name"), "parent_id": t.get("parent_id"),
                 "messages": t.get("message_count"), "archived": (t.get("thread_metadata") or {}).get("archived"),
                 "target": f"discord:{t.get('parent_id')}:{t['id']}"} for t in th]}, False
        if name == "discord_channels":
            if not a.get("guild_id"):
                gs = await self._json("GET", "/users/@me/guilds")
                return {"guilds": [{"id": g["id"], "name": g["name"]} for g in gs]}, False
            chs = await self._json("GET", f"/guilds/{a['guild_id']}/channels")
            return {"channels": [{"id": c["id"], "name": c.get("name"), "type": c.get("type"),
                                  "parent_id": c.get("parent_id"), "target": f"discord:{c['id']}"}
                                 for c in sorted(chs, key=lambda c: (c.get("position") or 0))]}, False
        if name == "discord_api":
            method = str(a.get("method", "GET")).upper()
            r = await self._req(method, str(a.get("path", "")), query=a.get("query"), body=a.get("body"),
                                reason=a.get("reason"))
            try:
                body = r.json() if r.content else None
            except ValueError:
                body = r.text[:2000]
            return {"status": r.status_code, "body": body}, r.status_code >= 400
        return {"error": f"unknown tool {name}"}, True


def dump(obj: Any) -> str:
    return obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=1)
