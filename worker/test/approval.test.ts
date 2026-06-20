import { describe, it, expect } from "vitest";
import {
  clientIdAlreadyApproved,
  parseRedirectApproval,
} from "../src/approval";

const SECRET = "test-cookie-secret-please-rotate";

async function makeApprovedRequest(clientId: string): Promise<Request> {
  // Drive a POST through parseRedirectApproval to produce a valid signed cookie,
  // then replay it on a fresh GET request.
  const state = btoa(JSON.stringify({ oauthReqInfo: { clientId } }));
  const form = new FormData();
  form.set("state", state);
  const post = new Request("https://mcp.example.com/authorize", {
    method: "POST",
    body: form,
  });
  const { headers } = await parseRedirectApproval(post, SECRET);
  const setCookie = headers["Set-Cookie"];
  const cookiePair = setCookie.split(";")[0]; // name=value
  return new Request("https://mcp.example.com/authorize", {
    headers: { Cookie: cookiePair },
  });
}

describe("approval cookie round-trip", () => {
  it("recognizes a previously approved client via the signed cookie", async () => {
    const req = await makeApprovedRequest("client-A");
    expect(await clientIdAlreadyApproved(req, "client-A", SECRET)).toBe(true);
  });

  it("does not recognize a different client", async () => {
    const req = await makeApprovedRequest("client-A");
    expect(await clientIdAlreadyApproved(req, "client-B", SECRET)).toBe(false);
  });

  it("rejects a cookie signed with the wrong secret (no forgery)", async () => {
    const req = await makeApprovedRequest("client-A");
    expect(await clientIdAlreadyApproved(req, "client-A", "WRONG-SECRET")).toBe(
      false,
    );
  });

  it("returns false when no cookie is present", async () => {
    const req = new Request("https://mcp.example.com/authorize");
    expect(await clientIdAlreadyApproved(req, "client-A", SECRET)).toBe(false);
  });

  it("parses the oauth state back out of the approval POST", async () => {
    const state = btoa(JSON.stringify({ oauthReqInfo: { clientId: "c1" } }));
    const form = new FormData();
    form.set("state", state);
    const post = new Request("https://mcp.example.com/authorize", {
      method: "POST",
      body: form,
    });
    const { state: parsed } = await parseRedirectApproval(post, SECRET);
    expect((parsed.oauthReqInfo as any).clientId).toBe("c1");
  });
});
