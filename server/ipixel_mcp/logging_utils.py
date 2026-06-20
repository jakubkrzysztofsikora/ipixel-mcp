"""Logging hygiene helpers (security review F-13).

pypixelcolor logs full BLE frame hex at DEBUG, and frame hex can encode rendered
user content. Our code must NEVER log image/frame bytes. These helpers make it
easy to log a *redacted* summary (length + a short fingerprint) instead of the
bytes themselves.

Stdlib-only; no heavy imports.
"""

from __future__ import annotations

import hashlib
from typing import Any

# How many hex chars of the digest to show. Enough to correlate logs, far too
# little to reconstruct content.
_FINGERPRINT_LEN = 12


def redact_bytes(data: Any) -> str:
    """Summarise a bytes-like value as ``<bytes len=N sha256=abc123…>``.

    Never returns the raw bytes. Safe to drop into a log line for an image/frame
    payload (F-13).
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return f"<non-bytes {type(data).__name__}>"
    raw = bytes(data)
    digest = hashlib.sha256(raw).hexdigest()[:_FINGERPRINT_LEN]
    return f"<bytes len={len(raw)} sha256={digest}>"


def redact_hex(hex_string: Any) -> str:
    """Summarise a hex *string* (e.g. an encoded frame) without echoing it."""
    if not isinstance(hex_string, str):
        return f"<non-str {type(hex_string).__name__}>"
    digest = hashlib.sha256(hex_string.encode("ascii", "ignore")).hexdigest()[:_FINGERPRINT_LEN]
    return f"<hex chars={len(hex_string)} sha256={digest}>"
