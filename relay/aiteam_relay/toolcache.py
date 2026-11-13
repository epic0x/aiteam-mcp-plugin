"""Per-process cache of each box's tool list for the multi-box relay.

Keyed by the box address and target id plus a hash of the account and granted scope, so one account's list is never
served to another. Holds tool definitions only: no tokens, no tool results, no box answers.

Fresh for `ttl`, then stale-while-revalidate: the cached list is served at once and one background load refreshes
it. A load that fails keeps the last good list (marked stale) instead of dropping the box, and a box that failed is
not tried again for `retry_after`. One load per key at a time: callers join the running one.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Awaitable, Callable

import mcp.types as types

log = logging.getLogger("aiteam_relay.toolcache")

TTL = 300.0
RETRY_AFTER = 30.0
FORCE_AFTER = 10.0  # a call to a tool the list does not show reloads that box at most this often
KEEP_STALE = 24 * 3600.0
MAX_ENTRIES = 2_000


@dataclass
class Entry:
    tools: list[types.Tool] | None = None  # None = never loaded
    loaded_at: float | None = None
    fresh_until: float = 0.0
    failed_at: float | None = None
    error: str | None = None
    forced_at: float | None = None

    @property
    def stale(self) -> bool:
        """The last load failed after a good one: the old list is still served."""
        return self.tools is not None and self.failed_at is not None and self.failed_at >= (self.loaded_at or 0)


Loader = Callable[[], Awaitable[list[types.Tool]]]


class ToolsCache:
    def __init__(self, *, clock: Callable[[], float] = time.time, ttl: float = TTL, retry_after: float = RETRY_AFTER,
                 keep_stale: float = KEEP_STALE, max_entries: int = MAX_ENTRIES) -> None:
        self.clock, self.ttl, self.retry_after = clock, ttl, retry_after
        self.keep_stale, self.max_entries = keep_stale, max_entries
        self.entries: OrderedDict[str, Entry] = OrderedDict()
        self.inflight: dict[str, asyncio.Task] = {}
        self.loads = 0  # loads started (tests, metrics)

    def peek(self, key: str) -> Entry | None:
        e = self.entries.get(key)
        if e is None:
            return None
        if self.clock() - max(e.loaded_at or 0, e.failed_at or 0) > self.keep_stale:
            del self.entries[key]
            return None
        self.entries.move_to_end(key)
        return e

    def _entry(self, key: str) -> Entry:
        e = self.entries.get(key)
        if e is None:
            while len(self.entries) >= self.max_entries:
                self.entries.popitem(last=False)
            e = self.entries[key] = Entry()
        self.entries.move_to_end(key)
        return e

    def needs_load(self, key: str) -> bool:
        """Not fresh, no load running, and not inside the retry pause after a failed load."""
        if key in self.inflight:
            return False
        e = self.peek(key)
        if e is None:
            return True
        now = self.clock()
        if e.tools is not None and now < e.fresh_until:
            return False
        return e.failed_at is None or now - e.failed_at >= self.retry_after

    def force(self, key: str) -> bool:
        """May a call that names a tool this list does not show reload it now? At most once per FORCE_AFTER per box,
        so a client repeating an unknown name cannot make the relay reload a box on every call."""
        now = self.clock()
        e = self._entry(key)
        if e.forced_at is not None and now - e.forced_at < FORCE_AFTER:
            return False
        e.forced_at = now
        return True

    def start(self, key: str, loader: Loader, label: str) -> asyncio.Task:
        """The running load for `key`, or a new one. The task never raises: it returns the tools or the exception."""
        running = self.inflight.get(key)
        if running is not None:
            return running
        self.loads += 1

        async def go():
            try:
                tools = await loader()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # BoxError messages are safe to show; they never carry a token
                msg = (str(e) or type(e).__name__)[:300]
                ent = self._entry(key)
                ent.failed_at, ent.error = self.clock(), msg
                log.info("tool list of %s not loaded (%s)%s", label, msg[:120],
                         "; keeping the last good list" if ent.tools is not None else "")
                return e
            ent = self._entry(key)
            now = self.clock()
            ent.tools, ent.loaded_at, ent.fresh_until, ent.failed_at, ent.error = tools, now, now + self.ttl, None, None
            return tools

        task = asyncio.get_running_loop().create_task(go())
        self.inflight[key] = task

        def done(t: asyncio.Task, k: str = key) -> None:
            if self.inflight.get(k) is t:
                del self.inflight[k]
        task.add_done_callback(done)
        return task

    async def idle(self) -> None:
        """Wait until no load is running (tests)."""
        while self.inflight:
            await asyncio.gather(*list(self.inflight.values()), return_exceptions=True)

    async def aclose(self) -> None:
        tasks = list(self.inflight.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.inflight.clear()
