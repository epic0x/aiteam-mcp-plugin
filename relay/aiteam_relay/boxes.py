"""MCP client side of the multi-box relay: one short session per call to a box's own /mcp, with an exchanged token.

Never sends the caller's relay token, cookies or any inbound header to a box: the only credential a box sees is the
box-audience token from the exchange. A 401 from a box drops only that box's cached token and retries once.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable

import httpx
import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from . import __version__
from .exchange import ExchangeError, Exchanger, InvalidGrant, InvalidTarget, token_key
from .targets import Target

log = logging.getLogger("aiteam_relay.boxes")

TOOLS_TTL = 60
BOX_TIMEOUT = 5.0  # tools/list per box and every fan-out leg
CALL_TIMEOUT = 900.0  # one routed tool call (a box may run a long job)
FANOUT_CONCURRENCY = 8
CACHE_MAX = 5_000


class BoxError(Exception):
    """A box could not be reached or answered something unusable. The message is safe to show the caller."""


class BoxUnauthorized(BoxError):
    pass


@dataclass
class _Tools:
    tools: list[types.Tool]
    expires_at: float


def _walk(e: BaseException):
    yield e
    if isinstance(e, BaseExceptionGroup):
        for sub in e.exceptions:
            yield from _walk(sub)
    if e.__cause__ is not None and e.__cause__ is not e:
        yield from _walk(e.__cause__)


class BoxPool:
    def __init__(self, exchanger: Exchanger, *, transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.time, box_timeout: float = BOX_TIMEOUT,
                 call_timeout: float = CALL_TIMEOUT, tools_ttl: float = TOOLS_TTL,
                 concurrency: int = FANOUT_CONCURRENCY) -> None:
        self.exchanger = exchanger
        self.transport = transport
        self.clock = clock
        self.box_timeout = box_timeout
        self.call_timeout = call_timeout
        self.tools_ttl = tools_ttl
        self.concurrency = concurrency
        self.tools_cache: dict[str, _Tools] = {}

    # ------------------------------------------------------------------ token
    async def _token(self, subject: str, subject_exp: float | None, t: Target, legacy: bool) -> str:
        """Exchange for this box. `legacy` = a single-box grant: today's exchange form, no `target`."""
        try:
            ex = await self.exchanger.get(subject, subject_exp, target=None if legacy else t.id)
        except InvalidTarget:
            raise BoxError("box not in this connection") from None
        except InvalidGrant:
            raise BoxError("this connection was revoked or expired: sign in again") from None
        except ExchangeError as e:
            raise BoxError(f"could not get access to the box ({e})") from None
        if ex.son_mcp_url != t.mcp_url:
            self.exchanger.drop(subject, None if legacy else t.id)
            log.warning("exchange for %s returned another address; refused", t.key)
            raise BoxError("the box address does not match this connection")
        return ex.access_token

    # ------------------------------------------------------------------ one session
    async def _with_session(self, t: Target, token: str, fn: Callable[[ClientSession], Awaitable[Any]],
                            timeout: float) -> Any:
        unauthorized = False

        async def hook(resp: httpx.Response) -> None:
            nonlocal unauthorized
            if resp.status_code == 401:
                unauthorized = True

        headers = {"authorization": f"Bearer {token}", "user-agent": f"aiteam-relay/{__version__}"}
        http = httpx.AsyncClient(transport=self.transport, headers=headers, follow_redirects=False,
                                 timeout=httpx.Timeout(10, read=timeout), event_hooks={"response": [hook]})
        try:
            async with asyncio.timeout(timeout):
                async with http:
                    async with streamable_http_client(t.mcp_url, http_client=http) as (r, w, _):
                        async with ClientSession(r, w) as s:
                            await s.initialize()
                            return await fn(s)
        except TimeoutError:
            raise BoxError(f"no answer within {timeout:g} s") from None
        except BoxError:
            raise
        except McpError as e:  # a JSON-RPC error from the box (unknown tool, bad arguments): its own words
            raise BoxError(f"the box answered: {str(e.error.message)[:300]}") from None
        except BaseException as e:  # the SDK reports HTTP errors as exception groups from its task group
            task = asyncio.current_task()
            if isinstance(e, (KeyboardInterrupt, SystemExit)) or (task is not None and task.cancelling()):
                raise  # the caller went away or an outer timeout fired: never swallow a real cancellation
            if unauthorized or any(isinstance(x, httpx.HTTPStatusError) and x.response.status_code == 401
                                   for x in _walk(e)):
                raise BoxUnauthorized("the box refused the exchanged token") from None
            kinds = sorted({type(x).__name__ for x in _walk(e) if not isinstance(x, BaseExceptionGroup)})
            log.info("box %s failed: %s", t.key, ",".join(kinds))
            raise BoxError("the box is unreachable or answered an error") from None

    async def run(self, subject: str, subject_exp: float | None, t: Target, legacy: bool,
                  fn: Callable[[ClientSession], Awaitable[Any]], timeout: float | None = None) -> Any:
        timeout = self.call_timeout if timeout is None else timeout
        for attempt in (1, 2):
            token = await self._token(subject, subject_exp, t, legacy)
            try:
                return await self._with_session(t, token, fn, timeout)
            except BoxUnauthorized:
                self.exchanger.drop(subject, None if legacy else t.id)
                log.info("box %s answered 401 (attempt %d)", t.key, attempt)
                if attempt == 2:
                    raise
        raise BoxError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ tools
    def _tools_key(self, subject: str, t: Target) -> str:
        return f"{token_key(subject)}|{t.kind}:{t.id}"

    async def tools(self, subject: str, subject_exp: float | None, t: Target, legacy: bool) -> list[types.Tool]:
        key = self._tools_key(subject, t)
        now = self.clock()
        hit = self.tools_cache.get(key)
        if hit and hit.expires_at > now:
            return hit.tools

        async def fetch(s: ClientSession) -> list[types.Tool]:
            out: list[types.Tool] = []
            cursor = None
            for _ in range(20):  # a box with more than 20 pages of tools is cut off
                res = await s.list_tools(cursor=cursor) if cursor else await s.list_tools()
                out.extend(res.tools)
                cursor = res.nextCursor
                if not cursor:
                    break
            return out

        tools = await self.run(subject, subject_exp, t, legacy, fetch, timeout=self.box_timeout)
        for k in [k for k, v in self.tools_cache.items() if v.expires_at <= now]:
            del self.tools_cache[k]
        while len(self.tools_cache) >= CACHE_MAX:
            self.tools_cache.pop(next(iter(self.tools_cache)))
        self.tools_cache[key] = _Tools(tools, now + self.tools_ttl)
        return tools

    def drop_tools(self, subject: str, t: Target) -> None:
        self.tools_cache.pop(self._tools_key(subject, t), None)

    async def call(self, subject: str, subject_exp: float | None, t: Target, legacy: bool, name: str,
                   args: dict, *, timeout: float | None = None,
                   progress: Callable[[float, float | None, str | None], Awaitable[None]] | None = None
                   ) -> types.CallToolResult:
        async def go(s: ClientSession) -> types.CallToolResult:
            return await raw_call(s, name, args, progress, timeout=timeout or self.call_timeout)
        return await self.run(subject, subject_exp, t, legacy, go, timeout=timeout)

    async def fan_out(self, targets: list[Target], fn: Callable[[Target], Awaitable[Any]]) -> list[tuple[Target, Any]]:
        """Run fn per box, at most `concurrency` at once, `box_timeout` each. Result per box: value or BoxError."""
        sem = asyncio.Semaphore(self.concurrency)

        async def one(t: Target):
            async with sem:
                try:
                    async with asyncio.timeout(self.box_timeout):
                        return t, await fn(t)
                except TimeoutError:
                    return t, BoxError(f"no answer within {self.box_timeout:g} s")
                except BoxError as e:
                    return t, e
                except Exception as e:  # never let one box break the others
                    log.info("box %s fan-out failed: %s", t.key, type(e).__name__)
                    return t, BoxError("the box answered an error")

        return list(await asyncio.gather(*(one(t) for t in targets)))


async def raw_call(s: ClientSession, name: str, args: dict, progress=None, *,
                   timeout: float = CALL_TIMEOUT) -> types.CallToolResult:
    """tools/call without the client-side output-schema check: the relay hands the box's result back unchanged."""
    return await s.send_request(
        types.ClientRequest(types.CallToolRequest(params=types.CallToolRequestParams(name=name, arguments=args))),
        types.CallToolResult, request_read_timeout_seconds=timedelta(seconds=timeout), progress_callback=progress)
