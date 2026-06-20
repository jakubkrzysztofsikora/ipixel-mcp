# Claude Code → board notifications (Mode B)

Make the LED matrix light up when a Claude Code session needs you, and clear it
when the turn finishes. Uses Claude Code's **native `mcp_tool` hook handler** —
no shell scripts, no `curl` (PLAN §3 Mode B / review E-2).

## Install

1. Register the origin with Claude Code so the `server` name `ipixel` resolves
   (direct tailnet path, static bearer — no OAuth on this path, review E-1):
   ```bash
   claude mcp add --transport http ipixel \
     https://ipixel-board.<tailnet>.ts.net:8765/mcp \
     --header "Authorization: Bearer $IPIXEL_STATIC_TOKEN"
   ```
2. Copy the `hooks` block from [`settings.json`](./settings.json) into your
   project's `.claude/settings.json` (or `~/.claude/settings.json` for all
   projects). The file here is a standalone snippet you merge in — Claude Code
   reads `.claude/settings.json`, not this directory.

## How it works

| Event | `notification_type` matched | Tool called | Result |
|---|---|---|---|
| `Notification` | `permission_prompt`, `idle_prompt` | `notify_operator` | Renders a `blocked` (red) banner: "operator input needed", tagged `source=claude-code`, auto-expiring after `ttl_seconds`. |
| `Stop` | (turn finished) | `clear_notification` | Clears this source's notification, restoring the prior display. |

- **`source` is effectively required.** The tailnet path is a single shared
  identity, so `source` is the only way the board (and `list_notifications`) can
  tell agents/sessions apart (review M-OWN). Set it to something meaningful per
  project if you run several.
- **`ttl_seconds` is enforced server-side.** `Stop` is the correct *clear*
  trigger but it can be missed (crash, kill). The TTL guarantees the board
  doesn't stay stuck on red forever (review "Smaller flags" / H-FLASH). Tune it
  to a bit longer than your typical think-time.
- **`blocked` preempts Mode A** display and restores it on clear (state stack,
  PLAN §3). Lower-urgency alerts can use `level: "info"`/`"warn"`.

## Why a board is an *ambient/secondary* channel

An LED matrix has **no ack button**, scrolls slowly, shows one thing at a time,
and has multi-second latency (review H-PANEL). Treat it as a glanceable nudge,
**not** your only alert. Pair `blocked` with a real push channel (phone,
Slack/ntfy, etc.) for anything you can't afford to miss.

## Alternative: Elicitation (the spec-correct "needs input" path)

When the *MCP server itself* needs structured input from the user, the proper
mechanism is **MCP Elicitation** — the server raises an elicitation request and
the client (Claude Code) surfaces it and returns an `ElicitationResult`. Claude
Code exposes `Elicitation` / `ElicitationResult` hook events you can also hook.

Use elicitation when there's a real **closed loop** (the answer flows back into
the tool call). Use the `notify_operator` hook above when you only need an
**ambient out-of-band nudge** — which is the board's actual strength, since it has
no input device to close the loop (PLAN §3 Mode B). The two are complementary:
the hook lights the panel; elicitation is how an answer would come back through a
channel that *can* receive one.
