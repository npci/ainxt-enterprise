#!/usr/bin/env bash
#
# AiNxt embed-svc starter — standalone, re-runnable at any time.
#
#   bash ./start-embed-service.sh
#
# Builds/starts the "embed-svc" and "kb-worker" containers (Knowledge Base
# document parsing, embedding, and reranking — see docs/KB_SETUP.md) and
# waits for embed-svc to report healthy.
#
# This is the embed-svc bring-up logic extracted out of kb-setup.sh's
# interactive questionnaire, so it can be (re)run on its own — by
# kb-setup.sh right after it writes your model choices to .env, by
# start-index-worker.sh (index-worker needs embed-svc reachable), or by
# hand any time you just want to (re)start embed-svc without answering the
# KB questionnaire again. It reads its configuration back out of .env
# (EMBED_PROVIDER, USE_DOCLING_PARSER, EMBED_SVC_PORT) rather than asking —
# run ./kb-setup.sh first if none of that is set yet.
#
# Deliberately non-interactive (no TTY requirement): this script is meant to
# be called from other scripts as well as run directly.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

# ── Colors / helpers (mirrors kb-setup.sh/install.sh) ────────────────────────
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

# ── Read existing configuration back out of .env ────────────────────────────
_env_get() {
  grep -E "^${1}=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true
}

EMBED_PROVIDER="$(_env_get EMBED_PROVIDER)"
EMBED_PROVIDER="${EMBED_PROVIDER:-ollama}"
USE_DOCLING_PARSER="$(_env_get USE_DOCLING_PARSER)"
EMBED_SVC_PORT="$(_env_get EMBED_SVC_PORT)"
EMBED_SVC_PORT="${EMBED_SVC_PORT:-8001}"

step "Starting the Knowledge Base model server (embed-svc)"
say "  ${DIM}First build downloads the chosen models — this can take several minutes.${R}"
if ! "${COMPOSE[@]}" --profile embed up -d --build embed-svc kb-worker; then
  die "embed-svc failed to start. Retry later with: ${COMPOSE[*]} --profile embed up -d --build embed-svc kb-worker"
fi
ok "embed-svc and kb-worker started"

if [[ "$EMBED_PROVIDER" == "ollama" ]]; then
  step "Downloading nomic-embed-text (~280MB)"
  docker exec ainxt-ollama ollama pull nomic-embed-text >/dev/null 2>&1 \
    && ok "nomic-embed-text ready" \
    || warn "pull failed — run 'docker exec ainxt-ollama ollama pull nomic-embed-text' later"
fi

if [[ "$USE_DOCLING_PARSER" == "1" ]]; then
  step "Downloading Docling parsing models (layout, table, OCR — ~150MB)"
  docker exec ainxt-embed-svc docling-tools models download >/dev/null 2>&1 \
    && ok "Docling models ready" \
    || warn "download failed — run 'docker exec ainxt-embed-svc docling-tools models download' later"
fi

step "Waiting for embed-svc to become healthy"
waited=0; limit=300
until curl -fsS -m 5 "http://localhost:${EMBED_SVC_PORT}/health" >/dev/null 2>&1; do
  if (( waited >= limit )); then
    die "embed-svc did not become healthy in ${limit}s — check: ${COMPOSE[*]} logs embed-svc"
  fi
  sleep 5; waited=$((waited+5))
done
ok "embed-svc healthy after ${waited}s"
