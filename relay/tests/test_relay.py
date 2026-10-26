"""Relay tests: local P-256 key + fake JWKS, fake aiteam.space token endpoint and fake Son via httpx.MockTransport."""
import asyncio
import base64
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from aiteam_relay.app import Settings, create_app
from aiteam_relay.exchange import safe_son_url

ISS = "https://aiteam.space"
RES = "https://mcp.aiteam.space/mcp"
SON_URL = "https://my-son.aiteam.space/mcp"
PRM_URL = "https://mcp.aiteam.space/.well-known/oauth-protected-resource/mcp"
KEY = ec.generate_private_key(ec.SECP256R1())
OTHER = ec.generate_private_key(ec.SECP256R1())


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def mint(key=KEY, kid="k1", **over) -> str:
    claims = {"iss": ISS, "aud": RES, "sub": "acct1", "son_id": "s1", "gid": "g1", "client_id": "c1",
              "scope": "son:read son:write", "iat": int(time.time()), "exp": int(time.time()) + 3600, "jti": "j"}
    claims.update(over)
    head = _b64(json.dumps({"alg": "ES256", "kid": kid, "typ": "JWT"}).encode())
    body = _b64(json.dumps(claims).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
    return f"{head}.{body}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


class FakeJwks:
    async def get(self, kid):
        return KEY.public_key() if kid == "k1" else None


class World:
    """Fake aiteam.space token endpoint + fake Son, one MockTransport for the relay's outbound client."""

    def __init__(self):
        self.exchanges = []
        self.upstream = []
        self.exchange_reply = None  # callable(n) -> httpx.Response
        self.son_reply = None  # callable(request, n) -> httpx.Response
        self.son_url = SON_URL
        self.son_token_seq = 0

    def handler(self, req: httpx.Request):
        if req.url.host == "aiteam.space" and req.url.path == "/oauth/token":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            self.exchanges.append(form)
            if self.exchange_reply:
                return self.exchange_reply(len(self.exchanges))
            self.son_token_seq += 1
            return httpx.Response(200, json={
                "access_token": f"son-token-{self.son_token_seq}", "token_type": "Bearer", "expires_in": 300,
                "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "scope": "son:read son:write", "son_mcp_url": self.son_url})
        self.upstream.append(req)
        if self.son_reply:
            return self.son_reply(req, len(self.upstream))
        return httpx.Response(200, headers={"content-type": "application/json", "mcp-session-id": "sess-up"},
                              json={"jsonrpc": "2.0", "id": 1, "result": {}})


@pytest.fixture
def world():
    return World()


@pytest.fixture
def make(world, monkeypatch):
    def _make(**env):
        for k in ("OPENAI_APPS_CHALLENGE", "OPENAI_APPS_CHALLENGE_FILE", "RELAY_PUBLIC_URL", "AITEAM_ISSUER"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        outbound = httpx.AsyncClient(transport=httpx.MockTransport(world.handler))
        app = create_app(Settings(), client=outbound, jwks=FakeJwks())
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mcp.aiteam.space")
    return _make


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}


def auth(tok):
    return {"authorization": f"Bearer {tok}", "content-type": "application/json",
            "accept": "application/json, text/event-stream"}


@pytest.mark.parametrize("path", ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"])
async def test_prm_shape(make, path):
    async with make() as c:
        r = await c.get(path)
    assert r.status_code == 200
    assert r.json() == {"resource": RES, "authorization_servers": [ISS], "scopes_supported": ["son:read", "son:write"],
                        "bearer_methods_supported": ["header"], "resource_name": "aiteam.space"}


async def test_challenge_plain_exact(make):
    async with make(OPENAI_APPS_CHALLENGE="abc123XYZ") as c:
        r = await c.get("/.well-known/openai-apps-challenge")
    assert r.status_code == 200 and r.content == b"abc123XYZ"
    assert r.headers["content-type"].startswith("text/plain")


async def test_challenge_from_file(make, tmp_path):
    f = tmp_path / "ch"
    f.write_text("tok-from-file\n")
    async with make(OPENAI_APPS_CHALLENGE_FILE=str(f)) as c:
        r = await c.get("/.well-known/openai-apps-challenge")
    assert r.content == b"tok-from-file"


async def test_challenge_unset_404(make):
    async with make() as c:
        r = await c.get("/.well-known/openai-apps-challenge")
    assert r.status_code == 404


async def test_healthz(make):
    async with make() as c:
        r = await c.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


async def test_no_token_401(make, world):
    async with make() as c:
        r = await c.post("/mcp", json=INIT)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == f'Bearer resource_metadata="{PRM_URL}"'
    assert not world.exchanges and not world.upstream


@pytest.mark.parametrize("tok", [
    mint(aud="https://my-son.aiteam.space/mcp"),
    mint(iss="https://evil.example.com"),
    mint(exp=int(time.time()) - 120),
    mint(key=OTHER),
    mint(kid="unknown"),
    mint(scope="openid"),
    "not.a.jwt",
    "garbage",
])
async def test_bad_tokens_401_invalid_token(make, world, tok):
    async with make() as c:
        r = await c.post("/mcp", json=INIT, headers=auth(tok))
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == f'Bearer resource_metadata="{PRM_URL}", error="invalid_token"'
    assert not world.exchanges and not world.upstream


async def test_valid_token_exchanged_and_proxied(make, world):
    tok = mint()
    h = auth(tok) | {"cookie": "sid=secret", "x-aiteam-token": "leak", "X-Aiteam-User": "u",
                     "mcp-session-id": "sess-in", "mcp-protocol-version": "2025-06-18", "x-other": "no"}
    async with make() as c:
        r = await c.post("/mcp", json=INIT, headers=h)
    assert r.status_code == 200
    assert r.json()["jsonrpc"] == "2.0"
    assert r.headers["mcp-session-id"] == "sess-up"
    assert world.exchanges == [{"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                                "subject_token": tok,
                                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token"}]
    up = world.upstream[0]
    assert str(up.url) == SON_URL and up.method == "POST"
    assert up.headers["authorization"] == "Bearer son-token-1"
    assert tok not in str(dict(up.headers))
    assert "cookie" not in up.headers and "x-other" not in up.headers
    assert not any(k.lower().startswith("x-aiteam") for k in up.headers)
    assert up.headers["mcp-session-id"] == "sess-in"
    assert up.headers["mcp-protocol-version"] == "2025-06-18"
    assert json.loads(up.content) == INIT


async def test_delete_and_get_forwarded(make, world):
    async with make() as c:
        r1 = await c.delete("/mcp", headers=auth(mint()) | {"mcp-session-id": "s"})
        r2 = await c.get("/mcp", headers=auth(mint()) | {"last-event-id": "e5"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert [u.method for u in world.upstream] == ["DELETE", "GET"]
    assert world.upstream[1].headers["last-event-id"] == "e5"


async def test_cache_hit_one_exchange(make, world):
    tok = mint()
    async with make() as c:
        await c.post("/mcp", json=INIT, headers=auth(tok))
        await c.post("/mcp", json=INIT, headers=auth(tok))
    assert len(world.exchanges) == 1 and len(world.upstream) == 2


async def test_cache_not_shared_between_tokens(make, world):
    async with make() as c:
        await c.post("/mcp", json=INIT, headers=auth(mint(jti="a")))
        await c.post("/mcp", json=INIT, headers=auth(mint(jti="b")))
    assert len(world.exchanges) == 2


async def test_upstream_401_reexchanges_once(make, world):
    world.son_reply = lambda req, n: (httpx.Response(401) if n == 1 else
                                      httpx.Response(200, json={"ok": True}, headers={"content-type": "application/json"}))
    async with make() as c:
        r = await c.post("/mcp", json=INIT, headers=auth(mint()))
    assert r.status_code == 200
    assert len(world.exchanges) == 2
    assert [u.headers["authorization"] for u in world.upstream] == ["Bearer son-token-1", "Bearer son-token-2"]
    assert json.loads(world.upstream[1].content) == INIT


async def test_upstream_401_twice_gives_up(make, world):
    world.son_reply = lambda req, n: httpx.Response(401)
    async with make() as c:
        r = await c.post("/mcp", json=INIT, headers=auth(mint()))
    assert r.status_code == 502
    assert len(world.exchanges) == 2 and len(world.upstream) == 2


async def test_exchange_invalid_grant_401(make, world):
    world.exchange_reply = lambda n: httpx.Response(400, json={"error": "invalid_grant",
                                                               "error_description": "grant revoked"})
    async with make() as c:
        r = await c.post("/mcp", json=INIT, headers=auth(mint()))
    assert r.status_code == 401
    assert 'error="invalid_token"' in r.headers["www-authenticate"]
    assert not world.upstream


async def test_exchange_server_error_502(make, world):
    world.exchange_reply = lambda n: httpx.Response(500, text="boom")
    async with make() as c:
        r = await c.post("/mcp", json=INIT, headers=auth(mint()))
    assert r.status_code == 502 and not world.upstream


@pytest.mark.parametrize("bad", [
    "https://mcp.aiteam.space/mcp",
    "https://evil.example.com/mcp",
    "https://a.b.aiteam.space/mcp",
    "http://my-son.aiteam.space/mcp",
    "https://my-son.aiteam.space:8443/mcp",
    "https://my-son.aiteam.space/other",
    "https://user" + "@my-son.aiteam.space/mcp",  # user info in the URL
    "https://my-son.aiteam.space.evil.example.com/mcp",
    "https://aiteam.space/mcp",
    "https://127.0.0.1/mcp",
    None,
])
async def test_ssrf_guard(make, world, bad):
    world.son_url = bad
    async with make() as c:
        r = await c.post("/mcp", json=INIT, headers=auth(mint()))
    assert r.status_code == 502
    assert not world.upstream


def test_safe_son_url_accepts_single_label():
    assert safe_son_url("https://my-son.aiteam.space/mcp", suffix=".aiteam.space", relay_host="mcp.aiteam.space") \
        == "https://my-son.aiteam.space/mcp"
    assert safe_son_url("https://MCP.aiteam.space/mcp", suffix=".aiteam.space", relay_host="mcp.aiteam.space") is None


async def test_mcp_no_redirect(make):
    async with make() as c:
        r = await c.post("/mcp", json=INIT)
        r2 = await c.post("/mcp/", json=INIT)
    assert r.status_code == 401
    assert r2.status_code == 404  # never a 307 to another path


async def test_sse_streams_without_buffering(world, monkeypatch):
    """The first SSE event reaches the client before the Son has finished the response."""
    for k in ("RELAY_PUBLIC_URL", "AITEAM_ISSUER"):
        monkeypatch.delenv(k, raising=False)
    gate = asyncio.Event()

    async def events():
        yield b"event: message\ndata: {\"n\":1}\n\n"
        await asyncio.wait_for(gate.wait(), 5)
        yield b"event: message\ndata: {\"n\":2}\n\n"

    world.son_reply = lambda req, n: httpx.Response(200, headers={"content-type": "text/event-stream",
                                                                  "mcp-session-id": "sse-1"}, content=events())
    app = create_app(Settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(world.handler)),
                     jwks=FakeJwks())
    tok = mint()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "https",
             "path": "/mcp", "raw_path": b"/mcp", "query_string": b"", "root_path": "",
             "headers": [(b"host", b"mcp.aiteam.space"), (b"authorization", f"Bearer {tok}".encode()),
                         (b"content-type", b"application/json"), (b"accept", b"text/event-stream")],
             "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8790)}
    body = json.dumps(INIT).encode()
    sent = {"done": False}

    async def receive():
        if not sent["done"]:
            sent["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(10)
        return {"type": "http.disconnect"}

    start, chunks = {}, []

    async def send(msg):
        if msg["type"] == "http.response.start":
            start.update(msg)
        elif msg["type"] == "http.response.body" and msg.get("body"):
            chunks.append(msg["body"])
            gate.set()  # release the Son's second event only after the first reached the client

    await asyncio.wait_for(app(scope, receive, send), 10)
    assert start["status"] == 200
    hdrs = {k.decode(): v.decode() for k, v in start["headers"]}
    assert hdrs["content-type"].startswith("text/event-stream") and hdrs["mcp-session-id"] == "sse-1"
    assert len(chunks) >= 2 and b"".join(chunks).count(b"event: message") == 2
    assert b'"n":1' in chunks[0]


async def test_tokens_never_logged(make, world, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    tok = mint()
    world.son_reply = lambda req, n: httpx.Response(401) if n == 1 else httpx.Response(200, json={})
    async with make() as c:
        await c.post("/mcp", json=INIT, headers=auth(tok))
    text = caplog.text
    assert tok not in text and "son-token-1" not in text and "son-token-2" not in text
    assert logging.getLogger("httpx").level == logging.WARNING
