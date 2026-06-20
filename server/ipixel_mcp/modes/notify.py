"""Mode B — operator-input notifications (review §3 / E-2 / H-FLASH).

A board-as-ambient-alert channel. An agent that is blocked renders a banner.
Design points enforced here:

- ``message`` <= 40 chars (reads in one scroll), ``level`` in {info,warn,blocked},
  ``source`` effectively required (single shared tailnet identity → only way to
  tell agents apart — review M-OWN), ``ttl_seconds`` enforced.
- **Volatile display only** (``save_slot=0``) to protect flash (review H-FLASH).
- ``blocked`` *preempts* the current display via the DisplayState stack and is
  *restored* on clear.
- The queue is **persisted** (JSON file) so a ``notification_id`` survives a
  restart; clear-of-unknown-id is a no-op.
- **TTL auto-expiry** is enforced so a missed Stop hook can't strand the board.

Hardware-free: the device-render callback and the clock are injected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Any, Awaitable, Callable, Optional

from .. import safety
from ..display_state import DisplayState

logger = logging.getLogger("ipixel_mcp.notify")

MAX_MESSAGE_CHARS = 40
LEVELS = ("info", "warn", "blocked")
DEFAULT_TTL = 300.0
MAX_TTL = 24 * 3600.0
NOTIFY_SLOT = 0  # volatile RAM only — never persist to flash (H-FLASH)

# Level → banner colour (review §3: blue / amber / red).
LEVEL_COLOR = {"info": "0066ff", "warn": "ffaa00", "blocked": "ff0000"}


@dataclass
class Notification:
    id: str
    message: str
    level: str
    source: str
    created_at: float
    ttl_seconds: float

    def expires_at(self) -> float:
        return self.created_at + self.ttl_seconds

    def to_public(self, now: float) -> dict[str, Any]:
        return {
            "notification_id": self.id,
            "message": self.message,
            "level": self.level,
            "source": self.source,
            "age_s": round(now - self.created_at, 3),
            "ttl_remaining_s": max(0.0, round(self.expires_at() - now, 3)),
        }


# Callback that actually paints the banner: (Notification) -> awaitable.
RenderCallback = Callable[[Notification], Awaitable[None]]


def _validate_message(message: str) -> str:
    if not isinstance(message, str) or message == "":
        raise safety.ValidationError("message must be a non-empty string")
    if len(message) > MAX_MESSAGE_CHARS:
        raise safety.ValidationError(
            f"message must be at most {MAX_MESSAGE_CHARS} characters"
        )
    return message


def _validate_level(level: str) -> str:
    if level not in LEVELS:
        raise safety.ValidationError(f"level must be one of: {', '.join(LEVELS)}")
    return level


def _validate_source(source: str) -> str:
    if not isinstance(source, str) or source.strip() == "":
        raise safety.ValidationError("source is required (label the agent/session)")
    return source.strip()[:64]


def _validate_ttl(ttl_seconds: Any) -> float:
    try:
        ttl = float(ttl_seconds)
    except (TypeError, ValueError):
        raise safety.ValidationError("ttl_seconds must be a number")
    if ttl <= 0 or ttl > MAX_TTL:
        raise safety.ValidationError(
            f"ttl_seconds must be between 1 and {int(MAX_TTL)}"
        )
    return ttl


class NotificationStore:
    """Persisted notification queue with TTL expiry + display preemption.

    Persistence is a small JSON file written atomically. The DisplayState stack
    is in-memory (the screen contents are volatile anyway), but the queue of
    ids survives restart so a later ``clear_notification(id)`` is meaningful.
    """

    def __init__(
        self,
        *,
        path: str,
        display_state: Optional[DisplayState] = None,
        render: Optional[RenderCallback] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = path
        self._display = display_state or DisplayState(clock=clock)
        self._render = render
        self._clock = clock
        self._items: dict[str, Notification] = {}
        # Serialises the async mutator so a render-await can't interleave a
        # read-modify-write of the persisted queue (review T-3).
        self._lock = asyncio.Lock()
        self._load()

    # -- persistence ----------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        for raw in data.get("notifications", []):
            try:
                n = Notification(**raw)
            except TypeError:
                continue
            self._items[n.id] = n
        # On load, re-preempt for any surviving blocked notifications.
        self._expire()
        for n in self._items.values():
            if n.level == "blocked":
                self._display.preempt(
                    owner=n.source,
                    summary=f"[{n.level}] {n.message}",
                    ttl_seconds=n.ttl_seconds,
                    ref_id=n.id,
                )

    def _save(self) -> None:
        payload = {"notifications": [asdict(n) for n in self._items.values()]}
        d = os.path.dirname(os.path.abspath(self._path)) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".notify-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- expiry ---------------------------------------------------------------

    def _expire(self) -> list[Notification]:
        """Drop TTL-expired notifications (review smaller-flags: enforce TTL)."""
        now = self._clock()
        expired = [n for n in self._items.values() if now >= n.expires_at()]
        for n in expired:
            self._items.pop(n.id, None)
            if n.level == "blocked":
                self._display.clear_preempt(ref_id=n.id)
        return expired

    # -- public API -----------------------------------------------------------

    async def notify_operator(
        self,
        *,
        message: str,
        level: str = "info",
        source: str,
        ttl_seconds: float = DEFAULT_TTL,
    ) -> dict[str, Any]:
        message = _validate_message(message)
        level = _validate_level(level)
        source = _validate_source(source)
        ttl = _validate_ttl(ttl_seconds)
        async with self._lock:
            return await self._notify_locked(message, level, source, ttl)

    async def _notify_locked(self, message, level, source, ttl):  # noqa: ANN001
        self._expire()

        n = Notification(
            id=uuid.uuid4().hex,
            message=message,
            level=level,
            source=source,
            created_at=self._clock(),
            ttl_seconds=ttl,
        )
        self._items[n.id] = n

        # blocked preempts the display; info/warn set a base layer.
        summary = f"[{level}] {message}"
        if level == "blocked":
            self._display.preempt(
                owner=source, summary=summary, ttl_seconds=ttl, ref_id=n.id
            )
        else:
            self._display.set_base(
                owner=source, summary=summary, ttl_seconds=ttl, ref_id=n.id
            )

        if self._render is not None:
            try:
                await self._render(n)
            except Exception as exc:  # noqa: BLE001 - render failure is non-fatal
                logger.warning("notify render failed for %s: %r", n.id, exc)

        self._save()
        return {
            "ok": True,
            "notification_id": n.id,
            "level": level,
            "slot": NOTIFY_SLOT,
            "message": f"Notification queued (slot {NOTIFY_SLOT}, volatile).",
        }

    async def clear_notification(
        self,
        notification_id: Optional[str] = None,
        source: Optional[str] = None,
    ) -> dict[str, Any]:
        """Remove notifications; restore the display.

        Precedence: by ``notification_id`` (one), else by ``source`` (all from
        that agent — the correct default for the Claude Code Stop hook so it only
        clears its own banners, review TOP-2), else all. Clear-of-unknown is a
        no-op (review smaller-flags).

        Acquires the same lock as ``notify_operator`` so a clear cannot interleave
        with an in-flight render (PR review: otherwise a finishing render could
        overwrite the board after the notification was cleared).
        """
        async with self._lock:
            return self._clear_locked(notification_id, source)

    def _clear_locked(
        self, notification_id: Optional[str], source: Optional[str]
    ) -> dict[str, Any]:
        self._expire()

        def _drop(n: Notification) -> None:
            self._items.pop(n.id, None)
            if n.level == "blocked":
                self._display.clear_preempt(ref_id=n.id)

        if notification_id is not None:
            n = self._items.get(notification_id)
            if n is None:
                return {"ok": True, "cleared": 0}  # unknown id → no-op
            _drop(n)
            self._save()
            return {"ok": True, "cleared": 1}

        if source is not None:
            src = source.strip()
            matches = [n for n in list(self._items.values()) if n.source == src]
            for n in matches:
                _drop(n)
            self._save()
            return {"ok": True, "cleared": len(matches)}

        cleared = len(self._items)
        for n in list(self._items.values()):
            _drop(n)
        self._save()
        return {"ok": True, "cleared": cleared}

    def list_notifications(self) -> dict[str, Any]:
        self._expire()
        now = self._clock()
        items = sorted(self._items.values(), key=lambda x: x.created_at)
        return {"notifications": [n.to_public(now) for n in items]}

    @property
    def display_state(self) -> DisplayState:
        return self._display
