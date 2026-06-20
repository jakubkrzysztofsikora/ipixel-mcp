"""Mode A — display passthrough (Phase 0: text + device info).

Pure validation/builders (no mcp import) so they are unit-testable, plus async
handlers that run through the DeviceManager. Image display arrives in Phase 2 as
an async job (review C-2); Phase 0 keeps the synchronous text path.
"""

from __future__ import annotations

from typing import Any, Optional

from .. import safety
from ..device import DeviceManager

# Defaults mirror pypixelcolor.send_text but with safe, model-agnostic values.
DEFAULT_COLOR = "ffffff"
DEFAULT_FONT = "CUSONG"
DEFAULT_SPEED = 80
DEFAULT_ANIMATION = 0
DEFAULT_RAINBOW = 0
DEFAULT_SLOT = 0  # 0 = volatile RAM (no flash write) — protects EEPROM (review H-FLASH)

# Conservative fallback when device info isn't available yet. Fails closed on
# animations (only 32x32 gets the extended set), so we assume a non-32x32 size.
FALLBACK_WIDTH = 64
FALLBACK_HEIGHT = 16


def build_send_text_kwargs(
    *,
    text: str,
    color: str = DEFAULT_COLOR,
    bg_color: Optional[str] = None,
    font: str = DEFAULT_FONT,
    animation: int = DEFAULT_ANIMATION,
    speed: int = DEFAULT_SPEED,
    rainbow: int = DEFAULT_RAINBOW,
    slot: int = DEFAULT_SLOT,
    width: int = FALLBACK_WIDTH,
    height: int = FALLBACK_HEIGHT,
) -> dict[str, Any]:
    """Validate inputs and produce kwargs for ``AsyncClient.send_text``.

    Raises ``safety.ValidationError`` on bad input (mapped to a generic client
    error by the app layer).
    """
    kwargs: dict[str, Any] = {
        "text": safety.validate_text(text),
        "color": safety.validate_color(color),
        "font": safety.validate_font(font),
        "speed": safety.clamp_int(speed, field="speed", lo=1, hi=100),
        "rainbow_mode": safety.clamp_int(rainbow, field="rainbow", lo=0, hi=1),
        "animation": safety.validate_animation(animation, width=width, height=height),
        "save_slot": safety.clamp_int(slot, field="slot", lo=0, hi=20),
    }
    if bg_color is not None:
        kwargs["bg_color"] = safety.validate_color(bg_color, field="bg_color")
    return kwargs


def _dims(device_info: Any) -> tuple[int, int]:
    if device_info is None:
        return FALLBACK_WIDTH, FALLBACK_HEIGHT
    w = getattr(device_info, "width", None) or FALLBACK_WIDTH
    h = getattr(device_info, "height", None) or FALLBACK_HEIGHT
    return int(w), int(h)


async def display_text(dm: DeviceManager, **params: Any) -> dict[str, Any]:
    """Render text on the board. Returns a short text confirmation (review M-RESULT)."""

    async def _op(client: Any) -> None:
        width, height = _dims(dm.device_info)
        kwargs = build_send_text_kwargs(width=width, height=height, **params)
        await client.send_text(**kwargs)

    await dm.execute("display_text", _op)
    slot = params.get("slot", DEFAULT_SLOT)
    return {
        "ok": True,
        "message": f"Displayed text on board (slot {slot}, "
        f"{'volatile' if slot == 0 else 'saved'}).",
    }


async def get_device_info(dm: DeviceManager) -> dict[str, Any]:
    """Read panel info (read-only)."""
    info = await dm.get_device_info()
    width, height = _dims(info)
    return {
        "width": width,
        "height": height,
        "led_type": getattr(info, "led_type", None),
        "device_type": getattr(info, "device_type", None),
        "allowed_animations": sorted(safety.allowed_animations(width, height)),
    }
