import asyncio

import pytest

from ipixel_mcp.ble_backend import (
    BleBackend,
    BackendError,
    derive_chunk_size,
    UPSTREAM_CHUNK,
    MIN_SAFE_MTU,
)


class FakeRaw:
    def __init__(self, mtu=247):
        self.mtu_size = mtu
        self.connected = False
        self.sent_images = []
        self.sent_text = []

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    def get_device_info(self):
        return {"w": 64}

    async def send_text(self, **kw):
        self.sent_text.append(kw)

    async def send_image_hex(self, **kw):
        self.sent_images.append(kw)


def test_derive_chunk_size():
    assert derive_chunk_size(247) == 244
    assert derive_chunk_size(None) == UPSTREAM_CHUNK
    with pytest.raises(BackendError):
        derive_chunk_size(23)  # degraded MTU refused (H-MTU)


def test_connect_disconnect_and_info():
    raw = FakeRaw()
    be = BleBackend("AA", client_factory=lambda a: raw)

    async def scenario():
        await be.connect()
        assert raw.connected
        assert be.read_mtu() == 247
        assert be.get_device_info() == {"w": 64}
        await be.disconnect()
        assert not raw.connected

    asyncio.run(scenario())


def test_send_image_hex_enforces_mtu_ok():
    raw = FakeRaw(mtu=247)
    be = BleBackend("AA", client_factory=lambda a: raw)

    async def scenario():
        await be.connect()
        await be.send_image_hex(hex_string="abcd", file_extension=".png", save_slot=0)
        assert raw.sent_images[0]["file_extension"] == ".png"

    asyncio.run(scenario())


def test_send_image_hex_refuses_small_mtu():
    raw = FakeRaw(mtu=23)
    be = BleBackend("AA", client_factory=lambda a: raw)

    async def scenario():
        await be.connect()
        with pytest.raises(BackendError):
            await be.send_image_hex(hex_string="abcd", file_extension=".png")
        assert raw.sent_images == []  # never transmitted on a degraded link

    asyncio.run(scenario())


def test_calls_before_connect_raise():
    be = BleBackend("AA", client_factory=lambda a: FakeRaw())
    with pytest.raises(BackendError):
        be.get_device_info()
