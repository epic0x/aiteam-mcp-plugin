"""v2 selected-target contract (fake AS and independently addressed MCP Sons)."""
import json
import httpx
import pytest

import base64
import time
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from aiteam_relay.app import Settings, create_app

KEY = ec.generate_private_key(ec.SECP256R1())

class FakeJwks:
    async def get(self, kid):
        return KEY.public_key() if kid == "k1" else None

def mint(**over):
    def b64(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
    claims = {"iss": "https://aiteam.space", "aud": "https://mcp.aiteam.space/mcp", "sub": "acct1",
              "son_id": "s1", "gid": "g1", "client_id": "c1", "scope": "son:read son:write",
              "iat": int(time.time()), "exp": int(time.time()) + 3600, "jti": "j"}
    claims.update(over)
    if claims.get("grant_v") == 2 and claims.get("son_id") is None:
        claims.pop("son_id")
    head = b64(json.dumps({"alg": "ES256", "kid": "k1", "typ": "JWT"}).encode())
    body = b64(json.dumps(claims).encode())
    r, s = decode_dss_signature(KEY.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
    return f"{head}.{body}.{b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"

def auth(tok):
    return {"authorization": f"Bearer {tok}", "content-type": "application/json",
            "accept": "application/json, text/event-stream"}

A, B, C = "son-a", "son-b", "son-c"


def multi(**over):
    return mint(**({"grant_v": 2, "son_ids": [A, B], "son_id": None} | over))


class World:
    def __init__(self):
        self.exchanges = []
        self.upstream = []
        self.revoked = set()
        self.calls = {}

    def handler(self, req):
        if req.url.host == "aiteam.space":
            from urllib.parse import parse_qs
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            self.exchanges.append(form)
            target = form.get("son_id")
            if target not in (A, B) or target in self.revoked:
                return httpx.Response(400, json={"error": "invalid_grant"})
            n = self.calls[target] = self.calls.get(target, 0) + 1
            return httpx.Response(200, json={"access_token": f"exchanged-{target}-{n}",
                "token_type": "Bearer", "expires_in": 300,
                "son_mcp_url": f"https://{target}.aiteam.space/mcp"})
        target = req.url.host.removesuffix(".aiteam.space")
        self.upstream.append((target, req))
        assert req.headers["authorization"].startswith(f"Bearer exchanged-{target}-")
        assert "cookie" not in req.headers and "x-aiteam-user" not in req.headers
        assert "mcp-session-id" not in req.headers and "last-event-id" not in req.headers
        body = json.loads(req.content)
        method = body["method"]
        if method == "server/discover":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "resultType": "complete", "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "instructions": f"{target} instructions", "ttlMs": 10000, "cacheScope": "private"}})
        if method == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [
                {"name": "shared", "description": f"{target} tool", "inputSchema": {"type": "object", "properties": {target: {"type": "string"}}}},
                {"name": f"only_{target}", "inputSchema": {"type": "object", "properties": {}}},
            ]}})
        if method == "tools/call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": target}]}})
        if method in ("resources/list", "prompts/list", "resources/templates/list"):
            key = {"resources/list": "resources", "prompts/list": "prompts", "resources/templates/list": "resourceTemplates"}[method]
            value = ({"uri": f"https://{target}.example/file", "name": "shared"} if key == "resources" else
                     {"name": "shared", "arguments": [{"name": target}]} if key == "prompts" else None)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {key: [value] if value else []}})
        if method == "resources/read":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"contents": [{"uri": body["params"]["uri"], "text": target}]}})
        if method == "prompts/get":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"messages": [{"role": "user", "content": {"type": "text", "text": target}}]}})
        return httpx.Response(400, json={"error": "unexpected method"})


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("RELAY_EXPERIMENTAL_MULTI", "1")
    world = World()
    outbound = httpx.AsyncClient(transport=httpx.MockTransport(world.handler))
    app = create_app(Settings(), client=outbound, jwks=FakeJwks())
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mcp.aiteam.space")
    return world, client


async def rpc(client, token, method, params=None, headers=None):
    p = dict(params or {})
    p["_meta"] = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                  "io.modelcontextprotocol/clientCapabilities": {}}
    h = auth(token) | {"mcp-protocol-version": "2026-07-28", "mcp-method": method}
    if method in ("tools/call", "prompts/get", "resources/read"):
        h["mcp-name"] = p.get("name", p.get("uri", "")) if isinstance(p.get("name", p.get("uri", "")), str) else "invalid"
    return await client.post("/mcp", json={"jsonrpc": "2.0", "id": 12, "method": method, "params": p},
                             headers=h | (headers or {}))


async def test_multi_disabled_by_default_fails_before_exchange(monkeypatch):
    monkeypatch.delenv("RELAY_EXPERIMENTAL_MULTI", raising=False)
    world = World()
    app = create_app(Settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(world.handler)),
                     jwks=FakeJwks())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mcp.aiteam.space") as c:
        r = await rpc(c, multi(), "tools/list")
    assert r.status_code == 501
    assert not world.exchanges and not world.upstream


async def test_mismatched_method_header_never_reaches_any_son(env):
    world, client = env
    async with client:
        r = await rpc(client, multi(), "tools/list", headers={"mcp-method": "tools/call"})
    assert r.status_code == 400
    assert not world.exchanges and not world.upstream


async def test_missing_body_version_never_reaches_any_son(env):
    world, client = env
    async with client:
        r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                              headers=auth(multi()) | {"mcp-method": "tools/list", "mcp-protocol-version": "2026-07-28"})
    assert r.status_code == 400
    assert not world.exchanges and not world.upstream


async def test_tools_preserve_both_colliding_names_and_schemas_and_route_b(env):
    world, client = env
    token = multi()
    async with client:
        listed = await rpc(client, token, "tools/list")
        assert listed.status_code == 200, listed.text
        tools = listed.json()["result"]["tools"]
        assert len(tools) == 4 and len({t["name"] for t in tools}) == 4
        shared_b = next(t for t in tools if t.get("description") == "son-b tool")
        assert shared_b["inputSchema"]["properties"] == {B: {"type": "string"}}
        called = await rpc(client, token, "tools/call", {"name": shared_b["name"], "arguments": {B: "ok"}},
                           {"cookie": "secret", "x-aiteam-user": "secret"})
    assert called.status_code == 200 and called.json()["result"]["content"][0]["text"] == B
    assert [target for target, req in world.upstream if json.loads(req.content)["method"] == "tools/call"] == [B]
    assert {x["son_id"] for x in world.exchanges} == {A, B}
    assert all(x["subject_token"] == token for x in world.exchanges)
    assert not any(target == C for target, _ in world.upstream)


async def test_forged_and_unlisted_tool_names_never_reach_upstream_call(env):
    world, client = env
    token = multi()
    async with client:
        for name in ("shared", "son-c__shared", "son-a__shared", "son_a__x", "son_YQ__e", "", "son-c", 42):
            r = await rpc(client, token, "tools/call", {"name": name, "arguments": {}})
            assert r.status_code in (400, 404), (name, r.text)
    assert not world.exchanges and not world.upstream


@pytest.mark.parametrize("override", [
    {"son_ids": [A, A]}, {"son_ids": []},
    {"son_ids": A}, {"son_ids": [A, 3]}, {"gid": ""}, {"client_id": ""},
    {"sub": ""}, {"son_id": A}, {"grant_v": 1},
])
async def test_malformed_multi_claims_denied_before_exchange(env, override):
    world, client = env
    token = multi(**override)
    async with client:
        r = await rpc(client, token, "tools/list")
    assert r.status_code == 401
    assert not world.exchanges and not world.upstream


async def test_revoked_target_fails_closed_on_fresh_exchange(env):
    world, client = env
    world.revoked.add(B)
    async with client:
        r = await rpc(client, multi(), "tools/list")
    assert r.status_code == 401
    assert all(t != B for t, _ in world.upstream)  # earlier selected Son may have been probed, never B


async def test_resources_and_prompts_are_namespaced_and_dispatched(env):
    world, client = env
    tok = multi()
    async with client:
        resources = (await rpc(client, tok, "resources/list")).json()["result"]["resources"]
        prompts = (await rpc(client, tok, "prompts/list")).json()["result"]["prompts"]
        assert len({r["uri"] for r in resources}) == 2
        assert len({p["name"] for p in prompts}) == 2
        b_resource = next(r for r in resources if B in r["uri"] or r["name"].startswith(B))
        # Target is selected by the opaque namespace, never a caller-supplied unvalidated target header.
        r = await rpc(client, tok, "resources/read", {"uri": b_resource["uri"]})
        assert r.status_code == 200
        assert r.json()["result"]["contents"][0]["uri"] == b_resource["uri"]
        b_prompt = next(p for p in prompts if B in p["name"] or p.get("title") == B)
        r = await rpc(client, tok, "prompts/get", {"name": b_prompt["name"], "arguments": {}})
        assert r.status_code == 200
        assert r.json()["result"]["messages"][0]["content"]["text"] == B
    assert [t for t, req in world.upstream if json.loads(req.content)["method"] in ("resources/read", "prompts/get")] == [B, B]


async def test_cache_separates_selected_sons_and_401_refreshes_only_b(env):
    world, client = env
    token = multi()
    original = world.handler
    bad_once = {B: True}

    def reject_first_b(req):
        if req.url.host == f"{B}.aiteam.space" and bad_once[B]:
            bad_once[B] = False
            world.upstream.append((B, req))
            return httpx.Response(401)
        return original(req)

    client._transport.app.state.http._transport = httpx.MockTransport(reject_first_b)
    async with client:
        r = await rpc(client, token, "tools/list")
        assert r.status_code == 200
        r = await rpc(client, token, "tools/list")
        assert r.status_code == 200
    assert [x["son_id"] for x in world.exchanges] == [A, B, B]
    assert all(target == B for target, req in world.upstream if req.headers["authorization"].endswith(f"{B}-1"))


async def test_unselected_c_ends_discovery_without_partial_list(env):
    world, client = env
    async with client:
        r = await rpc(client, multi(son_ids=[A, B, C]), "tools/list")
    assert r.status_code == 401 and "result" not in r.json()
    assert world.exchanges[-1]["son_id"] == C
    assert all(target in (A, B) for target, _ in world.upstream)


async def test_discovery_aggregates_selected_sons_without_public_cache(env):
    world, client = env
    async with client:
        r = await rpc(client, multi(), "server/discover")
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"] == ["2026-07-28"]
    assert result["capabilities"] == {"tools": {}, "resources": {}, "prompts": {}}
    assert result["cacheScope"] == "private" and result["ttlMs"] == 0
    assert A in result["instructions"] and B in result["instructions"]
    assert [target for target, _ in world.upstream if json.loads(_.content)["method"] == "server/discover"] == [A, B]
    for target in (A, B):
        probed = {json.loads(req.content)["method"] for son, req in world.upstream if son == target}
        assert {"tools/list", "resources/templates/list", "resources/list", "prompts/list"} <= probed
    assert {x["son_id"] for x in world.exchanges} == {A, B}

async def test_discovery_refuses_unsupported_upstream_capability_without_partial_result(env):
    world, client = env
    original = world.handler
    def unsupported(req):
        if req.url.host == f"{B}.aiteam.space" and json.loads(req.content)["method"] == "server/discover":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 12, "result": {
                "resultType": "complete", "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {}, "sampling": {}}, "cacheScope": "private"}})
        return original(req)
    client._transport.app.state.http._transport = httpx.MockTransport(unsupported)
    async with client:
        r = await rpc(client, multi(), "server/discover")
    assert r.status_code == 502 and "result" not in r.json()

async def test_malformed_discovery_version_list_fails_closed(env):
    world, client = env
    original = world.handler
    def malformed(req):
        if req.url.host == f"{B}.aiteam.space" and json.loads(req.content)["method"] == "server/discover":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 12, "result": {
                "resultType": "complete", "supportedVersions": None, "capabilities": {"tools": {}}}})
        return original(req)
    client._transport.app.state.http._transport = httpx.MockTransport(malformed)
    async with client:
        r = await rpc(client, multi(), "server/discover")
    assert r.status_code == 502 and "result" not in r.json()

@pytest.mark.parametrize("broken_method", ["resources/list", "prompts/list"])
async def test_discovery_refuses_sse_only_list_from_any_selected_son(env, broken_method):
    world, client = env
    original = world.handler
    intercepted = []
    def sse_list(req):
        if req.url.host == f"{B}.aiteam.space" and json.loads(req.content)["method"] == broken_method:
            intercepted.append(req)
            # A legal modern response transport, but the relay cannot aggregate it yet.
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                content='data: {"jsonrpc":"2.0","id":12,"result":{}}\n\n')
        return original(req)
    client._transport.app.state.http._transport = httpx.MockTransport(sse_list)
    async with client:
        r = await rpc(client, multi(), "server/discover")
    assert r.status_code == 502 and "result" not in r.json()
    assert len(intercepted) == 1 and intercepted[0].url.host == f"{B}.aiteam.space"

async def test_templates_list_returns_empty_only_if_every_son_has_none(env):
    world, client = env
    async with client:
        r = await rpc(client, multi(), "resources/templates/list")
    assert r.status_code == 200 and r.json()["result"] == {"resourceTemplates": []}
    assert {t for t, req in world.upstream if json.loads(req.content)["method"] == "resources/templates/list"} == {A, B}

async def test_discovery_and_resource_list_refuse_to_hide_son_templates(env):
    world, client = env
    original = world.handler
    def templated(req):
        if req.url.host == f"{B}.aiteam.space" and json.loads(req.content)["method"] == "resources/templates/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 12, "result": {
                "resourceTemplates": [{"uriTemplate": "file:///{name}", "name": "files"}]}})
        return original(req)
    client._transport.app.state.http._transport = httpx.MockTransport(templated)
    async with client:
        for method in ("server/discover", "resources/list", "resources/templates/list"):
            r = await rpc(client, multi(), method)
            assert r.status_code == 502 and "result" not in r.json(), (method, r.text)

async def test_tool_call_streams_request_scoped_sse_and_closes_without_ids(env):
    world, client = env
    original = world.handler
    def streaming(req):
        if req.url.host == f"{B}.aiteam.space" and json.loads(req.content)["method"] == "tools/call":
            return httpx.Response(200, headers={"content-type": "text/event-stream", "mcp-session-id": "upstream-session"},
                content='data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":1,"progressToken":"p"}}\n\n'
                        'data: {"jsonrpc":"2.0","id":12,"result":{"content":[{"type":"text","text":"son-b"}]}}\n\n')
        return original(req)
    client._transport.app.state.http._transport = httpx.MockTransport(streaming)
    async with client:
        tools = (await rpc(client, multi(), "tools/list")).json()["result"]["tools"]
        b = next(x for x in tools if x.get("description") == "son-b tool")
        r = await rpc(client, multi(), "tools/call", {"name": b["name"], "arguments": {}})
    # A session-era response cannot safely be mapped to the stateless downstream.
    assert r.status_code == 502 and "mcp-session-id" not in r.headers

async def test_tool_call_sse_without_upstream_session_delivers_progress_and_final(env):
    world, client = env
    original = world.handler
    def streaming(req):
        if req.url.host == f"{B}.aiteam.space" and json.loads(req.content)["method"] == "tools/call":
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                content='data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":1}}\n\n'
                        'data: {"jsonrpc":"2.0","id":12,"result":{"content":[{"type":"text","text":"son-b"}]}}\n\n')
        return original(req)
    client._transport.app.state.http._transport = httpx.MockTransport(streaming)
    async with client:
        tools = (await rpc(client, multi(), "tools/list")).json()["result"]["tools"]
        b = next(x for x in tools if x.get("description") == "son-b tool")
        r = await rpc(client, multi(), "tools/call", {"name": b["name"], "arguments": {}})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert "mcp-session-id" not in r.headers and "id:" not in r.text
    assert [json.loads(x.removeprefix("data: ")) for x in r.text.splitlines() if x.startswith("data: ")] == [
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}},
        {"jsonrpc": "2.0", "id": 12, "result": {"content": [{"type": "text", "text": B}]}}]

@pytest.mark.parametrize("event", [
    'id: external\ndata: {"jsonrpc":"2.0","id":12,"result":{}}\n\n',
    'data: {"jsonrpc":"2.0","id":999,"result":{}}\n\n',
    'data: {"jsonrpc":"2.0","method":"notifications/tools/list_changed","params":{}}\n\n',
    'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progressToken":"alien"}}\n\n',
    'data: {"jsonrpc":"2.0","id":12,"result":{}}',
])
async def test_sse_rejects_resumable_or_unrelated_messages_before_output(env, event):
    world, client = env
    original = world.handler
    def malicious(req):
        if req.url.host == f"{B}.aiteam.space" and json.loads(req.content)["method"] == "tools/call":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=event)
        return original(req)
    client._transport.app.state.http._transport = httpx.MockTransport(malicious)
    async with client:
        tools = (await rpc(client, multi(), "tools/list")).json()["result"]["tools"]
        b = next(x for x in tools if x.get("description") == "son-b tool")
        r = await rpc(client, multi(), "tools/call", {"name": b["name"], "arguments": {}})
    assert r.status_code == 502 and "mcp-session-id" not in r.headers

async def test_sse_resource_read_rewrites_only_selected_son_uri(env):
    world, client = env
    original = world.handler
    def streaming(req):
        if req.url.host == f"{B}.aiteam.space" and json.loads(req.content)["method"] == "resources/read":
            requested = json.loads(req.content)["params"]["uri"]
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=
                f'data: {json.dumps({"jsonrpc": "2.0", "id": 12, "result": {"contents": [{"uri": requested, "text": B}]}})}\n\n')
        return original(req)
    client._transport.app.state.http._transport = httpx.MockTransport(streaming)
    async with client:
        resources = (await rpc(client, multi(), "resources/list")).json()["result"]["resources"]
        b = next(x for x in resources if x["name"].startswith(B))
        r = await rpc(client, multi(), "resources/read", {"uri": b["uri"]})
    assert r.status_code == 200
    result = json.loads(next(x.removeprefix("data: ") for x in r.text.splitlines() if x.startswith("data: ")))
    assert result["result"]["contents"][0]["uri"] == b["uri"]

async def test_nonascii_upstream_resource_uri_is_header_encoded_safely(env):
    world, client = env
    original = world.handler
    uri = "file:///你好"
    def unicode_resource(req):
        if req.url.host == f"{B}.aiteam.space":
            method = json.loads(req.content)["method"]
            if method == "resources/list":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 12, "result": {
                    "resources": [{"uri": uri, "name": "unicode"}]}})
            if method == "resources/read":
                assert req.headers["mcp-name"].startswith("=?base64?")
                assert req.headers["mcp-name"].endswith("?=")
                assert base64.b64decode(req.headers["mcp-name"][9:-2]).decode() == uri
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 12,
                    "result": {"contents": [{"uri": uri, "text": "ok"}]}})
        return original(req)
    client._transport.app.state.http._transport = httpx.MockTransport(unicode_resource)
    async with client:
        resources = (await rpc(client, multi(), "resources/list")).json()["result"]["resources"]
        item = next(x for x in resources if x["name"].startswith(B))
        r = await rpc(client, multi(), "resources/read", {"uri": item["uri"]})
    assert r.status_code == 200 and r.json()["result"]["contents"][0]["uri"] == item["uri"]

async def test_client_encoded_name_header_matches_alias_body(env):
    world, client = env
    async with client:
        tools = (await rpc(client, multi(), "tools/list")).json()["result"]["tools"]
        b = next(x for x in tools if x.get("description") == "son-b tool")
        encoded = "=?base64?" + base64.b64encode(b["name"].encode()).decode() + "?="
        r = await rpc(client, multi(), "tools/call", {"name": b["name"], "arguments": {}},
            {"mcp-name": encoded})
    assert r.status_code == 200 and r.json()["result"]["content"][0]["text"] == B

async def test_malformed_encoded_name_never_reaches_upstream(env):
    world, client = env
    async with client:
        r = await rpc(client, multi(), "tools/call", {"name": "son_YQ__eA", "arguments": {}},
            {"mcp-name": "=?base64?!!!!?="})
    assert r.status_code == 400
    assert not world.exchanges and not world.upstream

async def test_header_annotated_tools_fail_closed_instead_of_exposing_unroutable_list(env):
    world, client = env
    original = world.handler
    def annotated(req):
        if req.url.host == f"{B}.aiteam.space" and json.loads(req.content)["method"] == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 12, "result": {"tools": [{
                "name": "requires-region", "inputSchema": {"type": "object", "properties": {
                    "region": {"type": "string", "x-mcp-header": "Region"}}}}]}})
        return original(req)
    client._transport.app.state.http._transport = httpx.MockTransport(annotated)
    async with client:
        for method in ("tools/list", "server/discover"):
            r = await rpc(client, multi(), method)
            assert r.status_code == 502 and "result" not in r.json(), (method, r.text)

async def test_multi_rejects_foreign_origin_before_exchange(env):
    world, client = env
    async with client:
        r = await rpc(client, multi(), "tools/list", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert not world.exchanges and not world.upstream

async def test_unsupported_methods_fail_closed_not_as_empty_feature_list(env):
    world, client = env
    async with client:
        for method in ("resources/subscribe", "subscriptions/listen"):
            r = await rpc(client, multi(), method)
            assert r.status_code in (404, 501)
    assert not world.exchanges and not world.upstream


async def test_cross_target_session_and_resume_rejected(env):
    world, client = env
    tok = multi()
    async with client:
        for method, headers in (("tools/list", {"mcp-session-id": "other"}),
                                ("tools/list", {"last-event-id": "other"})):
            r = await rpc(client, tok, method, headers=headers)
            assert r.status_code == 400
        assert (await client.get("/mcp", headers=auth(tok) | {"last-event-id": "other"})).status_code == 405
        assert (await client.delete("/mcp", headers=auth(tok) | {"mcp-session-id": "other"})).status_code == 405
    assert not world.exchanges and not world.upstream
