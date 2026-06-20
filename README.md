# ipixel-mcp

Remote **MCP server(s)** to drive [iPixel Color](https://github.com/lucagoc/pypixelcolor)
BLE LED matrix boards from **Claude.ai web**, **Claude Desktop**, and **Claude Code** —
served over a **Tailscale** tailnet, with a **Cloudflare Worker** terminating OAuth 2.1
(Dynamic Client Registration compatible) and proxying to the tailnet origin.

> **Status:** Phases 0–4 implemented and tested (hardware-free); pre-hardware /
> pre-deploy. The repo contains the security review + plan **and** the working
> code: the Python origin server (`server/`, 3 modes), the Cloudflare Worker
> (`worker/`), a security-patched `pypixelcolor` fork (`vendor/pypixelcolor/`),
> and deploy + client examples (`deploy/`, `examples/`). Remaining: real-hardware
> BLE smoke test, live Cloudflare/Tailscale deploy, and Phase-5 polish
> (rate limiting, metrics). See [docs/PLAN.md](docs/PLAN.md) §7 for phase status.

## Modes

- **`display.*`** — tailnet passthrough: any service/agent shows custom text or images.
- **`notify.*`** — operator-input notifications: an agent (e.g. Claude Code) that is
  blocked renders an attention banner on the board.
- **`gallery.*`** — a curated set of prebuilt images, ASCII art, and texts.

## Documents

- **[docs/PLAN.md](docs/PLAN.md)** (v2) — architecture, the three modes, OAuth/Tailscale/
  Cloudflare design, security controls, and delivery phases.
- **[docs/PLAN_REVIEW.md](docs/PLAN_REVIEW.md)** — three-persona adversarial review
  (fullstack/devops, microcontroller/BLE, MCP/harness) that produced the v2 revisions.
- **[docs/SECURITY_REVIEW_pypixelcolor.md](docs/SECURITY_REVIEW_pypixelcolor.md)** —
  Cyberlegion-style audit of `pypixelcolor`, which the plan's security controls address.

## Architecture in one line

`Claude.ai/Desktop → Cloudflare Worker (OAuth 2.1 + DCR) → Cloudflare Tunnel → tailnet
origin (Python FastMCP + pypixelcolor → BLE)`; **Claude Code** reaches the tailnet
origin directly with a static bearer token.

## Key constraint

Claude.ai web and Claude Desktop dial from Anthropic's cloud, so they **cannot** reach
a tailnet address — hence the public Cloudflare Worker. Claude Code dials locally and
**can** use the tailnet origin directly. See the plan for details.
