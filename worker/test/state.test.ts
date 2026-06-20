import { describe, it, expect } from "vitest";
import { signState, verifyState } from "../src/approval";

// Regression: OAuth `state` must be HMAC-signed so a tampered clientId/redirect
// cannot reach completeAuthorization (PR review: state was unsigned base64).

const SECRET = "test-cookie-secret";

describe("signed OAuth state", () => {
  it("round-trips a signed object", async () => {
    const obj = { clientId: "abc", redirectUri: "https://claude.ai/cb" };
    const state = await signState(SECRET, obj);
    expect(await verifyState(SECRET, state)).toEqual(obj);
  });

  it("rejects a tampered payload", async () => {
    const state = await signState(SECRET, { clientId: "abc" });
    const dot = state.indexOf(".");
    const sig = state.slice(0, dot);
    const forged = btoa(JSON.stringify({ clientId: "attacker" }));
    expect(await verifyState(SECRET, `${sig}.${forged}`)).toBeNull();
  });

  it("rejects a wrong secret and malformed state", async () => {
    const state = await signState(SECRET, { clientId: "abc" });
    expect(await verifyState("other-secret", state)).toBeNull();
    expect(await verifyState(SECRET, "not-a-state")).toBeNull();
  });
});
