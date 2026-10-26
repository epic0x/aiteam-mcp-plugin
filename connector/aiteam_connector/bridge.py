"""Paperclip-style HTTP bridge to one Son's Hermes api_server.

Talks to the Son over its REST/SSE API as an external caller with its own token. No Hermes CLI, no
bind mounts, no shared filesystem: the same shape as felipefontoura/paperclip-adapter-hermes-gateway,
but on the session API (``/api/sessions``) instead of chat completions, so every turn lands in a real
Hermes session with history, search and an author label.

Endpoints used (hermes-agent api_server, checked against 0.21.5):
  POST /api/sessions                         create a session (id, title, source=api_server)
  GET  /api/sessions?limit&offset            list sessions
  GET  /api/sessions/{id}                    one session
  GET  /api/sessions/{id}/messages           history (limit, offset, order)
  POST /api/sessions/{id}/chat               one turn, JSON reply
  POST /api/sessions/{id}/chat/stream        one turn, SSE: run.started, assistant.delta, tool.*,
                                             assistant.completed {content}, run.completed, error, done
  GET  /v1/health                            liveness
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import httpx


class BridgeError(RuntimeError):
    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(f"{status} {code} {message}".strip())
        self.status, self.message, self.code = status, message, code


@dataclass
class TurnResult:
    session_id: str
    reply: str
    events: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    status: str = "completed"
    error: str = ""


def parse_sse(lines: list[str]) -> Optional[tuple[str, dict]]:
    """One SSE frame (list of lines without the blank terminator) -> (event, data). Comments are skipped."""
    event, data = "", []
    for ln in lines:
        if not ln or ln.startswith(":"):
            continue
        if ln.startswith("event:"):
            event = ln[6:].strip()
        elif ln.startswith("data:"):
            data.append(ln[5:].lstrip())
    if not event and not data:
        return None
    raw = "\n".join(data)
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        payload = {"raw": raw}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return event or str(payload.get("event") or "message"), payload


TITLE_MAX = 100  # Hermes SessionDB.MAX_TITLE_LENGTH: a longer title makes POST /api/sessions answer 500


def fit_title(title: str, limit: int = TITLE_MAX) -> str:
    """Collapse whitespace and cap at `limit` chars. A trailing " [xxxxxx]" tag (the box's session suffix) is kept so
    titles stay unique; the middle is cut with an ellipsis."""
    t = " ".join(str(title).split())
    if len(t) <= limit:
        return t
    tail = ""
    if t.endswith("]") and " [" in t[-12:]:
        tail = t[t.rindex(" ["):]
    head = t[: limit - len(tail) - 1].rstrip()
    return head + "\u2026" + tail


class HermesBridge:
    """Async client for one Son. ``token`` is the Son's API_SERVER_KEY, held only by the messaging box."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 600.0,
                 transport: Optional[httpx.AsyncBaseTransport] = None, model: Optional[str] = None):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url, transport=transport,
            headers={"Authorization": f"Bearer {token}", "User-Agent": "aiteam-connector/0.1"},
            timeout=httpx.Timeout(timeout, connect=10.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _json(self, method: str, path: str, **kw) -> Any:
        r = await self._client.request(method, path, **kw)
        if r.status_code >= 400:
            raise _error(r.status_code, r.text)
        return r.json()

    async def health(self) -> dict:
        return await self._json("GET", "/v1/health")

    async def create_session(self, session_id: str, title: Optional[str] = None, model: Optional[str] = None) -> dict:
        body: dict[str, Any] = {"id": session_id, "source": "api_server"}
        if title:
            body["title"] = fit_title(title)
        if model:
            body["model"] = model
        return (await self._json("POST", "/api/sessions", json=body)).get("session", {})

    async def get_session(self, session_id: str) -> Optional[dict]:
        try:
            return (await self._json("GET", f"/api/sessions/{session_id}")).get("session")
        except BridgeError as e:
            if e.status == 404:
                return None
            raise

    async def list_sessions(self, limit: int = 20, offset: int = 0, source: Optional[str] = None) -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if source:
            params["source"] = source
        d = await self._json("GET", "/api/sessions", params=params)
        return d.get("data") or d.get("sessions") or []

    async def messages(self, session_id: str, limit: int = 20, offset: int = 0, order: str = "latest") -> list[dict]:
        d = await self._json("GET", f"/api/sessions/{session_id}/messages",
                             params={"limit": limit, "offset": offset, "order": order})
        return d.get("data") or []

    async def chat(self, session_id: str, message: str, author: Optional[dict] = None) -> TurnResult:
        body: dict[str, Any] = {"message": message}
        if author:
            body["author"] = author
        d = await self._json("POST", f"/api/sessions/{session_id}/chat", json=body)
        msg = d.get("message") or {}
        return TurnResult(session_id=session_id, reply=str(msg.get("content") or d.get("reply") or ""),
                          status=str(d.get("status") or "completed"))

    async def chat_stream(self, session_id: str, message: str, author: Optional[dict] = None,
                          on_event: Optional[Callable[[str, dict], Awaitable[None]]] = None) -> TurnResult:
        """Run one turn over SSE. ``on_event`` sees every frame (used for MCP progress + box relays)."""
        body: dict[str, Any] = {"message": message}
        if author:
            body["author"] = author
        res = TurnResult(session_id=session_id, reply="")
        deltas: list[str] = []
        async with self._client.stream("POST", f"/api/sessions/{session_id}/chat/stream", json=body,
                                       headers={"Accept": "text/event-stream"}) as r:
            if r.status_code >= 400:
                raise _error(r.status_code, (await r.aread()).decode(errors="replace"))
            async for name, data in _frames(r.aiter_lines()):
                res.events.append(name)
                if on_event:
                    await on_event(name, data)
                if name == "assistant.delta":
                    deltas.append(str(data.get("delta") or ""))
                elif name == "assistant.completed":
                    res.reply = str(data.get("content") or "")
                elif name in ("tool.started",):
                    res.tools.append(str(data.get("tool_name") or ""))
                elif name == "run.queued":
                    res.status = "queued"
                elif name == "error":
                    res.status, res.error = "failed", str(data.get("message") or data)
                elif name in ("run.failed", "run.cancelled"):
                    res.status = name.split(".", 1)[1]
                    res.error = str(data.get("error") or "")
        if not res.reply:
            res.reply = "".join(deltas)
        return res


async def _frames(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, dict]]:
    buf: list[str] = []
    async for ln in lines:
        if ln == "":
            f = parse_sse(buf)
            buf = []
            if f:
                yield f
        else:
            buf.append(ln)
    if buf:
        f = parse_sse(buf)
        if f:
            yield f


def _error(status: int, text: str) -> BridgeError:
    try:
        d = json.loads(text)
        e = d.get("error", d) if isinstance(d, dict) else {}
        if isinstance(e, dict):
            return BridgeError(status, str(e.get("message") or text)[:300], str(e.get("code") or ""))
    except ValueError:
        pass
    return BridgeError(status, text[:300])
