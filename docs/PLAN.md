# `ipixel-mcp` — Implementation Plan

Remote MCP server(s) to drive **iPixel Color** BLE LED matrix boards (via
[`pypixelcolor`](https://github.com/lucagoc/pypixelcolor)), reachable by **Claude.ai
web**, **Claude Desktop**, and **Claude Code**, served **over a Tailscale tailnet**
with a **Cloudflare Worker** terminating OAuth 2.1 (DCR-compatible) and proxying to the
tailnet origin.

> Companion docs:
> [`SECURITY_REVIEW_pypixelcolor.md`](./SECURITY_REVIEW_pypixelcolor.md) — the upstream
> library audit that this plan's security controls are built around.

---

## 0. TL;DR / decisions

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
| `notify_operator` | `message` (≤120 chars), `level` (`info`/`warn`/`blocked`), `source?` (agent/session label), `ttl_seconds?` | Renders a level-colored scrolling banner (e.g. blue/amber/red + an alert glyph). Pushes onto a small in-memory **notification queue**; newest shown, queue cycles. Returns a `notification_id`. |
| `clear_notification` | `notification_id?` (omit = clear all) | Removes from queue; restores previous display or idle screen. |
| `list_notifications` | — | Returns active notifications (id, level, message, age). |

**Integration recipe (shipped in `examples/`):** a Claude Code **Notification/Stop
hook** that calls `notify_operator` when the agent needs input, and `clear_notification`
when it resumes. Because the board has no input device, "ack" = the operator clearing
it (a button/web-ack endpoint is a future extension). Documented as such.

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
| Rate limiting + request-size limits at Worker **and** origin | F-1, F-3, DoS |
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
- Allow-list Claude's callback `https://claude.ai/api/mcp/auth_callback` (+ `.com`).
- Worker validates the bearer on every `/mcp` request, then proxies to the origin over
  the Tunnel (adding the Access service-token headers).

**Direct tailnet path (Claude Code / Desktop-via-`mcp-remote` on the tailnet):**
- Origin accepts a **static bearer** (`Authorization: Bearer <token>` from env) — the
  brief's "simplest auth possible." Tailnet ACLs are the real boundary; the token stops
  casual tailnet peers. Add with:
  `claude mcp add --transport http ipixel https://board.<tailnet>.ts.net/mcp --header "Authorization: Bearer $TOKEN"`.

**Authless mode (first light only):** Worker template `remote-mcp-authless` + origin
with auth disabled, behind an unguessable hostname + Cloudflare Access. Not the end
state; documented as test-only.

> The origin implements RFC 9728 PRM + `401` challenge so that the *direct* tailnet
> path can also do real OAuth later if desired; for now static bearer keeps it simple.

---

## 6. Tech stack & repo layout

**Origin (Python 3.12):** `mcp` (official SDK, FastMCP, Streamable HTTP) ·
`pydantic` · hardened `pypixelcolor` (pinned) · `Pillow` (pinned, `MAX_IMAGE_PIXELS`) ·
`uvicorn`/`starlette`. **Edge (TypeScript):** Cloudflare `agents` +
`@cloudflare/workers-oauth-provider` + `wrangler`. **Infra:** `cloudflared`,
`tailscale`, `systemd` units.

```
ipixel-mcp/
├── docs/
│   ├── PLAN.md                      # this file
│   └── SECURITY_REVIEW_pypixelcolor.md
├── server/                         # Python origin MCP server
│   ├── ipixel_mcp/
│   │   ├── __main__.py             # FastMCP app, Streamable HTTP, auth middleware
│   │   ├── device.py               # single persistent BLE session + asyncio lock
│   │   ├── safety.py               # image/text limits, schema helpers (F-2/3/6)
│   │   ├── modes/display.py        # Mode A tools
│   │   ├── modes/notify.py         # Mode B tools + notification queue
│   │   └── modes/gallery.py        # Mode C tools (reads assets/manifest.json)
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

## 7. Delivery phases

- **Phase 0 — Origin MVP (local/tailnet, authless):** FastMCP Streamable HTTP server;
  `device.py` persistent BLE session + lock; `display_text` / `display_image`
  (bytes-only, limits) / `get_device_info`. Bound to loopback. Test with Claude Code
  over tailnet. *Exit:* board shows text/img from Claude Code.
- **Phase 1 — Safety hardening:** all `safety.py` limits (F-2/3/6/11), pinned deps +
  `pip-audit`, generic errors (F-9), static-bearer auth on origin, gate
  `clear`/`delete`. Unit tests with mocked BLE. *Exit:* security controls table ✔.
- **Phase 2 — Modes B & C:** notification queue + `notify.*`; `gallery.*` + assets +
  manifest; Claude Code notify-hook example. *Exit:* operator-alert + presets working.
- **Phase 3 — Public exposure:** `cloudflared` tunnel + Access service token; Worker
  with `workers-oauth-provider` (authless first, then single-operator GitHub OAuth +
  DCR); connect from **Claude.ai web** and **Claude Desktop**. *Exit:* all three
  clients connected; OAuth flow green.
- **Phase 4 — Ops & polish:** systemd units, Tailscale ACL doc, rate limiting,
  structured logging/metrics, runbook, README quickstarts per client, optional web-ack
  endpoint for Mode B.

---

## 8. Open questions / decisions for the operator

1. **Auth for the public path:** GitHub-OAuth-restricted-to-you (recommended) vs.
   one-password login vs. authless-behind-Access? (Affects Worker `defaultHandler`.)
2. **Worker→origin bridge:** Cloudflare Tunnel (keeps origin tailnet-only —
   recommended) vs. Tailscale **Funnel** (simpler, origin becomes public `*.ts.net`)?
3. **Destructive tools** (`clear`/`delete`): expose behind an admin scope, or omit
   entirely?
4. **Custom domain** for the Worker, or use the `*.workers.dev` default?
5. **Device host & board model** (matrix W×H, single or multiple boards → multi-device
   routing in tools?).
6. Do you want me to also **upstream the `pypixelcolor` fixes** (F-1/F-5/F-6/F-3) as
   PRs, or only vendor a local hardened wrapper?

---

*Next step after sign-off: scaffold `server/` (Phase 0) and `worker/` and wire the
first end-to-end `display_text` from Claude Code over the tailnet.*
