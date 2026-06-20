import { describe, it, expect } from "vitest";
import {
  scopesForOperator,
  serializeScopes,
  ALL_SCOPES,
  BASE_SCOPES,
  SCOPE_ADMIN,
} from "../src/scopes";

describe("scopesForOperator", () => {
  it("grants admin to the configured owner (case-insensitive)", () => {
    const s = scopesForOperator("OctoCat", "octocat");
    expect(s).toEqual([...ALL_SCOPES]);
    expect(s).toContain(SCOPE_ADMIN);
  });

  it("trims whitespace in the configured owner login", () => {
    const s = scopesForOperator("octocat", "  octocat  ");
    expect(s).toContain(SCOPE_ADMIN);
  });

  it("denies admin to a non-owner (fail closed -> base scopes only)", () => {
    const s = scopesForOperator("intruder", "octocat");
    expect(s).toEqual([...BASE_SCOPES]);
    expect(s).not.toContain(SCOPE_ADMIN);
  });

  it("denies admin for an empty login", () => {
    const s = scopesForOperator("", "octocat");
    expect(s).not.toContain(SCOPE_ADMIN);
  });

  it("uses the exact scope strings the origin gates on", () => {
    expect(ALL_SCOPES).toEqual([
      "ipixel:display",
      "ipixel:notify",
      "ipixel:gallery",
      "ipixel:admin",
    ]);
  });
});

describe("serializeScopes", () => {
  it("space-joins for the X-Mcp-Scopes header", () => {
    expect(serializeScopes(["ipixel:display", "ipixel:admin"])).toBe(
      "ipixel:display ipixel:admin",
    );
  });
});
