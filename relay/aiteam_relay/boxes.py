"""MCP client side of the multi-box relay: one short session per call to a box's own /mcp, with an exchanged token.

Never sends the caller's relay token, cookies or any inbound header to a box: the only credential a box sees is the
box-audience token from the exchange. A 401 from a box drops only that box's cached token and retries once.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any, Awaitable, Callable

import httpx
import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from . import __version__
from .exchange import ExchangeError, Exchanger, InvalidGrant, InvalidTarget
from .targets import Target
from .toolcache import TTL as TOOLS_TTL, Entry, ToolsCache

log = logging.getLogger("aiteam_relay.boxes")

BOX_TIMEOUT = 30.0  # loading one box's tool list: exchange + MCP initialize + tools/list (env RELAY_BOX_TIMEOUT)
FANOUT_TIMEOUT = 10.0  # one box in boxes_list or an `all` read (env RELAY_FANOUT_TIMEOUT)
LIST_TIMEOUT = 25.0  # one tools/list answer waits at most this long for boxes not cached yet (env RELAY_LIST_TIMEOUT)
CALL_TIMEOUT = 900.0  # one routed tool call (a box may run a long job)
FANOUT_CONCURRENCY = 4  # boxes asked at once per fan-out (env RELAY_FANOUT_CONCURRENCY)
LOAD_CONCURRENCY = 32  # tool list loads running at once in this process; their token exchanges queue in exchange.py


class BoxError(Exception):
    """A box could not be reached or answered something unusable. The message is safe to show the caller."""


class BoxUnauthorized(BoxError):
    pass


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
                 fanout_timeout: float = FANOUT_TIMEOUT, list_timeout: float = LIST_TIMEOUT,
                 call_timeout: float = CALL_TIMEOUT, tools_ttl: float = TOOLS_TTL,
                 concurrency: int = FANOUT_CONCURRENCY) -> None:
        self.exchanger = exchanger
        self.transport = transport
        self.clock = clock
        self.box_timeout = box_timeout
        self.fanout_timeout = fanout_timeout
        self.list_timeout = list_timeout
        self.call_timeout = call_timeout
        self.concurrency = concurrency
        self.tools_cache = ToolsCache(clock=clock, ttl=tools_ttl)
        self._load_sem: asyncio.Semaphore | None = None

    # ------------------------------------------------------------------ token
    async def _token(self, subject: str, subject_exp: float | None, t: Target, legacy: bool,
                     deadline: float | None = None) -> str:
        """Exchange for this box. `legacy` = a single-box grant: today's exchange form, no `target`. `deadline` (loop
        time) bounds the exchanger's retries; concurrent callers for one box share one exchange (exchange.py)."""
        try:
            ex = await self.exchanger.get(subject, subject_exp, target=None if legacy else t.id, label=t.key,
                                          deadline=deadline)
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
        deadline = asyncio.get_running_loop().time() + timeout
        for attempt in (1, 2):
            token = await self._token(subject, subject_exp, t, legacy, deadline)
            try:
                return await self._with_session(t, token, fn, timeout)
            except BoxUnauthorized:
                self.exchanger.drop(subject, None if legacy else t.id)
                log.info("box %s answered 401 (attempt %d)", t.key, attempt)
                if attempt == 2:
                    raise
        raise BoxError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ tools
    @staticmethod
    def tools_key(cache_id: str, t: Target) -> str:
        """Box address + target + the caller's account/scope hash. Never a token."""
        return f"{t.mcp_url}|{t.kind}:{t.id}|{cache_id}"

    def cached_tools(self, cache_id: str, t: Target) -> Entry | None:
        return self.tools_cache.peek(self.tools_key(cache_id, t))

    def load_tools(self, subject: str, subject_exp: float | None, t: Target, legacy: bool, cache_id: str
                   ) -> asyncio.Task:
        """Start (or join) a background load of one box's tool list, `box_timeout` from its first byte out."""
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

        async def load() -> list[types.Tool]:
            if self._load_sem is None:
                self._load_sem = asyncio.Semaphore(LOAD_CONCURRENCY)
            async with self._load_sem:
                try:  # the budget covers the token exchange too, not only the box
                    async with asyncio.timeout(self.box_timeout):
                        return await self.run(subject, subject_exp, t, legacy, fetch, timeout=self.box_timeout)
                except TimeoutError:
                    raise BoxError(f"no answer within {self.box_timeout:g} s") from None

        return self.tools_cache.start(self.tools_key(cache_id, t), load, t.key)

    def warm(self, subject: str, subject_exp: float | None, targets: list[Target], legacy: bool,
             cache_id: str) -> dict[str, asyncio.Task]:
        """Start background loads for every box whose list is missing or due; returns the running loads by box."""
        out = {}
        for t in targets:
            key = self.tools_key(cache_id, t)
            if self.tools_cache.needs_load(key) or key in self.tools_cache.inflight:
                out[t.key] = self.load_tools(subject, subject_exp, t, legacy, cache_id)
        return out

    async def tools(self, subject: str, subject_exp: float | None, t: Target, legacy: bool, cache_id: str, *,
                    force: bool = False) -> list[types.Tool]:
        """One box's tools: the cached list (stale or not) when there is one, else wait for a load. force = reload
        and wait (a call named a tool the cached list does not show). Raises BoxError when there is no list at all."""
        key = self.tools_key(cache_id, t)
        cache = self.tools_cache
        if force and (key in cache.inflight or cache.force(key)):
            await asyncio.shield(self.load_tools(subject, subject_exp, t, legacy, cache_id))
        hit = cache.peek(key)
        if hit is not None and hit.tools is not None:
            if cache.needs_load(key):
                self.load_tools(subject, subject_exp, t, legacy, cache_id)  # refresh in the background
            return hit.tools
        if not cache.needs_load(key) and key not in cache.inflight:
            raise BoxError(hit.error if hit and hit.error else "the box did not list its tools")
        got = await asyncio.shield(self.load_tools(subject, subject_exp, t, legacy, cache_id))
        if isinstance(got, BaseException):
            raise got if isinstance(got, BoxError) else BoxError("the box did not list its tools")
        return got

    async def call(self, subject: str, subject_exp: float | None, t: Target, legacy: bool, name: str,
                   args: dict, *, timeout: float | None = None,
                   progress: Callable[[float, float | None, str | None], Awaitable[None]] | None = None
                   ) -> types.CallToolResult:
        async def go(s: ClientSession) -> types.CallToolResult:
            return await raw_call(s, name, args, progress, timeout=timeout or self.call_timeout)
        return await self.run(subject, subject_exp, t, legacy, go, timeout=timeout)

    async def fan_out(self, targets: list[Target], fn: Callable[[Target], Awaitable[Any]]) -> list[tuple[Target, Any]]:
        """Run fn per box, at most `concurrency` at once, `fanout_timeout` each. Result per box: value or BoxError."""
        sem = asyncio.Semaphore(self.concurrency)
        limit = self.fanout_timeout

        async def one(t: Target):
            async with sem:
                try:
                    async with asyncio.timeout(limit):
                        return t, await fn(t)
                except TimeoutError:
                    return t, BoxError(f"no answer within {limit:g} s")
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
