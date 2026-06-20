"""Input validation and limits for the curated tool surface.

Stdlib-only so it is testable without BLE/mcp/pillow installed. These functions
implement the security controls from docs/SECURITY_REVIEW_pypixelcolor.md:

- F-6  text length must not overflow the single-byte ``num_chars`` field (<255).
- F-5  colours validated to a strict 6-hex form (the upstream check is broken).
- F-2  no filesystem paths accepted; ``font`` restricted to a bundled allow-list.
- F-3  image bytes capped before they ever reach Pillow.
- H-BOOTLOOP  animations 3/4 bootloop non-32x32 boards, so gate per detected model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---- limits -----------------------------------------------------------------

# The protocol stores the glyph/chunk count in a single byte, so the effective
# count must stay < 256. We cap raw characters well under that; an emoji-heavy
# string can expand to more *chunks* than characters, so the hard ceiling is
# enforced again at encode time by the device layer. (Review F-6.)
MAX_TEXT_CHARS = 200
MAX_TEXT_COUNT_BYTE = 255

# F-3: cap decoded image bytes before Pillow ever sees them.
MAX_IMAGE_BYTES = 256 * 1024
MAX_IMAGE_PIXELS = 64 * 64 * 64  # generous vs any supported panel, far below Pillow's bomb default

# F-2: only fonts bundled with pypixelcolor; never an arbitrary path.
ALLOWED_FONTS = frozenset({"CUSONG", "SIMSUN", "VCR_OSD_MONO"})

ALLOWED_RESIZE = frozenset({"crop", "fit", "stretch"})

_HEX_COLOR = re.compile(r"^#?([0-9a-fA-F]{6})$")


class ValidationError(ValueError):
    """Raised for invalid tool input. Mapped to a generic client error (F-9)."""


# ---- scalars ----------------------------------------------------------------

def validate_color(value: str, *, field: str = "color") -> str:
    """Return a normalised lowercase 6-hex colour (no ``#``) or raise.

    Fixes the inverted upstream validation (F-5) by validating here instead.
    """
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    m = _HEX_COLOR.match(value.strip())
    if not m:
        raise ValidationError(f"{field} must be a 6-digit hex colour like 'ff8800'")
    return m.group(1).lower()


def clamp_int(value, *, field: str, lo: int, hi: int) -> int:
    """Coerce to int and require lo <= value <= hi (no silent clamping)."""
    try:
        iv = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be an integer")
    if iv < lo or iv > hi:
        raise ValidationError(f"{field} must be between {lo} and {hi}")
    return iv


def validate_text(text: str) -> str:
    """Validate display text length so it cannot overflow the count byte (F-6)."""
    if not isinstance(text, str):
        raise ValidationError("text must be a string")
    if text == "":
        raise ValidationError("text must not be empty")
    if len(text) > MAX_TEXT_CHARS:
        raise ValidationError(f"text must be at most {MAX_TEXT_CHARS} characters")
    return text


def validate_font(font: str) -> str:
    if font not in ALLOWED_FONTS:
        allowed = ", ".join(sorted(ALLOWED_FONTS))
        raise ValidationError(f"font must be one of: {allowed}")
    return font


def validate_resize(method: str) -> str:
    if method not in ALLOWED_RESIZE:
        allowed = ", ".join(sorted(ALLOWED_RESIZE))
        raise ValidationError(f"resize must be one of: {allowed}")
    return method


def enforce_count_byte(count: int) -> int:
    """Final guard at encode time: the device count byte must be < 256 (F-6)."""
    if count < 1 or count >= MAX_TEXT_COUNT_BYTE + 1:
        raise ValidationError(
            f"encoded glyph count {count} exceeds the device limit of {MAX_TEXT_COUNT_BYTE}"
        )
    return count


# ---- model-aware gating (H-BOOTLOOP) ----------------------------------------

def allowed_animations(width: int, height: int) -> frozenset[int]:
    """Animations valid for a panel of the given size.

    pypixelcolor bans animations 3 and 4 on non-32x32 panels because they
    bootloop the firmware. We fail closed: only 32x32 gets the full set.
    """
    base = {0, 1, 2}
    if width == 32 and height == 32:
        return frozenset(base | {3, 4})
    return frozenset(base)


def validate_animation(animation: int, *, width: int, height: int) -> int:
    allowed = allowed_animations(width, height)
    if animation not in allowed:
        raise ValidationError(
            f"animation {animation} not allowed on a {width}x{height} panel "
            f"(allowed: {sorted(allowed)})"
        )
    return animation


# ---- images (F-2/F-3) -------------------------------------------------------

ALLOWED_IMAGE_FORMATS = frozenset({"png", "gif", "jpeg", "jpg"})


@dataclass(frozen=True)
class ImagePayload:
    data: bytes
    extension: str  # e.g. ".png"


def validate_image_bytes(data: bytes, fmt: str) -> ImagePayload:
    """Validate raw image bytes: size cap + format allow-list. Never a path (F-2)."""
    if not isinstance(data, (bytes, bytearray)):
        raise ValidationError("image data must be bytes")
    if len(data) == 0:
        raise ValidationError("image data must not be empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValidationError(
            f"image exceeds the {MAX_IMAGE_BYTES // 1024} KB limit "
            f"({len(data) // 1024} KB)"
        )
    f = fmt.lower().lstrip(".")
    if f not in ALLOWED_IMAGE_FORMATS:
        allowed = ", ".join(sorted(ALLOWED_IMAGE_FORMATS))
        raise ValidationError(f"format must be one of: {allowed}")
    ext = ".jpg" if f in ("jpg", "jpeg") else f".{f}"
    return ImagePayload(data=bytes(data), extension=ext)
