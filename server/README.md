# ipixel-mcp origin server (Phases 0-2)

Stateless MCP origin that drives a single iPixel Color BLE LED matrix through a
hardened wrapper around [`pypixelcolor`](https://github.com/lucagoc/pypixelcolor).
See [`../docs/PLAN.md`](../docs/PLAN.md) (v2) and
[`../docs/PLAN_REVIEW.md`](../docs/PLAN_REVIEW.md) for the design and rationale.

## Layout

- `ipixel_mcp/device.py` — **disposable-link** BLE manager: single-flight lock,
  per-op `asyncio.wait_for` timeouts, retry-once-on-disconnect, reconnect
  supervisor + circuit breaker, MTU check, `health()`. (Phase 0)
- `device.py` calls the patched `pypixelcolor.AsyncClient` directly (lazy import);
  tools only ever pass validated bytes / scalars (never a filesystem path — F-2).
- **`../vendor/pypixelcolor/`** — a **security-patched fork** of the upstream
  library (see its `SECURITY-PATCHES.md`). The real bug fixes now live *in the
  library*: MTU-aware chunking (H-MTU), `set_pixel` validation (F-5),
  `num_chars` overflow (F-6), strict ACK frames (F-8), image bomb/frame guards
  (F-3), and optional WebSocket auth (F-1). Install it editable:
  `pip install -e ../vendor/pypixelcolor`.
- `ipixel_mcp/safety.py` — input validation + limits (F-2/F-3/F-5/F-6,
  H-BOOTLOOP) **plus** a hardened image decode (`decode_and_prepare_image`):
  sets `Image.MAX_IMAGE_PIXELS`, treats the decompression-bomb warning as an
  error, caps GIF frames + total decoded pixels, enforces the byte cap, and an
  encoded-output-size cap for the BLE transfer (review C-2). Pillow import is
  lazy; the frame sizer is injectable for hardware-free tests.
- `ipixel_mcp/auth.py` — origin authorization (verified CF Access JWT → static
  bearer → plain 401, **no OAuth advertised**) **plus** a real Cloudflare Access
  JWT verifier (`make_access_jwt_verifier`): RS256 signature against the team
  JWKS, `aud`/`iss`/`exp` checks. JWKS fetching and the RSA primitive are
  injectable; a pure-Python RS256 verifier ships as the default so it works
  without PyJWT/network.
- `ipixel_mcp/logging_utils.py` — redaction helpers so image/frame bytes are
  **never** logged (F-13).
- `ipixel_mcp/jobs.py` — async job registry for long media transfers; tools
  return a `job_id` immediately (review C-2).
- `ipixel_mcp/display_state.py` — display ownership/state **stack**:
  `blocked` notifications preempt the current display and restore it on clear
  (review M-OWN).
- `ipixel_mcp/modes/display.py` — Mode A: `display_text`, `get_device_info`,
  `display_image` (async **job**), `get_display_state`.
- `ipixel_mcp/modes/notify.py` — Mode B: `notify_operator`/`clear_notification`/
  `list_notifications`. Volatile display (slot 0, flash protection H-FLASH),
  blocked-preempt, **persisted JSON queue** (survives restart; clear-of-unknown
  is a no-op), **enforced TTL** auto-expiry.
- `ipixel_mcp/modes/gallery.py` — Mode C: `list_presets`/`show_preset` over a
  server-controlled `assets/manifest.json`; catalog also exposed as MCP
  **resources** (`gallery://presets/...`, review M-RES); SSRF-guarded
  `image_url` fetch path for model-friendly image display (review C-5).
- `ipixel_mcp/app.py` — stateless FastMCP app: registers all tools with
  `annotations` (`readOnlyHint` for reads, `destructiveHint` for clear),
  scope-gates tools via the auth `Principal` (no confirm-token args, review
  M-ANNOT), registers gallery resources, keeps `/healthz`. `mcp` is lazy-imported.
- `assets/` — sample presets (ascii art + short texts) + `manifest.json`.
- `constraints.txt` — tight version pins (incl. Pillow + bleak); per-artifact
  hashes / uv-lock land in CI.
- `Makefile` — `test`, `audit` (pip-audit), `lint` targets.

## Image provenance (review C-5)

- `show_preset(id)` — the cheap, model-friendly image path (server holds bytes).
- `display_image_url(url)` — server fetches with an SSRF guard + size/frame caps.
- `display_image(image_base64)` — **machine/passthrough only**; models should not
  base64 images themselves (huge token cost + timeouts).

All three slow image paths return a `job_id`; poll `get_job_status`.

## Develop & test

Tests are hardware-free (BLE, the image decoder, the URL fetcher, JWKS/crypto,
and clocks are all injected; the core is stdlib-only — `mcp`/`bleak`/`Pillow`/
`pypixelcolor` need not be installed):

```bash
cd server
python3 -m pytest -q     # or: make test
make lint                # offline byte-compile
make audit               # pip-audit against constraints.txt (needs network)
```

## Run (on the device host)

```bash
pip install -e ../vendor/pypixelcolor   # the security-patched fork (provides pypixelcolor)
pip install -e . -c constraints.txt
export IPIXEL_ADDRESS="AA:BB:CC:DD:EE:FF"
export IPIXEL_STATIC_TOKEN="$(openssl rand -hex 32)"
# optional Cloudflare Access (Worker path):
export IPIXEL_ACCESS_TEAM="myteam.cloudflareaccess.com"
export IPIXEL_ACCESS_AUD="<access-app-aud-tag>"
python3 -m ipixel_mcp       # serves http://127.0.0.1:8765 (loopback by design)
```

Add it to Claude Code over the tailnet (direct path, static bearer):

```bash
claude mcp add --transport http ipixel http://<host>:8765/mcp \
  --header "Authorization: Bearer $IPIXEL_STATIC_TOKEN"
```

## Known limitations (tracked in the plan)

- **MTU is now handled in the vendored fork** — `send_plan` chunks by the
  negotiated ATT MTU, so a degraded link slows down instead of corrupting; the
  server's `assert_mtu_ok` is now informational only.
- `constraints.txt` pins versions; per-artifact hashes + uv-lock land in CI.
- BLE protocol/ACK behaviour is only exercisable on real hardware — see the
  manual smoke-test checklist in the plan; CI covers schema + manager + mode
  logic with fakes.
- Public exposure (cloudflared + Worker OAuth) is Phase 3-4.
