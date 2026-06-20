/**
 * Consent dialog + signed "approved client" cookie.
 *
 * Adapted from the standard Cloudflare workers-oauth-provider GitHub example.
 * The cookie records which OAuth client_ids the operator has already approved,
 * so repeat connects skip the dialog. It is signed (HMAC-SHA256) with
 * COOKIE_SECRET so a client cannot forge approval.
 *
 * Self-contained (Web Crypto only) so it is testable without the provider.
 */

const COOKIE_NAME = "ipixel-approved-clients";
const ONE_YEAR = 60 * 60 * 24 * 365;

interface ApprovalState {
  oauthReqInfo?: unknown;
}

// ----- HMAC signing helpers --------------------------------------------------

async function importKey(secret: string): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
}

function toHex(buf: ArrayBuffer): string {
  return [...new Uint8Array(buf)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function sign(secret: string, payload: string): Promise<string> {
  const key = await importKey(secret);
  const sig = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(payload),
  );
  return toHex(sig);
}

async function verify(
  secret: string,
  payload: string,
  signature: string,
): Promise<boolean> {
  const expected = await sign(secret, payload);
  // length-safe constant-time-ish compare
  if (expected.length !== signature.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) {
    diff |= expected.charCodeAt(i) ^ signature.charCodeAt(i);
  }
  return diff === 0;
}

// ----- signed OAuth state (review: state was unsigned/tamperable) ------------

/** Sign an object into an opaque, integrity-protected `state` string. */
export async function signState(secret: string, obj: unknown): Promise<string> {
  const payload = btoa(JSON.stringify(obj));
  const signature = await sign(secret, payload);
  return `${signature}.${payload}`;
}

/** Verify + decode a signed `state`. Returns null on tamper/format error. */
export async function verifyState(
  secret: string,
  state: string,
): Promise<unknown | null> {
  const dot = state.indexOf(".");
  if (dot === -1) return null;
  const signature = state.slice(0, dot);
  const payload = state.slice(dot + 1);
  if (!(await verify(secret, payload, signature))) return null;
  try {
    return JSON.parse(atob(payload));
  } catch {
    return null;
  }
}

// ----- cookie parsing --------------------------------------------------------

function parseCookies(header: string | null): Record<string, string> {
  const out: Record<string, string> = {};
  if (!header) return out;
  for (const part of header.split(";")) {
    const idx = part.indexOf("=");
    if (idx === -1) continue;
    out[part.slice(0, idx).trim()] = part.slice(idx + 1).trim();
  }
  return out;
}

/** Decode the approved-clients cookie value: `${signature}.${base64json}`. */
async function readApprovedClients(
  cookieValue: string | undefined,
  secret: string,
): Promise<string[]> {
  if (!cookieValue) return [];
  const dot = cookieValue.indexOf(".");
  if (dot === -1) return [];
  const signature = cookieValue.slice(0, dot);
  const payload = cookieValue.slice(dot + 1);
  if (!(await verify(secret, payload, signature))) return [];
  try {
    const arr = JSON.parse(atob(payload));
    return Array.isArray(arr) ? arr.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

/** True if `clientId` is in the operator's signed approved-clients cookie. */
export async function clientIdAlreadyApproved(
  request: Request,
  clientId: string,
  secret: string,
): Promise<boolean> {
  const cookies = parseCookies(request.headers.get("Cookie"));
  const approved = await readApprovedClients(cookies[COOKIE_NAME], secret);
  return approved.includes(clientId);
}

async function buildApprovedCookie(
  request: Request,
  clientId: string,
  secret: string,
): Promise<string> {
  const cookies = parseCookies(request.headers.get("Cookie"));
  const existing = await readApprovedClients(cookies[COOKIE_NAME], secret);
  const next = Array.from(new Set([...existing, clientId]));
  const payload = btoa(JSON.stringify(next));
  const signature = await sign(secret, payload);
  const value = `${signature}.${payload}`;
  return `${COOKIE_NAME}=${value}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=${ONE_YEAR}`;
}

// ----- approval POST parsing -------------------------------------------------

/**
 * Parse the submitted consent form. Reads the encoded OAuth state and returns
 * a Set-Cookie header recording approval of this client_id.
 */
export async function parseRedirectApproval(
  request: Request,
  secret: string,
): Promise<{ state: ApprovalState; headers: Record<string, string> }> {
  const form = await request.formData();
  const encodedState = form.get("state");
  if (typeof encodedState !== "string") {
    return { state: {}, headers: {} };
  }
  let state: ApprovalState = {};
  try {
    state = JSON.parse(atob(encodedState)) as ApprovalState;
  } catch {
    return { state: {}, headers: {} };
  }
  const clientId =
    (state.oauthReqInfo as { clientId?: string } | undefined)?.clientId ?? "";
  const headers: Record<string, string> = {};
  if (clientId) {
    headers["Set-Cookie"] = await buildApprovedCookie(request, clientId, secret);
  }
  return { state, headers };
}

// ----- dialog rendering ------------------------------------------------------

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Render a minimal HTML consent page that POSTs back to /authorize. */
export function renderApprovalDialog(
  _request: Request,
  opts: {
    client: { clientName?: string; clientId?: string } | null;
    server: { name: string; description?: string };
    state: ApprovalState;
  },
): Response {
  const encodedState = btoa(JSON.stringify(opts.state));
  const clientName = escapeHtml(
    opts.client?.clientName || opts.client?.clientId || "An MCP client",
  );
  const serverName = escapeHtml(opts.server.name);
  const serverDesc = escapeHtml(opts.server.description ?? "");

  const html = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Authorize ${serverName}</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 30rem; margin: 4rem auto; padding: 0 1rem; }
    .card { border: 1px solid #ddd; border-radius: 12px; padding: 1.5rem; }
    button { font-size: 1rem; padding: 0.6rem 1.2rem; border-radius: 8px; border: 0; cursor: pointer; }
    .approve { background: #1f6feb; color: #fff; }
    .muted { color: #666; font-size: 0.9rem; }
  </style>
</head>
<body>
  <div class="card">
    <h1>${serverName}</h1>
    <p class="muted">${serverDesc}</p>
    <p><strong>${clientName}</strong> is requesting access. You'll sign in with GitHub next.</p>
    <form method="POST" action="/authorize">
      <input type="hidden" name="state" value="${encodedState}" />
      <button class="approve" type="submit">Approve and continue</button>
    </form>
  </div>
</body>
</html>`;

  return new Response(html, {
    headers: { "content-type": "text/html; charset=utf-8" },
  });
}
