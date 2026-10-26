"""Stream-proxy one MCP request to the Son's public /mcp with the exchanged token."""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from .exchange import ExchangeError, Exchanger, InvalidGrant

log = logging.getLogger("aiteam_relay.proxy")

FORWARD_REQUEST = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id")
RETURN_RESPONSE = ("content-type", "mcp-session-id", "cache-control")
USER_AGENT = "aiteam-relay/0.1"  # Cloudflare answers 403 to some default library user agents


def upstream_headers(request: Request, son_token: str) -> dict[str, str]:
    """Allowlist only: inbound Authorization, Cookie, X-Aiteam-* and everything else never reach the Son."""
    out = {}
    for name in FORWARD_REQUEST:
        v = request.headers.get(name)
        if v is not None:
            out[name] = v
    out["authorization"] = f"Bearer {son_token}"
    out["user-agent"] = USER_AGENT
    out["accept-encoding"] = "identity"
    return out


class UpstreamUnauthorized(Exception):
    pass


async def send(client: httpx.AsyncClient, request: Request, body: bytes, son_url: str, son_token: str):
    """Open the upstream response as a stream. Raises UpstreamUnauthorized on a 401 (caller re-exchanges once)."""
    req = client.build_request(request.method, son_url, headers=upstream_headers(request, son_token),
                               content=body if request.method not in ("GET", "HEAD") else None)
    resp = await client.send(req, stream=True)
    if resp.status_code == 401:
        await resp.aclose()
        raise UpstreamUnauthorized
    return resp


def to_client(resp: httpx.Response) -> Response:
    headers = {k: resp.headers[k] for k in RETURN_RESPONSE if k in resp.headers}
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        headers.setdefault("cache-control", "no-cache")
        headers["x-accel-buffering"] = "no"

    async def body():
        try:
            if resp.is_stream_consumed:  # body already in memory (in-process transports)
                if resp.content:
                    yield resp.content
                return
            async for chunk in resp.aiter_raw():
                if chunk:
                    yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(body(), status_code=resp.status_code, headers=headers,
                             background=BackgroundTask(resp.aclose))


async def proxy(request: Request, *, client: httpx.AsyncClient, exchanger: Exchanger, subject_token: str,
                subject_exp: float | None, on_invalid_grant, on_error) -> Response:
    body = await request.body()  # MCP POST bodies are small JSON-RPC messages; read once so a retry can replay it
    for attempt in (1, 2):
        try:
            ex = await exchanger.get(subject_token, subject_exp)
        except InvalidGrant as e:
            return on_invalid_grant(str(e))
        except ExchangeError as e:
            return on_error(502, str(e))
        host = urlsplit(ex.son_mcp_url).hostname
        try:
            resp = await send(client, request, body, ex.son_mcp_url, ex.access_token)
        except UpstreamUnauthorized:
            exchanger.drop(subject_token)
            log.info("upstream %s answered 401 (attempt %d)", host, attempt)
            if attempt == 1:
                continue
            return on_error(502, "the Son refused the exchanged token")
        except httpx.HTTPError as e:
            log.warning("upstream %s unreachable: %s", host, type(e).__name__)
            return on_error(502, "the Son is unreachable")
        log.info("%s /mcp -> %s %s", request.method, host, resp.status_code)
        return to_client(resp)
    return on_error(502, "unreachable")  # pragma: no cover
