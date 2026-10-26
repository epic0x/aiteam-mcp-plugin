"""Discord relay: connector posts land in Discord as a distinct author and wake the Son they tag.

Why this exists (live test, 2026-10-12):
  * Upstream ``messages_send`` posts with the Son's OWN bot token. The Son's gateway drops its own messages
    (``message.author == client.user``), so a connector post that tags the Son never wakes it: the self-ignore wall.
  * ``mirrored: true`` from upstream only means "copied into the gateway's session transcript". It never proved
    the message rendered in Discord.
  * Upstream resolves targets from the box's own channel directory, so a channel the box has not seen 404s.

Fix: Discord targets go through a per-channel webhook instead.
  * A webhook message has its own author (``webhook_id`` set, ``author.bot`` true, author id != any Son), so a Son
    with ``discord.allow_bots: mentions`` (``DISCORD_ALLOW_BOTS=mentions``) and an inline ``<@son>`` token admits it
    like any other bot tag, and replies in the same thread. Proven live before this module was written.
  * Delivery is confirmed by reading the message back from Discord (``GET /webhooks/{id}/{token}/messages/{mid}``).
    Only then the result carries ``delivered: true``. ``mirrored`` is no longer reported for Discord targets.
  * Any channel the bot can see works (threads resolve to their parent channel for the webhook), so cross-box
    targets stop 404ing. Every id in the target is looked up, so ``discord:<thread_id>`` and a thread id put in the
    channel slot both post INSIDE the thread (before 2026-10-13 the latter listed webhooks on the thread: 404).
    An archived, unlocked thread is reopened first, so one thread carries a task for its whole life.

Reply to a message (``reply_to_message_id``, owner 2026-10-12): so a relayed answer threads under the exact message
that asked, and wakes the person/agent who ASKED (the replied-to author), not whoever that message happened to tag.
  * Discord silently drops ``message_reference`` on webhook posts (checked live 2026-10-12), so a real reply is
    posted with the bot token: ``POST /channels/{id}/messages`` + ``message_reference`` (fail_if_not_exists).
    ``allowed_mentions.replied_user`` pings the asker. Hermes bots only wake on an inline ``<@id>`` from another bot
    (reply pings do not count, by design), so when the asker is a bot its ``<@id>`` is put first in the text.
  * Self case: when the replied-to message was written by THIS box's bot, a bot-authored reply could never wake
    this box's Son (self-ignore). Then the webhook posts it with a jump link to the message instead
    (``threaded: "link"``). Every other case is a native reply (``threaded: "native"``).
  * Delivery is confirmed by reading the message back and checking its ``message_reference``.

The bot token (``discord.bot_token_env``) is used to find or create the webhook and to post native replies (Manage Webhooks on the parent
channel). Webhook tokens stay in memory, are never logged and never returned to the caller.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

API = "https://discord.com/api/v10"
WEBHOOK_NAME = "aiteam connector"
TARGET_RX = re.compile(r"^discord:(?P<chat>\d{15,22})(?::(?P<thread>\d{15,22}))?$")
THREAD_TYPES = {10, 11, 12}  # announcement / public / private thread
MAX_LEN = 2000


class RelayError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class DiscordRelay:
    bot_token: str
    username: str = "Claude connector"
    http: Optional[httpx.AsyncClient] = None
    _dms: dict[str, bool] = field(default_factory=dict)   # channel id -> is a DM / group DM (no webhooks)
    _hooks: dict[str, tuple[str, str]] = field(default_factory=dict)   # parent channel id -> (webhook id, token)
    _parents: dict[str, tuple[str, Optional[str]]] = field(default_factory=dict)  # channel -> (parent, thread)
    _me: Optional[str] = None
    _guilds: dict[str, str] = field(default_factory=dict)  # channel id -> guild id

    def _client(self) -> httpx.AsyncClient:
        if self.http is None:
            self.http = httpx.AsyncClient(timeout=30, headers={"User-Agent": "DiscordBot (aiteam-connector, 1)"})
        return self.http

    def _bot(self) -> dict:
        return {"Authorization": f"Bot {self.bot_token}"}

    @staticmethod
    def parse_target(target: str) -> Optional[tuple[str, Optional[str]]]:
        m = TARGET_RX.match((target or "").strip())
        return (m["chat"], m["thread"]) if m else None

    async def _channel(self, cid: str) -> Optional[dict]:
        """GET /channels/{id} with the bot token; None on 404 (not a channel, or a message id)."""
        r = await self._client().get(f"{API}/channels/{cid}", headers=self._bot())
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise RelayError(r.status_code, f"channel {cid}: {_err(r)}")
        return r.json()

    async def _resolve(self, chat: str, thread: Optional[str]) -> tuple[str, Optional[str]]:
        """(parent channel for the webhook, thread id or None).

        Webhooks live on the PARENT text/forum channel and post into a thread with ``?thread_id=``. A thread id in
        the parent slot (``discord:<thread>`` or ``discord:<thread>:<anything>``) used to be taken as the parent,
        so the relay listed webhooks on the thread itself and Discord answered ``404 Unknown Channel``. Every id is
        now looked up: the first one that is a thread wins and its real ``parent_id`` is used for the webhook.
        """
        key = f"{chat}:{thread or ''}"
        if key in self._parents:
            return self._parents[key]
        seen: dict[str, Optional[dict]] = {}
        for cid in [x for x in (thread, chat) if x]:
            ch = seen[cid] = await self._channel(cid)
            if ch and ch.get("type") in THREAD_TYPES and ch.get("parent_id"):
                out = (str(ch["parent_id"]), str(ch["id"]))
                await self._unarchive(ch)
                self._parents[key] = out
                return out
        base = seen.get(chat)
        if base is None:
            raise RelayError(404, f"channel {chat} not found or not visible to the bot")
        if thread:
            raise RelayError(404, f"thread {thread} not found in channel {chat} (pass discord:<channel_id>:<thread_id> "
                                  "or just discord:<thread_id>)")
        out = (chat, None)
        self._parents[key] = out
        return out

    async def _unarchive(self, ch: dict) -> None:
        """An archived (not locked) thread is reopened before posting, so one thread can carry a task for life."""
        md = ch.get("thread_metadata") or {}
        if md.get("archived") and not md.get("locked"):
            r = await self._client().patch(f"{API}/channels/{ch['id']}", headers=self._bot(), json={"archived": False})
            if r.status_code >= 400:
                raise RelayError(r.status_code, f"thread {ch['id']} is archived and could not be reopened "
                                                f"(needs Manage Threads or Send Messages in Threads): {_err(r)}")

    # ------------------------------------------------------------------ thread management (owner 2026-10-13)
    async def thread_get(self, thread_id: str) -> dict[str, Any]:
        ch = await self._channel(_id(thread_id, "thread_id"))
        if ch is None or ch.get("type") not in THREAD_TYPES:
            raise RelayError(404, f"{thread_id} is not a thread the bot can see")
        return _thread_view(ch)

    async def thread_get_members(self, thread_id: str, member_id: Optional[str] = None) -> dict[str, Any]:
        """thread_get + member_ids (+ is_member for member_id)."""
        row = await self.thread_get(thread_id)
        await self._attach_members([row], _uid(member_id) if member_id else None)
        return row

    async def thread_edit(self, thread_id: str, name: Optional[str] = None, archived: Optional[bool] = None,
                          locked: Optional[bool] = None, auto_archive_duration: Optional[int] = None,
                          slowmode_seconds: Optional[int] = None) -> dict[str, Any]:
        """Rename / archive / unarchive / lock / unlock a thread (PATCH /channels/{thread}). Needs Manage Threads
        (or being the thread's creator for name/archive)."""
        thread_id = _id(thread_id, "thread_id")
        await self.thread_get(thread_id)
        body: dict[str, Any] = {}
        if name is not None:
            name = str(name).strip()
            if not 1 <= len(name) <= 100:
                raise RelayError(400, "name must be 1-100 characters")
            body["name"] = name
        if archived is not None:
            body["archived"] = bool(archived)
        if locked is not None:
            body["locked"] = bool(locked)
        if auto_archive_duration is not None:
            if int(auto_archive_duration) not in (60, 1440, 4320, 10080):
                raise RelayError(400, "auto_archive_duration must be 60, 1440, 4320 or 10080 (minutes)")
            body["auto_archive_duration"] = int(auto_archive_duration)
        if slowmode_seconds is not None:
            body["rate_limit_per_user"] = max(0, min(21600, int(slowmode_seconds)))
        if not body:
            raise RelayError(400, "nothing to change: pass name, archived, locked, auto_archive_duration or slowmode_seconds")
        c = self._client()
        if "name" in body or "auto_archive_duration" in body or "rate_limit_per_user" in body:
            # An archived thread rejects edits other than unarchive: reopen first, re-archive after if asked.
            cur = await self.thread_get(thread_id)
            if cur["archived"] and body.get("archived") is not False:
                await c.patch(f"{API}/channels/{thread_id}", headers=self._bot(), json={"archived": False})
                body.setdefault("archived", True)
        r = await c.patch(f"{API}/channels/{thread_id}", headers=self._bot(), json=body)
        if r.status_code == 429:
            wait = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get("retry_after")
            raise RelayError(429, f"Discord rate limit on thread edits (renames: about 2 per 10 minutes per thread); "
                                  f"retry after {wait or '?'} s. Nothing was changed.")
        if r.status_code >= 400:
            raise RelayError(r.status_code, f"edit thread {thread_id} (needs Manage Threads): {_err(r)}")
        self._parents = {k: v for k, v in self._parents.items() if thread_id not in k}
        return {"success": True, "changed": sorted(body), **_thread_view(r.json())}

    async def thread_create(self, channel_id: str, name: str, message: Optional[str] = None,
                            from_message_id: Optional[str] = None, auto_archive_duration: int = 10080,
                            add_user_ids: Optional[list] = None) -> dict[str, Any]:
        """Open a thread in a channel: on an existing message (from_message_id) or as a new thread whose first post
        is ``message`` (sent through the webhook, so tagged Sons wake). ``add_user_ids``: people added as part of
        creating it (rule: when the owner asked for the thread during a discussion with him, he is
        added). Only call this when the owner asked for a thread; never open one unprompted."""
        uids = [_uid(u) for u in (add_user_ids or [])]
        cid = _id(channel_id, "channel_id")
        name = str(name or "").strip()
        if not 1 <= len(name) <= 100:
            raise RelayError(400, "name must be 1-100 characters")
        c = self._client()
        if from_message_id:
            r = await c.post(f"{API}/channels/{cid}/messages/{_id(from_message_id, 'from_message_id')}/threads",
                             headers=self._bot(), json={"name": name, "auto_archive_duration": auto_archive_duration})
        else:
            r = await c.post(f"{API}/channels/{cid}/threads", headers=self._bot(),
                             json={"name": name, "type": 11, "auto_archive_duration": auto_archive_duration})
        if r.status_code >= 400:
            raise RelayError(r.status_code, f"create thread in {cid} (needs Create Public Threads): {_err(r)}")
        th = r.json()
        out: dict[str, Any] = {"success": True, **_thread_view(th), "target": f"discord:{cid}:{th['id']}"}
        if message:
            out["first_message"] = await self.send(f"discord:{cid}:{th['id']}", message)
        if uids:
            added = []
            for u in dict.fromkeys(uids):
                try:
                    added.append(await self.thread_member(str(th["id"]), u, add=True))
                except RelayError as e:
                    added.append({"success": False, "user_id": u, "error": str(e), "status": e.status})
            out["members_added"] = added
        return out

    # ------------------------------------------------------------------ thread members (owner 2026-10-13)
    # Add/remove happen ONLY on an explicit ask from the owner (or, for add, when the owner asks for a thread to be
    # created in a discussion with him: thread_create add_user_ids). Nothing here runs on its own; never auto-remove.
    async def thread_members(self, thread_id: str, limit: int = 100) -> dict[str, Any]:
        """Who is in a thread (GET /channels/{thread}/thread-members). Needs the Server Members intent for
        user details; ids are returned either way."""
        th = await self.thread_get(thread_id)
        ids = (await self._member_ids(th["thread_id"]))[:max(1, min(100, int(limit)))]
        return {"thread_id": th["thread_id"], "name": th["name"], "count": len(ids), "user_ids": ids}

    async def _is_member(self, thread_id: str, user_id: str) -> bool:
        r = await self._client().get(f"{API}/channels/{thread_id}/thread-members/{user_id}", headers=self._bot())
        if r.status_code == 404:
            return False
        if r.status_code >= 400:
            raise RelayError(r.status_code, f"read thread member {user_id}: {_err(r)}")
        return True

    async def thread_member(self, thread_id: str, user_id: str, add: bool) -> dict[str, Any]:
        """Add (PUT) or remove (DELETE) one user in a thread, then read membership back to prove it.

        Discord rules: both need the thread unarchived, so an archived (unlocked) thread is reopened first; after a
        REMOVE it is re-archived so the thread is left as it was (after an ADD it stays open: it needs the person).
        Add needs Send Messages in Threads (and the user must be able to see the parent channel); remove needs
        Manage Threads (or being the creator of a private thread)."""
        tid, uid = _id(thread_id, "thread_id"), _uid(user_id)
        th = await self.thread_get(tid)
        if th["locked"] and th["archived"]:
            raise RelayError(409, f"thread {tid} is archived and locked; unlock it first (discord_thread_edit locked=false)")
        c = self._client()
        was_archived = th["archived"]
        if was_archived:
            r = await c.patch(f"{API}/channels/{tid}", headers=self._bot(), json={"archived": False})
            if r.status_code >= 400:
                raise RelayError(r.status_code, f"thread {tid} is archived and could not be reopened: {_err(r)}")
        before = await self._is_member(tid, uid)
        verb = "add" if add else "remove"
        if before != add:
            r = await (c.put if add else c.delete)(f"{API}/channels/{tid}/thread-members/{uid}", headers=self._bot())
            if r.status_code >= 400:
                need = "Send Messages in Threads; the user must also see the parent channel" if add else "Manage Threads"
                raise RelayError(r.status_code, f"{verb} {uid} in thread {tid} (needs {need}): {_err(r)}")
        after = await self._is_member(tid, uid)
        if was_archived and not add:
            await c.patch(f"{API}/channels/{tid}", headers=self._bot(), json={"archived": True})
        if after != add:
            raise RelayError(502, f"Discord accepted the {verb} but {uid} is {'not ' if not after else ''}a member "
                                  f"of thread {tid} when read back")
        return {"success": True, "action": verb, "user_id": uid, "thread_id": tid, "name": th["name"],
                "member": after, "changed": before != after, "verified": True,
                "reopened": was_archived and add, "url": th["url"]}

    async def threads_list(self, channel_id: str, include_archived: bool = False, limit: int = 50,
                           include_members: bool = False, member_id: Optional[str] = None) -> dict[str, Any]:
        """Active threads of a channel (from the guild's active list), plus recently archived public ones.
        include_members adds member_ids per thread; member_id adds is_member per thread + member_of (ids)."""
        cid = _id(channel_id, "channel_id")
        base = await self._channel(cid)
        if base is None:
            raise RelayError(404, f"channel {cid} not found or not visible to the bot")
        if base.get("type") in THREAD_TYPES:
            cid = str(base["parent_id"])
        c = self._client()
        r = await c.get(f"{API}/guilds/{base['guild_id']}/threads/active", headers=self._bot())
        if r.status_code >= 400:
            raise RelayError(r.status_code, f"list active threads: {_err(r)}")
        rows = [_thread_view(t) for t in r.json().get("threads", []) if str(t.get("parent_id")) == cid]
        if include_archived:
            a = await c.get(f"{API}/channels/{cid}/threads/archived/public", headers=self._bot(),
                            params={"limit": max(1, min(100, int(limit)))})
            if a.status_code >= 400:
                raise RelayError(a.status_code, f"list archived threads: {_err(a)}")
            rows += [_thread_view(t) for t in a.json().get("threads", [])]
        rows.sort(key=lambda t: int(t["thread_id"]), reverse=True)
        rows = rows[:limit]
        out: dict[str, Any] = {"channel_id": cid}
        if include_members or member_id:
            await self._attach_members(rows, _uid(member_id) if member_id else None)
            if member_id:
                out["member_id"] = _uid(member_id)
                out["member_of"] = [t["thread_id"] for t in rows if t.get("is_member")]
        return {**out, "count": len(rows), "threads": rows}

    async def _member_ids(self, thread_id: str) -> list[str]:
        r = await self._client().get(f"{API}/channels/{thread_id}/thread-members", headers=self._bot(),
                                     params={"limit": 100})
        if r.status_code >= 400:
            raise RelayError(r.status_code, f"list members of thread {thread_id}: {_err(r)}")
        return [str(m.get("user_id") or (m.get("member") or {}).get("user", {}).get("id")) for m in r.json()]

    async def _attach_members(self, rows: list[dict], member_id: Optional[str]) -> None:
        """Adds member_ids (+ is_member for member_id) to each thread row; 5 lookups at a time (rate limits)."""
        sem = asyncio.Semaphore(5)

        async def one(t: dict) -> None:
            async with sem:
                try:
                    t["member_ids"] = await self._member_ids(t["thread_id"])
                except RelayError as e:
                    t["member_ids"], t["members_error"] = None, str(e)
            if member_id:
                t["is_member"] = None if t["member_ids"] is None else member_id in t["member_ids"]
        await asyncio.gather(*(one(t) for t in rows))

    async def _is_dm(self, channel: str) -> bool:
        """DM / group DM channels (type 1/3) have no webhooks: Discord 404s on /webhooks (2026-10-23 fix)."""
        if channel not in self._dms:
            ch = await self._channel(channel)
            self._dms[channel] = bool(ch and ch.get("type") in (1, 3))
        return self._dms[channel]

    async def _bot_post(self, channel: str, message: str) -> dict[str, Any]:
        """Post as the bot itself (DMs), chunked, read back."""
        c = self._client()
        mentions = sorted(set(re.findall(r"<@!?(\d{15,22})>", message)))
        ids: list[str] = []
        for chunk in _chunks(message):
            p = await c.post(f"{API}/channels/{channel}/messages", headers=self._bot(),
                             json={"content": chunk, "allowed_mentions": {"parse": [], "users": mentions[:100]}})
            if p.status_code >= 400:
                raise RelayError(p.status_code, f"DM post: {_err(p)}")
            ids.append(str(p.json()["id"]))
        b = await c.get(f"{API}/channels/{channel}/messages/{ids[-1]}", headers=self._bot())
        if b.status_code >= 400:
            raise RelayError(502, f"posted but read-back failed: {_err(b)}")
        return {"success": True, "delivered": True, "via": "bot_dm", "platform": "discord", "channel_id": channel,
                "message_id": ids[-1], "message_ids": ids, "wakes": mentions,
                "note": "DM channel: posted as the bot (DMs have no webhooks), read back from Discord."}

    async def _webhook(self, parent: str) -> tuple[str, str]:
        if parent in self._hooks:
            return self._hooks[parent]
        c = self._client()
        r = await c.get(f"{API}/channels/{parent}/webhooks", headers=self._bot())
        if r.status_code >= 400:
            raise RelayError(r.status_code, f"list webhooks on {parent} (needs Manage Webhooks): {_err(r)}")
        hook = next((h for h in r.json() if h.get("name") == WEBHOOK_NAME and h.get("token")), None)
        if hook is None:
            r = await c.post(f"{API}/channels/{parent}/webhooks", headers=self._bot(), json={"name": WEBHOOK_NAME})
            if r.status_code >= 400:
                raise RelayError(r.status_code, f"create webhook on {parent} (needs Manage Webhooks): {_err(r)}")
            hook = r.json()
        self._hooks[parent] = (str(hook["id"]), str(hook["token"]))
        return self._hooks[parent]

    async def _my_id(self) -> str:
        if self._me is None:
            r = await self._client().get(f"{API}/users/@me", headers=self._bot())
            if r.status_code >= 400:
                raise RelayError(r.status_code, f"users/@me: {_err(r)}")
            self._me = str(r.json()["id"])
        return self._me

    async def _guild(self, channel: str) -> str:
        if channel not in self._guilds:
            r = await self._client().get(f"{API}/channels/{channel}", headers=self._bot())
            if r.status_code >= 400:
                raise RelayError(r.status_code, f"channel {channel}: {_err(r)}")
            self._guilds[channel] = str(r.json().get("guild_id") or "@me")
        return self._guilds[channel]

    async def send(self, target: str, message: str, username: Optional[str] = None,
                   reply_to: Optional[str] = None, wake_author: bool = True) -> dict[str, Any]:
        parsed = self.parse_target(target)
        if not parsed:
            raise RelayError(400, "target must be discord:<channel_id> or discord:<channel_id>:<thread_id>")
        if not (message or "").strip():
            raise RelayError(400, "message is required")
        if reply_to is not None and not re.fullmatch(r"\d{15,22}", str(reply_to)):
            raise RelayError(400, "reply_to_message_id must be a numeric Discord message id")
        parent, thread = await self._resolve(*parsed)
        if reply_to is not None:
            return await self._reply(parent, thread, message, str(reply_to), username, wake_author)
        if not thread and await self._is_dm(parent):
            return await self._bot_post(parent, message)
        wid, wtok = await self._webhook(parent)
        params = {"wait": "true", **({"thread_id": thread} if thread else {})}
        mentions = sorted(set(re.findall(r"<@!?(\d{15,22})>", message)))
        ids = []
        c = self._client()
        for chunk in _chunks(message):
            r = await c.post(f"{API}/webhooks/{wid}/{wtok}", params=params,
                             json={"content": chunk, "username": (username or self.username)[:80],
                                   "allowed_mentions": {"parse": [], "users": mentions[:100]}})
            if r.status_code == 404:          # webhook deleted under us: forget it, recreate once
                self._hooks.pop(parent, None)
                wid, wtok = await self._webhook(parent)
                r = await c.post(f"{API}/webhooks/{wid}/{wtok}", params=params,
                                 json={"content": chunk, "username": (username or self.username)[:80],
                                       "allowed_mentions": {"parse": [], "users": mentions[:100]}})
            if r.status_code >= 400:
                raise RelayError(r.status_code, f"post: {_err(r)}")
            ids.append(str(r.json()["id"]))
        # Proof of render: read the last message back from Discord itself.
        r = await c.get(f"{API}/webhooks/{wid}/{wtok}/messages/{ids[-1]}",
                        params={"thread_id": thread} if thread else None)
        if r.status_code >= 400:
            raise RelayError(502, f"posted but read-back failed: {_err(r)}")
        back = r.json()
        return {"success": True, "delivered": True, "via": "webhook", "platform": "discord",
                "channel_id": str(back.get("channel_id") or thread or parent), "message_id": ids[-1],
                "message_ids": ids, "wakes": mentions,
                "note": "Read back from Discord. Tagged Sons with discord.allow_bots=mentions will act on it."}


    async def _reply(self, parent: str, thread: Optional[str], message: str, ref_id: str,
                     username: Optional[str], wake_author: bool) -> dict[str, Any]:
        c = self._client()
        ch = thread or parent
        r = await c.get(f"{API}/channels/{ch}/messages/{ref_id}", headers=self._bot())
        if r.status_code == 404:
            raise RelayError(404, f"message {ref_id} is not in channel {ch} (reply in the same channel/thread as the message)")
        if r.status_code >= 400:
            raise RelayError(r.status_code, f"read message {ref_id}: {_err(r)}")
        ref = r.json()
        author = ref.get("author") or {}
        asker, asker_is_bot = str(author.get("id") or ""), bool(author.get("bot"))
        me = await self._my_id()
        text_ids = set(re.findall(r"<@!?(\d{15,22})>", message))
        if asker == me or ref.get("webhook_id"):
            # Self-ignore case (or a webhook author nobody can ping): webhook post + jump link.
            link = f"https://discord.com/channels/{await self._guild(ch)}/{ch}/{ref_id}"
            lead = f"<@{asker}> " if wake_author and asker == me and asker not in text_ids else ""
            out = await self.send(f"discord:{parent}:{thread}" if thread else f"discord:{parent}",
                                  f"↪ Reply to {link}\n{lead}{message}", username)
            out.update({"threaded": "link", "reply_to_message_id": ref_id, "replied_to_author": asker,
                        "note": "The asked message was written by this box's bot (or a webhook), so it was posted "
                                "via webhook with a jump link: a bot reply could not wake this box's own Son."})
            return out
        body = message
        if wake_author and asker_is_bot and asker not in text_ids:
            body = f"<@{asker}> {message}"    # Hermes bots wake only on an inline tag from another bot
        mentions = sorted(set(re.findall(r"<@!?(\d{15,22})>", body)))
        ids: list[str] = []
        for i, chunk in enumerate(_chunks(body)):
            payload: dict[str, Any] = {"content": chunk, "allowed_mentions": {
                "parse": [], "users": mentions[:100], "replied_user": bool(wake_author)}}
            if i == 0:
                payload["message_reference"] = {"message_id": ref_id, "channel_id": ch, "fail_if_not_exists": True}
            p = await c.post(f"{API}/channels/{ch}/messages", headers=self._bot(), json=payload)
            if p.status_code >= 400:
                raise RelayError(p.status_code, f"reply post: {_err(p)}")
            ids.append(str(p.json()["id"]))
        b = await c.get(f"{API}/channels/{ch}/messages/{ids[0]}", headers=self._bot())
        if b.status_code >= 400:
            raise RelayError(502, f"posted but read-back failed: {_err(b)}")
        got = (b.json().get("message_reference") or {}).get("message_id")
        if str(got) != ref_id:
            raise RelayError(502, f"posted {ids[0]} but Discord shows no reply link to {ref_id}")
        return {"success": True, "delivered": True, "via": "bot_reply", "platform": "discord", "channel_id": ch,
                "message_id": ids[-1], "message_ids": ids, "threaded": "native", "reply_to_message_id": ref_id,
                "replied_to_author": asker, "wakes": [m for m in mentions if m != me] + ([asker] if wake_author and asker not in mentions else []),
                "note": "Native Discord reply, read back. The asker is pinged; other tags in the text only wake if typed."}


def _id(v: Any, field: str) -> str:
    v = str(v or "").strip()
    if v.startswith("discord:"):
        v = v.split(":")[-1]
    if not re.fullmatch(r"\d{15,22}", v):
        raise RelayError(400, f"{field} must be a numeric Discord id")
    return v


def _uid(v: Any) -> str:
    """A Discord user id: plain digits or a mention token <@id> / <@!id>."""
    s = str(v or "").strip()
    m = re.fullmatch(r"<@!?(\d{15,22})>", s)
    s = m[1] if m else s
    if not re.fullmatch(r"\d{15,22}", s):
        raise RelayError(400, "user_id must be a numeric Discord user id (or <@id>)")
    return s


def _thread_view(t: dict) -> dict[str, Any]:
    md = t.get("thread_metadata") or {}
    parent = str(t.get("parent_id") or "")
    return {"thread_id": str(t.get("id")), "name": t.get("name"), "parent_id": parent,
            "archived": bool(md.get("archived")), "locked": bool(md.get("locked")),
            "auto_archive_duration": md.get("auto_archive_duration"),
            "message_count": t.get("message_count"), "target": f"discord:{parent}:{t.get('id')}",
            "url": f"https://discord.com/channels/{t.get('guild_id', '@me')}/{t.get('id')}"}


def _chunks(text: str) -> list[str]:
    out, rest = [], text
    while len(rest) > MAX_LEN:
        cut = rest.rfind("\n", 0, MAX_LEN)
        cut = cut if cut > MAX_LEN // 2 else MAX_LEN
        out.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return out + [rest]


def _err(r: httpx.Response) -> str:
    try:
        d = r.json()
        return f"{r.status_code} {d.get('message') or d}"[:200]
    except ValueError:
        return f"{r.status_code} {r.text[:200]}"
