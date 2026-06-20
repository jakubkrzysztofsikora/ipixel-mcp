# `ipixel-mcp` — Adversarial Plan Review (3 personas)

Three adversarial reviewers stress-tested [`PLAN.md`](./PLAN.md): a **senior fullstack/
devops engineer**, a **microcontroller/BLE engineer**, and an **AI-harness/MCP
engineer**. All read the plan, the [security review](./SECURITY_REVIEW_pypixelcolor.md),
and the `pypixelcolor` source as ground truth. This document consolidates their
findings, resolves them into decisions, and is the basis for the **PLAN.md v2**
revisions (see the changelog at the bottom of PLAN.md).

**Shared verdict:** the security spine and threat model are solid. The weaknesses are
all in the **distributed-systems / ops layer**, the **BLE/hardware reality**, and the
**MCP-client layer** — including **two factual errors** in v1 that would have caused
real breakage. None are fatal; the design survives, but several "configuration steps"
in v1 are actually multi-week problems and two must be reversed.

---

## 🔴 Two factual errors in PLAN v1 (must fix)

### E-1 — Static bearer + OAuth advertisement *breaks* Claude Code (reverse of what v1 claimed)
v1 §5 said the origin could accept a static bearer **and** advertise RFC 9728 PRM + a
`401` challenge "so the direct path can do real OAuth later." That's backwards. Per
**claude-code issue #59467** (open as of May 2026): when an HTTP MCP server is given a
static `Authorization` header **and** also advertises OAuth, **Claude Code ignores the
header, runs OAuth discovery, shows "✓ Connected" but exposes only synthetic
`authenticate`/`complete_authentication` tools** — none of our real tools appear.
- **Fix:** the **direct tailnet endpoint must advertise NO OAuth** — no
  `/.well-known/oauth-protected-resource`, no `401 + WWW-Authenticate`. It plainly
  `200`s with a valid static bearer and `401`s without. **All** OAuth lives on the
  Cloudflare Worker (public path). Delete the "origin advertises PRM for later" idea.

### E-2 — Claude Code hooks *can* call MCP tools directly (v1's "hooks are shell commands" is stale)
v1 Mode B shipped a "recipe" implying hooks must shell out / `curl`. Current Claude
Code hooks support **five handler types incl. a native `mcp_tool`** (and an `http`
type):
```json
{ "type": "mcp_tool", "server": "ipixel", "tool": "notify_operator",
  "input": { "message": "${...}", "level": "blocked" } }
```
- **Fix:** Mode B integration = a **`Notification`** hook (type `mcp_tool`) firing on
  `notification_type ∈ {permission_prompt, idle_prompt}` → `notify_operator`; a
  **`Stop`** hook → `clear_notification` (Stop = "turn finished," correct for *clearing*,
  wrong for "needs input"). Also surface **`Elicitation`/`ElicitationResult`** events +
  MCP elicitation as the spec-correct "server needs user input" path.

---

## 🔴 Cross-cutting consensus (raised by ≥2 reviewers)

### C-1 — Go **stateless** Streamable HTTP (highest-leverage single change)
Fullstack + MCP reviewers both: v1 never decided stateful vs stateless and name-dropped
Cloudflare `agents`/`McpAgent` — which **hosts the MCP server in-Worker on a Durable
Object** and is the *wrong* tool when the real server is Python on a Pi. Proxying a
**stateful, streaming** MCP session (`Mcp-Session-Id`, long-lived GET SSE) through a
Worker is off the paved path and a multi-week rabbit hole (SSE buffering, Worker
wall-clock limits, session stickiness, restart-drops-sessions).
- **Decision:** origin runs **stateless Streamable HTTP** — POST request/response only,
  no `Mcp-Session-Id`, no server→client GET stream. Mode B is **poll-based**
  (`list_notifications`) so we don't need server push. This makes the Worker a **plain
  authenticating request/response proxy** (an afternoon, not weeks) and makes origin
  restarts a non-event. **Drop `agents`/`McpAgent` from the stack**; the Worker uses
  `workers-oauth-provider` + a hand-written `fetch` proxy via `apiHandlers`.

### C-2 — The single BLE lock causes head-of-line blocking; large writes must be async jobs
All three: one BLE connection + one asyncio lock is **required** for correctness
(`send_plan` globally swaps the notify handler, so concurrency corrupts ACK routing —
F-8/F-13), but v1 never reconciled it with latency. Ground truth: chunks use
`write_gatt_char(..., response=True)` (a round-trip **per 244-byte chunk**, gated by the
~30–50 ms connection interval) and each window waits up to `ack_timeout=8 s`. Realistic
transfer times: a 64×64 image ≈ seconds; a 100 KB GIF ≈ **20–90 s** (the lib re-encodes
GIFs with `optimize=False`). That **exceeds the Worker ~100 s `fetch` ceiling and MCP
client timeouts**, and a mid-flight `display_image` **stalls the urgent `notify_operator`
banner** behind it.
- **Decision:** `display_image`/animation become **async jobs** — return a `job_id`
  immediately + a status/`get_display_state` tool (no blocking the HTTP response on BLE
  completion). `notify.*` gets a **priority/preempt path** ahead of long image jobs.
  Cap **encoded output bytes**, GIF **frame count**, and **total transfer time**; enable
  GIF `optimize=True`. Document the system as **single-board, single-flight,
  seconds-latency-under-contention by design.**

### C-3 — "One persistent BLE connection" is the wrong model; build a real supervisor
Fullstack + MCU: cheap iPixel panels sleep/drop after idle; BlueZ long-lived links are
flaky; the lib's `disconnected_callback` only sets a flag and **the reconnect loop lives
in the library's WebSocket server we are NOT using** — so we must build it. v1's
`device.py` ("persistent session + lock") said nothing about reconnection, write
timeouts, or health.
- **Decision:** treat the connection as **disposable**: lazy-connect, optional
  verified keep-alive, **reconnect with backoff + circuit breaker**, and an
  **adapter-reset escalation ladder** (re-create client → `bluetoothctl/rfkill` reset →
  `systemctl restart bluetooth`) **inside the process**. Wrap every BLE op in
  `asyncio.wait_for(...)` at our layer (the lib's writes have **no timeout**); on
  timeout → force disconnect + reconnect. Add `/healthz` (BLE connected? lock free? last
  ACK age?). **Phase 0 exit becomes "survives a disconnect and recovers," not "shows
  text."**

### C-4 — The dual-auth trust boundary is undefined (and the audience invariant is unstated)
Fullstack + MCP: v1 has the origin trusting both a static bearer and Worker-proxied
calls, but never says **what the origin checks**, where scope gating happens, or how
OAuth scopes cross the proxy. The MCP reviewer adds the spec invariant: claude.ai's
token audience = **the Worker** (RFC 8707 `resource`); the origin must **not**
re-validate the user token (confused-deputy/token-passthrough is explicitly forbidden) —
it authenticates **the Worker** via the **Cloudflare Access service-token JWT** only.
- **Decision:** one origin authorization function, explicit precedence:
  1. valid **CF Access service-token JWT** → trust Worker; read a **trusted
     `X-Mcp-Scopes`** header (only honored on this path) for `clear`/`delete` gating;
  2. else **`Authorization: Bearer` == static token** → fixed non-admin scope set;
  3. else `401` (plain, **no** OAuth advertisement — see E-1).
  The origin must **verify the Access JWT itself** (not trust loopback implicitly, or
  the two paths collapse). Worker terminates the user OAuth token; never forwards it.

### C-5 — Model-authored base64 images are a trap; provenance must change
MCU + MCP: **Claude cannot emit raw image bytes**, and a 256 KB image ≈ ~85K base64
tokens in one tool call — absurd cost + guaranteed timeout. v1 conflated "any tailnet
service" (base64 is fine — machine caller) with "Claude calls it" (base64 is wrong).
- **Decision:** keep `display_image(image_base64)` **documented as machine/passthrough
  only**; make **`show_preset(id)`** the model's image path (cheap IDs, server holds
  bytes); add a guarded **`image_url`** variant (server fetches, SSRF guard + F-3
  limits). Tell the model in the tool description not to base64 images itself.

### C-6 — Ops on a Pi is the real long pole, not the security hardening
Fullstack + MCU: v1 buries ops in one Phase-4 bullet. Concrete landmines: **bind-order
conflict** — "bind to the tailnet iface, never `0.0.0.0`" vs. the origin booting before
`tailscaled` exists (it'll crash-loop); three coupled daemons (`tailscaled`,
`cloudflared`, origin) need `After=/Wants=` + backoff; BlueZ permissions/claiming;
**SD-card corruption on power loss**; secrets spread across Worker + Pi with no rotation
runbook; tunnel/Access/tailnet-key renewal; no remote log access on a headless Pi.
- **Decision:** promote ops to a **pre-exposure phase**. Resolve bind-order (always bind
  loopback; add tailnet bind lazily/optionally). Ship systemd units with ordering +
  `Restart=on-failure`. Document SD/power posture (log to volatile, read-mostly rootfs),
  secrets locations + rotation, renewal, and ship journald off-box. Add `/healthz`.

---

## 🟠 Hardware-specific (MCU reviewer)

- **H-MTU — Dynamic MTU, never hardcode 244.** `chunk_size=244` assumes a negotiated
  247-byte ATT MTU. On a default-23 MTU (common after a degraded reconnect) **every
  chunk overflows → garbled frames / wedged transfer**. Request MTU exchange, read
  `client.mtu_size`, derive `chunk_size = mtu − 3`, segment/refuse if small.
- **H-WEDGE — Failed windows can wedge the display until power-cycle.** A
  dropped/partial window leaves the device's byte-accumulator mid-transfer; the next
  write may render garbage or freeze. The ACK manager accepts **any** `0x05…{0,1,3}`
  notify with **no window correlation** (F-8), so a stale/out-of-order ACK advances the
  sender into desync. **On any window timeout/CRC/ACK failure → disconnect+reconnect to
  reset the device state machine**, drain stale notifies on `reset()`, enforce min
  inter-window pacing, and don't let an early `data[4]==3` short-circuit a multi-window
  transfer. Make the notify-handler swap exception/disconnect-safe (or eliminate it with
  one opcode-dispatching handler).
- **H-FLASH — EEPROM/flash wear.** `save_slot≥1` and `clear` write **non-volatile**
  store (~10k–100k cycles). v1's "notifications every few seconds" would burn flash in
  weeks. **Default all notify/ephemeral display to `save_slot=0` (volatile RAM);**
  rate-limit *persistent* writes hard and separately; don't `set_time` on every
  reconnect.
- **H-BOOTLOOP — Model-specific limits.** The lib bans animations **3/4 on non-32×32
  boards to avoid a firmware bootloop** — i.e. the wrong animation on the wrong panel
  can crash it until power-cycle. **Drive `animation`/`orientation`/`font`/size enums
  from the *detected* `led_type`; fail closed on unknown device types** (the lib
  wrongly defaults unknown → 64×64). Resolve single-vs-multi-board before Phase 0.
- **H-PANEL — Longevity & alert quality.** 24/7 panels burn in (static clock/banner),
  run hot at full brightness, and full-white = worst-case PSU draw (brownout →
  mid-transfer MCU reset → wedge). Default **modest brightness**, idle auto-dim/blank +
  content rotation, scheduled off-hours. An LED matrix is a **mediocre, no-closed-loop
  alert channel** (no ack button, slow scroll, one-at-a-time, multi-second latency) —
  position Mode B as **ambient/secondary**, pair "blocked" with a real push channel,
  keep messages ≤~40 chars, volatile.

---

## 🟠 MCP-layer (harness reviewer)

- **M-NAMES — Flatten to snake_case.** Dotted names are spec-legal (SEP-986) but
  clients prefix with the server name (`ipixel:display.display_text` is redundant noise).
  Use `display_text`, `notify_operator`, `gallery_show_preset`; rely on server-prefix
  namespacing. (v1's table was also internally inconsistent: `display.*` headers vs
  `display_text` rows.)
- **M-ANNOT — Use tool `annotations`, drop the `confirm token` arg.** A model can't
  obtain a confirm token (it'll hallucinate one). Gate destructive ops via the **OAuth
  admin scope** + client human-in-the-loop, and mark them
  `annotations:{destructiveHint:true}`; mark reads `readOnlyHint:true`
  (`get_device_info`, `list_presets`, `list_notifications`).
- **M-OWN — Display ownership/state for the 3-client single board.** All clients clobber
  one screen, last-write-wins, no identity. Add a thin **display-state layer**: every
  write carries `ttl` + `owner/source`; a `get_display_state` read tool; Mode B
  `blocked` **preempts** Mode A and **restores** prior state on clear (formalize as a
  state stack). Make `notify_operator.source` effectively required (single shared
  tailnet identity ⇒ it's the only way to tell agents apart).
- **M-RES — Expose the gallery as MCP resources (+ a prompt), not only tools.** The
  preset catalog is what **resources** are for (`gallery://presets/...`, browsable with
  previews); keep `show_preset` as the action tool. Consider a prompt ("show something
  for X mood").
- **M-LISTCHANGED — Dynamic tool visibility.** Scope-gated tools mean `tools/list`
  varies by token; runtime mode toggles require `notifications/tools/list_changed`.
  Decide static-per-deploy (simplest) vs dynamic and say so.
- **M-SCHEMA — Concrete schemas.** Give real bounds/enums, not "0–N": `orientation`
  enum with degrees, `color` `pattern:"^#?[0-9a-fA-F]{6}$"` (state whether `#` is
  required), slot count from `get_device_info`. Ambiguous formats are the top cause of
  failed model tool calls.
- **M-REDIRECT — DCR redirect URIs.** Don't hard-allowlist only
  `claude.ai/api/mcp/auth_callback`; Desktop and Code use **different** redirect URIs
  (Code uses a localhost loopback). Let `workers-oauth-provider` DCR register per-client
  redirects (exact-match per spec), incl. the loopback form.
- **M-RESULT — Return text confirmations, not image echoes.** Don't echo the rendered
  image into chat (token noise); return `"Displayed on board, slot 2, owner=…, ttl=60s"`.

---

## 🟡 Smaller flags worth keeping

- **Notification restart-tolerance:** in-memory queue means a `notification_id` returned
  to a hook evaporates on restart → later `clear_notification(id)` silently no-ops, and
  a missed `Stop` hook **strands the board on red forever**. Persist the queue (small
  SQLite/JSON) **and enforce `ttl_seconds`** (auto-expire).
- **Cut the speculative origin-side RFC 9728 scaffolding** (ties into E-1).
- **Tailscale Funnel is not a "fallback"** — it makes the origin public, contradicting
  the "tailnet only" spine. Drop it as a live option or label it a different (weaker)
  threat model.
- **Pin the whole transitive tree with hashes** incl. `bleak` (BLE behavior varies
  sharply by version); decide vendoring as a **frozen fork at a named commit** we own,
  not "until upstream merges."
- **`text` cap:** cap the count that actually feeds `bytes([num_chars])` (chunk/emoji
  count, not raw char length — F-6); test an emoji in `display_text` to confirm the F-12
  offline path short-circuits instead of failing mid-render.
- **`get_device_info` cache** must be invalidated on reconnect (dims/board can change);
  coordinate clamping must use *current* dims.
- **Python version:** Pi OS may ship 3.11; confirm before committing to 3.12.
- **CI scope honesty:** CI = schema + mode logic with mocked `bleak`; BLE
  protocol/ACK/timeout/disconnect risks need a **manual hardware smoke-test checklist** —
  don't imply CI covers them.
- **Don't log image bytes:** the lib logs full frame hex at DEBUG (F-13); ensure prod
  log level never hits that path (SD-card flooding).

---

## Revised phase ordering (consensus)

0. **Origin MVP + BLE robustness** — stateless Streamable HTTP; `display_text`/
   `get_device_info`; **reconnect supervisor, per-op timeouts, dynamic MTU, `/healthz`**;
   resolve bind-order + single-vs-multi-board. *Exit: survives a disconnect & recovers.*
1. **Safety hardening** — all `safety.py` limits (F-2/3/6/11), flash-wear defaults
   (volatile by default), model-specific enum gating (H-BOOTLOOP), pinned tree + hashes,
   generic errors, origin auth function (C-4), gate `clear`/`delete` by scope+annotations.
2. **Async media + modes B & C** — `display_image`/animation as **jobs**; notify
   preempt path + persisted queue + TTL; ownership/state layer (M-OWN); gallery as
   **resources** + `show_preset` + `image_url`; Claude Code `mcp_tool` `Notification`
   hook example (E-2).
3. **Ops & provisioning (pre-exposure)** — systemd ordering, secrets+rotation, SD/power
   posture, renewal, remote logs. *Promoted ahead of public exposure.*
4. **Public exposure** — `cloudflared` + Access service token; Worker
   (`workers-oauth-provider`, **plain proxy**, single-operator login, DCR); connect from
   claude.ai web + Desktop; verify audience invariant (C-4) end to end.
5. **Polish** — rate limits, metrics, runbooks, per-client docs, burn-in/longevity
   defaults, optional web-ack for Mode B.

---

## What stayed right (reviewers agreed)

- The **security review and threat model** need no correction.
- **One server, capability-flagged tool groups** (not three servers) is the correct MCP
  shape.
- **Cloudflare Worker for the public path + Claude Code direct on the tailnet** is the
  correct topology given Anthropic-cloud-origin reachability.
- **Curated, validated, bytes-bounded subset** (never naive pass-through) is correct.

---

# Round 2 — review of the IMPLEMENTATION (post-fan-out)

After the four phases were implemented, the **same three personas** audited the real
code. They confirmed the pure domain logic was solid and well-tested (108 tests) but
found that several review "bars" were **not met at the integration seam** — invisible to
the suite because `mcp`/`bleak`/`Pillow` aren't installed in CI. All consequential
findings below were **fixed** in the same pass; tests now: **server 119, worker 25**.

## Fixed

| ID | Finding | Fix |
|----|---------|-----|
| B-2 | `/mcp` would 500 on first request — FastMCP session-manager lifespan was dropped when mounting under Starlette | `build_app` now runs `mcp.session_manager.run()` in a Starlette `lifespan` |
| B-1 | reconnect supervisor never started in prod | lifespan calls `dm.start_supervisor()`; drains jobs + `dm.close()` on shutdown (T-2) |
| B-3 | scope gating **failed open** — the principal contextvar didn't survive FastMCP's task-group dispatch | authoritative **ASGI scope enforcement**: middleware parses the JSON-RPC `tools/call` body and gates `TOOL_SCOPES` against the Principal *before* dispatch (fail-closed); `require_scope` kept as in-tool defense. New tests in `test_app_scopes.py` |
| B-4 | Mode-B notifications never painted the board (no `render` wired) | `build_app` wires a `render` callback that paints the level-coloured banner via `display_text` |
| TOP-1 (worker) | dead `POST /authorize` branch → consent dialog could never submit (first-time auth broken) | method-gated the GET branch; `test/authorize-routing.test.ts` |
| TOP-2 | `clear_notification` didn't accept the `source` the Stop-hook sends → cleared *all* agents' banners | added `source` filter (clear only that agent's); enum-typed `level` |
| TOP-3 | vague tool input schemas | `Literal` enums for `level`/`format`/`category` (model self-corrects) |
| H-WEDGE | ACK-timeout (`cur12k_no_answer`) wasn't recognised as link-recyclable → wedge risk | added `no_answer`/`no ack`/`cur12k`/`timed out` to `_DISCONNECT_MARKERS` (execute now recycles + retries) |
| H-MTU | MTU read off the wrong object (always `None`); enforcement cosmetic | read `._session._client.mtu_size`; `assert_mtu_ok()` **refuses** image transfers on a known-degraded link (image `_op` calls it) |
| T-3 | notification queue had no lock around the render-await read-modify-write | `asyncio.Lock` around the async mutator |
| T-4 | SSRF guard bypassable via 3xx redirect to metadata IP | fetcher uses a no-redirect opener |
| MED-1 | reads (`get_device_info`/`get_job_status`) weren't scope-gated | covered by `TOOL_SCOPES` middleware |
| MED-3 | Worker forwarded `www-authenticate` back to claude.ai | dropped from the forwarded-header allow-list |
| M-6 | persisted notify DB unwritable under `ProtectSystem=strict` | unit sets `StateDirectory`/`ReadWritePaths` + `IPIXEL_NOTIFY_DB=/var/lib/ipixel-mcp/...` |

## Residual / tracked (NOT fixed — accepted or needs upstream)

- **F-8 ACK forgery / stale-ACK (inherent upstream):** pypixelcolor's notify handler
  accepts any `0x05…{0,1,3}` frame with no window correlation. Not mitigable without an
  upstream patch; the single-flight lock prevents *concurrent* corruption only. Accepted
  residual; flagged for an upstream PR.
- **True MTU chunk fix:** the library hardcodes a 244-byte chunk size; we can only
  *refuse* on a degraded link, not re-chunk. Real fix needs an upstream patch threading
  `chunk_size` into `_build_send_plan`. Tracked.
- **T-1 notify preempt is not cancellation:** a `blocked` banner still queues behind an
  in-flight 120 s image job (single BLE lock). Accepted; documented at the call site.
  A true preempt (cancel/interpose the in-flight op) is a future enhancement.
- **M-2 `source` is caller-supplied** (spoofable ownership label). Low impact for a
  single operator; deriving it from the principal is a follow-up.
- **Encoded-size cap measures input, not re-encoded output** (a small-but-expanding GIF
  could still exceed 120 s, but fails cleanly via the timeout→recycle path).
- **Dependency hash-lock (F-7):** `constraints.txt` pins versions; per-artifact hashes /
  `uv.lock` + `pip-audit` in CI remain a Phase-3 task.
- **NIT-2:** image gallery resources advertise `image/png` but `read_resource` returns
  JSON; serve real blob bytes or relabel — minor.
- **Worker** remains verified by `tsc --noEmit` + `vitest` only; a `wrangler deploy
  --dry-run` against a real account is still required before go-live.
