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
for arg in "$@"; do
  case "$arg" in
    --yes|-y)    ASSUME_YES="yes" ;;
    --local)     MODE="local" ;;
    --with-ocr)  WITH_OCR=1 ;;
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
               [[ "$PROVIDER" == "none" ]] && PROVIDER="ollama" ;;
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
s = open('.env').read()
# every occurrence, not just the first: a key duplicated later in the
# file would be re-asserted and silently win when the file is sourced.
s = re.sub(rf'^{re.escape(k)}=.*$', f'{k}={v}', s, flags=re.M)
open('.env','w').write(s)
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

# .env.example points OLLAMA_URL / LOCAL_LLM_BASE_URL at localhost, which is right
# for a native run and wrong inside a container — there, localhost is the gateway
# itself. compose's ${OLLAMA_URL:-http://ollama:11434} default never applies
# because the variable IS set in .env, so the local model tier came up with
# "ollama: error: [Errno 111] Connection refused". Rewrite them to the service
# name for Docker mode; native mode overrides them with 127.0.0.1 at run time in
# export_env_for_host(), so writing service names here is safe for both.
use_container_service_names() {
  step "Pointing the gateway at the bundled services"
  set_env OLLAMA_URL         "http://ollama:11434"
  set_env LOCAL_LLM_BASE_URL "http://ollama:11434"
  set_env POSTGRES_HOST      "postgres"
  set_env PGVECTOR_HOST      "postgres"
  set_env REDIS_HOST         "redis"
  set_env REDIS_URL          "redis://redis:6379/0"
  ok "gateway will reach postgres/redis/ollama by service name"
}

# ── 5. Start ────────────────────────────────────────────────────────────────
start_stack() {
  step "Building and starting the stack"
  export WITH_OCR
  if [[ "$WITH_OCR" == "1" ]]; then
    say "  ${DIM}Including the optional PaddleOCR engine (--with-ocr).${R}"
  fi
  say "  ${DIM}First run pulls and builds several GB of images. Grab a coffee.${R}"
  "${COMPOSE[@]}" up -d --build \
    || die "Startup failed. Show the logs with:  ${COMPOSE[*]} logs --tail=80"
  ok "containers started"

  step "Waiting for the platform to become healthy"
  local waited=0 limit=900
  until curl -fsS -m 5 "http://localhost:${GATEWAY_PORT:-8000}/health" >/dev/null 2>&1; do
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

  if [[ "$PROVIDER" == "ollama" ]]; then
    step "Downloading the local model (llama3.2, ~2GB)"
    docker exec ainxt-ollama ollama pull llama3.2 >/dev/null 2>&1 \
      && ok "llama3.2 ready" \
      || warn "pull failed — run 'docker exec ainxt-ollama ollama pull llama3.2' later"
  fi
}

# ── 6. Done ─────────────────────────────────────────────────────────────────
DOCTOR_RESULT="skipped"

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
  local pw
  pw="$("${COMPOSE[@]}" logs gateway 2>/dev/null | grep -m1 'Password *:' | sed 's/.*: *//' | tr -d '\r' || true)"

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
  say "    reset      ${COMPOSE[*]} down -v   (deletes all data)${R}"
  if [[ "$PROVIDER" == "none" ]]; then
    say ""
    warn "No model provider set. Add a key to .env, then: ${COMPOSE[*]} up -d gateway"
  fi
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
  for spec in "POSTGRES_PORT:5432:PostgreSQL" "REDIS_PORT:6379:Redis" "OLLAMA_PORT:11434:Ollama" "GATEWAY_PORT:8000:the API" "UI_PORT:5173:the web UI"; do
    local var="${spec%%:*}" rest="${spec#*:}"
    local def="${rest%%:*}" label="${rest#*:}"
    local cur="${!var:-$def}"
    if port_in_use "$cur"; then
      local free; free="$(pick_free_port "$((cur+1))")"
      warn "port $cur is already in use — putting $label on $free instead"
      set_env "$var" "$free"
      export "$var=$free"
      changed=1
    else
      export "$var=$cur"
    fi
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
  step "Starting PostgreSQL, Redis and Ollama in Docker"
  say "  ${DIM}Only the datastores run in containers; the API and UI run on this machine.${R}"
  "${COMPOSE[@]}" up -d postgres redis ollama \
    || die "Could not start the datastores. Check:  ${COMPOSE[*]} logs postgres redis"
  local waited=0
  until [[ "$(docker inspect -f '{{.State.Health.Status}}' ainxt-postgres 2>/dev/null)" == "healthy" \
        && "$(docker inspect -f '{{.State.Health.Status}}' ainxt-redis 2>/dev/null)" == "healthy" ]]; do
    (( waited >= 180 )) && die "PostgreSQL/Redis did not become healthy in 180s."
    sleep 3; waited=$((waited+3))
  done
  ok "postgres and redis healthy"
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
  until curl -fsS -m 5 "http://localhost:${GATEWAY_PORT:-8000}/health" >/dev/null 2>&1; do
    if ! kill -0 "$(cat .ainxt-gateway.pid)" 2>/dev/null; then
      say ""; tail -30 log/gateway.out; die "The API stopped during startup — see above and log/gateway.out"
    fi
    (( waited >= 600 )) && die "API did not become healthy in 600s. See log/gateway.out"
    sleep 5; waited=$((waited+5))
    (( waited % 60 == 0 )) && say "  ${DIM}still starting… ${waited}s${R}"
  done
  ok "API healthy on :${GATEWAY_PORT:-8000} (pid $(cat .ainxt-gateway.pid))"
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
  say "    stop       ./stop-local.sh"
  say "    datastores ${COMPOSE[*]} stop postgres redis ollama${R}"
  say ""
  say "    ${DIM}The venv is .venv — activate it with:  source .venv/bin/activate${R}"
  say ""
}

banner
check_prereqs
fetch_source
choose_provider
write_env
choose_ports

if [[ "$MODE" == "local" ]]; then
  check_prereqs_local
  start_infra_only
  install_python_deps
  export_env_for_host
  use_local_storage_dirs
  run_migrations_local
  start_backend_local
  start_ui_local
  if [[ "$PROVIDER" == "ollama" ]]; then
    step "Downloading the local model (llama3.2, ~2GB)"
    docker exec ainxt-ollama ollama pull llama3.2 >/dev/null 2>&1 \
      && ok "llama3.2 ready" || warn "pull failed — run it later with: docker exec ainxt-ollama ollama pull llama3.2"
  fi
  verify_install
  finish_local
else
  use_container_service_names
  start_stack
  verify_install
  finish
fi
