/**
 * Single-operator login via GitHub OAuth (PLAN §5; review M-REDIRECT).
 *
 * This is the OAuthProvider `defaultHandler`: it owns everything that is NOT an
 * API route (i.e. the /authorize UI and the GitHub callback). Flow:
 *
 *   1. claude.ai (or Desktop/Code) hits /authorize on this Worker.
 *      We parse the OAuth request, then redirect the browser to GitHub.
 *   2. GitHub redirects back to /callback with a code.
 *      We exchange it, fetch the GitHub login, and check it against
 *      ALLOWED_GITHUB_LOGIN. If it doesn't match -> 403 (fail closed).
 *   3. On match, we mint the MCP grant via OAuthProvider.completeAuthorization,
 *      storing MINIMAL props (login + granted scopes). The provider issues the
 *      claude.ai token (audience = this Worker).
 *
 * FALLBACK (documented, not wired): instead of GitHub you can render a single
 * password form here (compare against a `LOGIN_PASSWORD` secret with a
 * constant-time check) and call completeAuthorization the same way. GitHub is
 * recommended because it avoids handling a password at the edge.
 *
 * NOTE: the exact OAuthHelpers method names (parseAuthRequest /
 * completeAuthorization / lookupClient) and their option shapes must be
 * verified against the installed @cloudflare/workers-oauth-provider version —
 * see worker/README.md "Version verification".
 */

import type { Env, GrantProps } from "./types";
import { scopesForOperator } from "./scopes";
import {
  clientIdAlreadyApproved,
  parseRedirectApproval,
  renderApprovalDialog,
} from "./approval";

const GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize";
const GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token";
const GITHUB_USER_URL = "https://api.github.com/user";

/**
 * Pure allow-list check. Case-insensitive, trims config whitespace, rejects
 * empty logins. Exported for unit testing.
 */
export function isAllowedGithubLogin(
  login: string | null | undefined,
  allowed: string | null | undefined,
): boolean {
  if (!login || !allowed) return false;
  const a = login.trim().toLowerCase();
  const b = allowed.trim().toLowerCase();
  return a.length > 0 && a === b;
}

/** Build the GitHub authorize redirect URL. Pure; exported for testing. */
export function buildGithubAuthorizeUrl(opts: {
  clientId: string;
  redirectUri: string;
  state: string;
  scope?: string;
}): string {
  const u = new URL(GITHUB_AUTHORIZE_URL);
  u.searchParams.set("client_id", opts.clientId);
  u.searchParams.set("redirect_uri", opts.redirectUri);
  u.searchParams.set("state", opts.state);
  // "read:user" is enough to read the login; no repo/admin scopes.
  u.searchParams.set("scope", opts.scope ?? "read:user");
  u.searchParams.set("allow_signup", "false");
  return u.toString();
}

async function exchangeCodeForToken(
  env: Env,
  code: string,
  redirectUri: string,
): Promise<string | null> {
  const res = await fetch(GITHUB_TOKEN_URL, {
    method: "POST",
    headers: { accept: "application/json", "content-type": "application/json" },
    body: JSON.stringify({
      client_id: env.GITHUB_CLIENT_ID,
      client_secret: env.GITHUB_CLIENT_SECRET,
      code,
      redirect_uri: redirectUri,
    }),
  });
  if (!res.ok) return null;
  const data = (await res.json()) as { access_token?: string };
  return data.access_token ?? null;
}

async function fetchGithubLogin(token: string): Promise<string | null> {
  const res = await fetch(GITHUB_USER_URL, {
    headers: {
      authorization: `Bearer ${token}`,
      accept: "application/vnd.github+json",
      "user-agent": "ipixel-mcp-worker",
    },
  });
  if (!res.ok) return null;
  const data = (await res.json()) as { login?: string };
  return data.login ?? null;
}

export const githubAuthHandler = {
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    // ----- GET /authorize : start of the OAuth flow ------------------------
    // NB: must be method-gated, else it also swallows the POST handler below and
    // the consent dialog can never submit (review TOP-1).
    if (url.pathname === "/authorize" && request.method === "GET") {
      // The provider parses the inbound OAuth 2.1 authorize request for us.
      const oauthReqInfo = await env.OAUTH_PROVIDER.parseAuthRequest(request);
      const clientId = oauthReqInfo.clientId;
      if (!clientId) {
        return new Response("Invalid OAuth request", { status: 400 });
      }

      // If this client was already approved by the operator before, skip the
      // consent dialog and go straight to GitHub.
      if (
        await clientIdAlreadyApproved(request, clientId, env.COOKIE_SECRET)
      ) {
        return redirectToGithub(request, env, oauthReqInfo);
      }

      // Otherwise render a minimal consent dialog; submit posts back here.
      return renderApprovalDialog(request, {
        client: await env.OAUTH_PROVIDER.lookupClient(clientId),
        server: { name: "iPixel MCP", description: "LED matrix board control" },
        state: { oauthReqInfo },
      });
    }

    // ----- POST /authorize : consent dialog submitted ----------------------
    if (url.pathname === "/authorize" && request.method === "POST") {
      const { state, headers } = await parseRedirectApproval(
        request,
        env.COOKIE_SECRET,
      );
      if (!state.oauthReqInfo) {
        return new Response("Invalid approval", { status: 400 });
      }
      return redirectToGithub(request, env, state.oauthReqInfo, headers);
    }

    // ----- /callback : GitHub redirected back ------------------------------
    if (url.pathname === "/callback") {
      const code = url.searchParams.get("code");
      const stateParam = url.searchParams.get("state");
      if (!code || !stateParam) {
        return new Response("Missing code/state", { status: 400 });
      }

      // State carries the original OAuth request info (base64 JSON).
      let oauthReqInfo: any;
      try {
        oauthReqInfo = JSON.parse(atob(stateParam));
      } catch {
        return new Response("Invalid state", { status: 400 });
      }

      const redirectUri = new URL("/callback", request.url).toString();
      const ghToken = await exchangeCodeForToken(env, code, redirectUri);
      if (!ghToken) {
        return new Response("GitHub token exchange failed", { status: 502 });
      }

      const login = await fetchGithubLogin(ghToken);

      // THE allow-list gate. Fail closed.
      if (!isAllowedGithubLogin(login, env.ALLOWED_GITHUB_LOGIN)) {
        return new Response(
          "Forbidden: this MCP server is restricted to a single operator.",
          { status: 403 },
        );
      }

      const scopes = scopesForOperator(login as string, env.ALLOWED_GITHUB_LOGIN);
      const props: GrantProps = { login: login as string, scopes };

      // Mint the MCP grant. The provider issues the code/token to claude.ai.
      const { redirectTo } = await env.OAUTH_PROVIDER.completeAuthorization({
        request: oauthReqInfo,
        userId: login as string,
        metadata: { label: login as string },
        scope: scopes,
        props,
      });

      return Response.redirect(redirectTo, 302);
    }

    return new Response("Not found", { status: 404 });
  },
};

/** Redirect the browser to GitHub, encoding the OAuth request into `state`. */
function redirectToGithub(
  request: Request,
  env: Env,
  oauthReqInfo: unknown,
  extraHeaders: Record<string, string> = {},
): Response {
  const redirectUri = new URL("/callback", request.url).toString();
  const state = btoa(JSON.stringify(oauthReqInfo));
  const location = buildGithubAuthorizeUrl({
    clientId: env.GITHUB_CLIENT_ID,
    redirectUri,
    state,
  });
  return new Response(null, {
    status: 302,
    headers: { ...extraHeaders, location },
  });
}
