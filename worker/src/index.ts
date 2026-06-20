/**
 * ipixel-mcp public Worker entrypoint (PLAN §5, §6; review C-1, C-4, E-1, M-REDIRECT).
 *
 * `@cloudflare/workers-oauth-provider` makes this Worker a compliant OAuth 2.1
 * Authorization Server + Resource Server for claude.ai / Desktop / Code:
 *   - serves /authorize, /token, /register (DCR)
 *   - serves RFC 8414 AS metadata + RFC 9728 protected-resource metadata
 *   - enforces PKCE S256, hashes tokens in KV
 *
 * Routing:
 *   - apiHandlers["/mcp"]  -> the proxy (runs only AFTER token validation;
 *                             receives grant props via ctx.props). This is a
 *                             PLAIN auth proxy to the Python origin — NOT an
 *                             agents/McpAgent Durable Object host (review C-1).
 *   - defaultHandler       -> the single-operator GitHub login flow + /authorize
 *                             consent UI + /callback.
 *
 * Audience invariant (review C-4): the token the provider issues has THIS
 * Worker as its audience/resource. The proxy never forwards that token to the
 * origin; it authenticates to the origin with a Cloudflare Access service token
 * and forwards only the granted scopes (X-Mcp-Scopes).
 *
 * Version note: the OAuthProvider option names below match the documented shape
 * of @cloudflare/workers-oauth-provider. Confirm against the installed version
 * (see worker/README.md "Version verification") — option keys
 * (authorizeEndpoint vs apiRoute, etc.) have shifted across early releases.
 */

import OAuthProvider from "@cloudflare/workers-oauth-provider";

import { mcpProxyHandler } from "./proxy";
import { githubAuthHandler } from "./github-handler";
import { ALL_SCOPES } from "./scopes";

export default new OAuthProvider({
  // Protected MCP route(s). The map shape is the current README form
  // (path -> handler). Only /mcp (Streamable HTTP). No legacy /sse — the origin
  // is stateless POST request/response, so SSE buys us nothing here.
  apiHandlers: {
    "/mcp": mcpProxyHandler as any,
  },

  // Everything else (login UI, GitHub callback) is the default handler.
  defaultHandler: githubAuthHandler as any,

  // OAuth 2.1 endpoints the provider serves.
  authorizeEndpoint: "/authorize",
  tokenEndpoint: "/token",
  clientRegistrationEndpoint: "/register", // DCR (RFC 7591) — per-client redirects

  // Advertise our MCP scopes in AS metadata.
  scopesSupported: [...ALL_SCOPES],
});
