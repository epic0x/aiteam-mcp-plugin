"""AI Teamspace Plugin MCP Connector: one MCP server for Claude and ChatGPT that drives your Sons.

Three things folded into one connector:
  1. Hermes is the agent runtime. The connector never runs a model itself; every turn is a real Hermes turn
     on the Son, with its memory, skills and tools.
  2. OpenClaw-style session tools (sessions_list / session_status / sessions_history / sessions_search /
     sessions_send), forwarded to the Son's own ``hermes mcp serve`` (the OpenClaw session-tools shapes), so the
     argument and output shapes stay identical across boxes.
  3. A Paperclip-style HTTP messaging bridge: ``son_send`` posts through the messaging box (own identity,
     REST + SSE) and streams the reply back. This is what lets the connector holder drive a lone Son
     directly, with no coordinator and no chat platform in the loop.

Tools exposed (titles + annotations for store review):
  usage_guide              read    who we are + how to drive the Son (usage_guide.py); also sent as the MCP server
                                   instructions
Prompt `who_we_are` + resource `aiteam://who-we-are` (intro.py): the Sons' introduction, served on connect.
  son_send                 write   message a Son and get its reply (streamed; progress notifications)
  son_list                 read    which Sons this connector can reach + health
  son_conversations        read    your conversations with a Son (via the box)
  son_conversation_history read    messages of one of your conversations
  sessions_list / session_status / sessions_history / sessions_search    read (upstream Hermes)
  sessions_send            write   (upstream Hermes; API/CLI/cron sessions only, chat sessions refused)
  ping                     read    is the Son's computer up (one line, safe to call any time)
  health                   read    live health of the Son's computer: disk, memory, swap, CPU, uptime, live sessions,
                                   scheduled jobs, last cleanup, status ok / warning / critical (host_health.py)
  cleanup                  write   run the fixed safe cleanup now (rebuildable files only; once per 10 minutes)
  son_model_status         read    model + provider + fallback order, globally and per session (model_control.py)
  son_model_set_all        write   set provider + model for ALL sessions of the Son
  son_model_set_session    write   set / clear provider + model for ONE session (sessionKey/sessionId)
  son_model_fallback_set   write   set the fallback order (optionally the primary too)
  github_*                 read+write  the Son's own `gh` CLI login, same permissions and same output as `gh` on the
                           Son (github_gh.py). ON by default; `"github": false` turns it off.

Config: JSON file (``--config``) or env ``AITEAM_CONNECTOR_CONFIG``:
  {"sons": [{"name": "my-son", "box_url": "http://127.0.0.1:8710", "box_token_env": "BOX_TOKEN_SON",
             "upstream_mcp_url": "http://127.0.0.1:8700/mcp", "upstream_token_env": null}],
   "passthrough": "all",                # "all" (default: full surface) | "session" (store listing) | "none"
   "discord": {"bot_token_env": "DISCORD_BOT_TOKEN"},  # optional: Discord messages_send via webhook (discord_relay.py)
   "telegram": {"bot_token_env": "TELEGRAM_BOT_TOKEN"},  # optional: Telegram topic control (telegram_control.py, free)
   "github": {"timeout_s": 30},        # optional: gh tool settings; GitHub is on unless "github": false
   "model_control": {"socket": "..."}} # optional: model tools socket (default <HERMES_HOME>/mcp/model-control.sock);
                                       # false turns the model tools off
Tokens live in env vars named by the config, never in the file.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import mcp.types as types
from mcp import ClientSession
from mcp.server.lowlevel import Server

from .entitlements import CALLER, Entitlements
from .model_control import MODEL_TOOLS, ModelControlClient, call_model_tool, model_tools
from .github_gh import GITHUB_TOOLS, LEGACY_TOOLS, GhError, GhRunner, github_tools
from .intro import INTRO, INTRO_URI, intro_prompt, intro_prompt_result, intro_resource
from .usage_guide import GUIDE, INSTRUCTIONS, usage_guide_tool

log = logging.getLogger("aiteam.connector")
SESSION_TOOLS = ("sessions_list", "session_status", "sessions_history", "sessions_search", "sessions_send")
READ_ONLY = {"sessions_list", "session_status", "sessions_history", "sessions_search",
             # upstream `hermes mcp serve` read tools (the standard 15-tool set)
             "attachments_fetch", "channels_list", "conversation_get", "conversations_list", "events_poll",
             "events_wait", "messages_read", "permissions_list_open"}
# Writes: messages_send (posts to a chat platform), permissions_respond (answers an exec approval), sessions_send.
VERSION = "0.1.0"


@dataclass
class SonLink:
    name: str
    box_url: str
    box_token: str
    upstream_mcp_url: Optional[str] = None
    upstream_token: Optional[str] = None


def load_discord(raw: dict):
    """Optional ``"discord": {"bot_token_env": "...", "username": "..."}``: route Discord messages_send through a
    webhook (distinct author, wakes tagged Sons, read-back proof). See discord_relay.py."""
    d = raw.get("discord") or {}
    if not d.get("bot_token_env"):
        return None
    tok = os.environ.get(d["bot_token_env"], "")
    if not tok:
        raise SystemExit(f"discord: env {d['bot_token_env']} is empty")
    from .discord_relay import DiscordRelay
    return DiscordRelay(tok, username=str(d.get("username") or "Claude connector"))


def load_telegram(raw: dict):
    """Optional ``"telegram": {"bot_token_env": "TELEGRAM_BOT_TOKEN"}``: Telegram topic control (telegram_control.py).
    Free for everyone. Missing env = the tools stay off (a Son without Telegram has nothing to control)."""
    t = raw.get("telegram") or {}
    if not t.get("bot_token_env"):
        return None
    tok = os.environ.get(t["bot_token_env"], "")
    if not tok:
        log.warning("telegram: env %s is empty, topic tools off", t["bot_token_env"])
        return None
    from .telegram_control import TelegramControl
    return TelegramControl(tok)


def load_config(path: Optional[str]) -> tuple[list[SonLink], str]:
    raw = json.loads(open(path).read()) if path else json.loads(os.environ["AITEAM_CONNECTOR_CONFIG"])
    sons = []
    for s in raw["sons"]:
        tok = os.environ.get(s.get("box_token_env") or "", "")
        if not tok:
            raise SystemExit(f"son {s['name']}: env {s.get('box_token_env')} is empty")
        up_tok = os.environ.get(s["upstream_token_env"], "") if s.get("upstream_token_env") else None
        sons.append(SonLink(s["name"], s["box_url"].rstrip("/"), tok, s.get("upstream_mcp_url"), up_tok))
    mode = raw.get("passthrough", "all")
    if mode not in ("session", "none", "all"):
        raise SystemExit("passthrough must be session | none | all")
    return sons, mode


# --------------------------------------------------------------------------------------------- upstream
class Upstream:
    """Short-lived MCP client sessions to a Son's own ``hermes mcp serve`` (streamable HTTP)."""

    def __init__(self, url: str, token: Optional[str]):
        self.url, self.token = url, token
        self._tools: Optional[list[types.Tool]] = None

    @contextlib.asynccontextmanager
    async def session(self):
        from mcp.client.streamable_http import streamablehttp_client
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        async with streamablehttp_client(self.url, headers=headers, timeout=30, sse_read_timeout=900) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                yield s

    async def tools(self) -> list[types.Tool]:
        if self._tools is None:
            async with self.session() as s:
                self._tools = (await s.list_tools()).tools
        return self._tools

    async def call(self, name: str, args: dict) -> types.CallToolResult:
        async with self.session() as s:
            return await s.call_tool(name, args)


# --------------------------------------------------------------------------------------------- tools
HEALTH_TOOLS = ("ping", "health", "cleanup")


def health_tools(son_names: list[str]) -> list[types.Tool]:
    """The Son's computer: alive / health / safe cleanup. Free for everyone (part of the base)."""
    son = _son_prop(son_names)
    return [
        types.Tool(
            name="ping", title="Is my Son's computer up?",
            description="Check that your Son's computer is up and answering. Returns one short line, such as "
                        "\"Up for 19 days.\" Fast and safe to call at any time.",
            inputSchema={"type": "object", "properties": {"son": son}, "additionalProperties": False},
            annotations=types.ToolAnnotations(title="Is my Son's computer up?", readOnlyHint=True,
                                              idempotentHint=True, openWorldHint=False)),
        types.Tool(
            name="health", title="Check my Son's computer",
            description="Live health of your Son's computer: disk, memory, swap, CPU, uptime, live sessions, "
                        "scheduled jobs (last run and result) and the last cleanup, with one overall status: ok, "
                        "warning or critical. The first line is a short sentence you can read out as is, for example "
                        "\"Healthy. Disk 66% used, memory 48% free.\" Set history=true for 24 hours of 5-minute "
                        "samples.",
            inputSchema={"type": "object", "additionalProperties": False,
                         "properties": {"son": son, "history": {"type": "boolean", "default": False,
                                                                "description": "Add the last 24 hours of samples."}}},
            annotations=types.ToolAnnotations(title="Check my Son's computer", readOnlyHint=True,
                                              idempotentHint=True, openWorldHint=False)),
        types.Tool(
            name="cleanup", title="Free up space on my Son's computer",
            description="Run the safe cleanup on your Son's computer now. It removes only files that can be rebuilt "
                        "(build caches, package caches, old scratch media and logs), never memory, chats, skills, "
                        "settings or your work. It runs at most once every 10 minutes. Use it when health says the "
                        "disk is filling up, then call health again.",
            inputSchema={"type": "object", "properties": {"son": son}, "additionalProperties": False},
            annotations=types.ToolAnnotations(title="Free up space on my Son's computer", readOnlyHint=False,
                                              destructiveHint=False, idempotentHint=False, openWorldHint=False)),
    ]


def health_text(d: dict) -> str:
    """Voice-friendly first line, then the data."""
    return f"{d.get('summary') or d.get('message') or ''}\n\n{json.dumps(d, ensure_ascii=False, indent=1)}".lstrip()


def _son_prop(names: list[str]) -> dict:
    return {"type": "string", "enum": names, "description": f"Which Son. Default: {names[0]}."}


def native_tools(son_names: list[str]) -> list[types.Tool]:
    son = _son_prop(son_names)
    conv = {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,64}$",
            "description": "Conversation name. Same name = same thread with the Son (it remembers). Default: default."}
    return [
        types.Tool(
            name="son_send", title="Message your Son",
            description="Send a message to your Son and return its reply. The Son runs the task with its own "
                        "tools, memory and skills, so a reply can take a few minutes. Use a conversation name to "
                        "keep a thread going.",
            inputSchema={"type": "object", "required": ["message"], "additionalProperties": False,
                         "properties": {"message": {"type": "string", "minLength": 1, "maxLength": 32000},
                                        "conversation": conv, "son": son}},
            annotations=types.ToolAnnotations(title="Message your Son", readOnlyHint=False,
                                              destructiveHint=False, idempotentHint=False, openWorldHint=True)),
        types.Tool(
            name="son_list", title="List your Sons",
            description="List the Sons this connector can reach and whether each one is online.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            annotations=types.ToolAnnotations(title="List your Sons", readOnlyHint=True, openWorldHint=False)),
        types.Tool(
            name="son_conversations", title="Your conversations with a Son",
            description="List the conversations you started with a Son through this connector.",
            inputSchema={"type": "object", "properties": {"son": son}, "additionalProperties": False},
            annotations=types.ToolAnnotations(title="Your conversations", readOnlyHint=True, openWorldHint=False)),
        types.Tool(
            name="son_conversation_history", title="Read a conversation",
            description="Read the latest messages of one of your conversations with a Son.",
            inputSchema={"type": "object", "additionalProperties": False,
                         "properties": {"conversation": conv, "son": son,
                                        "limit": {"type": "integer", "minimum": 1, "maximum": 100}}},
            annotations=types.ToolAnnotations(title="Read a conversation", readOnlyHint=True, openWorldHint=False)),
    ]


DISCORD_THREAD_TOOLS = ("discord_thread_get", "discord_thread_edit", "discord_thread_create", "discord_threads_list",
                        "discord_thread_member_add", "discord_thread_member_remove", "discord_thread_members")
# Rule, also in the usage guide and both skills: add/remove members ONLY when the owner asks;
# when the owner asks for a thread during a discussion with him, add him as part of creating it; never open a
# thread unprompted; never remove the owner automatically.
MEMBER_RULE = ("Only on the owner's explicit ask: never add or remove anyone on your own judgment, and never remove "
               "the owner automatically (not even when a task looks done).")


def discord_thread_tools() -> list[types.Tool]:
    """Thread management through the connector's Discord bot (owner 2026-10-13: one thread per task, for its life)."""
    sid = {"type": "string", "pattern": "^(discord:[0-9:]+|[0-9]{15,22})$"}
    uid = {"type": "string", "pattern": "^(<@!?[0-9]{15,22}>|[0-9]{15,22})$"}
    member = {"type": "object", "required": ["thread_id", "user_id"], "additionalProperties": False,
              "properties": {"thread_id": sid, "user_id": {**uid, "description": "Discord user id (or <@id>)."}}}
    return [
        types.Tool(
            name="discord_thread_member_add", title="Add a user to a Discord thread",
            description="Add one user (by Discord user id) to a thread so it shows in their thread list. "
                        + MEMBER_RULE + " Reopens an archived thread first. Membership is read back: the result "
                        "says member=true and verified=true, changed=false if they were already in.",
            inputSchema=member,
            annotations=types.ToolAnnotations(title="Add to thread", readOnlyHint=False, destructiveHint=False,
                                              idempotentHint=True, openWorldHint=True)),
        types.Tool(
            name="discord_thread_member_remove", title="Remove a user from a Discord thread",
            description="Remove one user (by Discord user id) from a thread. " + MEMBER_RULE + " An archived "
                        "thread is reopened for the change and re-archived after. Membership is read back "
                        "(member=false, verified=true).",
            inputSchema=member,
            annotations=types.ToolAnnotations(title="Remove from thread", readOnlyHint=False, destructiveHint=True,
                                              idempotentHint=True, openWorldHint=True)),
        types.Tool(
            name="discord_thread_members", title="List a Discord thread's members",
            description="User ids of the people and bots in a thread.",
            inputSchema={"type": "object", "required": ["thread_id"], "additionalProperties": False,
                         "properties": {"thread_id": sid, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}},
            annotations=types.ToolAnnotations(title="Thread members", readOnlyHint=True, openWorldHint=True)),
        types.Tool(
            name="discord_threads_list", title="List Discord threads",
            description="List the threads of a Discord channel (active, plus recently archived with include_archived). "
                        "Each row has thread_id, name, archived/locked and a ready messages_send target "
                        "discord:<channel_id>:<thread_id>. Use it to find a task's thread before posting. "
                        "Membership: member_id=<user id> adds is_member to each row and member_of (the thread ids "
                        "that user is in); include_members=true adds member_ids to each row.",
            inputSchema={"type": "object", "required": ["channel_id"], "additionalProperties": False, "properties": {
                "channel_id": {**sid, "description": "Parent channel id (a thread id also works: its parent is used)."},
                "include_archived": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                "include_members": {"type": "boolean", "default": False,
                                    "description": "Add member_ids (user ids in the thread) to each row."},
                "member_id": {**uid, "description": "Check this user's membership in every listed thread."}}},
            annotations=types.ToolAnnotations(title="List Discord threads", readOnlyHint=True, openWorldHint=True)),
        types.Tool(
            name="discord_thread_get", title="Read a Discord thread's settings",
            description="Name, parent channel, archived/locked state and messages_send target of one thread. "
                        "include_members=true adds member_ids; member_id=<user id> adds is_member for that user.",
            inputSchema={"type": "object", "required": ["thread_id"], "additionalProperties": False,
                         "properties": {"thread_id": sid, "include_members": {"type": "boolean", "default": False},
                                        "member_id": uid}},
            annotations=types.ToolAnnotations(title="Read a thread", readOnlyHint=True, openWorldHint=True)),
        types.Tool(
            name="discord_thread_edit", title="Rename / archive / lock a Discord thread",
            description="Edit a thread: rename it (name, 1-100 chars, e.g. prefix 'Closed: ' when a task is done), "
                        "archive or unarchive it, lock or unlock it, change auto-archive (60/1440/4320/10080 min) or "
                        "slowmode. Pass only what changes. Renaming an archived thread reopens it, renames, and "
                        "re-archives it.",
            inputSchema={"type": "object", "required": ["thread_id"], "additionalProperties": False, "properties": {
                "thread_id": sid,
                "name": {"type": "string", "minLength": 1, "maxLength": 100},
                "archived": {"type": "boolean"}, "locked": {"type": "boolean"},
                "auto_archive_duration": {"type": "integer", "enum": [60, 1440, 4320, 10080]},
                "slowmode_seconds": {"type": "integer", "minimum": 0, "maximum": 21600}}},
            annotations=types.ToolAnnotations(title="Edit a thread", readOnlyHint=False, destructiveHint=False,
                                              idempotentHint=True, openWorldHint=True)),
        types.Tool(
            name="discord_thread_create", title="Open a Discord thread",
            description="Open a public thread in a channel, titled name. With from_message_id the thread hangs off "
                        "that message; otherwise it is a standalone thread. Optional message is posted as the first "
                        "message through the webhook (tagged Sons wake on it). Returns the target to use with "
                        "messages_send for every follow-up. Only when the owner asked for a thread: never open one on "
                        "your own initiative. If the owner asked for it while discussing with you, pass his user id "
                        "in add_user_ids so he is in it.",
            inputSchema={"type": "object", "required": ["channel_id", "name"], "additionalProperties": False,
                         "properties": {
                             "channel_id": sid, "name": {"type": "string", "minLength": 1, "maxLength": 100},
                             "message": {"type": "string", "maxLength": 32000},
                             "from_message_id": {"type": "string", "pattern": "^[0-9]{15,22}$"},
                             "add_user_ids": {"type": "array", "maxItems": 10, "items": uid, "description":
                                 "Discord user ids to add as part of creating it. Pass the owner's id when the owner "
                                 "asked for this thread while discussing with you."},
                             "auto_archive_duration": {"type": "integer", "enum": [60, 1440, 4320, 10080],
                                                       "default": 10080}}},
            annotations=types.ToolAnnotations(title="Open a thread", readOnlyHint=False, destructiveHint=False,
                                              idempotentHint=False, openWorldHint=True)),
    ]


TELEGRAM_TOOLS = ("telegram_chat_get", "telegram_topics_list", "telegram_topic_create", "telegram_topic_edit",
                  "telegram_topic_send", "telegram_topic_messages")
TOPIC_RULE = "Only when the owner asked: never open, rename or close a topic on your own initiative."


def telegram_tools() -> list[types.Tool]:
    """Telegram group-with-topics control (owner 2026-10-14: free, mirrors the Discord thread tools)."""
    chat = {"type": "string", "pattern": "^(telegram:-?[0-9:]+|-?[0-9]{5,20})$",
            "description": "Telegram group id (negative number, starts with -100) or a telegram:<chat_id>[:<topic_id>] target."}
    topic = {"type": "integer", "minimum": 1, "description": "Topic id (message_thread_id). 1 = General."}
    ro = lambda t: types.ToolAnnotations(title=t, readOnlyHint=True, openWorldHint=True)  # noqa: E731
    rw = lambda t, idem=False: types.ToolAnnotations(title=t, readOnlyHint=False, destructiveHint=False,  # noqa: E731
                                                     idempotentHint=idem, openWorldHint=True)
    return [
        types.Tool(
            name="telegram_chat_get", title="Read a Telegram group",
            description="Title, type, whether Topics are on, and what the Son's bot may do there (manage topics, "
                        "pin). Start here when a group's topic calls fail.",
            inputSchema={"type": "object", "required": ["chat_id"], "additionalProperties": False,
                         "properties": {"chat_id": chat}},
            annotations=ro("Read a group")),
        types.Tool(
            name="telegram_topics_list", title="List Telegram topics",
            description="Topics of a Telegram group with topics: the ones the Son is in plus the ones created "
                        "through this connector (Telegram gives bots no full list). Each row has topic_id, name, "
                        "closed and a ready messages_send target telegram:<chat_id>:<topic_id>.",
            inputSchema={"type": "object", "required": ["chat_id"], "additionalProperties": False,
                         "properties": {"chat_id": chat}},
            annotations=ro("List topics")),
        types.Tool(
            name="telegram_topic_messages", title="Read a Telegram topic",
            description="Latest messages of one topic, as the Son saw them (bots only see messages sent after they "
                        "joined the group).",
            inputSchema={"type": "object", "required": ["chat_id", "topic_id"], "additionalProperties": False,
                         "properties": {"chat_id": chat, "topic_id": topic,
                                        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}}},
            annotations=ro("Read a topic")),
        types.Tool(
            name="telegram_topic_send", title="Post in a Telegram topic",
            description="Post a message inside a topic with the Son's bot; delivered=true once Telegram returns the "
                        "message id. reply_to_message_id answers a specific message. Long text is split at 4,096 "
                        "characters. Same as messages_send to telegram:<chat_id>:<topic_id>, plus replies and proof.",
            inputSchema={"type": "object", "required": ["chat_id", "message"], "additionalProperties": False,
                         "properties": {"chat_id": chat, "topic_id": topic,
                                        "message": {"type": "string", "minLength": 1, "maxLength": 32000},
                                        "reply_to_message_id": {"type": "integer", "minimum": 1}}},
            annotations=rw("Post in a topic")),
        types.Tool(
            name="telegram_topic_create", title="Open a Telegram topic",
            description="Create a topic in a group with Topics on, optionally posting a first message. Returns the "
                        "target for every follow-up. " + TOPIC_RULE + " The bot needs admin 'Manage topics'.",
            inputSchema={"type": "object", "required": ["chat_id", "name"], "additionalProperties": False,
                         "properties": {"chat_id": chat, "name": {"type": "string", "minLength": 1, "maxLength": 128},
                                        "icon_color": {"type": "integer", "enum": [7322096, 16766590, 13338331,
                                                                                   9367192, 16749490, 16478047]},
                                        "message": {"type": "string", "maxLength": 32000}}},
            annotations=rw("Open a topic")),
        types.Tool(
            name="telegram_topic_edit", title="Rename / close / reopen a Telegram topic",
            description="Rename a topic (e.g. prefix 'Closed: ' when a task is done) and/or close or reopen it. "
                        "Pass only what changes. topic_id 1 edits General. " + TOPIC_RULE,
            inputSchema={"type": "object", "required": ["chat_id", "topic_id"], "additionalProperties": False,
                         "properties": {"chat_id": chat, "topic_id": topic,
                                        "name": {"type": "string", "minLength": 1, "maxLength": 128},
                                        "closed": {"type": "boolean"}}},
            annotations=rw("Edit a topic", True)),
    ]


def annotate_upstream(t: types.Tool, multi_son: list[str]) -> types.Tool:
    title = t.title or t.name.replace("_", " ").capitalize()
    schema = dict(t.inputSchema or {"type": "object"})
    if len(multi_son) > 1:
        props = dict(schema.get("properties") or {})
        props["son"] = _son_prop(multi_son)
        schema["properties"] = props
    ann = t.annotations
    if ann is None or ann.readOnlyHint is None:  # the Son's toolbox annotates its own tools; keep those
        ro = t.name in READ_ONLY
        ann = types.ToolAnnotations(title=title, readOnlyHint=ro, destructiveHint=False, openWorldHint=not ro)
    # Full description: Hermes tool docs carry the usage rules (terminal, browser, cron...). Capped for clients.
    return types.Tool(name=t.name, title=title, description=(t.description or "").strip()[:4000],
                      inputSchema=schema, annotations=ann)


def text(obj: Any, is_error: bool = False) -> types.CallToolResult:
    body = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=1)
    return types.CallToolResult(content=[types.TextContent(type="text", text=body)], isError=is_error)


class Connector:
    def __init__(self, sons: list[SonLink], passthrough: str = "all",
                 transport: Optional[httpx.AsyncBaseTransport] = None, discord=None, github: Any = True,
                 gh_runner: Optional[GhRunner] = None, telegram=None,
                 entitlements: Optional[Entitlements] = None, model_control: Any = True):
        self.discord = discord
        # Model / provider control (model_control.py): local socket into this Son's own Hermes gateway. On by default.
        self.model_control = None if model_control in (False, None) else ModelControlClient(
            (model_control or {}).get("socket") if isinstance(model_control, dict) else None)
        self.dtools = None
        if discord is not None:
            from .discord_tools import DiscordTools
            self.dtools = DiscordTools(discord.bot_token)
        self.telegram = telegram
        # Paid add-on gate (entitlements.py): None = a fresh reader of <HERMES_HOME>/plugs.json. With no file there,
        # nothing is gated, so dev boxes and hand-built gateways behave exactly as before.
        self.entitlements = entitlements if entitlements is not None else Entitlements()
        self.sons = {s.name: s for s in sons}
        self.order = [s.name for s in sons]
        self.passthrough = passthrough
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10), transport=transport,
                                      headers={"User-Agent": f"aiteam-connector/{VERSION}"})
        # GitHub = the Son's own gh login, on by default (owner 2026-10-12). Only an explicit false/None turns it off.
        self.github = None if github in (False, None) else (gh_runner or GhRunner(github if isinstance(github, dict) else None))
        self.upstreams = {s.name: Upstream(s.upstream_mcp_url, s.upstream_token)
                          for s in sons if s.upstream_mcp_url and passthrough != "none"}

    def son(self, args: dict) -> SonLink:
        name = args.pop("son", None) or self.order[0]
        if name not in self.sons:
            raise ValueError(f"unknown son {name!r}; known: {', '.join(self.order)}")
        return self.sons[name]

    def _h(self, s: SonLink) -> dict:
        return {"Authorization": f"Bearer {s.box_token}"}

    def _with_reply(self, t: types.Tool) -> types.Tool:
        """messages_send gains reply_to_message_id / wake_author when Discord goes through the relay."""
        if t.name != "messages_send" or not self.discord:
            return t
        schema = dict(t.inputSchema or {"type": "object"})
        props = dict(schema.get("properties") or {})
        props["reply_to_message_id"] = {
            "type": "string", "pattern": "^[0-9]{15,22}$",
            "description": "Discord only. Reply to this message (same channel/thread as target): the post threads "
                           "under it and pings its author, the one who asked, not whoever that message tagged."}
        props["wake_author"] = {"type": "boolean", "default": True,
                                "description": "With reply_to_message_id: ping/wake the replied-to author. Default true."}
        schema["properties"] = props
        desc = (t.description or "") + (" Discord: pass reply_to_message_id to answer a specific message in its thread.")
        return types.Tool(name=t.name, title=t.title, description=desc, inputSchema=schema, annotations=t.annotations)

    async def list_tools(self) -> list[types.Tool]:
        tools = [usage_guide_tool()] + native_tools(self.order) + health_tools(self.order) \
            + (model_tools(self.order) if self.model_control else []) + (github_tools() if self.github else []) \
            + (discord_thread_tools() if self.discord else []) + (telegram_tools() if self.telegram else [])
        if self.dtools:
            from .discord_tools import discord_tools
            tools += discord_tools()
        seen = {t.name for t in tools}
        for name in self.order:
            up = self.upstreams.get(name)
            if not up:
                continue
            try:
                for t in await up.tools():
                    if t.name in seen or (self.passthrough == "session" and t.name not in SESSION_TOOLS):
                        continue
                    tools.append(self._with_reply(annotate_upstream(t, self.order)))
                    seen.add(t.name)
            except Exception as e:  # noqa: BLE001 - a Son being down must not hide the native tools
                log.warning("upstream tools for %s unavailable: %s", name, type(e).__name__)
        return tools

    async def call(self, name: str, args: dict, progress=None, caller: Optional[str] = None) -> types.CallToolResult:
        args = dict(args or {})
        # Authorization: a paid tool the Son does not own answers with the upsell (normal result, tool not run).
        wall = self.entitlements.check(name, args, caller)
        if wall is not None:
            return wall
        try:
            if name in GITHUB_TOOLS or name in LEGACY_TOOLS:
                if not self.github:
                    return text(f"Tool {name} is not enabled on this connector.", True)
                args.pop("son", None)
                try:
                    ok, out = await self.github.call(name, args)
                except GhError as e:
                    return text(f"{e}", True)
                return text(out, not ok)
            if name == "usage_guide":
                return text(GUIDE)
            if name in MODEL_TOOLS:
                if not self.model_control:
                    return text(f"Tool {name} is not enabled on this connector.", True)
                target = args.pop("son", None) or self.order[0]
                if target != self.order[0]:
                    return text(f"Model tools reach only this connector's own Son ({self.order[0]}).", True)
                return await call_model_tool(self.model_control, name, args, caller)
            if name in DISCORD_THREAD_TOOLS:
                return await self._discord_thread(name, args)
            from .discord_tools import DISCORD_TOOLS as DISCORD_DIRECT, dump
            if name in DISCORD_DIRECT:
                if not self.dtools:
                    return text(f"Tool {name} needs the Discord relay (connector config discord.bot_token_env).", True)
                args.pop("son", None)
                obj, err = await self.dtools.call(name, args)
                return text(dump(obj), err)
            # messages_read on a raw Discord channel/thread (not a Son session key): read Discord directly.
            sk = str(args.get("session_key", ""))
            if name == "messages_read" and self.dtools and sk.startswith("discord:") and not sk.startswith("discord:dm"):
                obj, err = await self.dtools.call("discord_read", {"target": sk, "limit": args.get("limit") or 30})
                return text(dump(obj), err)
            if name in TELEGRAM_TOOLS:
                return await self._telegram(name, args)
            if name == "son_list":
                return await self._son_list()
            if name in HEALTH_TOOLS:
                return await self._health(name, self.son(args), args)
            s = self.son(args)
            if name == "son_send":
                return await self._send(s, args, progress)
            if name == "son_conversations":
                r = await self.http.get(f"{s.box_url}/v1/sons/{s.name}/conversations", headers=self._h(s))
                return text(r.json(), r.status_code >= 400)
            if name == "son_conversation_history":
                conv = args.get("conversation") or "default"
                r = await self.http.get(f"{s.box_url}/v1/sons/{s.name}/conversations/{conv}/messages",
                                        params={"limit": int(args.get("limit") or 20)}, headers=self._h(s))
                return text(r.json(), r.status_code >= 400)
            if name == "messages_send" and self.discord and str(args.get("target", "")).startswith("discord:"):
                return await self._discord_send(args)
            if name == "messages_send" and (args.get("reply_to_message_id") or "wake_author" in args):
                if args.get("reply_to_message_id"):
                    return text({"error": "reply_to_message_id works for Discord targets only (discord:<channel>:<thread>). "
                                          "Telegram and other platforms: send without it.", "delivered": False}, True)
                args.pop("wake_author", None)
            up = self.upstreams.get(s.name)
            if up is None:
                return text(f"Tool {name} is not available for {s.name}.", True)
            return await up.call(name, args)
        except (ValueError, httpx.HTTPError) as e:
            return text(f"{type(e).__name__}: {e}", True)

    async def _discord_send(self, args: dict) -> types.CallToolResult:
        """Discord targets: post via webhook so the Son sees a non-self author (wakes on <@son>), and report
        ``delivered`` only after Discord returns the message. Never falls back to the self-authored upstream path:
        that path is exactly what could not wake a Son."""
        from .discord_relay import RelayError
        target = str(args.get("target", ""))
        assert self.discord is not None
        if not self.discord.parse_target(target):
            return text({"error": "Discord target must be discord:<channel_id> or discord:<channel_id>:<thread_id> "
                                  "(numeric ids; find them with channels_list / conversations_list)"}, True)
        try:
            reply_to = args.get("reply_to_message_id")
            return text(await self.discord.send(target, str(args.get("message") or ""),
                                                reply_to=str(reply_to) if reply_to not in (None, "") else None,
                                                wake_author=args.get("wake_author", True) is not False))
        except RelayError as e:
            return text({"error": str(e), "status": e.status, "delivered": False}, True)

    async def _discord_thread(self, name: str, args: dict) -> types.CallToolResult:
        from .discord_relay import RelayError
        if not self.discord:
            return text(f"Tool {name} needs the Discord relay (connector config discord.bot_token_env).", True)
        args.pop("son", None)
        try:
            if name == "discord_threads_list":
                out = await self.discord.threads_list(args["channel_id"], bool(args.get("include_archived")),
                                                      int(args.get("limit") or 50), bool(args.get("include_members")),
                                                      args.get("member_id"))
            elif name == "discord_thread_get":
                if args.get("include_members") or args.get("member_id"):
                    out = await self.discord.thread_get_members(args["thread_id"], args.get("member_id"))
                else:
                    out = await self.discord.thread_get(args["thread_id"])
            elif name == "discord_thread_edit":
                out = await self.discord.thread_edit(args["thread_id"], args.get("name"), args.get("archived"),
                                                     args.get("locked"), args.get("auto_archive_duration"),
                                                     args.get("slowmode_seconds"))
            elif name == "discord_thread_member_add":
                out = await self.discord.thread_member(args["thread_id"], args["user_id"], add=True)
            elif name == "discord_thread_member_remove":
                out = await self.discord.thread_member(args["thread_id"], args["user_id"], add=False)
            elif name == "discord_thread_members":
                out = await self.discord.thread_members(args["thread_id"], int(args.get("limit") or 100))
            else:
                out = await self.discord.thread_create(args["channel_id"], args.get("name", ""), args.get("message"),
                                                       args.get("from_message_id"),
                                                       int(args.get("auto_archive_duration") or 10080),
                                                       args.get("add_user_ids"))
        except KeyError as e:
            return text(f"{e.args[0]} is required", True)
        except RelayError as e:
            return text({"error": str(e), "status": e.status}, True)
        return text(out)

    async def _upstream_json(self, name: str, args: dict) -> Any:
        """Call a tool on the first Son's own hermes mcp serve and parse its JSON text."""
        up = self.upstreams.get(self.order[0]) if self.order else None
        if up is None:
            raise LookupError("no upstream Son")
        res = await up.call(name, args)
        body = "".join(getattr(c, "text", "") for c in (res.content or []))
        try:
            return json.loads(body)
        except ValueError:
            return {"text": body}

    async def _telegram(self, name: str, args: dict) -> types.CallToolResult:
        from .telegram_control import TelegramError
        if not self.telegram:
            return text(f"Tool {name} needs the Son's Telegram bot (connector config telegram.bot_token_env).", True)
        args.pop("son", None)
        t = self.telegram
        upstream = self._upstream_json if self.upstreams else None
        try:
            if name == "telegram_chat_get":
                out = await t.chat_get(args["chat_id"])
            elif name == "telegram_topics_list":
                out = await t.topics_list(args["chat_id"], upstream)
            elif name == "telegram_topic_messages":
                if upstream is None:
                    return text("Reading topics needs the Son's own MCP server (upstream_mcp_url).", True)
                out = await t.topic_messages(args["chat_id"], args["topic_id"], int(args.get("limit") or 50), upstream)
            elif name == "telegram_topic_send":
                out = await t.send(args["chat_id"], args.get("topic_id"), str(args.get("message") or ""),
                                   args.get("reply_to_message_id"))
            elif name == "telegram_topic_create":
                out = await t.topic_create(args["chat_id"], args.get("name", ""), args.get("icon_color"),
                                           args.get("message"))
            else:
                out = await t.topic_edit(args["chat_id"], args["topic_id"], args.get("name"), args.get("closed"))
        except KeyError as e:
            return text(f"{e.args[0]} is required", True)
        except TelegramError as e:
            return text({"error": str(e), "status": e.status}, True)
        return text(out)

    async def _health(self, name: str, s: SonLink, args: dict) -> types.CallToolResult:
        try:
            if name == "ping":
                r = await self.http.get(f"{s.box_url}/v1/ping", headers=self._h(s), timeout=10)
                if r.status_code >= 400:
                    return text(f"{s.name}'s computer answered with an error ({r.status_code}).", True)
                d = r.json()
                return text(f"Up for {d.get('uptime') or 'a while'}.")
            if name == "health":
                params = {"history": "1"} if args.get("history") else None
                r = await self.http.get(f"{s.box_url}/v1/health", params=params, headers=self._h(s), timeout=20)
                if r.status_code >= 400:
                    return text(f"Could not read {s.name}'s health ({r.status_code}): {r.text[:200]}", True)
                return text(health_text(r.json()))
            r = await self.http.post(f"{s.box_url}/v1/cleanup", headers=self._h(s), timeout=60)
            d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"message": r.text[:300]}
            # 429 (too soon) and 409 (already running) are normal answers the assistant reads out, not failures.
            return text(health_text(d), r.status_code >= 400 and r.status_code not in (409, 429))
        except httpx.TimeoutException:
            if name == "cleanup":
                return text("Cleanup started and is still running. Check health in a few minutes.")
            return text(f"{s.name}'s computer did not answer in time. It may be down or very busy.", True)
        except httpx.HTTPError as e:
            return text(f"{s.name}'s computer is not reachable ({type(e).__name__}). It may be down.", True)

    async def _son_list(self) -> types.CallToolResult:
        out = []
        for n in self.order:
            s = self.sons[n]
            try:
                r = await self.http.get(f"{s.box_url}/v1/sons", headers=self._h(s), timeout=10)
                hit = next((x for x in r.json().get("sons", []) if x.get("name") == n), None)
                out.append({"son": n, "online": bool(hit and hit.get("ok")), "version": (hit or {}).get("version")})
            except Exception as e:  # noqa: BLE001
                out.append({"son": n, "online": False, "error": type(e).__name__})
        return text({"sons": out})

    async def _send(self, s: SonLink, args: dict, progress) -> types.CallToolResult:
        """Relay a message to a Son. Waits up to SON_SEND_WAIT_S (default 25 s) for the reply; if the
        turn is longer, returns what is there so far and keeps reading in the background so the turn
        is never cut off (2026-10-23 fix: long turns hit the client tool timeout, the box saw
        'Cannot write to closing transport' and the reply was lost)."""
        msg = args.get("message")
        if not isinstance(msg, str) or not msg.strip():
            return text("message is required", True)
        conv = args.get("conversation") or "default"
        body = {"text": msg, "conversation": conv, "stream": True}
        st = {"reply": "", "deltas": [], "tools": [], "err": "", "n": 0, "http": ""}

        async def pump() -> None:
            try:
                async with self.http.stream("POST", f"{s.box_url}/v1/sons/{s.name}/messages", json=body,
                                            headers=self._h(s)) as r:
                    if r.status_code >= 400:
                        st["http"] = f"box {r.status_code}: {(await r.aread()).decode(errors='replace')[:300]}"
                        return
                    ev, data = "", []
                    async for ln in r.aiter_lines():
                        if ln.startswith("event:"):
                            ev = ln[6:].strip()
                        elif ln.startswith("data:"):
                            data.append(ln[5:].lstrip())
                        elif ln == "" and (ev or data):
                            try:
                                d = json.loads("\n".join(data) or "{}")
                            except ValueError:
                                d = {}
                            if ev == "assistant.delta":
                                st["deltas"].append(str(d.get("delta") or ""))
                            elif ev == "assistant.completed":
                                st["reply"] = str(d.get("content") or "")
                            elif ev == "tool.started":
                                st["tools"].append(str(d.get("tool_name") or ""))
                                st["n"] += 1
                                if progress:
                                    try:
                                        await progress(st["n"], f"{s.name} is using {d.get('tool_name')}")
                                    except Exception:  # noqa: BLE001  client may be gone; keep the turn alive
                                        progress_off()
                            elif ev == "error":
                                st["err"] = str(d.get("message") or d)
                            ev, data = "", []
            except Exception as e:  # noqa: BLE001
                st["err"] = st["err"] or f"{type(e).__name__}: {e}"

        def progress_off() -> None:
            nonlocal progress
            progress = None

        task = asyncio.create_task(pump())
        bg = getattr(self, "_bg_sends", None)
        if bg is None:
            bg = self._bg_sends = set()
        bg.add(task)
        task.add_done_callback(bg.discard)
        wait_s = float(os.environ.get("SON_SEND_WAIT_S", "25"))
        done, _ = await asyncio.wait({task}, timeout=wait_s)
        footer = f"\n\n[son: {s.name} · conversation: {conv}" + (f" · tools: {', '.join(st['tools'])}" if st["tools"] else "") + "]"
        if not done:
            progress_off()
            so_far = "".join(st["deltas"]).strip()
            return text((f"{so_far}\n\n" if so_far else "") +
                        f"[{s.name} is still working (>{int(wait_s)} s); the turn continues on the box. "
                        f"Get the full reply with son_conversation_history son={s.name} conversation={conv}.]" + footer)
        if st["http"]:
            return text(st["http"], True)
        reply = st["reply"] or "".join(st["deltas"])
        if st["err"] and not reply:
            return text(f"{s.name} could not answer: {st['err']}", True)
        return text((reply or "(no reply)") + footer)


# --------------------------------------------------------------------------------------------- server
def build_server(conn: Connector) -> Server:
    server = Server("aiteam-connector", version=VERSION,
                    instructions=INSTRUCTIONS)

    @server.list_tools()
    async def _list() -> list[types.Tool]:
        return await conn.list_tools()

    # Who we are (intro.py): the first thing a client meets, as an MCP prompt and an MCP resource.
    @server.list_prompts()
    async def _prompts() -> list[types.Prompt]:
        return [intro_prompt()]

    @server.get_prompt()
    async def _get_prompt(name: str, arguments: dict | None) -> types.GetPromptResult:
        if name != intro_prompt().name:
            raise ValueError(f"unknown prompt {name!r}")
        return intro_prompt_result()

    @server.list_resources()
    async def _resources() -> list[types.Resource]:
        return [intro_resource()]

    @server.read_resource()
    async def _read(uri):
        from mcp.server.lowlevel.helper_types import ReadResourceContents
        if str(uri) != INTRO_URI:
            raise ValueError(f"unknown resource {uri}")
        return [ReadResourceContents(content=INTRO, mime_type="text/markdown")]

    @server.call_tool(validate_input=False)
    async def _call(name: str, arguments: dict) -> types.CallToolResult:
        ctx = server.request_context
        token = getattr(ctx.meta, "progressToken", None) if ctx.meta else None

        async def progress(n: int, message: str) -> None:
            if token is not None:
                with contextlib.suppress(Exception):
                    await ctx.session.send_progress_notification(token, n, message=message)
        # The OAuth gate verified the token and forwards the caller's email (X-Aiteam-Sub) for the owner exemption.
        req = getattr(ctx, "request", None)
        caller = None
        if req is not None and getattr(req, "headers", None) is not None:
            caller = req.headers.get("x-aiteam-sub") or None
        tok = CALLER.set(caller)
        try:
            return await conn.call(name, arguments, progress, caller=caller)
        finally:
            CALLER.reset(tok)

    return server


def serve_http(conn: Connector, host: str, port: int) -> None:
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    mgr = StreamableHTTPSessionManager(app=build_server(conn), json_response=False, stateless=True)

    async def health(_req):
        return JSONResponse({"ok": True, "version": VERSION, "sons": conn.order})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with mgr.run():
            yield

    inner = Starlette(routes=[Route("/healthz", health)], lifespan=lifespan)

    async def app(scope, receive, send):
        # Serve /mcp and /mcp/ directly: a Starlette Mount would 307 /mcp -> /mcp/, and that redirect (with this
        # box's local address) would leak through the OAuth gate to Claude/ChatGPT.
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
            await mgr.handle_request(scope, receive, send)
        else:
            await inner(scope, receive, send)

    uvicorn.run(app, host=host, port=port, log_level="warning")


async def serve_stdio(conn: Connector) -> None:
    from mcp.server.stdio import stdio_server
    server = build_server(conn)
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


def main(argv: Optional[list[str]] = None) -> None:
    p = argparse.ArgumentParser(prog="aiteam-connector")
    p.add_argument("--config")
    p.add_argument("--transport", choices=["http", "stdio"], default="http")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8720)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(message)s")
    sons, mode = load_config(a.config)
    raw = json.loads(open(a.config).read()) if a.config else json.loads(os.environ["AITEAM_CONNECTOR_CONFIG"])
    conn = Connector(sons, mode, discord=load_discord(raw), github=raw.get("github", True), telegram=load_telegram(raw),
                     model_control=raw.get("model_control", True))
    if a.transport == "stdio":
        asyncio.run(serve_stdio(conn))
    else:
        serve_http(conn, a.host, a.port)


if __name__ == "__main__":
    main(sys.argv[1:])
