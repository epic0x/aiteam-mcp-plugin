"""Reference patch for the Son OAuth gate (:8701): accept aiteam.space access tokens.

NOT APPLIED TO ANY BOX. Spec: docs/AUTH-DESIGN.md section 2. Drop verify_bearer() and prm() into the gate's request
handler; keep the old approval-code path behind LEGACY_APPROVAL_CODE=on.

Deps: Python 3.9+, `cryptography` (ES256 verify). No network on the hot path except a JWKS refresh (<= 1 / 60 s).
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

ISSUER = os.environ.get("AITEAM_ISSUER", "https://aiteam.space").rstrip("/")
JWKS_URL = os.environ.get("AITEAM_JWKS_URL", ISSUER + "/oauth/jwks")
SON_RESOURCE = os.environ.get("SON_RESOURCE", "")  # e.g. https://my-son.aiteam.space/mcp (canonical, no trailing slash)
SON_ID = os.environ.get("SON_ID", "")  # aiteam.sons.id of this Son
AITEAM_AUTH = os.environ.get("AITEAM_AUTH", "on") == "on"
LEGACY = os.environ.get("LEGACY_APPROVAL_CODE", "off") == "on"
SCOPES = ("son:read", "son:write")
# Advertised in the protected-resource metadata. GitHub has NO separate scope any more (owner 2026-10-12): the connector
# runs the Son's own `gh` login, so any token that opens /mcp also gets the GitHub tools. Tokens minted earlier with
# "son:read son:write github:read" still verify (a son:* scope is all that is checked), so nobody re-consents.
PRM_SCOPES = SCOPES


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class _Jwks:
    def __init__(self) -> None:
        self.keys: dict[str, ec.EllipticCurvePublicKey] = {}
        self.fetched = 0.0
        self.lock = threading.Lock()

    def _load(self) -> None:
        req = urllib.request.Request(JWKS_URL, headers={"User-Agent": "aiteam-son-gate/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 (fixed https URL); default Python UA gets 403 from Cloudflare
            doc = json.load(r)
        keys = {}
        for k in doc.get("keys", []):
            if k.get("kty") == "EC" and k.get("crv") == "P-256" and k.get("kid"):
                x = int.from_bytes(_b64d(k["x"]), "big")
                y = int.from_bytes(_b64d(k["y"]), "big")
                keys[k["kid"]] = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
        self.keys, self.fetched = keys, time.time()

    def get(self, kid: str):
        with self.lock:
            stale = time.time() - self.fetched > 300
            if (kid not in self.keys or stale) and time.time() - self.fetched > 60:
                try:
                    self._load()
                except Exception:  # keep the cached keys if aiteam.space is unreachable
                    pass
            return self.keys.get(kid)


JWKS = _Jwks()


def verify_aiteam_jwt(token: str, *, now: float | None = None, jwks=None) -> dict | None:
    """Return the claims when the token is a valid aiteam.space token for THIS Son, else None."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        head = json.loads(_b64d(parts[0]))
        claims = json.loads(_b64d(parts[1]))
        sig = _b64d(parts[2])
    except Exception:
        return None
    if head.get("alg") != "ES256" or len(sig) != 64:
        return None
    key = (jwks or JWKS).get(head.get("kid", ""))
    if key is None:
        return None
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    try:
        key.verify(der, f"{parts[0]}.{parts[1]}".encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return None
    t = time.time() if now is None else now
    if claims.get("iss") != ISSUER or claims.get("aud") != SON_RESOURCE or claims.get("son_id") != SON_ID:
        return None
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= t - 30:
        return None
    if not any(s in str(claims.get("scope", "")).split() for s in SCOPES):
        return None
    return claims


def verify_bearer(auth_header: str | None, legacy_check=None) -> tuple[bool, dict | None]:
    """Gate hook. legacy_check(token) -> bool is the existing approval-code token check (used only if LEGACY)."""
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return False, None
    token = auth_header[7:].strip()
    if AITEAM_AUTH:
        c = verify_aiteam_jwt(token)
        if c:
            return True, c
    if LEGACY and legacy_check is not None and legacy_check(token):
        return True, None
    return False, None


def forward_headers(claims: dict | None) -> dict:
    """Headers the gate adds when it proxies a verified call upstream (after stripping every inbound X-Aiteam-*).

    X-Aiteam-Sub = the verified caller's email (JWT ``sub``). The connector's add-on gate (connector
    entitlements.py) reads it ONLY for the owner exemption (AITEAM_GATE_EXEMPT). It is never trusted from the
    client: the gate strips any inbound X-Aiteam-* first, so only a verified token can set it.
    """
    sub = str((claims or {}).get("sub") or "").strip().lower()
    return {"X-Aiteam-Sub": sub} if "@" in sub else {}


# Per-add-on entitlement (owner 2026-10-14, docs/ENTITLEMENTS.md): the gate never refuses a connection for a missing
# add-on. Connecting, voice included, is the free base. Paid tools are decided per call inside the connector, which
# returns the upsell as the tool result.


def unauthorized_header(host: str) -> str:
    return f'Bearer resource_metadata="https://{host}/.well-known/oauth-protected-resource/mcp", error="invalid_token"'


def prm() -> dict:
    """GET /.well-known/oauth-protected-resource[/mcp]. Claude only uses the FIRST authorization server."""
    return {
        "resource": SON_RESOURCE,
        "authorization_servers": [ISSUER],
        "scopes_supported": list(PRM_SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://aiteam.space/help/connect",
    }

# Wiring notes for the existing gate:
#  - on success: proxy upstream WITHOUT the Authorization header, strip any inbound X-Aiteam-* header, then add
#    forward_headers(claims) (X-Aiteam-Sub, for the connector's owner exemption). Nothing
#    caller-specific is forwarded: GitHub runs on the Son's own `gh` login (connector github_gh.py), not a per-caller
#    token, so the old X-Aiteam-Token hand-off is gone.
#  - on failure: 401 + WWW-Authenticate: unauthorized_header(host).
#  - when LEGACY is off: stop serving the gate's own /.well-known/oauth-authorization-server, /authorize, /token, /register.
