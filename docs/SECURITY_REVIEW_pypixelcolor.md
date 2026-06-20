# Security Review — `pypixelcolor` / `iPixel-CLI`

> Cyberlegion-style security review + bug hunt, performed as input to the
> `ipixel-mcp` design. **No code in the upstream library was modified** — this is
> a report only. Findings here directly shape the threat model and the
> "expose a curated subset" decisions in [`PLAN.md`](./PLAN.md).

- **Target:** `github.com/lucagoc/pypixelcolor` (cloned at review time), version `0.4.0`
- **Scope:** Full source under `src/pypixelcolor/` (~3,244 LOC), packaging, dependency hygiene
- **Deployment context assessed:** library wrapped by an MCP server, reachable over a Tailscale tailnet and proxied to the public internet via a Cloudflare Worker
- **Date:** 2026-06-20

---

## Executive summary

`pypixelcolor` is a thin BLE control library plus a CLI **and a bundled WebSocket
control server**. There is **no use of `eval`/`exec`/`pickle`/`yaml.load`/`os.system`/
`subprocess`** anywhere — that entire class of issue is **absent** (confirmed by grep
across `src/`). The most serious problems are **architectural**, not
memory-corruption:

1. The bundled **WebSocket server has zero authentication, zero authorization, and
   no input validation**, yet it dispatches to command functions that **open
   arbitrary local files** (`send_image`) and decode attacker-controlled image
   bytes through Pillow (`send_image_hex`). Wrapped naively by an internet-proxied
   MCP server, this becomes a remote, unauthenticated **arbitrary-file-probe /
   image-parser-DoS** surface.
2. **No image size / pixel limits** anywhere (`Image.open` without `MAX_IMAGE_PIXELS`,
   no GIF frame cap, no upload size cap) → decompression-bomb / memory-exhaustion DoS.
3. **Unpinned dependencies** (`websockets`, `bleak`, `pillow`, `crccheck`) in both
   `requirements.txt` and `pyproject.toml` — supply-chain and known-CVE exposure
   (notably Pillow).

Plus several genuine correctness bugs (broken `set_pixel` color validation,
`bytes([num_chars])` overflow, unclosed `PIL.Image` handles, forgeable BLE ACK logic).

**Overall risk:** **Medium** as a local CLI/library. **High** once the bundled
WebSocket server (or an equivalent MCP wrapper exposing the same command set) is
reachable from an untrusted network. The command surface was designed for a
*trusted local operator* and does not defend against a hostile caller.

---

## Findings

### F-1 — WebSocket control server: no authentication / authorization (Critical)

**Severity: Critical** — `AV:N / AC:L / PR:N / UI:N / S:U` → unauthenticated remote
control of the device and the host's command surface.

**File:** `src/pypixelcolor/websocket.py:34-122`, `151-191`; docs `docs/getting_started/websocket.md`.

```python
# websocket.py
server = await websockets.serve(handle_websocket, ip, port)
...
message = await websocket.recv()
command_data = json.loads(message)
command_name = command_data.get("command")
params = command_data.get("params", [])
...
elif command_name in COMMANDS:
    positional_args, keyword_args = build_command_args(params)
    command_func = COMMANDS[command_name]
    result = await _device_session.execute_command(command_func, *positional_args, **keyword_args)
```

No token, no origin check, no TLS, no per-message auth. Any client that can open a
TCP/WS connection drives the full `COMMANDS` set (`clear` wipes device settings,
`send_image`, `send_image_hex`, `send_text`, `delete`, …). Default bind is
`localhost` (good) — **but the shipped docs explicitly instruct users to bind
`0.0.0.0`**, and `--host` accepts any value with no warning.

**Impact:** With the server on a routable interface (or proxied by an MCP layer), an
attacker sends `{"command":"clear"}` to wipe the device, or floods commands. In the
stated Cloudflare→Tailscale→MCP topology, anything reaching the MCP endpoint that is
proxied to these handlers inherits this lack of auth.

**Remediation:** Require a shared secret / bearer token on connect; bind loopback by
default and refuse `0.0.0.0` without an explicit `--insecure` ack; add TLS (`wss`);
per-connection rate limiting. In the MCP wrapper, do **not** expose `clear`,
`delete`, `send_image` (path), or `send_image_hex` to untrusted callers.

---

### F-2 — Remote arbitrary file open / read-amplification via `send_image` path (High)

**Severity: High** — `AV:N / AC:L / PR:N`; confidentiality + availability.

**File:** `src/pypixelcolor/commands/send_image.py:363-402`; reached from
`websocket.py:86` / `cli.py:44` via untrusted `params`.

```python
if isinstance(path, str):
    path = Path(path)
...
if path.exists() and path.is_file():
    with open(path, "rb") as f:
        file_bytes = f.read()
    file_bytes, is_gif = _process_loaded_bytes(file_bytes, path.suffix.lower())
else:
    raise ValueError(f"File not found: {path}")
```

`path` flows straight from `params` (`build_command_args` → `execute_command`). No
allow-list, no base-directory confinement, no traversal check. A remote client can
request `{"command":"send_image","params":["path=/etc/passwd"]}` or
`path=/home/user/.ssh/id_rsa`.

**Impact:**
- **File-existence oracle** — the error string distinguishes "File not found" vs. a
  parse error, and `websocket.py:115` returns `str(cmd_error)` to the client (see F-9).
- **Read amplification / DoS** — any readable file is fully read into memory
  (`f.read()`) and pushed through Pillow; a device file or huge file → exhaustion.
- Bytes go to the BLE device, not the attacker, but timing/error differences make it
  an information-disclosure + DoS primitive.

**Remediation:** Confine to a configured base dir; `Path(path).resolve()` and verify
it's within an allow-listed root; reject absolute paths / `..` from network callers.
In the MCP wrapper, never let a remote caller supply a filesystem path — accept image
**bytes** only.

---

### F-3 — No image decompression-bomb / size limits (Pillow) (High)

**Severity: High** — `AV:N / AC:L / PR:N`; availability (memory + CPU DoS).

**Files:** `send_image.py:177,315` (`Image.open(BytesIO(...))`), `:199-278` (per-GIF-frame
`.copy()`/resize/re-encode), `image_processing.py:52,203` (`ImageFont.truetype`),
`emoji_manager.py:119` (`Image.open` of downloaded bytes).

`Image.open` is never given a guarded `MAX_IMAGE_PIXELS`/load budget. Pillow's default
`DecompressionBombWarning` (~89.5M px) is **only a warning, not an error**, so a
crafted PNG/TIFF/WEBP with huge declared dimensions allocates large buffers.
`send_image_hex` takes **attacker-supplied image bytes over the network** with no size
cap on the hex string and no pixel cap. GIFs explode per-frame with no frame-count cap.

**Exploit:** `{"command":"send_image_hex","params":["hex_string=<bomb>","file_extension=.png"]}`
→ OOM / CPU spin on the host.

**Remediation:** Set `Image.MAX_IMAGE_PIXELS` to a small device-appropriate bound and
treat the warning as an error; cap input byte length before decode; cap GIF frame
count; `img.draft()`/verify dimensions before full decode. Pin Pillow (F-7).

---

### F-4 — Unbounded `execute_command` kwarg/positional injection from network (High)

**Severity: High** — `AV:N / AC:L / PR:N`; enables F-2/F-3 and broad misuse.

**File:** `lib/device_session.py:186-219`; dispatch `websocket.py:79-86`,
`cli.py:41-44`; parsing `websocket.py:21-31` (`build_command_args`).

```python
sig = inspect.signature(command_func)
if 'device_info' in sig.parameters and self._device_info is not None:
    kwargs['device_info'] = self._device_info
plan = command_func(*args, **kwargs)
```

`build_command_args` turns every `"key=value"` token from JSON `params` into a kwarg
and everything else into a positional, then splats them into the command function.
No schema, no per-command parameter allow-list, no type coercion. All values arrive as
**strings**. This is the mechanism that reaches `send_image(path=...)` and
`send_image_hex(...)`. A caller-supplied `device_info=` is only overwritten when
device_info is present; otherwise a forged value reaches commands.

**Remediation:** Define an explicit per-command parameter schema (names, types,
ranges); validate/whitelist before dispatch; reject unknown params; coerce types
centrally.

---

### F-5 — `set_pixel` color validation is logically broken; unbounded coordinate bytes (Medium)

**Severity: Medium** — validation bypass + uncaught exception.

**File:** `src/pypixelcolor/commands/set_fun_mode.py:33-71`.

```python
if (not (isinstance(color, str))
    and len(color) == 6
    and all(c in '0123456789abcdefABCDEF' for c in color)):
        raise ValueError("Color must be a 6-character hexadecimal string.")
```

Inverted logic: raises **only** when `color` is NOT a string AND length 6 AND all hex
— for any real string this is `False`, so **validation never runs**. Downstream
`int(color[0:2],16)` then throws unguarded on bad input. Also `int(x)`/`int(y)` go
straight into `bytes([...])` (`:67-68`); without `device_info` the range check at
`:51` is skipped, so `x=300` → `ValueError: bytes must be in range(0, 256)`.

**Remediation:** Fix to `if not (isinstance(color, str) and len(color) == 6 and all(...)):`.
Always range-check/clamp `x`,`y` to the matrix even without device_info.

---

### F-6 — `bytes([num_chars])` overflow for long text (Medium)

**Severity: Medium** — uncaught exception on valid-looking input (trivial remote DoS).

**File:** `src/pypixelcolor/commands/send_text/__init__.py:175`, length check `:83`.

```python
(len(text), 1, 500, "Text length"),   # allows up to 500 chars
...
data_payload = bytes([num_chars]) + properties + characters_bytes
```

Text length allowed up to **500**, but `bytes([num_chars])` requires `num_chars ≤ 255`.
Both in fixed-width (`num_chars = len(text)`) and var-width (chunk/emoji count) modes
this raises `ValueError` well before 500. A 300-char `send_text` always crashes.

**Remediation:** Cap effective char/chunk count at 255 with a clear error, or encode
the count in the width the protocol expects; reconcile the `1..500` limit.

---

### F-7 — Unpinned dependencies / known-vulnerable Pillow exposure (Medium)

**Severity: Medium** — supply chain + known-CVE risk.

**Files:** `requirements.txt` (no specifiers), `pyproject.toml:13-18` (all unpinned),
optional `pillow-heif>=1.0.0`.

```
websockets
bleak
pillow
crccheck
```

No floors, ceilings, or hashes. **Pillow** is the highest-risk dependency given the
untrusted-image surface (F-3) — historically multiple parser/memory CVEs
(e.g. CVE-2023-50447 EPS, libwebp/TIFF/GIF issues). HEIF support adds libheif/libde265
native attack surface. Builds are non-reproducible.

**Remediation:** Pin to known-good patched versions with a lockfile + hashes; add
Dependabot / `pip-audit` to CI; treat Pillow upgrades as security-relevant.

---

### F-8 — Forgeable / over-broad BLE ACK handling; no length validation (Medium)

**Severity: Medium** — protocol robustness vs. a malicious/compromised BLE peer.

**File:** `src/pypixelcolor/lib/transport/ack_manager.py:21-45`; consumed in
`send_plan.py:101-111`.

Any notification frame with byte0 `0x05` and byte4 ∈ {0,1,3} is accepted as a valid
ACK — no CRC/sequence/window-index validation, no correlation to the window just sent.
A BLE peer (or MITM during Just-Works pairing, F-10) can forge ACKs or send
`data[4]==3` early to short-circuit a multi-window transfer. The second `if` block is
partly dead and also accepts arbitrarily long frames starting `0x05`.

**Remediation:** Validate the full ACK frame (length, opcode, window index, optional
CRC) before signaling; tie ACK to the in-flight window.

---

### F-9 — Internal error/exception detail leaked to WebSocket clients (Medium)

**Severity: Medium** — information disclosure.

**File:** `src/pypixelcolor/websocket.py:104-119`.

Raw exception strings are returned to the unauthenticated client — including absolute
filesystem paths ("File not found: /home/user/…"), Pillow decoder errors, and Python
type errors. Directly enables the file-existence oracle (F-2) and recon.

**Remediation:** Return generic error codes to clients; log details server-side only.

---

### F-10 — BLE trust model: address-only targeting, no pairing/MITM hardening (Low/Info)

**Files:** `lib/device_session.py:110` (`BleakClient(self._address, ...)`),
`cli.py:54-64` (scan trusts any device whose name contains `"LED"`).

Connects by MAC only with whatever pairing the device negotiates (iPixel devices use
"Just Works"). No device identity verification → an attacker advertising the same
name/address can impersonate the device (enabling F-8) or capture commands. Largely
inherent to the device class. No injection from the address into payloads.

**Remediation:** Document the trust assumption; require bonded/authenticated pairing
where supported; don't auto-select scanned devices by name.

---

### F-11 — Resource leaks: unclosed `PIL.Image` and file handles (Low)

**Files:** `send_image.py:177,315`; `emoji_manager.py:103,119`;
`image_processing.py:50,201`.

`Image.open(...)` results are never closed / used as context managers.
`emoji_manager.download_emoji` does `Image.open(cache_path)` and returns it, leaving
the file handle open (Pillow lazy-loads). A long-running server (F-1) leaks descriptors.

**Remediation:** Use `with Image.open(...) as img:` and `.load()`/`.copy()` before
close; close BytesIO/file objects.

---

### F-12 — Twemoji network fetch on text rendering (Low / SSRF-limited)

**File:** `src/pypixelcolor/lib/emoji_manager.py:83-129`.

`send_text` triggers `urllib.request.urlopen(url)` to a **hardcoded** CDN; the path is
derived from emoji codepoints (hex of `ord(c)`), **not** free-form text, so classic
(arbitrary-host) SSRF is **absent**. Residual issues: (a) processing untrusted
*downloaded* bytes through Pillow (same as F-3); (b) a remote `send_text` caller can
force many outbound requests (5s timeout each, no overall budget) → latency/traffic
amplification; (c) `@latest` Twemoji URL is non-reproducible. Cache filename is
hyphen-joined hex — **not** traversable.

**Remediation:** Make emoji fetching opt-in / offline-bundled; cap fetches per request;
apply F-3 limits to downloaded images; pin a Twemoji version.

---

### F-13 — Minor correctness / robustness notes (Info)

- `websocket.py:112` mutates `_device_session._connected` (private) on a substring
  match of the error message — fragile, reaches into another object's internals.
- `reconnect_device` (`websocket.py:129-148`) is an infinite loop racing with
  `handle_websocket` over a global `_device_session` reassigned without synchronization.
- `send_plan.py:72-143` swaps the notify handler globally during response-capture
  commands; concurrent commands on the shared session corrupt ACK routing (no per-session lock).
- `send_image.py:240,247` `int(d)` over GIF durations/disposal raises uncaught on
  malformed frames.
- `device_info.py:117` dead commented debug override.
- **No ReDoS** (no `re` in `src/`), **no insecure temp/randomness** — both **absent**.
- Logging includes BLE MAC + full frame hex at DEBUG (frame hex can include rendered
  user content); no secrets logged (none exist).

---

## Explicit checklist results

| # | Item | Result |
|---|------|--------|
| 1 | `eval`/`exec`/`pickle`/`yaml.load`/`os.system`/`subprocess(shell=True)` | **Absent.** None anywhere in `src/`. |
| 2 | Path traversal / arbitrary file read-write | **Present (High).** `send_image(path=...)` opens any path from untrusted `params` (F-2). `send_text(font=<path>)` opens arbitrary `.ttf`/`.json` — same class, lower exposure. Emoji cache path **not** traversable. |
| 3 | Image/font parsing attack surface | **Present (High).** No `MAX_IMAGE_PIXELS`, no size/frame caps; untrusted GIF/PNG/WEBP/HEIF + downloaded emoji bytes into Pillow; `ImageFont.truetype` on arbitrary TTF (F-3, F-12). Pillow unpinned (F-7). |
| 4 | WebSocket transport | **Server**, defaults `localhost` but **docs push `0.0.0.0`**; no auth/TLS/schema; returns raw error strings; unrestricted command/param dispatch (F-1, F-4, F-9). No OS command injection. |
| 5 | BLE transport | Address-only trust, Just-Works pairing, forgeable ACKs; no injection from address into framing (F-8, F-10). |
| 6 | CRC / framing / ACK / window / send_plan | Outbound CRC correct (`binascii.crc32`, little-endian). **Inbound frames not CRC/length/sequence-validated** (F-8). Chunking is bounds-safe (`min`), **no overflow/off-by-one**, but **no upper bound on total payload/window count** → memory DoS. `bytes([num_chars])` overflow (F-6). No accidental infinite loops in send paths. |
| 7 | Dependency hygiene | **Poor** — fully unpinned (F-7). |
| 8 | Logging/temp/randomness/DoS/ReDoS | No ReDoS, no temp/randomness issues; MAC + frame hex at DEBUG; **missing size/length limits → DoS** (F-3, F-6, F-12). |
| 9 | General correctness bugs | Broken `set_pixel` validation (F-5), `num_chars` overflow (F-6), unclosed handles (F-11), shared-session/global races (F-13), dead code (F-13). |

---

## Prioritized Top-5

1. **F-1 — Unauthenticated WebSocket control server** (Critical) — auth + TLS + loopback-by-default before any network exposure.
2. **F-2 — Remote arbitrary file open via `send_image` path** (High) — confine to an allow-listed dir; never accept paths from untrusted callers.
3. **F-3 — No image decompression-bomb / size limits** (High) — `MAX_IMAGE_PIXELS`, cap input bytes + GIF frames; bomb warning → error.
4. **F-4 — Unvalidated command/param dispatch** (High) — per-command schema + type/range validation; whitelist params.
5. **F-7 — Unpinned dependencies (esp. Pillow)** (Medium→High given F-3) — pin + lockfile + `pip-audit`/Dependabot.

---

## Suitability for a network-exposed MCP server (Tailscale + Cloudflare proxy)

**Do not expose the existing command surface as-is to untrusted callers.**

- The library's own WebSocket server (`websocket.py`) must **never** be the thing
  Cloudflare proxies — no auth (F-1), leaks errors (F-9). If the MCP wrapper merely
  forwards `{command, params}` to `execute_command`, it inherits **all** of
  F-1/F-2/F-3/F-4 verbatim.
- Tailscale gives network-layer ACLs + identity; **rely on that as the primary trust
  boundary** and keep the listener bound to the tailnet interface or loopback, not
  `0.0.0.0`. Cloudflare proxying to the internet **dramatically raises** the impact of
  every finding — only do so behind Cloudflare Access (authenticated) + an app-layer token.
- In the MCP tool layer, **expose a curated, validated subset only** (`set_brightness`,
  `set_power`, `set_text` with length cap per F-6, `set_clock_mode`). **Exclude or
  strictly gate** `clear`/`delete` (destructive) and any path-based `send_image` (F-2).
  Accept only pre-validated, size-capped image bytes processed with `MAX_IMAGE_PIXELS`
  + a frame cap.
- Add app-layer **auth, input schemas, rate limiting, request-size limits** in the MCP
  wrapper regardless of network controls (defense in depth; protects against a
  compromised tailnet peer).
- Disable/sandbox network emoji fetching (F-12) and pin Pillow (F-7) before exposure.

**Bottom line:** Safe to embed **only** if the MCP wrapper enforces auth, a strict
command/param allow-list, image size/pixel/frame limits, path confinement, and generic
error messages — and the listener stays off the public interface (loopback/Tailscale-only)
behind Cloudflare Access. Naive pass-through wrapping would create a remotely reachable,
unauthenticated file-probe and parser-DoS surface.
