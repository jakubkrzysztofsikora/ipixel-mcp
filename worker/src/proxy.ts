/**
 * MCP proxy handler (PLAN §5; review C-1, C-4).
 *
 * This runs ONLY after `@cloudflare/workers-oauth-provider` has validated the
 * claude.ai bearer token whose audience is THIS Worker. The provider hands us
 * the props that were stored at grant time via `ctx.props`.
 *
 * What this does:
 *   - Reads the granted scopes from the validated token props.
 *   - Builds an upstream request to `${ORIGIN_URL}/mcp`.
 *   - Authenticates to the origin with a Cloudflare Access SERVICE TOKEN
 *     (CF-Access-Client-Id / CF-Access-Client-Secret) — NOT the user's token.
 *   - Forwards the granted scopes in a trusted `X-Mcp-Scopes` header.
 *   - Passes MCP-relevant headers through verbatim, both directions.
 *   - Streams the response body back without buffering/transforming.
 *   - Returns generic errors (no upstream internals leak to the client).
 */

import type { GrantProps } from "./types";

export interface ProxyEnv {
  ORIGIN_URL: string;
  CF_ACCESS_CLIENT_ID: string;
  CF_ACCESS_CLIENT_SECRET: string;
}

/**
 * Request headers we forward from the MCP client to the origin, verbatim.
 * Lower-cased for case-insensitive matching. We deliberately do NOT forward
 * Authorization (that's the user token — audience invariant) or Cookie/Host.
 */
export const FORWARD_REQUEST_HEADERS: readonly string[] = [
  "content-type",
  "accept",
  "mcp-protocol-version",
  "mcp-session-id", // origin is stateless, but pass it through if a client sends it
];

/**
 * Response headers we pass back from the origin to the MCP client, verbatim.
 * Content-Type/Length and the MCP headers matter; hop-by-hop headers are dropped.
 */
export const FORWARD_RESPONSE_HEADERS: readonly string[] = [
  "content-type",
  "cache-control",
  "mcp-protocol-version",
  "mcp-session-id",
  // NB: www-authenticate is deliberately NOT forwarded (review MED-3). The
  // Worker is the sole OAuth authority on the public path; the origin advertises
  // no OAuth (E-1), so relaying an auth challenge from it would only confuse
  // claude.ai's discovery state machine.
];

/** Build the joined upstream URL: ORIGIN_URL + "/mcp" (no double slashes). */
export function buildUpstreamUrl(originUrl: string): string {
  return originUrl.replace(/\/+$/, "") + "/mcp";
}

/** Copy only the allow-listed headers from `src` into a fresh Headers. */
export function pickHeaders(
  src: Headers,
  allow: readonly string[],
): Headers {
  const out = new Headers();
  for (const name of allow) {
    const v = src.get(name);
    if (v !== null) out.set(name, v);
  }
  return out;
}

/**
 * Build the upstream Request from the inbound client request + grant props.
 * Pure-ish: takes everything it needs as args so it can be unit-tested.
 */
export function buildUpstreamRequest(
  inbound: Request,
  env: ProxyEnv,
  props: GrantProps,
): Request {
  const headers = pickHeaders(inbound.headers, FORWARD_REQUEST_HEADERS);

  // Authenticate to the origin AS THE WORKER via the Access service token.
  headers.set("CF-Access-Client-Id", env.CF_ACCESS_CLIENT_ID);
  headers.set("CF-Access-Client-Secret", env.CF_ACCESS_CLIENT_SECRET);

  // Trusted scope list (origin honors this only on the Access path).
  headers.set("X-Mcp-Scopes", (props.scopes ?? []).join(" "));

  return new Request(buildUpstreamUrl(env.ORIGIN_URL), {
    method: inbound.method,
    headers,
    // Stream the request body through (POST JSON-RPC). GET/HEAD have no body.
    body:
      inbound.method === "GET" || inbound.method === "HEAD"
        ? undefined
        : inbound.body,
    // Required by the runtime when a ReadableStream body is set.
    ...(inbound.method === "GET" || inbound.method === "HEAD"
      ? {}
      : { duplex: "half" as const }),
    redirect: "manual",
  });
}

/** Build the client-facing Response, streaming the upstream body through. */
export function buildClientResponse(upstream: Response): Response {
  const headers = pickHeaders(upstream.headers, FORWARD_RESPONSE_HEADERS);
  // Body is streamed, not buffered — do not call .text()/.json() here.
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers,
  });
}

/**
 * The handler shape `apiHandlers` expects: a fetch-style export.
 * `ctx.props` carries the grant props the OAuth provider validated.
 */
export const mcpProxyHandler = {
  async fetch(
    request: Request,
    env: ProxyEnv,
    ctx: ExecutionContext & { props?: GrantProps },
  ): Promise<Response> {
    const props: GrantProps = (ctx.props as GrantProps) ?? { login: "", scopes: [] };

    try {
      const upstreamReq = buildUpstreamRequest(request, env, props);
      const upstreamRes = await fetch(upstreamReq);
      return buildClientResponse(upstreamRes);
    } catch (_err) {
      // Generic error: never leak origin/tunnel internals to the client.
      return new Response(
        JSON.stringify({
          jsonrpc: "2.0",
          error: { code: -32603, message: "Upstream MCP origin unavailable" },
          id: null,
        }),
        { status: 502, headers: { "content-type": "application/json" } },
      );
    }
  },
};
