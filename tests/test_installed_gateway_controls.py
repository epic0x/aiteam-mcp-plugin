"""Opt-in installed Hermes probe: run ONLY in a fresh HERMES_HOME.

Real gateway registry/dispatch methods, MessageEvent and durable SessionStore.
Interrupt/reset side effects are isolated substitutes: no running gateway or provider touched.
"""
import asyncio
import importlib.util
import os
from contextlib import asynccontextmanager
from pathlib import Path
import pytest

pytestmark = pytest.mark.skipif("control-probe-" not in Path(os.environ.get("HERMES_HOME", "")).name,
                                reason="opt-in installed Hermes probe requires an isolated home")


def test_installed_gateway_dispatch_and_durable_store(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionStore, SessionSource
    root = Path(os.environ["HERMES_HOME"])
    assert root != Path("/opt/data") and "control-probe-" in root.name
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    cfg = root / "config.yaml"
    cfg.write_text("approvals:\n  mode: smart\naiteam_control:\n  owners:\n    owner@example.com:\n      discord: '123'\n")
    spec = importlib.util.spec_from_file_location("probe_control", Path(__file__).parents[1] / "son/model_control/handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = SessionStore(root / "sessions", runner.config)
    source = SessionSource(platform=Platform.DISCORD, chat_id="probe-chat", user_id="123")
    entry = runner.session_store.get_or_create_session(source)
    old_id, key = entry.session_id, entry.session_key
    runner._session_key_for_source = lambda s: key
    runner._is_user_authorized_for_source = lambda s: True
    runner._is_session_running = lambda k: False
    calls = []
    @asynccontextmanager
    async def scope(source):
        yield
    runner._async_profile_scope_for_source = scope
    async def interrupt(k, source, **kwargs):
        calls.append(("interrupt", k, kwargs))
    runner._interrupt_and_clear_session = interrupt
    async def reset(event):
        assert event.source is entry.origin
        new = runner.session_store.reset_session(key)
        calls.append(("reset", new.session_id))
        return "native isolated reset"
    runner._handle_reset_command = reset
    async def status(event):
        calls.append(("status", event.text))
        return "unstructured-secret-must-not-escape"
    runner._handle_status_command = status
    control = mod.ModelControl(runner, root)
    async def run():
        for verb, args in [("slash", {"command": "/status"}), ("session_interrupt", {}), ("session_reset", {})]:
            result = await control.dispatch({"verb": verb, "principal": "owner@example.com",
                                            "args": {"session_key": key, "session_id": old_id, **args}})
            assert result["ok"], result
            assert "unstructured-secret" not in str(result)
            if verb == "session_reset":
                assert result["session_id"] == runner.session_store.lookup_by_session_key(key).session_id
        current = runner.session_store.lookup_by_session_key(key)
        assert current.session_id != old_id
        for mode, native in [("yolo", "off"), ("smart", "smart")]:
            result = await control.dispatch({"verb": "approvals_set", "principal": "owner@example.com",
                                            "args": {"session_id": current.session_id, "mode": mode}})
            assert result["ok"] and result["native_mode"] == native, result
        bad = await control.dispatch({"verb": "session_stop", "principal": "owner@example.com",
                                      "args": {"session_key": key, "session_id": old_id}})
        assert not bad["ok"]
    asyncio.run(run())
    assert [x[0] for x in calls] == ["status", "interrupt", "interrupt", "reset"]
    # Exercise Hermes' actual startup hook registry, not just direct class construction.
    import shutil
    import sys
    import json
    import gateway.run as gateway_run
    from gateway.hooks import HookRegistry
    hook_dir = root / "hooks" / "aiteam-model-control"
    hook_dir.mkdir(parents=True)
    for filename in ("HOOK.yaml", "handler.py"):
        shutil.copyfile(Path(__file__).parents[1] / "son/model_control" / filename, hook_dir / filename)
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
    registry = HookRegistry()
    registry.discover_and_load()
    assert any(x["name"] == "aiteam-model-control" for x in registry.loaded_hooks)
    async def startup_probe():
        await registry.emit("gateway:startup", {})
        socket = root / "mcp" / "model-control.sock"
        assert socket.stat().st_mode & 0o777 == 0o600
        handler = sys.modules["hermes_hook_aiteam-model-control"]
        try:
            reader, writer = await asyncio.open_unix_connection(str(socket))
            request = {"verb": "slash", "principal": "owner@example.com",
                       "args": {"session_key": key, "command": "/status"}}
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            result = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
            assert result["ok"] and result["command"] == "status", result
            assert "unstructured-secret" not in str(result)
        finally:
            handler._SERVER.close()
            await handler._SERVER.wait_closed()
            handler._SERVER = None
    asyncio.run(startup_probe())
    print("Installed registry, startup hook, 0600 socket and idle/busy dispatch exercised; reset persisted; native approvals off/smart written only in isolated home.")
