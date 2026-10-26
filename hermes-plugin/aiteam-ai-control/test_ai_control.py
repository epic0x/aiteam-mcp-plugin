"""Tests for the aiteam-ai-control Hermes plugin. Run with the Hermes venv:

    /opt/hermes/.venv/bin/python -m pytest -q hermes-plugin/aiteam-ai-control/test_ai_control.py
"""
import importlib.util
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("aiteam_ai_control", HERE / "__init__.py")
ac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ac)

KEY = "test-api-server-key-0123456789"


@pytest.fixture()
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("API_SERVER_KEY", KEY)
    monkeypatch.setattr(ac, "_hermes_home", lambda: tmp_path)
    ac.SIGNINS = ac.SignIns()
    srv = ac.start_server(port=0)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    ac.stop_server()


def call(base, method, path, body=None, key=KEY):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), dict(e.headers)


def test_auth_required(server):
    assert call(server, "GET", "/ai/status", key=None)[0] == 401
    assert call(server, "GET", "/ai/status", key="wrong")[0] == 401
    code, body, headers = call(server, "GET", "/ai/nope")
    assert code == 404 and headers.get("Cache-Control") == "no-store"


def test_no_key_configured(monkeypatch, server):
    monkeypatch.delenv("API_SERVER_KEY")
    assert call(server, "GET", "/ai/status")[0] == 503


def test_status_shape(monkeypatch, server):
    monkeypatch.setattr(ac, "_load_config", lambda: {
        "model": {"provider": "anthropic", "default": "claude-opus-5-5"},
        "fallback_providers": [{"provider": "openai-codex", "model": "gpt-5.5"}]})
    monkeypatch.setattr(ac, "_authenticated", lambda cfg: [{"slug": "anthropic", "name": "Anthropic", "models": ["a"]}])
    monkeypatch.setattr(ac, "_pool_summary", lambda p: [{"id": "x1", "label": "me@x", "auth_type": "oauth", "status": "ok"}] if p == "anthropic" else [])
    from datetime import datetime
    entries = [SimpleNamespace(session_key="k1", session_id="s1", platform=SimpleNamespace(value="discord"),
                               display_name="t", model_override={"provider": "openai-codex", "model": "gpt-5.5"},
                               updated_at=datetime(2026, 10, 26)),
               SimpleNamespace(session_key="k2", session_id="s2", platform="telegram", display_name=None,
                               model_override=None, updated_at=datetime(2026, 10, 26))]
    runner = SimpleNamespace(_session_model_overrides={}, session_store=SimpleNamespace(list_sessions=lambda: entries))
    monkeypatch.setattr(ac, "_runner", lambda: runner)
    code, body, _ = call(server, "GET", "/ai/status")
    assert code == 200
    assert body["global"] == {"provider": "anthropic", "model": "claude-opus-5-5"}
    assert body["fallback"] == [{"provider": "openai-codex", "model": "gpt-5.5"}]
    by = {p["provider"]: p for p in body["providers"]}
    assert by["anthropic"]["signed_in"] and by["anthropic"]["credentials"][0]["label"] == "me@x"
    assert by["openai-codex"]["signed_in"] is False
    assert body["sessions"][0]["model"] == "gpt-5.5" and body["sessions"][0]["override"] is True
    assert body["sessions"][1]["model"] == "claude-opus-5-5" and body["sessions"][1]["override"] is False
    assert "access_token" not in json.dumps(body)


class FakeStore:
    def __init__(self):
        self.entries = {"k1": SimpleNamespace(session_key="k1", session_id="s1", model_override={"model": "old"}),
                        "k2": SimpleNamespace(session_key="k2", session_id="s2", model_override=None)}
        self.set_calls = []

    def lookup_by_session_key(self, k):
        return self.entries.get(k)

    def lookup_by_session_id(self, s):
        return next((e for e in self.entries.values() if e.session_id == s), None)

    def set_model_override(self, k, ov):
        self.set_calls.append((k, ov))
        self.entries[k].model_override = ov

    def list_sessions(self):
        return list(self.entries.values())


def _fake_switch(monkeypatch):
    res = SimpleNamespace(success=True, new_model="gpt-5.5", target_provider="openai-codex", api_key="SECRET",
                          base_url="https://x", api_mode="codex_responses", request_overrides={},
                          runtime_capabilities={}, error_message="")
    monkeypatch.setattr(ac, "_switch", lambda p, m, is_global: res)
    return res


def test_model_session_scope(monkeypatch, server):
    _fake_switch(monkeypatch)
    store = FakeStore()
    evicted = []
    runner = SimpleNamespace(_session_model_overrides={}, session_store=store, _evict_cached_agent=evicted.append)
    monkeypatch.setattr(ac, "_runner", lambda: runner)
    code, body, _ = call(server, "POST", "/ai/model", {"provider": "openai-codex", "model": "gpt-5.5",
                                                     "scope": "session", "session_id": "s2"})
    assert code == 200 and body["session_key"] == "k2" and body["sessions_changed"] == 1
    assert runner._session_model_overrides["k2"]["model"] == "gpt-5.5"
    assert evicted == [] and "SECRET" not in json.dumps(body)
    assert call(server, "POST", "/ai/model", {"provider": "a", "model": "b", "scope": "session",
                                             "session_key": "nope"})[0] == 404


def test_model_all_scope_clears_pins(monkeypatch, server):
    _fake_switch(monkeypatch)
    persisted = []
    import hermes_cli.model_switch as ms
    monkeypatch.setattr(ms, "persist_model_selection", lambda r, p: persisted.append(p))
    store = FakeStore()
    evicted = []
    runner = SimpleNamespace(_session_model_overrides={"k1": {"model": "old"}}, session_store=store,
                             _evict_cached_agent=evicted.append)
    monkeypatch.setattr(ac, "_runner", lambda: runner)
    code, body, _ = call(server, "POST", "/ai/model", {"provider": "openai-codex", "model": "gpt-5.5"})
    assert code == 200 and body["scope"] == "all" and body["sessions_changed"] == 2
    assert persisted and runner._session_model_overrides == {}
    assert ("k1", None) in store.set_calls and evicted == []


def test_model_validation(server):
    assert call(server, "POST", "/ai/model", {"provider": "", "model": "x"})[0] == 400
    assert call(server, "POST", "/ai/model", {"provider": "a", "model": "x", "scope": "some"})[0] == 400


def test_fallback(monkeypatch, server, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model:\n  default: m\n# keep me\n")
    monkeypatch.setattr(ac, "_config_path", lambda: cfg)
    monkeypatch.setattr(ac, "_load_config", lambda: {})
    monkeypatch.setattr(ac, "_authenticated", lambda c: [{"slug": "anthropic"}])
    monkeypatch.setattr(ac, "_pool_summary", lambda p: [])
    code, body, _ = call(server, "PUT", "/ai/fallback", {"chain": [{"provider": "openai-codex", "model": "g"}]})
    assert code == 409
    code, body, _ = call(server, "PUT", "/ai/fallback", {"chain": [{"provider": "anthropic", "model": "claude-sonnet-5-5"}]})
    assert code == 200 and body["fallback"] == [{"provider": "anthropic", "model": "claude-sonnet-5-5"}]
    text = cfg.read_text()
    assert "fallback_providers" in text and "claude-sonnet-5-5" in text and "# keep me" in text


def test_claude_signin_and_credential(monkeypatch, server, caplog):
    caplog.set_level(logging.DEBUG)
    code, body, _ = call(server, "POST", "/ai/signin/start", {"provider": "claude"})
    assert code == 200 and body["needs_code"] is True
    assert body["provider_url"].startswith("https://claude.ai/oauth/authorize?")
    ticket, sid = body["ticket"], body["signin_id"]
    assert len(ticket) >= 32
    rec = ac.SIGNINS.get(sid)
    assert "ticket" not in rec and rec["ticket_hash"] != ticket
    state = rec["oauth_state"]
    st = call(server, "GET", f"/ai/signin/{sid}")[1]
    assert st["state"] == "waiting" and "ticket" not in st

    import agent.anthropic_credentials as anth
    sent = {}

    def fake_post(data, **kw):
        sent.update(json.loads(data))
        return {"access_token": "sk-ant-oat-ACCESS", "refresh_token": "REFRESH", "expires_in": 3600}
    monkeypatch.setattr(anth, "_post_oauth_token", fake_post)
    added = []
    monkeypatch.setattr(ac, "_add_pool_entry", lambda prov, **kw: added.append((prov, kw)) or "me@claude")

    assert call(server, "POST", "/ai/credential", {"ticket": "x" * 43, "secret": "c"})[0] == 404
    assert call(server, "POST", "/ai/credential", {"ticket": ticket, "secret": "CODE#wrongstate"})[0] == 400
    code, body, _ = call(server, "POST", "/ai/credential", {"ticket": ticket, "secret": f"THECODE#{state}"})
    assert code == 200 and body == {"ok": True, "provider": "anthropic", "label": "me@claude", "state": "connected"}
    assert sent["code"] == "THECODE" and sent["code_verifier"] and sent["state"] == state
    assert added[0][0] == "anthropic" and added[0][1]["token"] == "sk-ant-oat-ACCESS"
    # single use
    assert call(server, "POST", "/ai/credential", {"ticket": ticket, "secret": f"THECODE#{state}"})[0] == 410
    assert call(server, "GET", f"/ai/signin/{sid}")[1]["state"] == "connected"
    assert "verifier" not in ac.SIGNINS.get(sid)
    logs = caplog.text
    for secret in ("THECODE", "sk-ant-oat-ACCESS", "REFRESH", ticket, state):
        assert secret not in logs


def test_attempt_limit(monkeypatch, server):
    body = call(server, "POST", "/ai/signin/start", {"provider": "anthropic-key"})[1]
    for _ in range(ac.MAX_ATTEMPTS):
        assert call(server, "POST", "/ai/credential", {"ticket": body["ticket"], "secret": "short"})[0] == 400
    assert call(server, "POST", "/ai/credential", {"ticket": body["ticket"], "secret": "sk-ant-" + "a" * 40})[0] == 410
    assert call(server, "GET", f"/ai/signin/{body['signin_id']}")[1]["state"] == "failed"


def test_api_key_signin(monkeypatch, server):
    added = []
    monkeypatch.setattr(ac, "_add_pool_entry", lambda prov, **kw: added.append((prov, kw)) or kw["label"])
    body = call(server, "POST", "/ai/signin/start", {"provider": "openai-key", "label": "work"})[1]
    assert body["needs_code"] is True and "provider_url" not in body
    code, res, _ = call(server, "POST", "/ai/credential", {"ticket": body["ticket"], "secret": "sk-proj-" + "b" * 40})
    assert code == 200 and res["provider"] == "openai-api" and res["label"] == "work"
    assert "sk-proj" not in json.dumps(res)
    assert added[0][1]["auth_type"] == "api_key"


def test_expiry(monkeypatch, server):
    body = call(server, "POST", "/ai/signin/start", {"provider": "anthropic-key"})[1]
    ac.SIGNINS.get(body["signin_id"])["expires"] = 0
    assert call(server, "POST", "/ai/credential", {"ticket": body["ticket"], "secret": "sk-ant-" + "a" * 40})[0] in (404, 410)


def test_chatgpt_device(monkeypatch, server):
    import hermes_cli.auth_codex as cx
    monkeypatch.setattr(cx, "_codex_request_device_code", lambda i, c: {"user_code": "ABCD-1234", "device_auth_id": "dev", "interval": 5})
    started = []
    real = ac.signin_start
    monkeypatch.setattr(ac, "signin_start", lambda b: real(b, codex_starter=lambda r, d: started.append(d)))
    code, body, _ = call(server, "POST", "/ai/signin/start", {"provider": "chatgpt"})
    assert code == 200 and body["user_code"] == "ABCD-1234" and body["needs_code"] is False
    assert body["provider_url"] == "https://auth.openai.com/codex/device" and started
    assert call(server, "POST", "/ai/credential", {"ticket": body["ticket"], "secret": "x"})[0] == 409


def test_codex_worker_stores(monkeypatch):
    import hermes_cli.auth_codex as cx
    monkeypatch.setattr(cx, "_codex_poll_authorization_code", lambda *a, **k: {"authorization_code": "a", "code_verifier": "v"})
    monkeypatch.setattr(cx, "_codex_exchange_authorization_code", lambda *a: {"access_token": "AT", "refresh_token": "RT"})
    added = []
    monkeypatch.setattr(ac, "_add_pool_entry", lambda prov, **kw: added.append((prov, kw)) or "me@openai")
    import hermes_cli.auth as auth
    monkeypatch.setattr(auth, "mark_provider_active_if_unset", lambda p: None)
    rec = {"id": "si_x", "label": "", "state": "waiting", "expires": ac.time.time() + 900}
    ac._codex_worker(rec, {"device_auth_id": "d", "user_code": "u", "interval": 3})
    assert rec["state"] == "connected" and added[0][0] == "openai-codex" and added[0][1]["refresh_token"] == "RT"


def test_bad_provider_and_body(server):
    assert call(server, "POST", "/ai/signin/start", {"provider": "gemini"})[0] == 400
    assert call(server, "GET", "/ai/signin/si_missing")[0] == 404


def test_register_skips_non_gateway(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hermes", "chat"])
    monkeypatch.delenv("AITEAM_AI_CONTROL_FORCE", raising=False)
    called = []
    monkeypatch.setattr(ac, "start_server", lambda *a, **k: called.append(1))
    ac.register(SimpleNamespace(on_unload=lambda f: None))
    assert not called


def test_missing_oauth_state_rejected(monkeypatch, server):
    st = call(server, "POST", "/ai/signin/start", {"provider": "claude"})[1]
    assert call(server, "POST", "/ai/credential", {"ticket": st["ticket"], "secret": "bare-code"})[0] == 400


def test_provider_error_never_echoes_secret():
    assert ac._clean_err(RuntimeError("opaque-credential-with-no-known-prefix"), "Try again.") == "Try again."


def test_conflicting_key_and_id(monkeypatch, server):
    store = FakeStore()
    monkeypatch.setattr(ac, "_runner", lambda: SimpleNamespace(session_store=store))
    assert call(server, "POST", "/ai/model", {"provider": "a", "model": "b", "scope": "session",
                                             "session_key": "k1", "session_id": "s2"})[0] == 404


def test_failed_session_write_does_not_publish(monkeypatch, server):
    _fake_switch(monkeypatch)
    store = FakeStore()
    def fail(*args):
        raise OSError("test failure")
    store.set_model_override = fail
    runner = SimpleNamespace(session_store=store, _session_model_overrides={})
    monkeypatch.setattr(ac, "_runner", lambda: runner)
    assert call(server, "POST", "/ai/model", {"provider": "a", "model": "b", "scope": "session",
                                             "session_key": "k1"})[0] == 500
    assert not runner._session_model_overrides


def test_all_clears_channel_route_but_keeps_prompt(monkeypatch, server, tmp_path):
    _fake_switch(monkeypatch)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model:\n  provider: anthropic\n  default: old\nplatforms:\n  discord:\n    channel_overrides:\n      '123':\n        model: old-channel\n        provider: anthropic\n        system_prompt: keep-me\n")
    import yaml
    import hermes_cli.model_switch as ms
    monkeypatch.setattr(ms, "persist_model_selection", lambda r, p: None)
    monkeypatch.setattr(ac, "_config_path", lambda: cfg)
    monkeypatch.setattr(ac, "_load_config", lambda: yaml.safe_load(cfg.read_text()))
    ov = SimpleNamespace(model="old-channel", provider="anthropic", system_prompt="keep-me")
    runner = SimpleNamespace(session_store=FakeStore(), _session_model_overrides={},
                             config=SimpleNamespace(platforms={"discord": SimpleNamespace(channel_overrides={"123": ov})}))
    monkeypatch.setattr(ac, "_runner", lambda: runner)
    code, _, _ = call(server, "POST", "/ai/model", {"provider": "a", "model": "b"})
    assert code == 200 and ov.model is None and ov.provider is None and ov.system_prompt == "keep-me"
    row = yaml.safe_load(cfg.read_text())["platforms"]["discord"]["channel_overrides"]["123"]
    assert row == {"system_prompt": "keep-me"}


class FakeDB:
    def __init__(self):
        self.row = {"id": "api-1", "source": "api_server", "title": "Browser", "model_config": {
            "browser_model_lock": {"confirmed": True, "model": "old-api", "provider": "anthropic"}}}
        self.locks = []
        self.models = []
    def list_sessions_rich(self, **kwargs):
        return [self.row] if kwargs.get("offset", 0) == 0 else []
    def get_session(self, sid):
        return self.row if sid == self.row["id"] else None
    def update_session_runtime_lock(self, sid, **kw):
        self.locks.append((sid, kw))
        self.row["model_config"]["browser_model_lock"] = kw
    def update_session_model(self, sid, model, **kw):
        self.models.append((sid, model))


def test_api_only_session_status_and_pin(monkeypatch, server):
    _fake_switch(monkeypatch)
    store = FakeStore()
    store._db = FakeDB()
    runner = SimpleNamespace(session_store=store, _session_model_overrides={})
    monkeypatch.setattr(ac, "_runner", lambda: runner)
    monkeypatch.setattr(ac, "_authenticated", lambda c: [])
    monkeypatch.setattr(ac, "_pool_summary", lambda p: [])
    # Fake routing metadata for status.
    from datetime import datetime
    for e in store.entries.values():
        e.platform, e.display_name, e.updated_at = "discord", "chat", datetime.now()
    row = next(x for x in call(server, "GET", "/ai/status")[1]["sessions"] if x["session_id"] == "api-1")
    assert row["model"] == "old-api" and row["override"]
    code, body, _ = call(server, "POST", "/ai/model", {"provider": "a", "model": "b", "scope": "session", "session_id": "api-1"})
    assert code == 200 and store._db.locks[-1][1]["confirmed"] is True
    assert not runner._session_model_overrides  # no invented gateway routing key


def test_all_clears_api_model_lock(monkeypatch, server):
    _fake_switch(monkeypatch)
    import hermes_cli.model_switch as ms
    monkeypatch.setattr(ms, "persist_model_selection", lambda r, p: None)
    monkeypatch.setattr(ac, "_load_config", lambda: {})
    store = FakeStore()
    store._db = FakeDB()
    runner = SimpleNamespace(session_store=store, _session_model_overrides={})
    monkeypatch.setattr(ac, "_runner", lambda: runner)
    code, body, _ = call(server, "POST", "/ai/model", {"provider": "a", "model": "b"})
    assert code == 200 and body["sessions_changed"] == 3
    assert store._db.locks[-1][1]["confirmed"] is False
