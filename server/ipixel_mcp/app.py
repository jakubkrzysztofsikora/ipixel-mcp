"""Stateless MCP origin app + plain-bearer ASGI auth (Phase 0).

The origin exposes a **stateless** Streamable HTTP MCP server (review C-1) and
advertises **no OAuth** (review E-1 / Claude Code #59467): unauthenticated calls
get a plain 401 with no ``WWW-Authenticate``. All OAuth lives on the Cloudflare
Worker, which authenticates here via a Cloudflare Access service-token JWT.

This module imports the ``mcp`` SDK; it is runtime code (the pure logic it calls
lives in ``modes/`` and ``safety.py`` and is tested without ``mcp`` installed).
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import json
import logging
import os
from typing import Awaitable, Callable, Optional

from . import auth as auth_mod
from .auth import AccessJwtVerifier, Principal, Unauthorized, authorize
from .device import DeviceManager
from .display_state import DisplayState
from .jobs import JobRegistry
from .modes import display
from .modes.gallery import Gallery
from .modes.notify import NotificationStore
from .safety import ValidationError

logger = logging.getLogger("ipixel_mcp.app")

# Paths reachable without a bearer (loopback-only health check for cloudflared).
EXEMPT_PATHS = frozenset({"/healthz"})

# Where the persisted notification queue lives (override via env).
DEFAULT_NOTIFY_DB = os.environ.get(
    "IPIXEL_NOTIFY_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "var", "notifications.json"),
)
DEFAULT_ASSET_ROOT = os.environ.get(
    "IPIXEL_ASSET_ROOT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets"),
)


# Per-request principal, set by the auth middleware and read by scope-gated tools.
# A contextvar carries it across the FastMCP tool boundary without threading it
# through every signature.
_current_principal: "contextvars.ContextVar[Optional[Principal]]" = contextvars.ContextVar(
    "ipixel_principal", default=None
)


def current_principal() -> Optional[Principal]:
    return _current_principal.get()


def require_scope(scope: str) -> None:
    """Raise Unauthorized unless the current principal holds ``scope``.

    Used to gate destructive/admin tools (review M-ANNOT). When no principal is
    present (e.g. a direct unit call), it is treated as authorized so the pure
    logic stays testable; the ASGI layer guarantees a principal in production.
    """
    p = _current_principal.get()
    if p is None:
        return
    p.require(scope)


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
        token = _current_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_principal.reset(token)


def _guard(fn):
    """Wrap a tool body so ValidationError → generic client error (F-9).

    Preserves the wrapped function's signature so FastMCP can still infer the
    tool's input schema from the original type-annotated parameters (FastMCP
    follows ``__wrapped__`` via ``inspect.signature``).
    """
    @functools.wraps(fn)
    async def _inner(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ValidationError as exc:
            return {"ok": False, "error": str(exc)}
        except Unauthorized as exc:
            return {"ok": False, "error": str(exc)}

    # Explicitly carry the signature too (belt-and-suspenders for schema infer).
    _inner.__signature__ = inspect.signature(fn)
    return _inner


def build_mcp(
    dm: DeviceManager,
    *,
    jobs: JobRegistry,
    display_state: DisplayState,
    notifications: NotificationStore,
    gallery: Gallery,
):
    """Create the FastMCP server with all Phase 0-2 tools + gallery resources.

    Reads are marked ``readOnlyHint``; ``clear``-style ops are marked
    ``destructiveHint``. Every tool is scope-gated on the Principal carried by
    the auth middleware (display/notify/gallery scopes; the admin scope is
    reserved for the destructive Mode-A clear/delete tools added later). No
    confirm-token args (a model can't obtain one — review M-ANNOT).
    """
    from mcp.server.fastmcp import FastMCP  # lazy import

    mcp = FastMCP("ipixel", stateless_http=True)

    # -- Mode A: display ------------------------------------------------------

    @mcp.tool(
        description=(
            "Display short text on the iPixel LED matrix. Colours are 6-digit hex "
            "(e.g. 'ff8800'). save_slot=0 is volatile (recommended); a non-zero slot "
            "persists to flash. Do not use this for images."
        )
    )
    @_guard
    async def display_text(  # noqa: ANN001
        text: str,
        color: str = display.DEFAULT_COLOR,
        bg_color: Optional[str] = None,
        font: str = display.DEFAULT_FONT,
        animation: int = display.DEFAULT_ANIMATION,
        speed: int = display.DEFAULT_SPEED,
        rainbow: int = display.DEFAULT_RAINBOW,
        slot: int = display.DEFAULT_SLOT,
    ) -> dict:
        require_scope(auth_mod.SCOPE_DISPLAY)
        result = await display.display_text(
            dm, text=text, color=color, bg_color=bg_color, font=font,
            animation=animation, speed=speed, rainbow=rainbow, slot=slot,
        )
        display_state.set_base(owner="display", summary=f"text: {text[:32]}")
        return result

    @mcp.tool(
        description=(
            "Display an image on the board from raw base64 bytes. MACHINE/PASSTHROUGH "
            "ONLY: do NOT base64-encode an image yourself (huge token cost + timeout). "
            "Models should use show_preset or display_image_url instead. Returns a "
            "job_id; poll get_job_status. format is one of png/gif/jpeg."
        )
    )
    @_guard
    async def display_image(  # noqa: ANN001
        image_base64: str,
        format: str,
        slot: int = display.DEFAULT_SLOT,
        source: str = "display",
    ) -> dict:
        import base64
        require_scope(auth_mod.SCOPE_DISPLAY)
        try:
            data = base64.b64decode(image_base64, validate=True)
        except Exception:  # noqa: BLE001
            raise ValidationError("image_base64 is not valid base64")
        return display.display_image(
            dm, jobs, data=data, fmt=format, slot=slot,
            source=source, display_state=display_state,
        )

    @mcp.tool(
        description=(
            "Display an image fetched from an https URL (server-side, SSRF-guarded "
            "and size-capped). Model-friendly alternative to base64. Returns a job_id."
        )
    )
    @_guard
    async def display_image_url(  # noqa: ANN001
        image_url: str,
        format: str,
        slot: int = display.DEFAULT_SLOT,
        source: str = "display",
    ) -> dict:
        from .modes import gallery as gallery_mod
        require_scope(auth_mod.SCOPE_DISPLAY)
        decoded = await gallery_mod.fetch_image_url(image_url, format)
        return display.display_image(
            dm, jobs, data=decoded.payload.data, fmt=format, slot=slot,
            source=source, display_state=display_state,
        )

    @mcp.tool(
        description="Check the status/result of an async display job.",
        annotations={"readOnlyHint": True},
    )
    @_guard
    async def get_job_status(job_id: str) -> dict:  # noqa: ANN001
        return jobs.status(job_id)

    @mcp.tool(
        description="Read the connected panel's dimensions and capabilities.",
        annotations={"readOnlyHint": True},
    )
    @_guard
    async def get_device_info() -> dict:  # noqa: ANN001
        return await display.get_device_info(dm)

    @mcp.tool(
        description="Read who currently owns the display and its TTL (state stack top).",
        annotations={"readOnlyHint": True},
    )
    @_guard
    async def get_display_state() -> dict:  # noqa: ANN001
        return display.get_display_state(display_state)

    # -- Mode B: notify -------------------------------------------------------

    @mcp.tool(
        description=(
            "Show an operator-attention banner (ambient/secondary alert). message<=40 "
            "chars; level info|warn|blocked; source labels the agent/session (required); "
            "ttl_seconds auto-expires. 'blocked' preempts the current display. Volatile."
        )
    )
    @_guard
    async def notify_operator(  # noqa: ANN001
        message: str,
        source: str,
        level: str = "info",
        ttl_seconds: float = 300.0,
    ) -> dict:
        require_scope(auth_mod.SCOPE_NOTIFY)
        return await notifications.notify_operator(
            message=message, level=level, source=source, ttl_seconds=ttl_seconds
        )

    @mcp.tool(
        description="Clear a notification (or all if id omitted); restores prior display.",
        annotations={"destructiveHint": True},
    )
    @_guard
    async def clear_notification(notification_id: Optional[str] = None) -> dict:  # noqa: ANN001
        require_scope(auth_mod.SCOPE_NOTIFY)
        return notifications.clear_notification(notification_id)

    @mcp.tool(
        description="List active notifications (id, level, message, source, age).",
        annotations={"readOnlyHint": True},
    )
    @_guard
    async def list_notifications() -> dict:  # noqa: ANN001
        require_scope(auth_mod.SCOPE_NOTIFY)
        return notifications.list_notifications()

    # -- Mode C: gallery ------------------------------------------------------

    @mcp.tool(
        description="List curated presets (optionally filter by category image|ascii|text).",
        annotations={"readOnlyHint": True},
    )
    @_guard
    async def list_presets(category: Optional[str] = None) -> dict:  # noqa: ANN001
        require_scope(auth_mod.SCOPE_GALLERY)
        return gallery.list_presets(category)

    @mcp.tool(
        description="Render a curated preset by id. The cheap, model-friendly image path.",
    )
    @_guard
    async def show_preset(preset_id: str, slot: int = display.DEFAULT_SLOT) -> dict:  # noqa: ANN001
        require_scope(auth_mod.SCOPE_GALLERY)
        preset = gallery.get(preset_id)
        if preset.category in ("ascii", "text"):
            text = gallery.load_text(preset)
            render = preset.render or {}
            result = await display.display_text(
                dm, text=text.strip()[:200],
                color=render.get("color", display.DEFAULT_COLOR),
                font=render.get("font", display.DEFAULT_FONT),
                speed=render.get("speed", display.DEFAULT_SPEED),
                slot=slot,
            )
            display_state.set_base(owner="gallery", summary=f"preset {preset_id}")
            return result
        # image preset → async job
        data = gallery.load_image_bytes(preset)
        return display.display_image(
            dm, jobs, data=data, fmt="png", slot=slot,
            source="gallery", display_state=display_state,
        )

    # -- gallery as MCP resources (review M-RES) ------------------------------

    for desc in gallery.resources():
        def _make_reader(uri: str):
            async def _read() -> str:
                return gallery.read_resource(uri)
            return _read

        mcp.resource(
            desc["uri"],
            name=desc["name"],
            description=desc["description"],
            mime_type=desc["mimeType"],
        )(_make_reader(desc["uri"]))

    return mcp


def build_app(
    dm: DeviceManager,
    *,
    static_token: Optional[str],
    access_jwt_verifier: Optional[AccessJwtVerifier] = None,
    jobs: Optional[JobRegistry] = None,
    display_state: Optional[DisplayState] = None,
    notifications: Optional[NotificationStore] = None,
    gallery: Optional[Gallery] = None,
    notify_db: str = DEFAULT_NOTIFY_DB,
    asset_root: str = DEFAULT_ASSET_ROOT,
):
    """Build the ASGI app: stateless MCP under auth, plus an unauthenticated /healthz."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    jobs = jobs or JobRegistry()
    display_state = display_state or DisplayState()
    notifications = notifications or NotificationStore(
        path=notify_db, display_state=display_state
    )
    gallery = gallery or Gallery(asset_root)

    mcp = build_mcp(
        dm, jobs=jobs, display_state=display_state,
        notifications=notifications, gallery=gallery,
    )
    mcp_app = mcp.streamable_http_app()

    async def healthz(request):  # noqa: ANN001
        return JSONResponse(dm.health())

    app = Starlette(routes=[Route("/healthz", healthz)])
    app.mount("/", mcp_app)
    return BearerAuthMiddleware(
        app, static_token=static_token, access_jwt_verifier=access_jwt_verifier
    )
