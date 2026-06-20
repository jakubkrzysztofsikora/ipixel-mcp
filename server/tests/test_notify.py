import asyncio
import os

import pytest

from ipixel_mcp.display_state import DisplayState, KIND_NOTIFY, KIND_IDLE
from ipixel_mcp.modes.notify import NotificationStore
from ipixel_mcp.safety import ValidationError


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make_store(tmp_path, clock=None, render=None):
    clock = clock or Clock()
    ds = DisplayState(clock=clock)
    store = NotificationStore(
        path=str(tmp_path / "notify.json"),
        display_state=ds,
        render=render,
        clock=clock,
    )
    return store, ds, clock


def test_notify_info_sets_base_volatile(tmp_path):
    store, ds, _ = make_store(tmp_path)

    async def scenario():
        rendered = []

        async def render(n):
            rendered.append(n.id)

        store._render = render
        res = await store.notify_operator(message="hi", source="cc", level="info")
        assert res["ok"] and res["slot"] == 0  # volatile (H-FLASH)
        assert rendered == [res["notification_id"]]
        assert len(store.list_notifications()["notifications"]) == 1

    asyncio.run(scenario())


def test_blocked_preempts_and_clear_restores(tmp_path):
    store, ds, _ = make_store(tmp_path)

    async def scenario():
        ds.set_base(owner="disp", summary="something")
        res = await store.notify_operator(
            message="need input", source="agent-1", level="blocked", ttl_seconds=100
        )
        assert ds.get_display_state()["kind"] == KIND_NOTIFY
        store.clear_notification(res["notification_id"])
        assert ds.get_display_state()["summary"] == "something"

    asyncio.run(scenario())


def test_validation(tmp_path):
    store, _, _ = make_store(tmp_path)

    async def scenario():
        with pytest.raises(ValidationError):
            await store.notify_operator(message="x" * 41, source="s")
        with pytest.raises(ValidationError):
            await store.notify_operator(message="ok", source="", level="info")
        with pytest.raises(ValidationError):
            await store.notify_operator(message="ok", source="s", level="bogus")
        with pytest.raises(ValidationError):
            await store.notify_operator(message="ok", source="s", ttl_seconds=0)

    asyncio.run(scenario())


def test_ttl_auto_expiry(tmp_path):
    clk = Clock()
    store, ds, _ = make_store(tmp_path, clock=clk)

    async def scenario():
        await store.notify_operator(message="hi", source="s", ttl_seconds=10)
        assert len(store.list_notifications()["notifications"]) == 1
        clk.t = 11
        # expiry happens on the next read/op so a missed Stop can't strand the board
        assert store.list_notifications()["notifications"] == []

    asyncio.run(scenario())


def test_clear_unknown_id_noop(tmp_path):
    store, _, _ = make_store(tmp_path)
    res = store.clear_notification("does-not-exist")
    assert res["ok"] and res["cleared"] == 0


def test_persistence_survives_restart(tmp_path):
    path = str(tmp_path / "n.json")

    async def scenario():
        clk = Clock()
        ds1 = DisplayState(clock=clk)
        s1 = NotificationStore(path=path, display_state=ds1, clock=clk)
        res = await s1.notify_operator(
            message="reboot me", source="s", level="blocked", ttl_seconds=1000
        )
        nid = res["notification_id"]

        # New process: fresh store + fresh display_state, same file + clock time.
        ds2 = DisplayState(clock=clk)
        s2 = NotificationStore(path=path, display_state=ds2, clock=clk)
        ids = [n["notification_id"] for n in s2.list_notifications()["notifications"]]
        assert nid in ids
        # blocked notification re-preempts on load
        assert ds2.get_display_state()["kind"] == KIND_NOTIFY
        # clear-of-known-id works across the "restart"
        s2.clear_notification(nid)
        assert s2.list_notifications()["notifications"] == []

    asyncio.run(scenario())


def test_expired_not_reloaded(tmp_path):
    path = str(tmp_path / "n.json")

    async def scenario():
        clk = Clock()
        s1 = NotificationStore(path=path, clock=clk)
        await s1.notify_operator(message="short", source="s", ttl_seconds=5)
        clk.t = 100
        s2 = NotificationStore(path=path, clock=clk)
        assert s2.list_notifications()["notifications"] == []

    asyncio.run(scenario())
