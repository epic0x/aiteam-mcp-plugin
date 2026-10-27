"""Portable control routing tests: configured destinations only, never live Sons."""
import json

import httpx
import pytest

from aiteam_connector.server import Connector, SonLink


async def test_remote_signin_never_uses_local_identity():
    seen = []
    def transport(req):
        seen.append(req.url.host)
        return httpx.Response(200, json={"provider": "claude", "signin_id": "i", "ticket": "fake-ticket"})
    c = Connector([SonLink("dee", "https://dee.invalid", "dee-token", son_id="dee-id"),
                   SonLink("davis", "https://davis.invalid", "davis-token", control_owners=["owner@example.com"])], "none", github=False,
                  son_id="dee-id", transport=httpx.MockTransport(transport))
    try:
        result = await c.call("ai_signin_start", {"son": "davis", "provider": "claude"}, caller="owner@example.com")
        assert result.isError
        assert seen == [], "refuse before starting a login whose secure entry would target the wrong Son"
    finally:
        await c.http.aclose()


@pytest.mark.parametrize("caller", [None, "outsider@example.com"])
async def test_ai_controls_require_verified_authorized_caller(monkeypatch, caller):
    monkeypatch.setenv("AITEAM_SON_OWNERS", "owner@example.com")
    seen = []
    def transport(req):
        seen.append(req)
        return httpx.Response(200, json={})
    c = Connector([SonLink("dee", "https://dee.invalid", "token")], "none", github=False,
                  transport=httpx.MockTransport(transport))
    try:
        for name, args in [("ai_status", {}), ("ai_model_set", {"provider": "x", "model": "y"})]:
            result = await c.call(name, args, caller=caller)
            assert result.isError
        assert not seen
    finally:
        await c.http.aclose()


async def test_new_tools_route_remote_and_never_fall_back_local():
    seen = []
    def transport(req):
        seen.append((req.url.host, req.headers["Authorization"], json.loads(req.content)))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "native dispatch returned"}], "isError": False})
    c = Connector([SonLink("dee", "https://dee.invalid", "one"),
                   SonLink("davis", "https://davis.invalid", "two", son_id="davis-id", control_owners=["owner@example.com"])],
                  "none", github=False, transport=httpx.MockTransport(transport))
    try:
        tools = {t.name: t for t in await c.list_tools()}
        expected = {"son_session_stop", "son_session_interrupt", "son_session_reset", "son_slash", "son_approvals_set"}
        assert expected <= tools.keys()
        for name in expected:
            assert tools[name].inputSchema["properties"]["son"]["enum"] == ["dee", "davis"]
            assert tools[name].annotations.readOnlyHint is False
        result = await c.call("son_session_stop", {"son": "davis", "sessionId": "d-session"}, caller="owner@example.com")
        assert not result.isError, result
        assert seen == [("davis.invalid", "Bearer two", {"tool": "son_session_stop", "args": {"sessionId": "d-session"}})]
        for target in ("unknown", "dee-typo"):
            assert (await c.call("son_session_stop", {"son": target, "sessionId": "s"}, caller="owner@example.com")).isError
        assert len(seen) == 1
    finally:
        await c.http.aclose()


async def test_singleton_gate_identity_does_not_authorize_remote():
    seen = []
    def transport(req):
        seen.append(req)
        return httpx.Response(200, json={})
    c = Connector([SonLink("dee", "https://dee.invalid", "one"),
                   SonLink("davis", "https://davis.invalid", "two", son_id="davis-id")],
                  "none", github=False, transport=httpx.MockTransport(transport))
    try:
        result = await c.call("ai_status", {"son": "davis"}, caller="owner@example.com")
        assert result.isError
        assert not seen
    finally:
        await c.http.aclose()
