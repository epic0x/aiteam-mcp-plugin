"""Model / provider control tools: read and switch what model a Son runs, for every session or one, and the
fallback order. Owner request 2026-10-26 (aiteam-mcp-plugin#model-switch-tools-20261026).

The switch itself happens inside the Son's Hermes gateway (son/model_control/handler.py, a gateway hook): the live
per-session override, the cached agents and config.yaml belong to that process. The connector talks to it over a
local unix socket (<HERMES_HOME>/mcp/model-control.sock, mode 0600, never TCP). Nothing here holds or returns a
credential: the gateway answers with provider names, model ids and a credentials_ok boolean only.

Tools:
  son_model_status        read   global primary + fallback order, configured providers/models, and per session the
                                 override, the effective primary and the full order (one session or a list)
  son_model_set_all       write  set provider + model for ALL sessions (config.yaml model.*, clears every
                                 session override so nothing pins an old model)
  son_model_set_session   write  set (or clear) provider + model for ONE session (sessionKey/sessionId from
                                 sessions_list); the same persisted override `/model` writes
  son_model_fallback_set  write  set the fallback order (e.g. second Anthropic account, then GPT last), optionally
                                 the primary too

Owner only. The Son's public gate already admits only an aiteam.space token issued for this Son (its owner). On
top, when the gate forwards the verified caller (X-Aiteam-Sub) and AITEAM_SON_OWNERS / AITEAM_GATE_EXEMPT list
emails, a caller outside that list is refused for every model tool (read included: the route reveals config).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional

import mcp.types as types

MODEL_TOOLS = ("son_model_status", "son_model_set_all", "son_model_set_session", "son_model_fallback_set")
SOCKET_NAME = "model-control.sock"
_ID = {"type": "string", "minLength": 1, "maxLength": 200, "pattern": r"^[A-Za-z0-9._:/@+\-\[\]]{1,200}$"}


def default_socket() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return Path(home) / "mcp" / SOCKET_NAME


def owner_emails() -> set[str]:
    raw = os.environ.get("AITEAM_SON_OWNERS", "")
    owners = {e.strip().lower() for e in raw.split(",") if "@" in e}
    if owners:
        owners |= {e.strip().lower() for e in os.environ.get("AITEAM_GATE_EXEMPT", "").split(",") if "@" in e}
    return owners


def caller_allowed(caller: Optional[str], owners: Optional[set[str]] = None) -> bool:
    """Require the owner identity forwarded by the Son's JWT gate; no anonymous or stdio bypass."""
    owners = owner_emails() if owners is None else owners
    # No anonymous/stdio exemption: all model tools (including reads) need the gate's verified caller.
    # Without an additional local owner list, the gate's signed, Son-bound owner token is authoritative.
    if not caller or "@" not in caller:
        return False
    return not owners or caller.strip().lower() in owners


def _route_props() -> dict:
    return {"provider": {**_ID, "description": "Provider name as configured on the Son (see son_model_status "
                                               "providers), e.g. anthropic-primary, anthropic-secondary, openai-codex."},
            "model": {**_ID, "description": "Model id offered by that provider, e.g. claude-opus-5-5, gpt-6.1-sol."}}


def model_tools(son_names: list[str]) -> list[types.Tool]:
    son = {"type": "string", "enum": son_names, "description": f"Which Son. Default: {son_names[0]}."}
    sess = {"sessionKey": {"type": "string", "maxLength": 256, "description": "Session key from sessions_list."},
            "sessionId": {"type": "string", "maxLength": 256, "description": "Session id from sessions_list."}}
    route = {"type": "object", "required": ["provider", "model"], "additionalProperties": False,
             "properties": _route_props()}
    write = lambda title, idem, destructive=False: types.ToolAnnotations(  # noqa: E731
        title=title, readOnlyHint=False, destructiveHint=destructive, idempotentHint=idem, openWorldHint=False)
    return [
        types.Tool(
            name="son_model_status", title="Which model is my Son using?",
            description="Current model and provider of the Son, and the fallback order it tries when the primary "
                        "fails (primary, then fallback_1, fallback_2 ...). With sessionKey or sessionId: that one "
                        "session (its override if any, the effective primary, the full order, and the model of the "
                        "live agent). Without: the global route plus every recent session, with which ones have "
                        "their own override. Also lists the providers configured on the Son and their models. "
                        "Never shows keys.",
            inputSchema={"type": "object", "additionalProperties": False,
                         "properties": {**sess, "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                                        "offset": {"type": "integer", "minimum": 0}, "son": son}},
            annotations=types.ToolAnnotations(title="Which model is my Son using?", readOnlyHint=True,
                                              idempotentHint=True, openWorldHint=False)),
        types.Tool(
            name="son_model_set_all", title="Switch the model for all sessions",
            description="Set the provider and model for ALL of the Son's sessions at once, e.g. provider "
                        "anthropic-secondary, model claude-opus-5-5. Saved as the Son's default (survives "
                        "restarts) and, by default, clears every per-session override so no session stays on an "
                        "old model. A turn already running finishes on its old model; the next turn uses the new "
                        "one. The provider must already be set up on the Son (son_model_status lists them).",
            inputSchema={"type": "object", "required": ["provider", "model"], "additionalProperties": False,
                         "properties": {**_route_props(),
                                        "clear_session_overrides": {"type": "boolean", "default": True,
                                            "description": "Also drop every session's own override (default "
                                                           "true). false = sessions with an override keep it."},
                                        "son": son}},
            annotations=write("Switch model for all sessions", True)),
        types.Tool(
            name="son_model_set_session", title="Switch the model for one session",
            description="Set the provider and model for ONE session (sessionKey or sessionId from sessions_list), "
                        "like /model in that chat. Persisted, survives restarts. clear=true removes the override "
                        "so the session goes back to the Son's default. Running turns finish first.",
            inputSchema={"type": "object", "additionalProperties": False,
                         "properties": {**sess, **_route_props(),
                                        "clear": {"type": "boolean", "default": False,
                                                  "description": "Remove the override (inherits channel/default route)."},
                                        "son": son}},
            annotations=write("Switch model for one session", True)),
        types.Tool(
            name="son_model_fallback_set", title="Set the fallback order",
            description="Set the order the Son tries when the primary model fails, e.g. fallback = "
                        "[{provider: anthropic-secondary, model: claude-opus-5-5}, {provider: openai-codex, model: "
                        "gpt-6.1-sol}] for primary Anthropic, then the second Anthropic account, GPT last. Replaces "
                        "the whole list (an empty list turns fallback off). Optional primary sets the first entry "
                        "too (as son_model_set_all without clearing overrides). Applies to every session from its "
                        "next turn; Hermes has one fallback list per Son, not per session.",
            inputSchema={"type": "object", "required": ["fallback"], "additionalProperties": False,
                         "properties": {"fallback": {"type": "array", "maxItems": 8, "items": route},
                                        "primary": route, "son": son}},
            annotations=write("Set the fallback order", True)),
    ]


class ModelControlClient:
    def __init__(self, socket_path: Optional[str] = None, timeout: float = 30.0):
        self.socket_path = Path(socket_path) if socket_path else default_socket()
        self.timeout = timeout

    async def request(self, verb: str, args: dict, *, principal: Optional[str] = None) -> dict:
        if not self.socket_path.exists():
            return {"ok": False, "error": "Model control is not running on this Son yet: its Hermes gateway needs the "
                                          "aiteam-model-control hook (Son image update), then a gateway restart."}
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self.socket_path), limit=512 * 1024), 5)
        except (OSError, asyncio.TimeoutError) as e:
            return {"ok": False, "error": f"Model control socket not answering ({type(e).__name__}). The Son's "
                                          "gateway may be restarting."}
        try:
            writer.write(json.dumps({"verb": verb, "args": args, **({"principal": principal} if principal else {})}).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), self.timeout)
            return json.loads(line.decode() or "{}")
        except (OSError, ValueError, asyncio.TimeoutError) as e:
            return {"ok": False, "error": f"model control failed ({type(e).__name__})"}
        finally:
            writer.close()


def _session_args(args: dict) -> dict:
    out = {}
    if args.get("sessionKey"):
        out["session_key"] = str(args["sessionKey"])
    if args.get("sessionId"):
        out["session_id"] = str(args["sessionId"])
    return out


def build_request(name: str, args: dict) -> tuple[str, dict]:
    """Tool call -> (verb, gateway args). Raises ValueError on a bad call."""
    route = {"provider": args.get("provider"), "model": args.get("model")}
    if name == "son_model_status":
        out = _session_args(args)
        if args.get("limit"):
            out["limit"] = int(args["limit"])
        if args.get("offset"):
            out["offset"] = int(args["offset"])
        return "status", out
    if name == "son_model_set_all":
        if not (route["provider"] and route["model"]):
            raise ValueError("provider and model are required")
        return "set_global", {**route, "clear_session_overrides": args.get("clear_session_overrides", True) is not False}
    if name == "son_model_set_session":
        sa = _session_args(args)
        if not sa:
            raise ValueError("sessionKey or sessionId is required (from sessions_list)")
        if args.get("clear"):
            return "set_session", {**sa, "clear": True}
        if not (route["provider"] and route["model"]):
            raise ValueError("provider and model are required (or clear=true)")
        return "set_session", {**sa, **route}
    if name == "son_model_fallback_set":
        if not isinstance(args.get("fallback"), list):
            raise ValueError("fallback (a list of {provider, model}) is required")
        out: dict[str, Any] = {"fallback": args["fallback"]}
        if args.get("primary"):
            out["primary"] = args["primary"]
        return "set_fallback", out
    raise ValueError(f"unknown model tool {name}")


def summary(name: str, res: dict) -> str:
    """One readable first line, then the JSON."""
    if not res.get("ok"):
        return f"Model control error: {res.get('error') or 'unknown error'}"
    if name == "son_model_status":
        if res.get("session"):
            s = res["session"]
            order = " -> ".join(f"{r['provider']}/{r['model']}" for r in s["fallback_order"])
            return f"Session {s['sessionKey']} ({s['source']}): {order}"
        order = " -> ".join(f"{r['provider']}/{r['model']}" for r in res["global"]["order"])
        return f"All sessions: {order}. {res.get('overridden', 0)} of {res.get('count', 0)} listed sessions have their own model."
    if name == "son_model_set_all":
        p = res["primary"]
        return (f"All sessions now use {p['provider']}/{p['model']} from their next turn"
                f" ({res.get('session_overrides_cleared', 0)} session overrides cleared).")
    if name == "son_model_set_session":
        o = res.get("override")
        when = "after its current turn" if res.get("applies") == "after_current_turn" else "from its next turn"
        return (f"Session {res['sessionKey']} uses {o['provider']}/{o['model']} {when}." if o else
                f"Session {res['sessionKey']} is back on its inherited channel/default route {when}.")
    order = " -> ".join(f"{r['provider']}/{r['model']}" for r in res.get("order", []))
    return f"Fallback order is now: {order}."


async def call_model_tool(client: ModelControlClient, name: str, args: dict, caller: Optional[str]) -> types.CallToolResult:
    def out(body: str, err: bool) -> types.CallToolResult:
        return types.CallToolResult(content=[types.TextContent(type="text", text=body)], isError=err)

    if not caller_allowed(caller):
        return out("Only the Son's owner can read or change its model.", True)
    try:
        verb, gw_args = build_request(name, args)
    except (ValueError, TypeError) as e:
        return out(str(e), True)
    res = await client.request(verb, gw_args)
    return out(summary(name, res) + "\n\n" + json.dumps(res, ensure_ascii=False, indent=1), not res.get("ok"))
