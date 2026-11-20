"""Owner session control on shared group/thread sessions (gateway hook ``aiteam-model-control``).

A session's stored origin is whoever posted first. With ``group_sessions_per_user: false`` (and in every
thread by default) the session is shared, so the origin is often a bot or another member. The owner may
still control it, but only when the gateway itself authorizes the owner's platform id for that chat.
Another user's DM, or someone else's per-user group session, stays refused.
"""
import asyncio
import importlib.util
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Optional

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "aiteam_model_control_handler_owner", ROOT / "son" / "model_control" / "handler.py")
H = importlib.util.module_from_spec(spec)
spec.loader.exec_module(H)

OWNER = "owner@example.com"
OWNER_DC, OWNER_TG, BOT, OTHER = "111", "222", "999", "333"


@dataclass
class Source:  # the SessionSource fields the gate reads
    platform: NS
    chat_id: str
    chat_type: str = "dm"
    user_id: Optional[str] = None
    thread_id: Optional[str] = None
    user_id_alt: Optional[str] = None
    user_name: Optional[str] = None
    is_bot: bool = False
    role_authorized: bool = False
    delivered_via_upstream_relay: bool = False


def key_for(src, per_user):
    """Mirror of gateway.session.build_session_key for dm / group / thread."""
    if src.chat_type == "dm":
        return f"agent:main:{src.platform.value}:dm:{src.chat_id}"
    parts = ["agent:main", src.platform.value, src.chat_type, src.chat_id]
    if src.thread_id:
        parts.append(src.thread_id)
    if per_user and not src.thread_id and src.user_id:
        parts.append(src.user_id)
    return ":".join(parts)


class Store:
    def __init__(self):
        self.entries = {}

    def add(self, sid, origin, per_user):
        key = key_for(origin, per_user)
        self.entries[key] = NS(session_key=key, session_id=sid, origin=origin)
        return key

    def lookup_by_session_key(self, k):
        return self.entries.get(k)

    def lookup_by_session_id(self, sid):
        return next((e for e in self.entries.values() if e.session_id == sid), None)


class Config:
    def __init__(self, owners):
        self.cfg = {"aiteam_control": {"owners": owners}}

    def load(self):
        return self.cfg


async def _inline(fn, *a, **k):
    return fn(*a, **k)


def make(owners=None, allowed=(OWNER_DC, OWNER_TG, BOT, OTHER), per_user=False, slash_denied=()):
    owners = owners if owners is not None else {OWNER: {"discord": OWNER_DC, "telegram": OWNER_TG}}
    runner = NS(session_store=Store(), calls=[], auth_seen=[], config=NS(multiplex_profiles=False))
    runner._session_key_for_source = lambda s: key_for(s, per_user)

    def authorized(s):
        runner.auth_seen.append(s.user_id)
        return str(s.user_id) in allowed

    runner._is_user_authorized_for_source = authorized
    runner._check_slash_access = lambda s, cmd: "denied" if str(s.user_id) in slash_denied else None

    @asynccontextmanager
    async def scope(source):
        yield

    runner._async_profile_scope_for_source = scope

    async def busy(event, cmd, key, source):
        runner.calls.append((cmd.name, key, source.user_id))

    async def idle(event, source, key, canonical):
        runner.calls.append((canonical, key, source.user_id))
        return True, "reply"

    runner._dispatch_busy_slash_command = busy
    runner._hm_dispatch_canonical_command = idle
    return H.ModelControl(runner, Path("/nonexistent"), config=Config(owners), run_blocking=_inline), runner


@pytest.fixture(autouse=True)
def native(monkeypatch):
    def resolve(cmd):
        canonical = {"reset": "new"}.get(cmd, cmd)
        return NS(name=canonical, cli_only=False) if canonical in {"status", "stop", "new"} else None
    monkeypatch.setitem(sys.modules, "hermes_cli.commands", NS(resolve_command=resolve))
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", NS(MessageEvent=lambda **kw: NS(**kw)))


def run(control, verb, sid, principal=OWNER, **extra):
    return asyncio.run(control.dispatch({"verb": verb, "principal": principal,
                                         "args": {"session_id": sid, **extra}}))


DC, TG = NS(value="discord"), NS(value="telegram")


def test_owner_dm_session_ok():
    control, runner = make()
    key = runner.session_store.add("dm1", Source(TG, "222", "dm", OWNER_TG), per_user=False)
    res = run(control, "session_reset", "dm1")
    assert res["ok"], res
    assert runner.calls == [("new", key, OWNER_TG)]


def test_shared_thread_started_by_bot_ok_when_owner_authorized():
    control, runner = make()
    origin = Source(DC, "chan", "thread", BOT, thread_id="t1", is_bot=True, role_authorized=True)
    key = runner.session_store.add("th1", origin, per_user=False)
    res = run(control, "session_reset", "th1")
    assert res["ok"], res
    # Dispatched as the owner on the exact shared key, never as the bot that posted first.
    assert runner.calls == [("new", key, OWNER_DC)]
    assert OWNER_DC in runner.auth_seen
    assert origin.user_id == BOT  # the stored origin is not mutated


def test_shared_group_session_started_by_other_member_ok():
    control, runner = make()
    key = runner.session_store.add("g1", Source(DC, "chan", "group", OTHER), per_user=False)
    res = run(control, "slash", "g1", command="/status")
    assert res["ok"], res
    assert runner.calls == [("status", key, OWNER_DC)]


def test_shared_thread_refused_when_owner_not_authorized_there():
    control, runner = make(allowed=(BOT, OTHER))
    runner.session_store.add("th1", Source(DC, "chan", "thread", BOT, thread_id="t1", is_bot=True), per_user=False)
    res = run(control, "session_reset", "th1")
    assert not res["ok"] and "not authorized" in res["error"]
    assert runner.calls == []


def test_owner_copy_does_not_inherit_origin_grants():
    # The origin was let in by a role grant / relay delivery; the owner's copy must stand on its own id.
    control, runner = make(allowed=())
    origin = Source(DC, "chan", "thread", BOT, thread_id="t1", role_authorized=True,
                    delivered_via_upstream_relay=True, user_id_alt="alt")
    runner.session_store.add("th1", origin, per_user=False)
    seen = []
    runner._is_user_authorized_for_source = lambda s: seen.append(s) or bool(s.role_authorized or s.delivered_via_upstream_relay)
    res = run(control, "session_stop", "th1")
    assert not res["ok"] and runner.calls == []
    assert seen and all(s.user_id == OWNER_DC and s.user_id_alt is None for s in seen)


def test_shared_session_slash_policy_applies_to_owner():
    control, runner = make(slash_denied=(OWNER_DC,))
    runner.session_store.add("th1", Source(DC, "chan", "thread", BOT, thread_id="t1"), per_user=False)
    res = run(control, "slash", "th1", command="/status")
    assert not res["ok"] and runner.calls == []


def test_another_users_dm_refused():
    control, runner = make()
    runner.session_store.add("dm2", Source(DC, "dmchan", "dm", OTHER), per_user=False)
    res = run(control, "session_reset", "dm2")
    assert not res["ok"] and "not bound" in res["error"]
    assert runner.calls == []


def test_another_members_per_user_group_session_refused():
    # group_sessions_per_user: true -> OTHER's group session is theirs alone; the owner's copy keys elsewhere.
    control, runner = make(per_user=True)
    runner.session_store.add("g2", Source(DC, "chan", "group", OTHER), per_user=True)
    res = run(control, "session_reset", "g2")
    assert not res["ok"] and runner.calls == []


def test_unknown_principal_still_refused_on_shared_session():
    control, runner = make()
    runner.session_store.add("th1", Source(DC, "chan", "thread", BOT, thread_id="t1"), per_user=False)
    res = run(control, "session_reset", "th1", principal="intruder@example.com")
    assert not res["ok"] and runner.calls == []


@pytest.mark.parametrize("ids", [[OWNER_DC, "444"], ["444", int(OWNER_DC)], (" 111 ",)])
def test_list_form_owner_ids(ids):
    control, runner = make(owners={OWNER: {"discord": list(ids)}})
    key = runner.session_store.add("dm1", Source(DC, "dmchan", "dm", OWNER_DC), per_user=False)
    res = run(control, "session_stop", "dm1")
    assert res["ok"], res
    assert runner.calls == [("stop", key, OWNER_DC)]


def test_list_form_shared_session_uses_the_authorized_id():
    control, runner = make(owners={OWNER: {"discord": ["555", OWNER_DC]}}, allowed=(OWNER_DC,))
    key = runner.session_store.add("th1", Source(DC, "chan", "thread", BOT, thread_id="t1"), per_user=False)
    res = run(control, "session_stop", "th1")
    assert res["ok"], res
    assert runner.calls == [("stop", key, OWNER_DC)]


@pytest.mark.parametrize("raw,expected", [
    ("111", {"111"}), (111, {"111"}), ([111, "222", ""], {"111", "222"}),
    (None, set()), (True, set()), ({"x": 1}, set()), ([None, False], set()),
])
def test_owner_ids_normalised(raw, expected):
    assert H._owner_ids({"discord": raw}, "discord") == expected
