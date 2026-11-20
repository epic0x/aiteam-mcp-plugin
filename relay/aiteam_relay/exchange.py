"""RFC 8693 token exchange at aiteam.space: relay token (aud = relay) -> short-lived box token (aud = box /mcp).

The exchanged token and the box URL are cached by sha256(subject token) (+ the target id when one is sent) until the
exchanged token's exp - 30 s.

The token route must never see a burst from this process (docs/RELAY.md, Token exchange):
  * one exchange per cache key at a time: concurrent callers for the same box join the one in flight;
  * at most `concurrency` exchanges in flight per process (RELAY_EXCHANGE_SLOTS, default 2), each POST waiting at
    most RELAY_EXCHANGE_TIMEOUT seconds (default 25) and never past the caller's deadline;
  * 503 and 429 are retried up to RETRIES times with exponential backoff and jitter, honouring Retry-After (capped),
    and while one answer says "busy" every new exchange of the process waits the same pause;
  * 500, 502, 504 and an unreachable endpoint are retried once; every other refusal is final;
  * no retry, and no wait for the limiter, past the latest deadline of the callers waiting for that exchange;
  * a key whose retries ran out on a busy answer is not sent again for COOL_DOWN seconds.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("aiteam_relay.exchange")

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
CACHE_SKEW = 30
CACHE_MAX = 10_000
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

CONCURRENCY = 2  # exchanges in flight per process (env RELAY_EXCHANGE_SLOTS, or RELAY_EXCHANGE_CONCURRENCY)
RETRIES = 3  # retries after a 503 or 429
RETRIES_ERROR = 1  # retries after a 500, 502, 504 or an unreachable endpoint
BACKOFF_BASE = 0.5  # seconds; doubles per retry, capped, then 50 to 100 % of it (jitter)
BACKOFF_MAX = 8.0
RETRY_AFTER_MAX = 10.0
COOL_DOWN = 5.0  # seconds a key is not sent again after its retries ran out on a busy answer
DEFAULT_BUDGET = 30.0  # seconds, for a caller that gives no deadline
REQUEST_TIMEOUT = 25.0  # one POST to the token route (env RELAY_EXCHANGE_TIMEOUT, at most 120); never past the deadline
BUSY = (429, 503)
RETRY_ONCE = (500, 502, 504)


class InvalidGrant(Exception):
    """aiteam.space refused the subject token (expired, revoked, Son gone): the client must refresh or sign in."""


class ExchangeError(Exception):
    """aiteam.space unreachable, or an answer the relay cannot use (bad JSON, Son URL outside the allowed hosts)."""


class InvalidTarget(ExchangeError):
    """aiteam.space refused this one target (not in the grant's scope right now). The grant itself stays live."""


class ExchangeBusy(ExchangeError):
    """aiteam.space answered 503 / 429 and the retries ran out, or this box is in its short pause after that."""


@dataclass(frozen=True)
class Exchanged:
    access_token: str
    son_mcp_url: str
    expires_at: float


def safe_son_url(url: object, *, suffix: str, relay_host: str) -> str | None:
    """SSRF guard: only https://<one label><suffix>/mcp, and never the relay's own host. Returns the URL or None."""
    if not isinstance(url, str) or not url:
        return None
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if p.scheme != "https" or port is not None or p.username or p.password or p.query or p.fragment:
        return None
    if p.path != "/mcp" or p.netloc.lower() != host:
        return None
    suffix = suffix.lower()
    if not suffix.startswith(".") or not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    if not _LABEL.match(label) or host == relay_host.lower() or label == "mcp":
        return None
    return f"https://{host}/mcp"


def token_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def cache_key(subject_token: str, target: str | None = None) -> str:
    return token_key(subject_token) + (f"|{target}" if target else "")


def retry_after(value: str | None, now: float | None = None) -> float | None:
    """Seconds from a Retry-After header (delta seconds or an HTTP date), capped; None when absent or unreadable."""
    if not value:
        return None
    value = value.strip()
    try:
        secs = float(value)
    except ValueError:
        try:
            secs = parsedate_to_datetime(value).timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    if secs != secs:  # NaN
        return None
    return max(0.0, min(secs, RETRY_AFTER_MAX))


def backoff(n: int, rand: Callable[[], float] = random.random) -> float:
    """Wait before retry n (0-based): BACKOFF_BASE * 2^n, capped, then 50 to 100 % of that."""
    return min(BACKOFF_MAX, BACKOFF_BASE * (2 ** n)) * (0.5 + rand() / 2)


class _Retry(Exception):
    def __init__(self, error: ExchangeError, what: str, *, busy: bool, wait: float | None = None) -> None:
        super().__init__(what)
        self.error, self.what, self.busy, self.wait = error, what, busy, wait


class _Flight:
    """One exchange in flight for one cache key; `deadline` is the latest any waiting caller allows (loop time)."""
    __slots__ = ("task", "deadline")

    def __init__(self, deadline: float) -> None:
        self.task: asyncio.Task | None = None
        self.deadline = deadline


class Exchanger:
    def __init__(self, client: httpx.AsyncClient, *, token_url: str, suffix: str, relay_host: str,
                 clock: Callable[[], float] = time.time, concurrency: int = CONCURRENCY,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 request_timeout: float = REQUEST_TIMEOUT) -> None:
        self.client = client
        self.token_url = token_url
        self.suffix = suffix
        self.relay_host = relay_host
        self.clock = clock
        self.concurrency = max(1, int(concurrency))
        self.sleep = sleep
        self.request_timeout = float(request_timeout)
        self.cache: dict[str, Exchanged] = {}
        self.calls = 0  # exchanges sent (tests, metrics)
        self._flights: dict[str, _Flight] = {}
        self._cool: dict[str, float] = {}  # key -> clock time until which it is not sent again
        self._sem: asyncio.Semaphore | None = None
        self._busy_until = 0.0  # loop time: no exchange of this process is sent before it

    def drop(self, subject_token: str, target: str | None = None) -> None:
        """Forget one cached box token: the single-target one, or only `target`'s."""
        self.cache.pop(cache_key(subject_token, target), None)

    def _prune(self, now: float) -> None:
        for k in [k for k, v in self.cache.items() if v.expires_at <= now]:
            del self.cache[k]
        while len(self.cache) >= CACHE_MAX:
            self.cache.pop(next(iter(self.cache)))
        if len(self._cool) > CACHE_MAX:
            for k in [k for k, v in self._cool.items() if v <= now]:
                del self._cool[k]

    async def get(self, subject_token: str, subject_exp: float | None = None, *, target: str | None = None,
                  extra: dict[str, str] | None = None, label: str | None = None,
                  deadline: float | None = None) -> Exchanged:
        """`target` (a son or node id) is sent as `target=` and keys the cache; None = the legacy single-target form.
        `label` names the box in logs (never a token). `deadline` (event loop time) bounds the retries and the wait
        for the limiter; without one the exchange gets DEFAULT_BUDGET seconds."""
        now = self.clock()
        key = cache_key(subject_token, target)
        hit = self.cache.get(key)
        if hit and hit.expires_at > now:
            return hit
        loop = asyncio.get_running_loop()
        dl = deadline if deadline is not None else loop.time() + DEFAULT_BUDGET
        fl = self._flights.get(key)
        if fl is not None:
            fl.deadline = max(fl.deadline, dl)
        else:
            if self._cool.get(key, 0.0) > now:
                raise ExchangeBusy("aiteam.space is busy; try again in a few seconds")
            fl = self._flights[key] = _Flight(dl)
            form = {"target": target} if target else {}
            form.update(extra or {})
            fl.task = loop.create_task(self._fly(key, subject_token, subject_exp, form, label or "box", fl))

            def done(t: asyncio.Task, k: str = key, f: _Flight = fl) -> None:
                if self._flights.get(k) is f:
                    del self._flights[k]
                if not t.cancelled():
                    t.exception()  # retrieved, so a flight whose callers all left logs nothing
            fl.task.add_done_callback(done)
        return await asyncio.shield(fl.task)

    async def _fly(self, key: str, subject_token: str, subject_exp: float | None, form: dict[str, str], label: str,
                   fl: _Flight) -> Exchanged:
        loop = asyncio.get_running_loop()
        tries = {True: 0, False: 0}  # retries so far: after busy answers, after other errors
        pause = True  # wait out a busy pause another flight set (not the one this flight just slept itself)
        while True:
            try:
                ex = await self._attempt(subject_token, form, fl, pause)
            except _Retry as r:
                limit = RETRIES if r.busy else RETRIES_ERROR
                n = tries[r.busy]
                wait = r.wait if r.wait is not None else backoff(n)
                if n >= limit or loop.time() + wait >= fl.deadline:
                    if r.busy:
                        self._cool[key] = self.clock() + max(COOL_DOWN, wait)
                    log.info("token exchange for %s answered %s; not retried (%s)", label, r.what,
                             "retries used up" if n >= limit else "no time left")
                    raise r.error from None
                tries[r.busy] = n + 1
                if r.busy:
                    self._busy_until = max(self._busy_until, loop.time() + wait)
                log.info("token exchange for %s answered %s; retry %d of %d in %.1f s", label, r.what, n + 1, limit,
                         wait)
                await self.sleep(wait)
                pause = not r.busy
                continue
            self._cool.pop(key, None)
            now = self.clock()
            cache_until = ex.expires_at - CACHE_SKEW
            if subject_exp is not None:
                cache_until = min(cache_until, float(subject_exp) - CACHE_SKEW)
            if cache_until > now:
                self._prune(now)
                self.cache[key] = Exchanged(ex.access_token, ex.son_mcp_url, cache_until)
            return ex

    async def _attempt(self, subject_token: str, extra: dict[str, str], fl: _Flight, pause: bool) -> Exchanged:
        """One POST to the token route, inside the process-wide limiter and after any busy pause."""
        loop = asyncio.get_running_loop()
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.concurrency)
        wait = self._busy_until - loop.time() if pause else 0.0
        if wait > 0:
            if loop.time() + wait >= fl.deadline:
                raise ExchangeBusy("aiteam.space is busy; try again in a few seconds")
            await self.sleep(wait)
        async with self._sem:
            left = fl.deadline - loop.time()
            if left <= 0:
                raise ExchangeError("token endpoint: no time left for this exchange")
            return await self._exchange(subject_token, self.clock(), extra, timeout=min(self.request_timeout, left))

    async def _exchange(self, subject_token: str, now: float, extra: dict[str, str] | None = None, *,
                        timeout: float = REQUEST_TIMEOUT) -> Exchanged:
        self.calls += 1
        form = {"grant_type": GRANT_TYPE, "subject_token": subject_token, "subject_token_type": ACCESS_TOKEN_TYPE}
        form.update(extra or {})
        try:
            r = await self.client.post(self.token_url, data=form, headers={"Accept": "application/json"},
                                       timeout=timeout)
        except httpx.TransportError as e:
            raise _Retry(ExchangeError(f"token endpoint unreachable ({type(e).__name__})"), type(e).__name__,
                         busy=False) from None
        except httpx.HTTPError as e:
            raise ExchangeError(f"token endpoint unreachable ({type(e).__name__})") from None
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code != 200:
            err = body.get("error") if isinstance(body, dict) else None
            what = (f"{r.status_code} {err}" if isinstance(err, str) else str(r.status_code))[:80]
            if r.status_code in BUSY:
                raise _Retry(ExchangeBusy("aiteam.space is busy; try again in a few seconds"), what, busy=True,
                             wait=retry_after(r.headers.get("retry-after")))
            if r.status_code in RETRY_ONCE:
                raise _Retry(ExchangeError(f"token endpoint answered {r.status_code}"), what, busy=False)
            log.info("token exchange refused: status=%s error=%s", r.status_code, err)
            if r.status_code in (400, 401) and err in ("invalid_grant", "invalid_token"):
                raise InvalidGrant(str(body.get("error_description") or err))
            if err == "invalid_target" and extra and "target" in extra:
                raise InvalidTarget(str(body.get("error_description") or err))
            raise ExchangeError(f"token endpoint answered {r.status_code}")
        if not isinstance(body, dict):
            raise ExchangeError("token endpoint answered non-JSON")
        tok, ttype = body.get("access_token"), str(body.get("token_type", ""))
        if not isinstance(tok, str) or not tok or ttype.lower() != "bearer":
            raise ExchangeError("token endpoint answer has no bearer access_token")
        url = safe_son_url(body.get("son_mcp_url"), suffix=self.suffix, relay_host=self.relay_host)
        if url is None:
            log.warning("token exchange returned a Son URL outside the allowed hosts; refused")
            raise ExchangeError("son_mcp_url outside the allowed hosts")
        try:
            ttl = float(body.get("expires_in", 300))
        except (TypeError, ValueError):
            ttl = 300.0
        ttl = max(0.0, min(ttl, 300.0))
        return Exchanged(tok, url, now + ttl)

    async def aclose(self) -> None:
        tasks = [f.task for f in self._flights.values() if f.task is not None]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._flights.clear()
