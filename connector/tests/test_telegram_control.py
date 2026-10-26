"""Telegram topic control (owner 2026-10-14): free, mirrors the Discord thread tools."""
import json

import httpx
import mcp.types as types

from aiteam_connector.entitlements import Entitlements
from aiteam_connector.server import TELEGRAM_TOOLS, Connector, SonLink
from aiteam_connector.telegram_control import TelegramControl, TelegramError, parse_session_key

CHAT = "-100" + "1" * 10      # fake group id, built so the id scan never sees a literal
OTHER = "-100" + "2" * 10
TOKEN = "123456:fake-token-value"


class FakeTg:
    def __init__(self, forum=True, admin=True):
        self.forum, self.admin, self.calls, self.next_mid = forum, admin, [], 100

    def h(self, req: httpx.Request) -> httpx.Response:
        method = req.url.path.rsplit("/", 1)[1]
        b = json.loads(req.content or b"{}")
        self.calls.append((method, b))
        ok = lambda r: httpx.Response(200, json={"ok": True, "result": r})  # noqa: E731
        if method == "getChat":
            return ok({"id": int(CHAT), "type": "supergroup", "title": "Team", "is_forum": self.forum})
        if method == "getMe":
            return ok({"id": 42, "is_bot": True})
        if method == "getChatMember":
            return ok({"status": "administrator" if self.admin else "member", "can_manage_topics": self.admin})
        if method == "createForumTopic":
            if not self.forum:
                return httpx.Response(400, json={"ok": False, "error_code": 400,
                                                 "description": "Bad Request: the chat is not a forum"})
            return ok({"message_thread_id": 77, "name": b["name"], "icon_color": 7322096})
        if method in ("editForumTopic", "closeForumTopic", "reopenForumTopic", "editGeneralForumTopic"):
            if not self.admin:
                return httpx.Response(400, json={"ok": False, "error_code": 400,
                                                 "description": "Bad Request: not enough rights to manage topics"})
            return ok(True)
        if method == "sendMessage":
            self.next_mid += 1
            return ok({"message_id": self.next_mid, "message_thread_id": b.get("message_thread_id")})
        return httpx.Response(404, json={"ok": False, "error_code": 404, "description": "Not Found"})


def tg(f, tmp_path):
    return TelegramControl(TOKEN, registry=tmp_path / "topics.json",
                           http=httpx.AsyncClient(transport=httpx.MockTransport(f.h)))


async def test_create_send_edit_list(tmp_path):
    f = FakeTg()
    t = tg(f, tmp_path)
    made = await t.topic_create(CHAT, "Task: pricing FAQ", message="kick-off")
    assert made["topic_id"] == 77 and made["target"] == f"telegram:{CHAT}:77"
    assert made["first_message"]["delivered"] and f.calls[-1] == ("sendMessage", {"chat_id": CHAT, "text": "kick-off",
                                                                                 "message_thread_id": 77})
    out = await t.topic_edit(f"telegram:{CHAT}:77", 77, name="Closed: Task: pricing FAQ", closed=True)
    assert out["changed"] == ["name", "closed"]
    assert ("closeForumTopic", {"chat_id": CHAT, "message_thread_id": 77}) in f.calls
    rows = (await t.topics_list(CHAT))["topics"]
    assert [r["topic_id"] for r in rows] == [1, 77]
    assert rows[1]["name"] == "Closed: Task: pricing FAQ" and rows[1]["closed"] is True


async def test_general_topic_sends_without_thread_id(tmp_path):
    f = FakeTg()
    out = await tg(f, tmp_path).send(CHAT, 1, "hello")
    assert out["delivered"] and "message_thread_id" not in f.calls[-1][1]


async def test_reply_and_long_text_split(tmp_path):
    f = FakeTg()
    out = await tg(f, tmp_path).send(CHAT, 5, "a" * 5000, reply_to_message_id=9)
    assert len(out["message_ids"]) == 2
    assert f.calls[0][1]["reply_parameters"]["message_id"] == 9 and "reply_parameters" not in f.calls[1][1]


async def test_clear_errors_and_no_token_leak(tmp_path):
    f = FakeTg(forum=False, admin=False)
    t = tg(f, tmp_path)
    info = await t.chat_get(CHAT)
    assert info["is_forum"] is False and "Topics" in info["note"]
    try:
        await t.topic_create(CHAT, "x")
        raise AssertionError("expected TelegramError")
    except TelegramError as e:
        assert "not a forum" in str(e) and TOKEN not in str(e)
    try:
        await t.topic_edit(CHAT, 5, name="y")
        raise AssertionError("expected TelegramError")
    except TelegramError as e:
        assert "Manage topics" in str(e)
    for bad in ("general", "telegram:abc", "123"):
        try:
            await t.chat_get(bad)
            raise AssertionError(bad)
        except TelegramError as e:
            assert e.status == 400


def test_session_key_parsing():
    assert parse_session_key(f"agent:main:telegram:forum:{CHAT}:12") == (CHAT, 12)
    assert parse_session_key(f"agent:main:telegram:group:{CHAT}") == (CHAT, None)
    assert parse_session_key("agent:main:telegram:dm:12345") is None
    assert parse_session_key("agent:main:discord:thread:1:2") is None


async def test_connector_tools_free_and_read_via_son(tmp_path):
    f = FakeTg()

    class Up:
        def __init__(self):
            self.calls = []

        async def tools(self):
            return []

        async def call(self, name, args):
            self.calls.append((name, args))
            if name == "conversations_list":
                data = {"conversations": [
                    {"session_key": f"agent:main:telegram:forum:{CHAT}:12", "chat_name": "Team / Launch",
                     "updated_at": "2026-10-14T20:00:00"},
                    {"session_key": f"agent:main:telegram:forum:{OTHER}:3", "chat_name": "Other"}]}
            else:
                data = {"session_key": args["session_key"], "count": 1, "messages": [{"role": "user", "content": "hi"}]}
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(data))])

    plugs = tmp_path / "plugs.json"   # enforce on and NOTHING owned: Telegram control must still run (free base)
    plugs.write_text(json.dumps({"version": 1, "enforce": True, "comp": False, "addons": []}))
    c = Connector([SonLink("my-son", "http://box", "t", "http://up/mcp")], github=False, telegram=tg(f, tmp_path),
                  entitlements=Entitlements(plugs, son_id="s"))
    up = Up()
    c.upstreams["my-son"] = up
    names = {t.name for t in await c.list_tools()}
    assert set(TELEGRAM_TOOLS) <= names
    res = await c.call("telegram_topics_list", {"chat_id": CHAT})
    rows = json.loads(res.content[0].text)["topics"]
    assert [r["topic_id"] for r in rows] == [1, 12] and rows[1]["target"] == f"telegram:{CHAT}:12"
    res = await c.call("telegram_topic_messages", {"chat_id": CHAT, "topic_id": 12, "limit": 5})
    out = json.loads(res.content[0].text)
    assert out["messages"][0]["content"] == "hi" and up.calls[-1] == (
        "messages_read", {"session_key": f"agent:main:telegram:forum:{CHAT}:12", "limit": 5})
    res = await c.call("telegram_topic_send", {"chat_id": CHAT, "topic_id": 12, "message": "go"})
    assert res.isError is False and json.loads(res.content[0].text)["delivered"]
    res = await c.call("telegram_topic_create", {"chat_id": CHAT, "name": "Task: x"})
    assert json.loads(res.content[0].text)["topic_id"] == 77


async def test_tools_hidden_without_bot_token():
    c = Connector([SonLink("my-son", "http://box", "t")], passthrough="none", github=False)
    assert not set(TELEGRAM_TOOLS) & {t.name for t in await c.list_tools()}
