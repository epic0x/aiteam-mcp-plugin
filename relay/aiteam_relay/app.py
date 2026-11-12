"""ASGI app + CLI for the unified relay. Contract: docs/RELAY.md."""
from __future__ import annotations

import argparse
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from . import __version__
from .auth import Jwks, verify_relay_jwt
from .exchange import ExchangeError, Exchanger, InvalidGrant
from .proxy import proxy

log = logging.getLogger("aiteam_relay")
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx INFO lines print full request URLs
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _env(name: str, default: str) -> str:
    v = os.environ.get(name, "").strip()
    return v or default


@dataclass
class Settings:
    public_url: str = field(default_factory=lambda: _env("RELAY_PUBLIC_URL", "https://mcp.aiteam.space").rstrip("/"))
    issuer: str = field(default_factory=lambda: _env("AITEAM_ISSUER", "https://aiteam.space").rstrip("/"))
    host: str = field(default_factory=lambda: _env("RELAY_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("RELAY_PORT", "8790")))
    son_host_suffix: str = field(default_factory=lambda: _env("RELAY_SON_HOST_SUFFIX", ".aiteam.space"))
    challenge: str = field(default_factory=lambda: os.environ.get("OPENAI_APPS_CHALLENGE", ""))
    challenge_file: str = field(default_factory=lambda: os.environ.get("OPENAI_APPS_CHALLENGE_FILE", ""))
    # on = the relay is its own MCP server over every box of the grant (multibox.py); off = one-box stream proxy
    multi_box: bool = field(default_factory=lambda: _env("RELAY_MULTI_BOX", "off").lower() in ("on", "1", "true"))

    @property
    def resource(self) -> str:
        return self.public_url + "/mcp"

    @property
    def prm_url(self) -> str:
        return self.public_url + "/.well-known/oauth-protected-resource/mcp"

    @property
    def relay_host(self) -> str:
        return (urlsplit(self.public_url).hostname or "").lower()

    def challenge_token(self) -> str:
        if self.challenge.strip():
            return self.challenge.strip()
        if self.challenge_file:
            try:
                with open(self.challenge_file, encoding="utf-8") as f:
                    return f.read().strip()
            except OSError:
                return ""
        return ""


def create_app(settings: Settings | None = None, *, client: httpx.AsyncClient | None = None,
               jwks=None, box_transport: httpx.AsyncBaseTransport | None = None,
               clock: Callable[[], float] = time.time, box_timeout: float | None = None) -> Starlette:
    """`box_transport` / `clock` / `box_timeout` exist for tests (multi-box mode only)."""
    s = settings or Settings()
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=None, write=30, pool=10),
                                       headers={"user-agent": f"aiteam-relay/{__version__}"},
                                       follow_redirects=False)
    keys = jwks or Jwks(s.issuer + "/oauth/jwks", http)
    exchanger = Exchanger(http, token_url=s.issuer + "/oauth/token", suffix=s.son_host_suffix,
                          relay_host=s.relay_host, clock=clock)
    multi = None
    if s.multi_box:
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        from .boxes import BOX_TIMEOUT, BoxPool
        from .multibox import Router, build_server
        from .targets import TargetsClient
        targets = TargetsClient(http, url=s.issuer + "/oauth/targets", suffix=s.son_host_suffix,
                                relay_host=s.relay_host, clock=clock)
        pool = BoxPool(exchanger, transport=box_transport, clock=clock,
                       box_timeout=BOX_TIMEOUT if box_timeout is None else box_timeout)
        router = Router(targets, exchanger, pool, clock=clock)
        # Stateless: every HTTP request is authenticated and scoped on its own; no MCP session outlives a request.
        multi = StreamableHTTPSessionManager(app=build_server(router), json_response=False, stateless=True)

    def unauthorized(error: str | None = None, desc: str = "") -> Response:
        www = f'Bearer resource_metadata="{s.prm_url}"'
        if error:
            www += f', error="{error}"'
        body = {"error": error or "unauthorized", "error_description": desc or "sign in with aiteam.space"}
        return JSONResponse(body, status_code=401, headers={"WWW-Authenticate": www})

    def upstream_error(status: int, desc: str) -> Response:
        return JSONResponse({"error": "bad_gateway", "error_description": desc}, status_code=status)

    async def prm(_: Request) -> Response:
        return JSONResponse({
            "resource": s.resource,
            "authorization_servers": [s.issuer],
            "scopes_supported": ["son:read", "son:write"],
            "bearer_methods_supported": ["header"],
            "resource_name": "aiteam.space",
        })

    async def challenge(_: Request) -> Response:
        tok = s.challenge_token()
        if not tok:
            return PlainTextResponse("not found", status_code=404)
        return Response(tok, media_type="text/plain")

    async def healthz(_: Request) -> Response:
        return PlainTextResponse("ok")

    async def mcp(request: Request) -> Response:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer ") or not auth[7:].strip():
            return unauthorized()
        token = auth[7:].strip()
        claims = await verify_relay_jwt(token, jwks=keys, issuer=s.issuer, resource=s.resource)
        if claims is None:
            return unauthorized("invalid_token", "token is invalid, expired or not for this resource")
        if multi is not None:
            return await multi_box(request, token, claims)
        return await proxy(request, client=http, exchanger=exchanger, subject_token=token,
                           subject_exp=claims.get("exp"),
                           on_invalid_grant=lambda d: unauthorized("invalid_token", d or "sign in again"),
                           on_error=upstream_error)

    async def multi_box(request: Request, token: str, claims: dict) -> Response:
        from .multibox import SCOPE_KEY, Caller, prefetch
        c = Caller(token, claims)
        try:
            await prefetch(router, c)
        except InvalidGrant as e:
            return unauthorized("invalid_token", str(e) or "sign in again")
        except ExchangeError as e:
            return upstream_error(502, str(e))
        request.scope[SCOPE_KEY] = c
        # Only the MCP transport headers reach the SDK; the bearer stays here (it is in the scope as Caller).
        return _Asgi(multi.handle_request)

    routes = [
        Route("/.well-known/oauth-protected-resource", prm, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/mcp", prm, methods=["GET"]),
        Route("/.well-known/openai-apps-challenge", challenge, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/mcp", mcp, methods=["GET", "POST", "DELETE"]),  # a plain Route: no Mount, so no 307 to /mcp/
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with contextlib.AsyncExitStack() as stack:
            if multi is not None:
                await stack.enter_async_context(multi.run())
            yield
        if client is None:
            await http.aclose()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.router.redirect_slashes = False
    app.state.settings, app.state.exchanger, app.state.http = s, exchanger, http
    return app


class _Asgi(Response):
    """Hand the rest of a request to a raw ASGI app (the MCP SDK transport) from a Starlette endpoint."""

    def __init__(self, asgi) -> None:  # noqa: D107 (no body of its own)
        self.asgi = asgi
        self.background = None

    async def __call__(self, scope, receive, send) -> None:
        await self.asgi(scope, receive, send)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="aiteam-relay", description="aiteam.space unified MCP relay (mcp.aiteam.space).")
    p.add_argument("--host", help="bind address (env RELAY_HOST, default 127.0.0.1)")
    p.add_argument("--port", type=int, help="bind port (env RELAY_PORT, default 8790)")
    p.add_argument("--version", action="version", version=f"aiteam-relay {__version__}")
    a = p.parse_args(argv)
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = Settings()
    uvicorn.run(create_app(s), host=a.host or s.host, port=a.port or s.port, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1", log_level="info")


if __name__ == "__main__":
    main()
