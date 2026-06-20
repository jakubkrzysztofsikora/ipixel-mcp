import { describe, it, expect } from "vitest";
import { githubAuthHandler } from "../src/github-handler";

// Regression for review TOP-1: GET /authorize starts the flow (parses the OAuth
// request); POST /authorize is the consent submission and must NOT fall into the
// GET branch (it previously did, so the dialog could never submit).

function makeEnv(parseCalls: { n: number }) {
  return {
    COOKIE_SECRET: "test-secret",
    OAUTH_PROVIDER: {
      async parseAuthRequest() {
        parseCalls.n += 1;
        return { clientId: "client-123" };
      },
      async lookupClient() {
        return { clientId: "client-123", clientName: "Test" };
      },
    },
  } as any;
}

const ctx = {} as any;

describe("/authorize method routing (TOP-1)", () => {
  it("GET /authorize parses the OAuth request", async () => {
    const calls = { n: 0 };
    const env = makeEnv(calls);
    const req = new Request("https://mcp.example.com/authorize?client_id=client-123", {
      method: "GET",
    });
    await githubAuthHandler.fetch(req, env, ctx);
    expect(calls.n).toBe(1);
  });

  it("POST /authorize does NOT enter the GET branch (no parseAuthRequest)", async () => {
    const calls = { n: 0 };
    const env = makeEnv(calls);
    const req = new Request("https://mcp.example.com/authorize", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: "",
    });
    // The POST branch calls parseRedirectApproval which will reject on an empty
    // body; what matters is it did NOT take the GET path.
    try {
      await githubAuthHandler.fetch(req, env, ctx);
    } catch {
      /* expected: malformed approval submission */
    }
    expect(calls.n).toBe(0);
  });
});
