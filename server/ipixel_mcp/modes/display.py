"""Mode A — display passthrough (text, image-as-job, device/display state).

Pure validation/builders (no mcp import) so they are unit-testable, plus async
handlers that run through the DeviceManager. ``display_image`` is an **async job**
(review C-2): it validates + hardens the bytes synchronously, then returns a
``job_id`` immediately while the (slow) BLE transfer runs in the background.
"""

from __future__ import annotations

from typing import Any, Optional

from .. import safety
from ..device import DeviceManager
from ..display_state import DisplayState
from ..jobs import JobRegistry
from ..logging_utils import redact_bytes

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
    # Warm the connection so animation gating uses the LIVE panel dimensions, then
    # validate BEFORE dm.execute (review G-4): a ValidationError raised inside the
    # _op would otherwise be re-wrapped by DeviceManager.execute as a generic
    # DeviceError and the model would never learn which parameter was wrong. If the
    # link can't be established, _dims falls back to a non-32x32 size (fail-closed).
    await dm.ensure_ready()
    width, height = _dims(dm.device_info)
    kwargs = build_send_text_kwargs(width=width, height=height, **params)

    async def _op(client: Any) -> None:
        await client.send_text(**kwargs)

    await dm.execute("display_text", _op)
    slot = params.get("slot", DEFAULT_SLOT)
    return {
        "ok": True,
        "message": f"Displayed text on board (slot {slot}, "
        f"{'volatile' if slot == 0 else 'saved'}).",
    }


# ---- Mode A device-control tools (plan §3 Mode A) ---------------------------


async def _call(dm: DeviceManager, op_name: str, method: str, *args: Any) -> None:
    async def _op(client: Any) -> None:
        await getattr(client, method)(*args)

    await dm.execute(op_name, _op)


async def set_brightness(dm: DeviceManager, level: int) -> dict[str, Any]:
    lvl = safety.clamp_int(level, field="level", lo=0, hi=100)
    await _call(dm, "set_brightness", "set_brightness", lvl)
    return {"ok": True, "message": f"Brightness set to {lvl}."}


async def set_power(dm: DeviceManager, on: bool) -> dict[str, Any]:
    state = bool(on)
    await _call(dm, "set_power", "set_power", state)
    return {"ok": True, "message": f"Power {'on' if state else 'off'}."}


async def set_orientation(dm: DeviceManager, orientation: int) -> dict[str, Any]:
    o = safety.clamp_int(orientation, field="orientation", lo=0, hi=3)
    await _call(dm, "set_orientation", "set_orientation", o)
    return {"ok": True, "message": f"Orientation set to {o}."}


async def show_slot(dm: DeviceManager, number: int) -> dict[str, Any]:
    n = safety.clamp_int(number, field="number", lo=0, hi=20)
    await _call(dm, "show_slot", "show_slot", n)
    return {"ok": True, "message": f"Showing slot {n}."}


async def set_clock_mode(
    dm: DeviceManager, style: int = 1, show_date: bool = True, format_24: bool = True
) -> dict[str, Any]:
    s = safety.clamp_int(style, field="style", lo=0, hi=255)

    async def _op(client: Any) -> None:
        await client.set_clock_mode(
            style=s, show_date=bool(show_date), format_24=bool(format_24)
        )

    await dm.execute("set_clock_mode", _op)
    return {"ok": True, "message": f"Clock mode {s} set."}


async def clear_screen(dm: DeviceManager) -> dict[str, Any]:
    """Destructive: wipes device ROM/settings (admin-gated at the app layer)."""
    await _call(dm, "clear", "clear")
    return {"ok": True, "message": "Cleared device ROM."}


async def delete_slot(dm: DeviceManager, n: int) -> dict[str, Any]:
    """Destructive: delete a saved screen (admin-gated at the app layer)."""
    idx = safety.clamp_int(n, field="n", lo=0, hi=20)
    await _call(dm, "delete", "delete", idx)
    return {"ok": True, "message": f"Deleted slot {idx}."}


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


# ---- image as async job (review C-2) ----------------------------------------


def display_image(
    dm: DeviceManager,
    jobs: JobRegistry,
    *,
    data: bytes,
    fmt: str,
    slot: int = DEFAULT_SLOT,
    resize: str = "crop",
    source: str = "display",
    display_state: Optional[DisplayState] = None,
    frame_sizer: "safety.FrameSizer" = safety._pillow_frame_sizer,
) -> dict[str, Any]:
    """Validate + harden image bytes, then enqueue the slow transfer as a job.

    Returns ``{job_id, status}`` immediately (review C-2). Validation errors are
    raised synchronously (so the caller gets a real ValidationError); the BLE
    transfer happens in the background and its outcome is read via job status.
    """
    slot = safety.clamp_int(slot, field="slot", lo=0, hi=20)
    resize = safety.validate_resize(resize)
    # Synchronous hardening so a bad image fails the tool call, not silently a job.
    decoded = safety.decode_and_prepare_image(data, fmt, frame_sizer=frame_sizer)

    async def _work() -> dict[str, Any]:
        async def _op(client: Any) -> None:
            # Refuse on a degraded BLE link rather than garble the panel (H-MTU).
            dm.assert_mtu_ok()
            hex_string = decoded.payload.data.hex()
            # Final encoded-size guard before the (slow) transfer (C-2).
            safety.enforce_encoded_output_size(decoded.payload.data)
            await client.send_image_hex(
                hex_string=hex_string,
                file_extension=decoded.payload.extension,
                resize_method=resize,
                save_slot=slot,
            )

        await dm.execute("display_image", _op, timeout=120.0)
        if display_state is not None:
            display_state.set_base(
                owner=source,
                summary=f"image {decoded.width}x{decoded.height} "
                f"({redact_bytes(decoded.payload.data)})",
            )
        return {
            "ok": True,
            "slot": slot,
            "frames": decoded.frame_count,
            "message": f"Displayed image on board (slot {slot}, "
            f"{'volatile' if slot == 0 else 'saved'}).",
        }

    job = jobs.submit("display_image", _work)
    return {"job_id": job.id, "status": job.status}


def get_display_state(display_state: DisplayState) -> dict[str, Any]:
    """Read the current display ownership/state (review M-OWN, read-only)."""
    return display_state.get_display_state()
