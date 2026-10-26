"""AI control (Layer 2): box /v1/ai/* proxy to the gateway plugin + the five connector tools.

Real sockets: connector -> messaging box (aiohttp) -> fake aiteam-ai-control plugin (aiohttp). Checks the caller-token
gate on every route, that /v1/ai/credential does not exist on the box, pass-through and error mapping, the tool
annotations, the sign-in text (aiteam.space one-time link) and that no raw ticket ever lands in a structured result.
"""
import json

import httpx
import pytest
from aiohttp import web

from aiteam_connector.box import AI_BODY_MAX, AiControl, Box, BoxConfig, Caller, SonTarget, sha
from aiteam_connector.bridge import HermesBridge
from aiteam_connector.entitlements import Entitlements, required_addons
from aiteam_connector.server import AI_TOOLS, Connector, SonLink

from .fake_hermes import KEY, FakeHermes
from .test_connector import ALICE, BOB, free_port, start

PLUGIN_KEY = "plugin-api-server-key-zzzzzzzzzzzz"
TICKET = "TICKET_abcdefghijklmnopqrstuvwxyz0123456"
STATUS = {"global": {"provider": "anthropic", "model": "claude-sonnet-4"},
          "fallback": [{"provider": "openai-codex", "model": "gpt-5"}],
          "providers": [{"provider": "anthropic", "name": "Claude", "signed_in": True,
                         "credentials": [{"id": "c1", "label": "work", "auth_type": "oauth", "status": "ok"}]},
                        {"provider": "openai-codex", "name": "ChatGPT", "signed_in": False, "credentials": []}],
          "sessions": [{"session_key": "agent:main:discord:1", "session_id": "s1", "platform": "discord",
                        "title": "dev", "provider": "anthropic", "model": "claude-sonnet-4", "override": False}]}


class FakePlugin:
    """Stand-in for the aiteam-ai-control gateway plugin (contract Layer 1)."""

    def __init__(self):
        self.seen: list[tuple[str, str, dict | None, str]] = []
        self.fail: dict[str, tuple[int, dict]] = {}

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        self.seen.append((request.method, request.path, body, request.headers.get("Authorization", "")))
        if request.headers.get("Authorization") != f"Bearer {PLUGIN_KEY}":
            return web.json_response({"error": "unauthorized"}, status=401)
        if request.path in self.fail:
            code, d = self.fail[request.path]
            return web.json_response(d, status=code)
        p = request.path
        if p == "/ai/status":
            return web.json_response(STATUS)
        if p == "/ai/model":
            return web.json_response({"ok": True, "scope": body["scope"], "provider": body["provider"],
                                      "model": body["model"], "sessions_changed": 3})
        if p == "/ai/fallback":
            return web.json_response({"ok": True, "fallback": body["chain"]})
        if p == "/ai/signin/start":
            prov = body["provider"]
            d = {"signin_id": "si_123", "provider": prov, "expires_at": "2026-10-26T12:15:00Z", "ticket": TICKET,
                 "needs_code": prov != "chatgpt"}
            if prov == "claude":
                d["provider_url"] = "https://claude.ai/oauth/authorize?x=1"
            if prov == "chatgpt":
                d.update(provider_url="https://auth.openai.com/codex/device", user_code="ABCD-1234")
            return web.json_response(d)
        if p.startswith("/ai/signin/"):
            return web.json_response({"signin_id": p.rsplit("/", 1)[1], "provider": "claude", "state": "connected"})
        if p == "/ai/credential":
            return web.json_response({"ok": True})
        return web.json_response({"error": "nope"}, status=404)

    def app(self) -> web.Application:
        a = web.Application()
        a.router.add_route("*", "/{tail:.*}", self.handle)
        return a


@pytest.fixture
async def aistack(monkeypatch):
    monkeypatch.setenv("AITEST_PLUGIN_KEY", PLUGIN_KEY)
    fake, plugin = FakeHermes(), FakePlugin()
    r1, hermes_url = await start(fake.app())
    r0, plugin_url = await start(plugin.app())
    cfg = BoxConfig("test-box", "Test box", {"my-son": SonTarget("my-son", hermes_url, "X")},
                    [Caller("alice", "Alice", sha(ALICE), ["my-son"]), Caller("bob", "Bob", sha(BOB), ["other"])],
                    ai_control={"url": plugin_url, "key_env": "AITEST_PLUGIN_KEY"})
    box = Box(cfg, bridges={"my-son": HermesBridge(hermes_url, KEY)})
    r2, box_url = await start(box.app())
    yield box_url, plugin, box
    for r in (r2, r1, r0):
        await r.cleanup()


AI_ROUTES = (("GET", "/v1/ai/status"), ("POST", "/v1/ai/model"), ("PUT", "/v1/ai/fallback"),
             ("POST", "/v1/ai/signin/start"), ("GET", "/v1/ai/signin/si_123"))


# ------------------------------------------------------------------------------------------- box
async def test_every_ai_route_needs_a_caller_token(aistack):
    box_url, plugin, _ = aistack
    async with httpx.AsyncClient() as c:
        for m, path in AI_ROUTES:
            assert (await c.request(m, box_url + path, json={})).status_code == 401, path
            r = await c.request(m, box_url + path, json={}, headers={"Authorization": "Bearer nope"})
            assert r.status_code == 401, path
    assert plugin.seen == [], "nothing reaches the plugin without a caller token"


async def test_credential_route_is_never_exposed(aistack):
    box_url, plugin, _ = aistack
    h = {"Authorization": f"Bearer {ALICE}"}
    async with httpx.AsyncClient() as c:
        for m in ("POST", "GET", "PUT"):
            r = await c.request(m, f"{box_url}/v1/ai/credential", json={"ticket": "t", "secret": "s"}, headers=h)
            assert r.status_code in (404, 405), m
        # /v1/ai/signin/credential is a signin id lookup, never the credential endpoint
        await c.get(f"{box_url}/v1/ai/signin/credential", headers=h)
    assert all(p != "/ai/credential" for _, p, _, _ in plugin.seen)


async def test_pass_through_with_the_plugin_key(aistack):
    box_url, plugin, _ = aistack
    h = {"Authorization": f"Bearer {ALICE}"}
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{box_url}/v1/ai/status", headers=h)
        assert r.status_code == 200 and r.json() == STATUS and r.headers["Cache-Control"] == "no-store"
        body = {"provider": "anthropic", "model": "claude-opus-4", "scope": "session", "session_key": "k"}
        r = await c.post(f"{box_url}/v1/ai/model", json=body, headers=h)
        assert r.status_code == 200 and r.json()["sessions_changed"] == 3
        chain = [{"provider": "openai-codex", "model": "gpt-5"}]
        r = await c.put(f"{box_url}/v1/ai/fallback", json={"chain": chain}, headers=h)
        assert r.json() == {"ok": True, "fallback": chain}
        r = await c.post(f"{box_url}/v1/ai/signin/start", json={"provider": "claude"}, headers=h)
        assert r.json()["ticket"] == TICKET  # the box relays it; the connector turns it into the link
        r = await c.get(f"{box_url}/v1/ai/signin/si_123", headers=h)
        assert r.json()["state"] == "connected"
        assert (await c.get(f"{box_url}/v1/ai/signin/bad%20id", headers=h)).status_code == 400
    paths = [(m, p) for m, p, _, _ in plugin.seen]
    assert paths == [("GET", "/ai/status"), ("POST", "/ai/model"), ("PUT", "/ai/fallback"),
                     ("POST", "/ai/signin/start"), ("GET", "/ai/signin/si_123")]
    assert all(a == f"Bearer {PLUGIN_KEY}" for *_, a in plugin.seen), "the plugin key, never the caller token"
    assert plugin.seen[1][2] == body


async def test_upstream_errors_are_mapped(aistack):
    box_url, plugin, _ = aistack
    h = {"Authorization": f"Bearer {ALICE}"}
    async with httpx.AsyncClient() as c:
        plugin.fail["/ai/model"] = (409, {"error": "Claude is not signed in on this Son."})
        r = await c.post(f"{box_url}/v1/ai/model", json={"provider": "anthropic", "model": "m", "scope": "all"},
                         headers=h)
        assert r.status_code == 409 and r.json() == {"error": "Claude is not signed in on this Son."}
        plugin.fail["/ai/status"] = (500, {"error": "Traceback: secret stuff"})
        r = await c.get(f"{box_url}/v1/ai/status", headers=h)
        assert r.status_code == 502 and "Traceback" not in r.text
        r = await c.post(f"{box_url}/v1/ai/model", content=b"[1,2]", headers=h)
        assert r.status_code == 400
        r = await c.post(f"{box_url}/v1/ai/model", content=b"x" * (AI_BODY_MAX + 10), headers=h)
        assert r.status_code == 413


async def test_plugin_down_is_503(monkeypatch, aistack):
    box_url, _, box = aistack
    box.ai.url = f"http://127.0.0.1:{free_port()}"
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{box_url}/v1/ai/status", headers={"Authorization": f"Bearer {ALICE}"})
    assert r.status_code == 503 and r.json() == {"error": "ai control not available on this Son yet"}
    monkeypatch.delenv("AITEST_PLUGIN_KEY")
    monkeypatch.setenv("HERMES_HOME", "/nonexistent-hermes-home")
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{box_url}/v1/ai/status", headers={"Authorization": f"Bearer {ALICE}"})
    assert r.status_code == 503, "no key at all = not available, never an unauthenticated call"


def test_key_from_hermes_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("OTHER=1\n# API_SERVER_KEY=commented\nexport API_SERVER_KEY=\"from-file\"\n")
    ai = AiControl(None)
    assert ai.url == "http://127.0.0.1:8651" and ai.key_env == "API_SERVER_KEY"
    assert ai.key() == "from-file"
    monkeypatch.setenv("API_SERVER_KEY", "from-env")
    assert ai.key() == "from-env"


def test_box_config_loads_ai_control(tmp_path):
    p = tmp_path / "box.json"
    p.write_text(json.dumps({"box_id": "b-1", "sons": [], "ai_control": {"url": "http://127.0.0.1:9999/"}}))
    cfg = BoxConfig.load(str(p))
    assert AiControl(cfg.ai_control).url == "http://127.0.0.1:9999"


# ------------------------------------------------------------------------------------------- connector tools
def conn_for(box_url, **kw) -> Connector:
    kw.setdefault("son_id", "son-abc")
    return Connector([SonLink("my-son", box_url, ALICE)], passthrough="none", github=False, **kw)


async def test_ai_tools_listed_with_exact_annotations(aistack):
    box_url, *_ = aistack
    tools = {t.name: t for t in await conn_for(box_url).list_tools()}
    assert set(AI_TOOLS) <= set(tools) and len(AI_TOOLS) == 5
    exp = {"ai_status": (True, None, False), "ai_signin_status": (True, None, False),
           "ai_signin_start": (False, False, True), "ai_model_set": (False, False, False),
           "ai_fallback_set": (False, False, False)}
    for name, (ro, destructive, open_world) in exp.items():
        a = tools[name].annotations
        assert a.readOnlyHint is ro and a.openWorldHint is open_world, name
        assert a.title and tools[name].title == a.title
        if not ro:
            assert a.destructiveHint is destructive, name
    assert tools["ai_signin_start"].annotations.idempotentHint is False
    assert tools["ai_model_set"].annotations.idempotentHint is True
    assert tools["ai_fallback_set"].annotations.idempotentHint is True
    assert tools["ai_status"].title == "Which AI is my Son using?"
    assert tools["ai_signin_start"].inputSchema["properties"]["provider"]["enum"] == [
        "claude", "chatgpt", "anthropic-key", "openai-key"]
    assert tools["ai_signin_status"].inputSchema["required"] == ["signin_id"]
    ms = tools["ai_model_set"].inputSchema
    assert ms["required"] == ["provider", "model"] and ms["properties"]["scope"]["enum"] == ["all", "session"]
    assert ms["properties"]["scope"]["default"] == "all"
    item = tools["ai_fallback_set"].inputSchema["properties"]["chain"]["items"]
    assert item["required"] == ["provider", "model"]


def _no_ticket(obj) -> bool:
    if isinstance(obj, dict):
        return "ticket" not in obj and all(_no_ticket(v) for v in obj.values())
    if isinstance(obj, list):
        return all(_no_ticket(v) for v in obj)
    return obj != TICKET


async def test_signin_start_claude_gives_the_one_time_link(aistack):
    box_url, plugin, _ = aistack
    r = await conn_for(box_url).call("ai_signin_start", {"provider": "claude", "label": "work"})
    t = r.content[0].text
    link = f"https://aiteam.space/connect/ai/son-abc?t={TICKET}"
    assert not r.isError
    lines = t.splitlines()
    assert lines[0] == "Claude sign-in started for my-son."
    assert lines[1] == "1. Open https://claude.ai/oauth/authorize?x=1 and sign in. Claude shows a code."
    assert lines[2] == f"2. Paste that code here (one-time, expires in 15 min): {link}"
    assert "Never paste the code in this chat." in t and "si_123" in t
    assert TICKET not in t.replace(link, ""), "the ticket appears only inside the aiteam.space link"
    sc = r.structuredContent
    assert _no_ticket(sc) and sc["secure_link"] == link and sc["signin_id"] == "si_123"
    assert plugin.seen[-1][2] == {"provider": "claude", "label": "work"}


async def test_signin_start_chatgpt_and_keys(aistack):
    box_url, *_ = aistack
    conn = conn_for(box_url, site_url="https://staging.aiteam.space/")
    r = await conn.call("ai_signin_start", {"provider": "chatgpt"})
    t = r.content[0].text
    assert "1. Open https://auth.openai.com/codex/device and enter code ABCD-1234." in t
    assert "Done: no code to paste." in t and "Optional" in t
    assert f"https://staging.aiteam.space/connect/ai/son-abc?t={TICKET}" in t
    assert _no_ticket(r.structuredContent) and r.structuredContent["user_code"] == "ABCD-1234"
    for prov in ("anthropic-key", "openai-key"):
        r = await conn.call("ai_signin_start", {"provider": prov})
        t = r.content[0].text
        assert not r.isError and "Paste your key here (one-time" in t and "Never paste the key in this chat." in t
        assert _no_ticket(r.structuredContent)
    r = await conn.call("ai_signin_start", {"provider": "gemini"})
    assert r.isError


async def test_signin_start_without_son_id_never_shows_the_ticket(aistack, monkeypatch, tmp_path):
    box_url, *_ = aistack
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("SON_ID", raising=False)
    r = await conn_for(box_url, son_id=None).call("ai_signin_start", {"provider": "claude"})
    t = r.content[0].text
    assert not r.isError and TICKET not in t and "https://claude.ai/oauth/authorize?x=1" in t
    assert "secure link is not available" in t and "aiteam.space" in t
    assert r.structuredContent["secure_link"] is None and _no_ticket(r.structuredContent)
    # son.json fills the gap
    (tmp_path / "mcp").mkdir()
    (tmp_path / "mcp" / "son.json").write_text(json.dumps({"son_id": "son-from-file"}))
    r = await conn_for(box_url, son_id=None).call("ai_signin_start", {"provider": "claude"})
    assert f"/connect/ai/son-from-file?t={TICKET}" in r.content[0].text


async def test_status_model_fallback_signin_status_tools(aistack):
    box_url, plugin, _ = aistack
    conn = conn_for(box_url)
    r = await conn.call("ai_status", {})
    first = r.content[0].text.splitlines()[0]
    assert not r.isError and first.startswith("Signed in: Claude.") and "claude-sonnet-4" in first
    assert r.structuredContent == STATUS
    r = await conn.call("ai_model_set", {"provider": "anthropic", "model": "claude-opus-4"})
    assert not r.isError and r.content[0].text.startswith("All sessions now use Anthropic claude-opus-4 (3 changed).")
    assert plugin.seen[-1][2] == {"provider": "anthropic", "model": "claude-opus-4", "scope": "all"}
    r = await conn.call("ai_model_set", {"provider": "anthropic", "model": "m", "scope": "session"})
    assert r.isError and "session_key" in r.content[0].text
    r = await conn.call("ai_model_set", {"provider": "anthropic", "model": "m", "scope": "session", "session_id": "s1"})
    assert not r.isError and r.content[0].text.startswith("That session now uses")
    r = await conn.call("ai_fallback_set", {"chain": [{"provider": "openai-codex", "model": "gpt-5"}]})
    assert not r.isError and r.content[0].text.startswith("Fallback order: ChatGPT gpt-5.")
    r = await conn.call("ai_fallback_set", {"chain": []})
    assert not r.isError and r.content[0].text.startswith("Fallback order cleared.")
    r = await conn.call("ai_signin_status", {"signin_id": "si_123"})
    assert not r.isError and r.content[0].text.startswith("Connected: Claude is signed in")
    assert (await conn.call("ai_signin_status", {"signin_id": "../x"})).isError


async def test_tool_errors_are_plain(aistack):
    box_url, plugin, box = aistack
    conn = conn_for(box_url)
    plugin.fail["/ai/model"] = (409, {"error": "Claude is not signed in on this Son."})
    r = await conn.call("ai_model_set", {"provider": "anthropic", "model": "m"})
    assert r.isError and r.content[0].text.splitlines()[0] == (
        "Could not switch the model: Claude is not signed in on this Son.")
    box.ai.url = f"http://127.0.0.1:{free_port()}"
    r = await conn.call("ai_status", {})
    assert r.isError and r.content[0].text.splitlines()[0] == "AI control is not available on my-son yet."
    r = await Connector([SonLink("my-son", box_url, "wrong")], passthrough="none", github=False).call("ai_status", {})
    assert r.isError
    r = await Connector([SonLink("my-son", "http://127.0.0.1:9", "t")], passthrough="none",
                        github=False).call("ai_status", {})
    assert r.isError and "not reachable" in r.content[0].text


async def test_ai_tools_are_never_paywalled(aistack, tmp_path):
    box_url, *_ = aistack
    plugs = tmp_path / "plugs.json"
    plugs.write_text(json.dumps({"enforce": True, "addons": []}))
    ent = Entitlements(path=plugs, son_id="son-abc", exempt=set())
    conn = conn_for(box_url, entitlements=ent)
    assert ent.owned() == set(), "enforcement is on and nothing is owned"
    assert ent.check("discord_thread_create", {}) is not None, "sanity: paid tools are walled here"
    for name in AI_TOOLS:
        assert required_addons(name, {}) == () and ent.check(name, {}) is None
    r = await conn.call("ai_status", {})
    assert not r.isError and not (r.structuredContent or {}).get("paywall")
    r = await conn.call("ai_signin_start", {"provider": "claude"})
    assert not r.isError and not r.structuredContent.get("paywall")
