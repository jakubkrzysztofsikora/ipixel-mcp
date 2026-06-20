"""Thin hardened adapter over ``pypixelcolor.AsyncClient`` (Phase 1 vendoring).

We deliberately do NOT vendor the whole upstream library (security review: the
bundled WebSocket server, path-based ``send_image``, and broken validation are
all things we never want in-tree). Instead this adapter:

- lazily imports ``pypixelcolor`` so the module is importable/testable without it;
- reads and enforces the negotiated ATT MTU, deriving the safe chunk size
  (``mtu - 3``) and refusing/segmenting when the MTU is too small (review H-MTU);
- exposes only the narrow method set our tools need:
  ``connect`` / ``disconnect`` / ``get_device_info`` / ``send_text`` /
  ``send_image_hex``;
- never accepts a filesystem path (F-2) — images are bytes/hex only.

Known upstream limitation: ``pypixelcolor`` hardcodes a 244-byte chunk size
(assumes a 247-byte MTU). On a degraded reconnect the MTU can fall back to 23,
in which case every chunk overflows and the transfer garbles. Until we land an
upstream patch that honours ``chunk_size``, this adapter *refuses* a transfer
when the MTU is below ``MIN_SAFE_MTU`` rather than silently corrupting the panel.

The underlying client is injected via ``client_factory`` so tests use a fake.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional, Protocol

from .logging_utils import redact_hex

logger = logging.getLogger("ipixel_mcp.ble_backend")

# ATT MTU the upstream 244-byte chunking assumes (247 - 3 ATT header).
UPSTREAM_MTU = 247
UPSTREAM_CHUNK = 244
# Below this we refuse rather than corrupt (review H-MTU). Anything < upstream's
# assumption risks overflow with the hardcoded chunk size.
MIN_SAFE_MTU = UPSTREAM_MTU


class RawClient(Protocol):
    """The subset of pypixelcolor.AsyncClient we call into."""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def get_device_info(self) -> Any: ...
    async def send_text(self, **kwargs: Any) -> Any: ...
    async def send_image_hex(self, **kwargs: Any) -> Any: ...


RawClientFactory = Callable[[str], RawClient]


class BackendError(Exception):
    """Generic backend failure (detail logged, never surfaced raw — F-9)."""


def _default_raw_factory(address: str) -> RawClient:
    from pypixelcolor import AsyncClient  # type: ignore

    return AsyncClient(address)


def derive_chunk_size(mtu: Optional[int]) -> int:
    """Safe write chunk size for an MTU, or raise if too small (H-MTU)."""
    if mtu is None:
        # Unknown MTU: assume the upstream default rather than guessing larger.
        return UPSTREAM_CHUNK
    if mtu < MIN_SAFE_MTU:
        raise BackendError(
            f"negotiated MTU {mtu} below safe minimum {MIN_SAFE_MTU}; "
            "refusing transfer to avoid corrupting the panel"
        )
    return mtu - 3


class BleBackend:
    """Adapter wrapping one raw client. Owns no locking — the DeviceManager does."""

    def __init__(
        self,
        address: str,
        *,
        client_factory: RawClientFactory = _default_raw_factory,
    ) -> None:
        self.address = address
        self._factory = client_factory
        self._client: Optional[RawClient] = None

    @property
    def client(self) -> Optional[RawClient]:
        return self._client

    def read_mtu(self) -> Optional[int]:
        """Read the negotiated MTU from the raw client (best effort)."""
        c = self._client
        if c is None:
            return None
        mtu = getattr(c, "mtu_size", None) or getattr(c, "mtu", None)
        return int(mtu) if isinstance(mtu, int) else None

    def enforce_mtu(self) -> int:
        """Return a safe chunk size for the current link or raise (H-MTU)."""
        return derive_chunk_size(self.read_mtu())

    async def connect(self) -> None:
        client = self._factory(self.address)
        await client.connect()
        self._client = client

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.disconnect()

    def get_device_info(self) -> Any:
        if self._client is None:
            raise BackendError("not connected")
        return self._client.get_device_info()

    async def send_text(self, **kwargs: Any) -> Any:
        if self._client is None:
            raise BackendError("not connected")
        return await self._client.send_text(**kwargs)

    async def send_image_hex(self, *, hex_string: str, file_extension: str, **kwargs: Any) -> Any:
        """Send pre-validated, pre-encoded image hex (bytes-only path; never F-2).

        Enforces a safe MTU first so a degraded link refuses rather than garbles.
        """
        if self._client is None:
            raise BackendError("not connected")
        chunk = self.enforce_mtu()  # raises if MTU unsafe
        logger.info(
            "send_image_hex ext=%s chunk=%s payload=%s",
            file_extension,
            chunk,
            redact_hex(hex_string),  # F-13: never log the frame bytes
        )
        return await self._client.send_image_hex(
            hex_string=hex_string, file_extension=file_extension, **kwargs
        )
