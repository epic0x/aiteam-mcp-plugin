"""Thread targets post INSIDE the thread (no 404 on a thread id in the channel slot) + thread tools (owner 2026-10-13)."""
import json

import httpx

from aiteam_connector.discord_relay import DiscordRelay, RelayError
from aiteam_connector.server import Connector, SonLink

GUILD, PARENT, THREAD, SON = "100000000000000000", "100000000000000001", "100000000000000002", "100000000000000009"


class Fake:
    def __init__(self, archived=False):
        self.thread = {"id": THREAD, "type": 11, "parent_id": PARENT, "guild_id": GUILD, "name": "Task: old",
                       "thread_metadata": {"archived": archived, "locked": False, "auto_archive_duration": 10080}}
        self.calls, self.posts, self.patches = [], [], []

    def h(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path.replace("/api/v10", ""), req.method
        self.calls.append((m, p))
        if p == f"/channels/{THREAD}" and m == "GET":
            return httpx.Response(200, json=self.thread)
        if p == f"/channels/{THREAD}" and m == "PATCH":
            body = json.loads(req.content)
            self.patches.append(body)
            md = self.thread["thread_metadata"]
            for k in ("archived", "locked", "auto_archive_duration"):
                if k in body:
                    md[k] = body[k]
            if "name" in body:
                self.thread["name"] = body["name"]
            return httpx.Response(200, json=self.thread)
        if p == f"/channels/{PARENT}" and m == "GET":
            return httpx.Response(200, json={"id": PARENT, "type": 0, "guild_id": GUILD})
        if p == f"/channels/{THREAD}/webhooks":
            return httpx.Response(404, json={"message": "Unknown Channel"})   # what Discord really says
        if p == f"/channels/{PARENT}/webhooks":
            return httpx.Response(200, json=[{"id": "7", "token": "wtok", "name": "aiteam connector"}])
        if p == "/webhooks/7/wtok" and m == "POST":
            self.posts.append(dict(req.url.params))
            return httpx.Response(200, json={"id": "555555555555555555", "channel_id": req.url.params.get("thread_id", PARENT)})
        if p.startswith("/webhooks/7/wtok/messages/"):
            return httpx.Response(200, json={"id": p.rsplit("/", 1)[1], "channel_id": req.url.params.get("thread_id", PARENT)})
        if p == f"/guilds/{GUILD}/threads/active":
            return httpx.Response(200, json={"threads": [self.thread, {**self.thread, "id": "100000000000000003",
                                                                         "parent_id": "100000000000000099"}]})
        if p == f"/channels/{PARENT}/threads" and m == "POST":
            b = json.loads(req.content)
            return httpx.Response(201, json={**self.thread, "name": b["name"]})
        return httpx.Response(404, json={"message": "Unknown"})


def relay(f):
    return DiscordRelay("bot-token", http=httpx.AsyncClient(transport=httpx.MockTransport(f.h)))


async def test_thread_id_in_channel_slot_posts_inside_thread():
    for target in (f"discord:{THREAD}", f"discord:{PARENT}:{THREAD}", f"discord:{THREAD}:{THREAD}"):
        f = Fake()
        out = await relay(f).send(target, f"<@{SON}> follow-up")
        assert out["delivered"] and out["channel_id"] == THREAD, target
        assert f.posts == [{"wait": "true", "thread_id": THREAD}], target
        assert ("GET", f"/channels/{THREAD}/webhooks") not in f.calls, target


async def test_archived_thread_is_reopened_before_posting():
    f = Fake(archived=True)
    out = await relay(f).send(f"discord:{PARENT}:{THREAD}", "still this task")
    assert out["channel_id"] == THREAD and f.patches[0] == {"archived": False}


async def test_unknown_thread_is_a_clear_error():
    f = Fake()
    try:
        await relay(f).send(f"discord:{PARENT}:100000000000000077", "x")
        raise AssertionError("expected RelayError")
    except RelayError as e:
        assert e.status == 404 and "thread 100000000000000077" in str(e)


async def test_thread_edit_rename_archive_lock():
    f = Fake()
    r = relay(f)
    out = await r.thread_edit(f"discord:{PARENT}:{THREAD}", name="Closed: Task: old")
    assert out["name"] == "Closed: Task: old" and out["changed"] == ["name"]
    out = await r.thread_edit(THREAD, archived=True, locked=True)
    assert out["archived"] and out["locked"]
    try:
        await r.thread_edit(THREAD)
        raise AssertionError("expected RelayError")
    except RelayError as e:
        assert e.status == 400


async def test_rename_archived_thread_reopens_then_rearchives():
    f = Fake(archived=True)
    out = await relay(f).thread_edit(THREAD, name="Task: renamed")
    assert f.patches[0] == {"archived": False}
    assert f.patches[-1] == {"name": "Task: renamed", "archived": True} and out["archived"]


async def test_threads_list_filters_by_parent_and_create():
    f = Fake()
    r = relay(f)
    out = await r.threads_list(PARENT)
    assert [t["thread_id"] for t in out["threads"]] == [THREAD]
    assert out["threads"][0]["target"] == f"discord:{PARENT}:{THREAD}"
    made = await r.thread_create(PARENT, "Task: new", message=f"<@{SON}> go")
    assert made["target"] == f"discord:{PARENT}:{THREAD}" and made["first_message"]["delivered"]


async def test_connector_exposes_thread_tools_only_with_relay():
    son = SonLink("my-son", "http://box", "t")
    names = {t.name for t in await Connector([son], passthrough="none", github=False).list_tools()}
    assert "discord_thread_edit" not in names
    f = Fake()
    c = Connector([son], passthrough="none", github=False, discord=relay(f))
    names = {t.name for t in await c.list_tools()}
    assert {"discord_thread_get", "discord_thread_edit", "discord_thread_create", "discord_threads_list"} <= names
    res = await c.call("discord_thread_edit", {"thread_id": THREAD, "name": "Task: via MCP"})
    assert not res.isError and "Task: via MCP" in res.content[0].text
    res = await c.call("discord_thread_edit", {"name": "x"})
    assert res.isError and "thread_id is required" in res.content[0].text


async def test_rename_rate_limit_is_plain():
    f = Fake()
    orig = f.h

    def h(req):
        if req.method == "PATCH":
            return httpx.Response(429, json={"message": "You are being rate limited.", "retry_after": 312.5})
        return orig(req)
    r = DiscordRelay("bot-token", http=httpx.AsyncClient(transport=httpx.MockTransport(h)))
    try:
        await r.thread_edit(THREAD, name="x")
        raise AssertionError("expected RelayError")
    except RelayError as e:
        assert e.status == 429 and "312.5" in str(e) and "Nothing was changed" in str(e)
