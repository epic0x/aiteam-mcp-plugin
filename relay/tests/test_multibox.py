"""Multi-box relay (RELAY_MULTI_BOX=on): a real MCP client session against the relay ASGI app, a fake aiteam.space
(targets + token exchange, every call recorded) and two fake boxes that are real in-process MCP servers."""
import asyncio
import base64
import contextlib
import json
import time
from urllib.parse import parse_qs

import httpx
import mcp.types as types
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from aiteam_relay.app import Settings, create_app
from aiteam_relay.multibox import cap_rows, sniff
from aiteam_relay.targets import MAX_TARGETS, parse_targets

from .test_relay import FakeJwks, mint

A_URL, B_URL, CAL_URL = "https://box-a.aiteam.space/mcp", "https://box-b.aiteam.space/mcp", \
    "https://calendar-team.aiteam.space/mcp"
SON_A, SON_B, CAL = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222", \
    "33333333-3333-3333-3333-333333333333"


def target(tid, slug, url, **kw):
    return {"kind": "son", "id": tid, "slug": slug, "name": slug.title(), "project_id": "t1",
            "project_name": "Team", "mcp_url": url, **kw}


RO = types.ToolAnnotations(readOnlyHint=True, destructiveHint=False)
RW = types.ToolAnnotations(readOnlyHint=False, destructiveHint=True)


class FakeBox:
    """A real MCP server (low-level SDK, stateless Streamable HTTP) with a fixed tool set; records every call."""

    def __init__(self, name, tools):
        self.name, self.tools = name, tools  # tools: {name: (annotations, handler(args) -> dict)}
        self.calls, self.auth_seen = [], []
        self.slow = 0.0
        self.init_delay = 0.0  # seconds before answering MCP initialize
        self.list_delay = 0.0  # seconds before answering tools/list
        self.lists = 0  # tools/list requests received over HTTP
        self.server = Server(name)

        @self.server.list_tools()
        async def _list():
            if self.slow or self.list_delay:
                await asyncio.sleep(self.slow or self.list_delay)
            return [types.Tool(name=n, description=f"{n} on {name}", annotations=a,
                               inputSchema={"type": "object", "properties": {"x": {"type": "string"}}})
                    for n, (a, _) in self.tools.items()]

        @self.server.call_tool(validate_input=False)
        async def _call(n, args):
            self.calls.append((n, dict(args)))
            if self.slow:
                await asyncio.sleep(self.slow)
            if n not in self.tools:
                return types.CallToolResult(content=[types.TextContent(type="text", text="unknown tool")],
                                            isError=True)
            data = self.tools[n][1](args)
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(data))],
                                        structuredContent=data)

        self.mgr = StreamableHTTPSessionManager(app=self.server, stateless=True)


class World:
    def __init__(self):
        self.targets = [target(SON_A, "box-a", A_URL), target(SON_B, "box-b", B_URL)]
        self.targets_calls, self.exchanges, self.proxied = [], [], []
        self.exchange_reply = None
        self.targets_status = 200
        self.targets_raise = None  # an httpx error class: /oauth/targets unreachable
        self.targets_timeouts = []  # the read timeout the relay set on each GET /oauth/targets
        self.grant_v = None  # set: /oauth/targets says so, and a v2/v3 grant needs `target` (as on aiteam.space)
        self.exchange_delay = 0.0  # seconds before each token exchange answers
        self.seq = 0
        self.issued = {}  # box host -> set of valid tokens
        self.dead = set()  # box hosts that refuse connections
        self.reject_next = set()  # box hosts answering one 401
        self.a = FakeBox("a", {
            "shared": (RO, lambda a: {"from": "a", "args": a}),
            "only_a": (RW, lambda a: {"from": "a"}),
            "box_health": (RO, lambda a: {"ok": True, "gateway_alive": True}),
            "kanban_list": (RO, lambda a: {"count": 1, "cards": [{"id": "c1", "title": "A card"}]}),
            "kanban_read": (RO, lambda a: {"card": {"id": a.get("id"), "on": "a"}}),
            "memory_read": (RO, lambda a: {"count": 1, "entries": [{"text": "a note"}]}),
        })
        self.b = FakeBox("b", {
            "shared": (RW, lambda a: {"from": "b", "args": a}),
            "only_b": (RO, lambda a: {"from": "b"}),
            "box_health": (RO, lambda a: {"ok": True, "gateway_alive": True}),
            "kanban_list": (RO, lambda a: {"count": 2, "cards": [{"id": "c2"}, {"id": "c3"}]}),
        })
        self.cal = FakeBox("cal", {
            "board_upload": (RW, lambda a: {"upload_id": "up1", "done": True}),
            "board_add_item": (RW, lambda a: {"item": {"id": "it1", "status": "pending" if a.get("planned_date")
                                                       else "draft", "caption": a.get("caption")}}),
            "board_mark_published": (RW, lambda a: {"never": True}),
        })
        self.boxes = {"box-a.aiteam.space": self.a, "box-b.aiteam.space": self.b,
                      "calendar-team.aiteam.space": self.cal}

    # aiteam.space
    async def site(self, req: httpx.Request):
        if req.url.path == "/oauth/targets":
            self.targets_calls.append(req.headers.get("authorization"))
            self.targets_timeouts.append((req.extensions.get("timeout") or {}).get("read"))
            if self.targets_raise:
                raise self.targets_raise("targets down")
            if self.targets_status != 200:
                return httpx.Response(self.targets_status, json={"error": "invalid_grant"} if
                                      self.targets_status == 401 else {"error": "x"})
            body = {"targets": self.targets, "scope_rule": "all"}
            if self.grant_v is not None:
                body["grant_v"] = self.grant_v
            return httpx.Response(200, json=body)
        if req.url.path == "/oauth/token":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            self.exchanges.append(form)
            if self.exchange_delay:
                await asyncio.sleep(self.exchange_delay)
            if self.grant_v in (2, 3) and not form.get("target"):
                # the grant row decides, not the token's claims (exchangeScope / the v2 branch of exchangeRelayToken)
                return httpx.Response(400, json={"error": "invalid_target", "error_description": "target is required"})
            if self.exchange_reply:
                r = self.exchange_reply(form)
                if r is not None:
                    return r
            t = next((x for x in self.targets if x["id"] == form.get("target")), None)
            if form.get("target") and t is None:
                return httpx.Response(403, json={"error": "invalid_target", "error_description": "not in scope"})
            if t is None:
                t = self.targets[0]
            self.seq += 1
            tok = f"box-token-{self.seq}"
            host = httpx.URL(t["mcp_url"]).host
            self.issued.setdefault(host, set()).add(tok)
            return httpx.Response(200, json={"access_token": tok, "token_type": "Bearer", "expires_in": 300,
                                             "son_mcp_url": t["mcp_url"], "target_kind": t["kind"]})
        self.proxied.append(req)  # flag off: the old stream proxy sends the raw request here
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    # every box, one ASGI app keyed by Host
    async def box_app(self, scope, receive, send):
        host = dict(scope["headers"]).get(b"host", b"").decode()
        box = self.boxes.get(host)
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        if box is not None:
            box.auth_seen.append(auth)
        tok = auth[7:] if auth.startswith("Bearer ") else ""
        if box is None or tok not in self.issued.get(host, set()) or host in self.reject_next:
            self.reject_next.discard(host)
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"no"})
            return
        if scope.get("method") == "POST":
            first = await receive()
            body = first.get("body", b"")
            if b'"tools/list"' in body:
                box.lists += 1  # a tools/list request from the relay (the SDK also calls _list itself on calls)
            if box.init_delay and b'"initialize"' in body:
                await asyncio.sleep(box.init_delay)
            queued = [first]

            async def replay():
                return queued.pop() if queued else await receive()
            await box.mgr.handle_request(scope, replay, send)
            return
        await box.mgr.handle_request(scope, receive, send)


class DeadAware(httpx.AsyncBaseTransport):
    def __init__(self, world):
        self.world = world
        self.inner = httpx.ASGITransport(app=world.box_app)

    async def handle_async_request(self, request):
        if request.url.host in self.world.dead:
            raise httpx.ConnectError("refused")
        return await self.inner.handle_async_request(request)


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


@pytest.fixture
def world():
    return World()


@pytest.fixture
def clock():
    return Clock()


TIMEOUT_ENV = ("RELAY_BOX_TIMEOUT", "RELAY_TARGETS_TIMEOUT", "RELAY_FANOUT_TIMEOUT", "RELAY_LIST_TIMEOUT",
               "RELAY_TOOLS_TTL")


@contextlib.asynccontextmanager
async def relay(world, clock, monkeypatch, *, box_timeout=2.0, multi="on", env=None):
    """box_timeout=None: every limit comes from Settings (the environment, then the defaults)."""
    for k in ("RELAY_PUBLIC_URL", "AITEAM_ISSUER", "RELAY_MULTI_BOX", *TIMEOUT_ENV):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RELAY_MULTI_BOX", multi)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, str(v))
    app = create_app(Settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(world.site)),
                     jwks=FakeJwks(), box_transport=DeadAware(world), clock=clock, box_timeout=box_timeout)
    async with contextlib.AsyncExitStack() as st:
        for b in world.boxes.values():
            await st.enter_async_context(b.mgr.run())
        await st.enter_async_context(app.router.lifespan_context(app))
        yield app


@contextlib.asynccontextmanager
async def session(app, tok):
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers={"authorization": f"Bearer {tok}",
                                                                              "cookie": "sid=browser-secret"})
    async with http:
        async with streamable_http_client("https://mcp.aiteam.space/mcp", http_client=http) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                yield s


def V3(**kw):
    return mint(son_id=None, grant_v=3, scope_rule="all", **kw)


def data(res):
    return res.structuredContent if res.structuredContent is not None else json.loads(res.content[0].text)


def all_box_auth(world):
    return [a for b in world.boxes.values() for a in b.auth_seen]


# ---------------------------------------------------------------------------------------------------------- list
async def test_list_union_with_box_enum(world, clock, monkeypatch):
    tok = V3()
    async with relay(world, clock, monkeypatch) as app, session(app, tok) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
    assert {"boxes_list", "kanban_list", "kanban_read", "memory_read", "calendar_post"} <= set(tools)
    assert {"shared", "only_a", "only_b", "box_health"} <= set(tools)
    assert "board_add_item" not in tools  # the calendar box is not in this grant
    sh = tools["shared"]
    assert sh.inputSchema["properties"]["box"]["enum"] == ["box-a", "box-b"]
    assert "box" in sh.inputSchema["required"]
    assert "Available on" not in (sh.description or "")
    assert sh.annotations.readOnlyHint is False and sh.annotations.destructiveHint is True  # least safe of a/b
    assert tools["only_a"].description.endswith("Available on: box-a")
    assert tools["only_b"].annotations.readOnlyHint is True
    assert tools["kanban_list"].inputSchema["properties"]["box"]["enum"] == ["box-a", "box-b", "all"]
    # the relay JWT went to aiteam.space only; boxes saw exchanged tokens only
    assert world.targets_calls and all(a == f"Bearer {tok}" for a in world.targets_calls)
    assert all(a.startswith("Bearer box-token-") for a in all_box_auth(world))
    assert {e["target"] for e in world.exchanges} == {SON_A, SON_B}


async def test_list_tools_cached_per_box(world, clock, monkeypatch):
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        await s.list_tools()
        await app.state.pool.tools_cache.idle()
        n = len(world.a.auth_seen)
        await s.list_tools()
        clock.t += 61
        await s.list_tools()
        await app.state.pool.tools_cache.idle()
        assert len(world.a.auth_seen) == n  # fresh for 5 minutes
        clock.t += 240
        await s.list_tools()  # served from the cache at once, refreshed in the background
        await app.state.pool.tools_cache.idle()
        assert len(world.a.auth_seen) > n


# ---------------------------------------------------------------------------------------------------------- call
async def test_call_routes_to_box_with_exchanged_token(world, clock, monkeypatch):
    tok = V3()
    async with relay(world, clock, monkeypatch) as app, session(app, tok) as s:
        r = await s.call_tool("shared", {"box": "box-b", "x": "1"})
    assert not r.isError and data(r) == {"from": "b", "args": {"x": "1"}}
    assert world.b.calls == [("shared", {"x": "1"})] and world.a.calls == []
    assert any(e.get("target") == SON_B for e in world.exchanges)
    assert tok not in json.dumps(all_box_auth(world))
    assert all("browser-secret" not in a for a in all_box_auth(world))


async def test_box_missing_or_unknown_refused(world, clock, monkeypatch):
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        r1 = await s.call_tool("shared", {"box": "box-z"})
        r2 = await s.call_tool("shared", {})
    assert r1.isError and r1.content[0].text == "box not in this connection"
    assert r2.isError and "box is required" in r2.content[0].text
    assert world.a.calls == [] and world.b.calls == []


async def test_grant_shrinks_after_targets_ttl(world, clock, monkeypatch):
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        assert not (await s.call_tool("only_b", {"box": "box-b"})).isError
        world.targets = [world.targets[0]]  # owner narrowed the connection to box-a
        clock.t += 5
        assert not (await s.call_tool("only_b", {"box": "box-b"})).isError  # still inside the 30 s cache
        clock.t += 30
        r = await s.call_tool("only_b", {"box": "box-b"})
        assert r.isError and r.content[0].text == "box not in this connection"
        tools = {t.name: t for t in (await s.list_tools()).tools}
    assert "only_b" not in tools
    assert "box" not in tools["shared"].inputSchema.get("required", [])  # one box left: box optional


async def test_invalid_target_from_site_is_clean_error(world, clock, monkeypatch):
    world.exchange_reply = lambda form: httpx.Response(403, json={"error": "invalid_target",
                                                                  "error_description": "gone"}) \
        if form.get("target") == SON_B else None
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        r = await s.call_tool("shared", {"box": "box-b"})
        ok = await s.call_tool("shared", {"box": "box-a"})
    assert r.isError and r.content[0].text == "box-b: box not in this connection"
    assert not ok.isError
    assert world.b.calls == []


async def test_box_401_drops_only_that_target_and_retries_once(world, clock, monkeypatch):
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        await s.call_tool("shared", {"box": "box-a"})
        await s.call_tool("shared", {"box": "box-b"})
        before = [e["target"] for e in world.exchanges]
        world.reject_next.add("box-b.aiteam.space")
        r = await s.call_tool("shared", {"box": "box-b"})
        await s.call_tool("shared", {"box": "box-a"})
    assert not r.isError
    after = [e["target"] for e in world.exchanges][len(before):]
    assert after == [SON_B]  # box-a's cached token survived


async def test_revoked_grant_is_401(world, clock, monkeypatch):
    world.targets_status = 401
    async with relay(world, clock, monkeypatch) as app:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mcp.aiteam.space") as c:
            r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                             headers={"authorization": f"Bearer {V3()}",
                                      "accept": "application/json, text/event-stream"})
    assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"]
    assert not all_box_auth(world)


# ---------------------------------------------------------------------------------------------------------- fan-out
async def test_dead_box_partial_boxes_list(world, clock, monkeypatch):
    world.dead.add("box-b.aiteam.space")
    async with relay(world, clock, monkeypatch, box_timeout=1.0) as app, session(app, V3()) as s:
        t0 = time.monotonic()
        r = await s.call_tool("boxes_list", {})
        took = time.monotonic() - t0
        ok = await s.call_tool("shared", {"box": "box-a"})
    rows = {row["box"]: row for row in data(r)["boxes"]}
    assert rows["box-a"]["ok"] is True and rows["box-a"]["health"]["gateway_alive"] is True
    assert rows["box-b"]["ok"] is False and rows["box-b"]["error"]
    assert took < 5 and not ok.isError


async def test_boxes_list_without_box_health_pings(world, clock, monkeypatch):
    del world.b.tools["box_health"]
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        r = await s.call_tool("boxes_list", {})
    rows = {row["box"]: row for row in data(r)["boxes"]}
    assert rows["box-b"]["ok"] is True and "no box_health" in rows["box-b"]["health"]["note"]
    assert data(r)["scope_rule"] == "all" and data(r)["count"] == 2


async def test_progress_is_relayed(world, clock, monkeypatch):
    async def slow_job(name, args):
        ctx = world.a.server.request_context
        await ctx.session.send_progress_notification(ctx.meta.progressToken, 1, 2, message="half",
                                                     related_request_id=ctx.request_id)
        return types.CallToolResult(content=[types.TextContent(type="text", text="done")])

    orig = world.a.server.request_handlers[types.CallToolRequest]

    async def handler(req):
        if req.params.name == "job":
            return types.ServerResult(await slow_job(req.params.name, req.params.arguments))
        return await orig(req)
    world.a.server.request_handlers[types.CallToolRequest] = handler
    world.a.tools["job"] = (RW, lambda a: {})
    seen = []

    async def on_progress(p, total, message):
        seen.append((p, total, message))
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        r = await s.call_tool("job", {"box": "box-a"}, progress_callback=on_progress)
    assert r.content[0].text == "done"
    assert seen == [(1, 2, "half")]


async def test_slow_box_times_out_others_answer(world, clock, monkeypatch):
    world.b.slow = 5
    async with relay(world, clock, monkeypatch, box_timeout=0.5) as app, session(app, V3()) as s:
        t0 = time.monotonic()
        r = await s.call_tool("kanban_list", {"box": "all"})
        took = time.monotonic() - t0
        tools = {t.name for t in (await s.list_tools()).tools}
    res = {row["box"]: row for row in data(r)["results"]}
    assert res["box-a"]["ok"] and res["box-a"]["data"]["count"] == 1
    assert not res["box-b"]["ok"] and "no answer" in res["box-b"]["error"]
    assert took < 3
    assert "only_a" in tools and "only_b" not in tools  # a box never loaded is left out until its list arrives


async def test_kanban_fanout_all_partial(world, clock, monkeypatch):
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        r = await s.call_tool("kanban_read", {"box": "all", "id": "c1"})
        one = await s.call_tool("kanban_list", {"box": "box-b"})
        last_b = world.b.calls[-1]
        mem = await s.call_tool("memory_read", {"box": "all", "query": "note"})
    res = {row["box"]: row for row in data(r)["results"]}
    assert res["box-a"]["ok"] and res["box-a"]["data"]["card"]["id"] == "c1"
    assert res["box-b"] == {"box": "box-b", "ok": False, "error": "kanban_read is not available on this box"}
    assert not r.isError
    assert data(one)["count"] == 2 and last_b == ("kanban_list", {})
    assert [c[0] for c in world.b.calls] == ["kanban_list"]  # never asked box-b for a tool it does not have
    mres = {row["box"]: row for row in data(mem)["results"]}
    assert mres["box-a"]["ok"] and not mres["box-b"]["ok"]


# ---------------------------------------------------------------------------------------------------------- calendar
async def test_calendar_post_only_on_calendar_box(world, clock, monkeypatch):
    world.targets.append(target(CAL, "calendar-team", CAL_URL))
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
        r = await s.call_tool("calendar_post", {"calendar": "calendar-team", "caption": "Launch day\nmore",
                                                "date": "2026-11-20", "media_b64": png})
        r2 = await s.call_tool("calendar_post", {"calendar": "box-a", "caption": "x"})
    assert tools["calendar_post"].inputSchema["properties"]["calendar"]["enum"] == ["calendar-team"]
    assert not r.isError and data(r)["item"]["status"] == "pending"
    names = [c[0] for c in world.cal.calls]
    assert names == ["board_upload", "board_add_item"]
    add = world.cal.calls[1][1]
    assert add == {"type": "post", "title": "Launch day", "caption": "Launch day\nmore", "planned_date": "2026-11-20",
                   "upload_id": "up1"}
    assert "board_mark_published" not in names
    assert r2.isError and "not a content calendar" in r2.content[0].text
    assert world.a.calls == [] and world.b.calls == []


async def test_calendar_post_refused_outside_grant(world, clock, monkeypatch):
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        r = await s.call_tool("calendar_post", {"calendar": "calendar-team", "caption": "hi"})
    assert r.isError and r.content[0].text == "calendar not in this connection"
    assert world.cal.calls == [] and not world.cal.auth_seen
    assert all(e.get("target") != CAL for e in world.exchanges)


# ---------------------------------------------------------------------------------------------------------- legacy
async def test_v1_grant_one_box_optional_no_target(world, clock, monkeypatch):
    world.targets = [world.targets[0]]
    tok = mint()  # grant_v 1: son_id claim, no grant_v
    async with relay(world, clock, monkeypatch) as app, session(app, tok) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
        r = await s.call_tool("shared", {"x": "y"})
    assert "box" not in tools["shared"].inputSchema.get("required", [])
    assert not r.isError and world.a.calls == [("shared", {"x": "y"})]
    assert world.exchanges and all("target" not in e for e in world.exchanges)  # today's exchange form


async def test_v1_grant_without_targets_endpoint(world, clock, monkeypatch):
    world.targets_status = 404  # aiteam.space deploy without /oauth/targets
    async with relay(world, clock, monkeypatch) as app, session(app, mint()) as s:
        r = await s.call_tool("only_a", {})
    assert not r.isError and world.a.calls == [("only_a", {})]


V1_TARGETS_FAILURES = [("missing", 404, None), ("server error", 500, None), ("bad gateway", 502, None),
                       ("refused", 401, None), ("unreachable", 200, httpx.ConnectError),
                       ("timeout", 200, httpx.ReadTimeout)]


@pytest.mark.parametrize("why,status,exc", V1_TARGETS_FAILURES, ids=[f[0] for f in V1_TARGETS_FAILURES])
async def test_v1_grant_survives_any_targets_failure(world, clock, monkeypatch, why, status, exc):
    # A single-box grant must keep working whatever /oauth/targets does: it never needed it.
    world.targets_status, world.targets_raise = status, exc
    world.targets = [world.targets[0]]
    async with relay(world, clock, monkeypatch) as app, session(app, mint()) as s:
        names = {t.name for t in (await s.list_tools()).tools}
        r = await s.call_tool("only_a", {})
    assert world.targets_calls, "the relay asked for targets first"
    assert "only_a" in names and not r.isError and world.a.calls == [("only_a", {})]
    assert world.exchanges and all("target" not in e for e in world.exchanges)  # today's exchange form


async def test_v1_grant_revoked_is_still_401_when_targets_fails(world, clock, monkeypatch):
    world.targets_status = 500
    world.exchange_reply = lambda form: httpx.Response(400, json={"error": "invalid_grant",
                                                                  "error_description": "grant revoked"})
    async with relay(world, clock, monkeypatch) as app:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mcp.aiteam.space") as c:
            r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                             headers={"authorization": f"Bearer {mint()}",
                                      "accept": "application/json, text/event-stream"})
    assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"]
    assert not all_box_auth(world)


@pytest.mark.parametrize("grant", ["v2", "v3"])
@pytest.mark.parametrize("status,exc", [(500, None), (404, None), (200, httpx.ConnectError)])
async def test_scoped_grant_fails_closed_when_targets_fails(world, clock, monkeypatch, grant, status, exc):
    world.targets_status, world.targets_raise = status, exc
    tok = V3() if grant == "v3" else mint(son_id=None, grant_v=2, son_ids=[SON_A, SON_B])
    async with relay(world, clock, monkeypatch) as app:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mcp.aiteam.space") as c:
            r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                             headers={"authorization": f"Bearer {tok}",
                                      "accept": "application/json, text/event-stream"})
    assert r.status_code == 502 and r.json()["error"] == "bad_gateway"
    assert world.exchanges == [] and not all_box_auth(world)  # nothing guessed, no box reached


async def test_flag_off_is_the_old_proxy(world, clock, monkeypatch):
    async with relay(world, clock, monkeypatch, multi="off") as app:
        assert app.state.settings.multi_box is False
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mcp.aiteam.space") as c:
            r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                             headers={"authorization": f"Bearer {mint()}"})
    assert world.targets_calls == []  # the proxy never asks for targets
    assert r.status_code == 200 and r.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert [e.get("target") for e in world.exchanges] == [None]
    assert [str(p.url) for p in world.proxied] == [A_URL]
    assert world.proxied[0].headers["authorization"] == "Bearer box-token-1"


# ---------------------------------------------------------------------------------------------------------- guards
def test_targets_reject_foreign_hosts():
    body = {"targets": [
        target("x1", "evil", "https://evil.example.com/mcp"),
        target("x2", "relay", "https://mcp.aiteam.space/mcp"),
        target("x3", "deep", "https://a.b.aiteam.space/mcp"),
        target("x4", "port", "https://p.aiteam.space:8443/mcp"),
        target("x5", "plain", "http://p.aiteam.space/mcp"),
        target(SON_A, "box-a", A_URL),
        target("x_0123456789ab", "node-1", "https://node-1.aiteam.space/mcp", kind="node"),
    ]}
    t = parse_targets(body, suffix=".aiteam.space", relay_host="mcp.aiteam.space")
    assert t.keys == ["box-a", "node-1"]
    assert t.targets[1].kind == "node"
    assert sorted(r["box"] for r in t.rejected) == ["deep", "evil", "plain", "port", "relay"]


async def test_foreign_host_target_never_called(world, clock, monkeypatch):
    world.targets.append(target("x9", "evil", "https://evil.example.com/mcp"))
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        r = await s.call_tool("shared", {"box": "evil"})
        lst = await s.call_tool("boxes_list", {})
    assert r.isError and r.content[0].text == "box not in this connection"
    rows = {row["box"]: row for row in data(lst)["boxes"]}
    assert rows["evil"] == {"box": "evil", "ok": False, "error": "address not allowed"}
    assert all(e.get("target") != "x9" for e in world.exchanges)


def test_targets_capped_and_slugs_unique():
    many = [target(f"id{i}", "same" if i < 2 else f"b{i}", f"https://b{i}.aiteam.space/mcp") for i in range(80)]
    t = parse_targets({"targets": many}, suffix=".aiteam.space", relay_host="mcp.aiteam.space")
    assert len(t.targets) == MAX_TARGETS and t.truncated
    assert t.keys[:2] == ["same", "id1"]  # a taken slug falls back to the id


def test_calendar_detection():
    t = parse_targets({"targets": [target("c1", "calendar-acme", "https://calendar-acme.aiteam.space/mcp"),
                                   target("c2", "posts", "https://posts.aiteam.space/mcp", template="content-calendar"),
                                   target("s1", "box-a", A_URL)]},
                      suffix=".aiteam.space", relay_host="mcp.aiteam.space")
    assert [x.calendar for x in t.targets] == [True, True, False]


def test_fanout_answer_capped():
    rows = [{"box": f"b{i}", "ok": True, "data": "x" * 1000} for i in range(10)]
    out, cut = cap_rows(rows, limit=3500)
    assert cut and [r.get("truncated", False) for r in out] == [False] * 3 + [True] * 7


def test_least_safe_hints():
    from aiteam_relay.multibox import _least_safe
    m = _least_safe([RO, None])
    assert m.readOnlyHint is None and m.destructiveHint is None  # one box says nothing: not read-only
    m = _least_safe([RO, RO])
    assert m.readOnlyHint is True and m.destructiveHint is None
    m = _least_safe([RO, RW])
    assert m.readOnlyHint is False and m.destructiveHint is True
    assert _least_safe([None, None]) is None


async def test_tokens_never_logged(world, clock, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    tok = V3()
    world.dead.add("box-b.aiteam.space")
    async with relay(world, clock, monkeypatch) as app, session(app, tok) as s:
        world.reject_next.add("box-a.aiteam.space")
        await s.call_tool("shared", {"box": "box-a"})
        await s.call_tool("boxes_list", {})
    assert tok not in caplog.text and "box-token-" not in caplog.text


def test_sniff():
    assert sniff(b"\x89PNG\r\n\x1a\n...") == ("image/png", "png")
    assert sniff(b"\x00\x00\x00\x18ftypisom") == ("video/mp4", "mp4")
    assert sniff(b"#!/bin/sh") is None


# ---------------------------------------------------------------------------------------------------------- slow boxes
# Production: 8 boxes, each needing a token exchange (1 to 2 s) and an MCP initialize before tools/list; with a 5 s
# per-box limit none made it and the list showed only the relay's own tools. Times below are scaled down (0.1 s here
# for 1 s there); `old_limit` is the old 5 s per-box limit at that scale, for a build that reads it from boxes.py.
SLOW = {"RELAY_BOX_TIMEOUT": 3, "RELAY_LIST_TIMEOUT": 2.5, "RELAY_FANOUT_TIMEOUT": 1}


def old_limit(monkeypatch, seconds=0.5):
    from aiteam_relay import boxes
    monkeypatch.setattr(boxes, "BOX_TIMEOUT", seconds, raising=False)


async def settle(app, limit=4.0):
    """Let background tool list loads finish."""
    pool = getattr(app.state, "pool", None)
    if pool is not None and hasattr(pool.tools_cache, "idle"):
        await asyncio.wait_for(pool.tools_cache.idle(), limit)
    else:
        await asyncio.sleep(limit)


def box_tools(world):
    for b in (world.a, world.b):
        b.tools["messages_send"] = (RW, lambda a: {"sent": True})
        b.tools["permissions_list_open"] = (RO, lambda a: {"count": 0, "open": []})


async def test_slow_exchange_and_initialize_still_list_every_box(world, clock, monkeypatch):
    box_tools(world)
    world.exchange_delay = 0.35  # each box: exchange + initialize = 0.65 s, over the old 0.5 s
    world.a.init_delay = world.b.init_delay = 0.3
    old_limit(monkeypatch)
    async with relay(world, clock, monkeypatch, box_timeout=None, env=SLOW) as app, session(app, V3()) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
    for name in ("messages_send", "permissions_list_open", "only_a", "only_b"):
        assert name in tools, name
    ms = tools["messages_send"]
    assert ms.inputSchema["properties"]["box"]["enum"] == ["box-a", "box-b"]
    assert "box" in ms.inputSchema["required"] and "Available on" not in (ms.description or "")
    assert ms.annotations.readOnlyHint is False


async def test_hanging_box_list_is_bounded_then_appears(world, clock, monkeypatch):
    world.b.list_delay = 1.0  # box-b hangs past the whole tools/list bound
    old_limit(monkeypatch, 0.4)
    env = {**SLOW, "RELAY_LIST_TIMEOUT": 0.4}
    async with relay(world, clock, monkeypatch, box_timeout=None, env=env) as app, session(app, V3()) as s:
        t0 = time.monotonic()
        first = {t.name for t in (await s.list_tools()).tools}
        took = time.monotonic() - t0
        await settle(app)  # box-b's load keeps running in the background and lands
        t1 = time.monotonic()
        second = {t.name for t in (await s.list_tools()).tools}
        took2 = time.monotonic() - t1
    assert took < 1.0, "the list waited for the hanging box"
    assert "only_a" in first and "only_b" not in first
    assert "only_b" in second and "only_a" in second
    assert took2 < 0.5  # served from the cache, no wait on box-b


async def test_failing_box_keeps_last_good_list(world, clock, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    async with relay(world, clock, monkeypatch, box_timeout=None, env=SLOW) as app, session(app, V3()) as s:
        await s.list_tools()
        await settle(app)
        world.dead.add("box-b.aiteam.space")
        clock.t += 301  # past the 5 minute freshness: the next list refreshes every box
        during = {t.name for t in (await s.list_tools()).tools}
        await settle(app)  # box-b's refresh fails
        after = {t.name for t in (await s.list_tools()).tools}
        await settle(app)
        cache = getattr(getattr(app.state, "pool", None), "tools_cache", None)
        entries = getattr(cache, "entries", {})
        b_entry = next((e for k, e in entries.items() if k.startswith(B_URL)), None)
    assert "only_b" in during and "only_b" in after
    assert "keeping the last good list" in caplog.text
    assert b_entry is not None and b_entry.stale and "unreachable" in b_entry.error


async def test_call_on_unlisted_box_refreshes_once_then_routes(world, clock, monkeypatch):
    from aiteam_relay.boxes import raw_call  # tools/call as sent by a client that does not list first
    world.dead.add("box-b.aiteam.space")  # box-b's first load fails: the list has nothing from it
    async with relay(world, clock, monkeypatch, box_timeout=None, env=SLOW) as app, session(app, V3()) as s:
        first = {t.name for t in (await s.list_tools()).tools}
        await settle(app)
        world.dead.discard("box-b.aiteam.space")
        n = world.b.lists
        r = await raw_call(s, "only_b", {"box": "box-b"})
        after_call = world.b.lists
        listed = {t.name for t in (await s.list_tools()).tools}
        after_list = world.b.lists
        # a tool added on the box later: one reload, then routed
        world.b.tools["fresh_b"] = (RO, lambda a: {"fresh": True})
        clock.t += 11
        r2 = await raw_call(s, "fresh_b", {"box": "box-b"})
        m = world.b.lists
        # a name the box does not have: no reload storm, the box answers for itself
        await raw_call(s, "nope", {"box": "box-b"})
        await raw_call(s, "nope", {"box": "box-b"})
        m2 = world.b.lists
        names = {t.name for t in (await s.list_tools()).tools}
    assert "only_b" not in first
    assert not r.isError and data(r) == {"from": "b"}
    assert after_call == n + 1, "the call reloaded box-b's list once"
    assert "only_b" in listed and after_list == after_call  # the next list is served from that reload
    assert not r2.isError and data(r2) == {"fresh": True} and m == after_list + 1
    assert m2 == m and "fresh_b" in names


async def test_tools_cache_holds_no_tokens(world, clock, monkeypatch, caplog):
    import hashlib
    import logging
    caplog.set_level(logging.DEBUG)
    tok = V3()
    world.dead.add("box-b.aiteam.space")
    async with relay(world, clock, monkeypatch, box_timeout=None, env=SLOW) as app, session(app, tok) as s:
        await s.list_tools()
        await settle(app)
        world.dead.discard("box-b.aiteam.space")
        clock.t += 31
        await s.list_tools()
        await settle(app)
        await s.call_tool("shared", {"box": "box-a"})
        cache = app.state.pool.tools_cache
        dump = json.dumps([[k, [t.model_dump(mode="json") for t in (e.tools or [])], e.error]
                           for k, e in cache.entries.items()])
    assert len(cache.entries) == 2
    issued = [t for ts in world.issued.values() for t in ts]
    for secret in (tok, hashlib.sha256(tok.encode()).hexdigest(), *issued):
        assert secret not in dump and secret not in caplog.text
    assert "Bearer" not in dump and "box-token-" not in dump
    assert "left out" in caplog.text  # which boxes were missing is logged, by box name only


async def test_tools_cache_is_per_account_and_bounded(world, clock, monkeypatch):
    from aiteam_relay.toolcache import ToolsCache
    async with relay(world, clock, monkeypatch, box_timeout=None, env=SLOW) as app:
        for who in ("acct1", "acct2"):
            async with session(app, V3(sub=who)) as s:
                await s.list_tools()
        await settle(app)
        keys = list(app.state.pool.tools_cache.entries)
    assert len(keys) == 4 and len({k.rsplit("|", 1)[1] for k in keys}) == 2  # one list per box per account
    c = ToolsCache(clock=clock, max_entries=3)

    async def load():
        return []
    for i in range(5):
        await c.start(f"k{i}", load, f"b{i}")
    assert list(c.entries) == ["k2", "k3", "k4"]


# ---------------------------------------------------------------------------------------------------------- settings
def test_timeouts_from_env(monkeypatch):
    for k in TIMEOUT_ENV:
        monkeypatch.delenv(k, raising=False)
    s = Settings()
    assert (s.box_timeout, s.targets_timeout, s.fanout_timeout, s.list_timeout, s.tools_ttl) == \
        (30.0, 15.0, 10.0, 25.0, 300.0)
    for k, v in zip(TIMEOUT_ENV, ("45", "20", "4", "12.5", "60")):
        monkeypatch.setenv(k, v)
    s = Settings()
    assert (s.box_timeout, s.targets_timeout, s.fanout_timeout, s.list_timeout, s.tools_ttl) == \
        (45.0, 20.0, 4.0, 12.5, 60.0)
    for bad in ("", "abc", "0", "-3", "99999"):
        monkeypatch.setenv("RELAY_BOX_TIMEOUT", bad)
        assert Settings().box_timeout == 30.0, bad


async def test_timeouts_reach_the_pool_and_targets_call(world, clock, monkeypatch):
    env = {"RELAY_BOX_TIMEOUT": 4, "RELAY_TARGETS_TIMEOUT": 7, "RELAY_FANOUT_TIMEOUT": 2, "RELAY_LIST_TIMEOUT": 3}
    async with relay(world, clock, monkeypatch, box_timeout=None, env=env) as app, session(app, V3()) as s:
        await s.list_tools()
        pool = app.state.pool
    assert (pool.box_timeout, pool.fanout_timeout, pool.list_timeout) == (4.0, 2.0, 3.0)
    assert world.targets_timeouts and set(world.targets_timeouts) == {7.0}


async def test_fanout_keeps_its_own_short_budget(world, clock, monkeypatch):
    world.b.list_delay = 3.0  # box-b never lists within a fan-out leg
    env = {"RELAY_BOX_TIMEOUT": 5, "RELAY_FANOUT_TIMEOUT": 0.3, "RELAY_LIST_TIMEOUT": 0.3}
    async with relay(world, clock, monkeypatch, box_timeout=None, env=env) as app, session(app, V3()) as s:
        t0 = time.monotonic()
        r = await s.call_tool("kanban_list", {"box": "all"})
        h = await s.call_tool("boxes_list", {})
        took = time.monotonic() - t0
        world.b.list_delay = 0
        await app.state.pool.tools_cache.aclose()
    res = {row["box"]: row for row in data(r)["results"]}
    rows = {row["box"]: row for row in data(h)["boxes"]}
    assert res["box-a"]["ok"] and not res["box-b"]["ok"] and "0.3 s" in res["box-b"]["error"]
    assert rows["box-a"]["ok"] and not rows["box-b"]["ok"]
    assert took < 2.0


# ---------------------------------------------------------------------------------------------------- invalid_target
async def test_widened_single_box_grant_sends_target(world, clock, monkeypatch):
    # The owner widened a one-box connection in Settings (aiteam.space now says grant_v 3); the client still holds a
    # token minted before, with the old single-box claims. An exchange without `target` is refused (invalid_target).
    world.grant_v = 3
    tok = mint()  # v1 claims: son_id, no grant_v
    async with relay(world, clock, monkeypatch) as app, session(app, tok) as s:
        names = {t.name for t in (await s.list_tools()).tools}
        r = await s.call_tool("only_b", {"box": "box-b"})
    assert {"only_a", "only_b"} <= names
    assert not r.isError and data(r) == {"from": "b"}
    assert world.exchanges and all(e.get("target") in (SON_A, SON_B) for e in world.exchanges)


async def test_single_box_grant_keeps_plain_exchange_when_site_says_v1(world, clock, monkeypatch):
    world.grant_v = 1
    world.targets = [world.targets[0]]
    async with relay(world, clock, monkeypatch) as app, session(app, mint()) as s:
        r = await s.call_tool("only_a", {})
    assert not r.isError
    assert world.exchanges and all("target" not in e for e in world.exchanges)


# ---------------------------------------------------------------------------------------------------------- box tools
async def test_box_tools_pass_through_with_box(world, clock, monkeypatch):
    names = ("messages_send", "messages_read", "discord_read", "sessions_list", "permissions_list_open",
             "permissions_respond", "cronjob_manage", "ping")
    for b in (world.a, world.b):
        for n in names:
            b.tools[n] = (RO if n in ("discord_read", "messages_read", "sessions_list", "permissions_list_open",
                                      "ping") else RW, lambda a, n=n: {"tool": n, "args": a})
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
        r = await s.call_tool("discord_read", {"box": "box-b", "x": "c1"})
    for n in names:
        assert tools[n].inputSchema["properties"]["box"]["enum"] == ["box-a", "box-b"], n
    assert data(r) == {"tool": "discord_read", "args": {"x": "c1"}}
    assert world.b.calls[-1] == ("discord_read", {"x": "c1"})
