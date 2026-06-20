# pypixelcolor — fork patches (ipixel-mcp)

This is a vendored fork of [`lucagoc/pypixelcolor`](https://github.com/lucagoc/pypixelcolor)
`v0.4.0` with the security/correctness fixes from
[`../../docs/SECURITY_REVIEW_pypixelcolor.md`](../../docs/SECURITY_REVIEW_pypixelcolor.md)
and the BLE review applied **at the source** (rather than worked around in the MCP
wrapper). Tests live in `tests_security/` (`python3 -m pytest tests_security -q`).

| Finding | File | Fix |
|---------|------|-----|
| **F-5** broken `set_pixel` colour validation (inverted condition → never fired) + unbounded coords | `commands/set_fun_mode.py` | corrected the boolean so non-hex/short colours are rejected; bound x/y to 0..255 with a clear error |
| **F-6** `bytes([num_chars])` overflow for long/emoji-heavy text | `commands/send_text/__init__.py` | reject when the encoded glyph/chunk count exceeds 255 with a clear message |
| **H-MTU** hardcoded 244-byte chunks corrupt transfers on a degraded link | `lib/transport/send_plan.py` | new `effective_chunk_size()` caps chunking at `mtu_size - 3`; `send_plan` chunks by the negotiated MTU; `mtu_size` exposed on `DeviceSession`/`AsyncClient` |
| **F-8** ACK forgery / oversized-frame misread | `lib/transport/ack_manager.py` | accept **only** the strict 5-byte `0x05` ACK frame; dropped the permissive fallback that accepted any length-≥5 frame |
| **F-3** image decompression-bomb + GIF frame explosion | `commands/send_image.py` | `MAX_IMAGE_PIXELS`/`MAX_GIF_FRAMES` guards via `_open_image_guarded()`; `Image.MAX_IMAGE_PIXELS` set so Pillow raises; used at both decode sites |
| **F-11** (partial) leaked PIL handle in the format-convert path | `commands/send_image.py` | `with _open_image_guarded(...) as img:` in `_process_loaded_bytes` |
| **F-1** unauthenticated WebSocket control server | `websocket.py` | optional shared-secret bearer (`--token` / `IPIXEL_WS_TOKEN`), constant-time check, loopback default kept, warns on non-loopback bind without a token |

## Not changed here (tracked elsewhere)
- **F-2** path-based `send_image(path=...)` arbitrary file open — the MCP server never
  calls the path API (it uses `send_image_hex` with validated bytes), so this is
  mitigated at the wrapper; a base-dir confinement patch upstream is still worth doing.
- **F-12** Twemoji network fetch — the MCP server keeps emoji rendering offline; not
  altered here.
- Per-chunk `response=True` throughput and the `stop/start_notify` handler-swap race
  are behavioural/perf items left as-is to minimise divergence from upstream.

These patches are intended to be offered back upstream as PRs.
