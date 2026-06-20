# Secrets inventory & rotation runbook

This system spreads secrets across **two trust domains**: the **Pi (origin)** and
the **Cloudflare account (Worker + Tunnel + Access)**. Keep them straight; rotation
differs per domain.

> Principle: no secret in git, no secret in a world-readable file, every secret
> rotatable without a redeploy of unrelated components.

## 1. Inventory — where each secret lives

| Secret | Lives | Owner/perms | Used by |
|---|---|---|---|
| `IPIXEL_STATIC_TOKEN` | `/etc/ipixel-mcp/ipixel-mcp.env` on the Pi | `ipixel:ipixel`, `0600` | Origin auth (Claude Code direct tailnet path) |
| `IPIXEL_ADDRESS` (board BLE MAC) | same env file | `0600` | Not secret, but kept with config |
| CF Access **service token** (Client-Id + Client-Secret) | wrangler secret on the Worker (`CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`) | Cloudflare | Worker → origin (sent as `CF-Access-Client-*` headers) |
| CF Access app **policy / audience (AUD)** | Cloudflare Access app config (not secret, but pinned) | Cloudflare | Origin verifies the resulting Access JWT against this AUD |
| **cloudflared tunnel credentials** | `/etc/cloudflared/<tunnel-id>.json` on the Pi | `cloudflared:cloudflared`, `0600` | cloudflared to dial the edge |
| **GitHub OAuth** client secret (single-operator login) | wrangler secret on the Worker (`GITHUB_CLIENT_SECRET`) | Cloudflare | Worker `defaultHandler` IdP login |
| Worker OAuth **signing/KV** material | Workers KV + Worker secrets (managed by `workers-oauth-provider`) | Cloudflare | Issued client tokens (hashed in KV) |
| **Tailnet auth key** | used once at `tailscale up`; not stored long-term | Tailscale admin console | Joining the Pi to the tailnet (tagged) |

The origin **never** holds the GitHub secret, the Worker OAuth keys, or any
claude.ai user token (audience invariant, PLAN §5 / review C-4). The Worker
**never** holds `IPIXEL_STATIC_TOKEN`. This separation bounds the blast radius of
any single leak.

### The origin env file

```ini
# /etc/ipixel-mcp/ipixel-mcp.env   (chmod 0600, chown ipixel:ipixel)
IPIXEL_ADDRESS=AA:BB:CC:DD:EE:FF
IPIXEL_STATIC_TOKEN=<64 hex chars from: openssl rand -hex 32>
# Optional overrides (defaults: 127.0.0.1 / 8765):
# IPIXEL_HOST=127.0.0.1
# IPIXEL_PORT=8765
```

```bash
sudo install -d -m 0750 -o ipixel -g ipixel /etc/ipixel-mcp
sudo install -m 0600 -o ipixel -g ipixel /dev/stdin /etc/ipixel-mcp/ipixel-mcp.env <<'EOF'
IPIXEL_ADDRESS=AA:BB:CC:DD:EE:FF
IPIXEL_STATIC_TOKEN=replace-me
EOF
```

Worker secrets are set with wrangler (never in `wrangler.jsonc`, which is committed):

```bash
cd worker
wrangler secret put CF_ACCESS_CLIENT_ID
wrangler secret put CF_ACCESS_CLIENT_SECRET
wrangler secret put GITHUB_CLIENT_SECRET
```

---

## 2. Rotation runbook

General rule: **add the new credential, switch consumers, then revoke the old** —
no downtime, no lockout. Test against `/healthz` and one tool call after each.

### 2a. `IPIXEL_STATIC_TOKEN` (Claude Code direct path)
1. Mint: `openssl rand -hex 32`.
2. Edit `/etc/ipixel-mcp/ipixel-mcp.env`, replace the value (keep `0600`).
3. `sudo systemctl restart ipixel-mcp` (the token is read at start).
4. Update each Claude Code client: `claude mcp remove ipixel` then re-add with the
   new `--header "Authorization: Bearer <new>"` (or edit the stored header).
5. Verify: a `tools/list` from Claude Code returns the real tools (not synthetic
   auth tools). Cadence: every 90 days, or immediately on suspected leak.

> There is no overlap window for a single static token. If you need zero-downtime,
> temporarily accept two tokens in `auth.py` (env list) during the swap, then drop
> the old one.

### 2b. CF Access service token (Worker → origin)
1. Cloudflare dashboard → Access → Service Auth → **create a new** service token
   (e.g. `ipixel-worker-2`). Add it to the Access policy on
   `board-tunnel.example.com` alongside the old one.
2. Update Worker secrets to the new pair and deploy:
   `wrangler secret put CF_ACCESS_CLIENT_ID && wrangler secret put CF_ACCESS_CLIENT_SECRET && wrangler deploy`.
3. Verify a claude.ai call still reaches the board.
4. Remove the **old** service token from the Access policy, then delete it.
   Cadence: 90 days. Service tokens themselves expire (default 1 year) — rotate
   well before.

### 2c. GitHub OAuth client secret (single-operator login)
1. GitHub → the OAuth App → **Generate a new client secret** (GitHub allows two
   live at once).
2. `wrangler secret put GITHUB_CLIENT_SECRET && wrangler deploy`.
3. Re-run the OAuth login from claude.ai to confirm.
4. Delete the old secret in GitHub. Cadence: 90 days or on leak.

### 2d. Tailnet auth keys
- Keys are used **once** to join the Pi; they should be **reusable=false** (or
  ephemeral) and **expiry ≤ 90 days** so a leaked key is short-lived.
- Node key rotation: tailnet node keys auto-rotate; ensure **key expiry is ON**
  for `tag:ipixel` in the admin console (don't disable expiry for convenience).
- To rotate the joining key: mint a new tagged auth key, no action needed on the
  already-joined node. To force re-auth: `sudo tailscale up --force-reauth`.
- Revoke a compromised node from the admin console (Machines → remove). Cadence:
  review every 90 days.

### 2e. cloudflared tunnel credentials
1. `cloudflared tunnel create ipixel-2` → writes a new `<id>.json`.
2. Re-point DNS: `cloudflared tunnel route dns ipixel-2 board-tunnel.example.com`.
3. Update `/etc/cloudflared/config.yml` (`tunnel:` + `credentials-file:`),
   `sudo systemctl restart cloudflared`, verify, then
   `cloudflared tunnel delete ipixel` (old one). Cadence: on leak or annually.
   The credentials JSON must stay `0600`.

### Leak response (any secret)
Rotate that one secret immediately per its section above; the trust separation
means you usually do **not** need to rotate the others. If the **Pi itself** is
compromised, rotate `IPIXEL_STATIC_TOKEN`, the tunnel creds, and remove the node
from the tailnet — but the Worker/GitHub/Access secrets are unaffected.
