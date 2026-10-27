"""Bounded request-scoped modern MCP SSE decoder; no session or resume IDs."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from .exchange import ExchangeError

MAX_EVENT = 1_048_576
MAX_STREAM = 20_971_520


async def events(resp: httpx.Response, request_id: object, *, progress_token: object = None) -> AsyncIterator[dict]:
    """Yield validated notifications and the terminal response; reject unrelated traffic."""
    data: list[str] = []
    size = total = 0
    event = "message"
    final = False
    async for line in resp.aiter_lines():
        total += len(line.encode("utf-8")) + 2
        if total > MAX_STREAM:
            raise ExchangeError("Son SSE stream exceeded the size limit")
        if line.startswith(":"):
            continue
        if line:
            size += len(line.encode("utf-8")) + 2
            if size > MAX_EVENT:
                raise ExchangeError("Son SSE event exceeded the size limit")
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "data":
                data.append(value)
            elif field == "event":
                event = value
            elif field == "id":
                raise ExchangeError("Son sent a resumable SSE ID on a stateless request")
            elif field not in ("retry",):
                raise ExchangeError("Son sent an unknown SSE field")
            continue
        if not data:
            event, size = "message", 0
            continue
        if event != "message":
            raise ExchangeError("Son sent an unsupported SSE event type")
        try:
            doc = json.loads("\n".join(data))
        except ValueError:
            raise ExchangeError("Son sent invalid SSE JSON") from None
        data, event, size = [], "message", 0
        if not isinstance(doc, dict) or doc.get("jsonrpc") != "2.0":
            raise ExchangeError("Son sent malformed SSE JSON-RPC")
        if "id" in doc:
            if doc["id"] != request_id or ("result" in doc) == ("error" in doc):
                raise ExchangeError("Son sent an unrelated SSE response")
            final = True
            yield doc
            break
        if (doc.get("method") not in ("notifications/progress", "notifications/message")
            or not isinstance(doc.get("params"), dict)
            or (doc["method"] == "notifications/progress" and
                doc["params"].get("progressToken") not in (None, progress_token))):
            raise ExchangeError("Son sent an unrelated SSE notification")
        yield doc
    if not final:
        raise ExchangeError("Son SSE stream closed without a final response")


def encode(doc: dict) -> bytes:
    return b"data: " + json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode() + b"\n\n"


def error(request_id: object) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {
        "code": -32603, "message": "Selected Son stream failed"}}
