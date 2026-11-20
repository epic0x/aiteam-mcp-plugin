"""Extra headers for the relay's own calls to the issuer (RELAY_ISSUER_HEADERS_FILE). Off when unset.

For an issuer behind an access gate that wants a service token on every request, such as a staging site:
the gate's `CF-Access-Client-Id` and `CF-Access-Client-Secret`. The file holds `Name: value` lines, must be readable
by its owner only (mode 600 or stricter) and may name only those two headers. Anything else stops the relay at
start, so a typo never runs with the gate half set up.

The headers go only on requests to the issuer's own origin (scheme, host and port) at its three OAuth paths:
/oauth/token, /oauth/targets, /oauth/jwks. Never to a box, never to any other path or host, and never on a redirect
(the relay follows none). Values are never logged: the log names the headers only.
"""
from __future__ import annotations

import logging
import os
import stat
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

ALLOWED = {"cf-access-client-id": "CF-Access-Client-Id", "cf-access-client-secret": "CF-Access-Client-Secret"}
PATHS = frozenset({"/oauth/token", "/oauth/targets", "/oauth/jwks"})


class IssuerHeadersError(ValueError):
    """The headers file is missing, readable by others, or names a header that is not allowed. Never holds a value."""


def _origin(url: str) -> tuple[str, str, int]:
    u = urlsplit(url)
    scheme = (u.scheme or "").lower()
    return scheme, (u.hostname or "").lower(), u.port or (443 if scheme == "https" else 80)


def load(path: str) -> dict[str, str]:
    """`Name: value` lines from `path`. Blank lines and lines starting with # are skipped."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        raise IssuerHeadersError(f"RELAY_ISSUER_HEADERS_FILE: cannot open ({type(e).__name__})") from None
    with os.fdopen(fd, encoding="utf-8") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise IssuerHeadersError("RELAY_ISSUER_HEADERS_FILE: not a regular file")
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise IssuerHeadersError(f"RELAY_ISSUER_HEADERS_FILE: mode {stat.S_IMODE(st.st_mode):o}; "
                                     "use chmod 600")
        lines = f.read().splitlines()
    out: dict[str, str] = {}
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition(":")
        key, value = name.strip().lower(), value.strip()
        if not sep or key not in ALLOWED:
            raise IssuerHeadersError(f"RELAY_ISSUER_HEADERS_FILE line {n}: only "
                                     + ", ".join(ALLOWED.values()) + " are allowed")
        if not value or any(c in value for c in "\r\n\0"):
            raise IssuerHeadersError(f"RELAY_ISSUER_HEADERS_FILE line {n}: {ALLOWED[key]} has no usable value")
        out[ALLOWED[key]] = value
    if not out:
        raise IssuerHeadersError("RELAY_ISSUER_HEADERS_FILE: no headers in the file")
    return out


def install(client: httpx.AsyncClient, issuer: str, headers: dict[str, str]) -> None:
    """Add `headers` to the client's requests to the issuer's OAuth paths, and to no other request."""
    want = _origin(issuer)

    async def add(request: httpx.Request) -> None:
        u = request.url
        if (u.scheme, (u.host or "").lower(), u.port or (443 if u.scheme == "https" else 80)) == want \
                and u.path in PATHS:
            for k, v in headers.items():
                request.headers[k] = v
        else:  # never let a header of that name through to anything else, whoever set it
            for k in headers:
                request.headers.pop(k, None)

    client.event_hooks["request"] = [*client.event_hooks.get("request", []), add]
    log.info("issuer headers on for %s (%s)", issuer, ", ".join(sorted(headers)))
