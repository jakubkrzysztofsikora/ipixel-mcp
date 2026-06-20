import asyncio

import pytest

from ipixel_mcp.device import DeviceManager, DeviceError


class FakeInfo:
    width = 64
    height = 16
    led_type = 1
    device_type = "FakePanel"


class FakeClient:
    """Single shared fake; the factory returns this instance on every connect."""

    def __init__(self):
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.connect_fail_first = 0  # number of initial connect() calls that fail
        self.sent = []
        self.mtu_size = 247

    async def connect(self):
        self.connect_calls += 1
        if self.connect_calls <= self.connect_fail_first:
            raise Exception("cannot connect: not connected")

    async def disconnect(self):
        self.disconnect_calls += 1

    def get_device_info(self):
        return FakeInfo()

    async def send_text(self, **kw):
        self.sent.append(kw)


def make_dm(client, **kw):
    return DeviceManager("AA:BB:CC:DD:EE:FF", client_factory=lambda addr: client, **kw)


def run(coro):
    return asyncio.run(coro)


def test_happy_path_connects_caches_and_runs():
    client = FakeClient()
    dm = make_dm(client)

    async def scenario():
        await dm.execute("send_text", lambda c: c.send_text(text="hi"))
        return dm.health()

    health = run(scenario())
    assert client.connect_calls == 1
    assert client.sent == [{"text": "hi"}]
    assert health["connected"] is True
    assert health["device"] == {"width": 64, "height": 16, "led_type": 1}
    assert health["mtu"] == 247


def test_retry_once_on_disconnect_error():
    client = FakeClient()
    dm = make_dm(client)
    calls = {"n": 0}

    async def flaky(c):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("device disconnected")
        await c.send_text(text="ok")

    async def scenario():
        await dm.execute("send_text", flaky)

    run(scenario())
    assert calls["n"] == 2              # retried once
    assert client.connect_calls == 2   # link recycled (reconnected)
    assert client.disconnect_calls == 1
    assert client.sent == [{"text": "ok"}]


def test_timeout_recycles_and_retries():
    client = FakeClient()
    dm = make_dm(client, op_timeout=0.02)
    calls = {"n": 0}

    async def hang_then_ok(c):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.Event().wait()  # never completes -> wait_for cancels it
        await c.send_text(text="late")

    run(dm.execute("send_text", hang_then_ok))
    assert calls["n"] == 2
    assert client.connect_calls == 2
    assert client.sent == [{"text": "late"}]


def test_reconnect_after_initial_connect_failure():
    client = FakeClient()
    client.connect_fail_first = 1
    dm = make_dm(client)

    run(dm.execute("send_text", lambda c: c.send_text(text="x")))
    assert client.connect_calls == 2          # failed once, then succeeded
    assert dm.health()["consecutive_failures"] == 0


def test_circuit_breaker_stops_fast():
    client = FakeClient()
    client.connect_fail_first = 999
    dm = make_dm(client, circuit_threshold=1)

    with pytest.raises(DeviceError):
        run(dm.execute("send_text", lambda c: c.send_text(text="x")))
    # threshold=1 => after the first failure the breaker is open, no retry storm
    assert client.connect_calls == 1
    assert dm.health()["circuit_open"] is True


def test_lock_serialises_operations():
    client = FakeClient()
    dm = make_dm(client)
    events = []

    async def op(tag):
        async def fn(c):
            events.append(f"{tag}-start")
            for _ in range(3):
                await asyncio.sleep(0)
            events.append(f"{tag}-end")
        await dm.execute(f"op-{tag}", fn)

    async def scenario():
        await asyncio.gather(op("A"), op("B"))

    run(scenario())
    # With the single-flight lock, one op fully completes before the other starts.
    assert events in (
        ["A-start", "A-end", "B-start", "B-end"],
        ["B-start", "B-end", "A-start", "A-end"],
    )


def test_ensure_ready_reports_failure_without_raising():
    client = FakeClient()
    client.connect_fail_first = 1
    dm = make_dm(client)
    assert run(dm.ensure_ready()) is False   # first attempt fails
    assert run(dm.ensure_ready()) is True     # second attempt succeeds
