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
  // authentication". Output is a standard server build; Fly.io deployment is
  // subsystem 7's (§19).
  poweredByHeader: false,

  eslint: {
    dirs: ["app", "components", "lib"],
  },
};

export default nextConfig;
