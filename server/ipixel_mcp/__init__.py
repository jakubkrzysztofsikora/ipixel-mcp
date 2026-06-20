"""ipixel-mcp origin server (Phase 0).

A stateless MCP origin that drives a single iPixel Color BLE LED matrix through a
hardened wrapper around `pypixelcolor`. The BLE link is treated as *disposable*:
all device access goes through `DeviceManager`, which serializes operations behind
one lock, applies per-op timeouts, and runs a reconnect supervisor.

See ../docs/PLAN.md (v2) and ../docs/PLAN_REVIEW.md for the design rationale.
"""

__version__ = "0.0.1"
