import { defineConfig } from "vitest/config";

// Pure-logic tests run in the default (node) environment. Node 18+ provides
// global fetch/Request/Response/Headers and Web Crypto (crypto.subtle), which
// is all these tests need — no Cloudflare runtime required. If you later add
// integration tests that exercise the OAuthProvider end-to-end, switch to
// @cloudflare/vitest-pool-workers.
export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    environment: "node",
  },
});
