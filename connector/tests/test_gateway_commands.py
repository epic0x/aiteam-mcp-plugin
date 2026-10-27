"""Gateway command boundary; native modules stubbed only in these unit tests."""
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import pytest
from .test_model_control import H, FakeRunner, FakeConfig, _inline

OWNER = "owner@example.com"
KEY = "agent:main:discord:group:1:2"


@pytest.fixture
def commands(tmp_path, monkeypatch):
    runner, cfg = FakeRunner(), FakeConfig()
    cfg.cfg["aiteam_control"] = {"owners": {OWNER: {"discord": "123"}}}
    source = NS(platform=NS(value="discord"), user_id="123", chat_id="1", thread_id="2")
    runner.session_store.entries[KEY].origin = source
    runner.config = NS(multiplex_profiles=False)
    runner._session_key_for_source = lambda s: KEY
    runner._check_slash_access = lambda s, cmd: None
    runner._is_user_authorized_for_source = lambda s: True
    runner.calls = []
    @asynccontextmanager
    async def scope(source):
        yield
    runner._async_profile_scope_for_source = scope
    async def idle(event, source, key, canonical):
        runner.calls.append((canonical, event.text, key))
        return True, "unstructured output containing arbitrary-SECRET"
    async def busy(event, cmd, key, source):
        runner.calls.append((cmd.name, event.text, key))
        if cmd.name == "new":
            runner.session_store.entries[key].session_id = "fresh-id"
        return "native reply"
    runner._hm_dispatch_canonical_command = idle
    runner._dispatch_busy_slash_command = busy
    def resolve(cmd):
        canonical = {"reset": "new", "v": "version"}.get(cmd, cmd)
        return NS(name=canonical, cli_only=False) if canonical in {"status", "stop", "new", "version", "login", "approvals", "reasoning"} else None
    monkeypatch.setitem(sys.modules, "hermes_cli.commands", NS(resolve_command=resolve))
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", NS(MessageEvent=lambda **kwargs: NS(**kwargs)))
    control = H.ModelControl(runner, tmp_path, config=cfg, run_blocking=_inline)
    return control, runner, cfg


async def test_native_session_stop_is_exact_and_output_not_echoed(commands):
    control, runner, cfg = commands
    res = await control.dispatch({"verb": "session_stop", "principal": OWNER,
                                  "args": {"session_key": KEY, "session_id": "s1"}})
    assert res["ok"], res
    assert runner.calls == [("stop", "/stop", KEY)]
    assert "SECRET" not in str(res)


async def test_session_replaced_while_entering_scope_is_not_dispatched(commands):
    control, runner, _ = commands
    @asynccontextmanager
    async def replace(source):
        runner.session_store.entries[KEY].session_id = "replacement"
        yield
    runner._async_profile_scope_for_source = replace
    result = await control.dispatch({"verb": "session_stop", "principal": OWNER,
                                    "args": {"session_id": "s1"}})
    assert not result["ok"] and not runner.calls


async def test_generic_slash_uses_native_alias_dispatch(commands):
    control, runner, _ = commands
    res = await control.dispatch({"verb": "slash", "principal": OWNER,
                                  "args": {"session_id": "s1", "command": "/v"}})
    assert res["ok"], res
    assert runner.calls == [("version", "/version", KEY)]
    assert "SECRET" not in str(res)


@pytest.mark.parametrize("args,principal", [
    ({"session_key": KEY}, None),
    ({"session_key": KEY}, "intruder@example.com"),
    ({"session_key": "unknown", "session_id": "s1"}, OWNER),
    ({"session_key": KEY, "session_id": "s2"}, OWNER),
    ({"session_id": "s1", "owner": OWNER}, None),
])
async def test_command_auth_and_exact_session_fail_closed(commands, args, principal):
    control, runner, _ = commands
    result = await control.dispatch({"verb": "session_stop", "principal": principal, "args": args})
    assert not result["ok"] and not runner.calls


@pytest.mark.parametrize("command", ["/login", "/login secret-code", "/model x --api-key anything", "/resume s2",
                                    "/status\n/stop", "/status; rm -rf /", "/exec echo x", "/yolo", "/approvals off"])
async def test_generic_dispatch_refuses_credentials_cross_session_and_exec(commands, command):
    control, runner, _ = commands
    result = await control.dispatch({"verb": "slash", "principal": OWNER,
                                    "args": {"session_id": "s1", "command": command}})
    assert not result["ok"] and not runner.calls
    assert "secret-code" not in str(result)


async def test_native_handler_failure_is_static_and_redacted(commands):
    control, runner, _ = commands
    async def fail(*args):
        raise RuntimeError("arbitrary-SECRET")
    runner._hm_dispatch_canonical_command = fail
    result = await control.dispatch({"verb": "slash", "principal": OWNER,
                                    "args": {"session_id": "s1", "command": "/status"}})
    assert not result["ok"] and "SECRET" not in str(result)


@pytest.mark.parametrize("verb,canonical", [("session_reset", "new"), ("session_interrupt", "stop")])
async def test_reset_and_interrupt_use_exact_native_session_path(commands, verb, canonical):
    control, runner, _ = commands
    result = await control.dispatch({"verb": verb, "principal": OWNER, "args": {"session_id": "s1"}})
    assert result["ok"], result
    assert runner.calls == [(canonical, "/" + canonical, KEY)]
    if verb == "session_reset":
        assert result["session_id"] == "fresh-id"


@pytest.mark.parametrize("denial", ["source", "slash", "binding", "multiplex"])
async def test_native_authorization_layers_are_not_bypassed(commands, denial):
    control, runner, cfg = commands
    if denial == "source":
        runner._is_user_authorized_for_source = lambda source: False
    elif denial == "slash":
        runner._check_slash_access = lambda source, cmd: "denied"
    elif denial == "binding":
        runner.session_store.entries[KEY].origin.user_id = "another-user"
    else:
        runner.config.multiplex_profiles = True
    result = await control.dispatch({"verb": "session_stop", "principal": OWNER, "args": {"session_id": "s1"}})
    assert not result["ok"] and not runner.calls


@pytest.mark.parametrize("admin,native_ok", [(False, True), (True, False)])
async def test_approvals_denials_and_native_failure_do_not_report_success(commands, monkeypatch, admin, native_ok):
    control, _, _ = commands
    calls = []
    monkeypatch.setitem(sys.modules, "gateway.slash_access", NS(policy_for_source=lambda *a: NS(is_admin=lambda uid: admin)))
    def apply(value):
        calls.append(value)
        return NS(ok=native_ok, mode="smart", changed=False, message="arbitrary-SECRET")
    monkeypatch.setitem(sys.modules, "hermes_cli.approval_mode", NS(run_approval_mode_command=apply))
    result = await control.dispatch({"verb": "approvals_set", "principal": OWNER,
                                    "args": {"session_id": "s1", "mode": "yolo"}})
    assert not result["ok"] and "SECRET" not in str(result)
    assert calls == (["off"] if admin else [])


@pytest.mark.parametrize("mode,native", [("smart", "smart"), ("yolo", "off")])
async def test_approval_mode_explicit_native_mapping(commands, monkeypatch, mode, native):
    control, runner, _ = commands
    modes = []
    monkeypatch.setitem(sys.modules, "gateway.slash_access", NS(policy_for_source=lambda *a: NS(is_admin=lambda uid: True)))
    def apply(value):
        modes.append(value)
        return NS(ok=True, mode=value, changed=True, message="arbitrary-SECRET")
    monkeypatch.setitem(sys.modules, "hermes_cli.approval_mode", NS(run_approval_mode_command=apply))
    result = await control.dispatch({"verb": "approvals_set", "principal": OWNER,
                                    "args": {"session_id": "s1", "mode": mode}})
    assert result["ok"], result
    assert result["native_mode"] == native and modes == [native]
    assert "SECRET" not in str(result)
