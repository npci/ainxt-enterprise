# Build context is the REPOSITORY ROOT, not ai-ui/.
#
# ai-ui cannot be built in isolation: vite.config.js aliases '@abs' to
# ../ABStudio/frontend/src, so the sibling ABStudio sources must be present or
# the build fails with
#   [vite:load-fallback] Could not load /ABStudio/frontend/src/BuildStudio.jsx
#
#   docker build -f ai-ui/Dockerfile .

# ── Build stage ────────────────────────────────────────────────────────────────
FROM node:20-alpine AS builder
WORKDIR /app/ai-ui

# Dependencies first so a source-only change does not re-resolve the tree.
COPY ai-ui/package.json ./
COPY ai-ui/package-lock.json* ./
# `npm ci` when a lockfile is present, `npm install` otherwise. ai-ui ships no
# lockfile today, so this stays correct either way.
RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; \
    else npm install --no-audit --no-fund; fi

# Sibling sources reached through the '@abs' alias.
COPY ABStudio/frontend/src /app/ABStudio/frontend/src

COPY ai-ui/ ./
RUN npm run build

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM nginx:1.27-alpine AS runtime

# vite builds with base '/portal/', so the bundle is served from that prefix.
COPY --from=builder /app/ai-ui/dist /usr/share/nginx/html/portal
COPY ai-ui/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 5173
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1:5173/portal/ >/dev/null 2>&1 || exit 1
