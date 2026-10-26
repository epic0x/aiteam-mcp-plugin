"""Messaging box: a sidecar with its OWN identity that relays messages into one (or more) Sons.

Why it exists ("the single-Son problem"): a customer may run a single Son with no coordinator above it.
Chat platforms drop a bot's own messages (self-ignore), and a lone Son has nobody to cross-tag it, so
nothing outside its owner's chat can drive it. The box fixes that without touching chat platforms:

  caller (Claude/ChatGPT connector, a coordinator, a script)
     -- HTTPS + caller token --> messaging box (own identity, holds the Son's API key)
     -- REST/SSE, author = box:<box_id>/<caller_id> --> Son's Hermes api_server
     <-- streamed reply ------------------------------------------------ (relayed back out)

* Each caller has its own token (only its SHA-256 is stored) and an allowlist of Sons.
* Each (caller, conversation) pair maps to ONE dedicated Hermes session on the Son, id
  ``box_<sha256(box_id|caller|conversation)[:24]>``, created on first use. Callers never see or touch
  the owner's chat sessions, and the turn is labelled with the caller as author (memory stays honest).
* The Son's API key never leaves the box. Callers never get it.

HTTP API (JSON unless noted):
  GET  /v1/whoami                                  -> {box, caller, sons}
  GET  /v1/sons                                    -> [{name, ok, version}]
  POST /v1/sons/{son}/messages                     {text, conversation?, stream?}
                                                   stream=false -> {reply, session_id, status, tools}
                                                   stream=true  -> text/event-stream relay of the Son's frames
  GET  /v1/sons/{son}/conversations                -> the caller's conversations on that Son
  GET  /v1/sons/{son}/conversations/{conv}/messages?limit
  GET  /healthz                                    -> {ok: true} (no auth)
  GET  /v1/ping                                    -> {alive, box, uptime_s, checked_at} (caller token)
  GET  /v1/health                                  -> live health of this computer (host_health.py): disk, memory,
                                                      swap, CPU, uptime, live sessions, scheduled jobs, last cleanup,
                                                      status ok|warning|critical + a one-sentence summary
  GET  /v1/health/history                          -> 24 h of 5-minute samples (graphs)
  POST /v1/cleanup                                 -> run the fixed safe cleanup (no parameters; once per 10 min)
  GET  /v1/ai/status                               -> which AI the Son uses: providers signed in, model per session
  POST /v1/ai/model                                {provider, model, scope: all|session, session_key?, session_id?}
  PUT  /v1/ai/fallback                             {chain: [{provider, model}]}
  POST /v1/ai/signin/start                         {provider: claude|chatgpt|anthropic-key|openai-key, label?}
  GET  /v1/ai/signin/{signin_id}                   -> waiting | connected | failed | expired
       (the five /v1/ai routes proxy to the Son's aiteam-ai-control gateway plugin on loopback, see AiControl.
        /ai/credential is NEVER exposed here: a code or key only reaches the Son through the aiteam.space page.)
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import pathlib
from dataclasses import dataclass, field
from typing import Any, Optional

from aiohttp import web

from .bridge import BridgeError, HermesBridge
from .host_health import HealthConfig, HostHealth, live_sessions

log = logging.getLogger("aiteam.box")
CONV_RX = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_TEXT = 32_000


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@dataclass
class Caller:
    id: str
    name: str
    token_sha256: str
    sons: list[str] = field(default_factory=lambda: ["*"])

    def may(self, son: str) -> bool:
        return "*" in self.sons or son in self.sons


@dataclass
class SonTarget:
    name: str
    url: str
    key_env: str  # name of the env var holding that Son's API_SERVER_KEY (value never in the config file)
    model: Optional[str] = None  # pin the box session's model (Hermes <=0.20 stores "hermes-agent" otherwise -> provider 404)


@dataclass
class BoxConfig:
    box_id: str
    box_name: str
    sons: dict[str, SonTarget]
    callers: list[Caller]
    health: Optional[dict] = None  # host_health.HealthConfig keys; None = defaults (HERMES_HOME, its disk)
    ai_control: Optional[dict] = None  # {"url": "http://127.0.0.1:8651", "key_env": "API_SERVER_KEY"}; None = defaults

    @classmethod
    def load(cls, path: str) -> "BoxConfig":
        with open(path) as f:
            raw = json.load(f)
        box_id = str(raw["box_id"])
        if not re.fullmatch(r"[a-z0-9-]{3,40}", box_id):
            raise ValueError("box_id must be 3-40 chars of a-z 0-9 -")
        sons = {s["name"]: SonTarget(s["name"], s["url"], s.get("key_env", "SON_API_KEY"), s.get("model")) for s in raw["sons"]}
        callers = [Caller(c["id"], c.get("name", c["id"]), c["token_sha256"], c.get("sons", ["*"]))
                   for c in raw.get("callers", [])]
        return cls(box_id, raw.get("box_name", box_id), sons, callers, raw.get("health"), raw.get("ai_control"))


AI_BODY_MAX = 16 * 1024
AI_UNAVAILABLE = "ai control not available on this Son yet"
SIGNIN_ID_RX = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _env_file_value(path: pathlib.Path, name: str) -> str:
    """KEY=value from a dotenv file (``export`` and quotes tolerated). Empty when absent or unreadable."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    for ln in lines:
        ln = ln.strip()
        if ln.startswith("export "):
            ln = ln[7:].lstrip()
        k, sep, v = ln.partition("=")
        if sep and k.strip() == name:
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            return v
    return ""


class AiControl:
    """Proxy to the Son's ``aiteam-ai-control`` gateway plugin (loopback HTTP, Bearer = the api_server key).

    The key is read per call (env ``key_env``, else ``$HERMES_HOME/.env``) so a rotated key needs no restart. It never
    leaves the box, and request/response bodies are never logged (a sign-in start carries a one-time ticket)."""

    def __init__(self, raw: Optional[dict] = None):
        raw = raw or {}
        self.url = str(raw.get("url") or "http://127.0.0.1:8651").rstrip("/")
        self.key_env = str(raw.get("key_env") or "API_SERVER_KEY")
        self.timeout_s = float(raw.get("timeout_s") or 30)

    def key(self) -> str:
        k = os.environ.get(self.key_env, "")
        if k:
            return k
        home = pathlib.Path(os.environ.get("HERMES_HOME") or "/opt/data")
        return _env_file_value(home / ".env", self.key_env)

    async def forward(self, method: str, path: str, body: Optional[dict] = None) -> tuple[int, dict]:
        import aiohttp
        key = self.key()
        if not key:
            return 503, {"error": AI_UNAVAILABLE}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout_s)) as http:
                async with http.request(method, self.url + path, json=body,
                                        headers={"Authorization": f"Bearer {key}"}) as r:
                    status = r.status
                    try:
                        data = await r.json(content_type=None)
                    except Exception:  # noqa: BLE001 - non-JSON upstream body: never relayed
                        data = None
        except (aiohttp.ClientError, OSError, TimeoutError):
            return 503, {"error": AI_UNAVAILABLE}
        if status >= 500:
            return 502, {"error": "ai control failed on this Son"}
        if status in (401, 403):  # the box's own key was refused: not the caller's fault, never echo upstream
            return 502, {"error": "ai control refused this box's key"}
        if not isinstance(data, dict):
            data = {"error": f"ai control answered {status}"} if status >= 400 else {}
        return status, data


def session_id_for(box_id: str, caller_id: str, conversation: str) -> str:
    return "box_" + sha(f"{box_id}\0{caller_id}\0{conversation}")[:24]


class Box:
    def __init__(self, cfg: BoxConfig, bridges: Optional[dict[str, HermesBridge]] = None,
                 host: Optional[HostHealth] = None):
        self.cfg = cfg
        self.host = host or HostHealth(HealthConfig.from_raw(cfg.health))
        self.ai = AiControl(cfg.ai_control)
        self.bridges = bridges or {
            n: HermesBridge(t.url, os.environ.get(t.key_env, ""), model=t.model) for n, t in cfg.sons.items()}
        missing = [n for n, t in cfg.sons.items() if bridges is None and not os.environ.get(t.key_env)]
        if missing:
            raise SystemExit(f"missing Son API key env for: {', '.join(missing)}")

    # --- auth -------------------------------------------------------------------------------
    def caller_for(self, request: web.Request) -> Caller:
        h = request.headers.get("Authorization", "")
        tok = h[7:].strip() if h.startswith("Bearer ") else ""
        if tok:
            digest = sha(tok)
            for c in self.cfg.callers:
                if hmac.compare_digest(digest, c.token_sha256):
                    return c
        raise web.HTTPUnauthorized(text=json.dumps({"error": "invalid caller token"}),
                                   content_type="application/json")

    def target(self, caller: Caller, son: str) -> HermesBridge:
        if son not in self.cfg.sons:
            raise web.HTTPNotFound(text=json.dumps({"error": f"unknown son {son!r}"}), content_type="application/json")
        if not caller.may(son):
            raise web.HTTPForbidden(text=json.dumps({"error": "caller not allowed on this son"}),
                                    content_type="application/json")
        return self.bridges[son]

    def author(self, caller: Caller) -> dict:
        # Hermes labels the turn with this author; ``box:`` keeps it distinct from the Son's own bot ids.
        return {"id": f"box:{self.cfg.box_id}/{caller.id}", "name": caller.name, "is_bot": True}

    async def ensure_session(self, b: HermesBridge, caller: Caller, conv: str) -> str:
        sid = session_id_for(self.cfg.box_id, caller.id, conv)
        if await b.get_session(sid) is None:
            try:
                await b.create_session(sid, title=f"Box {self.cfg.box_name}: {caller.name} / {conv} [{sid[-6:]}]", model=b.model)
            except BridgeError as e:
                if e.status != 409:
                    raise
        return sid

    # --- handlers ---------------------------------------------------------------------------
    async def healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "box": self.cfg.box_id})

    async def whoami(self, request: web.Request) -> web.Response:
        c = self.caller_for(request)
        return web.json_response({"box": {"id": self.cfg.box_id, "name": self.cfg.box_name},
                                  "caller": {"id": c.id, "name": c.name},
                                  "sons": [n for n in self.cfg.sons if c.may(n)]})

    async def sons(self, request: web.Request) -> web.Response:
        c = self.caller_for(request)
        out = []
        for n in self.cfg.sons:
            if not c.may(n):
                continue
            try:
                h = await self.bridges[n].health()
                out.append({"name": n, "ok": h.get("status") == "ok", "version": h.get("version")})
            except Exception as e:  # noqa: BLE001 - report, never crash the listing
                out.append({"name": n, "ok": False, "error": type(e).__name__})
        return web.json_response({"sons": out})

    async def post_message(self, request: web.Request) -> web.StreamResponse:
        c = self.caller_for(request)
        son = request.match_info["son"]
        b = self.target(c, son)
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text='{"error":"invalid JSON"}', content_type="application/json")
        text = body.get("text") if isinstance(body, dict) else None
        conv = str(body.get("conversation") or "default")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise web.HTTPBadRequest(text='{"error":"text required (max 32000 chars)"}', content_type="application/json")
        if not CONV_RX.fullmatch(conv):
            raise web.HTTPBadRequest(text='{"error":"conversation: 1-64 of A-Z a-z 0-9 . _ -"}',
                                     content_type="application/json")
        try:
            sid = await self.ensure_session(b, c, conv)
        except BridgeError as e:
            return web.json_response({"error": e.message, "code": e.code}, status=502)
        log.info("relay in: caller=%s son=%s conv=%s session=%s chars=%d", c.id, son, conv, sid, len(text))
        if not body.get("stream"):
            try:
                r = await b.chat_stream(sid, text, author=self.author(c))
            except BridgeError as e:
                return web.json_response({"error": e.message, "code": e.code, "session_id": sid}, status=502)
            return web.json_response({"reply": r.reply, "session_id": sid, "conversation": conv,
                                      "status": r.status, "tools": r.tools, "error": r.error or None})
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no", "X-Box-Session-Id": sid})
        await resp.prepare(request)

        async def relay(name: str, data: dict) -> None:
            await resp.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        try:
            await b.chat_stream(sid, text, author=self.author(c), on_event=relay)
        except BridgeError as e:
            await relay("error", {"message": e.message, "code": e.code})
        await relay("done", {"session_id": sid})
        return resp

    async def conversations(self, request: web.Request) -> web.Response:
        c = self.caller_for(request)
        son = request.match_info["son"]
        b = self.target(c, son)
        prefix = f"Box {self.cfg.box_name}: {c.name} / "
        rows = await b.list_sessions(limit=200, source="api_server")
        out = [{"conversation": (s.get("title") or "")[len(prefix):].rsplit(" [", 1)[0], "session_id": s["id"],
                "last_active": s.get("last_active"), "message_count": s.get("message_count")}
               for s in rows if (s.get("title") or "").startswith(prefix) and str(s.get("id", "")).startswith("box_")]
        return web.json_response({"conversations": out})

    async def conv_messages(self, request: web.Request) -> web.Response:
        c = self.caller_for(request)
        son, conv = request.match_info["son"], request.match_info["conv"]
        if not CONV_RX.fullmatch(conv):
            raise web.HTTPBadRequest()
        b = self.target(c, son)
        limit = max(1, min(int(request.query.get("limit", "20") or 20), 200))
        sid = session_id_for(self.cfg.box_id, c.id, conv)
        if await b.get_session(sid) is None:
            return web.json_response({"messages": [], "session_id": sid})
        msgs = await b.messages(sid, limit=limit)
        keep = ("role", "content", "timestamp", "tool_name")
        return web.json_response({"session_id": sid, "messages": [{k: m.get(k) for k in keep if k in m} for m in msgs]})

    # --- computer health (host_health.py) ------------------------------------------------------
    async def _sessions(self, c: Caller) -> dict:
        import time as _t
        rows: list[dict] = []
        for n in self.cfg.sons:
            if not c.may(n):
                continue
            try:
                rows += await self.bridges[n].list_sessions(limit=200)
            except Exception:  # noqa: BLE001 - Hermes down must not hide disk/memory
                return {"live": None, "error": "the agent is not answering"}
        return live_sessions(rows, _t.time())

    async def ping(self, request: web.Request) -> web.Response:
        self.caller_for(request)
        snap = self.host.static_snapshot()
        return web.json_response({"alive": True, "box": self.cfg.box_id, "uptime_s": snap["uptime"]["seconds"],
                                  "uptime": snap["uptime"]["text"]})

    async def health(self, request: web.Request) -> web.Response:
        c = self.caller_for(request)
        snap = await self.host.snapshot(sessions=await self._sessions(c))
        snap["box"] = {"id": self.cfg.box_id, "name": self.cfg.box_name}
        if request.query.get("history") in ("1", "true", "24h"):
            snap["history"] = self.host.history()
        return web.json_response(snap)

    async def health_history(self, request: web.Request) -> web.Response:
        self.caller_for(request)
        rows = self.host.history()
        return web.json_response({"interval_s": self.host.cfg.sample_interval_s, "count": len(rows), "samples": rows})

    async def cleanup(self, request: web.Request) -> web.Response:
        c = self.caller_for(request)
        log.info("cleanup requested by caller=%s", c.id)
        code, body = await self.host.cleanup()
        headers = {"Retry-After": str(body["retry_after_s"])} if code == 429 else None
        return web.json_response(body, status=code, headers=headers)

    # --- AI control (proxy to the gateway plugin; see AiControl) --------------------------------
    @staticmethod
    async def _small_json(request: web.Request) -> dict:
        if (request.content_length or 0) > AI_BODY_MAX:
            raise web.HTTPRequestEntityTooLarge(max_size=AI_BODY_MAX, actual_size=request.content_length or 0,
                                                text='{"error":"request too large"}', content_type="application/json")
        raw = b""
        while len(raw) <= AI_BODY_MAX:  # StreamReader.read(n) may return a partial chunk: read to EOF, capped
            chunk = await request.content.read(AI_BODY_MAX + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
        if len(raw) > AI_BODY_MAX:
            raise web.HTTPRequestEntityTooLarge(max_size=AI_BODY_MAX, actual_size=len(raw),
                                                text='{"error":"request too large"}', content_type="application/json")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='{"error":"a JSON object is required"}', content_type="application/json")
        return body

    async def _ai(self, request: web.Request, method: str, path: str, with_body: bool,
                  label: Optional[str] = None) -> web.Response:
        c = self.caller_for(request)
        # AI control is profile-wide, not caller-conversation-scoped. A token restricted to a
        # different Son must never gain model/credential control merely by authenticating.
        local_son = self.cfg.ai_control.get("son") if self.cfg.ai_control else None
        if not local_son and len(self.cfg.sons) == 1:
            local_son = next(iter(self.cfg.sons))
        if not local_son or local_son not in self.cfg.sons:
            return web.json_response({"error": "ai control needs an explicit local son"}, status=503)
        if not c.may(local_son):
            return web.json_response({"error": "caller not allowed on this son"}, status=403)
        body = await self._small_json(request) if with_body else None
        code, data = await self.ai.forward(method, path, body)
        log.info("ai control: caller=%s %s %s -> %d", c.id, method, label or path, code)  # never the bodies
        return web.json_response(data, status=code, headers={"Cache-Control": "no-store"})

    async def ai_status(self, request: web.Request) -> web.Response:
        return await self._ai(request, "GET", "/ai/status", False)

    async def ai_model(self, request: web.Request) -> web.Response:
        return await self._ai(request, "POST", "/ai/model", True)

    async def ai_fallback(self, request: web.Request) -> web.Response:
        return await self._ai(request, "PUT", "/ai/fallback", True)

    async def ai_signin_start(self, request: web.Request) -> web.Response:
        return await self._ai(request, "POST", "/ai/signin/start", True)

    async def ai_signin_get(self, request: web.Request) -> web.Response:
        self.caller_for(request)  # auth before input validation: no oracle for callers without a token
        sid = request.match_info["signin_id"]
        if not SIGNIN_ID_RX.fullmatch(sid):
            raise web.HTTPBadRequest(text='{"error":"invalid signin_id"}', content_type="application/json")
        return await self._ai(request, "GET", f"/ai/signin/{sid}", False, label="/ai/signin/<id>")

    async def _start_sampler(self, _app: web.Application) -> None:
        self.host.start_sampler()

    async def _stop_sampler(self, _app: web.Application) -> None:
        await self.host.stop()

    def app(self, sample: bool = False) -> web.Application:
        # sample=True (the serve command) keeps 24 h of 5-minute samples; tests and embedders leave it off.
        a = web.Application(client_max_size=256 * 1024)
        if sample:
            a.on_startup.append(self._start_sampler)
            a.on_cleanup.append(self._stop_sampler)
        a.router.add_get("/v1/ping", self.ping)
        a.router.add_get("/v1/health", self.health)
        a.router.add_get("/v1/health/history", self.health_history)
        a.router.add_post("/v1/cleanup", self.cleanup)
        # AI control. Deliberately NO /v1/ai/credential: codes and keys only arrive through the aiteam.space page.
        a.router.add_get("/v1/ai/status", self.ai_status)
        a.router.add_post("/v1/ai/model", self.ai_model)
        a.router.add_put("/v1/ai/fallback", self.ai_fallback)
        a.router.add_post("/v1/ai/signin/start", self.ai_signin_start)
        a.router.add_get("/v1/ai/signin/{signin_id}", self.ai_signin_get)
        a.router.add_get("/healthz", self.healthz)
        a.router.add_get("/v1/whoami", self.whoami)
        a.router.add_get("/v1/sons", self.sons)
        a.router.add_post("/v1/sons/{son}/messages", self.post_message)
        a.router.add_get("/v1/sons/{son}/conversations", self.conversations)
        a.router.add_get("/v1/sons/{son}/conversations/{conv}/messages", self.conv_messages)
        return a


def main(argv: Optional[list[str]] = None) -> None:
    p = argparse.ArgumentParser(prog="aiteam-box", description="AI Teamspace messaging box (sidecar)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--config", required=True)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8710)
    t = sub.add_parser("new-caller", help="mint a caller token; prints the token ONCE and the config entry")
    t.add_argument("id")
    t.add_argument("--name")
    t.add_argument("--sons", default="*")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if a.cmd == "new-caller":
        tok = "box_" + secrets.token_urlsafe(32)
        entry = {"id": a.id, "name": a.name or a.id, "token_sha256": sha(tok), "sons": a.sons.split(",")}
        print(json.dumps({"token": tok, "config_entry": entry}, indent=2))
        return
    web.run_app(Box(BoxConfig.load(a.config)).app(sample=True), host=a.host, port=a.port, print=None)


if __name__ == "__main__":
    main(sys.argv[1:])
