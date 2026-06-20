import asyncio

import pytest

from ipixel_mcp.modes import display
from ipixel_mcp.device import DeviceManager
from ipixel_mcp.jobs import JobRegistry, SUCCEEDED, FAILED
from ipixel_mcp.display_state import DisplayState, KIND_DISPLAY
from ipixel_mcp.safety import ValidationError


class FakeInfo:
    width = 32
    height = 32
    led_type = 1
    device_type = "Fake"


class FakeClient:
    def __init__(self):
        self.images = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    def get_device_info(self):
        return FakeInfo()

    async def send_image_hex(self, **kw):
        self.images.append(kw)


def png():
    return b"\x89PNG\r\n" + b"\x00" * 64


def sizer(data, fmt):
    return [(16, 16)]


def test_display_image_returns_job_then_succeeds():
    async def scenario():
        client = FakeClient()
        dm = DeviceManager("X", client_factory=lambda a: client)
        jobs = JobRegistry()
        ds = DisplayState()

        res = display.display_image(
            dm, jobs, data=png(), fmt="png", slot=0,
            display_state=ds, frame_sizer=sizer,
        )
        assert "job_id" in res
        done = await jobs.wait(res["job_id"])
        assert done.status == SUCCEEDED
        assert client.images and client.images[0]["file_extension"] == ".png"
        # display state recorded after successful transfer
        assert ds.get_display_state()["kind"] == KIND_DISPLAY

    asyncio.run(scenario())


def test_display_image_validation_is_synchronous():
    client = FakeClient()
    dm = DeviceManager("X", client_factory=lambda a: client)
    jobs = JobRegistry()
    # bad format raises right away (not buried in a job)
    with pytest.raises(ValidationError):
        display.display_image(dm, jobs, data=png(), fmt="bmp", frame_sizer=sizer)


def test_display_image_bad_image_fails_sync():
    client = FakeClient()
    dm = DeviceManager("X", client_factory=lambda a: client)
    jobs = JobRegistry()

    def boom(data, fmt):
        raise RuntimeError("decode boom")

    with pytest.raises(ValidationError):
        display.display_image(dm, jobs, data=png(), fmt="png", frame_sizer=boom)


def test_get_display_state_reads():
    ds = DisplayState()
    ds.set_base(owner="x", summary="hello")
    out = display.get_display_state(ds)
    assert out["owner"] == "x"
    assert out["summary"] == "hello"
