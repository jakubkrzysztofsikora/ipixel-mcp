"""Regression tests for the post-review fixes (device MTU/markers, notify source)."""

import asyncio

from ipixel_mcp import device as device_mod
from ipixel_mcp.device import DeviceManager, DeviceError, _looks_like_disconnect
from ipixel_mcp.modes.notify import NotificationStore


# ---- H-WEDGE: ACK-timeout errors must be treated as link-recyclable ----------

def test_ack_timeout_errors_are_disconnect_class():
    assert _looks_like_disconnect(RuntimeError("cur12k_no_answer: no ack from device"))
    assert _looks_like_disconnect(Exception("operation timed out"))
    # an unrelated error is NOT recycled
    assert not _looks_like_disconnect(ValueError("bad colour"))


# ---- H-MTU: gate refuses on a known-degraded link, allows unknown -------------

def test_assert_mtu_ok():
    dm = DeviceManager("X", client_factory=lambda a: None)
    dm._mtu = None
    dm.assert_mtu_ok()  # unknown MTU is allowed (best effort)
    dm._mtu = device_mod.EXPECTED_MTU
    dm.assert_mtu_ok()  # healthy
    dm._mtu = 23
    try:
        dm.assert_mtu_ok()
        assert False, "expected refusal on degraded MTU"
    except DeviceError:
        pass


def test_mtu_read_reaches_nested_bleak_client():
    class Bleak:
        mtu_size = 185

    class Session:
        _client = Bleak()

    class Async:
        _session = Session()

    dm = DeviceManager("X", client_factory=lambda a: None)
    dm._check_mtu(Async())
    assert dm._mtu == 185  # read off ._session._client, not the AsyncClient


# ---- TOP-2: clear_notification by source clears only that agent ---------------

def _store(tmp_path):
    return NotificationStore(path=str(tmp_path / "n.json"))


def test_clear_by_source_only_targets_that_source(tmp_path):
    store = _store(tmp_path)
    asyncio.run(store.notify_operator(message="a", level="info", source="agent-1", ttl_seconds=100))
    asyncio.run(store.notify_operator(message="b", level="info", source="agent-2", ttl_seconds=100))
    res = store.clear_notification(source="agent-1")
    assert res["cleared"] == 1
    remaining = store.list_notifications()["notifications"]
    assert len(remaining) == 1 and remaining[0]["source"] == "agent-2"


def test_clear_all_and_unknown_id(tmp_path):
    store = _store(tmp_path)
    asyncio.run(store.notify_operator(message="a", level="info", source="x", ttl_seconds=100))
    assert store.clear_notification("does-not-exist")["cleared"] == 0  # no-op
    assert store.clear_notification()["cleared"] == 1                   # clear all


def test_render_callback_invoked_on_notify(tmp_path):
    painted = []

    async def render(n):
        painted.append((n.level, n.message))

    store = NotificationStore(path=str(tmp_path / "n.json"), render=render)
    asyncio.run(store.notify_operator(message="hi", level="blocked", source="x", ttl_seconds=50))
    assert painted == [("blocked", "hi")]
