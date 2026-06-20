# deploy/ — ops & provisioning (Phase 3, pre-exposure)

Everything needed to take a fresh Raspberry Pi to a running, reachable
ipixel-mcp origin. This is the "real long pole" (review C-6): three coupled
daemons, secrets across two trust domains, SD-card/power survivability, and
credential rotation — all of which must be solid **before** public exposure.

## Index

| File | What it is |
|---|---|
| [`runbook.md`](./runbook.md) | Step-by-step bring-up: OS → Python 3.11 → BlueZ → install → pair board → enable the three services → verify `/healthz` → tunnel → connect clients. Includes SD-card durability + power-loss posture. |
| [`secrets.md`](./secrets.md) | Where every secret lives (perms) + a rotation runbook for the static token, CF Access service token, GitHub OAuth secret, tailnet keys, and tunnel creds. |
| [`observability.md`](./observability.md) | Reading logs on a headless Pi (journald, Tailscale SSH, optional shipping), the `/healthz` field reference, and a simple external uptime check. |
| [`systemd/ipixel-mcp.service`](./systemd/ipixel-mcp.service) | The origin unit: non-root + bluetooth group, correct ordering, `Restart=on-failure` w/ backoff, EnvironmentFile, hardening that still allows BLE. |
| [`systemd/cloudflared.service`](./systemd/cloudflared.service) | Tunnel unit (or use the packaged `cloudflared service install`). |
| [`cloudflared/config.yml`](./cloudflared/config.yml) | Tunnel ingress: public hostname → `http://127.0.0.1:8765`, Access-protected, service-token note. |
| [`tailscale/acls.hujson`](./tailscale/acls.hujson) | `tag:ipixel` + an ACL limiting who on the tailnet can reach the origin's MCP port + Tailscale SSH for remote admin. |

tailscaled is **system-provided** — enable it with `systemctl enable --now
tailscaled` (see runbook §9); there's no unit shipped here.

## Daemon dependency & ordering

```
                         Cloudflare edge (public: mcp.example.com -> Worker)
                                        │  OAuth terminated; fetch() tunnel host
                                        ▼
   ┌──────────────────────── Raspberry Pi (device host) ────────────────────────┐
   │                                                                             │
   │   tailscaled ──────────┐                 cloudflared ──────────┐            │
   │   (system unit)        │ tailnet         (deploy/systemd or    │ CF Tunnel  │
   │   enable --now         │ reachability    `service install`)    │ + Access   │
   │                        │                 Wants/After=          │ svc token  │
   │                        │                 ipixel-mcp (SOFT,      │            │
   │                        │                 retries; non-fatal)   │            │
   │                        ▼                                        ▼            │
   │                ╔═══════════════════════════════════════════════════════╗   │
   │   Claude Code  ║  ipixel-mcp.service  (the origin)                      ║   │
   │   (tailnet,    ║   After/Wants = network-online + bluetooth  (HARD-ish) ║   │
   │    direct) ───▶║   binds 127.0.0.1:8765  ALWAYS (loopback by design)    ║   │
   │   static bearer║   does NOT order after tailscaled  <-- bind-order fix  ║   │
   │                ║   Restart=on-failure, RestartSec backoff               ║   │
   │                ╚════════════════════════╤══════════════════════════════╝   │
   │                                         │ single-flight BLE lock            │
   │                                         ▼  (disposable link + supervisor)   │
   │                                       BlueZ / HCI ── BLE ──▶ 🟥 iPixel board │
   └─────────────────────────────────────────────────────────────────────────┘
```

Key ordering rules (review C-6):
1. **The origin binds loopback and must start even if `tailscaled` isn't up yet.**
   It is ordered `After=`/`Wants=` only `network-online` + `bluetooth`, never
   `tailscaled`. Otherwise it would crash-loop waiting for the tailnet.
2. **tailscaled and cloudflared are independent of the origin's bind.** They
   provide reachability. cloudflared has a *soft* `After=ipixel-mcp` purely so the
   first proxied request is more likely to land; it retries regardless.
3. **bluetooth is the only hard local dependency** — the BLE supervisor needs an
   HCI adapter. Even so, the supervisor treats the link as disposable and
   reconnects with backoff, so a not-yet-ready adapter just delays the first link.

Start order in practice: `bluetooth` → `ipixel-mcp` → `tailscaled` → `cloudflared`
(runbook §9), but only the bluetooth→origin edge is load-bearing.
