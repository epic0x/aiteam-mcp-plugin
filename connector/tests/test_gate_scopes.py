"""Gate patch (patches/gate-aiteam-jwt.py): GitHub has no separate scope any more, and tokens minted before the change
(scope "son:read son:write github:read") still open /mcp, so existing connections need no re-consent."""
import base64
import importlib.util
import json
import os
import pathlib
import time

import pytest

ec = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ec")
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature  # noqa: E402

RES, SON = "https://my-son.aiteam.space/mcp", "s1"


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setenv("SON_RESOURCE", RES)
    monkeypatch.setenv("SON_ID", SON)
    path = pathlib.Path(__file__).resolve().parents[2] / "patches" / "gate-aiteam-jwt.py"
    spec = importlib.util.spec_from_file_location("gate_aiteam_jwt", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def mint(key, scope: str) -> str:
    head = _b64(json.dumps({"alg": "ES256", "kid": "k1"}).encode())
    body = _b64(json.dumps({"iss": "https://aiteam.space", "aud": RES, "son_id": SON, "scope": scope,
                            "exp": time.time() + 600}).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
    return f"{head}.{body}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


class Keys:
    def __init__(self, pub):
        self.pub = pub

    def get(self, kid):
        return self.pub if kid == "k1" else None


def test_old_and_new_tokens_both_open_mcp(gate):
    key = ec.generate_private_key(ec.SECP256R1())
    jw = Keys(key.public_key())
    old = mint(key, "son:read son:write github:read offline_access")  # minted before 2026-10-12
    new = mint(key, "son:read son:write offline_access")
    assert gate.verify_aiteam_jwt(old, jwks=jw)["scope"].startswith("son:read")
    assert gate.verify_aiteam_jwt(new, jwks=jw)
    assert gate.verify_aiteam_jwt(mint(key, "github:read"), jwks=jw) is None, "github alone never opened /mcp"


def test_no_separate_github_scope_advertised(gate):
    assert gate.prm()["scopes_supported"] == ["son:read", "son:write"]


def test_gate_forwards_verified_caller_for_owner_exemption(gate):
    assert gate.forward_headers({"sub": "Owner@Example.com"}) == {"X-Aiteam-Sub": "owner@example.com"}
    assert gate.forward_headers({"sub": ""}) == {} and gate.forward_headers(None) == {}
