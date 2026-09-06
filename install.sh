#!/usr/bin/env bash
#
# AiNxt Enterprise — one-command installer.
#
#   curl -fsSL https://raw.githubusercontent.com/npci/ainxt-enterprise/main/install.sh | bash
#
# Brings up PostgreSQL, Redis, Ollama, the gateway and the web UI, runs the
# database migrations, and prints the URL and first-login credentials.
#
# Non-interactive (CI):  AINXT_PROVIDER=none ./install.sh --yes
set -euo pipefail

REPO_URL="${AINXT_REPO_URL:-https://github.com/npci/ainxt-enterprise.git}"
REPO_DIR="${AINXT_DIR:-ainxt-enterprise}"
ASSUME_YES="no"
MODE="docker"          # docker | local
WITH_OCR="${WITH_OCR:-0}"   # optional PaddleOCR engine — see requirements-ocr.txt
# CodeWiki doc-generation engine (Dockerfile's codewiki_builder stage) — ON by
# default so the CodeWiki panel actually works out of the box instead of
# accepting requests that then fail with "No module named codewiki". Opt out
# with --without-codewiki if you don't want the extra build time/size (its own
# Python 3.12 venv + a git+https install) and don't plan to use the feature.
WITH_CODEWIKI="${WITH_CODEWIKI:-1}"
for arg in "$@"; do
  case "$arg" in
    --yes|-y)    ASSUME_YES="yes" ;;
    --local)     MODE="local" ;;
    --with-ocr)  WITH_OCR=1 ;;
    --with-codewiki)    WITH_CODEWIKI=1 ;;   # kept for backward compat — this is now the default
    --without-codewiki) WITH_CODEWIKI=0 ;;
    --docker)    MODE="docker" ;;
    -h|--help)
      cat <<'USAGE'
AiNxt Enterprise installer

  ./install.sh            everything in Docker (recommended)
  ./install.sh --local    run the API and UI natively on this machine,
                          with only PostgreSQL/Redis/Ollama in Docker
  ./install.sh --with-ocr include the optional PaddleOCR engine (~353 MB).
                          The default rapidocr engine already does CPU OCR for
                          scanned PDFs and images; most installs do not need this.
  ./install.sh --without-codewiki  skip building the CodeWiki documentation
                          engine (on by default — its own Python 3.12 venv +
                          a git+https install, extra build time/size). Use
                          this if you don't plan to use the CodeWiki panel.
                          Note: CodeWiki also REQUIRES its own LLM endpoint —
                          set CODEWIKI_BASE_URL and CODEWIKI_API_KEY
                          separately; it does NOT reuse your chat provider's
                          key from the prompt above. See the printed summary
                          at the end of this install.
  ./install.sh --yes      non-interactive; pair with AINXT_PROVIDER=
  ./install.sh --help     this message

Environment:
  AINXT_PROVIDER   anthropic | openai | gemini | ollama | none
  AINXT_DIR        directory to clone into (default: ainxt-enterprise)
USAGE
      exit 0 ;;
  esac
done

# Prompts must read from the terminal, not from the piped script body.
if [[ -e /dev/tty ]]; then TTY=/dev/tty; else TTY=""; ASSUME_YES="yes"; fi

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

banner() {
  cat <<'EOF'

    ┌─────────────────────────────────────────────────────────┐
    │   AiNxt Enterprise — open-source AI platform            │
    │   Agents · document pipelines · multi-model LLM routing  │
    └─────────────────────────────────────────────────────────┘
EOF
  if [[ "$MODE" == "local" ]]; then
    say "  ${B}Native mode${R} — the API and UI run directly on this machine."
    say ""
  fi
  say "  This installer will:"
  say "    • check Docker is available"
  say "    • ask which AI model provider you want to use"
  say "    • generate strong local secrets for you"
  say "    • start Postgres, Redis, Ollama, the API and the web UI"
  say "    • create the database schema and a first admin login"
  say ""
  say "  ${DIM}Nothing leaves your machine except model API calls, and only if"
  say "  you choose a cloud provider. Expect 5-15 minutes on first run —"
  say "  the container images are large.${R}"
}

# ── 1. Prerequisites ────────────────────────────────────────────────────────
check_prereqs() {
  step "Checking prerequisites"
  command -v docker >/dev/null 2>&1 || die \
"Docker is required but was not found.

  Install Docker Desktop:  https://docs.docker.com/get-docker/
  Then re-run this installer."

  if ! docker info >/dev/null 2>&1; then
    # Distinguish the two very different causes. On Linux "cannot connect" is
    # usually a permissions problem, not a stopped daemon, and telling someone to
    # start a daemon that is already running wastes their time.
    local derr; derr="$(docker info 2>&1 || true)"
    if printf '%s' "$derr" | grep -qiE 'permission denied|dial unix.*permission'; then
      die \
"Docker is running but this user is not allowed to talk to it.

  On Linux, add yourself to the docker group and start a new login shell:

      sudo usermod -aG docker \"\$USER\"
      newgrp docker

  Then re-run this installer. (Running the installer under sudo also works, but
  the files it creates will be owned by root.)"
    fi
    die \
"Docker is installed but the daemon is not reachable.

  macOS / Windows : start Docker Desktop and wait for it to report ready.
  Linux           : sudo systemctl start docker
  Windows          : run this from a WSL2 shell, with Docker Desktop's WSL
                     integration enabled for your distro.

  Then re-run this installer."
  fi
  ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo present)"

  if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    die "Docker Compose was not found. Install Docker Desktop, or the 'docker-compose-plugin' package."
  fi
  ok "docker compose $("${COMPOSE[@]}" version --short 2>/dev/null || echo present)"

  local avail
  avail="$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)"
  if [[ "$avail" =~ ^[0-9]+$ ]] && (( avail > 0 )) && (( avail < 6000000000 )); then
    warn "Docker has $(( avail / 1000000000 ))GB of RAM. 8GB+ is recommended;"
    warn "the platform loads a large ML stack and may be killed below that."
  fi
}

# ── 2. Source ───────────────────────────────────────────────────────────────
fetch_source() {
  if [[ -f docker-compose.yml && -f gateway.py ]]; then
    step "Using the AiNxt checkout in $(pwd)"
    return
  fi
  step "Fetching AiNxt Enterprise"
  command -v git >/dev/null 2>&1 || die "git is required to download AiNxt. Install git and re-run."
  if [[ -d "$REPO_DIR/.git" ]]; then
    ok "$REPO_DIR already exists — reusing it"
  else
    git clone --depth 1 "$REPO_URL" "$REPO_DIR" \
      || die "Could not clone $REPO_URL. Check the URL and your network, or download the repository manually and run ./install.sh inside it."
    ok "cloned into $REPO_DIR"
  fi
  cd "$REPO_DIR"
}

# ── 3. Model provider ───────────────────────────────────────────────────────
PROVIDER=""            # first/primary choice — decides whether llama3.2 is pulled
PROVIDER_KEYS=()       # collected "VAR=value" pairs
choose_provider() {
  step "Choose your AI model provider"

  if [[ -n "${AINXT_PROVIDER:-}" ]]; then
    PROVIDER="$AINXT_PROVIDER"
    ok "using AINXT_PROVIDER=$PROVIDER"
  elif [[ "$ASSUME_YES" == "yes" || -z "$TTY" ]]; then
    PROVIDER="ollama"
    ok "non-interactive — defaulting to local Ollama"
  else
    cat <<EOF

  AiNxt does not bundle a model. Pick where completions should come from.
  ${DIM}These four are the only providers implemented — there is no xAI/Grok,
  Mistral, Cohere or Bedrock gateway in this release.${R}

    ${B}1)${R} Anthropic (Claude)   ${DIM}paid API key · best quality${R}
    ${B}2)${R} OpenAI (GPT)         ${DIM}paid API key${R}
    ${B}3)${R} Google (Gemini)      ${DIM}paid API key${R}
    ${B}4)${R} Ollama — local       ${DIM}free, private, no key; downloads ~2GB${R}
    ${B}5)${R} Decide later         ${DIM}everything starts; add a key when ready${R}

EOF
    local choice=""
    while :; do
      printf '  Enter 1-5 [4]: ' > "$TTY"
      read -r choice < "$TTY" || choice="4"
      choice="${choice:-4}"
      case "$choice" in
        1) PROVIDER="anthropic"; break ;;
        2) PROVIDER="openai";    break ;;
        3) PROVIDER="gemini";    break ;;
        4) PROVIDER="ollama";    break ;;
        5) PROVIDER="none";      break ;;
        *) printf '  %sPlease enter a number from 1 to 5.%s\n' "$YLW" "$R" > "$TTY" ;;
      esac
    done
  fi

  _configure_one_provider "$PROVIDER"

  # More than one provider is normal — a team may hold Anthropic and OpenAI keys
  # and switch per request in the model picker. Keep asking until they are done.
  while [[ "$ASSUME_YES" != "yes" && -n "$TTY" ]]; do
    printf '\n  Add another provider? [y/N]: ' > "$TTY"
    local more=""; read -r more < "$TTY" || more="n"
    case "$more" in
      y|Y|yes|YES) ;;
      *) break ;;
    esac
    say ""
    say "    ${B}1)${R} Anthropic (Claude)   ${B}2)${R} OpenAI (GPT)   ${B}3)${R} Google (Gemini)   ${B}4)${R} Ollama — local"
    printf '  Enter 1-4: ' > "$TTY"
    local c=""; read -r c < "$TTY" || c=""
    case "$c" in
      1) _configure_one_provider anthropic ;;
      2) _configure_one_provider openai ;;
      3) _configure_one_provider ollama_switch_guard; _configure_one_provider gemini ;;
      4) _configure_one_provider ollama ;;
      *) warn "not a valid choice — skipping" ;;
    esac
  done
}

_configure_one_provider() {
  case "$1" in
    anthropic) prompt_key "Anthropic" "ANTHROPIC_API_KEY" "sk-ant-" "https://console.anthropic.com/settings/keys" ;;
    openai)    prompt_key "OpenAI"    "OPENAI_API_KEY"    "sk-"     "https://platform.openai.com/api-keys" ;;
    gemini)    prompt_key "Google AI" "GEMINI_API_KEY"    ""        "https://aistudio.google.com/apikey" ;;
    ollama)    ok "Ollama selected — no API key needed"
               # Under `set -e`, a bare `[[ cond ]] && x` as the LAST command in a
               # function makes the function return the test's exit status — here
               # that's 1 whenever $PROVIDER is already "ollama" (the common case),
               # which killed the whole script silently on return to choose_provider.
               if [[ "$PROVIDER" == "none" ]]; then PROVIDER="ollama"; fi ;;
    none)      warn "No provider configured. Chat will return an error until you add a key to .env." ;;
    ollama_switch_guard) : ;;
    *)         die "Unknown provider '$1'. Use anthropic, openai, gemini, ollama or none." ;;
  esac
}

prompt_key() {
  local label="$1" var="$2" prefix="$3" url="$4"
  local KEY_VALUE=""

  # Reuse a key already exported in the environment.
  local existing="${!var:-}"
  if [[ -n "$existing" ]]; then
    ok "$label key found in your environment ($var) — reusing it"
    PROVIDER_KEYS+=("$var=$existing")
    return
  fi
  if [[ -z "$TTY" ]]; then
    warn "$var not set and no terminal to prompt on — continuing without it"
    return
  fi

  say ""
  say "  Get a $label key here: ${CYN}${url}${R}"
  say "  ${DIM}It is written only to .env in this directory, which is gitignored."
  say "  Press Enter to skip and add it later.${R}"
  while :; do
    printf '  Paste your %s key (hidden): ' "$label" > "$TTY"
    read -rs KEY_VALUE < "$TTY" || KEY_VALUE=""
    printf '\n' > "$TTY"
    [[ -z "$KEY_VALUE" ]] && { warn "skipped — chat will not work until $var is set in .env"; return; }
    if [[ -n "$prefix" && "$KEY_VALUE" != "$prefix"* ]]; then
      printf '  %sThat does not look like a %s key (expected it to start with %s). Try again, or press Enter to skip.%s\n' \
        "$YLW" "$label" "$prefix" "$R" > "$TTY"
      continue
    fi
    ok "$label key captured (${#KEY_VALUE} characters)"
    PROVIDER_KEYS+=("$var=$KEY_VALUE")
    return
  done
}

# ── 4. Configuration ────────────────────────────────────────────────────────
gen_secret() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 24
  else head -c 48 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 48; fi
}

set_env() {
  local k="$1" v="$2"
  [[ -f .env ]] || return 0
  if grep -qE "^${k}=" .env; then
    python3 - "$k" "$v" <<'PY'
import re, sys
k, v = sys.argv[1], sys.argv[2]
s = open('.env', encoding='utf-8').read()
# every occurrence, not just the first: a key duplicated later in the
# file would be re-asserted and silently win when the file is sourced.
s = re.sub(rf'^{re.escape(k)}=.*$', f'{k}={v}', s, flags=re.M)
open('.env','w', encoding='utf-8').write(s)
PY
  else
    printf '%s=%s\n' "$k" "$v" >> .env
  fi
}

write_env() {
  step "Writing configuration"
  if [[ -f .env ]]; then
    cp .env ".env.backup.$(date +%s)"
    ok "existing .env backed up"
  else
    cp .env.example .env
    ok ".env created from .env.example"
  fi

  set_env POSTGRES_PASSWORD  "$(gen_secret)"
  set_env JWT_SECRET         "$(gen_secret)"
  set_env SECRET_KEY         "$(gen_secret)"
  # AUDIT_SIGNING_KEY signs the audit log. .env.example ships it as the literal
  # "change-me-in-production". Nothing used to reject that value — not even prod
  # validation, which only checked that the variable was non-empty — so an
  # install that copied the template signed its tamper-evident audit log with a
  # key published in the repository. core/audit_signer.py now refuses to import
  # with a template or short key; generating it here means the one-command path
  # never hits that.
  set_env AUDIT_SIGNING_KEY  "$(gen_secret)"
  ok "generated database password and signing secrets (incl. audit signing key)"

  # FERNET_KEY encrypts store/credential_vault.py (API keys for LLM providers,
  # connectors, etc.) — .env.example ships it blank. Unlike the secrets above,
  # this one must NOT be regenerated on a re-run: rotating it makes every
  # already-stored encrypted credential permanently undecryptable. Only set it
  # when genuinely absent.
  if ! grep -qE '^FERNET_KEY=.+' .env 2>/dev/null; then
    set_env FERNET_KEY "$(python3 -c 'import os, base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
    ok "generated FERNET_KEY for the credential vault"
  fi

  # The scanner sidecar is not part of this repo; with this on and no URL set the
  # gateway refuses to start.
  set_env ENABLE_INJECTION_SCAN false

  local pair
  for pair in "${PROVIDER_KEYS[@]+"${PROVIDER_KEYS[@]}"}"; do
    set_env "${pair%%=*}" "${pair#*=}"
    ok "${pair%%=*} written to .env"
  done
  chmod 600 .env 2>/dev/null || true
}

# ── LLM provider config visibility (admin "LLM Providers" screen) ──────────
# Whatever provider(s) this installer configures should show up — and stay
# further editable — in the admin UI, not just sit in .env. Reads the model
# name from config/ollama_model_suggestions.json's first entry rather than a
# literal in this script, so there is no hardcoded model name here either.
default_ollama_model() {
  python3 -c "
import json
try:
    print(json.load(open('config/ollama_model_suggestions.json'))['suggestions'][0]['name'])
except Exception:
    print('llama3.2')
" 2>/dev/null || echo "llama3.2"
}

_provider_family_for_var() {
  case "$1" in
    ANTHROPIC_API_KEY) echo "anthropic" ;;
    OPENAI_API_KEY)    echo "openai" ;;
    GEMINI_API_KEY)    echo "gemini" ;;
    *)                 echo "" ;;
  esac
}

# Slug/name MUST match db/migrate.py's _AC1_PROVIDER_SPECS exactly ({family}-env,
# "X (from .env)") — both this script and Part AC1 seed from the SAME env vars,
# and both only check "does a row with THIS slug exist" before inserting. If the
# two paths disagreed on the slug, both would insert, producing a duplicate
# provider for the same key (fixed 2026-09-01 — previously this used
# "{family}-default"/"X (from install)", a different scheme from Part AC1's).
_provider_display_name_for_family() {
  case "$1" in
    anthropic) echo "Anthropic (from .env)" ;;
    openai)    echo "OpenAI (from .env)" ;;
    gemini)    echo "Gemini (from .env)" ;;
    ollama)    echo "Ollama (from .env)" ;;
  esac
}

# Registers every cloud provider key collected in PROVIDER_KEYS via
# db/bootstrap_llm_providers.py. $1.. is how to invoke python for the running
# mode: `docker exec ainxt-gateway python` (docker) or `./.venv/bin/python`
# (local) — the script reads the actual key from that process's environment,
# never from a CLI argument, so a plaintext key never appears in `ps` output.
seed_provider_db() {
  local runner=("$@")
  local pair var family display
  for pair in "${PROVIDER_KEYS[@]+"${PROVIDER_KEYS[@]}"}"; do
    var="${pair%%=*}"
    family="$(_provider_family_for_var "$var")"
    [[ -z "$family" ]] && continue
    display="$(_provider_display_name_for_family "$family")"
    "${runner[@]}" db/bootstrap_llm_providers.py \
      --family "$family" --slug "${family}-env" \
      --name "$display" --key-env-var "$var" \
      && ok "registered $family in the LLM Providers admin screen" \
      || warn "could not register $family in the admin screen — add it manually under LLM Providers"
  done
}

# Registers the bundled/host Ollama + the model just pulled, so it appears
# immediately in the admin screen. $1.. is the python runner (see above);
# LOCAL_LLM_BASE_URL is already set (in .env for docker, exported for local)
# by the time this is called.
seed_ollama_model_db() {
  local runner=("$@")
  local model
  model="$(default_ollama_model)"
  "${runner[@]}" db/bootstrap_llm_providers.py \
    --family ollama --slug ollama-env --name "$(_provider_display_name_for_family ollama)" \
    --base-url-env-var LOCAL_LLM_BASE_URL --seed-model "$model" \
    && ok "registered Ollama + $model in the LLM Providers admin screen" \
    || warn "could not register Ollama in the admin screen — add it manually under LLM Providers"
}

# Always checks Ollama's real, fixed default port (11434) — NEVER
# ${OLLAMA_PORT:-11434}. OLLAMA_PORT is the HOST port choose_ports() maps the
# BUNDLED Docker Ollama container to, and it gets reassigned away from 11434
# whenever something is already listening there — which, on a machine with a
# native/host-installed Ollama, is exactly the host Ollama this function
# exists to detect. Checking the (by then reassigned) OLLAMA_PORT here meant
# this function could never re-detect it after choose_ports() ran, so the
# installer silently fell back to the empty bundled container — every model
# that only existed in the real host Ollama then failed with "no gateway
# available". The bundled container's host-side port is irrelevant to this
# check: it's a completely different port mapping question (see choose_ports).
host_ollama_usable() {
  if curl -fsS -m 3 "http://127.0.0.1:11434/api/tags" >/dev/null 2>&1; then
    return 0
  fi
  if command -v ollama >/dev/null 2>&1; then
    if ollama list >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

# .env.example points OLLAMA_URL / LOCAL_LLM_BASE_URL at localhost, which is right
# for a native run and wrong inside a container — there, localhost is the gateway
# itself. compose's ${OLLAMA_URL:-http://ollama:11434} default never applies
# because the variable IS set in .env, so the local model tier came up with
# "ollama: error: [Errno 111] Connection refused". Rewrite them to the service
# name for Docker mode; native mode overrides them with 127.0.0.1 at run time in
# export_env_for_host(), so writing service names here is safe for both.
use_container_service_names() {
  step "Pointing the gateway at the bundled services"

  if host_ollama_usable; then
    # This function only runs for DOCKER mode (see the MODE branch at the
    # bottom of this script) — 127.0.0.1 inside the gateway container refers
    # to the container itself, never the host, on any platform. Docker
    # Desktop (Mac/Windows) resolves host.docker.internal automatically;
    # docker-compose.yml's `extra_hosts: host.docker.internal:host-gateway`
    # on the gateway service makes it resolve on Linux too.
    set_env OLLAMA_URL         "http://host.docker.internal:${OLLAMA_PORT:-11434}"
    set_env LOCAL_LLM_BASE_URL "http://host.docker.internal:${OLLAMA_PORT:-11434}"
    ok "gateway will use the host-installed Ollama at http://host.docker.internal:${OLLAMA_PORT:-11434}"
  else
    set_env OLLAMA_URL         "http://ollama:11434"
    set_env LOCAL_LLM_BASE_URL "http://ollama:11434"
    set_env POSTGRES_HOST      "postgres"
    set_env PGVECTOR_HOST      "postgres"
    set_env REDIS_HOST         "redis"
    set_env REDIS_URL          "redis://redis:6379/0"
    ok "gateway will reach postgres/redis/ollama by service name"
  fi
  # Kafka is required, not optional: the gateway publishes chat history, audit
  # entries and usage rows rather than writing them, and the kafka-consumer
  # service performs the INSERT. Left at the template's old KAFKA_ENABLED=false
  # those events queue into Redis kafka:fallback:* under a 7-day TTL and the
  # rows never appear. kafka:19092 is the broker's INTERNAL listener — the
  # published port advertises "localhost", which is wrong from inside a container.
  set_env KAFKA_ENABLED      "true"
  set_env KAFKA_BOOTSTRAP    "kafka:19092"
}

# ── 5. Start ────────────────────────────────────────────────────────────────
start_stack() {
  step "Building and starting the stack"
  export WITH_OCR
  if [[ "$WITH_OCR" == "1" ]]; then
    say "  ${DIM}Including the optional PaddleOCR engine (--with-ocr).${R}"
  fi
  export WITH_CODEWIKI
  # codewiki-worker is behind docker-compose.yml's `codewiki` profile — a
  # bare "up -d <list>" never starts a profiled service unless --profile is
  # also passed, regardless of whether it's named in the list. Building the
  # invocation prefix as an array that ALWAYS has at least "${COMPOSE[@]} up
  # -d --build" in it (never a separately-expanded, possibly-empty array)
  # matters on macOS's stock bash 3.2, where "${empty_array[@]}" under
  # `set -u` throws "unbound variable".
  local -a up_cmd=("${COMPOSE[@]}" up -d --build)
  if [[ "$WITH_CODEWIKI" == "1" ]]; then
    say "  ${DIM}Including the CodeWiki documentation engine (on by default — use --without-codewiki to skip).${R}"
    up_cmd=("${COMPOSE[@]}" --profile codewiki up -d --build)
  else
    say "  ${DIM}Skipping the CodeWiki documentation engine (--without-codewiki) — the panel will accept requests but generation will fail until rebuilt with it.${R}"
  fi

  # kafka-consumer and doc-worker must be listed explicitly. `docker compose up
  # -d <list>` starts only the named services and their DEPENDENCIES, and
  # nothing depends on either of them — the arrow points the other way (both
  # depend on gateway, so its build produces their shared image first). Omit
  # kafka-consumer and the broker comes up, the gateway publishes, and nothing
  # ever writes a chat-history, audit or model_usages row to Postgres. Omit
  # doc-worker and document-generation jobs enqueue but nothing ever runs them.
  local compose_services=(postgres redis kafka gateway kafka-consumer doc-worker ai-ui)
  if host_ollama_usable; then
    say "  ${DIM}Host Ollama is reachable — using it instead of the bundled Docker Ollama service.${R}"
  else
    compose_services+=(ollama)
  fi

  # codewiki-worker only added to the list (and only started) when
  # WITH_CODEWIKI=1 — declining with --without-codewiki means the image was
  # built without the `codewiki` CLI, so the container would have nothing to
  # do but fail every job (see docker-compose.yml's comment on this service).
  if [[ "$WITH_CODEWIKI" == "1" ]]; then
    compose_services+=(codewiki-worker)
  fi

  # SDLC's sdlc-worker and the Knowledge Base's embed-svc/kb-worker are NOT
  # started here, deliberately: they need artifacts/config that
  # setup_sdlc_cli()/setup_knowledge_base() fetch/generate AFTER the base
  # stack is healthy, not before. index-worker is likewise deliberately never
  # started here — see finish() and ./start-index-worker.sh.

  say "  ${DIM}First run pulls and builds several GB of images. Grab a coffee.${R}"
  "${up_cmd[@]}" "${compose_services[@]}" \
    || die "Startup failed. Show the logs with:  ${COMPOSE[*]} logs --tail=80"
  ok "containers started"

  step "Waiting for the platform to become healthy"
  local waited=0 limit=900
  until curl -fsS -m 5 "http://localhost:${GATEWAY_PORT:-8000}/ainxt/v1/api/health" >/dev/null 2>&1; do
    if ! docker ps --format '{{.Names}}' | grep -q '^ainxt-gateway$'; then
      say ""
      "${COMPOSE[@]}" logs --tail=40 gateway migrate 2>/dev/null || true
      die "The gateway stopped. The log above should say why."
    fi
    (( waited >= limit )) && die "Gave up after ${limit}s. Check:  ${COMPOSE[*]} logs gateway"
    sleep 5; waited=$((waited+5))
    (( waited % 60 == 0 )) && say "  ${DIM}still starting… ${waited}s (the ML stack takes a while)${R}"
  done
  ok "gateway healthy after ${waited}s"

  # Capture the one-time admin password now, while it's still in THIS gateway
  # container's logs. setup_knowledge_base() (if the user opts in) hands off to
  # kb-setup.sh, which does `docker compose up -d gateway` to pick up the new
  # EMBED_SVC_URL/PARSE_SVC_URL — that recreates the container, so its previous
  # logs (and the "Password:" line seeded_admin_pw only ever prints once) are
  # gone by the time finish() would otherwise grep for them.
  ADMIN_PW="$("${COMPOSE[@]}" logs gateway 2>/dev/null | grep -m1 'Password *:' | sed 's/.*: *//' | tr -d '\r' || true)"

  step "Registering providers in the admin screen"
  seed_provider_db docker exec ainxt-gateway python

  if [[ "$PROVIDER" == "ollama" ]]; then
    if host_ollama_usable; then
      ok "host Ollama is already available; no bundled Ollama pull is required"
      seed_ollama_model_db docker exec ainxt-gateway python
    else
      local model; model="$(default_ollama_model)"
      step "Downloading the local model ($model, ~2GB)"
      docker exec ainxt-ollama ollama pull "$model" >/dev/null 2>&1 \
        && { ok "$model ready"; seed_ollama_model_db docker exec ainxt-gateway python; } \
        || warn "pull failed — run 'docker exec ainxt-ollama ollama pull $model' later"
    fi
  fi
}

# ── 6. Done ─────────────────────────────────────────────────────────────────
DOCTOR_RESULT="skipped"
# Set by start_stack() right after the gateway first becomes healthy — see the
# comment there for why finish() cannot just re-grep the logs itself.
ADMIN_PW=""

# Run the post-install checks and let their result decide what we claim.
#
# Until now this installer printed "AiNxt is running" unconditionally — it had
# started containers and waited for /health, and inferred the rest. That is the
# same optimism that let a broken login and a silently half-applied migration
# ship: the process was up, so the install was declared good. doctor.sh asserts
# on artifacts instead, and its exit status now gates the wording below.
verify_install() {
  if [[ ! -x ./doctor.sh ]]; then
    warn "doctor.sh not found — skipping the post-install checks"
    return 0
  fi
  step "Checking the install"
  if ./doctor.sh; then
    DOCTOR_RESULT="ok"
  else
    DOCTOR_RESULT="failed"
  fi
  return 0   # never abort the installer on a failed check; report instead
}

finish() {
  # Prefer the password captured right after the gateway's first boot
  # (start_stack) — a live re-grep here would come up empty whenever
  # setup_knowledge_base() ran kb-setup.sh in between, since that recreates
  # the gateway container and its earlier logs go with it. Fall back to a
  # live grep only if that capture never happened (e.g. this function is
  # reached some other way with ADMIN_PW unset).
  local pw="$ADMIN_PW"
  if [[ -z "$pw" ]]; then
    pw="$("${COMPOSE[@]}" logs gateway 2>/dev/null | grep -m1 'Password *:' | sed 's/.*: *//' | tr -d '\r' || true)"
  fi
  # Read fresh from .env, not the in-process $EMBED_SVC_PORT — kb-setup.sh
  # runs as a child process and could reassign this again if it changed
  # since choose_ports() ran, which the parent shell would never see.
  local kb_port; kb_port="$(grep -E '^EMBED_SVC_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  kb_port="${kb_port:-${EMBED_SVC_PORT:-8001}}"

  if [[ "$DOCTOR_RESULT" == "failed" ]]; then
    printf '\n%s' "$YLW"
    cat <<'EOF'
    ┌─────────────────────────────────────────────────────────┐
    │   AiNxt started, but some checks failed                  │
    │   Fix the ✗ lines above, then re-run ./doctor.sh         │
    └─────────────────────────────────────────────────────────┘
EOF
  else
    printf '\n%s' "$GRN"
    cat <<'EOF'
    ┌─────────────────────────────────────────────────────────┐
    │   AiNxt is running                                      │
    └─────────────────────────────────────────────────────────┘
EOF
  fi
  printf '%s' "$R"
  say ""
  say "    Open        ${B}${CYN}http://localhost:${UI_PORT:-5173}/portal/${R}"
  say "    API docs    ${DIM}http://localhost:${GATEWAY_PORT:-8000}/docs${R}"
  say ""
  say "    Sign in with"
  say "      email     ${B}admin@ainxt.local${R}"
  if [[ -n "$pw" ]]; then
    say "      password  ${B}${pw}${R}"
    say "                ${DIM}shown once — save it now, then change it in Profile → Security${R}"
  else
    say "      password  ${DIM}see: ${COMPOSE[*]} logs gateway | grep -A3 'admin user created'${R}"
  fi
  say ""
  say "    ${DIM}re-check  ./doctor.sh"
  say "    stop      ${COMPOSE[*]} down"
  say "    logs      ${COMPOSE[*]} logs -f gateway"
  say "    events    ${COMPOSE[*]} logs -f kafka-consumer   ${DIM}(chat history / audit writer)${R}"
  say "    reset      ${COMPOSE[*]} down -v   (deletes all data)${R}"
  if [[ "$PROVIDER" == "none" ]]; then
    say ""
    warn "No model provider set. Add a key to .env, then: ${COMPOSE[*]} up -d gateway"
  fi
  if [[ "$KB_ENABLED" == "1" ]]; then
    say ""
    say "    Knowledge Base   ${GRN}on${R}  ${DIM}(embed-svc :${kb_port} — see docs/KB_SETUP.md)${R}"
  fi
  if [[ "$SDLC_ENABLED" == "1" ]]; then
    say ""
    say "    SDLC CLI engine  ${GRN}on${R}  ${DIM}(sdlc-worker — reconfigure with ./sdlc-setup.sh)${R}"
  fi
  if [[ "$WITH_CODEWIKI" == "1" ]]; then
    say ""
    say "    ${B}CodeWiki${R} REQUIRES its own LLM endpoint — it does NOT reuse the"
    say "    ${B}${PROVIDER}${R} key you set above. Generation fails until you set in .env:"
    say "      ${B}CODEWIKI_BASE_URL${R}   an OpenAI-compatible /v1 endpoint (required)"
    say "      ${B}CODEWIKI_API_KEY${R}    its API key (required)"
    say "      ${B}CODEWIKI_PROVIDER${R}   optional — leave unset for a plain OpenAI-compatible"
    say "                          endpoint (the CLI's own default); see .env.example"
    say "    ${DIM}Then: ${COMPOSE[*]} up -d gateway${R}   ${DIM}(picks up the new .env values)${R}"
  fi
  say ""
  # index-worker (codebase AST chunking + embedding for semantic code
  # search / SDLC pipeline context) is deliberately never started above —
  # it's an opt-in step independent of CodeWiki/Knowledge Base, run whenever
  # you actually want to index a repository. start-index-worker.sh brings up
  # embed-svc first (index-worker needs it reachable) if it isn't already.
  say "    For codebase indexing / semantic code search, run:"
  say "      ${B}bash ./start-index-worker.sh${R}"
  say "    ${DIM}(also starts embed-svc if it isn't already running)${R}"
  say ""
}


# ── Port selection ──────────────────────────────────────────────────────────
# Published ports collide with anything already listening — a developer machine
# very often already runs PostgreSQL on 5432, Redis on 6379 or Ollama on 11434.
# In Docker mode the collision stops the container from starting; in native mode
# it is worse, because the backend silently connects to the WRONG database
# (localhost resolves to ::1 and reaches the pre-existing server, which fails
# later with something unhelpful like `role "postgres" does not exist`).
port_in_use() {
  : "${PYBIN_ANY:=$(command -v python3 || command -v python || echo python3)}"
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

choose_ports() {
  PYBIN_ANY="$(command -v python3 || command -v python || echo python3)"
  step "Choosing ports"
  local changed=0
  local port_specs=("POSTGRES_PORT:5432:PostgreSQL" "REDIS_PORT:6379:Redis" "KAFKA_PORT:9092:Kafka" "GATEWAY_PORT:8000:the API" "UI_PORT:5173:the web UI" "EMBED_SVC_PORT:8001:the Knowledge Base service")
  # Ollama is special-cased: if a host-installed Ollama is already answering
  # on its real default port (11434), that IS the "port already in use" this
  # loop would otherwise detect — but it's the instance we're about to use
  # instead of the bundled Docker container (see host_ollama_usable /
  # start_stack), not a conflict to route around. Reassigning OLLAMA_PORT
  # here would just relabel which (irrelevant) host port the never-started
  # bundled container isn't listening on, while breaking nothing else — EXCEPT
  # this exact port also used to leak into host_ollama_usable() before its own
  # fix, so keep them consistent: OLLAMA_PORT stays 11434 whenever the host
  # Ollama is what we're going to use.
  if host_ollama_usable; then
    ok "host Ollama already answering on 11434 — using it (no port reassignment needed)"
    export OLLAMA_PORT=11434
  else
    port_specs+=("OLLAMA_PORT:11434:Ollama")
  fi
  # Track ports already claimed earlier in THIS loop, not just OS-level
  # binding — port_in_use() only sees sockets bound at check-time, so two
  # defaults reassigned back-to-back (e.g. GATEWAY_PORT bumped 8000->8001,
  # then EMBED_SVC_PORT's own default of 8001 checked next) could both pick
  # the same still-free-at-the-time port and collide later when containers
  # actually start.
  local claimed=" "
  for spec in "${port_specs[@]}"; do
    local var="${spec%%:*}" rest="${spec#*:}"
    local def="${rest%%:*}" label="${rest#*:}"
    local cur="${!var:-$def}"
    while port_in_use "$cur" || [[ "$claimed" == *" $cur "* ]]; do
      local free; free="$(pick_free_port "$((cur+1))")"
      warn "port $cur is already in use — putting $label on $free instead"
      set_env "$var" "$free"
      export "$var=$free"
      cur="$free"
      changed=1
    done
    claimed="$claimed$cur "
    export "$var=$cur"
  done
  # The bundled PostgreSQL serves both the main database and pgvector, so the
  # vector connection has to follow POSTGRES_PORT. .env.example pins
  # PGVECTOR_PORT=5432 independently, which otherwise leaves PGS02 pointing at
  # whatever is on 5432 — on a developer machine, usually their own PostgreSQL.
  set_env PGVECTOR_PORT "${POSTGRES_PORT}"
  export PGVECTOR_PORT="${POSTGRES_PORT}"
  (( changed )) || ok "default ports are free"
}

# ── Native mode ─────────────────────────────────────────────────────────────
PYBIN=""
check_prereqs_local() {
  step "Checking prerequisites for native mode"
  for cand in python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)' 2>/dev/null; then
        PYBIN="$cand"; break
      fi
    fi
  done
  [[ -n "$PYBIN" ]] || die \
"Python 3.10 or newer is required for native mode.

  macOS:  brew install python@3.12
  Ubuntu: sudo apt install python3.12 python3.12-venv

  Or run everything in Docker instead:  ./install.sh"
  ok "$("$PYBIN" --version)"

  command -v node >/dev/null 2>&1 || die \
"Node.js 18 or newer is required for the web UI.

  Install from https://nodejs.org/ or via nvm, then re-run.
  Or run everything in Docker instead:  ./install.sh"
  local nodemajor
  nodemajor="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  (( nodemajor >= 18 )) || die "Node.js 18+ is required (found $(node -v))."
  ok "node $(node -v)"
  command -v npm >/dev/null 2>&1 || die "npm was not found alongside Node.js."
}

start_infra_only() {
  step "Starting PostgreSQL, Redis, Ollama and Kafka in Docker"
  say "  ${DIM}Only the datastores run in containers; the API and UI run on this machine.${R}"
  "${COMPOSE[@]}" up -d postgres redis ollama kafka \
    || die "Could not start the datastores. Check:  ${COMPOSE[*]} logs postgres redis kafka"
  local waited=0
  until [[ "$(docker inspect -f '{{.State.Health.Status}}' ainxt-postgres 2>/dev/null)" == "healthy" \
        && "$(docker inspect -f '{{.State.Health.Status}}' ainxt-redis 2>/dev/null)" == "healthy" \
        && "$(docker inspect -f '{{.State.Health.Status}}' ainxt-kafka 2>/dev/null)" == "healthy" ]]; do
    (( waited >= 180 )) && die "PostgreSQL/Redis/Kafka did not become healthy in 180s."
    sleep 3; waited=$((waited+3))
  done
  ok "postgres, redis and kafka healthy"
}

# Document generation (Word/PPT/Excel/PDF export) always shells out to
# `docker run ainxt-doc-sandbox:latest`, in BOTH --docker and --local mode —
# it's the one piece that's Docker-based even in native mode. Skipping this
# used to mean every document-generation attempt failed instantly with
# "Document sandbox image 'ainxt-doc-sandbox:latest' not built", and the
# self-repair loop would burn several minutes of LLM retries against that
# unfixable error before finally giving up — so build it here, once, as part
# of install rather than leaving it as a manual post-install step.
build_doc_sandbox_image() {
  step "Building the document-generation sandbox image"
  if [[ ! -f docker/doc-sandbox/build.sh ]]; then
    warn "docker/doc-sandbox/build.sh not found — skipping (document generation will be unavailable)"
    return 0
  fi
  # The script is executed directly (not sourced), so it needs the execute
  # bit; a git checkout / tarball extraction does not always preserve it.
  chmod 755 docker/doc-sandbox/build.sh

  if docker image inspect ainxt-doc-sandbox:latest >/dev/null 2>&1; then
    ok "ainxt-doc-sandbox:latest already built"
    return 0
  fi
  say "  ${DIM}Pulls LibreOffice + fonts — first build takes a few minutes.${R}"
  if ./docker/doc-sandbox/build.sh; then
    ok "ainxt-doc-sandbox:latest built"
  else
    warn "document-sandbox build failed — document generation (Word/PPT/Excel/PDF) will be unavailable until you run:  bash docker/doc-sandbox/build.sh"
  fi
}

install_python_deps() {
  step "Creating the Python virtual environment"
  [[ -d .venv ]] || "$PYBIN" -m venv .venv
  ok ".venv ready"
  say "  ${DIM}Installing dependencies — a few minutes, and several GB with the ML stack.${R}"
  ./.venv/bin/python -m pip install --quiet --upgrade pip
  ./.venv/bin/python -m pip install --quiet -r requirements.txt \
    || die "pip install failed. Re-run it without --quiet to see why:
    ./.venv/bin/python -m pip install -r requirements.txt"
  ok "python dependencies installed"
  if [[ "$WITH_OCR" == "1" ]]; then
    ./.venv/bin/python -m pip install --quiet -r requirements-ocr.txt \
      && ok "PaddleOCR installed (--with-ocr)" \
      || warn "PaddleOCR install failed — the default rapidocr engine still works"
  fi
}

# The datastores run in containers with published ports, so from the host they
# are on localhost. gunicorn.conf.py does NOT read .env, so everything the
# backend needs has to be exported into the environment first.
export_env_for_host() {
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
  # 127.0.0.1, not localhost: localhost can resolve to ::1 and reach a
  # pre-existing PostgreSQL on the host instead of the container.
  export POSTGRES_HOST="127.0.0.1"
  export POSTGRES_PORT="${POSTGRES_PORT:-5432}"
  export PGVECTOR_HOST="127.0.0.1"
  # Same server as the main database — follow POSTGRES_PORT, never .env's
  # independently-pinned value.
  export PGVECTOR_PORT="${POSTGRES_PORT:-5432}"
  export REDIS_HOST="127.0.0.1"
  export REDIS_URL="redis://127.0.0.1:${REDIS_PORT:-6379}/0"
  export OLLAMA_URL="http://127.0.0.1:${OLLAMA_PORT:-11434}"
  export LOCAL_LLM_BASE_URL="${LOCAL_LLM_BASE_URL:-$OLLAMA_URL}"
  # The broker's EXTERNAL listener is the one published to the host; the
  # INTERNAL kafka:19092 address .env carries for Docker mode is unreachable
  # from here. gunicorn.conf.py does not read .env, so this must be exported.
  export KAFKA_ENABLED="true"
  export KAFKA_BOOTSTRAP="127.0.0.1:${KAFKA_PORT:-9092}"
  # Persist it too, so .env matches what the process actually uses — doctor.sh
  # and workers/kafka_consumer.py both read .env and would otherwise be pointed
  # at the container-only kafka:19092 the template ships.
  set_env KAFKA_ENABLED   "true"
  set_env KAFKA_BOOTSTRAP "127.0.0.1:${KAFKA_PORT:-9092}"
  export ENABLE_INJECTION_SCAN="${ENABLE_INJECTION_SCAN:-false}"
  export WORKERS="${WORKERS:-2}"
  export APP_ENV="${APP_ENV:-development}"
}

# .env.example points file storage at /var/lib/ainxt/*, which only works when
# something root-owned created it — true in the container image, false for a
# native run, where the API died at startup with
#   [Errno 13] Permission denied: '/var/lib/ainxt'
# Keep native storage inside the checkout, where the user certainly can write.
use_local_storage_dirs() {
  step "Configuring file storage"
  local base="$PWD/data"
  mkdir -p "$base/docs" "$base/images" "$base/uploads/documents"
  set_env AINXT_DOC_STORAGE_DIR      "$base/docs"
  set_env AINXT_IMAGE_STORAGE_DIR    "$base/images"
  set_env AINXT_UPLOAD_DOCUMENT_PATH "$base/uploads/documents"
  export AINXT_DOC_STORAGE_DIR="$base/docs"
  export AINXT_IMAGE_STORAGE_DIR="$base/images"
  export AINXT_UPLOAD_DOCUMENT_PATH="$base/uploads/documents"
  ok "storage under ./data (the /var/lib/ainxt default needs root)"
}

run_migrations_local() {
  step "Creating the database schema"
  ./.venv/bin/python db/migrate.py 2>&1 | tail -5
  local rc="${PIPESTATUS[0]}"
  (( rc == 0 )) || die "Migrations failed (exit $rc). Re-run to see the full output:
    ./.venv/bin/python db/migrate.py"
  ok "schema ready"
}

start_backend_local() {
  step "Starting the API"
  mkdir -p log
  ./.venv/bin/gunicorn gateway:app -c gunicorn.conf.py > log/gateway.out 2>&1 &
  echo $! > .ainxt-gateway.pid
  local waited=0
  until curl -fsS -m 5 "http://localhost:${GATEWAY_PORT:-8000}/ainxt/v1/api/health" >/dev/null 2>&1; do
    if ! kill -0 "$(cat .ainxt-gateway.pid)" 2>/dev/null; then
      say ""; tail -30 log/gateway.out; die "The API stopped during startup — see above and log/gateway.out"
    fi
    (( waited >= 600 )) && die "API did not become healthy in 600s. See log/gateway.out"
    sleep 5; waited=$((waited+5))
    (( waited % 60 == 0 )) && say "  ${DIM}still starting… ${waited}s${R}"
  done
  ok "API healthy on :${GATEWAY_PORT:-8000} (pid $(cat .ainxt-gateway.pid))"
}

start_kafka_consumer_local() {
  step "Starting the Kafka consumer"
  # The event -> Postgres writer. In Docker this is the kafka-consumer service;
  # natively it has to be launched as its own process, because nothing else
  # drains the topics. Without it the API still answers and the UI still renders,
  # but no chat turn, audit entry or usage row is ever persisted — the failure is
  # invisible until someone reloads a conversation and finds it empty.
  mkdir -p log
  ./.venv/bin/python workers/kafka_consumer.py > log/kafka-consumer.out 2>&1 &
  echo $! > .ainxt-kafka-consumer.pid
  sleep 3
  if kill -0 "$(cat .ainxt-kafka-consumer.pid)" 2>/dev/null; then
    ok "kafka consumer running (pid $(cat .ainxt-kafka-consumer.pid))"
  else
    warn "the consumer exited immediately — see log/kafka-consumer.out"
    warn "chat history and audit rows will not be written until it stays up"
  fi
}

start_ui_local() {
  step "Starting the web UI"
  ( cd ai-ui && npm install --no-audit --no-fund >/dev/null 2>&1 ) \
    || die "npm install failed in ai-ui. Run it there directly to see why."
  ok "ui dependencies installed"
  ( cd ai-ui && exec npm run dev ) > log/ai-ui.out 2>&1 &
  echo $! > .ainxt-ui.pid
  local waited=0
  until curl -fsS -m 5 "http://localhost:${UI_PORT:-5173}/" >/dev/null 2>&1; do
    (( waited >= 120 )) && die "UI did not start in 120s. See log/ai-ui.out"
    sleep 3; waited=$((waited+3))
  done
  ok "UI on :${UI_PORT:-5173} (pid $(cat .ainxt-ui.pid))"
}

finish_local() {
  local pw
  pw="$(grep -m1 'Password *:' log/gateway.out 2>/dev/null | sed 's/.*: *//' | tr -d '\r' || true)"
  local kb_port; kb_port="$(grep -E '^EMBED_SVC_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  kb_port="${kb_port:-${EMBED_SVC_PORT:-8001}}"
  printf '\n%s' "$GRN"
  cat <<'EOF'
    ┌─────────────────────────────────────────────────────────┐
    │   AiNxt is running (native mode)                        │
    └─────────────────────────────────────────────────────────┘
EOF
  if [[ "$DOCTOR_RESULT" == "failed" ]]; then
    printf '  %s! some post-install checks failed — see the ✗ lines above%s\n' "$YLW" "$R"
  fi
  printf '%s' "$R"
  say ""
  say "    Open        ${B}${CYN}http://localhost:${UI_PORT:-5173}/${R}   ${DIM}(dev server — hot reload)${R}"
  say "    API docs    ${DIM}http://localhost:${GATEWAY_PORT:-8000}/docs${R}"
  say ""
  say "    Sign in with"
  say "      email     ${B}admin@ainxt.local${R}"
  if [[ -n "$pw" ]]; then
    say "      password  ${B}${pw}${R}"
    say "                ${DIM}shown once — save it now${R}"
  else
    say "      password  ${DIM}grep -A3 'admin user created' log/gateway.out${R}"
  fi
  say ""
  say "    ${DIM}api log    tail -f log/gateway.out"
  say "    ui log     tail -f log/ai-ui.out"
  say "    events     tail -f log/kafka-consumer.out   (chat history / audit writer)"
  say "    stop       ./stop-local.sh"
  say "    datastores ${COMPOSE[*]} stop postgres redis ollama kafka${R}"
  say ""
  say "    ${DIM}The venv is .venv — activate it with:  source .venv/bin/activate${R}"
  if [[ "$KB_ENABLED" == "1" ]]; then
    say ""
    say "    Knowledge Base   ${GRN}on${R}  ${DIM}(embed-svc :${kb_port} — see docs/KB_SETUP.md)${R}"
  fi
  say ""
}

# ── Knowledge Base setup ─────────────────────────────────────────────────────
# Offered interactively at the end of the install. Delegates to kb-setup.sh
# (which handles its own prompts, build, model download, and health check).
# The KnowledgeBase page also shows a banner pointing at it if embed-svc
# isn't reachable.
# This function is just the initial yes/no gate plus the hand-off.
KB_ENABLED="0"
SDLC_ENABLED="0"
setup_knowledge_base() {
  step "Knowledge Base (optional)"
  say "  Adds document parsing, semantic search, and answer reranking."
  say "  ${DIM}Runs in its own container (embed-svc) — see docs/KB_SETUP.md.${R}"

  if [[ "$ASSUME_YES" == "yes" || -z "$TTY" ]]; then
    say "  ${DIM}non-interactive — leaving the Knowledge Base disabled${R}"
    say "  ${DIM}enable later with: ./kb-setup.sh${R}"
    return 0
  fi

  local kb_yn=""
  printf '\n  Set up the Knowledge Base now? [y/N]: ' > "$TTY"
  read -r kb_yn < "$TTY" || kb_yn="n"
  case "$kb_yn" in
    y|Y|yes|YES) ;;
    *) say "  ${DIM}Skipping — enable later with: ./kb-setup.sh${R}"; return 0 ;;
  esac

  # Explicit chmod rather than trusting the execute bit survived a git
  # checkout/curl download/Windows filesystem — cheap, and removes a whole
  # class of "Permission denied" reports that have nothing to do with the
  # script itself.
  chmod 755 ./kb-setup.sh 2>/dev/null || true
  # kb-setup.sh handles its own errors/warnings and never dies on a failed
  # model download or build — it always returns so this installer's own
  # final summary can still print. Its own $TTY/prompt handling is identical
  # to this script's (same style, deliberately duplicated — see its header).
  # Invoked via `bash` rather than `./kb-setup.sh` so it runs the same way
  # regardless of whether the execute bit actually took (e.g. some Windows
  # filesystems mounted into WSL2 don't honor chmod reliably).
  bash ./kb-setup.sh || warn "Knowledge Base setup did not finish — re-run ./kb-setup.sh any time."

  # Re-check health here (not just trust kb-setup.sh's own exit code) so the
  # final summary below reflects reality even if kb-setup.sh warned but kept
  # going. Uses the possibly-reassigned EMBED_SVC_PORT from .env.
  local _esp
  _esp="$(grep -E '^EMBED_SVC_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  _esp="${_esp:-8001}"
  if curl -fsS -m 5 "http://localhost:${_esp}/health" >/dev/null 2>&1; then
    KB_ENABLED="1"
  fi
}

setup_sdlc_cli() {
  step "SDLC CLI engine (optional)"
  say "  Enables the SDLC pipeline's coding-agent engine (the sdlc-worker container)."

  if [[ "$MODE" == "local" ]]; then
    say "  ${DIM}SDLC CLI setup is Docker-only — skipping in local mode.${R}"
    return 0
  fi

  if [[ "$ASSUME_YES" == "yes" || -z "$TTY" ]]; then
    say "  ${DIM}non-interactive — leaving SDLC disabled${R}"
    say "  ${DIM}enable later with: ./sdlc-setup.sh${R}"
    return 0
  fi

  local sdlc_yn=""
  printf '\n  Set up the SDLC CLI engine now? [y/N]: ' > "$TTY"
  read -r sdlc_yn < "$TTY" || sdlc_yn="n"
  case "$sdlc_yn" in
    y|Y|yes|YES) ;;
    *) say "  ${DIM}Skipping — enable later with: ./sdlc-setup.sh${R}"; return 0 ;;
  esac

  chmod 755 ./sdlc-setup.sh 2>/dev/null || true
  # sdlc-setup.sh handles its own errors/warnings and never dies on a failed
  # download or build — it always returns so this installer's own final
  # summary can still print.
  if bash ./sdlc-setup.sh; then
    [[ -f bin/ainxt && -f bin/config.toml ]] && SDLC_ENABLED="1"
  else
    warn "SDLC CLI setup did not finish — re-run ./sdlc-setup.sh any time."
  fi
}

banner
check_prereqs
fetch_source
choose_provider
write_env
choose_ports
build_doc_sandbox_image

if [[ "$MODE" == "local" ]]; then
  check_prereqs_local
  start_infra_only
  install_python_deps
  export_env_for_host
  use_local_storage_dirs
  run_migrations_local
  step "Registering providers in the admin screen"
  seed_provider_db ./.venv/bin/python
  start_backend_local
  start_kafka_consumer_local
  start_ui_local
  if [[ "$PROVIDER" == "ollama" ]]; then
    model="$(default_ollama_model)"
    step "Downloading the local model ($model, ~2GB)"
    docker exec ainxt-ollama ollama pull "$model" >/dev/null 2>&1 \
      && { ok "$model ready"; seed_ollama_model_db ./.venv/bin/python; } \
      || warn "pull failed — run it later with: docker exec ainxt-ollama ollama pull $model"
  fi
  verify_install
  setup_knowledge_base
  finish_local
else
  use_container_service_names
  start_stack
  verify_install
  setup_knowledge_base
  setup_sdlc_cli
  finish
fi
