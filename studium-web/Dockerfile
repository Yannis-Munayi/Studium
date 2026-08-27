# Studium frontend image (infrastructure §4.3).
#
# Three stages: dependencies, build, runtime. The runtime stage carries the
# standalone output and nothing else -- no source, no dev dependencies, no
# package manager. On a 1GB VM (§4.3) that is not an optimisation, it is the
# difference between the app fitting and not.
#
# node:22-alpine, matching package.json's `engines: node >= 22`.

FROM node:22-alpine AS deps

WORKDIR /app
COPY package.json package-lock.json* ./
# `npm ci` rather than `npm install`: it installs exactly the lockfile and
# fails if package.json and the lock disagree, which is the check that keeps a
# deployed build identical to the one CI tested.
RUN npm ci


FROM node:22-alpine AS builder

WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Frontend §14 / F7: `lib/auth.ts` throws in production unless this is set, and
# `next build` prerenders pages that call it. Set for the build only -- the
# runtime value comes from fly.toml, where it is a visible decision rather than
# an inherited one.
ENV STUDIUM_ALLOW_UNAUTHENTICATED=1
ENV NEXT_TELEMETRY_DISABLED=1

RUN npm run build


FROM node:22-alpine AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

WORKDIR /app

# node:22-alpine ships a `node` user already. Non-root for the same reason the
# backend image is: a container that can rewrite its own application code turns
# a code-execution bug into a persistent one.
USER node

COPY --from=builder --chown=node:node /app/public ./public
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static

EXPOSE 3000

# §7.4's endpoint, so `docker run` behaves the way the deployment does.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:3000/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]
