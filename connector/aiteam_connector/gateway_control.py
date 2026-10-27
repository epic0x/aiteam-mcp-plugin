"""Owner-bound gateway controls. No shell, credentials, or arbitrary command kwargs."""
from __future__ import annotations
import json
import mcp.types as types
from jsonschema import validate, ValidationError
from .model_control import ModelControlClient, _session_args

CONTROL_TOOLS = ("son_session_stop", "son_session_interrupt", "son_session_reset", "son_slash", "son_approvals_set")


def control_tools(sons: list[str]) -> list[types.Tool]:
    common = {"son": {"type": "string", "enum": sons},
              "sessionKey": {"type": "string", "minLength": 1, "maxLength": 256},
              "sessionId": {"type": "string", "minLength": 1, "maxLength": 256}}
    definitions = [
        ("son_session_stop", "Stop one session", "Hard-interrupt this exact gateway session and clear its queued user work. Does not stop sibling sessions.", {}, [], True, True),
        ("son_session_interrupt", "Interrupt one session", "Same native hard-interrupt/queue cleanup as stop, not a soft pause. Exact existing gateway session only.", {}, [], True, True),
        ("son_session_reset", "Reset one session", "Interrupt this exact session, discard its active conversation and create a fresh session ID. Destructive and not retry-idempotent.", {}, [], True, False),
        ("son_slash", "Dispatch a gateway slash command", "Dispatch an audited built-in command in an existing owner-bound gateway session. Not shell execution. Only status/context/help/commands/profile/version/whoami/agents, stop/new/reset, reasoning, fast and footer are enabled. No CLI, plugins, quick commands, cross-session resume, or LLM fallthrough. Credential commands are refused: use ai_signin_start and its secure page, never paste secrets here. Opaque native replies are suppressed; dispatched does not prove the requested effect.",
         {"command": {"type": "string", "minLength": 1, "maxLength": 128}}, ["command"], True, False),
        ("son_approvals_set", "Set smart or yolo approval mode", "Explicit persistent profile policy: smart maps to native smart; yolo maps to native off (bypasses approval prompts). Requires a configured owner platform binding and native admin permission. Existing session-yolo overrides are NOT cleared. Never toggles /yolo implicitly.",
         {"mode": {"type": "string", "enum": ["smart", "yolo"]}}, ["mode"], True, True),
    ]
    return [types.Tool(name=name, title=title, description=description,
            inputSchema={"type": "object", "properties": {**common, **props}, "required": required,
                         "anyOf": [{"required": ["sessionKey"]}, {"required": ["sessionId"]}], "additionalProperties": False},
            annotations=types.ToolAnnotations(title=title, readOnlyHint=False, destructiveHint=destructive,
                                              idempotentHint=idem, openWorldHint=True))
            for name, title, description, props, required, destructive, idem in definitions]


def build_control_request(name: str, args: dict) -> tuple[str, dict]:
    schema = next(t.inputSchema for t in control_tools(["local"]) if t.name == name)
    try:
        validate(args, schema)
    except ValidationError:
        raise ValueError("Invalid control arguments; supply an existing session and no credential or identity fields.") from None
    out = _session_args(args)
    if name == "son_slash":
        out["command"] = args["command"]
    if name == "son_approvals_set":
        out["mode"] = args["mode"]
    return name.removeprefix("son_"), out


async def call_control_tool(client: ModelControlClient, name: str, args: dict, caller: str) -> types.CallToolResult:
    try:
        verb, payload = build_control_request(name, args)
        result = await client.request(verb, payload, principal=caller.strip().lower())
    except Exception:
        result = {"ok": False, "error": "Control request failed or arguments invalid; result unverified."}
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result))],
                                structuredContent=result, isError=not result.get("ok"))
