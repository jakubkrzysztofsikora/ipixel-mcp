# Observability on a headless Pi

The origin is stateless and logs to **journald only** (no app log files — keeps
the SD card alive, review C-6). This is how to see what's happening remotely and
how to read `/healthz`.

## 1. Logs (journald)

```bash
# Live tail of the origin:
sudo journalctl -u ipixel-mcp -f
# Last boot, since a time, with priority filter:
sudo journalctl -u ipixel-mcp -b --since "10 min ago" -p info
# The other two daemons:
sudo journalctl -u cloudflared -f
sudo journalctl -u tailscaled -f
```

Remote access to a headless Pi:
- **Tailscale SSH** (preferred): `tailscale ssh pi@ipixel-board` then journalctl.
  Scoped by the ACL in [`tailscale/acls.hujson`](./tailscale/acls.hujson) so only
  operators can connect — no public port 22.
- **One-off pull:** `tailscale ssh pi@ipixel-board sudo journalctl -u ipixel-mcp --since today --no-pager`.

### Optional: ship logs off-box
For a truly hands-off panel, forward journald to a central collector so you can see
logs without SSHing in (and survive an SD wipe):
- **systemd-journal-upload** → a `systemd-journal-remote` host, or
- **Promtail → Loki/Grafana** (filter on `unit=ipixel-mcp`), or
- **vector**/`rsyslog` forwarding.
Keep the **prod log level at INFO** (set in the unit via `IPIXEL_LOGLEVEL=INFO`):
the underlying library logs full frame hex at DEBUG (security review F-13), which
would both leak image bytes and flood whatever sink you ship to. Never run DEBUG
in production except for short, deliberate troubleshooting.

## 2. `/healthz` field reference

`GET http://127.0.0.1:8765/healthz` (unauthenticated, exempt path) returns
`DeviceManager.health()`:

```json
{
  "address": "AA:BB:CC:DD:EE:FF",
  "state": "connected",
  "connected": true,
  "consecutive_failures": 0,
  "circuit_open": false,
  "mtu": 247,
  "device": { "width": 64, "height": 64, "led_type": "..." },
  "last_op_ok_age_s": 3.21
}
```

| Field | Meaning | What to watch for |
|---|---|---|
| `state` | BLE link state machine: `disconnected` / `connecting` / `connected` | Stuck in `connecting` = adapter or board problem; `disconnected` persisting = supervisor can't link. |
| `connected` | True only when `state==connected` and a live client exists | The single best "is the board usable" flag. |
| `consecutive_failures` | Count of consecutive failed BLE ops/connects since last success | Climbing = a flaky link / sleeping panel; resets to 0 on success. |
| `circuit_open` | True once `consecutive_failures` hits the breaker threshold | The supervisor has tripped its breaker and is backing off; writes will fast-fail until it recovers. **Alert on this.** |
| `mtu` | Negotiated ATT MTU (expect ~247) | A low value (e.g. 23) after a degraded reconnect means chunking is unsafe (review H-MTU); a warning is logged. |
| `device` | Cached `DeviceInfo` (width/height/led_type), invalidated on reconnect | `null` until first connect / info read. Used for per-model enum gating. |
| `last_op_ok_age_s` | Seconds since the last successful BLE op (null if none yet) | Large/growing while `connected:true` = silent stall (link up, transfers failing). |

## 3. Simple external uptime / health check

Because `/healthz` is loopback-only, probe it through a reachable path:

- **From the tailnet** (operator laptop or a tailnet monitor box):
  ```bash
  curl -fsS https://ipixel-board.<tailnet>.ts.net:8765/healthz \
    | python3 -c 'import sys,json; h=json.load(sys.stdin); \
        sys.exit(0 if h["connected"] and not h["circuit_open"] else 1)'
  ```
  Exit code drives any uptime tool (cron + alert, Uptime Kuma, Healthchecks.io
  cron-ping, etc.).
- **Through the tunnel** (public, Access-protected): an external monitor that
  holds the CF Access service token can hit
  `https://board-tunnel.example.com/healthz`. Most simple uptime services can't
  present Access creds, so prefer the tailnet probe for liveness and reserve the
  public probe for end-to-end checks.
- **Local watchdog:** a tiny systemd timer on the Pi that curls loopback
  `/healthz` and, if `circuit_open` is true for N minutes, `systemctl restart
  bluetooth` then `ipixel-mcp` (last-resort adapter reset escalation, review C-3).

Recommended alert conditions: `connected==false` for >2 min, `circuit_open==true`,
or `last_op_ok_age_s` growing without bound while `connected==true`.
