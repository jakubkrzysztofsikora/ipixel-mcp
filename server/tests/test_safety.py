import pytest

from ipixel_mcp import safety
from ipixel_mcp.safety import ValidationError


def test_validate_color_normalises():
    assert safety.validate_color("FF8800") == "ff8800"
    assert safety.validate_color("#ff8800") == "ff8800"


@pytest.mark.parametrize("bad", ["fff", "gggggg", "ff88000", "", 123])
def test_validate_color_rejects(bad):
    with pytest.raises(ValidationError):
        safety.validate_color(bad)


def test_validate_text_length_cap_F6():
    safety.validate_text("a" * safety.MAX_TEXT_CHARS)  # ok
    with pytest.raises(ValidationError):
        safety.validate_text("a" * (safety.MAX_TEXT_CHARS + 1))
    with pytest.raises(ValidationError):
        safety.validate_text("")


def test_enforce_count_byte_F6():
    assert safety.enforce_count_byte(1) == 1
    assert safety.enforce_count_byte(255) == 255
    with pytest.raises(ValidationError):
        safety.enforce_count_byte(256)
    with pytest.raises(ValidationError):
        safety.enforce_count_byte(0)


def test_clamp_int_bounds():
    assert safety.clamp_int("50", field="speed", lo=1, hi=100) == 50
    with pytest.raises(ValidationError):
        safety.clamp_int(0, field="speed", lo=1, hi=100)
    with pytest.raises(ValidationError):
        safety.clamp_int("x", field="speed", lo=1, hi=100)


def test_font_allowlist_no_paths_F2():
    assert safety.validate_font("CUSONG") == "CUSONG"
    for bad in ["../etc/passwd", "/tmp/x.ttf", "Arial"]:
        with pytest.raises(ValidationError):
            safety.validate_font(bad)


def test_animation_gating_bootloop_H():
    # non-32x32 boards must not allow 3/4 (firmware bootloop)
    assert safety.allowed_animations(64, 16) == frozenset({0, 1, 2})
    assert safety.allowed_animations(32, 32) == frozenset({0, 1, 2, 3, 4})
    safety.validate_animation(2, width=64, height=16)
    with pytest.raises(ValidationError):
        safety.validate_animation(3, width=64, height=16)
    safety.validate_animation(4, width=32, height=32)


def test_validate_image_bytes_F2_F3():
    payload = safety.validate_image_bytes(b"\x89PNG\r\n", "png")
    assert payload.extension == ".png"
    assert safety.validate_image_bytes(b"x", "JPG").extension == ".jpg"
    with pytest.raises(ValidationError):
        safety.validate_image_bytes(b"", "png")
    with pytest.raises(ValidationError):
        safety.validate_image_bytes(b"x" * (safety.MAX_IMAGE_BYTES + 1), "png")
    with pytest.raises(ValidationError):
        safety.validate_image_bytes(b"x", "bmp")  # not in allow-list
