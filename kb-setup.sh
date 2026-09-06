#!/usr/bin/env bash
#
# AiNxt Knowledge Base setup — standalone, re-runnable at any time.
#
#   ./kb-setup.sh
#
# Enables document parsing, embedding, and reranking (the "embed-svc" and
# "kb-worker" containers) as an add-on to an already-running AiNxt install.
# This is deliberately a SEPARATE script from install.sh, not a function
# inside it: the Knowledge Base is its own Docker Compose profile with its
# own containers and no shared secrets with the base app, so turning it on
# later never requires a fresh install or touching the database. It DOES do
# a quick, no-rebuild restart of the gateway container at the end — Docker
# containers never re-read .env after they're created, so a gateway that
# was already running (e.g. because KB was declined during ./install.sh)
# would otherwise never see the new EMBED_SVC_URL/PARSE_SVC_URL this script
# writes, no matter how healthy embed-svc itself is. That restart doesn't
# touch secrets, the database, or ai-ui. Running ./install.sh's "Set up the
# Knowledge Base now?" prompt calls this same script — see there for the
# entry point during a fresh install; this file is what you run again later
# if you said no then, or want to change your model choices.
#
# Self-contained on purpose: this duplicates a small number of helpers from
# install.sh (colors, prompt style, set_env, port picking) rather than
# sourcing it, so this script keeps working correctly run standalone months
# after install.sh, with no dependency on it having been run first (beyond
# the base app already being up, which is what makes .env exist).
#
# One sibling dependency it does have: ./start-embed-service.sh, which
# actually builds/starts embed-svc+kb-worker and waits for healthy —
# extracted out of this script so it can also be run standalone, or by
# ./start-index-worker.sh, without re-answering the questionnaire below.
# Ships in the same repo/commit as this file, same assumption install.sh
# already makes about this file existing next to it.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

# ── Colors / helpers (mirrors install.sh) ────────────────────────────────────
if [[ -e /dev/tty ]]; then TTY=/dev/tty; else TTY=""; fi

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

if [[ -z "$TTY" ]]; then
  die "kb-setup.sh needs an interactive terminal to ask which models to use.
  Run it directly in a terminal (not piped through another command)."
fi

# ── Prerequisites ─────────────────────────────────────────────────────────────
[[ -f .env ]] || die \
"No .env found in $(pwd).
  Run ./install.sh first — it creates .env and starts the base app.
  Knowledge Base setup is an add-on to an already-running install, not a
  replacement for it."

command -v docker >/dev/null 2>&1 || die "Docker is required but was not found."
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  die "Docker Compose was not found. Install Docker Desktop, or the 'docker-compose-plugin' package."
fi

# ── set_env (mirrors install.sh) ─────────────────────────────────────────────
set_env() {
  local k="$1" v="$2"
  if grep -qE "^${k}=" .env; then
    python3 - "$k" "$v" <<'PY'
import re, sys
k, v = sys.argv[1], sys.argv[2]
s = open('.env').read()
s = re.sub(rf'^{re.escape(k)}=.*$', f'{k}={v}', s, flags=re.M)
open('.env','w').write(s)
PY
  else
    printf '%s=%s\n' "$k" "$v" >> .env
  fi
}

# ── Port picking (mirrors install.sh's port_in_use/pick_free_port) ──────────
# choose_ports() in install.sh only ran during the ORIGINAL install — a
# standalone run later has to do its own EMBED_SVC_PORT collision check,
# since nothing else picks a free port for embed-svc otherwise.
PYBIN_ANY="$(command -v python3 || command -v python || echo python3)"
port_in_use() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1 && return 0 || return 1
  fi
  "$PYBIN_ANY" - "$1" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1]))); sys.exit(1)
except OSError:
    sys.exit(0)
finally:
    s.close()
PY
}
pick_free_port() {
  local start="$1" p="$1"
  while port_in_use "$p"; do
    p=$((p+1))
    (( p > start + 200 )) && { echo "$start"; return; }
  done
  echo "$p"
}

EMBED_SVC_PORT="$(grep -E '^EMBED_SVC_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
EMBED_SVC_PORT="${EMBED_SVC_PORT:-8001}"
if port_in_use "$EMBED_SVC_PORT"; then
  new_port="$(pick_free_port "$((EMBED_SVC_PORT+1))")"
  warn "port $EMBED_SVC_PORT is already in use — putting the Knowledge Base on $new_port instead"
  EMBED_SVC_PORT="$new_port"
  set_env EMBED_SVC_PORT "$EMBED_SVC_PORT"
fi

# GATEWAY_PORT was picked by install.sh's choose_ports() when the app was
# first set up — this script runs standalone later and never ran that, so
# it has to read the host-mapped value back out of .env to know where to
# curl the running gateway's own /health for the final confirmation below.
GATEWAY_PORT="$(grep -E '^GATEWAY_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
GATEWAY_PORT="${GATEWAY_PORT:-8000}"

step "Knowledge Base setup"
say "  Adds document parsing, semantic search, and answer reranking."
say "  ${DIM}Runs in its own container (embed-svc) — see docs/KB_SETUP.md.${R}"

kb_embed_provider="ollama"

# -- Embedding model ---------------------------------------------------------
say ""
say "  ${B}Embedding model${R} ${DIM}(turns document text into vectors for search)${R}"
say "    ${B}1)${R} Ollama — nomic-embed-text          ${DIM}local, free, default${R}"
say "    ${B}2)${R} OpenAI — text-embedding-3-small     ${DIM}cloud, needs API key${R}"
say "    ${B}3)${R} Custom OpenAI-compatible endpoint   ${DIM}needs URL + key${R}"
emb_choice=""
printf '  Enter 1-3 [1]: ' > "$TTY"
read -r emb_choice < "$TTY" || emb_choice="1"
emb_choice="${emb_choice:-1}"
case "$emb_choice" in
  2)
    set_env OPENAI_EMBED_MODEL "text-embedding-3-small"
    kb_embed_provider="openai"
    ok "embedding: OpenAI text-embedding-3-small"
    ;;
  3)
    # This is genuinely flexible: services/embed_svc/embedder.py's
    # NomicEmbedder accepts ANY endpoint implementing the OpenAI-compatible
    # POST /v1/embeddings shape — no code change needed for a new provider
    # here, unlike the reranker menu below.
    nomic_url="" nomic_key=""
    printf '  Endpoint URL (OpenAI-compatible /v1/embeddings): ' > "$TTY"
    read -r nomic_url < "$TTY" || nomic_url=""
    printf '  API key (hidden, Enter to skip): ' > "$TTY"
    read -rs nomic_key < "$TTY" || nomic_key=""
    printf '\n' > "$TTY"
    set_env NOMIC_EMBED_URL "$nomic_url"
    set_env NOMIC_EMBED_API_KEY "$nomic_key"
    kb_embed_provider="nomic"
    ok "embedding: custom endpoint (${nomic_url:-none})"
    ;;
  *)
    set_env OLLAMA_EMBED_MODEL "nomic-embed-text"
    kb_embed_provider="ollama"
    ok "embedding: Ollama nomic-embed-text (default)"
    ;;
esac

# This was previously never written — every backend var above (OPENAI_EMBED_MODEL /
# NOMIC_EMBED_URL / OLLAMA_EMBED_MODEL) was configured correctly, but the one
# setting that actually tells the platform WHICH backend to call (core/config.py's
# EMBED_PROVIDER, read by workers/index_worker.py at index time and
# models/hybrid_search.py at query time) was left unset, so it silently defaulted
# to "ollama" no matter which of 1/2/3 was picked above.
set_env EMBED_PROVIDER "$kb_embed_provider"

# -- Reranker model -----------------------------------------------------------
# Unlike embedding, this is NOT free-text-configurable: services/embed_svc/
# reranker.py checks RERANKER_MODEL/RERANKER_VARIANT against a hardcoded
# _VALID_MODELS allow-list and SILENTLY falls back to the default if the
# value isn't recognized (just a log warning nobody sees) — so this menu
# only offers what's actually on that list, and says so plainly if you want
# something else, rather than accepting a name that would quietly not apply.
say ""
say "  ${B}Reranker model${R} ${DIM}(re-scores search results for relevance)${R}"
say "    ${B}1)${R} bge_large   ${DIM}BAAI/bge-reranker-large — best accuracy, ~560MB, default${R}"
say "    ${B}2)${R} bge_base    ${DIM}BAAI/bge-reranker-base — lighter, ~279MB${R}"
say "    ${B}3)${R} tinybert    ${DIM}cross-encoder/ms-marco-TinyBERT-L-2-v2 — smallest/fastest${R}"
say "    ${B}4)${R} Other supported variant   ${DIM}jina_tiny / jina_v2 / qwen_06b / gte_modernbert${R}"
rr_choice=""
printf '  Enter 1-4 [1]: ' > "$TTY"
read -r rr_choice < "$TTY" || rr_choice="1"
rr_choice="${rr_choice:-1}"
_KNOWN_VARIANTS="jina_tiny jina_v2 qwen_06b gte_modernbert bge_large bge_base tinybert bge_v2_m3"
case "$rr_choice" in
  2) set_env RERANKER_VARIANT "bge_base"; ok "reranker: bge_base" ;;
  3) set_env RERANKER_VARIANT "tinybert"; ok "reranker: tinybert" ;;
  4)
    variant=""
    printf '  Variant (jina_tiny/jina_v2/qwen_06b/gte_modernbert): ' > "$TTY"
    read -r variant < "$TTY" || variant="bge_large"
    variant="${variant:-bge_large}"
    if [[ " $_KNOWN_VARIANTS " != *" $variant "* ]]; then
      warn "'$variant' isn't in the supported list — the reranker will silently"
      warn "fall back to bge-reranker-large instead of using it (see the"
      warn "_VALID_MODELS allow-list in services/embed_svc/reranker.py)."
      warn "Adding a new model there is a small code change, not a setup option."
    fi
    set_env RERANKER_VARIANT "$variant"
    ok "reranker: $variant"
    ;;
  *) set_env RERANKER_VARIANT "bge_large"; ok "reranker: bge_large (default)" ;;
esac

# -- Parsing / OCR ------------------------------------------------------------
# No "different parser" option: core/docling_parser.py hardcodes Docling as
# the parsing engine with no swap mechanism in the code today, and PaddleOCR
# only exists as an OCR backend registered INTO Docling's own pipeline (see
# core/paddle_ocr_model.py) — it has no independent existence outside it, so
# it's only asked about when Docling itself is being used.
say ""
say "  ${B}Parsing model${R} ${DIM}(turns documents into structured text)${R}"
say "  ${DIM}Docling is the parsing engine currently wired into the code, with${R}"
say "  ${DIM}PaddleOCR as its optional add-on for scanned/image-based pages.${R}"
say "  ${DIM}These are the only parsing models supported today.${R}"
doc_yn="" ocr_yn="" kb_docling_enabled=0
printf '  Use Docling for parsing? [Y/n]: ' > "$TTY"
read -r doc_yn < "$TTY" || doc_yn="y"
case "$doc_yn" in
  n|N|no|NO)
    set_env USE_DOCLING_PARSER "0"
    warn "As of now, the Knowledge Base's parsing pipeline only supports Docling"
    warn "(optionally with PaddleOCR). Using a different parsing model would"
    warn "require additional code changes to core/docling_parser.py — it isn't"
    warn "a setup-time choice today. Continuing with the legacy per-format"
    warn "parsers (markitdown/pdfplumber/BeautifulSoup) instead — no behavior"
    warn "change from what the app already does without Docling enabled."
    ok "parsing: legacy parsers only"
    ;;
  *)
    set_env USE_DOCLING_PARSER "1"
    ok "parsing: Docling enabled"
    kb_docling_enabled=1
    ;;
esac
WITH_OCR="${WITH_OCR:-0}"
if [[ "$kb_docling_enabled" == "1" ]]; then
  printf '  Also enable PaddleOCR for scanned documents? (heavier download) [y/N]: ' > "$TTY"
  read -r ocr_yn < "$TTY" || ocr_yn="n"
  case "$ocr_yn" in
    y|Y|yes|YES) WITH_OCR=1; export WITH_OCR; ok "OCR: PaddleOCR enabled (in addition to the default rapidocr)" ;;
    *)           ok "OCR: default rapidocr engine only" ;;
  esac
fi

set_env EMBED_SVC_URL     "http://embed-svc:8001"
set_env PARSE_SVC_URL     "http://embed-svc:8001"
set_env PARSE_SVC_ENABLED "1"

# Delegates the actual build/start/model-download/health-wait to
# start-embed-service.sh (see its header) — it re-reads EMBED_PROVIDER/
# USE_DOCLING_PARSER/EMBED_SVC_PORT back out of .env, i.e. exactly the
# values set_env just wrote above, so there's no separate handoff needed.
# chmod explicitly rather than trusting the execute bit survived a git
# checkout/curl download/Windows filesystem; invoked via `bash` (not
# ./start-embed-service.sh) so it runs the same way regardless of whether
# the execute bit actually took (some Windows filesystems mounted into
# WSL2 don't honor chmod reliably) — same reasoning as this script's own
# invocation from install.sh.
chmod 755 ./start-embed-service.sh 2>/dev/null || true
bash ./start-embed-service.sh \
  || die "embed-svc failed to start — the Knowledge Base needs it. Check: ${COMPOSE[*]} logs embed-svc"

# ── Recreate gateway so it actually sees EMBED_SVC_URL/PARSE_SVC_URL ────────
# Docker containers never re-read .env after they're created — a gateway
# that was already running (e.g. because KB was declined during
# ./install.sh) still has these as empty, no matter what was just written
# to .env, until it's recreated. Only gateway needs this; ai-ui never reads
# these vars itself, it only calls the gateway's own API. Confirmed via
# live testing: skipping this step leaves the app reporting the Knowledge
# Base as unreachable even with a perfectly healthy embed-svc.
step "Restarting the gateway so it picks up the new configuration"
say "  ${DIM}Quick restart — no rebuild, no effect on your data or other settings.${R}"
"${COMPOSE[@]}" up -d gateway
ok "gateway restarted"

step "Confirming the app itself can reach the Knowledge Base"
waited=0; limit=120
until curl -fsS -m 5 "http://localhost:${GATEWAY_PORT:-8000}/ainxt/v1/api/health" 2>/dev/null \
    | grep -q '"embed_svc": *"ok"'; do
  (( waited >= limit )) && {
    warn "gateway does not yet report the Knowledge Base as reachable after ${limit}s."
    warn "Check: ${COMPOSE[*]} logs gateway   and   curl http://localhost:${GATEWAY_PORT:-8000}/ainxt/v1/api/health"
    exit 0
  }
  sleep 5; waited=$((waited+5))
done
ok "Knowledge Base fully wired up — the app can reach it (confirmed via gateway's own /health)"
say ""
say "  Open the Knowledge Base in the app — the setup banner should be gone now."
say ""
