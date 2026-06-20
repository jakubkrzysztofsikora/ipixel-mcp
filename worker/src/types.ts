/** Shared types for the ipixel-mcp Worker. */

import type { OAuthHelpers } from "@cloudflare/workers-oauth-provider";

/**
 * Props stored on the OAuth grant at approval time and surfaced to the proxy
 * handler as `ctx.props`. Keep this MINIMAL (review C-4): just the operator
 * identity and the granted MCP scopes. Never store the user's GitHub token.
 */
export interface GrantProps {
  /** Authenticated GitHub login of the operator. */
  login: string;
  /** Granted MCP scopes (e.g. ["ipixel:display", ...]). */
  scopes: string[];
  [key: string]: unknown;
}

/** Full Worker env: bindings + vars + secrets (see wrangler.jsonc). */
export interface Env {
  /** Injected by OAuthProvider into handlers; the helper API surface. */
  OAUTH_PROVIDER: OAuthHelpers;
  /** KV namespace used by the OAuth provider for token/grant/client storage. */
  OAUTH_KV: KVNamespace;

  // vars
  ORIGIN_URL: string;
  ALLOWED_GITHUB_LOGIN: string;

  // secrets
  CF_ACCESS_CLIENT_ID: string;
  CF_ACCESS_CLIENT_SECRET: string;
  GITHUB_CLIENT_ID: string;
  GITHUB_CLIENT_SECRET: string;
  COOKIE_SECRET: string;
}
