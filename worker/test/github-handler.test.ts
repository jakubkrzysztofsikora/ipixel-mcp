import { describe, it, expect } from "vitest";
import {
  isAllowedGithubLogin,
  buildGithubAuthorizeUrl,
} from "../src/github-handler";

describe("isAllowedGithubLogin", () => {
  it("accepts the exact configured login", () => {
    expect(isAllowedGithubLogin("octocat", "octocat")).toBe(true);
  });

  it("is case-insensitive and trims config", () => {
    expect(isAllowedGithubLogin("OctoCat", " octocat ")).toBe(true);
  });

  it("rejects a different login", () => {
    expect(isAllowedGithubLogin("intruder", "octocat")).toBe(false);
  });

  it("rejects null/empty login or unset allow-list (fail closed)", () => {
    expect(isAllowedGithubLogin(null, "octocat")).toBe(false);
    expect(isAllowedGithubLogin("", "octocat")).toBe(false);
    expect(isAllowedGithubLogin("octocat", null)).toBe(false);
    expect(isAllowedGithubLogin("octocat", "")).toBe(false);
  });
});

describe("buildGithubAuthorizeUrl", () => {
  it("builds a github authorize url with read:user scope and no signup", () => {
    const url = new URL(
      buildGithubAuthorizeUrl({
        clientId: "gh_client",
        redirectUri: "https://mcp.example.com/callback",
        state: "abc123",
      }),
    );
    expect(url.origin + url.pathname).toBe(
      "https://github.com/login/oauth/authorize",
    );
    expect(url.searchParams.get("client_id")).toBe("gh_client");
    expect(url.searchParams.get("redirect_uri")).toBe(
      "https://mcp.example.com/callback",
    );
    expect(url.searchParams.get("state")).toBe("abc123");
    expect(url.searchParams.get("scope")).toBe("read:user");
    expect(url.searchParams.get("allow_signup")).toBe("false");
  });
});
