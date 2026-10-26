"""Discord relay: webhook author (wakes tagged Sons), read-back proof, thread resolution, no upstream fallback."""
import json

import httpx

from aiteam_connector.discord_relay import DiscordRelay, RelayError
from aiteam_connector.server import Connector, SonLink

PARENT, THREAD, SON = "100000000000000001", "100000000000000002", "100000000000000009"


class FakeDiscord:
    def __init__(self, hooks=None):
        self.hooks = hooks if hooks is not None else []
        self.posts, self.calls = [], []

    def handler(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        self.calls.append((m, p, dict(req.url.params)))
        if p == f"/api/v10/channels/{THREAD}":
            return httpx.Response(200, json={"id": THREAD, "type": 11, "parent_id": PARENT})
        if p == f"/api/v10/channels/{PARENT}" and m == "GET":
            return httpx.Response(200, json={"id": PARENT, "type": 0})
        if p == f"/api/v10/channels/{PARENT}/webhooks":
            assert req.headers["authorization"].startswith("Bot ")
            if m == "GET":
                return httpx.Response(200, json=self.hooks)
            h = {"id": "7", "token": "wtok", "name": json.loads(req.content)["name"]}
            self.hooks.append(h)
            return httpx.Response(200, json=h)
        if p == "/api/v10/webhooks/7/wtok" and m == "POST":
            assert "authorization" not in req.headers          # webhook posts never carry the bot token
            body = json.loads(req.content)
            mid = str(500 + len(self.posts))
            self.posts.append((dict(req.url.params), body))
            return httpx.Response(200, json={"id": mid, "channel_id": req.url.params.get("thread_id", PARENT)})
        if p.startswith("/api/v10/webhooks/7/wtok/messages/"):
            return httpx.Response(200, json={"id": p.rsplit("/", 1)[1],
                                             "channel_id": req.url.params.get("thread_id", PARENT)})
        return httpx.Response(404, json={"message": "Unknown"})


def relay(fake):
    return DiscordRelay("bot-token", http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))


async def test_thread_target_posts_via_webhook_and_reads_back():
    fake = FakeDiscord()
    out = await relay(fake).send(f"discord:{THREAD}", f"<@{SON}> please reply here")
    assert out["delivered"] is True and out["via"] == "webhook" and "mirrored" not in out
    assert out["channel_id"] == THREAD and out["wakes"] == [SON]
    params, body = fake.posts[0]
    assert params == {"wait": "true", "thread_id": THREAD}              # webhook lives on the parent, posts in thread
    assert body["allowed_mentions"] == {"parse": [], "users": [SON]}    # only explicit tags ping; no @everyone
    assert any(p.startswith("/api/v10/webhooks/7/wtok/messages/") for _, p, _ in fake.calls)


async def test_parent_thread_form_and_webhook_reuse():
    fake = FakeDiscord(hooks=[{"id": "7", "token": "wtok", "name": "aiteam connector"}])
    r = relay(fake)
    await r.send(f"discord:{PARENT}:{THREAD}", "one")
    await r.send(f"discord:{PARENT}:{THREAD}", "two")
    assert sum(1 for m, p, _ in fake.calls if p.endswith("/webhooks") and m == "POST") == 0   # reused, not created
    assert sum(1 for m, p, _ in fake.calls if p.endswith("/webhooks")) == 1                 # cached after first


async def test_long_message_is_chunked():
    fake = FakeDiscord()
    out = await relay(fake).send(f"discord:{PARENT}", "x" * 4500)
    assert len(out["message_ids"]) == 3 and all(len(b["content"]) <= 2000 for _, b in fake.posts)


async def test_bad_target_and_errors():
    r = relay(FakeDiscord())
    for t in ("discord:#general", "telegram:1", "discord:abc"):
        try:
            await r.send(t, "hi")
            raise AssertionError("expected RelayError")
        except RelayError as e:
            assert e.status == 400


async def test_connector_routes_discord_to_relay_and_never_upstream():
    fake = FakeDiscord()
    conn = Connector([SonLink("my-son", "http://box.invalid", "t", "http://up.invalid/mcp")], "all", discord=relay(fake))

    class Boom:
        async def call(self, *a):
            raise AssertionError("upstream messages_send must not be used for Discord targets")
    conn.upstreams["my-son"] = Boom()
    res = await conn.call("messages_send", {"target": f"discord:{THREAD}", "message": f"<@{SON}> go"})
    assert not res.isError
    d = json.loads(res.content[0].text)
    assert d["delivered"] is True and d["wakes"] == [SON]
    bad = await conn.call("messages_send", {"target": "discord:#dev-team", "message": "x"})
    assert bad.isError and "numeric ids" in bad.content[0].text
