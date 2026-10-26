"""Model / provider control: connector tools (model_control.py) + the Son-side gateway hook (son/model_control).

The gateway hook runs against a fake runner, session store and config (no Hermes install); the connector talks to
the real hook over a real unix socket, so the wire contract is exercised end to end on this box.
"""
import asyncio
import importlib.util
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiteam_connector import model_control as mc
from aiteam_connector.server import Connector, SonLink

HANDLER = Path(__file__).resolve().parents[2] / "son" / "model_control" / "handler.py"
spec = importlib.util.spec_from_file_location("aiteam_model_control_handler", HANDLER)
H = importlib.util.module_from_spec(spec)
spec.loader.exec_module(H)


def _short_tmp() -> Path:
    # AF_UNIX paths are capped near 104-108 bytes: pytest's tmp_path is too deep on some hosts.
    return Path(tempfile.mkdtemp(prefix="mc", dir=os.environ.get("TMPDIR")))


# ------------------------------------------------------------------ fakes
class FakeConfig:
    def __init__(self):
        self.cfg = {
            "model": {"default": "claude-opus-5-5", "provider": "anthropic-primary"},
            "fallback_providers": [{"provider": "openai-codex", "model": "gpt-6.1-sol"}],
            "providers": {
                "anthropic-primary": {"base_url": "https://api.anthropic.com", "key_cmd": "SECRET-CMD",
                                      "models": ["claude-opus-5-5", "claude-sonnet-5-5"]},
                "anthropic-secondary": {"base_url": "https://api.anthropic.com:443", "key_cmd": "SECRET-CMD",
                                        "models": ["claude-opus-5-5", "claude-sonnet-5-5"]},
                "openrouter": {"discover_models": True, "models": ["x"]},
            },
        }
        self.writes = []

    def load(self):
        return json.loads(json.dumps(self.cfg))

    def set_primary(self, provider, model):
        self.writes.append(("primary", provider, model))
        self.cfg["model"] = {"default": model, "provider": provider}

    def set_fallback(self, chain, primary=None):
        if primary:
            self.set_primary(primary["provider"], primary["model"])
        self.writes.append(("fallback", chain))
        self.cfg["fallback_providers"] = chain

    @staticmethod
    def chain(cfg):
        return [dict(e) for e in cfg.get("fallback_providers") or [] if e.get("provider") and e.get("model")]

    @staticmethod
    def credentials_ok(provider, model):
        return True


class FakeStore:
    def __init__(self):
        e = lambda k, i, ov=None: SimpleNamespace(session_key=k, session_id=i, model_override=ov)  # noqa: E731
        self.entries = {"agent:main:discord:group:1:2": e("agent:main:discord:group:1:2", "s1"),
                        "agent:main:telegram:dm:9": e("agent:main:telegram:dm:9", "s2",
                                                      {"model": "claude-sonnet-5-5", "provider": "anthropic-primary"})}

    def list_sessions(self, active_minutes=None):
        return list(self.entries.values())

    def lookup_by_session_key(self, k):
        return self.entries.get(k)

    def lookup_by_session_id(self, sid):
        return next((e for e in self.entries.values() if e.session_id == sid), None)

    def get_model_override(self, k):
        e = self.entries.get(k)
        return dict(e.model_override) if e and e.model_override else None

    def set_model_override(self, k, ov):
        e = self.entries.get(k)
        if e is not None:
            e.model_override = {kk: str(v) for kk, v in (ov or {}).items() if kk in ("model", "provider", "base_url")} or None


class FakeRunner:
    def __init__(self):
        self.session_store = FakeStore()
        self._session_model_overrides = {"agent:main:telegram:dm:9": {"model": "claude-sonnet-5-5",
                                                                      "provider": "anthropic-primary",
                                                                      "api_key": "sk-ant-SECRET-SHOULD-NEVER-LEAK"}}
        self.evicted, self.running = [], set()
        self.agents = {"agent:main:discord:group:1:2": SimpleNamespace(model="claude-opus-5-5",
                                                                     provider="anthropic-primary", api_key="sk-x")}

    def _evict_cached_agent(self, k):
        self.evicted.append(k)

    def _is_session_running(self, k):
        return k in self.running

    def _cached_agent_for(self, k):
        return self.agents.get(k)


async def _inline(fn, *a):
    return fn(*a)


@pytest.fixture
def ctl():
    runner, cfg = FakeRunner(), FakeConfig()
    return H.ModelControl(runner, Path("/nonexistent"), config=cfg, run_blocking=_inline), runner, cfg


# ------------------------------------------------------------------ gateway hook
async def test_status_global_and_session_never_leaks(ctl):
    c, runner, _ = ctl
    res = H.scrub(await c.dispatch({"verb": "status", "args": {}}))
    assert res["ok"]
    assert [r["role"] for r in res["global"]["order"]] == ["primary", "fallback_1"]
    assert res["global"]["primary"] == {"provider": "anthropic-primary", "model": "claude-opus-5-5"}
    assert res["overridden"] == 1 and res["total"] == 2
    assert set(res["providers"]) >= {"anthropic-primary", "anthropic-secondary", "openai-codex", "openrouter"}
    assert res["providers"]["openrouter"]["models"] == ["x"]  # only configured models
    blob = json.dumps(res)
    assert "SECRET" not in blob and "sk-" not in blob and "base_url" not in blob and "key_cmd" not in blob
    one = await c.dispatch({"verb": "status", "args": {"session_id": "s2"}})
    s = one["session"]
    assert s["source"] == "session_override" and s["effective"]["model"] == "claude-sonnet-5-5"
    assert s["fallback_order"][1] == {"provider": "openai-codex", "model": "gpt-6.1-sol", "role": "fallback_1"}
    assert "SECRET" not in json.dumps(one)


async def test_set_session_validates_persists_and_evicts(ctl):
    c, runner, _ = ctl
    key = "agent:main:discord:group:1:2"
    bad = await c.dispatch({"verb": "set_session", "args": {"session_key": key, "provider": "nope", "model": "m"}})
    assert not bad["ok"] and "not configured" in bad["error"]
    bad = await c.dispatch({"verb": "set_session", "args": {"session_key": key, "provider": "anthropic-secondary",
                                                            "model": "gpt-4"}})
    assert not bad["ok"] and "not offered" in bad["error"]
    miss = await c.dispatch({"verb": "set_session", "args": {"session_key": "nope", "provider": "x", "model": "y"}})
    assert not miss["ok"] and "not found" in miss["error"]
    ok = await c.dispatch({"verb": "set_session", "args": {"session_key": key, "provider": "anthropic-secondary",
                                                           "model": "claude-opus-5-5"}})
    assert ok["ok"] and ok["applies"] == "next_turn" and runner.evicted == [key]
    assert runner.session_store.get_model_override(key) == {"provider": "anthropic-secondary", "model": "claude-opus-5-5"}
    # a running session is never interrupted
    runner.running.add(key)
    ok = await c.dispatch({"verb": "set_session", "args": {"session_key": key, "clear": True}})
    assert ok["ok"] and ok["applies"] == "after_current_turn" and runner.evicted == [key]
    assert runner.session_store.get_model_override(key) is None


async def test_set_global_switches_every_session(ctl):
    c, runner, cfg = ctl
    res = await c.dispatch({"verb": "set_global", "args": {"provider": "anthropic-secondary", "model": "claude-opus-5-5"}})
    assert res["ok"] and res["primary"] == {"provider": "anthropic-secondary", "model": "claude-opus-5-5"}
    assert cfg.writes == [("primary", "anthropic-secondary", "claude-opus-5-5")]
    assert res["session_overrides_cleared"] == 1
    assert runner.session_store.get_model_override("agent:main:telegram:dm:9") is None
    assert "agent:main:telegram:dm:9" not in runner._session_model_overrides
    st = await c.dispatch({"verb": "status", "args": {}})
    assert all(r["effective"]["provider"] == "anthropic-secondary" for r in st["sessions"])
    bad = await c.dispatch({"verb": "set_global", "args": {"provider": "openai", "model": "x"}})
    assert not bad["ok"] and cfg.writes == [("primary", "anthropic-secondary", "claude-opus-5-5")]


async def test_set_fallback_order(ctl):
    c, _, cfg = ctl
    res = await c.dispatch({"verb": "set_fallback", "args": {"fallback": [
        {"provider": "anthropic-secondary", "model": "claude-opus-5-5"},
        {"provider": "openai-codex", "model": "gpt-6.1-sol"}]}})
    assert res["ok"]
    assert [(r["role"], r["provider"]) for r in res["order"]] == [
        ("primary", "anthropic-primary"), ("fallback_1", "anthropic-secondary"), ("fallback_2", "openai-codex")]
    assert cfg.cfg["fallback_providers"][0]["provider"] == "anthropic-secondary"
    dup = await c.dispatch({"verb": "set_fallback", "args": {"fallback": [
        {"provider": "anthropic-primary", "model": "claude-opus-5-5"}]}})
    assert not dup["ok"] and "duplicate" in dup["error"]
    bad = await c.dispatch({"verb": "set_fallback", "args": {"fallback": [{"provider": "a b", "model": "x"}]}})
    assert not bad["ok"]
    off = await c.dispatch({"verb": "set_fallback", "args": {"fallback": []}})
    assert off["ok"] and cfg.cfg["fallback_providers"] == []
    unk = await c.dispatch({"verb": "rm_rf", "args": {}})
    assert not unk["ok"]


# ------------------------------------------------------------------ connector <-> real socket
async def test_connector_tools_over_real_socket():
    runner, cfg = FakeRunner(), FakeConfig()
    sock = _short_tmp() / "model-control.sock"
    server = await H.serve(H.ModelControl(runner, sock.parent, config=cfg, run_blocking=_inline), sock)
    try:
        assert (os.stat(sock).st_mode & 0o777) == 0o600
        conn = Connector([SonLink("son", "http://box.invalid", "t")], "none", github=False,
                         model_control={"socket": str(sock)})
        async def owner_call(name, args, **kwargs):
            return await conn.call(name, args, caller="owner@example.com")
        tools = {t.name: t for t in await conn.list_tools()}
        for n in mc.MODEL_TOOLS:
            assert n in tools
        assert tools["son_model_status"].annotations.readOnlyHint is True
        for n in ("son_model_set_all", "son_model_set_session", "son_model_fallback_set"):
            a = tools[n].annotations
            assert a.readOnlyHint is False and a.destructiveHint is False and a.openWorldHint is False
        r = await owner_call("son_model_status", {}, caller=None)
        assert not r.isError and r.content[0].text.startswith("All sessions: anthropic-primary/claude-opus-5-5 -> ")
        r = await owner_call("son_model_set_session", {"sessionId": "s1", "provider": "anthropic-secondary",
                                                      "model": "claude-opus-5-5"})
        assert not r.isError and "anthropic-secondary/claude-opus-5-5" in r.content[0].text
        r = await owner_call("son_model_fallback_set", {"fallback": [
            {"provider": "anthropic-secondary", "model": "claude-opus-5-5"},
            {"provider": "openai-codex", "model": "gpt-6.1-sol"}]})
        assert not r.isError and "anthropic-primary/claude-opus-5-5 -> anthropic-secondary/claude-opus-5-5 -> " \
                                  "openai-codex/gpt-6.1-sol" in r.content[0].text
        r = await owner_call("son_model_set_all", {"provider": "anthropic-secondary", "model": "claude-opus-5-5"})
        assert not r.isError and "All sessions now use anthropic-secondary/claude-opus-5-5" in r.content[0].text
        r = await owner_call("son_model_status", {"sessionKey": "agent:main:telegram:dm:9"})
        assert not r.isError and "anthropic-secondary/claude-opus-5-5" in r.content[0].text
        assert "SECRET" not in r.content[0].text and "sk-" not in r.content[0].text
        r = await owner_call("son_model_set_all", {"provider": "made-up", "model": "x"})
        assert r.isError and r.content[0].text.startswith("Model control error:")
        r = await owner_call("son_model_set_session", {"provider": "a", "model": "b"})
        assert r.isError and "sessionKey or sessionId" in r.content[0].text
    finally:
        server.close()
        await server.wait_closed()


async def test_owner_gate_and_missing_socket(monkeypatch):
    monkeypatch.setenv("AITEAM_SON_OWNERS", "jc@aiteam.space")
    conn = Connector([SonLink("son", "http://box.invalid", "t")], "none", github=False,
                     model_control={"socket": str(_short_tmp() / "absent.sock")})
    r = await conn.call("son_model_set_all", {"provider": "p", "model": "m"}, caller="stranger@example.com")
    assert r.isError and "owner" in r.content[0].text
    r = await conn.call("son_model_status", {}, caller="stranger@example.com")
    assert r.isError and "owner" in r.content[0].text
    r = await conn.call("son_model_status", {}, caller="JC@aiteam.space")
    assert r.isError and "not running on this Son yet" in r.content[0].text
    off = Connector([SonLink("son", "http://box.invalid", "t")], "none", github=False, model_control=False)
    assert not any(t.name in mc.MODEL_TOOLS for t in await off.list_tools())


def test_hook_manifest():
    text = (HANDLER.parent / "HOOK.yaml").read_text()
    assert "gateway:startup" in text and "name: aiteam-model-control" in text
    assert callable(H.handle)


async def test_session_ids_must_agree(ctl):
    c, runner, cfg = ctl
    r = await c.dispatch({"verb": "set_session", "args": {
        "session_key": "agent:main:discord:group:1:2", "session_id": "s2",
        "provider": "anthropic-primary", "model": "claude-opus-5-5"}})
    assert r["ok"] is False and "different" in r["error"]
    assert runner.evicted == []


async def test_credentials_failure_does_not_write(ctl):
    c, runner, cfg = ctl
    cfg.credentials_ok = lambda *a: False
    r = await c.dispatch({"verb": "set_global", "args": {"provider": "anthropic-secondary", "model": "claude-opus-5-5"}})
    assert r["ok"] is False and not cfg.writes
    r = await c.dispatch({"verb": "set_session", "args": {"session_id": "s1", "provider": "anthropic-secondary", "model": "claude-opus-5-5"}})
    assert r["ok"] is False and not runner.evicted


def test_owners_fail_closed(monkeypatch):
    monkeypatch.delenv("AITEAM_SON_OWNERS", raising=False)
    monkeypatch.delenv("AITEAM_GATE_EXEMPT", raising=False)
    assert not mc.caller_allowed(None)
    assert not mc.caller_allowed("")
    assert not mc.caller_allowed("not-email")
    assert mc.caller_allowed("owner@example.com")
    assert not mc.caller_allowed("other@example.com", {"owner@example.com"})


async def test_unknown_models_are_rejected_for_native_and_custom_providers(ctl):
    c, runner, cfg = ctl
    cfg.cfg["custom_providers"] = [{"name": "custom-lab", "model": "known-model", "api_key": "test-secret"}]
    for provider in ("custom-lab", "openai-codex", "openrouter"):
        r = await c.dispatch({"verb": "set_global", "args": {"provider": provider, "model": "unlisted-model"}})
        assert not r["ok"], provider
    assert cfg.writes == []


async def test_channel_override_is_shown_and_all_replaces_it(ctl):
    c, runner, cfg = ctl
    key = "agent:main:discord:group:1:2"
    runner.session_store.entries[key].origin = "test-source"
    runner._channel_override_for = lambda source: SimpleNamespace(model="claude-sonnet-5-5", provider="anthropic-primary") if source else None
    r = await c.dispatch({"verb": "status", "args": {"session_key": key}})
    assert r["session"]["source"] == "channel_override"
    assert r["session"]["effective"]["model"] == "claude-sonnet-5-5"
    r = await c.dispatch({"verb": "set_global", "args": {"provider": "anthropic-secondary", "model": "claude-opus-5-5"}})
    assert r["ok"]
    r = await c.dispatch({"verb": "status", "args": {"session_key": key}})
    assert r["session"]["effective"] == {"provider": "anthropic-secondary", "model": "claude-opus-5-5"}


async def test_status_pagination(ctl):
    c, _, _ = ctl
    one = await c.dispatch({"verb": "status", "args": {"limit": 1}})
    assert one["hasMore"] is True and one["nextOffset"] == 1
    two = await c.dispatch({"verb": "status", "args": {"limit": 1, "offset": 1}})
    assert two["hasMore"] is False
    assert one["sessions"][0]["sessionId"] != two["sessions"][0]["sessionId"]


async def test_new_switch_cancels_old_one_turn_restore(ctl):
    c, runner, cfg = ctl
    key = "agent:main:discord:group:1:2"
    runner._pending_one_turn_model_restores = {key: {"model": "old-model"}}
    r = await c.dispatch({"verb": "set_session", "args": {"session_key": key, "provider": "anthropic-secondary", "model": "claude-opus-5-5"}})
    assert r["ok"] and key not in runner._pending_one_turn_model_restores


async def test_multiplex_gateway_fails_closed_without_touching_other_profiles(ctl):
    c, runner, cfg = ctl
    runner.config = SimpleNamespace(multiplex_profiles=True)
    r = await c.dispatch({"verb": "set_global", "args": {"provider": "anthropic-secondary", "model": "claude-opus-5-5"}})
    assert r["ok"] is False and "multiplex" in r["error"] and cfg.writes == []


async def test_all_reports_partial_failure_instead_of_false_success(ctl):
    c, runner, cfg = ctl
    setter = runner.session_store.set_model_override
    def fail_one(key, value):
        if key.endswith(":9"):
            raise OSError("test persistence failure")
        return setter(key, value)
    runner.session_store.set_model_override = fail_one
    r = await c.dispatch({"verb": "set_global", "args": {"provider": "anthropic-secondary", "model": "claude-opus-5-5"}})
    assert not r["ok"] and r["partial"] is True
    assert r["primary"]["provider"] == "anthropic-secondary" and len(r["failed_sessions"]) == 1
    assert "partial" in r["error"].lower()



def test_scrubber_keeps_legitimate_ids_but_strips_secret_fields():
    r = H.scrub({"sessionKey": "key", "providers": {"oauth-backup": {"models": ["m"]}},
                 "credentials_ok": True, "api_key": "not-real", "access_token": "not-real", "key_cmd": "not-real"})
    assert r == {"sessionKey": "key", "providers": {"oauth-backup": {"models": ["m"]}}, "credentials_ok": True}


def test_legacy_exempt_list_does_not_exclude_a_son_owner(monkeypatch):
    monkeypatch.delenv("AITEAM_SON_OWNERS", raising=False)
    monkeypatch.setenv("AITEAM_GATE_EXEMPT", "operator@example.com")
    assert mc.caller_allowed("owner@example.com")  # Signed Son-bound JWT remains authoritative.
