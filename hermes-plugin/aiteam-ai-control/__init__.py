"""aiteam-ai-control: loopback AI-control API inside the Hermes gateway process.

Why in-process: the gateway keeps per-session /model overrides and the agent cache in memory,
so only code running in the gateway can switch one session (or all) without a restart.

Serves ``127.0.0.1:${AITEAM_AI_CONTROL_PORT:-8651}`` (gateway process only), every route needs
``Authorization: Bearer <API_SERVER_KEY>``. Contract: hotfix-signin/CONTRACT.md (Layer 1).

  GET  /ai/status                 providers signed in, global model, fallback, per-session model
  POST /ai/model                  {provider, model, scope: all|session, session_key?|session_id?}
  PUT  /ai/fallback               {chain: [{provider, model}, ...]}
  POST /ai/signin/start           {provider: claude|chatgpt|anthropic-key|openai-key, label?}
  GET  /ai/signin/<id>            sign-in progress
  POST /ai/credential             {ticket, secret}  (reached only via the Son gate from aiteam.space)

Secrets rule: tokens, keys, codes, PKCE verifiers and tickets are never logged and never
returned (the ticket is returned exactly once, by /ai/signin/start, to build the one-time link).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

logger = logging.getLogger("aiteam-ai-control")

DEFAULT_PORT = 8651
SIGNIN_TTL_S = 15 * 60
MAX_SIGNINS = 20
MAX_ATTEMPTS = 3
MAX_BODY = 16 * 1024
_MODEL_LOCK = threading.RLock()

# provider (tool enum) -> (Hermes pool provider, kind)
SIGNIN_KINDS: Dict[str, Tuple[str, str]] = {
    "claude": ("anthropic", "claude_oauth"),
    "chatgpt": ("openai-codex", "codex_device"),
    "anthropic-key": ("anthropic", "api_key"),
    "openai-key": ("openai-api", "api_key"),
}
POOL_PROVIDERS = ("anthropic", "openai-codex", "openai-api")
NAMES = {"anthropic": "Claude (Anthropic)", "openai-codex": "ChatGPT (OpenAI Codex)",
         "openai-api": "OpenAI API"}


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------------------------
# Small helpers (no secrets leave these)
# --------------------------------------------------------------------------------------------

def _hermes_home() -> Path:
    try:
        from gateway.run import _gateway_config_home  # active profile home in the gateway
        return Path(_gateway_config_home())
    except Exception:
        pass
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _config_path() -> Path:
    return _hermes_home() / "config.yaml"


def _load_config() -> dict:
    try:
        from gateway.run import _load_gateway_config
        cfg = _load_gateway_config(_config_path())
        if isinstance(cfg, dict):
            return cfg
    except Exception:
        pass
    try:
        import yaml
        return yaml.safe_load(_config_path().read_text()) or {}
    except Exception:
        return {}


def _api_key() -> str:
    """The Son's API_SERVER_KEY (env first, then $HERMES_HOME/.env). Never logged."""
    name = os.environ.get("AITEAM_AI_CONTROL_KEY_ENV", "API_SERVER_KEY")
    val = os.environ.get(name, "").strip()
    if val:
        return val
    try:
        for line in (_hermes_home() / ".env").read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _runner():
    try:
        from gateway.run import _gateway_runner_ref
        return _gateway_runner_ref()
    except Exception:
        return None


def _model_cfg(cfg: dict) -> Tuple[str, str]:
    m = cfg.get("model")
    if isinstance(m, str):
        return "", m
    if isinstance(m, dict):
        return str(m.get("provider") or ""), str(m.get("default") or m.get("model") or "")
    return "", ""


def _clean_err(exc: Any, default: str) -> str:
    """Plain one-line error; never a provider body (they can echo request data)."""
    # Provider exceptions are untrusted and may echo arbitrary credentials, without a known prefix.
    # Never try to redact an unknown error format: use caller-supplied static copy instead.
    return default


# --------------------------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------------------------

def _pool_summary(provider: str) -> List[dict]:
    out: List[dict] = []
    try:
        from agent.credential_pool import load_pool
        for e in load_pool(provider).entries():
            out.append({"id": e.id, "label": e.label, "auth_type": e.auth_type,
                        "status": e.last_status or "ok"})
    except Exception:
        logger.debug("aiteam-ai-control: pool read failed for %s", provider, exc_info=True)
    return out


def _authenticated(cfg: dict) -> List[dict]:
    try:
        from hermes_cli.model_switch_providers import list_authenticated_providers
        cur_p, cur_m = _model_cfg(cfg)
        return list_authenticated_providers(
            current_provider=cur_p, current_model=cur_m,
            user_providers=cfg.get("providers") or {}, custom_providers=cfg.get("custom_providers") or [],
            probe_custom_providers=False, non_blocking_catalogs=True, max_models=12)
    except Exception:
        logger.debug("aiteam-ai-control: provider listing failed", exc_info=True)
        return []


API_SOURCES = ("api_server", "hermes_browser", "browser", "desktop", "dashboard")


def _state_db(runner):
    return getattr(getattr(runner, "session_store", None), "_db", None)


def _api_sessions(runner) -> List[dict]:
    db = _state_db(runner)
    if db is None:
        return []
    out = []
    while True:
        page = db.list_sessions_rich(sources=list(API_SOURCES), limit=200, offset=len(out))
        out.extend(page)
        if len(page) < 200:
            return out


def _api_lock(row: dict) -> dict:
    raw = row.get("model_config") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    lock = raw.get("browser_model_lock") if isinstance(raw, dict) else None
    return lock if isinstance(lock, dict) and lock.get("confirmed") else {}


def status() -> dict:
    cfg = _load_config()
    g_provider, g_model = _model_cfg(cfg)
    fb = cfg.get("fallback_providers") or []
    fallback = [{"provider": str(x.get("provider") or ""), "model": str(x.get("model") or "")}
                for x in fb if isinstance(x, dict)]
    providers: Dict[str, dict] = {}
    for p in _authenticated(cfg):
        slug = str(p.get("slug") or "")
        if not slug:
            continue
        providers[slug] = {"provider": slug, "name": str(p.get("name") or slug), "signed_in": True,
                           "models": [str(m.get("id") or m.get("model") or "") if isinstance(m, dict) else str(m)
                                      for m in (p.get("models") or [])][:12], "credentials": []}
    for slug in POOL_PROVIDERS:
        creds = _pool_summary(slug)
        row = providers.setdefault(slug, {"provider": slug, "name": NAMES.get(slug, slug),
                                          "signed_in": bool(creds), "models": [], "credentials": []})
        row["credentials"] = creds
        row["signed_in"] = row["signed_in"] or bool(creds)
    sessions: List[dict] = []
    runner = _runner()
    if runner is not None:
        try:
            live = getattr(runner, "_session_model_overrides", {}) or {}
            for e in runner.session_store.list_sessions():
                api_db = _state_db(runner)
                api_row = api_db.get_session(e.session_id) if api_db is not None else None
                api_pin = _api_lock(api_row) if api_row and api_row.get("source") in API_SOURCES else {}
                ov = live.get(e.session_key) or e.model_override or api_pin or _channel_route(runner, e)
                plat = getattr(e.platform, "value", e.platform)
                sessions.append({
                    "session_key": e.session_key, "session_id": e.session_id,
                    "platform": str(plat or ""), "title": e.display_name or "",
                    "provider": str(ov.get("provider") or g_provider),
                    "model": str(ov.get("model") or g_model), "override": bool(ov),
                    "updated_at": e.updated_at.isoformat() if getattr(e, "updated_at", None) else ""})
        except Exception:
            logger.debug("aiteam-ai-control: session listing failed", exc_info=True)
    seen = {x["session_id"] for x in sessions}
    for row in _api_sessions(runner):
        if row["id"] in seen:
            continue
        lock = _api_lock(row)
        sessions.append({"session_key": "", "session_id": row["id"],
                         "platform": row.get("source") or "api_server", "title": row.get("title") or "",
                         "provider": lock.get("provider") or g_provider,
                         "model": lock.get("model") or g_model, "override": bool(lock)})
        seen.add(row["id"])
    return {"global": {"provider": g_provider, "model": g_model}, "fallback": fallback,
            "apply_timing": "next turn; running jobs are not interrupted",
            "providers": sorted(providers.values(), key=lambda r: (not r["signed_in"], r["provider"])),
            "sessions": sessions, "gateway": runner is not None}


# --------------------------------------------------------------------------------------------
# Model switch
# --------------------------------------------------------------------------------------------

def _switch(provider: str, model: str, *, is_global: bool):
    from hermes_cli.model_switch import switch_model
    cfg = _load_config()
    cur_p, cur_m = _model_cfg(cfg)
    result = switch_model(
        raw_input=model, current_provider=cur_p, current_model=cur_m, is_global=is_global,
        explicit_provider=provider, user_providers=cfg.get("providers") or {},
        custom_providers=cfg.get("custom_providers") or [])
    if not result.success:
        raise HttpError(409 if "credential" in (result.error_message or "").lower()
                        or "not signed" in (result.error_message or "").lower()
                        or "api key" in (result.error_message or "").lower() else 400,
                        _clean_err(result.error_message, "That provider or model is not available on this Son."))
    return result


def _find_session(runner, session_key: str, session_id: str):
    store = runner.session_store
    if session_key:
        e = store.lookup_by_session_key(session_key)
        if e is not None and (not session_id or e.session_id == session_id):
            return e
        raise HttpError(404, "No session matches that key and id on this Son.")
    if session_id:
        e = store.lookup_by_session_id(session_id)
        if e is not None:
            return e
        db = _state_db(runner)
        row = db.get_session(session_id) if db is not None else None
        if row and row.get("source") in API_SOURCES and not row.get("archived"):
            from types import SimpleNamespace
            return SimpleNamespace(session_id=session_id, session_key="", api_only=True)
    raise HttpError(404, "No session with that key or id on this Son.")


def _clear_channel_models(runner) -> None:
    """All means all: channel routing must not silently keep an older model/provider.

    Keep each channel's prompt and unrelated settings; clear just the two route fields in the
    durable user config and the already-parsed in-memory gateway config.
    """
    from utils import atomic_roundtrip_yaml_update
    cfg = _load_config()
    platforms = cfg.get("platforms") or {}
    if isinstance(platforms, dict):
        for name, pc in platforms.items():
            if not isinstance(pc, dict) or not isinstance(pc.get("channel_overrides"), dict):
                continue
            overrides = pc["channel_overrides"]
            clean = {cid: {k: v for k, v in row.items() if k not in ("model", "provider")}
                     if isinstance(row, dict) else row for cid, row in overrides.items()}
            if clean != overrides:
                atomic_roundtrip_yaml_update(_config_path(), f"platforms.{name}.channel_overrides", clean)
    parsed = getattr(getattr(runner, "config", None), "platforms", {}) if runner is not None else {}
    for pc in parsed.values():
        for ov in (getattr(pc, "channel_overrides", {}) or {}).values():
            ov.model = None
            ov.provider = None


def _channel_route(runner, entry) -> dict:
    origin = getattr(entry, "origin", None)
    if origin is None:
        return {}
    try:
        ov = runner._channel_override_for(origin)
        return {"model": ov.model, "provider": ov.provider} if ov else {}
    except (AttributeError, TypeError):
        return {}


def set_model(body: dict) -> dict:
    provider = str(body.get("provider") or "").strip()
    model = str(body.get("model") or "").strip()
    scope = str(body.get("scope") or "all").strip()
    if not provider or not model or len(provider) > 80 or len(model) > 200:
        raise HttpError(400, "provider and model are required.")
    if scope not in ("all", "session"):
        raise HttpError(400, "scope must be all or session.")
    runner = _runner()
    if scope == "session":
        if runner is None:
            raise HttpError(503, "The gateway is not running, so sessions cannot be switched.")
        entry = _find_session(runner, str(body.get("session_key") or ""), str(body.get("session_id") or ""))
        result = _switch(provider, model, is_global=False)
        override = {
            "model": result.new_model, "provider": result.target_provider, "api_key": result.api_key,
            "base_url": result.base_url, "api_mode": result.api_mode,
            "request_overrides": dict(result.request_overrides or {}),
            "capabilities": dict(result.runtime_capabilities or {}),
        }
        # Persist first, then publish; do not report a durable switch if its write failed.
        db = _state_db(runner)
        if db is not None:
            row = db.get_session(entry.session_id)
            # Hermes update_session_model intentionally clears browser_model_lock. Write that
            # metadata first, then install the confirmed API lock, never the other way around.
            db.update_session_model(entry.session_id, result.new_model, provider=result.target_provider,
                                    base_url=result.base_url, api_mode=result.api_mode)
            if row and row.get("source") in API_SOURCES:
                db.update_session_runtime_lock(entry.session_id, model=result.new_model,
                    provider=result.target_provider, route_source="raw_request", confirmed=True)
        if entry.session_key:
            runner.session_store.set_model_override(entry.session_key, override)  # sanitizes non-secret keys only
            runner._session_model_overrides[entry.session_key] = override
            getattr(runner, "_pending_one_turn_model_restores", {}).pop(entry.session_key, None)
        # Never evict/close an in-flight agent. The model/provider credential signature is checked
        # by Hermes before cache reuse, so the next turn rebuilds naturally without interrupting this one.
        return {"ok": True, "scope": "session", "session_key": entry.session_key,
                "provider": result.target_provider, "model": result.new_model, "sessions_changed": 1}
    result = _switch(provider, model, is_global=True)
    from hermes_cli.model_switch import persist_model_selection
    persist_model_selection(result, _config_path())
    _clear_channel_models(runner)
    changed = 0
    if runner is not None:
        overrides = getattr(runner, "_session_model_overrides", {})
        getattr(runner, "_pending_one_turn_model_restores", {}).clear()
        keys = set(overrides.keys())
        try:
            keys |= {e.session_key for e in runner.session_store.list_sessions() if e.model_override}
        except Exception:
            pass
        for key in keys:
            runner.session_store.set_model_override(key, None)
            overrides.pop(key, None)
        # A changed global route changes the cache signature on the next turn. Let running turns
        # finish using their existing clients; do not interrupt customer jobs to apply configuration.
        changed_ids = {e.session_id for e in runner.session_store.list_sessions()}
        db = _state_db(runner)
        for row in _api_sessions(runner):
            if _api_lock(row):
                db.update_session_runtime_lock(row["id"], model=result.new_model,
                    provider=result.target_provider, route_source="raw_request", confirmed=False)
            db.update_session_model(row["id"], result.new_model, provider=result.target_provider,
                                    base_url=result.base_url, api_mode=result.api_mode)
            changed_ids.add(row["id"])
        changed = len(changed_ids)
    return {"ok": True, "scope": "all", "provider": result.target_provider, "model": result.new_model,
            "sessions_changed": changed}


def set_fallback(body: dict) -> dict:
    chain = body.get("chain")
    if not isinstance(chain, list) or len(chain) > 6:
        raise HttpError(400, "chain must be a list of up to 6 {provider, model}.")
    cfg = _load_config()
    signed = {str(p.get("slug")) for p in _authenticated(cfg)}
    signed |= {p for p in POOL_PROVIDERS if _pool_summary(p)}
    clean: List[dict] = []
    for item in chain:
        if not isinstance(item, dict):
            raise HttpError(400, "Each fallback entry needs provider and model.")
        p, m = str(item.get("provider") or "").strip(), str(item.get("model") or "").strip()
        if not p or not m or len(p) > 80 or len(m) > 200:
            raise HttpError(400, "Each fallback entry needs provider and model.")
        if p not in signed:
            raise HttpError(409, f"{p} is not signed in on this Son.")
        clean.append({"provider": p, "model": m})
    from utils import atomic_roundtrip_yaml_update
    atomic_roundtrip_yaml_update(_config_path(), "fallback_providers", clean or None)
    return {"ok": True, "fallback": clean}


# --------------------------------------------------------------------------------------------
# Sign-in
# --------------------------------------------------------------------------------------------

class SignIns:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: Dict[str, dict] = {}

    def _gc(self) -> None:
        now = time.time()
        for sid, rec in list(self._by_id.items()):
            if rec["expires"] < now - 3600:
                self._by_id.pop(sid, None)
            elif rec["expires"] < now and rec["state"] == "waiting":
                rec["state"] = "expired"
                rec.pop("verifier", None)
        if len(self._by_id) > MAX_SIGNINS:
            for sid in sorted(self._by_id, key=lambda s: self._by_id[s]["created"])[:-MAX_SIGNINS]:
                self._by_id.pop(sid, None)

    def ensure_capacity(self) -> None:
        with self._lock:
            self._gc()
            if sum(r["state"] == "waiting" for r in self._by_id.values()) >= MAX_SIGNINS:
                raise HttpError(429, "Too many sign-ins are waiting. Finish one or wait for it to expire.")

    def add(self, rec: dict) -> Tuple[str, str]:
        ticket = secrets.token_urlsafe(32)
        sid = "si_" + uuid.uuid4().hex[:16]
        rec.update(id=sid, ticket_hash=_sha(ticket), created=time.time(),
                   expires=time.time() + SIGNIN_TTL_S, state="waiting", attempts=0)
        with self._lock:
            self._gc()
            self._by_id[sid] = rec
        return sid, ticket

    def get(self, sid: str) -> Optional[dict]:
        with self._lock:
            self._gc()
            return self._by_id.get(sid)

    def claim(self, ticket: str) -> dict:
        h = _sha(ticket)
        with self._lock:
            self._gc()
            for rec in self._by_id.values():
                if hmac.compare_digest(rec["ticket_hash"], h):
                    if rec["state"] != "waiting" or rec.get("used") or rec["expires"] < time.time():
                        raise HttpError(410, "This link expired or was already used. Ask for a new one.")
                    if rec.get("busy"):
                        raise HttpError(409, "This sign-in is already being processed.")
                    rec["busy"] = True
                    return rec
        raise HttpError(404, "This link expired or was already used. Ask for a new one.")

    def release(self, rec: dict, *, ok: bool) -> None:
        with self._lock:
            rec["busy"] = False
            if ok:
                rec["used"] = True
                rec["state"] = "connected"
                rec.pop("verifier", None)
                rec.pop("oauth_state", None)
            else:
                rec["attempts"] += 1
                if rec["attempts"] >= MAX_ATTEMPTS:
                    rec["used"] = True
                    rec["state"] = "failed"
                    rec["error"] = "Too many tries. Ask for a new sign-in link."
                    rec.pop("verifier", None)


SIGNINS = SignIns()


def _add_pool_entry(pool_provider: str, *, token: str, auth_type: str, source: str, label: str,
                    **fields: Any) -> str:
    from agent.credential_pool import PooledCredential, label_from_token, load_pool
    pool = load_pool(pool_provider)
    label = (label or "").strip()[:60] or label_from_token(
        token, f"{pool_provider}-{auth_type.replace('_', '-')}-{len(pool.entries()) + 1}")
    entry = PooledCredential(provider=pool_provider, id=uuid.uuid4().hex[:6], label=label,
                             auth_type=auth_type, priority=0, source=source, access_token=token,
                             **{k: v for k, v in fields.items() if v is not None})
    entry = pool.add_entry(entry)
    return entry.label


def _provider_base_url(provider: str) -> str:
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
        p = PROVIDER_REGISTRY.get(provider)
        return p.inference_base_url if p else ""
    except Exception:
        return ""


def _codex_worker(rec: dict, device: dict) -> None:
    """Background: wait for the ChatGPT device sign-in, then store the tokens in the pool."""
    try:
        from hermes_cli.auth_codex import (
            CODEX_OAUTH_CLIENT_ID, _codex_base_url, _codex_exchange_authorization_code,
            _codex_poll_authorization_code)
        from agent.credential_pool import AUTH_TYPE_OAUTH, SOURCE_MANUAL_DEVICE_CODE
        issuer = "https://auth.openai.com"
        code_resp = _codex_poll_authorization_code(
            issuer, device_auth_id=device["device_auth_id"], user_code=device["user_code"],
            poll_interval=int(device.get("interval") or 5))
        if rec.get("state") != "waiting" or rec.get("expires", 0) < time.time():
            rec.update(state="expired", used=True)
            return
        tokens = _codex_exchange_authorization_code(issuer, CODEX_OAUTH_CLIENT_ID, code_resp)
        try:
            from hermes_cli.auth import _utc_now_z
            last_refresh = _utc_now_z()
        except Exception:
            last_refresh = None
        label = _add_pool_entry("openai-codex", token=tokens["access_token"], auth_type=AUTH_TYPE_OAUTH,
                                source=SOURCE_MANUAL_DEVICE_CODE, label=rec.get("label", ""),
                                refresh_token=tokens.get("refresh_token"), base_url=_codex_base_url(),
                                last_refresh=last_refresh)
        try:
            from hermes_cli.auth import mark_provider_active_if_unset
            mark_provider_active_if_unset("openai-codex")
        except Exception:
            pass
        rec.update(state="connected", label=label, used=True)
        logger.info("aiteam-ai-control: ChatGPT sign-in %s connected", rec["id"])
    except BaseException as exc:  # SystemExit from the CLI helpers must not kill the thread silently
        rec.update(state="expired" if "timed out" in str(exc).lower() else "failed",
                   error=_clean_err(exc, "ChatGPT sign-in did not finish. Start a new one."), used=True)
        logger.info("aiteam-ai-control: ChatGPT sign-in %s ended: %s", rec["id"], rec["state"])


def signin_start(body: dict, *, codex_starter: Optional[Callable[[dict, dict], None]] = None) -> dict:
    provider = str(body.get("provider") or "").strip()
    if provider not in SIGNIN_KINDS:
        raise HttpError(400, "provider must be claude, chatgpt, anthropic-key or openai-key.")
    SIGNINS.ensure_capacity()
    pool_provider, kind = SIGNIN_KINDS[provider]
    label = str(body.get("label") or "").strip()[:60]
    rec: dict = {"provider": provider, "pool_provider": pool_provider, "kind": kind, "label": label}
    extra: dict = {}
    if kind == "claude_oauth":
        from agent.anthropic_credentials import (
            _OAUTH_CLIENT_ID, _OAUTH_REDIRECT_URI, _OAUTH_SCOPES, _generate_pkce)
        verifier, challenge = _generate_pkce()
        oauth_state = secrets.token_urlsafe(32)
        rec.update(verifier=verifier, oauth_state=oauth_state)
        params = {"code": "true", "client_id": _OAUTH_CLIENT_ID, "response_type": "code",
                  "redirect_uri": _OAUTH_REDIRECT_URI, "scope": _OAUTH_SCOPES,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": oauth_state}
        extra = {"provider_url": f"https://claude.ai/oauth/authorize?{urlencode(params)}", "needs_code": True}
    elif kind == "codex_device":
        try:
            from hermes_cli.auth_codex import CODEX_OAUTH_CLIENT_ID, _codex_request_device_code
            device = _codex_request_device_code("https://auth.openai.com", CODEX_OAUTH_CLIENT_ID)
        except BaseException as exc:
            raise HttpError(502, _clean_err(exc, "OpenAI did not start the sign-in. Try again in a minute."))
        extra = {"provider_url": "https://auth.openai.com/codex/device",
                 "user_code": str(device.get("user_code") or ""), "needs_code": False}
        rec["device"] = True
    else:
        extra = {"needs_code": True}
    sid, ticket = SIGNINS.add(rec)
    if kind == "codex_device":
        starter = codex_starter or (lambda r, d: threading.Thread(
            target=_codex_worker, args=(r, d), name=f"aiteam-codex-{sid}", daemon=True).start())
        starter(rec, device)
    logger.info("aiteam-ai-control: sign-in %s started for %s", sid, provider)
    return {"signin_id": sid, "provider": provider, "expires_at": int(rec["expires"]),
            "ticket": ticket, **extra}


def signin_status(sid: str) -> dict:
    rec = SIGNINS.get(sid)
    if rec is None:
        raise HttpError(404, "No sign-in with that id (it may have expired).")
    out = {"signin_id": sid, "provider": rec["provider"], "state": rec["state"]}
    if rec.get("label") and rec["state"] == "connected":
        out["label"] = rec["label"]
    if rec.get("error"):
        out["error"] = rec["error"]
    return out


def _exchange_claude(rec: dict, secret: str) -> Tuple[str, str]:
    from agent.anthropic_credentials import (
        _OAUTH_CLIENT_ID, _OAUTH_REDIRECT_URI, _oauth_token_state, _post_oauth_token)
    from agent.credential_pool import AUTH_TYPE_OAUTH, SOURCE_MANUAL
    raw = secret.strip()
    code, _, received_state = raw.partition("#")
    if not code or len(code) > 1000:
        raise HttpError(400, "That does not look like a Claude code. Copy the whole code Claude showed.")
    if not received_state or not hmac.compare_digest(received_state, rec.get("oauth_state", "")):
        raise HttpError(400, "That code is from a different sign-in. Use the link from the same message.")
    data = json.dumps({"grant_type": "authorization_code", "client_id": _OAUTH_CLIENT_ID, "code": code,
                       "state": rec.get("oauth_state", ""), "redirect_uri": _OAUTH_REDIRECT_URI,
                       "code_verifier": rec.get("verifier", "")}).encode()
    try:
        result = _post_oauth_token(data, content_type="application/json", timeout=15, what="exchange")
    except Exception:
        raise HttpError(400, "Claude did not accept that code. It may be expired: sign in again and paste the new code.")
    creds = _oauth_token_state(result)
    if not creds.get("access_token"):
        raise HttpError(400, "Claude did not return a sign-in. Try again.")
    label = _add_pool_entry("anthropic", token=creds["access_token"], auth_type=AUTH_TYPE_OAUTH,
                            source=f"{SOURCE_MANUAL}:hermes_pkce", label=rec.get("label", ""),
                            refresh_token=creds.get("refresh_token"), expires_at_ms=creds.get("expires_at_ms"),
                            base_url=_provider_base_url("anthropic"))
    return "anthropic", label


def _store_key(rec: dict, secret: str) -> Tuple[str, str]:
    from agent.credential_pool import AUTH_TYPE_API_KEY, SOURCE_MANUAL
    key = secret.strip()
    if len(key) < 20 or len(key) > 400 or any(c.isspace() for c in key):
        raise HttpError(400, "That does not look like an API key.")
    pool_provider = rec["pool_provider"]
    if pool_provider == "anthropic" and not key.startswith("sk-ant-"):
        raise HttpError(400, "Anthropic API keys start with sk-ant-.")
    if pool_provider == "openai-api" and not key.startswith("sk-"):
        raise HttpError(400, "OpenAI API keys start with sk-.")
    label = rec.get("label") or f"{pool_provider}-key"
    label = _add_pool_entry(pool_provider, token=key, auth_type=AUTH_TYPE_API_KEY, source=SOURCE_MANUAL,
                            label=label, base_url=_provider_base_url(pool_provider))
    return pool_provider, label


def credential(body: dict) -> dict:
    ticket = body.get("ticket")
    secret = body.get("secret")
    if not isinstance(ticket, str) or not isinstance(secret, str) or not ticket or not secret:
        raise HttpError(400, "ticket and secret are required.")
    if len(ticket) > 128 or len(secret) > 4000:
        raise HttpError(400, "ticket or secret is too long.")
    rec = SIGNINS.claim(ticket)
    ok = False
    try:
        if rec["kind"] == "codex_device":
            raise HttpError(409, "ChatGPT needs no code here. Finish the sign-in on the OpenAI page.")
        if rec["kind"] == "claude_oauth":
            provider, label = _exchange_claude(rec, secret)
        else:
            provider, label = _store_key(rec, secret)
        ok = True
        rec["label"] = label
        logger.info("aiteam-ai-control: sign-in %s connected (%s)", rec["id"], provider)
        return {"ok": True, "provider": provider, "label": label, "state": "connected"}
    finally:
        SIGNINS.release(rec, ok=ok)


# --------------------------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------------------------

def _route(method: str, path: str, body: dict) -> dict:
    if method == "GET" and path == "/ai/status":
        return status()
    if method == "POST" and path == "/ai/model":
        with _MODEL_LOCK:
            return set_model(body)
    if method == "PUT" and path == "/ai/fallback":
        with _MODEL_LOCK:
            return set_fallback(body)
    if method == "POST" and path == "/ai/signin/start":
        return signin_start(body)
    if method == "GET" and path.startswith("/ai/signin/"):
        return signin_status(path.rsplit("/", 1)[1])
    if method == "POST" and path == "/ai/credential":
        return credential(body)
    raise HttpError(404, "Not found.")


class Handler(BaseHTTPRequestHandler):
    server_version = "aiteam-ai-control"
    sys_version = ""

    def log_message(self, *args: Any) -> None:  # never log request lines or bodies
        return

    def _send(self, status_code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method: str) -> None:
        self.connection.settimeout(30)
        try:
            key = _api_key()
            if not key:
                raise HttpError(503, "AI control is not configured on this Son.")
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:].strip(), key):
                raise HttpError(401, "Unauthorized.")
            if self.headers.get("Transfer-Encoding"):
                raise HttpError(400, "Content-Length is required; transfer encoding is not supported.")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise HttpError(400, "Invalid Content-Length.")
            if length < 0:
                raise HttpError(400, "Invalid Content-Length.")
            if length > MAX_BODY:
                raise HttpError(413, "Request too large.")
            body: dict = {}
            if length:
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    raise HttpError(400, "Body must be JSON.")
                if not isinstance(body, dict):
                    raise HttpError(400, "Body must be a JSON object.")
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            self._send(200, _route(method, path, body))
        except HttpError as exc:
            self._send(exc.status, {"ok": False, "error": exc.message})
        except Exception as exc:
            logger.warning("aiteam-ai-control: request failed (%s)", type(exc).__name__)
            self._send(500, {"ok": False, "error": "AI control failed on this Son. Try again."})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")


_SERVER: Optional[ThreadingHTTPServer] = None


def start_server(port: Optional[int] = None, host: str = "127.0.0.1") -> Optional[ThreadingHTTPServer]:
    global _SERVER
    if _SERVER is not None:
        return _SERVER
    port = int(port if port is not None else os.environ.get("AITEAM_AI_CONTROL_PORT") or DEFAULT_PORT)
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        logger.warning("aiteam-ai-control: cannot bind %s:%s (%s); AI control API is off", host, port, exc)
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="aiteam-ai-control", daemon=True).start()
    _SERVER = srv
    logger.info("aiteam-ai-control: listening on %s:%s", host, srv.server_address[1])
    return srv


def stop_server() -> None:
    global _SERVER
    if _SERVER is not None:
        try:
            _SERVER.shutdown()
            _SERVER.server_close()
        finally:
            _SERVER = None


def _is_gateway_process() -> bool:
    if os.environ.get("AITEAM_AI_CONTROL_FORCE") == "1":
        return True
    argv = [a.lower() for a in sys.argv]
    return "gateway" in argv and "run" in argv


def register(ctx) -> None:
    if not _is_gateway_process():
        return  # CLI chats, cron runners and sub-processes must not grab the port
    start_server()
    try:
        ctx.on_unload(stop_server)
    except Exception:
        pass
