"""Disposable-link BLE device manager (review C-2 / C-3, Phase 0 centerpiece).

The iPixel panel's BLE link is treated as *disposable*, not persistent:

- **Single-flight lock** — exactly one operation touches the device at a time.
  pypixelcolor globally swaps the notify handler mid-command, so concurrency
  corrupts ACK routing (security review F-8/F-13). Serialization is mandatory.
- **Per-op timeout** — every operation is wrapped in ``asyncio.wait_for`` at *our*
  layer; the library's writes have no timeout and can hang forever.
- **Retry-once on a connection error** — on timeout/disconnect we recycle the link
  (disconnect + reconnect) once before giving up, which also resets the device's
  half-transfer state machine so a failed window can't wedge the display.
- **Reconnect supervisor + circuit breaker** — a background task keeps trying to
  (re)establish the link with backoff; consecutive failures are tracked and
  surfaced via ``health()``.
- **MTU check** — warns when the negotiated ATT MTU is below what the library's
  hardcoded 244-byte chunking assumes (a small MTU silently corrupts transfers).

The actual BLE client is injected via ``client_factory`` so this module is fully
testable without ``bleak``/``pypixelcolor`` installed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional, Protocol

logger = logging.getLogger("ipixel_mcp.device")

# ATT MTU the library's 244-byte chunk size assumes (247 - 3 ATT header).
EXPECTED_MTU = 247

# Substrings that indicate the BLE link dropped (mirrors how pypixelcolor and
# bleak surface disconnects). Used to decide whether to recycle + retry.
_DISCONNECT_MARKERS = (
    "disconnect",
    "not connected",
    "service discovery",
    "no longer connected",
    "connection lost",
    # ACK/window timeouts from pypixelcolor's send_plan: these leave the device's
    # transfer state machine mid-window, so the link MUST be recycled to avoid a
    # wedged panel (review H-WEDGE). The library raises e.g. "cur12k_no_answer".
    "no_answer",
    "no ack",
    "cur12k",
    "timed out",
)


class BleClient(Protocol):
    """The subset of pypixelcolor's AsyncClient we depend on."""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def get_device_info(self) -> Any: ...


# (address) -> BleClient
ClientFactory = Callable[[str], BleClient]


class DeviceError(Exception):
    """Generic, client-safe device error (internal detail is logged, not returned)."""


def _default_client_factory(address: str) -> BleClient:
    # Lazy import so tests don't require bleak/pypixelcolor.
    from pypixelcolor import AsyncClient  # type: ignore

    return AsyncClient(address)


def _looks_like_disconnect(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError)):
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _DISCONNECT_MARKERS)


class DeviceManager:
    """Owns the single BLE connection to the one board (single-board design)."""

    def __init__(
        self,
        address: str,
        *,
        client_factory: ClientFactory = _default_client_factory,
        op_timeout: float = 10.0,
        connect_timeout: float = 20.0,
        backoff_base: float = 2.0,
        backoff_max: float = 30.0,
        circuit_threshold: int = 5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.address = address
        self._factory = client_factory
        self._op_timeout = op_timeout
        self._connect_timeout = connect_timeout
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._circuit_threshold = circuit_threshold
        self._sleep = sleep
        self._clock = clock

        self._lock = asyncio.Lock()
        self._client: Optional[BleClient] = None
        self._state = "disconnected"  # disconnected | connecting | connected
        self._consecutive_failures = 0
        self._device_info: Any = None
        self._mtu: Optional[int] = None
        self._last_op_ok: Optional[float] = None
        self._closing = False
        self._supervisor: Optional[asyncio.Task] = None

    # -- lifecycle ------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def connected(self) -> bool:
        return self._state == "connected" and self._client is not None

    @property
    def device_info(self) -> Any:
        """Cached DeviceInfo (populated on connect, invalidated on drop)."""
        return self._device_info

    async def _connect_locked(self) -> None:
        """Establish the link. Caller must hold the lock."""
        if self.connected:
            return
        self._state = "connecting"
        client = self._factory(self.address)
        try:
            await asyncio.wait_for(client.connect(), self._connect_timeout)
        except Exception as exc:  # noqa: BLE001 - normalised below
            self._consecutive_failures += 1
            self._client = None
            self._state = "disconnected"
            logger.warning("connect to %s failed (%r)", self.address, exc)
            raise DeviceError("device unreachable") from exc

        self._client = client
        self._state = "connected"
        self._consecutive_failures = 0
        self._refresh_device_info(client)
        self._check_mtu(client)
        logger.info("connected to %s (mtu=%s)", self.address, self._mtu)

    async def _force_disconnect_locked(self) -> None:
        """Tear down the link and invalidate caches. Caller holds the lock."""
        client, self._client = self._client, None
        self._state = "disconnected"
        self._device_info = None
        self._mtu = None
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), self._connect_timeout)
            except Exception as exc:  # noqa: BLE001
                logger.debug("disconnect cleanup error (ignored): %r", exc)

    def _refresh_device_info(self, client: BleClient) -> None:
        try:
            self._device_info = client.get_device_info()
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_device_info failed: %r", exc)
            self._device_info = None

    def _check_mtu(self, client: BleClient) -> None:
        # pypixelcolor's AsyncClient does not expose MTU directly; the real value
        # lives on the underlying Bleak client at ._session._client.mtu_size
        # (review H-MTU — reading it off AsyncClient always returned None).
        mtu = (
            getattr(client, "mtu_size", None)
            or getattr(getattr(getattr(client, "_session", None), "_client", None), "mtu_size", None)
            or getattr(client, "mtu", None)
        )
        self._mtu = int(mtu) if isinstance(mtu, int) else None
        if self._mtu is not None and self._mtu < EXPECTED_MTU:
            logger.warning(
                "negotiated MTU %s < expected %s; the library's 244-byte chunking "
                "would corrupt transfers — image ops will be refused on this link",
                self._mtu,
                EXPECTED_MTU,
            )

    def assert_mtu_ok(self) -> None:
        """Refuse media transfers on a degraded link (review H-MTU).

        We cannot change pypixelcolor's hardcoded 244-byte chunk size, so when the
        negotiated MTU is known to be below what that chunking assumes we fail the
        transfer cleanly rather than silently garbling the panel. An unknown MTU
        (``None``) is allowed through (best effort) but logged at connect time.
        """
        if self._mtu is not None and self._mtu < EXPECTED_MTU:
            raise DeviceError(
                "BLE link MTU too small for a safe image transfer; reconnect the board"
            )

    # -- operation execution --------------------------------------------------

    async def execute(
        self,
        op_name: str,
        fn: Callable[[BleClient], Awaitable[Any]],
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        """Run ``fn(client)`` under the lock with timeout + retry-once-on-disconnect."""
        timeout = timeout if timeout is not None else self._op_timeout
        async with self._lock:
            last_exc: Optional[BaseException] = None
            for attempt in (1, 2):
                try:
                    await self._connect_locked()
                    result = await asyncio.wait_for(fn(self._client), timeout)  # type: ignore[arg-type]
                    self._last_op_ok = self._clock()
                    return result
                except DeviceError as exc:
                    # connect failed; honour circuit breaker, no point retrying fast
                    last_exc = exc
                    if self._consecutive_failures >= self._circuit_threshold:
                        raise
                    if attempt == 1:
                        continue
                    raise
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if _looks_like_disconnect(exc) and attempt == 1:
                        logger.warning(
                            "op %s failed (%r); recycling link and retrying", op_name, exc
                        )
                        await self._force_disconnect_locked()
                        continue
                    logger.error("op %s failed: %r", op_name, exc)
                    raise DeviceError(f"operation '{op_name}' failed") from exc
            raise DeviceError(f"operation '{op_name}' failed") from last_exc

    async def get_device_info(self) -> Any:
        """Return current device info (re-fetched on connect; cache invalidated on drop)."""
        async def _fn(client: BleClient) -> Any:
            self._refresh_device_info(client)
            return self._device_info

        return await self.execute("get_device_info", _fn, timeout=self._op_timeout)

    # -- supervisor -----------------------------------------------------------

    def _backoff(self) -> float:
        n = max(0, self._consecutive_failures - 1)
        return min(self._backoff_max, self._backoff_base * (2 ** n))

    async def ensure_ready(self) -> bool:
        """Best-effort (re)connect, used by the supervisor and /healthz warmup."""
        if self.connected:
            return True
        async with self._lock:
            if self.connected:
                return True
            try:
                await self._connect_locked()
                return True
            except DeviceError:
                return False

    async def run_supervisor(self, *, poll_interval: float = 15.0) -> None:
        """Background loop that keeps the link alive with backoff."""
        self._closing = False
        while not self._closing:
            ok = await self.ensure_ready()
            await self._sleep(poll_interval if ok else self._backoff())

    def start_supervisor(self, *, poll_interval: float = 15.0) -> asyncio.Task:
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(
                self.run_supervisor(poll_interval=poll_interval)
            )
        return self._supervisor

    async def close(self) -> None:
        self._closing = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._supervisor = None
        async with self._lock:
            await self._force_disconnect_locked()

    # -- health ---------------------------------------------------------------

    def health(self) -> dict:
        info = self._device_info
        device = None
        if info is not None:
            device = {
                "width": getattr(info, "width", None),
                "height": getattr(info, "height", None),
                "led_type": getattr(info, "led_type", None),
            }
        age = None
        if self._last_op_ok is not None:
            age = round(self._clock() - self._last_op_ok, 3)
        return {
            "address": self.address,
            "state": self._state,
            "connected": self.connected,
            "consecutive_failures": self._consecutive_failures,
            "circuit_open": self._consecutive_failures >= self._circuit_threshold,
            "mtu": self._mtu,
            "device": device,
            "last_op_ok_age_s": age,
        }
