"""Display ownership / state stack (review M-OWN).

Three clients share one board with no identity and last-write-wins. This layer
gives the board a thin notion of *who owns the screen right now* and supports
Mode B preempt/restore:

- Every write records a ``DisplayEntry`` {owner/source, ttl, kind, summary}.
- A normal display *replaces* the current top-of-stack base layer.
- A Mode-B ``blocked`` notification *pushes* (preempts) and is *popped* on clear,
  restoring whatever was underneath (a stack, not last-write-wins).
- TTL is recorded so a reader (and the notify expiry sweep) can tell when an
  entry is stale; ``get_display_state`` reports remaining TTL.

Pure + stdlib (injectable clock); no device or mcp imports → fully testable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Entry kinds.
KIND_DISPLAY = "display"      # Mode A / C base content
KIND_NOTIFY = "notify"        # Mode B preempting banner
KIND_IDLE = "idle"            # nothing shown


@dataclass(frozen=True)
class DisplayEntry:
    owner: str               # source/agent label
    kind: str                # KIND_*
    summary: str             # short human description (never raw bytes — F-13)
    ttl_seconds: Optional[float] = None  # None = no expiry
    created_at: float = 0.0
    ref_id: Optional[str] = None         # e.g. a notification_id, for targeted pop

    def expires_at(self) -> Optional[float]:
        if self.ttl_seconds is None:
            return None
        return self.created_at + self.ttl_seconds


class DisplayState:
    """A stack of display entries; bottom is the base, top is what's shown."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # Always keep an idle base at the bottom.
        self._stack: list[DisplayEntry] = [
            DisplayEntry(owner="system", kind=KIND_IDLE, summary="idle", created_at=clock())
        ]

    # -- writes ---------------------------------------------------------------

    def set_base(
        self,
        *,
        owner: str,
        summary: str,
        kind: str = KIND_DISPLAY,
        ttl_seconds: Optional[float] = None,
        ref_id: Optional[str] = None,
    ) -> DisplayEntry:
        """Replace the current base layer (a normal Mode A/C display).

        If a notify entry is currently preempting, the new base goes *under* it
        (the preempt stays on top); otherwise it becomes the top.
        """
        entry = DisplayEntry(
            owner=owner,
            kind=kind,
            summary=summary,
            ttl_seconds=ttl_seconds,
            created_at=self._clock(),
            ref_id=ref_id,
        )
        # Find the lowest notify entry; insert the base just beneath it.
        insert_at = len(self._stack)
        for i, e in enumerate(self._stack):
            if e.kind == KIND_NOTIFY:
                insert_at = i
                break
        # Drop the previous base(s) below the notify layer, keep the idle floor.
        below = [e for e in self._stack[:insert_at] if e.kind == KIND_IDLE]
        above = self._stack[insert_at:]
        self._stack = below + [entry] + above
        return entry

    def preempt(
        self,
        *,
        owner: str,
        summary: str,
        ttl_seconds: Optional[float] = None,
        ref_id: Optional[str] = None,
    ) -> DisplayEntry:
        """Push a Mode-B ``blocked`` banner on top (preempt current display)."""
        entry = DisplayEntry(
            owner=owner,
            kind=KIND_NOTIFY,
            summary=summary,
            ttl_seconds=ttl_seconds,
            created_at=self._clock(),
            ref_id=ref_id,
        )
        self._stack.append(entry)
        return entry

    def clear_preempt(self, ref_id: Optional[str] = None) -> Optional[DisplayEntry]:
        """Pop a preempting notify entry, restoring what's underneath.

        With ``ref_id`` only the matching notify entry is removed; without it the
        top-most notify entry is removed. Returns the new top entry, or None if
        nothing matched (clear-of-unknown is a no-op).
        """
        idx = None
        if ref_id is not None:
            for i in range(len(self._stack) - 1, -1, -1):
                e = self._stack[i]
                if e.kind == KIND_NOTIFY and e.ref_id == ref_id:
                    idx = i
                    break
        else:
            for i in range(len(self._stack) - 1, -1, -1):
                if self._stack[i].kind == KIND_NOTIFY:
                    idx = i
                    break
        if idx is None:
            return None
        self._stack.pop(idx)
        return self.current()

    def sweep_expired(self) -> list[DisplayEntry]:
        """Remove TTL-expired entries (keeps the idle floor). Returns removed."""
        now = self._clock()
        removed: list[DisplayEntry] = []
        kept: list[DisplayEntry] = []
        for e in self._stack:
            exp = e.expires_at()
            if e.kind != KIND_IDLE and exp is not None and now >= exp:
                removed.append(e)
            else:
                kept.append(e)
        if not kept:
            kept = [DisplayEntry(owner="system", kind=KIND_IDLE, summary="idle", created_at=now)]
        self._stack = kept
        return removed

    # -- reads ----------------------------------------------------------------

    def current(self) -> DisplayEntry:
        """The entry currently shown (top of stack)."""
        return self._stack[-1]

    def get_display_state(self) -> dict[str, Any]:
        """Public read for the ``get_display_state`` tool (review M-OWN)."""
        self.sweep_expired()
        top = self.current()
        now = self._clock()
        remaining = None
        exp = top.expires_at()
        if exp is not None:
            remaining = max(0.0, round(exp - now, 3))
        return {
            "owner": top.owner,
            "kind": top.kind,
            "summary": top.summary,
            "ttl_remaining_s": remaining,
            "ref_id": top.ref_id,
            "preempted": top.kind == KIND_NOTIFY,
            "depth": len(self._stack),
        }
