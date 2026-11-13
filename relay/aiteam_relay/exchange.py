"""RFC 8693 token exchange at aiteam.space: relay token (aud = relay) -> short-lived box token (aud = box /mcp).

The exchanged token and the box URL are cached by sha256(subject token) (+ the target id when one is sent) until the
exchanged token's exp - 30 s.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("aiteam_relay.exchange")

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
CACHE_SKEW = 30
CACHE_MAX = 10_000
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class InvalidGrant(Exception):
    """aiteam.space refused the subject token (expired, revoked, Son gone): the client must refresh or sign in."""


class ExchangeError(Exception):
    """aiteam.space unreachable, or an answer the relay cannot use (bad JSON, Son URL outside the allowed hosts)."""


class InvalidTarget(ExchangeError):
    """aiteam.space refused this one target (not in the grant's scope right now). The grant itself stays live."""


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


class Exchanger:
    def __init__(self, client: httpx.AsyncClient, *, token_url: str, suffix: str, relay_host: str,
                 clock: Callable[[], float] = time.time) -> None:
        self.client = client
        self.token_url = token_url
        self.suffix = suffix
        self.relay_host = relay_host
        self.clock = clock
        self.cache: dict[str, Exchanged] = {}
        self.calls = 0  # exchanges sent (tests, metrics)

    def drop(self, subject_token: str, target: str | None = None) -> None:
        """Forget one cached box token: the single-target one, or only `target`'s."""
        self.cache.pop(cache_key(subject_token, target), None)

    def _prune(self, now: float) -> None:
        for k in [k for k, v in self.cache.items() if v.expires_at <= now]:
            del self.cache[k]
        while len(self.cache) >= CACHE_MAX:
            self.cache.pop(next(iter(self.cache)))

    async def get(self, subject_token: str, subject_exp: float | None = None, *, target: str | None = None,
                  extra: dict[str, str] | None = None) -> Exchanged:
        """`target` (a son or node id) is sent as `target=` and keys the cache; None = the legacy single-target form."""
        now = self.clock()
        key = cache_key(subject_token, target)
        hit = self.cache.get(key)
        if hit and hit.expires_at > now:
            return hit
        form = {"target": target} if target else {}
        form.update(extra or {})
        ex = await self._exchange(subject_token, now, form)
        cache_until = ex.expires_at - CACHE_SKEW
        if subject_exp is not None:
            cache_until = min(cache_until, float(subject_exp) - CACHE_SKEW)
        if cache_until > now:
            self._prune(now)
            self.cache[key] = Exchanged(ex.access_token, ex.son_mcp_url, cache_until)
        return ex

    async def _exchange(self, subject_token: str, now: float, extra: dict[str, str] | None = None) -> Exchanged:
        self.calls += 1
        form = {"grant_type": GRANT_TYPE, "subject_token": subject_token, "subject_token_type": ACCESS_TOKEN_TYPE}
        form.update(extra or {})
        try:
            r = await self.client.post(self.token_url, data=form, headers={"Accept": "application/json"}, timeout=10)
        except httpx.HTTPError as e:
            raise ExchangeError(f"token endpoint unreachable ({type(e).__name__})") from None
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code != 200:
            err = body.get("error") if isinstance(body, dict) else None
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
