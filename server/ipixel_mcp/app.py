"""Stateless MCP origin app + plain-bearer ASGI auth (Phase 0).

The origin exposes a **stateless** Streamable HTTP MCP server (review C-1) and
advertises **no OAuth** (review E-1 / Claude Code #59467): unauthenticated calls
get a plain 401 with no ``WWW-Authenticate``. All OAuth lives on the Cloudflare
Worker, which authenticates here via a Cloudflare Access service-token JWT.

This module imports the ``mcp`` SDK; it is runtime code (the pure logic it calls
lives in ``modes/`` and ``safety.py`` and is tested without ``mcp`` installed).
"""

from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable, Optional

from .auth import AccessJwtVerifier, Unauthorized, authorize
from .device import DeviceManager
from .modes import display
from .safety import ValidationError

logger = logging.getLogger("ipixel_mcp.app")

# Paths reachable without a bearer (loopback-only health check for cloudflared).
EXEMPT_PATHS = frozenset({"/healthz"})


class BearerAuthMiddleware:
    """Pure-ASGI auth: verified Access JWT (Worker) or static bearer, else plain 401."""

    def __init__(
        self,
        app,
        *,
        static_token: Optional[str],
        access_jwt_verifier: Optional[AccessJwtVerifier] = None,
    ) -> None:
        self.app = app
        self._static_token = static_token
        self._verifier = access_jwt_verifier

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("path", "") in EXEMPT_PATHS:
            return await self.app(scope, receive, send)

        headers = {k.decode("latin1"): v.decode("latin1") for k, v in scope.get("headers", [])}
        try:
            principal = authorize(
                headers,
                static_token=self._static_token,
                access_jwt_verifier=self._verifier,
            )
        except Unauthorized:
            body = json.dumps({"error": "unauthorized"}).encode()
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": body})
            return

        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)


def build_mcp(dm: DeviceManager):
    """Create the FastMCP server with Phase 0 tools registered."""
    from mcp.server.fastmcp import FastMCP  # lazy import

    mcp = FastMCP("ipixel", stateless_http=True)

    @mcp.tool(
        description=(
            "Display short text on the iPixel LED matrix. Colours are 6-digit hex "
            "(e.g. 'ff8800'). save_slot=0 is volatile (recommended); a non-zero slot "
            "persists to flash. Do not use this for images."
        )
    )
    async def display_text(  # noqa: ANN001 - schema inferred by FastMCP
        text: str,
        color: str = display.DEFAULT_COLOR,
        bg_color: Optional[str] = None,
        font: str = display.DEFAULT_FONT,
        animation: int = display.DEFAULT_ANIMATION,
        speed: int = display.DEFAULT_SPEED,
        rainbow: int = display.DEFAULT_RAINBOW,
        slot: int = display.DEFAULT_SLOT,
    ) -> dict:
        try:
            return await display.display_text(
                dm,
                text=text,
                color=color,
                bg_color=bg_color,
                font=font,
                animation=animation,
                speed=speed,
                rainbow=rainbow,
                slot=slot,
            )
        except ValidationError as exc:
            # Safe to surface: these are user-facing input messages.
            return {"ok": False, "error": str(exc)}

    @mcp.tool(description="Read the connected panel's dimensions and capabilities.")
    async def get_device_info() -> dict:
        return await display.get_device_info(dm)

    return mcp


def build_app(
    dm: DeviceManager,
    *,
    static_token: Optional[str],
    access_jwt_verifier: Optional[AccessJwtVerifier] = None,
):
    """Build the ASGI app: stateless MCP under auth, plus an unauthenticated /healthz."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    mcp = build_mcp(dm)
    mcp_app = mcp.streamable_http_app()

    async def healthz(request):  # noqa: ANN001
        return JSONResponse(dm.health())

    app = Starlette(routes=[Route("/healthz", healthz)])
    app.mount("/", mcp_app)
    return BearerAuthMiddleware(
        app, static_token=static_token, access_jwt_verifier=access_jwt_verifier
    )
