# examples/ — client integration references

Concrete, minimal references for wiring each client to the ipixel-mcp origin.
See [`../docs/PLAN.md`](../docs/PLAN.md) for the design and
[`../deploy/`](../deploy/) for provisioning.

## Index

| Example | Mode | What it shows |
|---|---|---|
| [`claude-code-notify-hook/`](./claude-code-notify-hook/) | B (`notify.*`) | Claude Code `.claude/settings.json` hooks using the native `mcp_tool` handler: `notify_operator` on `Notification` (permission/idle prompt) and `clear_notification` on `Stop`. README covers the Elicitation alternative. |
| [`tailnet-service-display/`](./tailnet-service-display/) | A (`display.*`) | A minimal Python script any tailnet service uses to push text/an image to the board via the MCP `/mcp` endpoint with the static bearer (JSON-RPC `tools/call`). |

Both target the **direct tailnet path** (static bearer, no OAuth — review E-1).
claude.ai web / Desktop instead use the public Cloudflare Worker URL (Phase 4).
