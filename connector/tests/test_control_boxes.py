"""Two independent real local boxes and UNIX endpoints; no live credentials or providers."""
import json
import shutil
import httpx

from aiteam_connector.box import Box, BoxConfig, Caller, SonTarget, sha
from aiteam_connector.server import Connector, SonLink
from .test_connector import start
from .test_model_control import H, FakeRunner, FakeConfig, _inline, _short_tmp
from .test_gateway_commands import commands


async def test_connector_stop_over_real_socket_has_verified_envelope(commands):
    control, runner, _ = commands
    root = _short_tmp()
    sock = root / "control.sock"
    server = await H.serve(control, sock)
    conn = Connector([SonLink("dee", "http://unused.invalid", "unused")], "none", github=False,
                     model_control={"socket": str(sock)})
    try:
        for name, extra in [("son_session_stop", {}), ("son_session_interrupt", {}), ("son_session_reset", {}),
                            ("son_slash", {"command": "/status"}), ("son_approvals_set", {"mode": "smart"})]:
            assert (await conn.call(name, {"sessionId": "s1", **extra})).isError
        assert not runner.calls
        forged = await conn.call("son_session_stop", {"sessionId": "s1", "principal": "owner@example.com"}, caller="owner@example.com")
        assert forged.isError and not runner.calls
        result = await conn.call("son_session_stop", {"sessionId": "s1"}, caller="owner@example.com")
        assert not result.isError and runner.calls[0][0] == "stop", result
        assert "SECRET" not in str(result)
    finally:
        await conn.http.aclose()
        server.close()
        await server.wait_closed()
        shutil.rmtree(root)

OWNER = "owner@example.com"


async def test_two_authorized_boxes_route_model_changes_independently():
    roots, sockets, apps, conns = [], [], [], []
    states, links = {}, []
    try:
        for name in ("dee", "davis"):
            root = _short_tmp()
            roots.append(root)
            sock = root / "control.sock"
            runner, cfg = FakeRunner(), FakeConfig()
            states[name] = (runner, cfg)
            sockets.append(await H.serve(H.ModelControl(runner, root, config=cfg, run_blocking=_inline), sock))
            caller = Caller("owner", "Owner", sha(name + "-token"), [name])
            caller.control_owner = OWNER  # deployed token binding, never a request field
            box = Box(BoxConfig("test-box", "Test", {name: SonTarget(name, "http://unused.invalid", "unused")},
                                [caller], ai_control={"control_socket": str(sock)}), bridges={name: object()})
            app, url = await start(box.app())
            apps.append(app)
            links.append(SonLink(name, url, name + "-token", son_id=name + "-id", control_owners=[OWNER]))
        # Unreachable local placeholder forces both real destinations through their own Box.
        conn = Connector([SonLink("local", "http://unused.invalid", "unused"), *links], "none", github=False)
        conns.append(conn)
        for name in states:
            result = await conn.call("son_model_set_session", {"son": name, "sessionId": "s1",
                                     "provider": "anthropic-secondary", "model": "claude-opus-5-5"}, caller=OWNER)
            assert not result.isError, result
            own = states[name][0].session_store.lookup_by_session_id("s1")
            assert own.model_override["provider"] == "anthropic-secondary"
            if name == "dee":
                assert states["davis"][0].session_store.lookup_by_session_id("s1").model_override is None
        async with httpx.AsyncClient() as http:
            for link in links:
                body = {"tool": "son_model_status", "args": {}}
                assert (await http.post(link.box_url + "/v1/control", json=body)).status_code == 401
                assert (await http.post(link.box_url + "/v1/control", json=body,
                                       headers={"Authorization": "Bearer wrong"})).status_code == 401
                mismatch = await http.post(link.box_url + "/v1/control", json=body,
                                           headers={"Authorization": "Bearer " + link.box_token,
                                                    "X-Aiteam-Sub": "other@example.com"})
                assert mismatch.status_code == 403
                body["principal"] = OWNER
                assert (await http.post(link.box_url + "/v1/control", json=body,
                                       headers={"Authorization": "Bearer " + link.box_token, "X-Aiteam-Sub": OWNER})).status_code == 400
    finally:
        for conn in conns:
            await conn.http.aclose()
        for app in apps:
            await app.cleanup()
        for server in sockets:
            server.close()
            await server.wait_closed()
        for root in roots:
            shutil.rmtree(root)
