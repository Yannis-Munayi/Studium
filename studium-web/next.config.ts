import type { NextConfig } from "next";

/**
 * Next configuration (spec §4, §5.1).
 *
 * The only non-default here is the backend origin, which the proxy route in
 * `app/api/backend/[...path]/route.ts` reads. It is deliberately server-side
 * only (no `NEXT_PUBLIC_` prefix): the browser never learns the FastAPI origin,
 * so there is exactly one place -- the proxy -- where a request can pick up
 * auth, and no second code path that could forget to.
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,

  // Spec §4 "no server-side rendering fallback needed since the app requires
  // authentication".
  poweredByHeader: false,

  /**
   * Infrastructure §4.3. `standalone` emits `.next/standalone` with a minimal
   * `server.js` and only the `node_modules` actually reached at runtime, which
   * is what the Dockerfile's runtime stage copies.
   *
   * The alternative — shipping the whole `node_modules` and running
   * `next start` — costs about 600MB against §4.3's 1GB VM, and the memory it
   * takes is memory the SSE proxy does not have. This is the only change this
   * subsystem made to the frontend build.
   */
  output: "standalone",

  eslint: {
    dirs: ["app", "components", "lib"],
  },
};

export default nextConfig;
