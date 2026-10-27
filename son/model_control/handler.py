"""Model control for a Son's Hermes gateway (gateway hook ``aiteam-model-control``).

Why: over MCP, ``session_status`` cannot change a session's model ("a gateway action"). The live per-session
model override, the cached agents and the global ``model:`` / ``fallback_providers`` config all belong to the
running gateway process, so the change has to happen inside it. This hook runs there.

How: on ``gateway:startup`` it opens a local-only unix socket ``<HERMES_HOME>/mcp/model-control.sock`` (mode 0600,
same uid as the connector; nothing on TCP). The connector (aiteam_connector/model_control.py) sends ONE JSON line,
reads ONE JSON line back. Verbs:

  status        {session_key?, session_id?, limit?}  -> global route, fallback chain, configured providers (names and
                                                        model lists only), per-session override + effective order
  set_global    {provider, model, clear_session_overrides=true}
                -> config.yaml model.provider/model.default (same targeted write as `/model --global`); with
                   clear_session_overrides every session's /model override is dropped so ALL sessions use it
  set_session   {session_key|session_id, provider, model} or {..., clear: true}
                -> the same persisted override `/model` writes (model/provider only, never credentials); the
                   session's cached agent is evicted when idle so the next turn builds on the new route
  set_fallback  {fallback: [{provider, model}, ...], primary?: {provider, model}}
                -> config.yaml fallback_providers (the gateway re-reads it every turn)

Rules: provider/model must already be declared on this Son (config ``providers:`` / ``custom_providers:``,
configured model lists/defaults, the current primary or a current fallback entry). Unknown models are refused. No key, token or base URL is ever returned. A running turn is never interrupted: it finishes on its old
route, the next turn uses the new one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("aiteam.model_control")

SOCKET_NAME = "model-control.sock"
MAX_REQUEST = 64 * 1024
MAX_FALLBACKS = 8
STATUS_DEFAULT, STATUS_MAX = 50, 200
_ID_RX = re.compile(r"^[A-Za-z0-9._:/@+\-\[\]]{1,200}$")
_SECRET_KEYS = {"api_key", "token", "secret", "password", "key_cmd", "key_env", "api_key_env", "auth",
                "authorization", "headers", "access_token", "refresh_token", "credentials"}


class ControlError(Exception):
    pass


# --------------------------------------------------------------------------------------------- config I/O
class HermesConfig:
    """Reads/writes the Son's config.yaml through Hermes' own helpers (targeted, comment-preserving writes)."""

    def __init__(self, home: Path):
        self.path = Path(home) / "config.yaml"

    def load(self) -> dict:
        from hermes_cli.config_effective import load_user_config_effective
        return load_user_config_effective(self.path, fail_closed=True) or {}

    def _write(self, primary: Optional[dict] = None, chain: Optional[list] = None) -> None:
        from hermes_cli.config import _CONFIG_LOCK, managed_scope
        from hermes_cli.model_switch import ModelSwitchResult, model_selection_config_updates
        from utils import _roundtrip_load, _roundtrip_dump
        from ruamel.yaml.comments import CommentedMap
        touched = (["model.default", "model.provider", "model.base_url", "model.api_mode", "model.api_key",
                    "model.key_env", "model.api_key_env", "model.context_length"] if primary is not None else [])
        if chain is not None:
            touched += ["fallback_providers", "fallback_model"]
        if any(managed_scope.is_key_managed(key) for key in touched):
            raise ControlError("the requested model configuration is managed by the administrator; nothing changed")
        # One atomic replace for the whole route: a turn never sees new model + old provider (or vice versa).
        # The gateway's /model lock serializes this with its own /model commands.
        with _CONFIG_LOCK:
            yaml_rt, cfg = _roundtrip_load(self.path)
            if primary is not None:
                result = ModelSwitchResult(success=True, new_model=primary["model"], target_provider=primary["provider"])
                old = cfg.get("model")
                updates = model_selection_config_updates(result, old)
                block = old if isinstance(old, dict) else CommentedMap()
                for key, value in updates.items():
                    if value is None:
                        block.pop(key, None)
                    else:
                        block[key] = value
                cfg["model"] = block
            if chain is not None:
                cfg["fallback_providers"] = chain
                cfg.pop("fallback_model", None)
            _roundtrip_dump(self.path, yaml_rt, cfg)

    def set_primary(self, provider: str, model: str) -> None:
        self._write(primary={"provider": provider, "model": model})

    def set_fallback(self, chain: list[dict], primary: Optional[dict] = None) -> None:
        self._write(primary=primary, chain=chain)

    @staticmethod
    def chain(cfg: dict) -> list[dict]:
        from hermes_cli.fallback_config import get_fallback_chain
        return get_fallback_chain(cfg)

    @staticmethod
    def credentials_ok(provider: str, model: str) -> Optional[bool]:
        """Can this gateway resolve credentials for the route? Bool only, never the value."""
        try:
            from gateway.run import _resolve_runtime_agent_kwargs_for_provider
        except Exception:
            return None
        try:
            rt = _resolve_runtime_agent_kwargs_for_provider(provider, target_model=model)
        except Exception:
            return False
        return bool(rt.get("api_key") or rt.get("credential_pool"))


# --------------------------------------------------------------------------------------------- core
def _clean(v: Any, field: str) -> str:
    s = str(v or "").strip()
    if not s or not _ID_RX.match(s):
        raise ControlError(f"{field} is missing or not a valid id")
    return s


def _route(raw: Any, field: str) -> dict:
    if not isinstance(raw, dict):
        raise ControlError(f"{field} must be an object {{provider, model}}")
    return {"provider": _clean(raw.get("provider"), f"{field}.provider"),
            "model": _clean(raw.get("model"), f"{field}.model")}


def _primary(cfg: dict) -> dict:
    m = cfg.get("model")
    if isinstance(m, str):
        return {"provider": "", "model": m}
    m = m if isinstance(m, dict) else {}
    return {"provider": str(m.get("provider") or ""), "model": str(m.get("default") or m.get("model") or "")}


def _public_route(entry: dict) -> dict:
    return {"provider": str(entry.get("provider") or ""), "model": str(entry.get("model") or "")}


class ModelControl:
    def __init__(self, runner: Any, home: Path, config: Any = None, run_blocking: Optional[Callable] = None):
        self.runner = runner
        self.home = Path(home)
        self.config = config or HermesConfig(self.home)
        lock_factory = getattr(runner, "_model_switch_lock", None)
        self._lock = lock_factory() if lock_factory else asyncio.Lock()
        self._blocking = run_blocking or asyncio.to_thread

    # ---- catalogue / validation
    def providers(self, cfg: dict) -> dict[str, dict]:
        """Allow only configured provider/model ids, never an arbitrary URL, credential, or unknown model."""
        out: dict[str, dict] = {}

        def add(name, entry):
            if not name:
                return
            p = entry if isinstance(entry, dict) else {}
            models = p.get("models") or []
            if not isinstance(models, list):
                models = []
            ids = [str(x.get("id") or x.get("model") or "") if isinstance(x, dict) else str(x) for x in models]
            ids += [str(p[k]) for k in ("default_model", "model") if p.get(k) and isinstance(p[k], str)]
            slot = out.setdefault(str(name), {"models": []})
            slot["models"] = sorted(set(slot["models"] + [m for m in ids if m]))

        provs = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
        for name, p in provs.items():
            add(name, p)
        for p in cfg.get("custom_providers") or []:
            if isinstance(p, dict):
                add(p.get("name") or p.get("id"), p)
        for route in [_primary(cfg)] + self.config.chain(cfg):
            add(route.get("provider"), {"model": route.get("model")})
        return out

    def validate(self, cfg: dict, route: dict) -> dict:
        provs = self.providers(cfg)
        p = provs.get(route["provider"])
        if p is None:
            raise ControlError(f"provider {route['provider']!r} is not configured on this Son; configured: "
                               f"{', '.join(sorted(provs)) or 'none'}")
        if p["models"] is not None and route["model"] not in p["models"]:
            raise ControlError(f"model {route['model']!r} is not offered by provider {route['provider']!r}; "
                               f"offered: {', '.join(p['models'])}")
        return route

    # ---- session helpers
    def _store(self):
        store = getattr(self.runner, "session_store", None)
        if store is None:
            raise ControlError("this gateway has no session store")
        return store

    def _resolve_key(self, args: dict) -> str:
        store = self._store()
        key, sid = str(args.get("session_key") or "").strip(), str(args.get("session_id") or "").strip()
        entry = None
        if key:
            entry = store.lookup_by_session_key(key)
        if entry is None and sid:
            entry = store.lookup_by_session_id(sid)
        if entry is None and key:
            entry = store.lookup_by_session_id(key)  # sessions_list sessionKey doubles as the id for some rows
        if entry is not None and sid and entry.session_id != sid:
            raise ControlError("sessionKey and sessionId identify different sessions (key may have been reset)")
        if entry is None:
            raise ControlError("session not found on this gateway: pass a sessionKey/sessionId from sessions_list "
                               "(only gateway chat sessions carry a per-session model)")
        return entry.session_key

    def _override(self, key: str) -> Optional[dict]:
        mem = None
        try:
            mem = self.runner._session_model_overrides.get(key)
        except Exception:
            mem = None
        if isinstance(mem, dict) and mem.get("model"):
            return {"model": str(mem.get("model") or ""), "provider": str(mem.get("provider") or "")}
        try:
            persisted = self._store().get_model_override(key)
        except Exception:
            persisted = None
        return _public_route(persisted) if persisted else None

    def _running(self, key: str) -> bool:
        fn = getattr(self.runner, "_is_session_running", None)
        try:
            return bool(fn(key)) if fn else False
        except Exception:
            return False

    def _live(self, key: str) -> Optional[dict]:
        fn = getattr(self.runner, "_cached_agent_for", None)
        try:
            agent = fn(key) if fn else None
        except Exception:
            agent = None
        if agent is None:
            return None
        return {"model": str(getattr(agent, "model", "") or ""), "provider": str(getattr(agent, "provider", "") or ""),
                "on_fallback": bool(getattr(agent, "_fallback_activated", False))}

    def _drop_memory_override(self, key: str) -> None:
        # A queued /model --once restore must not overwrite this newer owner-issued switch after the turn.
        restores = getattr(self.runner, "_pending_one_turn_model_restores", None)
        if restores is not None:
            restores.pop(key, None)
        try:
            self.runner._session_model_overrides.pop(key, None)
        except Exception:
            pass

    def _evict(self, key: str) -> str:
        """Idle session: drop its cached agent so the next turn builds on the new route. Running: leave it."""
        if self._running(key):
            return "after_current_turn"
        fn = getattr(self.runner, "_evict_cached_agent", None)
        if fn:
            try:
                fn(key)
            except Exception:
                logger.debug("evict failed for %s", key, exc_info=True)
        return "next_turn"

    def _channel_route(self, key: str, cfg: dict) -> Optional[dict]:
        entry = self._store().lookup_by_session_key(key)
        source = getattr(entry, "origin", None)
        fn = getattr(self.runner, "_channel_override_for", None)
        channel = fn(source) if fn and source else None
        if channel is None:
            return None
        base = _primary(cfg)
        return {"provider": str(getattr(channel, "provider", None) or base["provider"]),
                "model": str(getattr(channel, "model", None) or base["model"])}

    def _session_view(self, key: str, sid: str, cfg: dict, chain: list[dict]) -> dict:
        ov = self._override(key)
        channel = self._channel_route(key, cfg)
        primary = ov or channel or _primary(cfg)
        order = [dict(primary, role="primary")] + [dict(_public_route(c), role=f"fallback_{i + 1}")
                                                 for i, c in enumerate(chain)]
        return {"sessionKey": key, "sessionId": sid, "source": "session_override" if ov else "channel_override" if channel else "global",
                "override": ov, "effective": _public_route(primary), "fallback_order": order,
                "live_agent": self._live(key), "running": self._running(key)}

    # ---- verbs
    async def status(self, args: dict) -> dict:
        cfg = await self._blocking(self.config.load)
        chain = [_public_route(c) for c in self.config.chain(cfg)]
        prim = _primary(cfg)
        out: dict[str, Any] = {
            "global": {"primary": prim, "fallback": chain,
                       "order": [dict(prim, role="primary")] + [dict(c, role=f"fallback_{i + 1}")
                                                                 for i, c in enumerate(chain)]},
            "providers": {k: {"models": v["models"]} for k, v in sorted(self.providers(cfg).items())},
            "note": "A session without an override uses the global route. The fallback chain is Son-wide: Hermes "
                    "has no per-session fallback list.",
        }
        if args.get("session_key") or args.get("session_id"):
            key = self._resolve_key(args)
            entry = self._store().lookup_by_session_key(key)
            out["session"] = self._session_view(key, getattr(entry, "session_id", ""), cfg, chain)
            return out
        limit = max(1, min(int(args.get("limit") or STATUS_DEFAULT), STATUS_MAX))
        entries = await self._blocking(self._store().list_sessions)
        offset = max(0, int(args.get("offset") or 0))
        rows = [self._session_view(e.session_key, e.session_id, cfg, chain) for e in entries[offset:offset + limit]]
        out["hasMore"] = offset + len(rows) < len(entries)
        if out["hasMore"]:
            out["nextOffset"] = offset + len(rows)
        out["sessions"] = rows
        out["count"], out["total"] = len(rows), len(entries)
        out["overridden"] = sum(1 for r in rows if r["override"])
        return out

    async def set_global(self, args: dict) -> dict:
        route = _route(args, "route")
        async with self._lock:
            cfg = await self._blocking(self.config.load)
            self.validate(cfg, route)
            creds = await self._blocking(self.config.credentials_ok, route["provider"], route["model"])
            if creds is False:
                raise ControlError("credentials unavailable for the selected provider; nothing changed")
            before = _primary(cfg)
            await self._blocking(self.config.set_primary, route["provider"], route["model"])
            cleared, deferred, channel_pins, failures = [], [], [], []
            affected = 0
            if args.get("clear_session_overrides", True) is not False:
                store = self._store()
                entries = await self._blocking(store.list_sessions)
                keys = {e.session_key for e in entries}
                try:
                    keys |= set(self.runner._session_model_overrides)
                except Exception:
                    pass
                for key in sorted(keys):
                    # Channel overrides take precedence over the global default: pin this existing session
                    # to the requested route as well, so ALL really means all, not only unpinned chats.
                    try:
                        channel = self._channel_route(key, cfg)
                        had_override = self._override(key) is not None
                        await self._blocking(store.set_model_override, key, route if channel else None)
                        self._drop_memory_override(key)
                        affected += 1
                        if had_override:
                            cleared.append(key)
                        if channel:
                            channel_pins.append(key)
                        if self._evict(key) == "after_current_turn":
                            deferred.append(key)
                    except Exception as exc:
                        failures.append({"sessionKey": key, "error": type(exc).__name__})
        result = {"ok": not failures, "partial": bool(failures), "changed": before != route or bool(cleared),
 "before": before, "primary": route,
                "session_overrides_cleared": len(cleared), "sessions_affected": affected,
                "channel_sessions_pinned": len(channel_pins), "running_sessions_switch_after_turn":
                len(deferred), "credentials_ok": creds,
                "applies": "every existing gateway session's next turn (running turns finish on the old route)"}
        if failures:
            result["failed_sessions"] = failures
            result["error"] = "Partial change: global route saved but some session overrides failed; retry son_model_set_all"
        return result

    async def set_session(self, args: dict) -> dict:
        store = self._store()
        async with self._lock:
            key = self._resolve_key(args)
            before = self._override(key)
            if args.get("clear"):
                await self._blocking(store.set_model_override, key, None)
                self._drop_memory_override(key)
                when = self._evict(key)
                return {"ok": True, "sessionKey": key, "before": before, "override": None, "applies": when}
            route = _route(args, "route")
            cfg = await self._blocking(self.config.load)
            self.validate(cfg, route)
            creds = await self._blocking(self.config.credentials_ok, route["provider"], route["model"])
            if creds is False:
                raise ControlError("credentials unavailable for the selected provider; nothing changed")
            await self._blocking(store.set_model_override, key, route)
            # Drop the in-memory copy: the next turn rehydrates from the store and re-resolves the provider's
            # credentials itself (the same path a gateway restart takes). Credentials never pass through here.
            self._drop_memory_override(key)
            when = self._evict(key)
        return {"ok": True, "sessionKey": key, "before": before, "override": route, "credentials_ok": creds,
                "applies": when}

    async def set_fallback(self, args: dict) -> dict:
        raw = args.get("fallback")
        if not isinstance(raw, list) or len(raw) > MAX_FALLBACKS:
            raise ControlError(f"fallback must be a list of at most {MAX_FALLBACKS} {{provider, model}} objects")
        chain = [_route(x, f"fallback[{i}]") for i, x in enumerate(raw)]
        primary = _route(args["primary"], "primary") if args.get("primary") else None
        async with self._lock:
            cfg = await self._blocking(self.config.load)
            for r in chain + ([primary] if primary else []):
                self.validate(cfg, r)
                if await self._blocking(self.config.credentials_ok, r["provider"], r["model"]) is False:
                    raise ControlError("credentials unavailable for a route in the requested order; nothing changed")
            prim = primary or _primary(cfg)
            seen, dedup = {(prim["provider"].lower(), prim["model"].lower())}, []
            for r in chain:
                ident = (r["provider"].lower(), r["model"].lower())
                if ident in seen:
                    raise ControlError(f"duplicate route in order: {r['provider']}/{r['model']}")
                seen.add(ident)
                dedup.append(r)
            before = [_public_route(c) for c in self.config.chain(cfg)]
            await self._blocking(self.config.set_fallback, dedup, primary)
        order = [dict(prim, role="primary")] + [dict(r, role=f"fallback_{i + 1}") for i, r in enumerate(dedup)]
        return {"ok": True, "before": before, "order": order,
                "applies": "next turn of every session (the gateway re-reads fallback_providers per turn)"}

    async def command(self, verb: str, args: dict, principal: Any) -> dict:
        """Only the socket peer supplies principal; command arguments cannot authorize themselves.

        A deployment-owned email -> platform-id mapping is required. Never impersonate a stored
        session origin just because a caller supplied its key. Invoke native dispatch on this
        gateway's event loop, not handle_message (which could fall through to an LLM or exec alias).
        """
        fields = {"session_key", "session_id"} | ({"command"} if verb == "slash" else {"mode"} if verb == "approvals_set" else set())
        if set(args) - fields:
            raise ControlError("unsupported command arguments")
        cfg = await self._blocking(self.config.load)
        bindings = (cfg.get("aiteam_control") or {}).get("owners") or {}
        identity = bindings.get(principal) if isinstance(principal, str) else None
        if not isinstance(identity, dict):
            raise ControlError("owner platform identity is not configured")
        key = str(args.get("session_key") or "")
        sid = str(args.get("session_id") or "")
        store = self._store()
        entry = store.lookup_by_session_key(key) if key else store.lookup_by_session_id(sid) if sid else None
        if entry is None or (sid and entry.session_id != sid):
            raise ControlError("session not found or selectors disagree")
        expected_id = entry.session_id
        def recheck():
            current = store.lookup_by_session_key(entry.session_key)
            if current is None or current.session_id != expected_id:
                raise ControlError("session changed before dispatch; refresh the selector")
        source = getattr(entry, "origin", None)
        platform = getattr(getattr(source, "platform", None), "value", None)
        if not source or not identity.get(platform) or str(source.user_id) != str(identity[platform]):
            raise ControlError("session is not bound to this owner's platform identity")
        if self.runner._session_key_for_source(source) != entry.session_key:
            raise ControlError("session origin no longer resolves to the selected session")
        if not self.runner._is_user_authorized_for_source(source):
            raise ControlError("gateway source authorization refused")
        if verb == "approvals_set":
            mode = args.get("mode")
            if mode not in ("smart", "yolo"):
                raise ControlError("mode must be smart or yolo")
            from gateway.slash_access import policy_for_source
            if self.runner._check_slash_access(source, "approvals") or not policy_for_source(self.runner.config, source).is_admin(source.user_id):
                raise ControlError("gateway approval policy requires an admin")
            from hermes_cli.approval_mode import run_approval_mode_command
            async with self.runner._async_profile_scope_for_source(source):
                recheck()
                result = run_approval_mode_command("off" if mode == "yolo" else "smart")
            # Typed native outcome only: native messages can echo exception/managed-policy text.
            if not result.ok:
                raise ControlError("approval mode was not applied; inspect native policy locally")
            return {"ok": True, "mode": mode, "native_mode": result.mode, "changed": result.changed,
                    "scope": "profile", "session_yolo_overrides_cleared": False}
        from hermes_cli.commands import resolve_command
        from gateway.platforms.base import MessageEvent
        raw = args.get("command") if verb == "slash" else "/new" if verb == "session_reset" else "/stop"
        # Constrained built-in grammar. Never execute plugin/quick aliases, arbitrary text,
        # credential-taking commands, or commands that select another session/profile.
        if not isinstance(raw, str) or not re.fullmatch(r"/[a-z-]+(?: [a-z-]+)?", raw):
            raise ControlError("use a supported built-in slash command; credentials must use ai_signin_start secure entry")
        token, _, argument = raw[1:].partition(" ")
        command = resolve_command(token)
        if command is None or command.cli_only:
            raise ControlError("native gateway command is unavailable")
        allowed = {name: {""} for name in ("status", "context", "help", "commands", "profile", "version", "whoami", "agents", "stop", "new")}
        allowed.update(reasoning={"", "none", "minimal", "low", "medium", "high", "xhigh", "show", "hide"},
                       fast={"", "normal", "fast"}, footer={"", "on", "off", "status"})
        if command.name not in allowed or argument not in allowed[command.name]:
            raise ControlError("command not enabled for remote dispatch; credentials must use ai_signin_start secure entry")
        if self.runner._check_slash_access(source, command.name):
            raise ControlError("gateway slash policy refused")
        event = MessageEvent(text="/" + command.name + (" " + argument if argument else ""), source=source)
        async with self.runner._async_profile_scope_for_source(source):
            recheck()
            # The native idle /stop handler widens to sibling runs. The busy stop dispatcher
            # invokes _interrupt_and_clear_session on this exact key even when it is idle.
            if command.name in {"stop", "new"} or self._running(entry.session_key):
                await self.runner._dispatch_busy_slash_command(event, command, entry.session_key, source)
            else:
                handled, _reply = await self.runner._hm_dispatch_canonical_command(event, source, entry.session_key, command.name)
                if not handled:
                    raise ControlError("native command did not handle request; no agent turn started")
        current = store.lookup_by_session_key(entry.session_key)
        return {"ok": True, "dispatched": True, "session_key": entry.session_key,
                "session_id": current.session_id if current else None, "command": command.name,
                "outcome": "native handler returned; reply suppressed (may contain private data)"}

    async def dispatch(self, req: dict) -> dict:
        if getattr(getattr(self.runner, "config", None), "multiplex_profiles", False):
            return {"ok": False, "error": "model control does not support multiplex gateways; use each profile's /model command"}
        verb = req.get("verb")
        if not isinstance(verb, str):
            return {"ok": False, "error": "verb must be a string"}
        args = req.get("args") if isinstance(req.get("args"), dict) else {}
        if verb in {"session_stop", "session_interrupt", "session_reset", "approvals_set", "slash"}:
            try:
                return await self.command(verb, args, req.get("principal"))
            except ControlError as exc:
                return {"ok": False, "error": str(exc)}
            except Exception:
                return {"ok": False, "error": "native command failed; result unverified"}
        fn = {"status": self.status, "set_global": self.set_global, "set_session": self.set_session,
              "set_fallback": self.set_fallback}.get(verb)
        if fn is None:
            return {"ok": False, "error": f"unknown verb {verb!r}"}
        try:
            if verb == "status":
                async with self._lock:
                    res = await fn(args)
            else:
                res = await fn(args)
            return res if "ok" in res else {"ok": True, **res}
        except ControlError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001  never leak a traceback (could carry config values)
            logger.warning("model control %s failed: %s", verb, type(e).__name__)
            return {"ok": False, "error": f"{verb} failed ({type(e).__name__})"}


def scrub(obj: Any) -> Any:
    """Defence in depth: drop any secret-looking key before a reply leaves the gateway."""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()
                if not (isinstance(k, str) and k.lower() in _SECRET_KEYS)}
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    return obj


async def serve(control: ModelControl, path: Path) -> asyncio.AbstractServer:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if len(line) > MAX_REQUEST:
                res = {"ok": False, "error": "request too large"}
            else:
                try:
                    req = json.loads(line.decode() or "{}")
                    res = await control.dispatch(req if isinstance(req, dict) else {})
                except ValueError:
                    res = {"ok": False, "error": "bad json"}
            encoded = json.dumps(scrub(res), default=str).encode()
            if len(encoded) > 500 * 1024:
                encoded = b'{"ok":false,"error":"response too large; request fewer sessions with limit/offset"}'
            writer.write(encoded + b"\n")
            await writer.drain()
        except Exception:  # noqa: BLE001
            logger.debug("model control connection failed", exc_info=True)
        finally:
            writer.close()

    # Bind before listening, chmod before accepting: never change the gateway process's global umask.
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
        os.chmod(path, 0o600)
        sock.setblocking(False)
        server = await asyncio.start_unix_server(handle, sock=sock, limit=MAX_REQUEST + 1)
    except BaseException:
        sock.close()
        raise
    return server


_SERVER: Optional[asyncio.AbstractServer] = None


async def handle(event_type: str, context: dict) -> None:
    """Hermes gateway hook entry (HOOK.yaml events: gateway:startup)."""
    global _SERVER
    if event_type != "gateway:startup" or _SERVER is not None:
        return
    try:
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref() if callable(_gateway_runner_ref) else None
    except Exception:
        runner = None
    if runner is None:
        logger.warning("aiteam-model-control: no gateway runner; model tools stay off")
        return
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    _SERVER = await serve(ModelControl(runner, home), home / "mcp" / SOCKET_NAME)
    logger.info("aiteam-model-control listening on %s", home / "mcp" / SOCKET_NAME)
