"""Telegram control: drive a Telegram group WITH TOPICS (forum supergroup) from Claude/ChatGPT. Free for everyone.

The Discord counterpart is discord_relay.py + the discord_thread_* tools. Telegram's Bot API is narrower, so:
  * Posting into a topic: ``messages_send`` with ``telegram:<chat_id>:<topic_id>`` (upstream Hermes, the Son's bot).
    Topic id 1 = the General topic (the Bot API takes no thread id for it; Hermes maps that already).
  * Reading a topic: the Bot API has no history call. Bots only see what arrives, and the Son's gateway already keeps
    every topic it is in as one session, so ``telegram_topic_messages`` reads that session (upstream messages_read).
  * Listing topics: the Bot API has no "list topics" call either. ``telegram_topics_list`` merges the topics the Son
    has a session in with the topics created through this connector (kept in a small registry file).
  * Managing topics: createForumTopic / editForumTopic / closeForumTopic / reopenForumTopic with the Son's own bot
    token (``telegram.bot_token_env``, the bot must be an admin with "Manage topics").
The bot token stays in memory, is never logged and never returned.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import httpx

API = "https://api.telegram.org"
CHAT_RX = re.compile(r"^-?\d{5,20}$")
TARGET_RX = re.compile(r"^telegram:(?P<chat>-?\d{5,20})(?::(?P<topic>\d{1,12}))?$")
ICON_COLORS = (7322096, 16766590, 13338331, 9367192, 16749490, 16478047)  # the six colours Telegram accepts
GENERAL = 1
MAX_LEN = 4096


class TelegramError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def chat_id(v: Any) -> str:
    s = str(v or "").strip()
    if s.startswith("telegram:"):
        m = TARGET_RX.match(s)
        if not m:
            raise TelegramError(400, f"bad telegram target {s!r}: use telegram:<chat_id>[:<topic_id>]")
        s = m["chat"]
    if not CHAT_RX.match(s):
        raise TelegramError(400, f"chat_id must be the numeric group id (negative, starts with -100), got {s!r}")
    return s


def topic_id(v: Any) -> int:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise TelegramError(400, f"topic_id must be a number, got {v!r}") from None
    if n < 1:
        raise TelegramError(400, "topic_id must be >= 1 (1 = General)")
    return n


def target(chat: str, topic: Optional[int]) -> str:
    return f"telegram:{chat}:{topic}" if topic else f"telegram:{chat}"


def parse_session_key(key: str) -> Optional[tuple[str, Optional[int]]]:
    """(chat_id, topic_id) from a gateway session key ``...:telegram:<chat_type>:<chat_id>[:<thread_id>][:<user>]``.
    Group/forum keys only (DMs are not topics)."""
    parts = str(key or "").split(":")
    if "telegram" not in parts:
        return None
    i = parts.index("telegram")
    rest = parts[i + 1:]
    if len(rest) < 2 or rest[0] in ("dm", "private"):
        return None
    chat = rest[1]
    if not CHAT_RX.match(chat):
        return None
    topic = int(rest[2]) if len(rest) > 2 and rest[2].isdigit() and len(rest[2]) <= 12 else None
    return chat, topic


# Upstream hook: async (tool name, args) -> parsed JSON (dict) from the Son's own hermes mcp serve.
UpstreamCall = Callable[[str, dict], Awaitable[Any]]


@dataclass
class TelegramControl:
    bot_token: str
    registry: Optional[pathlib.Path] = None
    http: Optional[httpx.AsyncClient] = None
    _me: Optional[dict] = field(default=None, repr=False)

    def _client(self) -> httpx.AsyncClient:
        if self.http is None:
            self.http = httpx.AsyncClient(timeout=30, headers={"User-Agent": "aiteam-connector/1"})
        return self.http

    async def _api(self, method: str, **params: Any) -> Any:
        body = {k: v for k, v in params.items() if v is not None}
        try:
            r = await self._client().post(f"{API}/bot{self.bot_token}/{method}", json=body)
        except httpx.HTTPError as e:  # never let the URL (it holds the token) reach the caller
            raise TelegramError(502, f"{method}: Telegram unreachable ({type(e).__name__})") from None
        try:
            d = r.json()
        except ValueError:
            raise TelegramError(r.status_code, f"{method}: HTTP {r.status_code}") from None
        if not d.get("ok"):
            raise TelegramError(int(d.get("error_code") or r.status_code or 400),
                                f"{method}: {d.get('description') or 'failed'}{_hint(d.get('description'))}")
        return d.get("result")

    # ------------------------------------------------------------------ registry (topics made via the connector)
    def _reg_path(self) -> pathlib.Path:
        if self.registry:
            return self.registry
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        return pathlib.Path(home) / "mcp" / "telegram_topics.json"

    def _reg_load(self) -> dict:
        try:
            d = json.loads(self._reg_path().read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _reg_put(self, chat: str, topic: int, **fields: Any) -> None:
        d = self._reg_load()
        row = d.setdefault(chat, {}).setdefault(str(topic), {})
        row.update({k: v for k, v in fields.items() if v is not None})
        p = self._reg_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=1, sort_keys=True))
            os.replace(tmp, p)
        except OSError:
            pass  # the registry is a convenience; the Telegram change already happened

    # ------------------------------------------------------------------ tools
    async def chat_get(self, chat: Any) -> dict[str, Any]:
        c = chat_id(chat)
        info = await self._api("getChat", chat_id=c)
        if self._me is None:
            self._me = await self._api("getMe")
        me = await self._api("getChatMember", chat_id=c, user_id=self._me["id"])
        out = {"chat_id": c, "title": info.get("title"), "type": info.get("type"),
               "is_forum": bool(info.get("is_forum")), "username": info.get("username"),
               "bot_status": me.get("status"),
               "bot_can_manage_topics": bool(me.get("status") == "creator" or me.get("can_manage_topics")),
               "bot_can_pin": bool(me.get("status") == "creator" or me.get("can_pin_messages")),
               "target": target(c, None)}
        if not out["is_forum"]:
            out["note"] = "Topics are off in this group. Turn them on in the group settings (Topics) first."
        elif not out["bot_can_manage_topics"]:
            out["note"] = "Make the bot an admin with 'Manage topics' to create, rename or close topics."
        return out

    async def topics_list(self, chat: Any, upstream: Optional[UpstreamCall] = None) -> dict[str, Any]:
        c = chat_id(chat)
        rows: dict[int, dict[str, Any]] = {}
        for tid, r in (self._reg_load().get(c) or {}).items():
            if str(tid).isdigit():
                rows[int(tid)] = {"topic_id": int(tid), "name": r.get("name"), "closed": bool(r.get("closed")),
                                  "source": "connector"}
        if upstream is not None:
            try:
                conv = await upstream("conversations_list", {"platform": "telegram", "limit": 200})
            except Exception:  # noqa: BLE001 - the Son being down must not hide the registry rows
                conv = None
            for cv in (conv or {}).get("conversations", []) if isinstance(conv, dict) else []:
                parsed = parse_session_key(cv.get("session_key", ""))
                if not parsed or parsed[0] != c:
                    continue
                tid = parsed[1] or GENERAL
                row = rows.setdefault(tid, {"topic_id": tid, "name": None, "closed": False, "source": "son"})
                row["session_key"] = row.get("session_key") or cv.get("session_key")
                row["name"] = row.get("name") or cv.get("chat_name") or cv.get("display_name")
                row["updated_at"] = cv.get("updated_at")
                if row["source"] == "connector":
                    row["source"] = "connector+son"
        rows.setdefault(GENERAL, {"topic_id": GENERAL, "name": "General", "closed": False, "source": "telegram"})
        out = []
        for tid in sorted(rows):
            r = rows[tid]
            r["target"] = target(c, tid)
            out.append(r)
        return {"chat_id": c, "topics": out,
                "note": "Telegram has no 'list topics' call for bots: this lists the topics the Son is in plus the "
                        "ones created through this connector."}

    async def topic_create(self, chat: Any, name: str, icon_color: Optional[int] = None,
                           message: Optional[str] = None) -> dict[str, Any]:
        c = chat_id(chat)
        name = (name or "").strip()
        if not 1 <= len(name) <= 128:
            raise TelegramError(400, "name must be 1-128 characters")
        if icon_color is not None and int(icon_color) not in ICON_COLORS:
            raise TelegramError(400, f"icon_color must be one of {list(ICON_COLORS)}")
        t = await self._api("createForumTopic", chat_id=c, name=name, icon_color=icon_color)
        tid = int(t["message_thread_id"])
        self._reg_put(c, tid, name=t.get("name", name), closed=False)
        out: dict[str, Any] = {"chat_id": c, "topic_id": tid, "name": t.get("name", name), "target": target(c, tid)}
        if message:
            out["first_message"] = await self.send(c, tid, message)
        return out

    async def topic_edit(self, chat: Any, topic: Any, name: Optional[str] = None,
                         closed: Optional[bool] = None) -> dict[str, Any]:
        c, tid = chat_id(chat), topic_id(topic)
        if name is None and closed is None:
            raise TelegramError(400, "pass name and/or closed")
        changed = []
        if name is not None:
            name = name.strip()
            if not 1 <= len(name) <= 128:
                raise TelegramError(400, "name must be 1-128 characters")
            if tid == GENERAL:
                await self._api("editGeneralForumTopic", chat_id=c, name=name)
            else:
                await self._api("editForumTopic", chat_id=c, message_thread_id=tid, name=name)
            changed.append("name")
        if closed is not None:
            if tid == GENERAL:
                await self._api("closeGeneralForumTopic" if closed else "reopenGeneralForumTopic", chat_id=c)
            else:
                await self._api("closeForumTopic" if closed else "reopenForumTopic", chat_id=c, message_thread_id=tid)
            changed.append("closed")
        self._reg_put(c, tid, name=name, closed=closed)
        return {"chat_id": c, "topic_id": tid, "changed": changed, "name": name, "closed": closed,
                "target": target(c, tid)}

    async def send(self, chat: Any, topic: Optional[Any], message: str,
                   reply_to_message_id: Optional[int] = None) -> dict[str, Any]:
        """Post into a topic with the Son's bot; delivered = Telegram returned the message id (it is stored)."""
        c = chat_id(chat)
        tid = topic_id(topic) if topic not in (None, "") else None
        sent = []
        for i, part in enumerate(_chunks(message or "")):
            m = await self._api("sendMessage", chat_id=c, text=part,
                                message_thread_id=tid if tid and tid != GENERAL else None,
                                reply_parameters={"message_id": int(reply_to_message_id),
                                                  "allow_sending_without_reply": True}
                                if reply_to_message_id and i == 0 else None)
            sent.append(m.get("message_id"))
        return {"delivered": bool(sent) and all(sent), "message_ids": sent, "target": target(c, tid)}

    async def topic_messages(self, chat: Any, topic: Any, limit: int, upstream: UpstreamCall) -> dict[str, Any]:
        c, tid = chat_id(chat), topic_id(topic)
        listing = await self.topics_list(c, upstream)
        row = next((r for r in listing["topics"] if r["topic_id"] == tid and r.get("session_key")), None)
        if row is None:
            return {"chat_id": c, "topic_id": tid, "messages": [], "target": target(c, tid),
                    "note": "The Son has no messages from this topic yet (bots only see messages sent after they "
                            "joined, and need privacy mode off or a mention to see group messages)."}
        out = await upstream("messages_read", {"session_key": row["session_key"], "limit": int(limit)})
        if isinstance(out, dict):
            out.update(chat_id=c, topic_id=tid, target=target(c, tid))
        return out


def _chunks(text: str) -> list[str]:
    text = text or ""
    if not text:
        raise TelegramError(400, "message is empty")
    out = []
    while text:
        if len(text) <= MAX_LEN:
            out.append(text)
            break
        cut = text.rfind("\n", 0, MAX_LEN)
        cut = cut if cut > MAX_LEN // 2 else MAX_LEN
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out


def _hint(desc: Any) -> str:
    d = str(desc or "").lower()
    if "not enough rights" in d or "rights to manage" in d:
        return " (make the bot an admin with 'Manage topics')"
    if "chat is not a forum" in d or "forum" in d and "not" in d:
        return " (turn Topics on in the group settings)"
    if "thread not found" in d:
        return " (that topic does not exist or was deleted)"
    return ""
