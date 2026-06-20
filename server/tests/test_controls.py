"""Tests for the Mode A control tools + G-4 (validate-before-execute) + resize."""

import asyncio

import pytest

from ipixel_mcp.device import DeviceManager
from ipixel_mcp.modes import display
from ipixel_mcp.safety import ValidationError


class FakeInfo:
    width = 64
    height = 16
    led_type = 1
    device_type = "Fake"


class FakeClient:
    def __init__(self):
        self.calls = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    def get_device_info(self):
        return FakeInfo()

    def _rec(self, name):
        def f(*a, **k):
            self.calls.append((name, a, k))
            async def _coro():
                return None
            return _coro()
        return f

    def __getattr__(self, name):
        # any command method records its call
        return self._rec(name)


def _dm():
    client = FakeClient()
    return DeviceManager("X", client_factory=lambda a: client), client


def test_set_brightness_bounds_and_call():
    dm, client = _dm()
    asyncio.run(display.set_brightness(dm, 50))
    assert ("set_brightness", (50,), {}) in client.calls
    with pytest.raises(ValidationError):
        asyncio.run(display.set_brightness(dm, 200))


def test_set_power_and_orientation_and_show_slot():
    dm, client = _dm()
    asyncio.run(display.set_power(dm, True))
    asyncio.run(display.set_orientation(dm, 2))
    asyncio.run(display.show_slot(dm, 3))
    names = [c[0] for c in client.calls]
    assert names == ["set_power", "set_orientation", "show_slot"]
    with pytest.raises(ValidationError):
        asyncio.run(display.set_orientation(dm, 9))


def test_clock_mode_passes_kwargs():
    dm, client = _dm()
    asyncio.run(display.set_clock_mode(dm, style=2, show_date=False, format_24=True))
    name, a, k = client.calls[-1]
    assert name == "set_clock_mode" and k["style"] == 2 and k["show_date"] is False


def test_clear_and_delete():
    dm, client = _dm()
    asyncio.run(display.clear_screen(dm))
    asyncio.run(display.delete_slot(dm, 4))
    names = [c[0] for c in client.calls]
    assert "clear" in names and ("delete", (4,), {}) in client.calls


def test_display_text_validation_raises_before_execute():
    # G-4: a bad colour must raise ValidationError (not be swallowed into a
    # generic DeviceError inside dm.execute).
    dm, client = _dm()
    with pytest.raises(ValidationError):
        asyncio.run(display.display_text(dm, text="hi", color="nothex"))
    # nothing was sent to the device
    assert client.calls == []


def test_display_image_resize_rejects_unsupported():
    from ipixel_mcp.jobs import JobRegistry
    dm, _ = _dm()
    with pytest.raises(ValidationError):
        # validation is synchronous, before any job is submitted
        display.display_image(dm, JobRegistry(), data=_tiny_png(), fmt="png", resize="stretch")


def test_display_image_resize_threaded_through():
    from ipixel_mcp.jobs import JobRegistry
    dm, client = _dm()
    jobs = JobRegistry()
    png = _tiny_png()

    async def scenario():
        # submit must run inside a loop (it schedules a task)
        res = display.display_image(dm, jobs, data=png, fmt="png", resize="fit")
        assert "job_id" in res
        await jobs.wait(res["job_id"])

    asyncio.run(scenario())
    sent = [c for c in client.calls if c[0] == "send_image_hex"]
    assert sent and sent[0][2].get("resize_method") == "fit"


def _tiny_png():
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, format="PNG")
    return buf.getvalue()
