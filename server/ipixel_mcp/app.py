"""Stateless MCP origin app + plain-bearer ASGI auth (Phase 0).

The origin exposes a **stateless** Streamable HTTP MCP server (review C-1) and
advertises **no OAuth** (review E-1 / Claude Code #59467): unauthenticated calls
get a plain 401 with no ``WWW-Authenticate``. All OAuth lives on the Cloudflare
Worker, which authenticates here via a Cloudflare Access service-token JWT.

This module imports the ``mcp`` SDK; it is runtime code (the pure logic it calls
lives in ``modes/`` and ``safety.py`` and is tested without ``mcp`` installed).
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import json
import logging
import os
from typing import Awaitable, Callable, Literal, Optional

from . import auth as auth_mod
from .auth import AccessJwtVerifier, Principal, Unauthorized, authorize
from .device import DeviceManager
from .display_state import DisplayState
from .jobs import JobRegistry
from .modes import display
from .modes import notify as notify_mod
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


# The authoritative scope gate is the ASGI middleware below: it parses the
# JSON-RPC ``tools/call`` body and enforces TOOL_SCOPES against the Principal
# BEFORE the request reaches FastMCP. This avoids relying on a contextvar
# surviving FastMCP's task-group dispatch (review B-3 — that approach failed
# open). The contextvar + ``require_scope`` below remain as in-tool defense.
_current_principal: "contextvars.ContextVar[Optional[Principal]]" = contextvars.ContextVar(
    "ipixel_principal", default=None
)

# Tool name -> required scope. Used by the middleware (authoritative) and the
# per-tool ``require_scope`` calls (defense in depth). Reads are gated too so the
# "every tool is scope-gated" invariant holds (review MED-1).
TOOL_SCOPES: dict[str, str] = {
    "display_text": auth_mod.SCOPE_DISPLAY,
    "display_image": auth_mod.SCOPE_DISPLAY,
    "display_image_url": auth_mod.SCOPE_DISPLAY,
    "set_brightness": auth_mod.SCOPE_DISPLAY,
    "set_power": auth_mod.SCOPE_DISPLAY,
    "set_orientation": auth_mod.SCOPE_DISPLAY,
    "set_clock_mode": auth_mod.SCOPE_DISPLAY,
    "show_slot": auth_mod.SCOPE_DISPLAY,
    "get_job_status": auth_mod.SCOPE_DISPLAY,
    "get_device_info": auth_mod.SCOPE_DISPLAY,
    "get_display_state": auth_mod.SCOPE_DISPLAY,
    # Destructive Mode-A ops require the admin scope (review M-ANNOT).
    "clear_screen": auth_mod.SCOPE_ADMIN,
    "delete_slot": auth_mod.SCOPE_ADMIN,
    "notify_operator": auth_mod.SCOPE_NOTIFY,
    "clear_notification": auth_mod.SCOPE_NOTIFY,
    "list_notifications": auth_mod.SCOPE_NOTIFY,
    "list_presets": auth_mod.SCOPE_GALLERY,
    "show_preset": auth_mod.SCOPE_GALLERY,
}


def current_principal() -> Optional[Principal]:
    return _current_principal.get()


def require_scope(scope: str) -> None:
    """In-tool defense-in-depth scope check (the middleware is authoritative).

    When no principal is on the contextvar (e.g. a direct unit call, or because
    FastMCP dispatched the tool in a task the contextvar didn't reach) this is a
    no-op: the ASGI middleware has already enforced the scope before dispatch.
    """
    p = _current_principal.get()
    if p is None:
        return
    p.require(scope)


def scope_for_tool_call(body: bytes) -> "tuple[Optional[str], Any]":
    """Parse a JSON-RPC body; return (required_scope, request_id) for tools/call.

    Returns (None, id) for anything that isn't a scoped tools/call (initialize,
    tools/list, notifications, unknown tools, or unparseable bodies) so those
    pass through untouched.
    """
    try:
        msg = json.loads(body)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(msg, dict) or msg.get("method") != "tools/call":
        return None, (msg.get("id") if isinstance(msg, dict) else None)
    name = (msg.get("params") or {}).get("name")
    return TOOL_SCOPES.get(name), msg.get("id")


async def _buffer_body(receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        event = await receive()
        if event["type"] == "http.request":
            chunks.append(event.get("body", b""))
            if not event.get("more_body", False):
                break
        elif event["type"] == "http.disconnect":
            break
    return b"".join(chunks)


def _replay_receive(body: bytes, original_receive=None):
    """Replay the buffered body once, then delegate to ``original_receive``.

    The downstream streamable-HTTP SSE handler watches ``receive`` for a real
    ``http.disconnect`` to know when the client went away. Fabricating a
    ``http.disconnect`` immediately after the body (the old behaviour) made the
    SSE handler tear the response down before it finished writing — every POST
    /mcp response was truncated/never-completed. Delegating to the original
    receive lets a genuine disconnect propagate while keeping the stream alive.
    """
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        if original_receive is not None:
            return await original_receive()
        # No original receive available (unit-test path): block until cancelled
        # rather than fabricating an early disconnect.
        import asyncio as _asyncio

        await _asyncio.Event().wait()

    return receive


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

        # Authoritative scope enforcement: parse the JSON-RPC body and gate
        # tools/call against the granted scopes BEFORE FastMCP dispatch (B-3).
        if scope.get("method") == "POST":
            body = await self._buffer_or_passthrough(scope, receive)
            required, req_id = scope_for_tool_call(body)
            if required is not None and required not in principal.scopes:
                return await self._jsonrpc_forbidden(send, req_id, required)
            receive = _replay_receive(body, receive)

        token = _current_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_principal.reset(token)

    async def _buffer_or_passthrough(self, scope, receive) -> bytes:
        return await _buffer_body(receive)

    @staticmethod
    async def _jsonrpc_forbidden(send, req_id, scope_name: str) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32001, "message": f"forbidden: missing scope {scope_name}"},
        }
        body = json.dumps(payload).encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": body})


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
        format: Literal["png", "gif", "jpeg"],
        slot: int = display.DEFAULT_SLOT,
        resize: Literal["crop", "fit"] = "crop",
        source: str = "display",
    ) -> dict:
        import base64
        require_scope(auth_mod.SCOPE_DISPLAY)
        try:
            data = base64.b64decode(image_base64, validate=True)
        except Exception:  # noqa: BLE001
            raise ValidationError("image_base64 is not valid base64")
        return display.display_image(
            dm, jobs, data=data, fmt=format, slot=slot, resize=resize,
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
        format: Literal["png", "gif", "jpeg"],
        slot: int = display.DEFAULT_SLOT,
        resize: Literal["crop", "fit"] = "crop",
        source: str = "display",
    ) -> dict:
        from .modes import gallery as gallery_mod
        require_scope(auth_mod.SCOPE_DISPLAY)
        decoded = await gallery_mod.fetch_image_url(image_url, format)
        return display.display_image(
            dm, jobs, data=decoded.payload.data, fmt=format, slot=slot, resize=resize,
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

    # -- Mode A: device controls ---------------------------------------------

    @mcp.tool(description="Set the panel brightness (0-100).")
    @_guard
    async def set_brightness(level: int) -> dict:  # noqa: ANN001
        require_scope(auth_mod.SCOPE_DISPLAY)
        return await display.set_brightness(dm, level)

    @mcp.tool(description="Turn the panel on or off.")
    @_guard
    async def set_power(on: bool) -> dict:  # noqa: ANN001
        require_scope(auth_mod.SCOPE_DISPLAY)
        return await display.set_power(dm, on)

    @mcp.tool(description="Set the panel orientation (0=0°, 1=90°, 2=180°, 3=270°).")
    @_guard
    async def set_orientation(orientation: int) -> dict:  # noqa: ANN001
        require_scope(auth_mod.SCOPE_DISPLAY)
        return await display.set_orientation(dm, orientation)

    @mcp.tool(description="Switch the panel to clock mode.")
    @_guard
    async def set_clock_mode(  # noqa: ANN001
        style: int = 1, show_date: bool = True, format_24: bool = True
    ) -> dict:
        require_scope(auth_mod.SCOPE_DISPLAY)
        return await display.set_clock_mode(
            dm, style=style, show_date=show_date, format_24=format_24
        )

    @mcp.tool(description="Show a previously saved screen slot (0-20).")
    @_guard
    async def show_slot(number: int) -> dict:  # noqa: ANN001
        require_scope(auth_mod.SCOPE_DISPLAY)
        return await display.show_slot(dm, number)

    @mcp.tool(
        description="DESTRUCTIVE: wipe the device's saved ROM/settings. Requires admin.",
        annotations={"destructiveHint": True},
    )
    @_guard
    async def clear_screen() -> dict:  # noqa: ANN001
        require_scope(auth_mod.SCOPE_ADMIN)
        return await display.clear_screen(dm)

    @mcp.tool(
        description="DESTRUCTIVE: delete a saved screen slot (0-20). Requires admin.",
        annotations={"destructiveHint": True},
    )
    @_guard
    async def delete_slot(n: int) -> dict:  # noqa: ANN001
        require_scope(auth_mod.SCOPE_ADMIN)
        return await display.delete_slot(dm, n)

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
        level: Literal["info", "warn", "blocked"] = "info",
        ttl_seconds: float = 300.0,
    ) -> dict:
        require_scope(auth_mod.SCOPE_NOTIFY)
        return await notifications.notify_operator(
            message=message, level=level, source=source, ttl_seconds=ttl_seconds
        )

    @mcp.tool(
        description=(
            "Clear notifications and restore the prior display. Pass notification_id "
            "for one, or source to clear only that agent's banners (use this in a "
            "Claude Code Stop hook so it clears just its own), or neither to clear all."
        ),
        annotations={"destructiveHint": True},
    )
    @_guard
    async def clear_notification(  # noqa: ANN001
        notification_id: Optional[str] = None,
        source: Optional[str] = None,
    ) -> dict:
        require_scope(auth_mod.SCOPE_NOTIFY)
        return notifications.clear_notification(notification_id, source=source)

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
    async def list_presets(  # noqa: ANN001
        category: Optional[Literal["image", "ascii", "text"]] = None,
    ) -> dict:
        require_scope(auth_mod.SCOPE_GALLERY)
        return gallery.list_presets(category)

    @mcp.tool(
        description="Render a curated preset by id. The cheap, model-friendly image path.",
        annotations={"destructiveHint": False},
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

    # B-4: actually paint the banner on the board when a notification arrives.
    # Note (review T-1): this goes through the single BLE lock, so an in-flight
    # 120 s image job delays the banner — accepted limitation, tracked in PLAN.
    async def _render_notification(n) -> None:  # noqa: ANN001
        await display.display_text(
            dm,
            text=n.message,
            color=notify_mod.LEVEL_COLOR.get(n.level, "ffffff"),
            slot=notify_mod.NOTIFY_SLOT,
        )

    notifications = notifications or NotificationStore(
        path=notify_db, display_state=display_state, render=_render_notification
    )
    gallery = gallery or Gallery(asset_root)

    mcp = build_mcp(
        dm, jobs=jobs, display_state=display_state,
        notifications=notifications, gallery=gallery,
    )
    mcp_app = mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def _lifespan(_app):
        # B-2: run FastMCP's StreamableHTTP session manager (its task group must
        # start, or the first /mcp request 500s). B-1: start the reconnect
        # supervisor. T-2: drain jobs + close the link cleanly on shutdown.
        async with mcp.session_manager.run():
            dm.start_supervisor()
            try:
                yield
            finally:
                await jobs.drain()
                await dm.close()

    async def healthz(request):  # noqa: ANN001
        return JSONResponse(dm.health())

    app = Starlette(routes=[Route("/healthz", healthz)], lifespan=_lifespan)
    app.mount("/", mcp_app)
    return BearerAuthMiddleware(
        app, static_token=static_token, access_jwt_verifier=access_jwt_verifier
    )
