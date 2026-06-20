# Tailnet service → board (Mode A passthrough)

A minimal reference for **any tailnet service** (CI, a home-automation box, a
status dashboard) to push text or an image to the iPixel board through the
origin's MCP endpoint, using the **static bearer** over the tailnet (PLAN §3
Mode A "passthrough").

This is the **machine caller** path: base64 image bytes are acceptable here.
That is explicitly **not** how a Claude model should send images (huge token cost
+ timeouts — review C-5); models use `show_preset` / `image_url` instead. The
origin validates everything regardless (byte caps, font/animation enums, F-2/3/6).

## Dependencies

```bash
pip install httpx        # the script uses httpx; requests works with a 1-line swap
```

## Configure

The endpoint is the stateless Streamable HTTP origin (PLAN C-1). On the tailnet it
advertises **no OAuth** — a valid static bearer `200`s, a bad one `401`s (E-1).

```bash
export IPIXEL_URL="https://ipixel-board.<tailnet>.ts.net:8765/mcp"
export IPIXEL_TOKEN="<the IPIXEL_STATIC_TOKEN value>"
# (when run on the device itself: IPIXEL_URL=http://127.0.0.1:8765/mcp)
```

## Use

```bash
python display.py text "Build passing" --color "#00ff00"
python display.py text "Deploying..." --animation scroll_left --speed 60
python display.py image ./status.png
```

## What it does

It POSTs a single JSON-RPC `tools/call` and reads the JSON response — no session
id, no SSE, no init handshake needed for one-shot calls against the stateless
origin. See [`display.py`](./display.py); the load-bearing pieces are:

- `Authorization: Bearer $IPIXEL_TOKEN` header (the only auth on this path).
- `MCP-Protocol-Version` header (required post-init by the spec).
- `display_text` returns a confirmation; **`display_image` returns a job
  reference**, not a finished transfer — image/animation writes are async jobs
  (PLAN C-2), so don't expect the panel to be updated the instant the call
  returns.

Adapt it to your own service by importing `call_tool(name, arguments)` and
calling any Mode A tool (`set_brightness`, `set_power`, `show_slot`, …).
