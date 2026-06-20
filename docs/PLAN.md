# `ipixel-mcp` — Implementation Plan

Remote MCP server(s) to drive **iPixel Color** BLE LED matrix boards (via
[`pypixelcolor`](https://github.com/lucagoc/pypixelcolor)), reachable by **Claude.ai
web**, **Claude Desktop**, and **Claude Code**, served **over a Tailscale tailnet**
with a **Cloudflare Worker** terminating OAuth 2.1 (DCR-compatible) and proxying to the
tailnet origin.

> Companion docs:
> [`SECURITY_REVIEW_pypixelcolor.md`](./SECURITY_REVIEW_pypixelcolor.md) — the upstream
> library audit that this plan's security controls are built around.
>
> **This is PLAN v2**, revised after a three-persona adversarial review — see
> [`PLAN_REVIEW.md`](./PLAN_REVIEW.md). The changelog at the bottom lists what changed
> and why (incl. two factual corrections to v1). Where this doc and the review differ,
> the review's reasoning is authoritative.

---

## 0. TL;DR / decisions

> **v2 load-bearing decisions** (from the review): origin is **stateless** Streamable
> HTTP (no session/SSE) so the Worker is a plain auth proxy; **all OAuth lives on the
> Worker** and the direct tailnet endpoint advertises **no** OAuth (else Claude Code
> breaks — issue #59467); large image writes are **async jobs** behind a single-flight
> BLE lock with a **notify preempt path**; the BLE link is **disposable** with a real
> reconnect supervisor; **ops is a pre-exposure phase**. Details in §1–§7 and the review.


- **One MCP server, three "modes" as tool namespaces** — `display.*` (tailnet
  passthrough for any service to show custom text/img), `notify.*` (Claude Code /
  agent "operator needed" notifications), `gallery.*` (prebuilt images / ASCII art /
  texts). Modes are independently toggleable via config/scopes.
- **The origin MCP server is Python** (official `mcp` SDK / FastMCP, **Streamable
  HTTP**) running on the device host so it can `import pypixelcolor` directly and own
  the single persistent BLE connection. **We do NOT use the library's bundled
  WebSocket server** (it has no auth — see security review F-1).
- **Public exposure = Cloudflare Worker** (`@cloudflare/workers-oauth-provider` +
  `agents`) doing OAuth 2.1 + Dynamic Client Registration, proxying to the tailnet
  origin over a **Cloudflare Tunnel** (origin never faces the public internet → stays
  "tailnet only"). Locked Worker→origin with a Cloudflare Access **service token**.
- **Two reachability paths, by client:**
  - **Claude.ai web + Claude Desktop (UI):** must use the **public Worker URL**
    (`https://mcp.example.com/mcp`) — they dial from Anthropic's cloud and **cannot**
    reach a tailnet address.
  - **Claude Code (and `mcp-remote`):** dials **locally**, so it can hit the
    **tailnet-only** origin directly (`https://board.<tailnet>.ts.net/mcp`) with a
    **static bearer header** — the "simplest auth possible" the brief asked for.
- **Auth posture:** "single authenticated user." Worker authenticates one operator
  (recommended: GitHub OAuth restricted to your account, or a one-password login page)
  and issues OAuth tokens to MCP clients; **DCR comes free** from
  `workers-oauth-provider`. An **authless** mode exists for first-light testing but is
  not the end state.
- **Security is the spine of this plan:** the wrapper exposes a **curated, validated,
  bytes-only** subset of the library and applies image/text limits, because the raw
  library is unsafe to expose as-is (review verdict: *naive pass-through wrapping would
  create a remotely reachable, unauthenticated file-probe and parser-DoS surface*).

---

## 1. Why this shape (key constraints discovered)

Researched against the MCP spec (rev **2025-11-25**), Anthropic connector docs,
Cloudflare `agents` / `workers-oauth-provider`, and Tailscale docs (June 2026):

1. **Remote MCP transport is Streamable HTTP** over a single HTTPS endpoint
   (`POST` for client→server JSON-RPC, optional `GET` SSE for server→client, optional
   `MCP-Session-Id`, required `MCP-Protocol-Version` header post-init, `Origin`
   validation). Legacy HTTP+SSE is deprecated — build Streamable HTTP, optionally keep
   `/sse` for old clients.
2. **Claude.ai web & Desktop connect from Anthropic's cloud**, not your machine ⇒ the
   URL must be **public HTTPS**. A tailnet/`100.x`/localhost URL will not work for
   them. **Claude Code connects locally** ⇒ it *can* reach the tailnet directly.
3. **MCP auth = OAuth 2.1.** The MCP server is an OAuth **Resource Server**: it must
   serve `/.well-known/oauth-protected-resource` (RFC 9728) and answer unauthenticated
   calls with `401 + WWW-Authenticate: Bearer resource_metadata="…"`. The
   **Authorization Server** (separate role) serves `/.well-known/oauth-authorization-server`
   (RFC 8414), `/authorize`, `/token`, and optionally `/register` (DCR, RFC 7591).
   PKCE S256 + the RFC 8707 `resource` param are required of clients.
4. **Claude.ai does NOT require DCR** (it also supports CIMD, Anthropic-held creds, or
   pasting a client ID/secret in *Advanced settings*), and it supports **authless**.
   It does **not** have a "paste a static token" box in the web UI — static tokens are
   a **Claude Code** (`--header`) / API feature only.
5. **Cloudflare `workers-oauth-provider`** turns a Worker into a compliant OAuth 2.1 AS
   (serves `/authorize`, `/token`, `/register`, RFC 8414 + RFC 9728 metadata, PKCE,
   hashed tokens in KV) and can delegate login to an upstream IdP — i.e. it gives us
   **DCR + OAuth 2.1 essentially for free**, which is the cheapest route to a real
   authenticated Claude.ai connector.
6. **A Cloudflare Worker cannot join a tailnet** (no WireGuard in the isolate). Bridge
   at the origin: run **`cloudflared` co-located with `tailscaled`**; the Worker
   `fetch()`es the Tunnel hostname and the last hop stays on the tailnet. (Alternative:
   **Tailscale Funnel** exposes the origin publicly on `*.ts.net` — simpler, but the
   origin is then public, which conflicts with "tailnet only," so it's a fallback.)

---

## 2. Architecture

```
                         ┌──────────────────────────────────────────────┐
   Claude.ai web ──┐     │  Cloudflare Worker  (public: mcp.example.com) │
   Claude Desktop ─┼────▶│  • workers-oauth-provider: OAuth 2.1 + DCR    │
        (cloud)    │     │    /authorize /token /register + RFC8414/9728 │
                   │     │  • single-operator login (GitHub OAuth / pwd) │
                   │     │  • validates bearer, then proxies /mcp ───────┼──┐
                   │     └──────────────────────────────────────────────┘  │
                   │                                                        │ Cloudflare
                   │                                  CF Access service tok │ Tunnel
                   │                                                        ▼
   Claude Code ────┼───────── tailnet (WireGuard, 100.x / *.ts.net) ─────┐ │
   (local, can     │                                                     │ │
    reach tailnet) │     ┌───────────────────────────────────────────┐  │ │
                   └────▶│  Device host (e.g. Raspberry Pi)            │◀─┘─┘
   board.<tailnet>.ts.net│  • ipixel-mcp  (Python, FastMCP,           │
   /mcp  +  static bearer│    Streamable HTTP, bound to loopback+      │
                         │    tailnet iface — NEVER 0.0.0.0)           │
                         │  • single persistent BLE session (asyncio   │
                         │    lock) ── pypixelcolor (hardened) ──BLE──▶ 🟥 iPixel board
                         │  • cloudflared (tunnel) + tailscaled        │
                         └───────────────────────────────────────────┘
```

**Trust boundaries (outer → inner):**
1. Cloudflare edge: TLS, WAF/rate-limit, OAuth token validation (Worker).
2. Cloudflare Tunnel + Access service token: only the Worker can reach the origin.
3. Tailnet ACLs: only tagged nodes can reach the origin's MCP port.
4. Origin app: OAuth Resource-Server checks (for the public path) **and** a static
   bearer (for the direct tailnet/Claude Code path), strict input schemas, capability
   gating, rate limits, single-flight BLE lock.

Defense in depth: even a fully compromised tailnet peer hits the app-layer auth +
schema validation, never a raw pass-through.

---

## 3. The three modes (MCP tool surface)

All tools use strict typed schemas (pydantic), reject unknown params, never accept
filesystem paths from callers, and return **generic** errors to the client (details
logged server-side only). Each mode is independently enabled via config; destructive
capabilities require an explicit scope/flag.

### Mode A — `display.*` — tailnet passthrough (any service shows custom text/img)
Generic primitives so any tailnet service or agent can push content to the board.

| Tool | Args (validated) | Maps to |
|---|---|---|
| `display_text` | `text` (≤200 chars, **hard-capped <255** per F-6), `color` hex, `bg_color?`, `font` (enum of bundled fonts only), `animation` (enum), `speed` (1–100), `rainbow` (bool), `slot` (0–N) | `pypixelcolor.send_text` |
| `display_image` | `image_base64` (≤256 KB decoded), `format` (enum: png/gif/jpeg), `resize` (crop/fit/stretch), `slot` | `send_image_hex` (**bytes only**, never `send_image(path=)`) |
| `set_brightness` | `level` (0–100) | `set_brightness` |
| `set_power` | `on` (bool) | `set_power` |
| `set_orientation` | `orientation` (enum 0–3) | `set_orientation` |
| `set_clock_mode` | `style`, `show_date`, `format_24` | `set_clock_mode` |
| `show_slot` | `number` (0–N) | `show_slot` |
| `get_device_info` | — | cached `DeviceInfo` (w/h/type) |
| `clear_screen`* | confirm token | `clear` — **gated** (destructive, F-1) |
| `delete_slot`* | `n`, confirm | `delete` — **gated** |

`*` gated tools require the `ipixel:admin` scope (OAuth) or `--enable-destructive`
config; off by default.

### Mode B — `notify.*` — operator-input notifications (Claude Code / any agent)
A board-as-ambient-alert channel: an agent that is blocked and needs a human renders an
attention banner on the matrix.

| Tool | Args | Behavior |
|---|---|---|
| `notify_operator` | `message` (≤~40 chars to read in one scroll), `level` (`info`/`warn`/`blocked`), `source` (agent/session label — effectively required: the tailnet path is a single shared identity, so this is the only way to tell agents apart), `ttl_seconds` (**enforced** auto-expire) | Renders a level-colored banner (blue/amber/red + alert glyph), **volatile (`save_slot=0`)** to spare flash. A `blocked` notification **preempts** Mode A display and **restores** prior state on clear (state stack). Pushes onto a **persisted** queue; returns a `notification_id`. |
| `clear_notification` | `notification_id?` (omit = clear all) | Removes from queue; pops the state stack to restore prior display or idle. Must tolerate clear-of-unknown-id (restart drops ids). |
| `list_notifications` | — | Active notifications (id, level, message, source, age). `readOnlyHint`. |

**Integration (corrected in v2 — see review E-2):** Claude Code hooks can invoke MCP
tools natively via the **`mcp_tool` handler type** (no shell/`curl`). Ship a
**`Notification`** hook firing on `notification_type ∈ {permission_prompt, idle_prompt}`
→ `notify_operator`, and a **`Stop`** hook → `clear_notification` (Stop = "turn
finished" — correct for *clearing*, wrong as the "needs input" trigger). The richer,
spec-correct path for "server needs user input" is the **`Elicitation` /
`ElicitationResult`** events + MCP elicitation. Because the board has no input device
there is **no closed-loop ack** — position the matrix as an **ambient/secondary** alert
and pair `blocked` with a real push channel. `ttl_seconds` is enforced so a missed
`Stop` hook can't strand the board on red.

```json
// .claude/settings.json — Notification hook (mcp_tool handler)
{ "hooks": { "Notification": [ { "hooks": [ {
  "type": "mcp_tool", "server": "ipixel", "tool": "notify_operator",
  "input": { "message": "operator input needed", "level": "blocked", "source": "claude-code" }
} ] } ] } }
```

### Mode C — `gallery.*` — prebuilt images / ASCII art / texts
A curated, **server-controlled** asset set (no caller-supplied bytes → smallest attack
surface). Assets live in `assets/` with a `manifest.json` (id, category, type, render
params). ASCII art is rendered via the monospace bundled font (or pre-rasterized).

| Tool | Args | Behavior |
|---|---|---|
| `list_presets` | `category?` (`image`/`ascii`/`text`) | Returns `{id, name, category, preview}` from the manifest. |
| `show_preset` | `id` (must exist in manifest), `slot?` | Renders the named asset; image presets via `send_image_hex`, ascii/text via `send_text`. |

---

## 4. Security controls (each maps to a review finding)

| Control | Addresses |
|---|---|
| Never expose the library's bundled WebSocket server; in-process `pypixelcolor` only | F-1 |
| Public path behind Cloudflare OAuth + Access service token; origin bound to loopback/tailnet, **never `0.0.0.0`**; tailnet ACLs | F-1 |
| **Bytes-only** image input (base64), no filesystem paths from callers; `font` restricted to a bundled enum (no arbitrary `.ttf`/`.json` paths) | F-2 |
| `Image.MAX_IMAGE_PIXELS` set low; decode-size cap (≤256 KB); GIF **frame cap**; bomb warning → error; verify dims before full decode | F-3 |
| Per-tool pydantic schemas, type/range validation, reject unknown params (no raw kwargs splat) | F-4 |
| Fix/clamp before exposing `set_pixel` (don't ship it until upstream validation fixed) | F-5 |
| `display_text` hard-caps effective char/chunk count **< 255** | F-6 |
| Pin all deps + lockfile + hashes; `pip-audit` + Dependabot in CI; treat Pillow as security-relevant | F-7 |
| (If response-reading commands used) validate ACK frames; single-flight **asyncio lock** around the one BLE session to avoid the notify-handler race | F-8, F-13 |
| Generic client errors; full detail to server logs only | F-9 |
| Document BLE Just-Works trust; keep device host physically/network trusted | F-10 |
| Context-managed `Image.open`; close handles (long-running server) | F-11 |
| **Disable** network emoji fetch (offline/opt-in); bundle any needed glyphs | F-12 |
| Request-size limits at Worker + origin (image byte caps **done**); **rate limiting deferred to Phase 5** (Cloudflare edge WAF rate-limit available in the meantime) | F-1, F-3, DoS |
| `clear`/`delete` gated behind admin scope + confirm token | F-1 |

**Upstream contribution:** file issues/PRs to `pypixelcolor` for F-1 (auth + loopback
default + drop the `0.0.0.0` doc), F-5 (broken `set_pixel` validation), F-6
(`num_chars` overflow), F-3 (image limits). We vendor a thin hardened wrapper until
merged; pin to a known-good Pillow.

---

## 5. Auth design (single operator, DCR-compatible)

**Public path (Claude.ai / Desktop) — Worker is the OAuth 2.1 AS + Resource proxy:**
- `@cloudflare/workers-oauth-provider` serves `/authorize`, `/token`, `/register`
  (DCR), `/.well-known/oauth-authorization-server` (RFC 8414), and RFC 9728 PRM;
  tokens hashed in Workers KV; PKCE S256 enforced.
- `defaultHandler` authenticates the **single operator**. Recommended (simplest robust):
  **GitHub OAuth allow-listed to your GitHub login**. Alternative: a one-password login
  page (secret in Worker env). Either way Claude clients see standard OAuth.
- DCR registers redirect URIs **per client** — do **not** hard-allowlist only
  `https://claude.ai/api/mcp/auth_callback`; Desktop and Claude Code use **different**
  redirects (Code uses a localhost loopback). Let DCR handle exact-match registration.
- **Audience invariant (review C-4):** the claude.ai token's `resource`/audience is the
  **Worker** URL. The Worker terminates that token and proxies; it **never forwards the
  user OAuth token** to the origin (forbidden token-passthrough / confused-deputy). The
  origin authenticates **the Worker** via the **Cloudflare Access service-token JWT**
  only, and reads a trusted `X-Mcp-Scopes` header (honored *only* on this path) for
  scope gating.

**Direct tailnet path (Claude Code / Desktop-via-`mcp-remote` on the tailnet):**
- This endpoint advertises **NO OAuth** — **no** `/.well-known/oauth-protected-resource`,
  **no** `401 + WWW-Authenticate`. It plainly `200`s with a valid **static bearer**
  (`Authorization: Bearer <token>` from env) and `401`s without. **This reversal vs v1
  is mandatory:** if the origin advertised OAuth *and* a static header were set, Claude
  Code ignores the header and falls into OAuth discovery (issue #59467), hiding all our
  tools. Tailnet ACLs are the real boundary; the token stops casual tailnet peers.
  `claude mcp add --transport http ipixel https://board.<tailnet>.ts.net/mcp --header "Authorization: Bearer $TOKEN"`.

**Origin authorization function (single, explicit precedence — review C-4):**
1. valid **CF Access service-token JWT** (verified by the origin itself, not trusted by
   loopback) → trust Worker; read `X-Mcp-Scopes` for `clear`/`delete` gating;
2. else `Authorization: Bearer == STATIC_TOKEN` → fixed non-admin scope set;
3. else plain `401` (no OAuth advertisement).

**Authless mode (first light only):** Worker template `remote-mcp-authless` + origin
with auth disabled, behind an unguessable hostname + Cloudflare Access. Not the end
state; documented as test-only. (No speculative origin-side RFC 9728 scaffolding —
removed in v2.)

---

## 6. Tech stack & repo layout

**Origin (Python 3.12):** `mcp` (official SDK, FastMCP, Streamable HTTP) ·
`pydantic` · hardened `pypixelcolor` (pinned) · `Pillow` (pinned, `MAX_IMAGE_PIXELS`) ·
`uvicorn`/`starlette`. **Edge (TypeScript):** `@cloudflare/workers-oauth-provider` +
`wrangler` — a **hand-written `fetch` auth proxy** via `apiHandlers` (**not** the
`agents`/`McpAgent` Durable-Object host: the MCP server is Python on the device, not
in-Worker). **Infra:** `cloudflared`, `tailscale`, `systemd` units.

```
ipixel-mcp/
├── docs/
│   ├── PLAN.md                      # this file
│   └── SECURITY_REVIEW_pypixelcolor.md
├── server/                         # Python origin MCP server
│   ├── ipixel_mcp/
│   │   ├── __main__.py             # FastMCP app, Streamable HTTP, auth middleware
│   │   ├── device.py               # BLE session + lock + reconnect supervisor + MTU + timeouts
│   │   ├── jobs.py                  # async media-transfer jobs (job_id + status)
│   │   ├── display_state.py        # ownership/TTL + preempt/restore state stack
│   │   ├── auth.py                  # CF-Access-JWT vs static-bearer precedence (§5)
│   │   ├── safety.py               # image/text limits, schema helpers (F-2/3/6)
│   │   ├── modes/display.py        # Mode A tools
│   │   ├── modes/notify.py         # Mode B tools + persisted notification queue
│   │   └── modes/gallery.py        # Mode C tools + MCP resources (assets/manifest.json)
│   ├── assets/                     # prebuilt images / ascii / texts + manifest.json
│   ├── pyproject.toml + lockfile   # pinned deps, hashes
│   └── tests/                      # schema, limits, mode logic (mock BLE)
├── worker/                         # Cloudflare Worker (OAuth + proxy)
│   ├── src/index.ts                # OAuthProvider{ apiHandlers, defaultHandler }
│   ├── wrangler.jsonc              # KV (tokens), routes, vars
│   └── package.json
├── deploy/
│   ├── ipixel-mcp.service          # systemd: origin
│   ├── cloudflared/config.yml      # tunnel ingress → loopback origin
│   ├── tailscale-acls.md           # tag:ipixel ACL snippet
│   └── README.md                   # provisioning runbook
├── examples/
│   ├── claude-code-notify-hook/    # Mode B Notification/Stop hook
│   └── tailnet-service-display.py  # Mode A passthrough example
└── README.md
```

---

## 7. Delivery phases (v2 — reordered per review)

- **Phase 0 — Origin MVP + BLE robustness:** **stateless** Streamable HTTP server;
  `display_text` / `get_device_info`; `device.py` single-flight lock **plus a reconnect
  supervisor, per-op `asyncio.wait_for` timeouts, dynamic MTU, and `/healthz`**. Resolve
  bind-order (always bind loopback; tailnet bind lazily) and single-vs-multi-board.
  *Exit:* **survives a BLE disconnect and recovers** (not merely "shows text").
  ✅ **Scaffolded in [`../server/`](../server/)** — `device.py` (lock + per-op timeouts +
  retry-once-on-disconnect + circuit breaker + MTU check + health), `safety.py`,
  `auth.py`, `modes/display.py`, stateless FastMCP `app.py`; 32 hardware-free tests pass
  (disconnect recovery, timeout-recycle, circuit breaker, lock serialization, validation).
  Remaining for exit: real-hardware disconnect smoke test; MTU *enforcement* needs the
  vendored/hardened `pypixelcolor` (Phase 1).
- **Phase 1 — Safety hardening:** all `safety.py` limits (F-2/3/6/11); **flash-wear
  defaults (volatile `save_slot=0`)**; **model-specific enum gating** (animations 3/4
  bootloop non-32×32 boards); pinned transitive tree + hashes + `pip-audit`; generic
  errors (F-9); the origin **auth function** (§5 precedence); gate `clear`/`delete` by
  admin scope + tool `annotations`. *Exit:* security controls table ✔.
- **Phase 2 — Async media + modes B & C:** `display_image`/animation as **async jobs**
  (`job_id` + status, encoded-size/frame/time caps); `notify.*` with **preempt path +
  persisted queue + enforced TTL**; **display ownership/state stack**; `gallery.*` as
  MCP **resources** + `show_preset` + guarded `image_url`; Claude Code **`mcp_tool`
  `Notification` hook** example. *Exit:* operator-alert + presets working under contention.
- **Phase 3 — Ops & provisioning (pre-exposure):** systemd ordering/`Restart=`, secrets
  locations + rotation, SD-card/power-loss posture, tunnel/Access/tailnet-key renewal,
  remote log shipping. *Promoted ahead of exposure — it's the real long pole.*
- **Phase 4 — Public exposure:** `cloudflared` + Access service token; Worker
  (`workers-oauth-provider`, **plain auth proxy**, single-operator login, per-client
  DCR); connect from **Claude.ai web** + **Desktop**; verify the **audience invariant**
  end-to-end. *Exit:* all three clients connected; OAuth green.
- **Phase 5 — Polish:** rate limits, metrics, runbooks, per-client docs,
  burn-in/longevity defaults (modest brightness, idle dim/rotate, off-hours), optional
  web-ack for Mode B.

---

## 8. Open questions / decisions for the operator

1. **Auth for the public path:** GitHub-OAuth-restricted-to-you (recommended) vs.
   one-password login vs. authless-behind-Access? (Affects Worker `defaultHandler`.)
2. **Worker→origin bridge:** Cloudflare Tunnel (keeps origin tailnet-only —
   recommended) vs. Tailscale **Funnel** (simpler, origin becomes public `*.ts.net`)?
3. **Destructive tools** (`clear`/`delete`): expose behind an admin scope, or omit
   entirely?
4. **Custom domain** for the Worker, or use the `*.workers.dev` default?
5. ~~Single or multiple boards?~~ **DECIDED: single board.** The origin manages exactly
   one `DeviceSession`; no multi-device routing in tools. (Still need the specific board
   model / matrix W×H to set per-model enum gating — discovered at runtime via
   `get_device_info`, but confirm the model for asset sizing.)
6. Do you want me to also **upstream the `pypixelcolor` fixes** (F-1/F-5/F-6/F-3) as
   PRs, or only vendor a local hardened wrapper?

---

## 9. v2 changelog (from the adversarial review)

Full reasoning in [`PLAN_REVIEW.md`](./PLAN_REVIEW.md). Key changes vs v1:

- **[FACTUAL FIX]** Direct tailnet endpoint advertises **no OAuth**; static bearer only
  (Claude Code issue #59467). All OAuth moved to the Worker. §5 rewritten.
- **[FACTUAL FIX]** Claude Code hooks call MCP tools natively (`mcp_tool` handler) —
  Mode B integration rewritten; added `Elicitation` path. §3 Mode B.
- **[DECISION]** Origin is **stateless** Streamable HTTP (no session/SSE); Worker is a
  **plain auth proxy**, **not** an `agents`/`McpAgent` host. §0, §6.
- **[DECISION]** Large image/animation writes are **async jobs** + caps; `notify.*` gets
  a **preempt path** (single BLE lock ⇒ head-of-line blocking). §3, §7.
- **[DECISION]** BLE link is **disposable**: reconnect supervisor + per-op timeouts +
  dynamic MTU + `/healthz`; on window failure → disconnect/reconnect to clear device
  state. §7 Phase 0.
- **[DECISION]** Single origin **auth function** with CF-Access-JWT vs static-bearer
  precedence; **audience invariant** (Worker is the RS for claude.ai; no token
  passthrough). §5.
- **[DECISION]** **Ops promoted to a pre-exposure phase** (bind-order, systemd ordering,
  SD/power, secrets/rotation, renewal, remote logs). §7 Phase 3.
- **[HARDWARE]** Flash-wear defaults (volatile by default), model-specific enum gating
  (animation bootloop), longevity defaults (brightness/burn-in). §3, §7.
- **[MCP]** Flat snake_case tool names + `annotations` (drop confirm-token arg); gallery
  as **resources**; `image_url` + `show_preset` as the model's image path (not
  base64); display **ownership/state** layer; concrete schemas. §3.
- Tailscale **Funnel** demoted from "fallback" to a weaker, different threat model.

New open question for the operator (adds to §8): **single vs multiple boards** must be
decided **before Phase 0** (it changes the session model), and **stateless vs stateful**
is now decided (stateless).

---

*Next step after sign-off: scaffold `server/` (Phase 0) — stateless FastMCP +
`device.py` reconnect supervisor — and wire the first end-to-end `display_text` from
Claude Code over the tailnet, proving disconnect recovery.*
