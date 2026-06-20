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
from typing import Callable

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

# F-3: cap on the GIF frame count and the total decoded pixels (sum across all
# frames). A panel-sized GIF is tiny; these stop a many-frame bomb that slips
# under the per-frame pixel cap.
MAX_GIF_FRAMES = 64
MAX_TOTAL_DECODED_PIXELS = 64 * 64 * MAX_GIF_FRAMES

# C-2: cap on the *encoded* output that the BLE layer will actually transfer.
# The library re-encodes images; an over-large encoding means a multi-minute
# transfer that blows MCP/Worker timeouts. The job layer refuses past this.
MAX_ENCODED_OUTPUT_BYTES = 256 * 1024

# F-2: only fonts bundled with pypixelcolor; never an arbitrary path.
ALLOWED_FONTS = frozenset({"CUSONG", "SIMSUN", "VCR_OSD_MONO"})

# Only what pypixelcolor's ResizeMethod actually supports (crop/fit).
ALLOWED_RESIZE = frozenset({"crop", "fit"})

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


# ---- hardened decode (F-3) --------------------------------------------------


@dataclass(frozen=True)
class DecodedImage:
    """Result of a hardened decode: dimensions + per-frame pixel accounting.

    We deliberately do NOT keep the decoded pixel buffers here — callers that
    need pixels re-open under the same guards. This struct is the safety summary
    plus the validated original payload to hand to the encoder.
    """

    payload: ImagePayload
    width: int
    height: int
    frame_count: int
    total_pixels: int


# An injectable decoder so tests run without Pillow. It takes raw bytes + the
# normalised lowercase format ("png"/"gif"/"jpeg") and yields, for each frame,
# a (width, height) tuple. The real implementation wraps Pillow lazily.
FrameSizer = Callable[[bytes, str], "list[tuple[int, int]]"]


def _pillow_frame_sizer(data: bytes, fmt: str) -> "list[tuple[int, int]]":
    """Lazy-Pillow frame sizer: bomb warning -> error, MAX_IMAGE_PIXELS set.

    Imports Pillow *inside* the function so the module stays importable without
    it. Reads frame sizes (cheap) rather than materialising every frame's
    pixels; the per-frame and total caps are enforced by the caller.
    """
    import io
    import warnings

    from PIL import Image  # type: ignore
    from PIL import ImageFile  # type: ignore

    # F-3: set a low pixel cap and make the decompression-bomb warning fatal.
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    # Don't let truncated files limp through into a half-decode.
    ImageFile.LOAD_TRUNCATED_IMAGES = False

    sizes: list[tuple[int, int]] = []
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        # F-11: context-managed so handles close on a long-running server.
        with Image.open(io.BytesIO(data)) as img:
            n_frames = getattr(img, "n_frames", 1)
            for i in range(n_frames):
                if i >= MAX_GIF_FRAMES:
                    # Stop probing; the caller will reject on the frame cap.
                    sizes.append((1 << 30, 1))  # sentinel that trips total cap too
                    break
                try:
                    img.seek(i)
                except EOFError:
                    break
                sizes.append((int(img.width), int(img.height)))
    return sizes


def decode_and_prepare_image(
    data: bytes,
    fmt: str,
    *,
    frame_sizer: FrameSizer = _pillow_frame_sizer,
) -> DecodedImage:
    """Validate + safely inspect image bytes before they reach the encoder (F-3).

    Order of operations (cheapest, hardest guard first):
      1. byte cap + format allow-list (``validate_image_bytes``);
      2. decode dimensions via the injected ``frame_sizer`` (Pillow lazily),
         with ``MAX_IMAGE_PIXELS`` set and the bomb warning treated as an error;
      3. enforce the GIF frame cap and the total-decoded-pixel cap.

    ``frame_sizer`` is injected so unit tests run a pure stub without Pillow.
    Raises ``ValidationError`` on any breach (mapped to a generic client error).
    """
    payload = validate_image_bytes(data, fmt)
    norm = payload.extension.lstrip(".")
    norm = "jpeg" if norm == "jpg" else norm

    try:
        sizes = frame_sizer(payload.data, norm)
    except ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalise to a generic error
        # Bomb warning, decoder errors, truncation: all become a single message.
        # The detail is the caller's to log (redacted); we don't leak it (F-9).
        raise ValidationError("image could not be decoded") from exc

    if not sizes:
        raise ValidationError("image has no decodable frames")
    if len(sizes) > MAX_GIF_FRAMES:
        raise ValidationError(
            f"image has too many frames (max {MAX_GIF_FRAMES})"
        )

    total = 0
    max_w = max_h = 0
    for (w, h) in sizes:
        if w <= 0 or h <= 0:
            raise ValidationError("image has an invalid frame size")
        px = w * h
        if px > MAX_IMAGE_PIXELS:
            raise ValidationError("image is too large (pixel limit exceeded)")
        total += px
        max_w = max(max_w, w)
        max_h = max(max_h, h)
    if total > MAX_TOTAL_DECODED_PIXELS:
        raise ValidationError("image is too large (total pixel limit exceeded)")

    return DecodedImage(
        payload=payload,
        width=max_w,
        height=max_h,
        frame_count=len(sizes),
        total_pixels=total,
    )


def enforce_encoded_output_size(encoded: bytes) -> bytes:
    """Cap the encoded bytes the BLE layer will transfer (C-2).

    Called after the library re-encodes an image/animation, before the transfer
    starts, so an over-large encoding fails fast instead of stranding the link.
    """
    if not isinstance(encoded, (bytes, bytearray)):
        raise ValidationError("encoded output must be bytes")
    if len(encoded) > MAX_ENCODED_OUTPUT_BYTES:
        raise ValidationError(
            f"encoded image exceeds the transfer limit "
            f"({MAX_ENCODED_OUTPUT_BYTES // 1024} KB)"
        )
    return bytes(encoded)
