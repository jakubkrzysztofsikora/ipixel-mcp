import pytest

from ipixel_mcp import safety
from ipixel_mcp.safety import ValidationError


def png(n=64):
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * n


def single_frame_sizer(w, h):
    return lambda data, fmt: [(w, h)]


def test_decode_single_frame_ok():
    d = safety.decode_and_prepare_image(png(), "png", frame_sizer=single_frame_sizer(32, 16))
    assert d.width == 32 and d.height == 16
    assert d.frame_count == 1
    assert d.total_pixels == 32 * 16
    assert d.payload.extension == ".png"


def test_decode_rejects_oversized_pixels():
    big = safety.MAX_IMAGE_PIXELS
    sizer = lambda data, fmt: [(big, 2)]
    with pytest.raises(ValidationError):
        safety.decode_and_prepare_image(png(), "png", frame_sizer=sizer)


def test_decode_rejects_too_many_frames():
    frames = [(8, 8)] * (safety.MAX_GIF_FRAMES + 1)
    with pytest.raises(ValidationError):
        safety.decode_and_prepare_image(png(), "gif", frame_sizer=lambda d, f: frames)


def test_decode_rejects_total_pixel_bomb():
    # Each frame under per-frame cap but sum over the total cap.
    per = safety.MAX_IMAGE_PIXELS
    n = (safety.MAX_TOTAL_DECODED_PIXELS // per) + 2
    frames = [(per, 1)] * int(n)
    # frame count may be within limit; ensure total cap trips (or frame cap)
    with pytest.raises(ValidationError):
        safety.decode_and_prepare_image(png(), "gif", frame_sizer=lambda d, f: frames[: safety.MAX_GIF_FRAMES])


def test_decode_byte_cap_runs_first():
    with pytest.raises(ValidationError):
        safety.decode_and_prepare_image(
            b"x" * (safety.MAX_IMAGE_BYTES + 1), "png",
            frame_sizer=single_frame_sizer(8, 8),
        )


def test_decode_decoder_error_is_generic():
    def boom(data, fmt):
        raise RuntimeError("internal pillow detail /home/secret")

    with pytest.raises(ValidationError) as ei:
        safety.decode_and_prepare_image(png(), "png", frame_sizer=boom)
    assert "decoded" in str(ei.value)
    assert "secret" not in str(ei.value)  # no internal leak (F-9)


def test_decode_no_frames_rejected():
    with pytest.raises(ValidationError):
        safety.decode_and_prepare_image(png(), "png", frame_sizer=lambda d, f: [])


def test_encoded_output_cap():
    assert safety.enforce_encoded_output_size(b"x" * 100) == b"x" * 100
    with pytest.raises(ValidationError):
        safety.enforce_encoded_output_size(b"x" * (safety.MAX_ENCODED_OUTPUT_BYTES + 1))
    with pytest.raises(ValidationError):
        safety.enforce_encoded_output_size("not-bytes")
