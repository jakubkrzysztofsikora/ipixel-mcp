"""Tests for app-layer helpers that don't require the mcp SDK.

The full FastMCP wiring needs `mcp` installed; here we cover the pure pieces:
the ValidationError-to-generic-error guard, the principal contextvar gating, and
the ASGI bearer middleware (which imports no mcp).
"""

import asyncio

import pytest

import ipixel_mcp.app as app
import ipixel_mcp.auth as auth
from ipixel_mcp.auth import Principal, Unauthorized
from ipixel_mcp.safety import ValidationError


def test_guard_maps_validation_error():
    async def boom():
        raise ValidationError("bad color")

    wrapped = app._guard(boom)
    out = asyncio.run(wrapped())
    assert out == {"ok": False, "error": "bad color"}


def test_guard_maps_unauthorized():
    async def boom():
        raise Unauthorized("missing scope")

    out = asyncio.run(app._guard(boom)())
    assert out["ok"] is False and "scope" in out["error"]


def test_guard_passes_through_success():
    async def ok():
        return {"ok": True, "v": 1}

    assert asyncio.run(app._guard(ok)()) == {"ok": True, "v": 1}


def test_guard_preserves_signature_for_schema_infer():
    import inspect

    async def tool(text: str, slot: int = 0) -> dict:
        return {}

    wrapped = app._guard(tool)
    sig = inspect.signature(wrapped)
    # FastMCP infers the tool schema from this signature; it must survive _guard.
    assert list(sig.parameters) == ["text", "slot"]
    assert sig.parameters["text"].annotation is str
    assert sig.parameters["slot"].default == 0


def test_require_scope_gating():
    tok = app._current_principal.set(
        Principal(kind="static", scopes=auth.STATIC_BEARER_SCOPES)
    )
    try:
        app.require_scope(auth.SCOPE_DISPLAY)  # ok
        with pytest.raises(Unauthorized):
            app.require_scope(auth.SCOPE_ADMIN)
    finally:
        app._current_principal.reset(tok)


def test_require_scope_no_principal_allows():
    # Direct unit calls (no ASGI layer) are not gated.
    assert app.current_principal() is None
    app.require_scope(auth.SCOPE_ADMIN)


# --- ASGI middleware ---------------------------------------------------------

class Recorder:
    def __init__(self):
        self.principal = "unset"
        self.started = None

    async def asgi_app(self, scope, receive, send):
        # capture the contextvar visible to downstream app
        self.principal = app.current_principal()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def run_mw(mw, scope):
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request"}

    asyncio.run(mw(scope, receive, send))
    return sent


def _scope(headers, path="/mcp"):
    return {
        "type": "http",
        "path": path,
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }


def test_middleware_401_without_token():
    rec = Recorder()
    mw = app.BearerAuthMiddleware(rec.asgi_app, static_token="secret")
    sent = run_mw(mw, _scope({}))
    assert sent[0]["status"] == 401


def test_middleware_sets_principal_for_valid_static():
    rec = Recorder()
    mw = app.BearerAuthMiddleware(rec.asgi_app, static_token="secret")
    sent = run_mw(mw, _scope({"authorization": "Bearer secret"}))
    assert sent[0]["status"] == 200
    assert rec.principal is not None and rec.principal.kind == "static"


def test_middleware_exempts_healthz():
    rec = Recorder()
    mw = app.BearerAuthMiddleware(rec.asgi_app, static_token="secret")
    sent = run_mw(mw, _scope({}, path="/healthz"))
    assert sent[0]["status"] == 200  # no token required


def test_middleware_resets_contextvar_after():
    rec = Recorder()
    mw = app.BearerAuthMiddleware(rec.asgi_app, static_token="secret")
    run_mw(mw, _scope({"authorization": "Bearer secret"}))
    assert app.current_principal() is None  # cleaned up
