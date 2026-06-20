"""Entrypoint: load config from env, start the BLE supervisor, serve the MCP app.

Binds loopback by default (review C-6: never 0.0.0.0; cloudflared forwards to
loopback). A tailnet bind can be added once tailscaled is up.

Env:
  IPIXEL_ADDRESS              BLE MAC of the board (required)
  IPIXEL_STATIC_TOKEN         static bearer for the direct Claude Code path (required)
  IPIXEL_HOST                 bind host (default 127.0.0.1)
  IPIXEL_PORT                 bind port (default 8765)
  IPIXEL_LOGLEVEL             default INFO
  IPIXEL_ACCESS_TEAM          Cloudflare Access team domain (enables Worker path)
  IPIXEL_ACCESS_AUD           Cloudflare Access application AUD tag
"""

from __future__ import annotations

import logging
import os
import sys


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("IPIXEL_LOGLEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("ipixel_mcp")

    address = os.environ.get("IPIXEL_ADDRESS")
    static_token = os.environ.get("IPIXEL_STATIC_TOKEN")
    if not address:
        log.error("IPIXEL_ADDRESS is required")
        return 2
    if not static_token:
        log.error("IPIXEL_STATIC_TOKEN is required (the direct tailnet auth)")
        return 2

    host = os.environ.get("IPIXEL_HOST", "127.0.0.1")
    port = int(os.environ.get("IPIXEL_PORT", "8765"))

    try:
        import uvicorn
    except ImportError:
        log.error("uvicorn is not installed; install the 'server' extras")
        return 1

    from .app import build_app
    from .auth import make_access_jwt_verifier
    from .device import DeviceManager

    dm = DeviceManager(address)

    # Optional Cloudflare Access JWT verifier (the Worker path). When the team +
    # AUD are configured, the origin verifies the Worker's service-token JWT.
    verifier = None
    team = os.environ.get("IPIXEL_ACCESS_TEAM")
    aud = os.environ.get("IPIXEL_ACCESS_AUD")
    if team and aud:
        verifier = make_access_jwt_verifier(team, aud)
        log.info("Cloudflare Access JWT verification enabled (team=%s)", team)

    # The supervisor starts inside the running loop via a startup hook.
    app = build_app(dm, static_token=static_token, access_jwt_verifier=verifier)

    log.info("ipixel-mcp origin starting on http://%s:%d (board=%s)", host, port, address)
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
