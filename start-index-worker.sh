#!/usr/bin/env bash
#
# AiNxt codebase-indexing starter — standalone, re-runnable at any time.
#
#   bash ./start-index-worker.sh
#
# Starts the "index-worker" container (workers/index_worker.py — codebase
# AST chunking + embedding for semantic code search / SDLC pipeline
# context). index-worker requires embed-svc to be reachable
# (EMBED_SVC_URL) — without it, index_worker.py raises "No embed service
# configured" on every job, and since this worker has no healthcheck
# defined, that failure would otherwise crash-loop silently rather than
# being surfaced. This script brings up embed-svc first (via
# ./start-embed-service.sh — a no-op if it's already running) so that
# never happens, then starts index-worker.
#
# Deliberately kept separate from ./install.sh and ./kb-setup.sh: codebase
# indexing is an opt-in step you run whenever you actually want to index a
# repository, not something every install needs by default.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

# ── Colors / helpers (mirrors kb-setup.sh/start-embed-service.sh) ───────────
if [[ -t 1 ]]; then
  B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[0m'
  GRN=$'\033[32m'; YLW=$'\033[33m'; RED=$'\033[31m'; CYN=$'\033[36m'
else
  B=""; DIM=""; R=""; GRN=""; YLW=""; RED=""; CYN=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s%s\n' "$CYN" "$R" "$B" "$*" "$R"; }
ok()   { printf '  %s✓%s %s\n' "$GRN" "$R" "$*"; }
warn() { printf '  %s!%s %s\n' "$YLW" "$R" "$*"; }
die()  { printf '\n  %s✗ %s%s\n\n' "$RED" "$*" "$R" >&2; exit 1; }

[[ -f .env ]] || die \
"No .env found in $(pwd).
  Run ./install.sh first — it creates .env and starts the base app."

command -v docker >/dev/null 2>&1 || die "Docker is required but was not found."
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  die "Docker Compose was not found. Install Docker Desktop, or the 'docker-compose-plugin' package."
fi

step "Ensuring embed-svc is up (index-worker needs it reachable)"
chmod 755 ./start-embed-service.sh 2>/dev/null || true
bash ./start-embed-service.sh \
  || die "embed-svc is required for codebase indexing and failed to start."

# Belt-and-suspenders: index-worker has no healthcheck, so a missing
# EMBED_SVC_URL would otherwise crash-loop silently rather than fail loudly
# here, before a single container even starts.
_EMBED_SVC_URL="$(grep -E '^EMBED_SVC_URL=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
[[ -n "$_EMBED_SVC_URL" ]] || die \
"EMBED_SVC_URL is still empty in .env even after start-embed-service.sh ran.
  Run ./kb-setup.sh first — codebase indexing reuses the Knowledge Base's
  embedding service and needs it configured."

step "Starting index-worker"
"${COMPOSE[@]}" --profile index up -d --build index-worker \
  || die "index-worker failed to start. Check: ${COMPOSE[*]} logs index-worker"
ok "index-worker started"
say ""
say "  Submit a repository for indexing via the Codebase/Index Router in the app."
say "  ${DIM}logs: ${COMPOSE[*]} logs -f index-worker${R}"
say ""
