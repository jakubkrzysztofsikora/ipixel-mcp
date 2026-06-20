import { describe, it, expect, vi, afterEach } from "vitest";
import {
  buildUpstreamUrl,
  pickHeaders,
  buildUpstreamRequest,
  buildClientResponse,
  mcpProxyHandler,
  FORWARD_REQUEST_HEADERS,
  type ProxyEnv,
} from "../src/proxy";
import type { GrantProps } from "../src/types";

const env: ProxyEnv = {
  ORIGIN_URL: "https://origin.example.com/",
  CF_ACCESS_CLIENT_ID: "cf-id",
  CF_ACCESS_CLIENT_SECRET: "cf-secret",
};

const props: GrantProps = {
  login: "octocat",
  scopes: ["ipixel:display", "ipixel:admin"],
};

afterEach(() => vi.restoreAllMocks());

describe("buildUpstreamUrl", () => {
  it("joins ORIGIN_URL + /mcp without double slashes", () => {
    expect(buildUpstreamUrl("https://o.example.com/")).toBe(
      "https://o.example.com/mcp",
    );
    expect(buildUpstreamUrl("https://o.example.com")).toBe(
      "https://o.example.com/mcp",
    );
  });
});

describe("pickHeaders allowlist", () => {
  it("copies only allow-listed headers", () => {
    const src = new Headers({
      "content-type": "application/json",
      accept: "text/event-stream",
      "mcp-protocol-version": "2025-11-25",
      authorization: "Bearer user-token",
      cookie: "secret=1",
      "x-evil": "no",
    });
    const out = pickHeaders(src, FORWARD_REQUEST_HEADERS);
    expect(out.get("content-type")).toBe("application/json");
    expect(out.get("accept")).toBe("text/event-stream");
    expect(out.get("mcp-protocol-version")).toBe("2025-11-25");
    // never forwarded:
    expect(out.get("authorization")).toBeNull();
    expect(out.get("cookie")).toBeNull();
    expect(out.get("x-evil")).toBeNull();
  });
});

describe("buildUpstreamRequest", () => {
  it("adds Access service-token headers and X-Mcp-Scopes, drops the user token", () => {
    const inbound = new Request("https://mcp.example.com/mcp", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "mcp-protocol-version": "2025-11-25",
        authorization: "Bearer USER_OAUTH_TOKEN",
      },
      body: JSON.stringify({ jsonrpc: "2.0", method: "tools/list", id: 1 }),
    });

    const req = buildUpstreamRequest(inbound, env, props);

    expect(req.url).toBe("https://origin.example.com/mcp");
    expect(req.method).toBe("POST");
    expect(req.headers.get("CF-Access-Client-Id")).toBe("cf-id");
    expect(req.headers.get("CF-Access-Client-Secret")).toBe("cf-secret");
    expect(req.headers.get("X-Mcp-Scopes")).toBe("ipixel:display ipixel:admin");
    // audience invariant: the user's OAuth token must NOT be forwarded.
    expect(req.headers.get("authorization")).toBeNull();
    // MCP header passes through.
    expect(req.headers.get("mcp-protocol-version")).toBe("2025-11-25");
  });

  it("emits an empty X-Mcp-Scopes when no scopes granted", () => {
    const inbound = new Request("https://mcp.example.com/mcp", { method: "POST", body: "{}" });
    const req = buildUpstreamRequest(inbound, env, { login: "x", scopes: [] });
    expect(req.headers.get("X-Mcp-Scopes")).toBe("");
  });
});

describe("buildClientResponse", () => {
  it("passes through status + allow-listed response headers", () => {
    const upstream = new Response("hi", {
      status: 200,
      headers: {
        "content-type": "application/json",
        "mcp-session-id": "abc",
        "set-cookie": "leak=1",
      },
    });
    const res = buildClientResponse(upstream);
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("application/json");
    expect(res.headers.get("mcp-session-id")).toBe("abc");
    // hop-by-hop / leaky headers dropped
    expect(res.headers.get("set-cookie")).toBeNull();
  });
});

describe("mcpProxyHandler.fetch", () => {
  it("proxies and streams the upstream response back", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const inbound = new Request("https://mcp.example.com/mcp", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    });

    const res = await mcpProxyHandler.fetch(inbound, env, { props } as any);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true });

    const sent = fetchSpy.mock.calls[0][0] as Request;
    expect(sent.url).toBe("https://origin.example.com/mcp");
    expect(sent.headers.get("X-Mcp-Scopes")).toBe("ipixel:display ipixel:admin");
  });

  it("returns a generic 502 (no internals) when the origin is unreachable", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("ECONNREFUSED tunnel down"));
    const inbound = new Request("https://mcp.example.com/mcp", { method: "POST", body: "{}" });
    const res = await mcpProxyHandler.fetch(inbound, env, { props } as any);
    expect(res.status).toBe(502);
    const body = (await res.json()) as { error: { message: string } };
    expect(body.error.message).toBe("Upstream MCP origin unavailable");
    expect(JSON.stringify(body)).not.toContain("ECONNREFUSED");
  });
});
