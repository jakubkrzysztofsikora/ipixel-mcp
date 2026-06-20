# ipixel-mcp origin server (Phase 0)

Stateless MCP origin that drives a single iPixel Color BLE LED matrix through a
hardened wrapper around [`pypixelcolor`](https://github.com/lucagoc/pypixelcolor).
See [`../docs/PLAN.md`](../docs/PLAN.md) (v2) and
[`../docs/PLAN_REVIEW.md`](../docs/PLAN_REVIEW.md) for the design and rationale.

## What's here (Phase 0)

- `ipixel_mcp/device.py` — **disposable-link** BLE manager: single-flight lock,
  per-op `asyncio.wait_for` timeouts, **retry-once-on-disconnect** (recycles the link,
  which also clears the device's half-transfer state), reconnect supervisor + circuit
  breaker, MTU check, and `health()`.
- `ipixel_mcp/safety.py` — input validation/limits implementing the security-review
  controls (F-2 no paths / font allow-list, F-3 image byte caps, F-5 colour validation,
  F-6 text count cap, H-BOOTLOOP per-model animation gating). Stdlib-only.
- `ipixel_mcp/auth.py` — single origin authorization function: verified Cloudflare
  Access JWT (Worker) → static bearer (Claude Code) → plain 401. **No OAuth advertised**
  (Claude Code #59467 / review E-1).
- `ipixel_mcp/modes/display.py` — Mode A: `display_text`, `get_device_info`.
- `ipixel_mcp/app.py` — stateless FastMCP app + pure-ASGI bearer auth + `/healthz`.

Modes B (`notify.*`) and C (`gallery.*`), async image jobs, and the display
ownership/state stack arrive in Phase 2.

## Develop & test

Tests are hardware-free (BLE is injected via a fake client factory; the core is
stdlib-only):

```bash
cd server
python3 -m pytest -q
```

## Run (on the device host)

```bash
pip install -e .            # pulls mcp, uvicorn, starlette, pypixelcolor, Pillow, bleak
export IPIXEL_ADDRESS="AA:BB:CC:DD:EE:FF"   # the board's BLE MAC
export IPIXEL_STATIC_TOKEN="$(openssl rand -hex 32)"
python3 -m ipixel_mcp       # serves http://127.0.0.1:8765 (loopback by design)
```

Add it to Claude Code over the tailnet (direct path, static bearer):

```bash
claude mcp add --transport http ipixel http://<host>:8765/mcp \
  --header "Authorization: Bearer $IPIXEL_STATIC_TOKEN"
```

> Phase 0 binds **loopback only** (review C-6). Tailnet/Cloudflare exposure is Phase 3–4.

## Known Phase 0 limitations (tracked in the plan)

- MTU is *checked and warned* but not yet *enforced* — `pypixelcolor` hardcodes a
  244-byte chunk size; a true fix needs a hardened/vendored library (Phase 1).
- Image display, notifications, and gallery are not wired yet (Phase 2).
- Dependencies are bounded but not hash-locked (Phase 1).
- BLE protocol/ACK behaviour is only exercisable on real hardware — see the manual
  smoke-test checklist in the plan; CI covers schema + manager logic with a fake device.
