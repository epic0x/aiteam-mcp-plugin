"""Fake Hermes api_server: the subset of routes the bridge uses, with the real SSE frame shapes.

Frame names and payloads copied from hermes-agent 0.21.5 gateway/platforms/api_server.py
(``_handle_session_chat_stream``): run.started, message.started, tool.started, assistant.delta,
assistant.completed {content}, run.completed, done.
"""
import json
import time

from aiohttp import web

KEY = "son-api-key-for-tests-0123456789"


class FakeHermes:
    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.authors: list[dict] = []

    def _auth(self, req):
        if req.headers.get("Authorization") != f"Bearer {KEY}":
            raise web.HTTPUnauthorized(text=json.dumps({"error": {"message": "Invalid gateway API key", "code": "gateway_auth_failed"}}),
                                       content_type="application/json")

    async def health(self, req):
        return web.json_response({"status": "ok", "platform": "hermes-agent", "version": "0.21.5"})

    async def create(self, req):
        self._auth(req)
        b = await req.json()
        sid = b["id"]
        if sid in self.sessions:
            return web.json_response({"error": {"message": "exists", "code": "session_exists"}}, status=409)
        self.sessions[sid] = {"id": sid, "title": b.get("title"), "source": b.get("source", "api_server"),
                              "last_active": time.time(), "message_count": 0}
        self.messages[sid] = []
        return web.json_response({"object": "hermes.session", "session": self.sessions[sid]}, status=201)

    async def get(self, req):
        self._auth(req)
        s = self.sessions.get(req.match_info["sid"])
        if not s:
            return web.json_response({"error": {"message": "Session not found", "code": "session_not_found"}}, status=404)
        return web.json_response({"object": "hermes.session", "session": s})

    async def list(self, req):
        self._auth(req)
        src = req.query.get("source")
        rows = [s for s in self.sessions.values() if not src or s["source"] == src]
        return web.json_response({"object": "list", "data": rows})

    async def msgs(self, req):
        self._auth(req)
        sid = req.match_info["sid"]
        if sid not in self.sessions:
            return web.json_response({"error": {"message": "Session not found"}}, status=404)
        lim = int(req.query.get("limit", 500))
        return web.json_response({"object": "list", "session_id": sid, "data": self.messages[sid][-lim:]})

    async def stream(self, req):
        self._auth(req)
        sid = req.match_info["sid"]
        if sid not in self.sessions:
            return web.json_response({"error": {"message": "Session not found"}}, status=404)
        b = await req.json()
        self.authors.append(b.get("author"))
        msg = b["message"]
        n = len([m for m in self.messages[sid] if m["role"] == "user"]) + 1
        reply = f"echo #{n}: {msg}"
        self.messages[sid] += [{"role": "user", "content": msg}, {"role": "assistant", "content": reply}]
        self.sessions[sid]["message_count"] = len(self.messages[sid])
        r = web.StreamResponse(headers={"Content-Type": "text/event-stream", "X-Hermes-Session-Id": sid})
        await r.prepare(req)

        async def w(ev, d):
            await r.write(f"event: {ev}\ndata: {json.dumps(d)}\n\n".encode())
        await w("run.started", {"user_message": {"role": "user", "content": msg}})
        await r.write(b": keepalive\n\n")
        await w("message.started", {"message": {"id": "m1", "role": "assistant"}})
        await w("tool.started", {"message_id": "m1", "tool_name": "terminal", "preview": "ls"})
        await w("tool.completed", {"message_id": "m1", "tool_name": "terminal"})
        for part in (reply[: len(reply) // 2], reply[len(reply) // 2:]):
            await w("assistant.delta", {"message_id": "m1", "delta": part})
        await w("assistant.completed", {"message_id": "m1", "content": reply})
        await w("run.completed", {"message_id": "m1", "usage": {}})
        await w("done", {})
        return r

    def app(self):
        a = web.Application()
        a.router.add_get("/v1/health", self.health)
        a.router.add_post("/api/sessions", self.create)
        a.router.add_get("/api/sessions", self.list)
        a.router.add_get("/api/sessions/{sid}", self.get)
        a.router.add_get("/api/sessions/{sid}/messages", self.msgs)
        a.router.add_post("/api/sessions/{sid}/chat/stream", self.stream)
        return a
