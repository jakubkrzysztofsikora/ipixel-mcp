/**
 * Scope mapping for the single operator (PLAN §3, §5; review C-4, M-ANNOT).
 *
 * These scope strings MUST match the origin's gating constants in
 * `server/ipixel_mcp/auth.py` exactly:
 *   ipixel:display, ipixel:notify, ipixel:gallery, ipixel:admin
 *
 * The Worker forwards the *granted* scopes to the origin in a trusted
 * `X-Mcp-Scopes` header (space-separated). The origin honors that header ONLY
 * on the Cloudflare-Access path, so the audience invariant holds: we never
 * forward the user's OAuth token, only a vetted scope list.
 */

export const SCOPE_DISPLAY = "ipixel:display";
export const SCOPE_NOTIFY = "ipixel:notify";
export const SCOPE_GALLERY = "ipixel:gallery";
export const SCOPE_ADMIN = "ipixel:admin";

/** Non-admin operational scopes every authenticated client gets. */
export const BASE_SCOPES: readonly string[] = [
  SCOPE_DISPLAY,
  SCOPE_NOTIFY,
  SCOPE_GALLERY,
];

/** All scopes advertised in AS metadata (`scopes_supported`). */
export const ALL_SCOPES: readonly string[] = [...BASE_SCOPES, SCOPE_ADMIN];

/**
 * Map the authenticated operator to their granted MCP scopes.
 *
 * Single-operator model: the one allow-listed GitHub login is the owner and
 * gets admin (destructive clear/delete). Anyone else never reaches this
 * function — the GitHub handler rejects them before grant — but we fail closed
 * here too: a non-owner login gets only the base (non-admin) scopes.
 *
 * @param login          the authenticated GitHub login
 * @param ownerLogin     the configured ALLOWED_GITHUB_LOGIN
 */
export function scopesForOperator(login: string, ownerLogin: string): string[] {
  const isOwner =
    login.length > 0 &&
    login.toLowerCase() === ownerLogin.trim().toLowerCase();
  return isOwner ? [...ALL_SCOPES] : [...BASE_SCOPES];
}

/** Serialize a scope list for the `X-Mcp-Scopes` header / OAuth `scope`. */
export function serializeScopes(scopes: readonly string[]): string {
  return scopes.join(" ");
}
