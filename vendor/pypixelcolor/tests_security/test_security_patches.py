"""Tests for the security/bug fixes applied to this pypixelcolor fork.

Covers: F-5 (set_pixel validation), F-6 (num_chars overflow), F-8 (ACK frame
validation), H-MTU (MTU-aware chunking), F-3 (image bomb/frame guards).
See SECURITY-PATCHES.md for the full list.
"""

import asyncio
import io

import pytest


# ---- F-5: set_pixel colour validation + coordinate bounds --------------------

def test_set_pixel_rejects_bad_colour():
    from pypixelcolor.commands.set_fun_mode import set_pixel

    for bad in ["xyzxyz", "fff", "ff00", 123, None]:
        with pytest.raises(ValueError):
            set_pixel(0, 0, bad)


def test_set_pixel_accepts_good_colour_and_builds_payload():
    from pypixelcolor.commands.set_fun_mode import set_pixel

    plan = set_pixel(1, 2, "FF8800")
    data = list(plan.windows)[0].data
    assert len(data) == 10
    assert data[5:8] == bytes([0xFF, 0x88, 0x00])  # R,G,B
    assert data[8] == 1 and data[9] == 2           # x,y


def test_set_pixel_rejects_out_of_byte_range_coords():
    from pypixelcolor.commands.set_fun_mode import set_pixel

    with pytest.raises(ValueError):
        set_pixel(300, 0, "ffffff")  # would overflow bytes([..]) before the fix


# ---- F-6: glyph/char count must fit one byte --------------------------------

def test_send_text_rejects_overlong_text():
    from pypixelcolor.commands.send_text import send_text

    with pytest.raises(ValueError) as exc:
        send_text("a" * 300, char_height=16)   # > 255 glyphs/chunks
    assert "255" in str(exc.value)


def test_send_text_short_text_ok():
    from pypixelcolor.commands.send_text import send_text

    plan = send_text("hi", char_height=16)
    assert list(plan.windows)  # produced at least one window, no overflow


# ---- F-8: ACK frame validation ----------------------------------------------

def test_ack_handler_accepts_only_strict_5byte_frames():
    from pypixelcolor.lib.transport.ack_manager import AckManager

    mgr = AckManager()
    handler = mgr.make_notify_handler()

    # oversized frame starting 0x05 must be IGNORED (previously misread as ACK)
    handler(None, bytes([0x05, 0, 0, 0, 1, 0, 0, 0]))
    assert not mgr.window_event.is_set()

    # strict 5-byte window ACK
    handler(None, bytes([0x05, 0, 0, 0, 1]))
    assert mgr.window_event.is_set()

    # strict 5-byte final ACK sets both
    mgr.reset()
    handler(None, bytes([0x05, 0, 0, 0, 3]))
    assert mgr.window_event.is_set() and mgr.all_event.is_set()


# ---- H-MTU: chunking respects the negotiated MTU ----------------------------

def test_effective_chunk_size_caps_by_mtu():
    from pypixelcolor.lib.transport.send_plan import effective_chunk_size, single_window_plan

    plan = single_window_plan("p", b"x" * 1000, requires_ack=False)

    class C23:
        mtu_size = 23

    class C247:
        mtu_size = 247

    class CNone:
        pass  # no mtu_size attr → fall back to plan.chunk_size

    assert effective_chunk_size(C23(), plan) == 20        # 23 - 3
    assert effective_chunk_size(C247(), plan) == 244      # min(244, 244)
    assert effective_chunk_size(CNone(), plan) == plan.chunk_size


def test_send_plan_never_writes_chunks_larger_than_mtu():
    from pypixelcolor.lib.transport.send_plan import send_plan, single_window_plan
    from pypixelcolor.lib.transport.ack_manager import AckManager

    class FakeClient:
        mtu_size = 23  # degraded link → max 20-byte chunks
        def __init__(self):
            self.writes = []
        async def write_gatt_char(self, uuid, chunk, response=True):
            self.writes.append(len(chunk))

    client = FakeClient()
    plan = single_window_plan("img", b"y" * 500, requires_ack=False)
    asyncio.run(send_plan(client, plan, AckManager()))
    assert client.writes and max(client.writes) <= 20  # 23 - 3


# ---- F-3: image decompression-bomb + frame-count guards ---------------------

def _png_bytes(w, h):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_open_guard_rejects_oversized_image(monkeypatch):
    from pypixelcolor.commands import send_image

    monkeypatch.setattr(send_image, "MAX_IMAGE_PIXELS", 100)  # 10x10 = 100 ok, 20x20 not
    with pytest.raises(ValueError):
        send_image._open_image_guarded(_png_bytes(20, 20))
    # under the cap is fine
    img = send_image._open_image_guarded(_png_bytes(10, 10))
    img.close()


def test_open_guard_rejects_too_many_frames(monkeypatch):
    from PIL import Image
    from pypixelcolor.commands import send_image

    monkeypatch.setattr(send_image, "MAX_GIF_FRAMES", 1)
    buf = io.BytesIO()
    f1 = Image.new("RGB", (8, 8), (0, 0, 0))
    f2 = Image.new("RGB", (8, 8), (255, 255, 255))
    f1.save(buf, format="GIF", save_all=True, append_images=[f2])
    with pytest.raises(ValueError):
        send_image._open_image_guarded(buf.getvalue())
