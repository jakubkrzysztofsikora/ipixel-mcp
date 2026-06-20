#!/usr/bin/env python3
"""Minimal Mode A passthrough client for ipixel-mcp.

Any service on the tailnet can use this to push text or an image to the board by
calling the origin's MCP endpoint directly with the static bearer (PLAN §3 Mode A,
the "tailnet passthrough" path). This is the *machine* caller path -- base64 image
input is fine here (it's NOT how a Claude model should send images; see review
C-5). The origin still validates everything (size caps, font/animation enums).

Transport: stateless Streamable HTTP (PLAN C-1). We POST a single JSON-RPC
`tools/call` and read the JSON response -- no session id, no SSE, no init dance
needed for this simple one-shot usage against the stateless origin.

Dependencies:
    pip install httpx          # or use requests; see _post() for the swap

Environment:
    IPIXEL_URL     e.g. https://ipixel-board.<tailnet>.ts.net:8765/mcp
                   (or http://127.0.0.1:8765/mcp when run on the device)
    IPIXEL_TOKEN   the IPIXEL_STATIC_TOKEN value

Examples:
    python display.py text "Build passing" --color "#00ff00"
    python display.py image ./status.png
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

import httpx

MCP_PROTOCOL_VERSION = "2025-11-25"  # sent as MCP-Protocol-Version after init


def _endpoint() -> tuple[str, str]:
    url = os.environ.get("IPIXEL_URL")
    token = os.environ.get("IPIXEL_TOKEN")
    if not url or not token:
        sys.exit("set IPIXEL_URL and IPIXEL_TOKEN (the static bearer)")
    return url, token


def _post(method: str, params: dict) -> dict:
    """Send one JSON-RPC request to the stateless MCP origin and return result.

    The origin advertises NO OAuth on this path; a valid static bearer 200s,
    a missing/wrong one 401s (review E-1).
    """
    url, token = _endpoint()
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        # Streamable HTTP clients should accept both; the stateless origin
        # replies with application/json for a plain request/response call.
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, json=payload, headers=headers)
    if resp.status_code == 401:
        sys.exit("401 from origin: bad/missing IPIXEL_TOKEN")
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        sys.exit(f"MCP error: {body['error']}")
    return body.get("result", {})


def call_tool(name: str, arguments: dict) -> dict:
    """Invoke an MCP tool via tools/call and return its result block."""
    result = _post("tools/call", {"name": name, "arguments": arguments})
    # MCP returns {"content": [...], "isError": bool}. Surface tool-level errors.
    if result.get("isError"):
        sys.exit(f"tool {name} reported an error: {result.get('content')}")
    return result


def show_text(text: str, color: str, animation: str, speed: int) -> None:
    res = call_tool(
        "display_text",
        {"text": text, "color": color, "animation": animation, "speed": speed},
    )
    print(_text_of(res) or "displayed")


def show_image(path: str) -> None:
    data = Path(path).read_bytes()
    fmt = _format_of(path)
    b64 = base64.b64encode(data).decode("ascii")
    res = call_tool(
        "display_image",
        {"image_base64": b64, "format": fmt, "resize": "fit"},
    )
    # display_image is an async job (PLAN C-2): the result is a job id/status,
    # not a finished transfer. Print whatever the origin returns.
    print(_text_of(res) or "image submitted")


def _format_of(path: str) -> str:
    ext = Path(path).suffix.lower().lstrip(".")
    return {"jpg": "jpeg"}.get(ext, ext)  # png/gif/jpeg


def _text_of(result: dict) -> str | None:
    for block in result.get("content", []):
        if block.get("type") == "text":
            return block.get("text")
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Push text/image to the iPixel board")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("text", help="scroll text on the board")
    t.add_argument("text")
    t.add_argument("--color", default="#ffffff", help="hex, e.g. #00ff00")
    t.add_argument("--animation", default="scroll_left")
    t.add_argument("--speed", type=int, default=50, help="1-100")

    i = sub.add_parser("image", help="display a PNG/GIF/JPEG")
    i.add_argument("path")

    args = p.parse_args(argv)
    if args.cmd == "text":
        show_text(args.text, args.color, args.animation, args.speed)
    elif args.cmd == "image":
        show_image(args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
