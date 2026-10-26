"""Thread member add/remove (owner 2026-10-13): only on the owner's ask, verified by read-back, never automatic."""
import json

import httpx

from aiteam_connector.discord_relay import DiscordRelay, RelayError
from aiteam_connector.server import Connector, SonLink
from aiteam_connector.usage_guide import GUIDE, INSTRUCTIONS

from .test_discord_threads import PARENT, SON, THREAD, Fake

OWNER = "100000000000000042"


class MemberFake(Fake):
    def __init__(self, archived=False, members=(), fail=None):
        super().__init__(archived)
        self.members, self.fail, self.ops = set(members), fail, []

    def h(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path.replace("/api/v10", ""), req.method
        base = f"/channels/{THREAD}/thread-members"
        if p == base and m == "GET":
            return httpx.Response(200, json=[{"user_id": u, "id": THREAD} for u in sorted(self.members)])
        if p.startswith(base + "/"):
            uid = p.rsplit("/", 1)[1]
            if m == "GET":
                return (httpx.Response(200, json={"user_id": uid}) if uid in self.members
                        else httpx.Response(404, json={"message": "Unknown Member"}))
            if self.thread["thread_metadata"]["archived"]:
                return httpx.Response(400, json={"message": "Thread is archived"})
            if self.fail:
                return httpx.Response(self.fail, json={"message": "Missing Permissions"})
            self.ops.append((m, uid))
            (self.members.add if m == "PUT" else self.members.discard)(uid)
            return httpx.Response(204)
        return super().h(req)


def relay(f):
    return DiscordRelay("bot-token", http=httpx.AsyncClient(transport=httpx.MockTransport(f.h)))


async def test_add_then_remove_is_verified():
    f = MemberFake()
    r = relay(f)
    out = await r.thread_member(THREAD, OWNER, add=True)
    assert out["member"] and out["verified"] and out["changed"] and f.ops == [("PUT", OWNER)]
    again = await r.thread_member(THREAD, f"<@{OWNER}>", add=True)          # idempotent, mention form accepted
    assert again["member"] and not again["changed"] and len(f.ops) == 1
    out = await r.thread_member(THREAD, OWNER, add=False)
    assert out["member"] is False and out["changed"] and f.ops[-1] == ("DELETE", OWNER)
    assert (await r.thread_members(THREAD))["user_ids"] == []


async def test_archived_thread_reopened_for_add_and_rearchived_after_remove():
    f = MemberFake(archived=True)
    out = await relay(f).thread_member(THREAD, OWNER, add=True)
    assert out["reopened"] and f.thread["thread_metadata"]["archived"] is False   # it needs the person: stays open
    f.thread["thread_metadata"]["archived"] = True
    await relay(f).thread_member(THREAD, OWNER, add=False)
    assert f.patches[-1] == {"archived": True} and OWNER not in f.members


async def test_permission_error_is_plain():
    f = MemberFake(fail=403)
    out = await relay(f).thread_member(THREAD, OWNER, add=False)          # not a member: no-op, no API write
    assert out["member"] is False and not out["changed"] and not f.ops
    f.members.add(OWNER)
    try:
        await relay(f).thread_member(THREAD, OWNER, add=False)
        raise AssertionError("expected RelayError")
    except RelayError as e:
        assert e.status == 403 and "Manage Threads" in str(e)


async def test_bad_user_id():
    try:
        await relay(MemberFake()).thread_member(THREAD, "@owner", add=True)
        raise AssertionError("expected RelayError")
    except RelayError as e:
        assert e.status == 400


async def test_create_adds_owner_when_asked():
    f = MemberFake()
    made = await relay(f).thread_create(PARENT, "Task: new", message=f"<@{SON}> go", add_user_ids=[OWNER])
    assert made["members_added"][0]["member"] and OWNER in f.members
    f2 = MemberFake()
    made = await relay(f2).thread_create(PARENT, "Task: plain")
    assert "members_added" not in made and not f2.ops                              # nobody added unless passed


async def test_connector_tools_and_rule_text():
    son = SonLink("my-son", "http://box", "t")
    f = MemberFake()
    c = Connector([son], passthrough="none", github=False, discord=relay(f))
    tools = {t.name: t for t in await c.list_tools()}
    for n in ("discord_thread_member_add", "discord_thread_member_remove", "discord_thread_members"):
        assert n in tools
    assert "never remove the owner automatically" in tools["discord_thread_member_remove"].description
    assert tools["discord_thread_member_remove"].annotations.destructiveHint is True
    assert "add_user_ids" in tools["discord_thread_create"].inputSchema["properties"]
    res = await c.call("discord_thread_member_add", {"thread_id": THREAD, "user_id": OWNER})
    assert not res.isError and json.loads(res.content[0].text)["member"] is True
    res = await c.call("discord_thread_member_remove", {"thread_id": THREAD, "user_id": OWNER})
    assert not res.isError and json.loads(res.content[0].text)["member"] is False
    res = await c.call("discord_thread_member_add", {"thread_id": THREAD})
    assert res.isError and "user_id is required" in res.content[0].text
    g = GUIDE.lower()
    assert "never open a thread on your own initiative" in g and "never remove the user automatically" in g
    assert "add_user_ids" in GUIDE and "never remove the user" in INSTRUCTIONS


async def test_membership_view_in_list_and_get():
    f = MemberFake(members=[OWNER])
    r = relay(f)
    out = await r.threads_list(PARENT, member_id=OWNER)
    assert out["member_of"] == [THREAD] and out["threads"][0]["is_member"] is True
    assert out["threads"][0]["member_ids"] == [OWNER]
    plain = await r.threads_list(PARENT)
    assert "member_ids" not in plain["threads"][0] and "member_of" not in plain   # no extra calls by default
    f.members.clear()
    got = await r.thread_get_members(THREAD, f"<@{OWNER}>")
    assert got["is_member"] is False and got["member_ids"] == []
    son = SonLink("my-son", "http://box", "t")
    c = Connector([son], passthrough="none", github=False, discord=r)
    res = await c.call("discord_threads_list", {"channel_id": PARENT, "member_id": OWNER})
    assert not res.isError and json.loads(res.content[0].text)["member_of"] == []
    res = await c.call("discord_thread_get", {"thread_id": THREAD, "include_members": True})
    assert not res.isError and json.loads(res.content[0].text)["member_ids"] == []
