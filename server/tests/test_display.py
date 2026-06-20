import asyncio

import pytest

from ipixel_mcp.modes import display
from ipixel_mcp.safety import ValidationError
from ipixel_mcp.device import DeviceManager


def test_build_send_text_kwargs_defaults():
    kw = display.build_send_text_kwargs(text="hello")
    assert kw["text"] == "hello"
    assert kw["color"] == "ffffff"
    assert kw["font"] == "CUSONG"
    assert kw["save_slot"] == 0          # volatile by default (flash protection)
    assert kw["rainbow_mode"] == 0
    assert kw["animation"] == 0
    assert "bg_color" not in kw


def test_build_send_text_kwargs_bg_and_normalisation():
    kw = display.build_send_text_kwargs(text="hi", color="#FF0000", bg_color="00FF00")
    assert kw["color"] == "ff0000"
    assert kw["bg_color"] == "00ff00"


def test_build_send_text_kwargs_rejects_bad_input():
    with pytest.raises(ValidationError):
        display.build_send_text_kwargs(text="hi", color="nothex")
    with pytest.raises(ValidationError):
        display.build_send_text_kwargs(text="hi", font="/etc/passwd")
    with pytest.raises(ValidationError):
        display.build_send_text_kwargs(text="", color="ffffff")


def test_animation_gated_by_panel_size():
    # default fallback panel is non-32x32, so animation 3 is rejected
    with pytest.raises(ValidationError):
        display.build_send_text_kwargs(text="hi", animation=3)
    # explicit 32x32 allows it
    kw = display.build_send_text_kwargs(text="hi", animation=3, width=32, height=32)
    assert kw["animation"] == 3


class FakeInfo:
    width = 32
    height = 32
    led_type = 2
    device_type = "Mini"


class FakeClient:
    def __init__(self):
        self.sent = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    def get_device_info(self):
        return FakeInfo()

    async def send_text(self, **kw):
        self.sent.append(kw)


def test_display_text_uses_live_dims_and_confirms():
    client = FakeClient()
    dm = DeviceManager("X", client_factory=lambda a: client)

    # 32x32 panel => animation 3 is allowed because dims come from the device
    result = asyncio.run(display.display_text(dm, text="hi", animation=3))
    assert result["ok"] is True
    assert "volatile" in result["message"]
    assert client.sent and client.sent[0]["animation"] == 3


def test_get_device_info_reports_allowed_animations():
    client = FakeClient()
    dm = DeviceManager("X", client_factory=lambda a: client)
    info = asyncio.run(display.get_device_info(dm))
    assert info["width"] == 32 and info["height"] == 32
    assert info["allowed_animations"] == [0, 1, 2, 3, 4]
