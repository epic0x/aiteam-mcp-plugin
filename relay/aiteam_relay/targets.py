"""The boxes one grant reaches right now: `GET <issuer>/oauth/targets` with the relay token, cached 30 s per token.

aiteam.space resolves the grant's scope live on every call (a stored rule such as "all" or "project:<id>", the picked
boxes, or the one box of a single-box grant). Every box address must pass the same host check as an exchanged
address (`safe_son_url`); a target that fails it is never called and shows up in `rejected`.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from .exchange import ExchangeError, InvalidGrant, safe_son_url, token_key

log = logging.getLogger("aiteam_relay.targets")

TARGETS_TTL = 30
MAX_TARGETS = 50
CACHE_MAX = 10_000
KINDS = ("son", "node")
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


@dataclass(frozen=True)
class Target:
    kind: str  # son | node
    id: str
    key: str  # what the caller passes as `box`: the slug, or the id when the slug is missing or taken
    name: str
    mcp_url: str
    project_id: str | None = None
    project_name: str | None = None
    calendar: bool = False

    def row(self) -> dict:
        return {"box": self.key, "kind": self.kind, "id": self.id, "name": self.name, "calendar": self.calendar,
                "project_id": self.project_id, "project_name": self.project_name}


@dataclass
class Targets:
    targets: list[Target]
    scope_rule: str | None = None
    project_name: str | None = None
    rejected: list[dict] = field(default_factory=list)  # {box, error}: never called
    truncated: bool = False
    expires_at: float = 0.0

    def find(self, key: str) -> Target | None:
        return next((t for t in self.targets if t.key == key), None)

    @property
    def keys(self) -> list[str]:
        return [t.key for t in self.targets]


def is_calendar(raw: dict, slug: str) -> bool:
    """A content-calendar box: marked by the site (`calendar: true`, kind/template `content-calendar`) or by its
    address slug (`calendar-<name>`, the fleet's naming for calendar boxes)."""
    if raw.get("calendar") is True:
        return True
    if "content-calendar" in (raw.get("template"), raw.get("box_kind"), raw.get("kind")):
        return True
    return slug.startswith("calendar-")


def parse_targets(body: object, *, suffix: str, relay_host: str) -> Targets:
    if not isinstance(body, dict) or not isinstance(body.get("targets"), list):
        raise ExchangeError("targets answer has no targets list")
    out: list[Target] = []
    rejected: list[dict] = []
    seen_keys: set[str] = set()
    seen_ids: set[tuple[str, str]] = set()
    raw_list = body["targets"]
    for raw in raw_list:
        if len(out) >= MAX_TARGETS:
            break
        if not isinstance(raw, dict):
            continue
        tid, kind = raw.get("id"), raw.get("kind", "son")
        if not isinstance(tid, str) or not _ID.match(tid) or kind not in KINDS:
            rejected.append({"box": str(raw.get("slug") or tid or "?")[:64], "error": "malformed target"})
            continue
        slug = raw.get("slug") if isinstance(raw.get("slug"), str) else ""
        url = safe_son_url(raw.get("mcp_url"), suffix=suffix, relay_host=relay_host)
        if url is None:
            log.warning("target %s refused: address outside the allowed hosts", tid)
            rejected.append({"box": (slug or tid)[:64], "error": "address not allowed"})
            continue
        if (kind, tid) in seen_ids:
            continue
        seen_ids.add((kind, tid))
        key = slug if _SLUG.match(slug or "") and slug not in seen_keys and slug != "all" else tid
        if key in seen_keys:
            continue
        seen_keys.add(key)
        name = raw.get("name") if isinstance(raw.get("name"), str) and raw.get("name") else key
        pid, pname = raw.get("project_id"), raw.get("project_name")
        out.append(Target(kind="node" if kind == "node" else "son", id=tid, key=key, name=name[:120], mcp_url=url,
                          project_id=pid if isinstance(pid, str) else None,
                          project_name=pname if isinstance(pname, str) else None,
                          calendar=is_calendar(raw, slug or "")))
    rule, pname = body.get("scope_rule"), body.get("project_name")
    return Targets(out, scope_rule=rule if isinstance(rule, str) else None,
                   project_name=pname if isinstance(pname, str) else None, rejected=rejected,
                   truncated=len(raw_list) > MAX_TARGETS)


class TargetsClient:
    def __init__(self, client: httpx.AsyncClient, *, url: str, suffix: str, relay_host: str,
                 clock: Callable[[], float] = time.time, ttl: float = TARGETS_TTL) -> None:
        self.client, self.url, self.suffix, self.relay_host = client, url, suffix, relay_host
        self.clock, self.ttl = clock, ttl
        self.cache: dict[str, Targets] = {}
        self.calls = 0

    def drop(self, subject_token: str) -> None:
        self.cache.pop(token_key(subject_token), None)

    async def get(self, subject_token: str, subject_exp: float | None = None) -> Targets:
        now = self.clock()
        key = token_key(subject_token)
        hit = self.cache.get(key)
        if hit and hit.expires_at > now:
            return hit
        got = await self._fetch(subject_token)
        got.expires_at = now + self.ttl
        if subject_exp is not None:
            got.expires_at = min(got.expires_at, float(subject_exp))
        if got.expires_at > now:
            for k in [k for k, v in self.cache.items() if v.expires_at <= now]:
                del self.cache[k]
            while len(self.cache) >= CACHE_MAX:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = got
        return got

    async def _fetch(self, subject_token: str) -> Targets:
        self.calls += 1
        try:
            r = await self.client.get(self.url, headers={"Accept": "application/json",
                                                         "Authorization": f"Bearer {subject_token}"}, timeout=5)
        except httpx.HTTPError as e:
            raise ExchangeError(f"targets endpoint unreachable ({type(e).__name__})") from None
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code != 200:
            err = body.get("error") if isinstance(body, dict) else None
            log.info("targets refused: status=%s error=%s", r.status_code, err)
            if r.status_code in (400, 401, 403) and err in ("invalid_grant", "invalid_token"):
                raise InvalidGrant(str(body.get("error_description") or err))
            if r.status_code == 404:
                raise TargetsMissing("targets endpoint not found")
            raise ExchangeError(f"targets endpoint answered {r.status_code}")
        return parse_targets(body, suffix=self.suffix, relay_host=self.relay_host)


class TargetsMissing(ExchangeError):
    """aiteam.space has no targets endpoint yet (older deploy): single-box grants fall back to the plain exchange."""
