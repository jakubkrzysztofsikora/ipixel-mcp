# ipixel-mcp Worker (Phase 4 — public exposure)

The public-facing Cloudflare Worker that terminates **OAuth 2.1 (DCR-capable)**
for `claude.ai` / Claude Desktop / Claude Code and proxies authenticated MCP
requests to the **stateless** Python origin on the tailnet.

This Worker is a **plain authenticating request/response proxy** (PLAN review
C-1). It is **not** an `agents` / `McpAgent` Durable Object host — the MCP server
itself is Python on the device. There is no Durable Object binding.

## What it does

- `@cloudflare/workers-oauth-provider` makes the Worker an OAuth 2.1
  **Authorization Server + Resource Server**: it serves `/authorize`, `/token`,
  `/register` (Dynamic Client Registration / RFC 7591), RFC 8414 AS metadata and
  RFC 9728 protected-resource metadata, enforces PKCE S256, and stores hashed
  tokens in KV.
- **Single-operator login** (`src/github-handler.ts`): a GitHub OAuth flow
  restricted to one allow-listed login (`ALLOWED_GITHUB_LOGIN`). Anyone else gets
  a `403`. On success it mints the MCP grant with minimal props (`login` +
  granted scopes).
- **Authenticated proxy** (`src/proxy.ts`): after the provider validates the
  claude.ai token, the handler builds an upstream request to
  `${ORIGIN_URL}/mcp`, authenticates to the origin with a **Cloudflare Access
  service token**, forwards the granted scopes in `X-Mcp-Scopes`, passes MCP
  headers through verbatim, and **streams** the response body back.

### The audience invariant (review C-4) — read this

The token issued to claude.ai has **this Worker** as its audience/resource. The
Worker validates that token and then **never forwards it to the origin**
(forwarding it would be a confused-deputy / token-passthrough vulnerability).
Instead the Worker authenticates to the origin **as itself** using a Cloudflare
Access **service token** (`CF-Access-Client-Id` / `CF-Access-Client-Secret`) and
forwards only a vetted, space-separated scope list in a trusted `X-Mcp-Scopes`
header. The origin honors `X-Mcp-Scopes` **only** on the Access-authenticated
path (see `server/ipixel_mcp/auth.py`).

## File map

| File | Role |
|---|---|
| `src/index.ts` | `OAuthProvider` wiring: `apiHandlers: { "/mcp": ... }`, `defaultHandler`, endpoints, `scopesSupported`. |
| `src/proxy.ts` | MCP proxy handler: header allow-lists, upstream request builder, streaming response, generic errors. |
| `src/github-handler.ts` | GitHub OAuth `defaultHandler` + `/authorize` consent + `/callback`; the allow-list gate. |
| `src/approval.ts` | Signed "approved client" cookie (HMAC) + consent dialog. |
| `src/scopes.ts` | Operator → MCP scope mapping (`ipixel:display/notify/gallery/admin`). |
| `src/types.ts` | `Env`, `GrantProps`. |
| `test/*.test.ts` | Vitest unit tests for the pure logic. |

## Configuration

### Vars (`wrangler.jsonc`)

| Var | Meaning |
|---|---|
| `ORIGIN_URL` | Base URL of the `cloudflared` Tunnel hostname fronting the tailnet origin (the proxy appends `/mcp`). This host is protected by a Cloudflare Access service-token policy. |
| `ALLOWED_GITHUB_LOGIN` | The single GitHub login allowed to operate the board. |

### Secrets (`wrangler secret put …`)

| Secret | Meaning |
|---|---|
| `CF_ACCESS_CLIENT_ID` | Cloudflare Access service token client id → sent as `CF-Access-Client-Id`. |
| `CF_ACCESS_CLIENT_SECRET` | Cloudflare Access service token secret → sent as `CF-Access-Client-Secret`. |
| `GITHUB_CLIENT_ID` | GitHub OAuth App client id. |
| `GITHUB_CLIENT_SECRET` | GitHub OAuth App client secret. |
| `COOKIE_SECRET` | 32+ byte random secret signing the approved-client cookie (`openssl rand -hex 32`). |

## Deploy runbook

```sh
cd worker
npm install

# 1) Create the OAuth KV namespace (binding name MUST be OAUTH_KV).
wrangler kv namespace create OAUTH_KV
wrangler kv namespace create OAUTH_KV --preview
#   -> paste the returned ids into wrangler.jsonc (id + preview_id).

# 2) Set vars in wrangler.jsonc:
#    ORIGIN_URL = your cloudflared tunnel hostname (e.g. https://ipixel-origin.example.com)
#    ALLOWED_GITHUB_LOGIN = your GitHub login

# 3) Push secrets.
wrangler secret put CF_ACCESS_CLIENT_ID
wrangler secret put CF_ACCESS_CLIENT_SECRET
wrangler secret put GITHUB_CLIENT_ID
wrangler secret put GITHUB_CLIENT_SECRET
wrangler secret put COOKIE_SECRET     # openssl rand -hex 32

# 4) Validate + ship.
npm run typecheck
npm test
wrangler deploy
```

### GitHub OAuth App

Create a GitHub OAuth App (Settings → Developer settings → OAuth Apps):

- **Homepage URL:** your Worker URL, e.g. `https://ipixel-mcp.<account>.workers.dev`
- **Authorization callback URL:** `https://<your-worker-host>/callback`
  (this is the Worker's own GitHub callback, **not** the claude.ai callback).

### Cloudflare Access service token + Tunnel

1. Run `cloudflared` co-located with `tailscaled` on the device; its tunnel
   ingress points at the loopback origin (`server/`). Note the tunnel hostname →
   that's `ORIGIN_URL`.
2. In Cloudflare Zero Trust, create an **Access application** for that hostname
   with a **service-token** policy; create a **service token** and put its
   id/secret into `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET`.
3. The origin (`server/ipixel_mcp/auth.py`) verifies the Access JWT itself before
   trusting `X-Mcp-Scopes`.

## How the clients connect

All three use standard OAuth against the Worker — **DCR per client** (review
M-REDIRECT). Do **not** hard-allowlist a single redirect URI; the provider's
`/register` endpoint registers each client's exact redirect(s):

- **Claude.ai web** → add a custom connector with URL
  `https://<your-worker-host>/mcp`. Its DCR redirect is
  `https://claude.ai/api/mcp/auth_callback`.
- **Claude Desktop** → add the same URL; it registers its **own** redirect.
- **Claude Code** → `claude mcp add --transport http ipixel https://<your-worker-host>/mcp`
  then `/mcp` and follow the OAuth prompt. Claude Code uses a **localhost
  loopback** redirect (e.g. `http://localhost:<port>/callback`), which DCR
  registers automatically.

> Claude Code can *also* hit the tailnet origin **directly** with a static
> bearer (PLAN §5) — that path bypasses this Worker entirely and is documented in
> the origin's README. This Worker is the path for cloud-originating clients.

The first time a client connects you'll see a consent page, then a GitHub login.
Only `ALLOWED_GITHUB_LOGIN` is accepted; everyone else gets `403`.

### Password fallback (not wired)

If you'd rather not use GitHub, `src/github-handler.ts` documents swapping the
`/callback` exchange for a single password form (compare against a
`LOGIN_PASSWORD` secret with a constant-time check), then calling
`completeAuthorization` the same way. GitHub is recommended (no password handled
at the edge).

## Version verification (please confirm before relying in prod)

Pinned versions are reasonable but **should be verified against what npm
installs**:

- `@cloudflare/workers-oauth-provider` — pinned `^0.0.5`. **Verified against the
  installed 0.0.5 typings:** the option keys used (`apiHandlers`,
  `defaultHandler`, `authorizeEndpoint`, `tokenEndpoint`,
  `clientRegistrationEndpoint`, `scopesSupported`) and the `OAuthHelpers` methods
  (`parseAuthRequest`, `lookupClient`, `completeAuthorization({ request, userId,
  metadata, scope, props })`) all match. This is a `0.0.x` package — re-check
  these names if you bump it, as the API has shifted across early releases.
- `wrangler` `^4`, `@cloudflare/workers-types`, `typescript`, `vitest` — pin to
  whatever your toolchain standardizes on.

## Tests

`npm test` runs `vitest` against the pure logic (no Cloudflare runtime needed —
Node 18+ provides `fetch`/`Request`/`Response`/`Headers`/`crypto.subtle`):

- scope mapping + owner/admin gating (`scopes.test.ts`)
- GitHub login allow-list + authorize URL building (`github-handler.test.ts`)
- header pass-through allow-lists, upstream request building (Access headers +
  `X-Mcp-Scopes`, user token dropped), streaming response, generic-error path
  (`proxy.test.ts`)
- signed approval-cookie round-trip / forgery rejection (`approval.test.ts`)

**Status:** built and verified in this environment — `npm install`, `npm test`
(23/23 pass) and `tsc --noEmit` (clean) all succeed under Node 22 / npm 10.
