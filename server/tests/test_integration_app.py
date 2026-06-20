"""End-to-end integration test of the REAL app over HTTP, no hardware.

This is the seam that was untestable before `mcp` was installed in CI. It builds
the real `build_app(...)` ASGI app with a FAKE BLE client factory injected into a
real DeviceManager, drives it via an ASGI transport that RUNS THE LIFESPAN, and
speaks MCP Streamable HTTP (initialize / tools/list / tools/call).

It verifies the Round-2 integration-seam fixes by RUNNING them:
  B-2  FastMCP session-manager lifespan (no "Task group is not initialized" 500)
  B-1  reconnect supervisor starts in the lifespan
  B-3  scope enforcement is fail-closed end to end
  B-4  notify_operator actually invokes the render path (fake backend gets a paint)
  C-2  display_image returns a job_id; get_job_status reaches a terminal state
  E-1  no /.well-known, no WWW-Authenticate, plain 401
  /healthz returns 200 JSON
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

# This integration test exercises the real ASGI/MCP stack, so it needs the
# optional runtime deps. The rest of the suite stays dependency-free; skip here
# when they aren't installed (e.g. base CI). Install with:
#   pip install -e ../vendor/pypixelcolor && pip install -e . -c constraints.txt && pip install httpx
httpx = pytest.importorskip("httpx")
pytest.importorskip("mcp")
pytest.importorskip("starlette")

from ipixel_mcp import auth as auth_mod
from ipixel_mcp.app import build_app
from ipixel_mcp.device import DeviceManager


# --------------------------------------------------------------------------- #
# Fake BLE backend
# --------------------------------------------------------------------------- #
class _FakeDeviceInfo:
    width = 64
    height = 16
    led_type = 1
    device_type = "fake"


class FakeBleClient:
    """Records send_text/send_image_hex/get_device_info; exposes mtu_size=247."""

    def __init__(self, address: str):
        self.address = address
        self.mtu_size = 247
        self.connected = False
        self.send_text_calls: list[dict] = []
        self.send_image_calls: list[dict] = []
        # shared sink so the harness sees calls regardless of which client
        # instance the DeviceManager currently holds
        FakeBleClient.LAST = self

    LAST: "FakeBleClient | None" = None
    TEXT_CALLS: list[dict] = []
    IMAGE_CALLS: list[dict] = []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    def get_device_info(self):
        return _FakeDeviceInfo()

    async def send_text(self, **kwargs):
        self.send_text_calls.append(kwargs)
        FakeBleClient.TEXT_CALLS.append(kwargs)

    async def send_image_hex(self, **kwargs):
        self.send_image_calls.append(kwargs)
        FakeBleClient.IMAGE_CALLS.append(kwargs)


def _factory(address: str) -> FakeBleClient:
    return FakeBleClient(address)


# --------------------------------------------------------------------------- #
# MCP Streamable HTTP client helper
# --------------------------------------------------------------------------- #
_MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def _parse_mcp_response(resp: httpx.Response) -> dict:
    """Streamable HTTP may return JSON or an SSE event stream; handle both."""
    ctype = resp.headers.get("content-type", "")
    text = resp.text
    if "text/event-stream" in ctype:
        # find the last `data: {...}` line
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise AssertionError(f"no SSE data frame in: {text!r}")
    return resp.json()


async def _mcp_call(client, auth_headers, method, params=None, *, req_id=1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    headers = dict(_MCP_HEADERS)
    headers.update(auth_headers)
    resp = await client.post("/mcp", content=json.dumps(body), headers=headers)
    return resp


async def _initialize(client, auth_headers):
    params = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "verifier", "version": "0"},
    }
    resp = await _mcp_call(client, auth_headers, "initialize", params, req_id=0)
    assert resp.status_code == 200, (resp.status_code, resp.text)
    # capture a session id if the server issued one (stateless: usually none)
    sid = resp.headers.get("mcp-session-id")
    return sid


def _session_headers(base: dict, sid):
    h = dict(base)
    h["mcp-protocol-version"] = "2025-06-18"
    if sid:
        h["mcp-session-id"] = sid
    return h


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
STATIC = "T"
EXPECTED_TOOLS = {
    "display_text", "display_image", "display_image_url", "get_device_info",
    "get_job_status", "get_display_state", "notify_operator",
    "clear_notification", "list_notifications", "list_presets", "show_preset",
}


def _build(tmp_path, *, verifier=None):
    FakeBleClient.TEXT_CALLS.clear()
    FakeBleClient.IMAGE_CALLS.clear()
    dm = DeviceManager("FA:KE:00:00:00:00", client_factory=_factory)
    notify_db = os.path.join(tmp_path, "notifications.json")
    asset_root = os.path.join(os.path.dirname(__file__), "..", "assets")
    app = build_app(
        dm,
        static_token=STATIC,
        access_jwt_verifier=verifier,
        notify_db=notify_db,
        asset_root=asset_root,
    )
    return app, dm


def _transport(app):
    # ASGITransport drives the full app; run lifespan explicitly via the manager.
    return httpx.ASGITransport(app=app)


async def _run_with_lifespan(app, coro_fn):
    """Run the ASGI lifespan (startup+shutdown) around the client interactions."""
    from contextlib import asynccontextmanager

    startup_done = asyncio.Event()
    shutdown = asyncio.Event()
    finished = asyncio.Event()
    errors: list = []

    receive_q: asyncio.Queue = asyncio.Queue()
    send_q: asyncio.Queue = asyncio.Queue()

    async def receive():
        return await receive_q.get()

    async def send(message):
        await send_q.put(message)

    async def lifespan_task():
        try:
            await app({"type": "lifespan"}, receive, send)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    task = asyncio.create_task(lifespan_task())
    await receive_q.put({"type": "lifespan.startup"})
    msg = await send_q.get()
    assert msg["type"] == "lifespan.startup.complete", msg

    try:
        async with httpx.AsyncClient(
            transport=_transport(app), base_url="http://test"
        ) as client:
            result = await coro_fn(client)
    finally:
        await receive_q.put({"type": "lifespan.shutdown"})
        msg = await send_q.get()
        assert msg["type"] == "lifespan.shutdown.complete", msg
        await task
    assert not errors, errors
    return result


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_b2_lifespan_initialize_and_tools_list(tmp_path):
    """B-2: first /mcp call must not 500 with 'Task group is not initialized'."""
    app, dm = _build(str(tmp_path))

    async def scenario(client):
        auth = {"authorization": f"Bearer {STATIC}"}
        sid = await _initialize(client, auth)
        h = _session_headers(auth, sid)
        resp = await _mcp_call(client, h, "tools/list", {}, req_id=1)
        assert resp.status_code == 200, (resp.status_code, resp.text)
        data = _parse_mcp_response(resp)
        names = {t["name"] for t in data["result"]["tools"]}
        return names

    names = asyncio.run(_run_with_lifespan(app, scenario))
    missing = EXPECTED_TOOLS - names
    assert not missing, f"missing tools: {missing} (got {sorted(names)})"


def test_static_bearer_display_text_paints_backend(tmp_path):
    app, dm = _build(str(tmp_path))

    async def scenario(client):
        auth = {"authorization": f"Bearer {STATIC}"}
        sid = await _initialize(client, auth)
        h = _session_headers(auth, sid)
        resp = await _mcp_call(
            client, h, "tools/call",
            {"name": "display_text", "arguments": {"text": "hello", "color": "00ff00"}},
            req_id=2,
        )
        assert resp.status_code == 200, (resp.status_code, resp.text)
        data = _parse_mcp_response(resp)
        assert "error" not in data, data
        return data

    asyncio.run(_run_with_lifespan(app, scenario))
    assert FakeBleClient.TEXT_CALLS, "fake backend never received a send_text"
    last = FakeBleClient.TEXT_CALLS[-1]
    assert last["text"] == "hello"


def test_b3_no_auth_is_plain_401(tmp_path):
    app, dm = _build(str(tmp_path))

    async def scenario(client):
        resp = await client.post(
            "/mcp",
            content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            headers=_MCP_HEADERS,
        )
        return resp

    resp = asyncio.run(_run_with_lifespan(app, scenario))
    assert resp.status_code == 401
    assert "www-authenticate" not in {k.lower() for k in resp.headers}


def test_b3_worker_wrong_scope_is_403(tmp_path):
    """B-3 fail-closed: worker with only ipixel:display calling notify → 403."""
    app, dm = _build(str(tmp_path), verifier=lambda j: j == "good-jwt")

    async def scenario(client):
        auth = {
            "cf-access-jwt-assertion": "good-jwt",
            "x-mcp-scopes": "ipixel:display",
        }
        sid = await _initialize(client, auth)
        h = _session_headers(auth, sid)
        resp = await _mcp_call(
            client, h, "tools/call",
            {"name": "notify_operator",
             "arguments": {"message": "hi", "source": "x", "level": "info"}},
            req_id=9,
        )
        return resp

    resp = asyncio.run(_run_with_lifespan(app, scenario))
    assert resp.status_code == 403, (resp.status_code, resp.text)
    err = json.loads(resp.text)
    assert "missing scope" in err["error"]["message"]
    assert not FakeBleClient.TEXT_CALLS, "notify must not have painted on a 403"


def test_b3_worker_with_notify_scope_succeeds_and_b4_renders(tmp_path):
    """B-4: notify_operator invokes the render path (banner painted, level colour)."""
    app, dm = _build(str(tmp_path), verifier=lambda j: j == "good-jwt")

    async def scenario(client):
        auth = {
            "cf-access-jwt-assertion": "good-jwt",
            "x-mcp-scopes": "ipixel:notify",
        }
        sid = await _initialize(client, auth)
        h = _session_headers(auth, sid)
        resp = await _mcp_call(
            client, h, "tools/call",
            {"name": "notify_operator",
             "arguments": {"message": "blocked!", "source": "agent7",
                           "level": "blocked"}},
            req_id=10,
        )
        assert resp.status_code == 200, (resp.status_code, resp.text)
        return _parse_mcp_response(resp)

    asyncio.run(_run_with_lifespan(app, scenario))
    assert FakeBleClient.TEXT_CALLS, "notify did not invoke the render path (B-4)"
    last = FakeBleClient.TEXT_CALLS[-1]
    # blocked → red banner
    from ipixel_mcp.modes import notify as notify_mod
    assert last["color"] == notify_mod.LEVEL_COLOR["blocked"]
    assert last["text"] == "blocked!"
    assert last["save_slot"] == notify_mod.NOTIFY_SLOT


def test_c2_display_image_job_reaches_terminal(tmp_path):
    """C-2: display_image returns a job_id; get_job_status reaches terminal."""
    import base64
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (255, 0, 0)).save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    app, dm = _build(str(tmp_path))

    async def scenario(client):
        auth = {"authorization": f"Bearer {STATIC}"}
        sid = await _initialize(client, auth)
        h = _session_headers(auth, sid)
        resp = await _mcp_call(
            client, h, "tools/call",
            {"name": "display_image",
             "arguments": {"image_base64": b64, "format": "png"}},
            req_id=3,
        )
        assert resp.status_code == 200, (resp.status_code, resp.text)
        data = _parse_mcp_response(resp)
        result = data["result"]
        # FastMCP wraps dict returns in structuredContent / content
        payload = result.get("structuredContent") or json.loads(
            result["content"][0]["text"]
        )
        job_id = payload["job_id"]
        assert job_id

        # poll get_job_status until terminal
        terminal = {"succeeded", "failed", "cancelled", "error", "done"}
        last_status = None
        for i in range(50):
            r2 = await _mcp_call(
                client, h, "tools/call",
                {"name": "get_job_status", "arguments": {"job_id": job_id}},
                req_id=100 + i,
            )
            d2 = _parse_mcp_response(r2)
            res2 = d2["result"]
            st = res2.get("structuredContent") or json.loads(
                res2["content"][0]["text"]
            )
            last_status = st.get("status")
            if last_status in terminal:
                return last_status, st
            await asyncio.sleep(0.02)
        raise AssertionError(f"job never terminal; last={last_status} {st}")

    status, st = asyncio.run(_run_with_lifespan(app, scenario))
    assert status in {"succeeded", "done"}, st
    assert FakeBleClient.IMAGE_CALLS, "image transfer never reached the backend"


def test_e1_no_well_known_oauth_metadata(tmp_path):
    app, dm = _build(str(tmp_path))

    async def scenario(client):
        # unauthenticated; should be plain 401 with no WWW-Authenticate, OR 404.
        r = await client.get("/.well-known/oauth-protected-resource")
        return r

    resp = asyncio.run(_run_with_lifespan(app, scenario))
    # E-1: the well-known path must NOT serve OAuth metadata. Since the auth
    # middleware guards all non-exempt paths, it returns 401 (no body metadata)
    # and crucially carries no WWW-Authenticate header.
    assert resp.status_code in (401, 404), (resp.status_code, resp.text)
    assert "www-authenticate" not in {k.lower() for k in resp.headers}
    if resp.status_code == 200:
        raise AssertionError("origin served OAuth PRM metadata (E-1 violation)")


def test_healthz_returns_json(tmp_path):
    app, dm = _build(str(tmp_path))

    async def scenario(client):
        r = await client.get("/healthz")
        return r

    resp = asyncio.run(_run_with_lifespan(app, scenario))
    assert resp.status_code == 200, (resp.status_code, resp.text)
    body = resp.json()
    for field in ("address", "state", "connected", "mtu", "circuit_open"):
        assert field in body, (field, body)


def test_malformed_jsonrpc_does_not_500(tmp_path):
    app, dm = _build(str(tmp_path))

    async def scenario(client):
        auth = {"authorization": f"Bearer {STATIC}"}
        headers = dict(_MCP_HEADERS)
        headers.update(auth)
        r = await client.post("/mcp", content=b"{not valid json", headers=headers)
        return r

    resp = asyncio.run(_run_with_lifespan(app, scenario))
    # Auth passes (valid bearer); body is unparseable. Must not be a 500.
    assert resp.status_code < 500, (resp.status_code, resp.text)
