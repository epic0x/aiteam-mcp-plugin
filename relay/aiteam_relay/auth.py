"""Verify aiteam.space ES256 access tokens minted for the relay resource (aud = RELAY_RESOURCE).

Same rules and code style as patches/gate-aiteam-jwt.py, but async (JWKS fetched with the relay's httpx client).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

log = logging.getLogger("aiteam_relay.auth")

SCOPES = ("son:read", "son:write")
LEEWAY = 30  # seconds of clock skew accepted on exp


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def parse_jwk_set(doc: dict) -> dict[str, ec.EllipticCurvePublicKey]:
    keys = {}
    for k in doc.get("keys", []):
        if k.get("kty") == "EC" and k.get("crv") == "P-256" and k.get("kid"):
            x = int.from_bytes(_b64d(k["x"]), "big")
            y = int.from_bytes(_b64d(k["y"]), "big")
            keys[k["kid"]] = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    return keys


class Jwks:
    """aiteam.space JWKS cache: refetch when stale (5 min) or on an unknown kid, at most once per 60 s."""

    def __init__(self, url: str, client: httpx.AsyncClient | None = None) -> None:
        self.url = url
        self.client = client
        self.keys: dict[str, ec.EllipticCurvePublicKey] = {}
        self.fetched = 0.0
        self.lock = asyncio.Lock()

    async def _load(self) -> None:
        assert self.client is not None
        r = await self.client.get(self.url, headers={"Accept": "application/json"}, timeout=5)
        r.raise_for_status()
        self.keys, self.fetched = parse_jwk_set(r.json()), time.time()

    async def get(self, kid: str):
        async with self.lock:
            stale = time.time() - self.fetched > 300
            if (kid not in self.keys or stale) and time.time() - self.fetched > 60 and self.client is not None:
                try:
                    await self._load()
                except Exception as e:  # keep the cached keys if aiteam.space is unreachable
                    log.warning("jwks refresh failed: %s", type(e).__name__)
            return self.keys.get(kid)


async def verify_relay_jwt(token: str, *, jwks, issuer: str, resource: str, now: float | None = None) -> dict | None:
    """Return the claims when `token` is a valid aiteam.space token for the relay resource, else None."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        head = json.loads(_b64d(parts[0]))
        claims = json.loads(_b64d(parts[1]))
        sig = _b64d(parts[2])
    except Exception:
        return None
    if not isinstance(head, dict) or not isinstance(claims, dict):
        return None
    if head.get("alg") != "ES256" or len(sig) != 64:
        return None
    key = await jwks.get(str(head.get("kid", "")))
    if key is None:
        return None
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    try:
        key.verify(der, f"{parts[0]}.{parts[1]}".encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return None
    t = time.time() if now is None else now
    if claims.get("iss") != issuer or claims.get("aud") != resource:
        return None
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= t - LEEWAY:
        return None
    if not any(s in str(claims.get("scope", "")).split() for s in SCOPES):
        return None
    return claims
