"""Experimental stateless selected-Son MCP dispatch (never used by legacy grants).

Lists and discovery require JSON. Calls accept bounded, request-scoped SSE; sessions,
resume streams, subscriptions, nonempty templates and unknown methods fail closed.
"""
from __future__ import annotations

import base64
import copy
import json
import logging
from typing import Literal, overload

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .exchange import ExchangeError, Exchanger, InvalidGrant
from . import sse

log = logging.getLogger("aiteam_relay.router")
LISTS = {"tools/list": ("tools", "name"), "resources/list": ("resources", "uri"),
         "prompts/list": ("prompts", "name"),
         "resources/templates/list": ("resourceTemplates", "uriTemplate")}
CALLS = {"tools/call": ("tools/list", "name"), "resources/read": ("resources/list", "uri"),
         "prompts/get": ("prompts/list", "name")}
MAX_PAGES = 50


def _enc(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _dec(value: str) -> str:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()


def _header_encode(value: str) -> str:
    if (value and value == value.strip() and all(32 <= ord(c) <= 126 for c in value)
        and not (value.startswith("=?base64?") and value.endswith("?="))):
        return value
    return "=?base64?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def _header_decode(value: str | None) -> str | None:
    if value is None:
        return None
    if value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2], validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            return None
    if (not value or value != value.strip() or any(ord(c) < 32 or ord(c) > 126 for c in value)):
        return None
    return value


def _has_header_annotation(schema: object) -> bool:
    pending = [schema]
    visited = 0
    while pending:
        item = pending.pop()
        visited += 1
        if visited > 10000:
            return True  # bounded scan: uncertainty cannot make the list public
        if isinstance(item, dict):
            if "x-mcp-header" in item:
                return True
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def alias(target: str, value: str, *, uri: bool = False) -> str:
    if uri:
        return f"aiteam-son://{_enc(target)}/{_enc(value)}"
    return f"son_{_enc(target)}__{_enc(value)}"


def unalias(value: object, *, uri: bool = False) -> tuple[str, str] | None:
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        if uri:
            prefix = "aiteam-son://"
            if not value.startswith(prefix):
                return None
            a, b = value[len(prefix):].split("/", 1)
        else:
            if not value.startswith("son_"):
                return None
            a, b = value[4:].split("__", 1)
        target, original = _dec(a), _dec(b)
        if target and original and alias(target, original, uri=uri) == value:
            return target, original
    except (ValueError, UnicodeDecodeError, UnicodeError):
        pass
    return None


def failure(status: int, message: str) -> Response:
    return JSONResponse({"error": "unsupported_route", "error_description": message}, status_code=status)


async def route(request: Request, *, client: httpx.AsyncClient, exchanger: Exchanger,
                subject_token: str, claims: dict, expected_origin: str,
                on_invalid_grant, on_error) -> Response:
    # Modern Streamable HTTP requires Origin validation. This only applies to
    # experimental v2; legacy remains the original opaque proxy.
    if request.headers.get("origin") and request.headers["origin"] != expected_origin:
        return failure(403, "Origin is not the relay origin")
    # A second transport/session cannot share legacy session or stream IDs. No GET
    # stream exists in 2026-07-28; reject even if a Son would otherwise accept it.
    if request.method != "POST":
        return failure(405, "selected-Son routing requires stateless POST")
    if request.headers.get("mcp-session-id") or request.headers.get("last-event-id"):
        return failure(400, "session and stream resume IDs cannot be routed across Sons")
    if request.headers.get("mcp-protocol-version") != "2026-07-28":
        return failure(400, "selected-Son routing only supports stateless MCP 2026-07-28")
    try:
        msg = await request.json()
    except (ValueError, UnicodeError):
        return failure(400, "invalid JSON-RPC request")
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "id" not in msg:
        return failure(400, "JSON-RPC notifications and batches are not routable")
    method = msg.get("method")
    params = msg.get("params", {})
    if not isinstance(params, dict):
        return failure(400, "invalid JSON-RPC params")
    meta = params.get("_meta")
    if (not isinstance(meta, dict)
        or meta.get("io.modelcontextprotocol/protocolVersion") != "2026-07-28"
        or not isinstance(meta.get("io.modelcontextprotocol/clientCapabilities"), dict)
        or request.headers.get("mcp-method") != method):
        return failure(400, "MCP request metadata mismatch")
    if method in CALLS:
        identity = CALLS[method][1]
        if _header_decode(request.headers.get("mcp-name")) != params.get(identity):
            return failure(400, "Mcp-Name does not match the request identity")
    ids = claims["son_ids"]

    @overload
    async def invoke(target: str, payload: dict, *, stream: Literal[False] = False) -> dict: ...

    @overload
    async def invoke(target: str, payload: dict, *, stream: Literal[True]) -> dict | httpx.Response: ...

    async def invoke(target: str, payload: dict, *, stream: bool = False) -> dict | httpx.Response:
        ex = await exchanger.get(subject_token, claims["exp"], target)
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"authorization": f"Bearer {ex.access_token}", "content-type": "application/json",
                   "accept": "application/json, text/event-stream", "mcp-protocol-version": "2026-07-28",
                   "mcp-method": payload["method"]}
        key = "uri" if payload["method"] == "resources/read" else "name"
        if payload["method"] in CALLS:
            headers["mcp-name"] = _header_encode(payload["params"][key])
        for attempt in (1, 2):
            try:
                if stream:
                    upstream = client.build_request("POST", ex.son_mcp_url, content=body, headers=headers,
                        timeout=httpx.Timeout(connect=10, read=None, write=30, pool=10))
                    response = await client.send(upstream, stream=True)
                else:
                    response = await client.post(ex.son_mcp_url, content=body, headers=headers, timeout=30)
            except httpx.HTTPError:
                raise ExchangeError("Son is unreachable") from None
            if response.status_code != 401:
                break
            await response.aclose()
            exchanger.drop(subject_token, target)
            if attempt == 2:
                raise ExchangeError("Son refused the exchanged token")
            ex = await exchanger.get(subject_token, claims["exp"], target)
            headers["authorization"] = f"Bearer {ex.access_token}"
        if response.headers.get("mcp-session-id"):
            await response.aclose()
            raise ExchangeError("stateful Son response cannot be multiplexed")
        if response.status_code != 200:
            await response.aclose()
            raise ExchangeError("Son answered an unsupported MCP response")
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type == "text/event-stream" and stream:
            return response
        if media_type != "application/json":
            await response.aclose()
            raise ExchangeError("Son answered an unsupported MCP response")
        try:
            doc = response.json() if not stream else json.loads(await response.aread())
        except ValueError:
            raise ExchangeError("Son answered invalid JSON") from None
        finally:
            await response.aclose()
        if (not isinstance(doc, dict) or doc.get("jsonrpc") != "2.0" or doc.get("id") != payload["id"]
            or ("result" in doc) == ("error" in doc)):
            raise ExchangeError("Son answered malformed JSON-RPC")
        return doc

    async def listed(target: str, list_method: str) -> list[dict]:
        field, identity = LISTS[list_method]
        items: list[dict] = []
        seen = set()
        cursor = None
        for _ in range(MAX_PAGES):
            p = copy.deepcopy(msg)
            p["method"] = list_method
            p["params"] = {k: v for k, v in params.items() if k == "_meta"}
            if cursor:
                p["params"]["cursor"] = cursor
            doc = await invoke(target, p)
            result = doc.get("result")
            if not isinstance(result, dict) or not isinstance(result.get(field), list):
                raise ExchangeError("Son list result malformed")
            for item in result[field]:
                if not isinstance(item, dict) or not isinstance(item.get(identity), str) or not item[identity]:
                    raise ExchangeError("Son list item malformed")
                if item[identity] in seen:
                    raise ExchangeError("Son returned duplicate identity")
                if field == "tools" and _has_header_annotation(item.get("inputSchema")):
                    raise ExchangeError("Son tool requires unsupported mirrored parameter headers")
                seen.add(item[identity])
                items.append(item)
            cursor = result.get("nextCursor")
            if cursor is None:
                return items
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ExchangeError("Son returned invalid cursor")
        raise ExchangeError("Son pagination limit exceeded")

    async def no_templates(target: str) -> None:
        if await listed(target, "resources/templates/list"):
            raise ExchangeError("Son resource templates cannot be routed safely")

    try:
        if method == "server/discover":
            capabilities: set[str] = set()
            instructions: list[str] = []
            for target in ids:
                doc = await invoke(target, msg)
                result = doc.get("result")
                if not isinstance(result, dict) or result.get("resultType") != "complete":
                    raise ExchangeError("Son discovery is incomplete")
                versions = result.get("supportedVersions")
                if not isinstance(versions, list) or "2026-07-28" not in versions:
                    raise ExchangeError("Son does not support modern MCP")
                offered = result.get("capabilities")
                if (not isinstance(offered, dict) or any(
                    key not in {"tools", "resources", "prompts"} or value != {}
                    for key, value in offered.items()
                )):
                    raise ExchangeError("Son advertises an unsupported MCP capability")
                # A resources capability can also expose templates. A relay must not
                # advertise it while silently hiding those identities.
                if "tools" in offered:
                    await listed(target, "tools/list")
                if "resources" in offered:
                    await no_templates(target)
                    await listed(target, "resources/list")
                if "prompts" in offered:
                    await listed(target, "prompts/list")
                capabilities.update(offered)
                if result.get("instructions"):
                    if not isinstance(result["instructions"], str):
                        raise ExchangeError("Son instructions malformed")
                    instructions.append(f"{target}: {result['instructions']}")
            return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "resultType": "complete", "supportedVersions": ["2026-07-28"],
                "capabilities": {k: {} for k in sorted(capabilities)},
                "_meta": {"io.modelcontextprotocol/serverInfo": {
                    "name": "aiteam-selected-sons", "version": "0.1.0"}},
                "instructions": "\n".join(instructions), "ttlMs": 0, "cacheScope": "private"}})
        if method == "resources/templates/list":
            for target in ids:
                await no_templates(target)
            return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": {"resourceTemplates": []}})
        if method in LISTS:
            if method == "resources/list":
                for target in ids:
                    await no_templates(target)
            field, identity = LISTS[method]
            combined = []
            for target in ids:
                for item in await listed(target, method):
                    wrapped = copy.deepcopy(item)
                    wrapped[identity] = alias(target, item[identity], uri=identity == "uri")
                    wrapped["name"] = f"{target}: {item.get('name', item[identity])}" if identity == "uri" else wrapped["name"]
                    if field == "prompts":
                        wrapped["title"] = target
                    if field == "tools":
                        wrapped["title"] = f"{target}: {item.get('title', item[identity])}"
                    combined.append(wrapped)
            return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": {field: combined}})
        if method in CALLS:
            list_method, identity = CALLS[method]
            decoded = unalias(params.get(identity), uri=identity == "uri")
            if decoded is None or decoded[0] not in ids:
                return failure(400, "unknown or unselected Son-qualified identity")
            target, original = decoded
            items = await listed(target, list_method)
            if not any(item[identity] == original for item in items):
                return failure(404, "identity no longer offered by selected Son")
            payload = copy.deepcopy(msg)
            payload["params"][identity] = original
            response = await invoke(target, payload, stream=True)

            def rewrite(doc: dict) -> dict:
                if method == "resources/read" and isinstance(doc.get("result"), dict):
                    contents = doc["result"].get("contents")
                    if not isinstance(contents, list):
                        raise ExchangeError("Son resource contents malformed")
                    for item in contents:
                        if not isinstance(item, dict) or item.get("uri") != original:
                            raise ExchangeError("Son returned another resource URI")
                        item["uri"] = params["uri"]
                return doc

            if isinstance(response, httpx.Response):
                async def documents():
                    try:
                        async for item in sse.events(response, msg["id"],
                            progress_token=params["_meta"].get("progressToken")):
                            yield sse.encode(rewrite(item))
                    finally:
                        await response.aclose()

                docs = documents()
                try:
                    first = await anext(docs)
                except (ExchangeError, httpx.HTTPError) as e:
                    await docs.aclose()
                    raise ExchangeError("Son SSE stream failed before first event") from None

                async def body():
                    try:
                        yield first
                        async for frame in docs:
                            yield frame
                    except (ExchangeError, httpx.HTTPError):
                        log.warning("selected-Son SSE stream failed after first event")
                        yield sse.encode(sse.error(msg["id"]))
                    finally:
                        await docs.aclose()
                return StreamingResponse(body(), media_type="text/event-stream",
                    headers={"cache-control": "no-cache", "x-accel-buffering": "no"})
            return JSONResponse(rewrite(response))
        # Explicit refusal: neither a reduced tool surface nor a guessed notification target.
        return failure(404, "MCP method cannot be safely selected across Sons")
    except InvalidGrant as e:
        return on_invalid_grant(str(e))
    except ExchangeError as e:
        log.warning("selected-Son request refused: %s", str(e))
        return on_error(502, str(e))
