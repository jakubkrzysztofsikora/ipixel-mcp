# Provisioning runbook — fresh Raspberry Pi → working ipixel-mcp

End-to-end bring-up of the device host (Phase 3, pre-exposure). Order matters: get
the **loopback origin + BLE** healthy first, then layer reachability (tailnet,
tunnel) on top. The origin binds loopback and does **not** depend on tailscaled to
start (review C-6 bind-order fix), so you can verify it before any networking.

Three coupled daemons end-state: `tailscaled` (tailnet for Claude Code direct),
`cloudflared` (Worker bridge for claude.ai), `ipixel-mcp` (the origin). They start
independently; only `cloudflared`/origin have a soft ordering. See
[`README.md`](./README.md) for the dependency diagram.

---

## 0. Prerequisites
- Raspberry Pi (Pi 4 / Pi 5 / Zero 2 W) with onboard BLE, or a USB BLE dongle.
- The iPixel Color board, powered, within BLE range.
- A Cloudflare account + a domain on Cloudflare (for Phase 4 exposure).
- A Tailscale tailnet + admin access.

## 1. OS
- Flash **Raspberry Pi OS Lite (64-bit)** (headless; no desktop needed).
- Enable SSH in the imager (or `touch /boot/ssh`), set a strong user password / key.
- First boot:
  ```bash
  sudo apt-get update && sudo apt-get full-upgrade -y
  sudo raspi-config nonint do_hostname ipixel-board
  ```

## 2. Python 3.11 (confirm before assuming 3.12 — review note)
Pi OS (Bookworm) ships Python **3.11**, which is fine for the origin.
```bash
python3 --version          # expect 3.11.x
sudo apt-get install -y python3 python3-venv python3-pip
```
Do not install 3.12 from source unless you have a specific need; 3.11 is supported.

## 3. BlueZ / Bluetooth permissions (review C-6)
```bash
sudo apt-get install -y bluetooth bluez libglib2.0-dev
sudo systemctl enable --now bluetooth
hciconfig -a            # or: bluetoothctl show   -> confirm an HCI adapter is up
```
- Create the service user and add it to the `bluetooth` group so it can talk to
  BlueZ over D-Bus **without root**:
  ```bash
  sudo useradd --system --no-create-home --shell /usr/sbin/nologin ipixel
  sudo usermod -aG bluetooth ipixel
  ```
- If GATT writes fail with permission errors, confirm the D-Bus policy for the
  `bluetooth` group allows `org.bluez` (default on Bookworm) and that the user's
  group membership has taken effect (re-login / restart the service).
- Disable BT auto-suspend if the panel drops a lot: append `btusb.enable_autosuspend=0`
  (USB dongle) or check `Powersave` in `/etc/bluetooth/main.conf`.

## 4. Install the server
```bash
sudo install -d -o ipixel -g ipixel /opt/ipixel-mcp
sudo -u ipixel git clone <repo> /opt/ipixel-mcp        # or rsync the tree
cd /opt/ipixel-mcp/server
sudo -u ipixel python3 -m venv /opt/ipixel-mcp/venv
sudo -u ipixel /opt/ipixel-mcp/venv/bin/pip install -e .
# pulls mcp, uvicorn, starlette, pypixelcolor, Pillow, bleak
```

## 5. Identify / pair the board (BLE MAC)
The origin needs `IPIXEL_ADDRESS` (the board's BLE MAC). iPixel boards use
BLE "Just Works" pairing — no PIN, so trust is by physical/radio proximity
(security review F-10): keep the host on a trusted network and the board nearby.
```bash
sudo bluetoothctl
  scan on            # watch for the board's name (e.g. "iPixel..." / "LED...")
  # note its address, e.g. AA:BB:CC:DD:EE:FF
  scan off
  quit
```
You generally do **not** need an explicit `pair`/`trust` for a Just-Works GATT
peripheral; bleak connects directly by MAC. If your panel insists, `pair` + `trust`
it once in `bluetoothctl`.

## 6. Secrets / config
Create the env file (see [`secrets.md`](./secrets.md) for full detail):
```bash
sudo install -d -m 0750 -o ipixel -g ipixel /etc/ipixel-mcp
sudo install -m 0600 -o ipixel -g ipixel /dev/stdin /etc/ipixel-mcp/ipixel-mcp.env <<EOF
IPIXEL_ADDRESS=AA:BB:CC:DD:EE:FF
IPIXEL_STATIC_TOKEN=$(openssl rand -hex 32)
EOF
```

## 7. Install + enable the origin service
```bash
sudo cp /opt/ipixel-mcp/deploy/systemd/ipixel-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ipixel-mcp
```

## 8. Verify /healthz (origin is up, BLE connecting)
```bash
curl -s http://127.0.0.1:8765/healthz | python3 -m json.tool
```
Expect JSON with `state` transitioning to `connected` and `connected: true` once
the board links. Fields are explained in [`observability.md`](./observability.md).
Then prove a tool call from a local Claude Code:
```bash
claude mcp add --transport http ipixel http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $(sudo grep IPIXEL_STATIC_TOKEN /etc/ipixel-mcp/ipixel-mcp.env | cut -d= -f2)"
# then ask Claude Code to call display_text
```

## 9. Enable the three services — in order
The origin (step 7) is already up and is the only one with a hard prerequisite
(BlueZ). Now bring up reachability:

1. **tailscaled** (system-provided):
   ```bash
   sudo apt-get install -y tailscale
   sudo systemctl enable --now tailscaled
   sudo tailscale up --advertise-tags=tag:ipixel --ssh
   ```
   Merge [`tailscale/acls.hujson`](./tailscale/acls.hujson) into your tailnet
   policy so only operators can reach the board. Claude Code can now dial the
   board over the tailnet directly (static bearer).
2. **ipixel-mcp** — already enabled (step 7); confirm `systemctl is-active`.
3. **cloudflared** (Phase 4 exposure; see step 10).

Ordering note: the origin must come up **independent of** tailscaled (it binds
loopback). cloudflared has a *soft* `After=ipixel-mcp` but retries the origin, so
exact start order is non-fatal.

## 10. Bring up the tunnel (Phase 4)
```bash
sudo apt-get install -y cloudflared          # or the .deb from Cloudflare
cloudflared tunnel login                     # browser auth, one-time
cloudflared tunnel create ipixel             # writes /root/.cloudflared/<id>.json
sudo install -d -m 0750 /etc/cloudflared
sudo install -m 0600 ~/.cloudflared/<id>.json /etc/cloudflared/<id>.json
sudo cp /opt/ipixel-mcp/deploy/cloudflared/config.yml /etc/cloudflared/config.yml
# edit config.yml: set tunnel id, credentials-file path, hostname
cloudflared tunnel route dns ipixel board-tunnel.example.com
sudo cloudflared service install             # or use deploy/systemd/cloudflared.service
sudo systemctl enable --now cloudflared
```
Then create the Cloudflare **Access** app + **service token** protecting
`board-tunnel.example.com` and wire the Worker (see PLAN §5, secrets.md §2b).
Verify the public path: a request through the Worker reaches `/healthz`.

## 11. Connect each client
- **Claude Code (tailnet, direct):**
  `claude mcp add --transport http ipixel https://ipixel-board.<tailnet>.ts.net:8765/mcp --header "Authorization: Bearer $TOKEN"`
  (or the tunnel hostname). No OAuth on this path by design (review E-1).
- **Claude.ai web / Claude Desktop:** add the **Worker** URL
  (`https://mcp.example.com/mcp`) as a custom connector; complete the OAuth login.
- **Mode B Notification hook (Claude Code):** see
  [`../examples/claude-code-notify-hook/`](../examples/claude-code-notify-hook/).
- **Any tailnet service (Mode A passthrough):** see
  [`../examples/tailnet-service-display/`](../examples/tailnet-service-display/).

---

## SD-card durability & power-loss posture (review C-6)
SD cards corrupt on power loss and wear out under constant small writes. Mitigate:
- **Log to volatile / journald only.** Set journald to RAM-backed storage so logs
  don't hammer the card: in `/etc/systemd/journald.conf` set `Storage=volatile`
  (or `Storage=auto` + a small `SystemMaxUse=`). The origin and cloudflared units
  log to journald, not files (no app log files on the card).
- **Read-mostly rootfs (optional, recommended for 24/7):** enable `overlayroot`
  / the Pi `raspi-config` "Overlay File System" so the rootfs is read-only with a
  RAM overlay; writes vanish on reboot (apply config changes by temporarily
  disabling the overlay). The origin is **stateless** (PLAN C-1), so a read-only
  rootfs is a natural fit. If you add the Phase 2 persisted notification queue,
  put it on a small writable partition or `tmpfs` (it's TTL'd/ephemeral anyway).
- **Flash-wear on the *board* (not the Pi):** notifications/ephemeral display
  default to `save_slot=0` (volatile RAM on the panel) so they don't burn the
  panel's EEPROM (review H-FLASH). Don't `set_time` on every reconnect.
- **Power-loss note:** use a quality 5V supply sized above the panel's full-white
  draw to avoid brownouts (a brownout mid-BLE-transfer can wedge the panel —
  review H-PANEL/H-WEDGE). Consider a small UPS/supercap HAT for clean shutdown.
  Because the rootfs is read-mostly and the app is stateless, an unclean power cut
  is survivable: on boot the services restart and the BLE supervisor re-links.
