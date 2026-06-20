"""Tests for the authoritative ASGI scope-enforcement middleware (review B-3)."""

import asyncio
import json

from ipixel_mcp import app as app_mod
from ipixel_mcp import auth as auth_mod
from ipixel_mcp.app import (
    BearerAuthMiddleware,
    scope_for_tool_call,
    _buffer_body,
    _replay_receive,
)

STATIC = "tok"


def _scope(method="POST", path="/mcp", headers=None):
    raw = []
    for k, v in (headers or {}).items():
        raw.append((k.encode(), v.encode()))
    return {"type": "http", "method": method, "path": path, "headers": raw}


def _recv_with_body(body: bytes):
    return _replay_receive(body)


class _Recorder:
    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        # drain the (replayed) body to prove it survives
        self.body = await _buffer_body(receive)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _run_request(mw, scope, body):
    sent = []

    async def send(ev):
        sent.append(ev)

    asyncio.run(mw(scope, _recv_with_body(body), send))
    return sent


def test_scope_for_tool_call_maps_and_ignores():
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "notify_operator"}}).encode()
    assert scope_for_tool_call(body) == (auth_mod.SCOPE_NOTIFY, 1)
    # non-tools/call passes through
    assert scope_for_tool_call(json.dumps({"method": "tools/list", "id": 2}).encode()) == (None, 2)
    # unparseable passes through
    assert scope_for_tool_call(b"not json") == (None, None)
    # unknown tool → no required scope
    body2 = json.dumps({"method": "tools/call", "id": 3, "params": {"name": "nope"}}).encode()
    assert scope_for_tool_call(body2) == (None, 3)


def test_static_bearer_allows_in_scope_tool():
    rec = _Recorder()
    mw = BearerAuthMiddleware(rec, static_token=STATIC)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "display_text"}}).encode()
    scope = _scope(headers={"authorization": f"Bearer {STATIC}"})
    _run_request(mw, scope, body)
    assert rec.called and rec.body == body  # body replayed intact


def test_worker_missing_scope_is_forbidden():
    rec = _Recorder()
    # worker principal with only display scope tries to call a notify tool
    mw = BearerAuthMiddleware(
        rec, static_token=STATIC, access_jwt_verifier=lambda j: j == "ok"
    )
    body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": "notify_operator"}}).encode()
    scope = _scope(headers={
        "cf-access-jwt-assertion": "ok",
        "x-mcp-scopes": "ipixel:display",
    })
    sent = _run_request(mw, scope, body)
    assert not rec.called
    start = sent[0]
    assert start["status"] == 403
    err = json.loads(sent[1]["body"])
    assert err["id"] == 7 and "missing scope" in err["error"]["message"]


def test_unauthenticated_is_plain_401_no_www_authenticate():
    rec = _Recorder()
    mw = BearerAuthMiddleware(rec, static_token=STATIC)
    sent = _run_request(mw, _scope(headers={}), b"{}")
    assert not rec.called
    assert sent[0]["status"] == 401
    # E-1: no WWW-Authenticate header (origin advertises no OAuth)
    hdr_names = [k.decode().lower() for k, _ in sent[0]["headers"]]
    assert "www-authenticate" not in hdr_names


def test_healthz_is_exempt():
    rec = _Recorder()
    mw = BearerAuthMiddleware(rec, static_token=STATIC)
    _run_request(mw, _scope(method="GET", path="/healthz"), b"")
    assert rec.called
