"""RELAY_ISSUER_HEADERS_FILE: the access gate headers go on the relay's calls to the issuer only, never to a box."""
import contextlib
import logging
import os

import httpx
import pytest

from aiteam_relay.app import Settings, create_app
from aiteam_relay.auth import Jwks

from .test_multibox import V3, DeadAware, World, session
from .test_multibox import Clock as _Clock
from .test_relay import INIT, FakeJwks, auth, mint

ID = "test-client-id.access"
SECRET = "test-secret-value-0123456789abcdef"
NAMES = ("cf-access-client-id", "cf-access-client-secret")


def ih():
    """The module under test, imported per test (so a relay without it fails test by test, not at collection)."""
    from aiteam_relay import issuer_headers
    return issuer_headers


def headers_file(tmp_path, body=f"CF-Access-Client-Id: {ID}\nCF-Access-Client-Secret: {SECRET}\n", mode=0o600):
    p = tmp_path / "issuer-headers"
    p.write_text(body)
    os.chmod(p, mode)
    return str(p)


def gate(req: httpx.Request) -> bool:
    return req.headers.get("cf-access-client-id") == ID and req.headers.get("cf-access-client-secret") == SECRET


def bare(req: httpx.Request) -> bool:
    return not any(n in req.headers for n in NAMES)


class Seen(httpx.AsyncBaseTransport):
    """Wrap a transport and keep every request that went through it."""

    def __init__(self, inner):
        self.inner, self.requests = inner, []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return await self.inner.handle_async_request(request)


@contextlib.asynccontextmanager
async def multi_relay(world, monkeypatch, tmp_path, **env):
    for k in ("RELAY_PUBLIC_URL", "AITEAM_ISSUER", "RELAY_ISSUER_HEADERS_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RELAY_MULTI_BOX", "on")
    monkeypatch.setenv("RELAY_ISSUER_HEADERS_FILE", headers_file(tmp_path))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    site = Seen(httpx.MockTransport(world.site))
    boxes = Seen(DeadAware(world))
    app = create_app(Settings(), client=httpx.AsyncClient(transport=site), jwks=FakeJwks(), box_transport=boxes,
                     clock=_Clock(), box_timeout=2.0)
    async with contextlib.AsyncExitStack() as st:
        for b in world.boxes.values():
            await st.enter_async_context(b.mgr.run())
        await st.enter_async_context(app.router.lifespan_context(app))
        yield app, site, boxes


async def test_multi_box_headers_reach_the_issuer_only(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    world = World()
    async with multi_relay(world, monkeypatch, tmp_path) as (app, site, boxes):
        async with session(app, V3()) as s:
            names = {t.name for t in (await s.list_tools()).tools}
            r = await s.call_tool("shared", {"box": "box-a"})
            await s.call_tool("boxes_list", {})
    assert {"only_a", "only_b"} <= names and not r.isError
    paths = {q.url.path for q in site.requests}
    assert {"/oauth/targets", "/oauth/token"} <= paths
    assert site.requests and all(gate(q) for q in site.requests)  # every issuer call carries both headers
    assert boxes.requests and all(bare(q) for q in boxes.requests)  # no box request carries either
    assert {q.url.host for q in boxes.requests} >= {"box-a.aiteam.space", "box-b.aiteam.space"}
    assert SECRET not in caplog.text and ID not in caplog.text
    assert "CF-Access-Client-Secret" in caplog.text  # the log names the header, never the value


async def test_single_box_proxy_headers_reach_the_token_route_only(monkeypatch, tmp_path):
    from .test_relay import World as ProxyWorld
    for k in ("RELAY_PUBLIC_URL", "AITEAM_ISSUER", "RELAY_MULTI_BOX"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RELAY_ISSUER_HEADERS_FILE", headers_file(tmp_path))
    w = ProxyWorld()
    out = Seen(httpx.MockTransport(w.handler))
    app = create_app(Settings(), client=httpx.AsyncClient(transport=out), jwks=FakeJwks())
    # a client sending the gate headers itself: they never pass through to the box either
    h = auth(mint()) | {"cf-access-client-id": "from-client", "cf-access-client-secret": "from-client"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mcp.aiteam.space") as c:
        assert (await c.post("/mcp", json=INIT, headers=h)).status_code == 200
        assert (await c.delete("/mcp", headers=h | {"mcp-session-id": "s"})).status_code == 200
    token = [q for q in out.requests if q.url.path == "/oauth/token"]
    box = [q for q in out.requests if q.url.host == "my-son.aiteam.space"]
    assert token and all(gate(q) for q in token)
    assert len(box) == 2 and all(bare(q) for q in box)


async def test_jwks_fetch_carries_the_headers():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"keys": []})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ih().install(http, "https://staging.example.test", {"CF-Access-Client-Id": ID,
                                                                  "CF-Access-Client-Secret": SECRET})
    await Jwks("https://staging.example.test/oauth/jwks", http).get("k1")
    assert len(seen) == 1 and gate(seen[0])


@pytest.mark.parametrize("url", [
    "https://box-a.aiteam.space/mcp",  # a box
    "https://staging.example.test/mcp",  # the issuer host, not an OAuth path
    "https://staging.example.test/oauth/tokenx",
    "http://staging.example.test/oauth/token",  # another scheme
    "https://staging.example.test:8443/oauth/token",  # another port
    "https://evil.example.test/oauth/token",  # another host, same path
    "https://staging.example.test.evil.test/oauth/token",
])
async def test_headers_never_leave_the_issuers_oauth_paths(url):
    seen = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(204)))
    ih().install(http, "https://staging.example.test", {"CF-Access-Client-Id": ID,
                                                                  "CF-Access-Client-Secret": SECRET})
    await http.get(url, headers={"CF-Access-Client-Secret": "set-by-caller"})  # stripped too
    assert bare(seen[0]), url


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("RELAY_ISSUER_HEADERS_FILE", raising=False)
    app = create_app(Settings(), jwks=FakeJwks())
    assert not app.state.http.event_hooks["request"]


@pytest.mark.parametrize("body,mode,why", [
    (f"CF-Access-Client-Id: {ID}\nCF-Access-Client-Secret: {SECRET}\n", 0o644, "mode 644"),
    (f"CF-Access-Client-Id: {ID}\nCF-Access-Client-Secret: {SECRET}\n", 0o640, "mode 640"),
    (f"CF-Access-Client-Id: {ID}\nAuthorization: Bearer {SECRET}\n", 0o600, "only"),
    (f"CF-Access-Client-Id {ID}\n", 0o600, "only"),
    ("CF-Access-Client-Secret:   \n", 0o600, "no usable value"),
    ("# nothing\n\n", 0o600, "no headers"),
])
def test_bad_file_stops_the_start_and_names_no_value(monkeypatch, tmp_path, body, mode, why):
    monkeypatch.setenv("RELAY_ISSUER_HEADERS_FILE", headers_file(tmp_path, body, mode))
    with pytest.raises(ih().IssuerHeadersError) as e:
        create_app(Settings(), jwks=FakeJwks())
    assert why in str(e.value) and SECRET not in str(e.value) and ID not in str(e.value)


def test_missing_file_stops_the_start(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_ISSUER_HEADERS_FILE", str(tmp_path / "nope"))
    with pytest.raises(ih().IssuerHeadersError, match="cannot open"):
        create_app(Settings(), jwks=FakeJwks())


def test_file_parse(tmp_path):
    p = headers_file(tmp_path, f"# staging gate\n\ncf-access-client-id:{ID}\nCF-Access-Client-Secret: {SECRET}\n")
    assert ih().load(p) == {"CF-Access-Client-Id": ID, "CF-Access-Client-Secret": SECRET}
