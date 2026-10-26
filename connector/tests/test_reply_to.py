"""reply_to_message_id on Discord messages_send: native reply that wakes the ASKER, self case via webhook + link."""
import json

import httpx
import mcp.types as types
import pytest

from aiteam_connector.discord_relay import DiscordRelay, RelayError
from aiteam_connector.server import Connector, SonLink

PARENT, THREAD, GUILD = "100000000000000001", "100000000000000002", "100000000000000100"
ME, COORD, OWNER, OTHER = "100000000000000009", "100000000000000010", "100000000000000011", "100000000000000012"
MSG = {  # message id -> (author id, is bot, extra)
    "200000000000000001": (COORD, True, {}),     # the coordinator bot asked, and tagged OTHER in it
    "200000000000000002": (OWNER, False, {}),       # the owner (a person) asked
    "200000000000000003": (ME, True, {}),        # this box's own bot asked
    "200000000000000004": ("999", True, {"webhook_id": "7"}),
}


class Fake:
    def __init__(self, drop_reference=False):
        self.posts, self.webhook_posts = [], []
        self.drop_reference = drop_reference

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        if p == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": ME})
        if p == f"/api/v10/channels/{THREAD}":
            return httpx.Response(200, json={"id": THREAD, "type": 11, "parent_id": PARENT, "guild_id": GUILD})
        if p.startswith(f"/api/v10/channels/{THREAD}/messages/") and m == "GET":
            mid = p.rsplit("/", 1)[1]
            if mid in MSG:
                a, bot, extra = MSG[mid]
                return httpx.Response(200, json={"id": mid, "author": {"id": a, "bot": bot}, **extra})
            for pid, body in self.posts:
                if pid == mid:
                    ref = None if self.drop_reference else body.get("message_reference")
                    return httpx.Response(200, json={"id": mid, "message_reference": ref})
            return httpx.Response(404, json={"message": "Unknown Message"})
        if p == f"/api/v10/channels/{THREAD}/messages" and m == "POST":
            assert req.headers["authorization"] == "Bot bot-token"
            body = json.loads(req.content)
            mid = str(300000000000000000 + len(self.posts))
            self.posts.append((mid, body))
            return httpx.Response(200, json={"id": mid})
        if p == f"/api/v10/channels/{PARENT}/webhooks":
            return httpx.Response(200, json=[{"id": "7", "token": "wtok", "name": "aiteam connector"}])
        if p == "/api/v10/webhooks/7/wtok" and m == "POST":
            body = json.loads(req.content)
            assert "message_reference" not in body
            self.webhook_posts.append(body)
            return httpx.Response(200, json={"id": "555", "channel_id": THREAD})
        if p.startswith("/api/v10/webhooks/7/wtok/messages/"):
            return httpx.Response(200, json={"id": "555", "channel_id": THREAD})
        return httpx.Response(404, json={"message": f"unhandled {m} {p}"})


def relay(f):
    return DiscordRelay("bot-token", http=httpx.AsyncClient(transport=httpx.MockTransport(f)))


TARGET = f"discord:{PARENT}:{THREAD}"


async def test_reply_to_bot_asker_threads_and_wakes_asker_not_its_tags():
    f = Fake()
    out = await relay(f).send(TARGET, "Approved, go ahead.", reply_to="200000000000000001")
    assert out["threaded"] == "native" and out["delivered"] and out["replied_to_author"] == COORD
    (_, body), = f.posts
    assert body["message_reference"] == {"message_id": "200000000000000001", "channel_id": THREAD, "fail_if_not_exists": True}
    assert body["content"] == f"<@{COORD}> Approved, go ahead.", "bot asker gets an inline tag (Hermes bot gate)"
    assert body["allowed_mentions"] == {"parse": [], "users": [COORD], "replied_user": True}
    assert OTHER not in json.dumps(body), "the asker's own tags are not copied: no wrong agent woken"
    assert out["wakes"] == [COORD]


async def test_reply_to_human_uses_reply_ping_only():
    f = Fake()
    out = await relay(f).send(TARGET, "Done.", reply_to="200000000000000002")
    (_, body), = f.posts
    assert body["content"] == "Done." and body["allowed_mentions"]["replied_user"] is True
    assert out["wakes"] == [OWNER]


async def test_wake_author_false_is_silent():
    f = Fake()
    out = await relay(f).send(TARGET, "FYI only.", reply_to="200000000000000001", wake_author=False)
    (_, body), = f.posts
    assert body["content"] == "FYI only." and body["allowed_mentions"] == {"parse": [], "users": [], "replied_user": False}
    assert out["wakes"] == []


async def test_self_asker_falls_back_to_webhook_with_jump_link():
    f = Fake()
    out = await relay(f).send(TARGET, "Answer.", reply_to="200000000000000003")
    assert f.posts == [] and out["threaded"] == "link" and out["via"] == "webhook"
    (body,) = f.webhook_posts
    assert body["content"] == f"↪ Reply to https://discord.com/channels/{GUILD}/{THREAD}/200000000000000003\n<@{ME}> Answer."


async def test_webhook_asker_also_links():
    f = Fake()
    out = await relay(f).send(TARGET, "x", reply_to="200000000000000004")
    assert out["threaded"] == "link" and "<@999>" not in f.webhook_posts[0]["content"]


async def test_long_reply_only_first_chunk_references():
    f = Fake()
    out = await relay(f).send(TARGET, "a" * 4500, reply_to="200000000000000002")
    assert len(f.posts) == 3 and len(out["message_ids"]) == 3
    assert "message_reference" in f.posts[0][1] and all("message_reference" not in b for _, b in f.posts[1:])


async def test_errors():
    with pytest.raises(RelayError, match="not in channel"):
        await relay(Fake()).send(TARGET, "x", reply_to="200000000000000099")
    with pytest.raises(RelayError, match="numeric"):
        await relay(Fake()).send(TARGET, "x", reply_to="abc")
    with pytest.raises(RelayError, match="no reply link"):
        await relay(Fake(drop_reference=True)).send(TARGET, "x", reply_to="200000000000000002")


async def test_connector_schema_and_routing():
    f = Fake()
    conn = Connector([SonLink("my-son", "http://box.invalid", "t")], "all", discord=relay(f))

    class Up:
        async def tools(self):
            return [types.Tool(name="messages_send", description="Send.", inputSchema={
                "type": "object", "properties": {"target": {"type": "string"}, "message": {"type": "string"}}})]

        async def call(self, name, args):
            raise AssertionError("Discord must never fall back to upstream")
    conn.upstreams = {"my-son": Up()}
    ms = next(t for t in await conn.list_tools() if t.name == "messages_send")
    assert ms.inputSchema["properties"]["reply_to_message_id"]["pattern"] == "^[0-9]{15,22}$"
    assert "wake_author" in ms.inputSchema["properties"] and "reply_to_message_id" in ms.description
    r = await conn.call("messages_send", {"target": TARGET, "message": "ok", "reply_to_message_id": "200000000000000002"})
    assert not r.isError and json.loads(r.content[0].text)["threaded"] == "native"
    # non-Discord target with reply_to: clear refusal, nothing sent
    r = await conn.call("messages_send", {"target": "telegram:-100123", "message": "ok", "reply_to_message_id": "5"})
    assert r.isError and "Discord targets only" in r.content[0].text


async def test_schema_untouched_without_discord_relay():
    conn = Connector([SonLink("my-son", "http://box.invalid", "t")], "all")

    class Up:
        async def tools(self):
            return [types.Tool(name="messages_send", description="Send.", inputSchema={"type": "object", "properties": {}})]
    conn.upstreams = {"my-son": Up()}
    ms = next(t for t in await conn.list_tools() if t.name == "messages_send")
    assert "reply_to_message_id" not in ms.inputSchema["properties"]
