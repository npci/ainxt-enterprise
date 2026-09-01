#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# First-run setup for the AiNxt Platform.
#
# Brings a clean checkout to a running backend: checks prerequisites, creates
# .env with generated secrets, starts Postgres and Redis, installs Python
# dependencies, runs migrations, and tells you what to do next.
#
#   ./scripts/setup.sh              # full setup
#   ./scripts/setup.sh --check      # prerequisites only, change nothing
#   ./scripts/setup.sh --no-docker  # you are running your own Postgres/Redis
#
# Safe to re-run. It never overwrites an existing .env, never drops data, and
# stops at the first real failure rather than continuing into a worse state.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CHECK_ONLY=0
USE_DOCKER=1
for arg in "$@"; do
  case "$arg" in
    --check)     CHECK_ONLY=1 ;;
    --no-docker) USE_DOCKER=0 ;;
    -h|--help)   sed -n '3,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# --- output helpers ---------------------------------------------------------
if [ -t 1 ]; then
  BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
else
  BOLD=''; RED=''; GREEN=''; YELLOW=''; OFF=''
fi
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$OFF" "$1"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$OFF" "$1"; }
bad()  { printf '  %s✗%s %s\n' "$RED" "$OFF" "$1"; }
step() { printf '\n%s%s%s\n' "$BOLD" "$1" "$OFF"; }
die()  { printf '\n%s%s%s\n' "$RED" "$1" "$OFF" >&2; exit 1; }

# Run a command with a wall-clock limit, returning 124 if it overruns.
# `timeout(1)` is GNU coreutils and absent from a stock macOS, and `docker info`
# against an unreachable daemon blocks for minutes -- which turned a
# prerequisite CHECK into a hang the first time this script was run.
with_timeout() {
  local secs="$1"; shift
  "$@" >/dev/null 2>&1 &
  local pid=$!
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$secs" ]; then
      kill -9 "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      return 124
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$pid"
}

# --- 1. prerequisites -------------------------------------------------------
step "1. Checking prerequisites"
MISSING=0

if command -v python3 >/dev/null 2>&1; then
  PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  # The project declares requires-python >= 3.10 in pyproject.toml.
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    ok "python3 $PY_VER"
  else
    bad "python3 $PY_VER — 3.10 or newer is required"; MISSING=1
  fi
else
  bad "python3 not found"; MISSING=1
fi

if [ "$USE_DOCKER" = "1" ]; then
  if command -v docker >/dev/null 2>&1; then
    if with_timeout 15 docker compose version; then
      ok "docker + compose"
    else
      bad "docker found but 'docker compose' is not available (need Compose v2)"; MISSING=1
    fi
    if with_timeout 15 docker info; then
      ok "docker daemon is running"
    elif [ $? = 124 ]; then
      bad "docker daemon did not respond within 15s — is Docker Desktop starting?"; MISSING=1
    else
      bad "docker daemon is not running — start Docker Desktop, or use --no-docker"; MISSING=1
    fi
  else
    bad "docker not found — install it, or use --no-docker with your own Postgres and Redis"
    MISSING=1
  fi
else
  warn "--no-docker: you are providing Postgres and Redis yourself"
fi

[ "$MISSING" = "0" ] || die "Prerequisites are missing. Install them and re-run."
if [ "$CHECK_ONLY" = "1" ]; then
  printf '\n%sPrerequisites look good.%s Re-run without --check to continue.\n' "$GREEN" "$OFF"
  exit 0
fi

# --- 2. .env ----------------------------------------------------------------
step "2. Configuring .env"
gen_secret() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32
  else python3 -c 'import secrets; print(secrets.token_hex(32))'
  fi
}

if [ -f .env ]; then
  ok ".env already exists — leaving it alone"
  # Re-running must never clobber a configured deployment. Report gaps instead.
  for var in JWT_SECRET POSTGRES_HOST POSTGRES_PASSWORD REDIS_HOST; do
    if ! grep -qE "^${var}=.+" .env; then
      warn "$var is empty in .env — the platform will warn at startup"
    fi
  done
else
  [ -f .env.example ] || die ".env.example is missing; cannot generate .env"
  cp .env.example .env
  JWT_VALUE="$(gen_secret)"
  PG_VALUE="$(gen_secret | cut -c1-24)"

  # `sed -i` differs between GNU and BSD, so edit in Python instead.
  python3 - "$JWT_VALUE" "$PG_VALUE" <<'PYEOF'
import io, re, sys
jwt, pg = sys.argv[1], sys.argv[2]
values = {
    "JWT_SECRET": jwt,
    "POSTGRES_PASSWORD": pg,
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_USER": "postgres",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    # Composed from the values above so the two never drift apart. A mismatched
    # DATABASE_URL against POSTGRES_* is the most common first-run failure.
    "DATABASE_URL": f"postgresql://postgres:{pg}@localhost:5432/ainxt_memory",
}
text = io.open(".env", encoding="utf-8").read()
for key, value in values.items():
    pattern = re.compile(r"^%s=.*$" % re.escape(key), re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub("%s=%s" % (key, value), text, count=1)
    else:
        text += "\n%s=%s\n" % (key, value)
io.open(".env", "w", encoding="utf-8").write(text)
PYEOF
  chmod 600 .env
  ok ".env created from .env.example (mode 600)"
  ok "JWT_SECRET and POSTGRES_PASSWORD generated"
  ok "DATABASE_URL composed to match POSTGRES_*"
fi

# A model provider cannot be generated — it is the one thing only you can supply.
if grep -qE '^(LOCAL_LLM_BASE_URL|LITELLM_BASE_URL|OPENAI_API_KEY|ANTHROPIC_API_KEY|GEMINI_API_KEY|GOOGLE_API_KEY)=.+' .env; then
  ok "a model provider is configured"
else
  warn "no model provider set — the platform will start, but chat will not work"
  warn "set ONE provider in .env. The START HERE block at the top of"
  warn ".env.example lists the options; the simplest is a local Ollama, which"
  warn "needs no API key at all."
fi

# --- 3. infrastructure ------------------------------------------------------
if [ "$USE_DOCKER" = "1" ]; then
  step "3. Starting Postgres and Redis"
  # Only the datastores the platform requires. Ollama, Kafka, Prometheus and
  # Grafana are also in the compose file but are not needed to boot.
  docker compose up -d postgres redis
  printf '  waiting for Postgres to accept connections'
  for _ in $(seq 1 60); do
    if docker compose exec -T postgres pg_isready -U "${POSTGRES_USER:-postgres}" >/dev/null 2>&1; then
      printf '\n'; ok "Postgres is ready"; break
    fi
    printf '.'; sleep 2
  done
  docker compose exec -T postgres pg_isready -U "${POSTGRES_USER:-postgres}" >/dev/null 2>&1 \
    || die "Postgres did not become ready. Check: docker compose logs postgres"
  docker compose exec -T redis redis-cli ping >/dev/null 2>&1 \
    && ok "Redis is ready" \
    || die "Redis did not become ready. Check: docker compose logs redis"
else
  step "3. Skipping infrastructure (--no-docker)"
  warn "ensure your own Postgres and Redis match POSTGRES_* and REDIS_* in .env"
fi

# --- 4. Python dependencies -------------------------------------------------
step "4. Installing Python dependencies"
if [ -d .venv ]; then
  ok ".venv already exists"
else
  python3 -m venv .venv
  ok ".venv created"
fi
# shellcheck disable=SC1091
. .venv/bin/activate
python3 -m pip install --quiet --upgrade pip
python3 -m pip install --quiet -r requirements.txt
ok "requirements.txt installed"

# --- 5. migrations ----------------------------------------------------------
step "5. Running database migrations"
python3 db/migrate.py
ok "migrations applied"

# --- 6. what next -----------------------------------------------------------
step "Setup complete"
cat <<'EOF'

  Start the backend:

      . .venv/bin/activate
      gunicorn gateway:app -c gunicorn.conf.py

  (Not `python gateway.py` — that file has no __main__ block and exits without
  serving. For a single-process dev server instead:
      python -m uvicorn gateway:app --host 127.0.0.1 --port 8000)

  It prints a ✅/⚠️/❌ config summary on boot — read that first if anything
  looks wrong. On the very first boot it also creates an admin user and prints
  a generated password ONCE. Copy it before clearing the terminal.

      Backend    http://localhost:8000
      API docs   http://localhost:8000/ainxt/v1/api/docs

  Then, in a second terminal, the web UI:

      cd ai-ui && npm install && npm run dev
      Frontend   http://localhost:5173

EOF
