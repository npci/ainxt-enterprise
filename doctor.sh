#!/usr/bin/env bash
#
# AiNxt Enterprise — post-install check.
#
#   ./doctor.sh              check whatever is running (Docker or native)
#   ./doctor.sh --json       machine-readable, for CI
#   ./doctor.sh --help       usage
#
# Answers one question: is this install actually working, and if not, which part
# is broken and what do I do about it?
#
# Every check asserts on an artifact — a table count, a JSON field, a byte
# length — never on an exit code. That is deliberate. This platform has a
# recurring habit of exiting 0 and returning HTTP 200 over a total failure: the
# migration runner reported success with 24 failed parts, the compose file
# started nothing and said nothing, and every login returned 500 while the
# process stayed "healthy". A checker that trusts exit codes would have passed
# all three.
#
# Exit status: 0 when nothing REQUIRED is broken, 1 otherwise. OPTIONAL findings
# never fail the run — they are things you may simply not have turned on.

set -uo pipefail   # deliberately NOT -e: checks are expected to fail individually

MODE="auto"        # auto | docker | local
JSON="no"

for arg in "$@"; do
  case "$arg" in
    --json)    JSON="yes" ;;
    --local)   MODE="local" ;;
    --docker)  MODE="docker" ;;
    -h|--help)
      cat <<'USAGE'
AiNxt Enterprise post-install check

  ./doctor.sh            check the running install (auto-detects Docker/native)
  ./doctor.sh --docker   assume the Docker install
  ./doctor.sh --local    assume the native install (--local from install.sh)
  ./doctor.sh --json     emit JSON instead of a report; still exits non-zero on
                         a REQUIRED failure, so it can gate a CI job

Environment (all optional — defaults match install.sh):
  GATEWAY_PORT   default 8000
  UI_PORT        default 5173
  POSTGRES_PORT  default 5432
  REDIS_PORT     default 6379
USAGE
      exit 0 ;;
    *) printf 'unknown argument: %s (try --help)\n' "$arg" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")" || exit 2

if [[ -t 1 && "$JSON" == "no" ]]; then
  B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[0m'
  GRN=$'\033[32m'; YLW=$'\033[33m'; RED=$'\033[31m'; CYN=$'\033[36m'
else
  B=""; DIM=""; R=""; GRN=""; YLW=""; RED=""; CYN=""
fi

# _env_or_default: prefer the in-process env var (set by install.sh / the
# caller's shell), then fall back to the .env file, then the hard-coded
# default. This is needed so doctor.sh correctly checks the ports of installs
# whose ports were reassigned (which choose_ports() does automatically on
# any conflict — not a rare case). Confirmed via live testing.
_env_or_default() {
  local var="$1" default="$2" current="${!1:-}"
  if [[ -n "$current" ]]; then printf '%s' "$current"; return; fi
  local v; v="$(grep -E "^${var}=" .env 2>/dev/null | tail -1 | cut -d= -f2-)"
  printf '%s' "${v:-$default}"
}
GATEWAY_PORT="$(_env_or_default GATEWAY_PORT 8000)"
UI_PORT="$(_env_or_default UI_PORT 5173)"
POSTGRES_PORT="$(_env_or_default POSTGRES_PORT 5432)"
REDIS_PORT="$(_env_or_default REDIS_PORT 6379)"
API="http://localhost:${GATEWAY_PORT}"
UI="http://localhost:${UI_PORT}"

PASS=0; FAIL=0; WARN=0; SKIP=0
FAILED_TITLES=()
JSON_ROWS=()

# ── reporting ────────────────────────────────────────────────────────────────
_json_row() {
  # $1 status, $2 severity, $3 title, $4 detail, $5 fix
  local esc
  esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
  JSON_ROWS+=("$(printf '{"status":"%s","severity":"%s","check":"%s","detail":"%s","fix":"%s"}' \
    "$1" "$2" "$(esc "$3")" "$(esc "$4")" "$(esc "$5")")")
}
section() { [[ "$JSON" == "yes" ]] && return 0; printf '\n%s──%s %s%s%s\n' "$CYN" "$R" "$B" "$*" "$R"; }
pass()    { PASS=$((PASS+1)); _json_row ok       required "$1" "${2:-}" ""
            [[ "$JSON" == "yes" ]] || printf '  %s✓%s %-46s %s%s%s\n' "$GRN" "$R" "$1" "$DIM" "${2:-}" "$R"; }
fail()    { FAIL=$((FAIL+1)); FAILED_TITLES+=("$1"); _json_row fail required "$1" "${2:-}" "${3:-}"
            [[ "$JSON" == "yes" ]] && return 0
            printf '  %s✗%s %-46s %s%s%s\n' "$RED" "$R" "$1" "$RED" "${2:-}" "$R"
            [[ -n "${3:-}" ]] && printf '      %s→ %s%s\n' "$DIM" "$3" "$R"; return 0; }
warno()   { WARN=$((WARN+1)); _json_row warn optional "$1" "${2:-}" "${3:-}"
            [[ "$JSON" == "yes" ]] && return 0
            printf '  %s!%s %-46s %s%s%s\n' "$YLW" "$R" "$1" "$YLW" "${2:-}" "$R"
            [[ -n "${3:-}" ]] && printf '      %s→ %s%s\n' "$DIM" "$3" "$R"; return 0; }
skip()    { SKIP=$((SKIP+1)); _json_row skip optional "$1" "${2:-}" ""
            [[ "$JSON" == "yes" ]] || printf '  %s·%s %-46s %s%s%s\n' "$DIM" "$R" "$1" "$DIM" "${2:-}" "$R"; }

have() { command -v "$1" >/dev/null 2>&1; }

# Read a value from .env without sourcing it — .env is data, not a script, and
# sourcing it would execute anything a stray backtick happened to contain.
envval() {
  [[ -f .env ]] || return 1
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" .env | tail -1 | tr -d '\r' | sed 's/[[:space:]]*$//'
}

# Bounded HTTP GET.
#
# The status is written to a FILE, not just a variable: callers use
# `body="$(http ...)"`, and a variable assigned inside a command substitution
# lives in that subshell and never reaches the caller. The first version of this
# script did exactly that, so every HTTP check compared an empty string against
# "200" and reported the API as unreachable while it was serving perfectly.
HTTP_CODE=""
http() {
  local url="$1" timeout="${2:-8}"
  curl -sS -m "$timeout" -o /tmp/.doctor_body -w '%{http_code}' "$url" \
    > /tmp/.doctor_code 2>/dev/null || printf '000' > /tmp/.doctor_code
  cat /tmp/.doctor_body 2>/dev/null
}

# Read the status recorded by the most recent http() call.
http_code() {
  local c
  c="$(cat /tmp/.doctor_code 2>/dev/null | tr -dc '0-9')"
  printf '%s' "${c:-000}"
}

jqf() {  # extract a field without depending on jq being installed
  python3 -c "
import json,sys
try: d=json.load(open('/tmp/.doctor_body'))
except Exception: sys.exit(1)
cur=d
for k in sys.argv[1].split('.'):
    if isinstance(cur,dict) and k in cur: cur=cur[k]
    else: sys.exit(1)
print(cur if not isinstance(cur,(dict,list)) else json.dumps(cur))
" "$1" 2>/dev/null
}

# ── mode detection ───────────────────────────────────────────────────────────
COMPOSE=()
if have docker && docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose)
elif have docker-compose; then COMPOSE=(docker-compose); fi

if [[ "$MODE" == "auto" ]]; then
  if [[ ${#COMPOSE[@]} -gt 0 ]] && [[ -n "$("${COMPOSE[@]}" ps -q 2>/dev/null)" ]]; then
    MODE="docker"
  else
    MODE="local"
  fi
fi

if [[ "$JSON" == "no" ]]; then
  printf '\n%s%s AiNxt Enterprise — post-install check %s\n' "$B" "$CYN" "$R"
  printf '  %smode: %s · api: %s · ui: %s%s\n' "$DIM" "$MODE" "$API" "$UI" "$R"
fi

# ── 1. configuration ─────────────────────────────────────────────────────────
section "Configuration"

if [[ -f .env ]]; then
  pass ".env present" "$(wc -l < .env | tr -d ' ') lines"

  # Secrets must exist AND be long enough. jwt_handler refuses to import with a
  # JWT_SECRET under 32 characters, which surfaces as the whole API failing to
  # start rather than as a config error.
  for spec in "POSTGRES_PASSWORD:8" "JWT_SECRET:32" "SECRET_KEY:16" "AUDIT_SIGNING_KEY:16"; do
    var="${spec%%:*}"; min="${spec##*:}"
    val="$(envval "$var")"
    if [[ -z "$val" ]]; then
      fail "$var set" "empty" "add $var to .env, then restart the gateway"
    elif [[ "${#val}" -lt "$min" ]]; then
      fail "$var length" "${#val} chars, needs >= $min" \
           "generate one: openssl rand -hex 32   (then restart the gateway)"
    elif [[ "$val" == *"change"* || "$val" == *"CHANGE"* || "$val" == "your-"* ]]; then
      fail "$var is not a placeholder" "looks like a template value" \
           "replace $var in .env with a generated secret"
    else
      pass "$var set" "${#val} chars"
    fi
  done

  # At least one model provider, or the platform runs but cannot answer.
  provider=""
  for k in ANTHROPIC_API_KEY OPENAI_API_KEY GEMINI_API_KEY GOOGLE_API_KEY; do
    [[ -n "$(envval "$k")" ]] && provider="${provider}${provider:+, }${k%%_API_KEY}"
  done
  if [[ -n "$provider" ]]; then
    pass "model provider configured" "$provider"
  elif [[ -n "$(envval LOCAL_LLM_BASE_URL)" || -n "$(envval OLLAMA_URL)" ]]; then
    warno "no cloud provider key" "local model only" \
          "chat will only work if Ollama has a model pulled: docker compose exec ollama ollama list"
  else
    fail "model provider configured" "no key and no local model URL" \
         "add ANTHROPIC_API_KEY, OPENAI_API_KEY or GEMINI_API_KEY to .env"
  fi
else
  fail ".env present" "missing" "run ./install.sh, or: cp .env.example .env"
fi

# ── 2. prerequisites ─────────────────────────────────────────────────────────
section "Prerequisites"

if [[ "$MODE" == "docker" ]]; then
  if [[ ${#COMPOSE[@]} -gt 0 ]]; then
    pass "docker compose available" "${COMPOSE[*]}"
    if docker info >/dev/null 2>&1; then
      pass "docker daemon reachable"
    else
      fail "docker daemon reachable" "not responding" \
           "start Docker Desktop; if it hangs, kill the stale backend: pkill -9 -f com.docker.backend && open -a Docker"
    fi
  else
    fail "docker compose available" "not found" "install Docker Desktop, or use ./doctor.sh --local"
  fi
else
  if have python3; then pass "python3 available" "$(python3 -V 2>&1 | awk '{print $2}')"
  else fail "python3 available" "not found" "install Python 3.10 or newer"; fi
  if have node; then pass "node available" "$(node -v)"
  else warno "node available" "not found" "needed only to build the web UI"; fi
fi

# ── 3. services ──────────────────────────────────────────────────────────────
section "Services"

if [[ "$MODE" == "docker" && ${#COMPOSE[@]} -gt 0 ]]; then
  ps_out="$("${COMPOSE[@]}" ps --format '{{.Service}} {{.Status}}' 2>/dev/null)"
  if [[ -z "$ps_out" ]]; then
    fail "containers running" "none" "start them: ${COMPOSE[*]} up -d"
  else
    while read -r svc status; do
      [[ -z "$svc" ]] && continue
      case "$status" in
        *healthy*)     pass "container $svc" "$status" ;;
        *unhealthy*)   fail "container $svc" "$status" "${COMPOSE[*]} logs --tail 50 $svc" ;;
        Up*|*Up*)      pass "container $svc" "$status" ;;
        *Restarting*)  fail "container $svc" "$status" "${COMPOSE[*]} logs --tail 50 $svc" ;;
        *)             fail "container $svc" "$status" "${COMPOSE[*]} logs --tail 50 $svc" ;;
      esac
    done <<< "$ps_out"
  fi
else
  skip "container status" "native mode"
fi

# ── 3b. document generation sandbox ─────────────────────────────────────────
# Document generation (Word / PowerPoint / Excel / PDF) runs inside the
# `ainxt-doc-sandbox:latest` image via the host Docker socket. Two things must
# be true for it to work:
#   1. The image exists on the host daemon.
#   2. The doc-worker container can reach that daemon (socket mounted).
# When either is missing, every document job fails with
# "Document sandbox unavailable: Docker is not running." — even though Docker
# itself is running fine. The `doc-sandbox-builder` service in docker-compose.yml
# builds the image automatically on `docker compose up`, so this check should
# pass on any install that went through the normal startup path.
section "Document generation sandbox"

if [[ "$MODE" == "docker" ]]; then
  if docker image inspect ainxt-doc-sandbox:latest >/dev/null 2>&1; then
    pass "ainxt-doc-sandbox image built" "ainxt-doc-sandbox:latest"
  else
    fail "ainxt-doc-sandbox image built" "image not found" \
         "docker compose up -d   (the doc-sandbox-builder service builds it automatically, or run: bash docker/doc-sandbox/build.sh)"
  fi

  if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^ainxt-doc-worker$'; then
    if docker exec ainxt-doc-worker docker info >/dev/null 2>&1; then
      pass "doc-worker can reach Docker daemon"
    else
      fail "doc-worker can reach Docker daemon" "docker info failed inside container" \
           "check /var/run/docker.sock is mounted: docker inspect ainxt-doc-worker | grep docker.sock"
    fi
  else
    skip "doc-worker Docker connectivity" "ainxt-doc-worker not running"
  fi
else
  skip "document generation sandbox" "native mode — sandbox runs via host Docker CLI"
fi

# ── 3c. event pipeline ───────────────────────────────────────────────────────
# Kafka is required, not an optional extra. The gateway does not write chat
# history, audit entries, model_usages or thread/SDLC/budget/agent/Coach rows
# itself — it publishes an event and returns, and workers/kafka_consumer.py
# performs the INSERT. Both halves must be up or those rows are never written,
# and nothing else in this script notices: /health stays green, the UI renders,
# and the loss only surfaces when someone reopens a conversation and finds it
# empty. That is exactly the "a container being up is not evidence the thing
# inside it works" case this script exists for, so these are required checks.
section "Event pipeline (Kafka)"

kafka_on="$(envval KAFKA_ENABLED | tr 'A-Z' 'a-z')"
kafka_boot="$(envval KAFKA_BOOTSTRAP)"

if [[ "$kafka_on" == "true" ]]; then
  pass "KAFKA_ENABLED" "true"
else
  fail "KAFKA_ENABLED" "${kafka_on:-unset}" \
       "set KAFKA_ENABLED=true in .env — with it off, events queue into Redis kafka:fallback:* on a 7-day TTL and no chat-history or audit row reaches Postgres"
fi

if [[ -n "$kafka_boot" ]]; then
  pass "KAFKA_BOOTSTRAP" "$kafka_boot"
else
  fail "KAFKA_BOOTSTRAP" "unset" \
       "set KAFKA_BOOTSTRAP in .env — kafka:19092 for the Docker install, 127.0.0.1:9092 for --local"
fi

# Broker reachability. In Docker the advertised INTERNAL address (kafka:19092)
# does not resolve from the host, so ask the container itself rather than
# dialling it from here and reporting a false failure.
if [[ "$MODE" == "docker" ]]; then
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^ainxt-kafka$'; then
    khealth="$(docker inspect -f '{{.State.Health.Status}}' ainxt-kafka 2>/dev/null || true)"
    # MSYS_NO_PATHCONV: under Git Bash, /opt/kafka/... is rewritten to a Windows
    # path before it reaches the container, so the exec fails with "no such file"
    # while the broker is perfectly healthy. Harmless on Linux/macOS/WSL2.
    if MSYS_NO_PATHCONV=1 docker exec ainxt-kafka /opt/kafka/bin/kafka-topics.sh \
         --bootstrap-server localhost:9092 --list >/tmp/.doctor_topics 2>/dev/null; then
      topics="$(grep -c . /tmp/.doctor_topics 2>/dev/null | head -1)"; topics="${topics:-0}"
      pass "kafka broker responding" "$topics topic(s)"
    elif [[ "$khealth" == "healthy" ]]; then
      # The compose healthcheck lists topics through the broker from inside the
      # container, so a healthy status asserts the same thing by another route.
      pass "kafka broker responding" "healthcheck passing"
    else
      fail "kafka broker responding" "broker not answering (${khealth:-no healthcheck})" \
           "${COMPOSE[*]} logs --tail 50 kafka"
    fi
  else
    fail "kafka broker running" "no ainxt-kafka container" \
         "${COMPOSE[*]} up -d kafka"
  fi
else
  # Native mode talks to the published EXTERNAL listener on the host.
  kb_host="${kafka_boot%%:*}"; kb_port="${kafka_boot##*:}"
  [[ "$kb_host" == "$kafka_boot" ]] && kb_port=9092
  if python3 -c "
import socket,sys
try:
    s=socket.create_connection(('${kb_host:-127.0.0.1}', int('${kb_port:-9092}')), 5); s.close()
except Exception: sys.exit(1)
" 2>/dev/null; then
    pass "kafka broker reachable" "${kb_host:-127.0.0.1}:${kb_port:-9092}"
  else
    fail "kafka broker reachable" "cannot connect to ${kb_host:-127.0.0.1}:${kb_port:-9092}" \
         "${COMPOSE[*]} up -d kafka"
  fi
fi

# The consumer. A broker with no consumer is the quieter failure of the two:
# events are accepted and retained, and still never become rows.
if [[ "$MODE" == "docker" ]]; then
  cstate="$(docker inspect -f '{{.State.Status}}' ainxt-kafka-consumer 2>/dev/null || true)"
  case "$cstate" in
    running) pass "kafka consumer running" "ainxt-kafka-consumer" ;;
    "")      fail "kafka consumer running" "no ainxt-kafka-consumer container" \
                  "${COMPOSE[*]} up -d kafka-consumer — without it no chat-history or audit row is ever written" ;;
    *)       fail "kafka consumer running" "$cstate" \
                  "${COMPOSE[*]} logs --tail 50 kafka-consumer" ;;
  esac
else
  cpid=""
  [[ -f .ainxt-kafka-consumer.pid ]] && cpid="$(cat .ainxt-kafka-consumer.pid 2>/dev/null)"
  if [[ -n "$cpid" ]] && kill -0 "$cpid" 2>/dev/null; then
    pass "kafka consumer running" "pid $cpid"
  elif pgrep -f 'kafka_consumer.py' >/dev/null 2>&1; then
    pass "kafka consumer running" "pgrep matched kafka_consumer.py"
  else
    fail "kafka consumer running" "not running" \
         "./.venv/bin/python workers/kafka_consumer.py & — without it no chat-history or audit row is ever written"
  fi
fi

# Undrained fallback. Non-empty lists mean events were produced while the
# broker or consumer was down; they carry a 7-day TTL and are written only on a
# consumer start, so a backlog that persists across a restart is data being
# lost on a timer rather than a transient blip.
if [[ "$MODE" == "docker" ]] && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^ainxt-redis$'; then
  backlog="$(docker exec ainxt-redis redis-cli -n 5 --scan --pattern 'kafka:fallback:*' 2>/dev/null | grep -c . | head -1)"
  backlog="${backlog:-0}"
  if [[ "${backlog:-0}" -eq 0 ]]; then
    pass "no undrained fallback events" "kafka:fallback:* empty"
  else
    warno "undrained fallback events" "$backlog topic list(s) queued in Redis DB 5" \
          "start the consumer to drain them: ${COMPOSE[*]} up -d kafka-consumer (they expire after 7 days)"
  fi
else
  skip "undrained fallback events" "needs the bundled redis container to inspect"
fi

# ── 4. database ──────────────────────────────────────────────────────────────
section "Database"

# The platform's tables live in the `ainxt` schema, not `public`. Counting
# `public` returns 0 on a perfectly good install and reads as catastrophe.
PSQL=()
if [[ "$MODE" == "docker" ]] && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^ainxt-postgres$'; then
  PSQL=(docker exec ainxt-postgres sh -c)
fi

run_sql() {
  local q="$1"
  if [[ ${#PSQL[@]} -gt 0 ]]; then
    docker exec ainxt-postgres sh -c "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tAc \"$q\"" 2>/dev/null
  elif have psql; then
    PGPASSWORD="$(envval POSTGRES_PASSWORD)" psql -h localhost -p "$POSTGRES_PORT" \
      -U "$(envval POSTGRES_USER || echo ainxt)" -d "$(envval POSTGRES_DB || echo ainxt)" -tAc "$q" 2>/dev/null
  else
    return 1
  fi
}

tables="$(run_sql "SELECT count(*) FROM information_schema.tables WHERE table_schema='ainxt'" | tr -d ' \r')"
if [[ -z "$tables" ]]; then
  fail "postgres reachable" "no answer" "check the container: ${COMPOSE[*]:-docker compose} logs --tail 50 postgres"
elif [[ "$tables" -lt 50 ]]; then
  fail "migrations applied" "only $tables tables in schema 'ainxt'" \
       "run them and read the output: docker compose exec gateway python db/migrate.py"
else
  pass "postgres reachable"
  pass "migrations applied" "$tables tables in schema 'ainxt'"
fi

if [[ -n "$tables" && "$tables" -ge 50 ]]; then
  vec="$(run_sql "SELECT extname FROM pg_extension WHERE extname='vector'" | tr -d ' \r')"
  if [[ "$vec" == "vector" ]]; then pass "pgvector extension" "installed"
  else fail "pgvector extension" "missing" "CREATE EXTENSION vector; — embeddings and RAG cannot work without it"; fi

  users="$(run_sql "SELECT count(*) FROM ainxt.users" | tr -d ' \r')"
  if [[ -n "$users" && "$users" -gt 0 ]]; then pass "admin user seeded" "$users user(s)"
  else fail "admin user seeded" "users table empty" "restart the gateway — it creates the first admin on boot"; fi

  conns="$(run_sql "SELECT count(*) FROM ainxt.connector_definitions" | tr -d ' \r')"
  if [[ -n "$conns" && "$conns" -gt 0 ]]; then pass "connectors seeded" "$conns definitions"
  else warno "connectors seeded" "none" "seeding runs inside db/migrate.py; connectors will be unavailable"; fi
fi

# ── 5. API ───────────────────────────────────────────────────────────────────
section "API"

body="$(http "$API/ainxt/v1/api/health" 10)"
if [[ "$(http_code)" != "200" ]]; then
  fail "gateway /health" "HTTP $(http_code)" \
       "is it up? ${COMPOSE[*]:-docker compose} logs --tail 80 gateway"
else
  status="$(jqf status)"
  case "$status" in
    ok|healthy)  pass "gateway /health" "$status" ;;
    degraded)    warno "gateway /health" "degraded" "usually just optional services — see the lines below" ;;
    *)           fail "gateway /health" "${status:-unparseable}" "${COMPOSE[*]:-docker compose} logs --tail 80 gateway" ;;
  esac

  for c in postgres redis; do
    v="$(jqf "checks.$c")"
    if [[ "$v" == "ok" ]]; then pass "  check: $c" "ok"
    else fail "  check: $c" "${v:-missing}" "the API cannot reach $c — check its container and .env host settings"; fi
  done

  # Per-logical-DB KV probe. Reported individually because a single bad DB index
  # is otherwise invisible behind an "ok" redis check.
  kv_bad="$(python3 -c "
import json
try: d=json.load(open('/tmp/.doctor_body'))
except Exception: raise SystemExit
kv=d.get('checks',{}).get('kv')
if isinstance(kv,dict):
    bad=[k for k,v in kv.items() if not (isinstance(v,dict) and v.get('ok'))]
    print(f'{len(kv)-len(bad)}/{len(kv)}' + ('|'+','.join(bad) if bad else ''))
" 2>/dev/null)"
  if [[ -n "$kv_bad" ]]; then
    counts="${kv_bad%%|*}"; badlist="${kv_bad#*|}"
    if [[ "$kv_bad" == *"|"* ]]; then
      fail "  check: KV logical databases" "$counts ok, failing: $badlist" \
           "each maps to a Redis DB index; confirm Redis accepts the configured DB count"
    else
      pass "  check: KV logical databases" "$counts ok"
    fi
  fi

  for opt in embed_svc ollama docker injection_svc; do
    v="$(jqf "checks.$opt")"
    [[ -z "$v" ]] && continue
    if [[ "$v" == "ok" ]]; then pass "  optional: $opt" "ok"
    else skip "  optional: $opt" "$(printf '%s' "$v" | cut -c1-46)"; fi
  done

  # The single most valuable assertion here. Every login returned HTTP 500 for
  # weeks because four call sites imported a JWT constant that did not exist.
  # A rejection proves the auth path executes end to end; a 500 proves it does
  # not, and nothing else in this script would notice.
  #
  # The probe address is deliberately unregisterable (.invalid is reserved by
  # RFC 2606) so it can never match, and never lock out a real account.
  #
  # A 429 counts as success. The platform rate-limits failed logins, so running
  # this script several times in a row trips the limiter — and a 429 still proves
  # the request reached the auth layer and was handled rather than crashing.
  # Treating it as a failure would make the check report a fault that is actually
  # the rate limiter working. Set AINXT_DOCTOR_SKIP_LOGIN_PROBE=1 to skip it
  # entirely if you would rather not add failed attempts to your audit log.
  if [[ "${AINXT_DOCTOR_SKIP_LOGIN_PROBE:-0}" == "1" ]]; then
    skip "login path executes" "skipped by AINXT_DOCTOR_SKIP_LOGIN_PROBE"
  else
    code="$(curl -sS -m 10 -o /dev/null -w '%{http_code}' -X POST \
            -H 'Content-Type: application/json' \
            -d '{"email":"doctor-probe@example.invalid","password":"deliberately-wrong"}' \
            "$API/ainxt/v1/api/auth/login" 2>/dev/null)"
    case "$code" in
      401|400|422) pass "login path executes" "rejects bad credentials with $code" ;;
      429)         pass "login path executes" "rate-limited (429) — the path runs; limiter is active" ;;
      500|502|503) fail "login path executes" "HTTP $code on a bad-credential login" \
                        "the auth path is raising, not rejecting: ${COMPOSE[*]:-docker compose} logs --tail 80 gateway | grep -i traceback" ;;
      200)         fail "login path executes" "HTTP 200 for deliberately wrong credentials" \
                        "authentication is not actually checking the password — do not expose this instance" ;;
      *)           warno "login path executes" "HTTP ${code:-000}" "unexpected; check the gateway logs" ;;
    esac
  fi

  ops="$(http "$API/openapi.json" 20 >/dev/null; python3 -c "
import json
try: d=json.load(open('/tmp/.doctor_body'))
except Exception: raise SystemExit
print(sum(len([m for m in v if m in ('get','post','put','patch','delete')]) for v in d.get('paths',{}).values()))
" 2>/dev/null)"
  if [[ -n "$ops" && "$ops" -gt 100 ]]; then pass "API surface published" "$ops operations"
  elif [[ -n "$ops" ]]; then warno "API surface published" "only $ops operations" "some routers may have failed to import — check the gateway log for ImportError"
  else warno "API surface published" "could not read /openapi.json" ""; fi
fi

# ── 6. web UI ────────────────────────────────────────────────────────────────
section "Web UI"

http "$UI/portal/" 10 >/dev/null
if [[ "$(http_code)" == "200" ]]; then
  pass "portal reachable" "$UI/portal/"
  # The built bundle, not the dev placeholder. An index.html that loads no JS is
  # what a failed `npm ci` leaves behind, and it still answers 200.
  if grep -qE '<script[^>]+src="[^"]*assets/' /tmp/.doctor_body 2>/dev/null; then
    pass "UI bundle built" "index.html references a built asset"
  else
    fail "UI bundle built" "no built asset referenced" \
         "the UI never compiled: ${COMPOSE[*]:-docker compose} logs --tail 60 ai-ui"
  fi
  for asset in ainxt-mark.svg ainxt-wordmark.svg favicon.ico; do
    http "$UI/portal/$asset" 6 >/dev/null
    ct="$(curl -sS -m 6 -o /dev/null -w '%{content_type}' "$UI/portal/$asset" 2>/dev/null)"
    if [[ "$(http_code)" == "200" && "$ct" != text/html* ]]; then
      pass "  asset $asset" "$ct"
    else
      warno "  asset $asset" "served as ${ct:-nothing} (HTTP $(http_code))" \
            "branding will fall back to text; check ai-ui/public/"
    fi
  done
else
  fail "portal reachable" "HTTP $(http_code) at $UI/portal/" \
       "check the UI container, or the UI_PORT setting: ${COMPOSE[*]:-docker compose} logs --tail 60 ai-ui"
fi

# ── 7. optional features ─────────────────────────────────────────────────────
section "Optional features"

for spec in "ENABLE_COACH:AiNxt Coach" "ENABLE_DISCUSSIONS:Discussions"; do
  var="${spec%%:*}"; label="${spec##*:}"
  if [[ "$(envval "$var" | tr 'A-Z' 'a-z')" == "true" ]]; then pass "$label" "enabled"
  else skip "$label" "off — set $var=true in .env to show it"; fi
done

# Google sign-in. The OAuth credentials live in the SYSTEM environment, not in
# .env (see .env.example for why), so check the process environment FIRST and
# only then fall back to the file — envval() reads .env alone and would report
# a correctly configured machine as broken.
#
# Worth its own check because the failure is silent: with the flag on but a
# credential missing, /auth/ui-config simply reports the feature as unavailable
# and the button never renders, with nothing on screen to say why. The usual
# cause is a terminal opened before the variable was set.
gval() { local v="${!1:-}"; [[ -n "$v" ]] && { printf '%s' "$v"; return 0; }; envval "$1"; }

# Is the variable in the Windows user's registry Environment key even though
# this shell cannot see it? Two different situations produce that, and they
# need opposite advice — saying only "not set" sends people off to re-add a
# variable that is already there:
#
#   Native Windows (Git Bash / PowerShell) — a process copies its environment
#   AT LAUNCH, so a `setx` value is invisible to every terminal and IDE that
#   was already running. Fix: reopen the terminal.
#
#   WSL — a Linux environment that never reads the Windows registry at all.
#   Reopening the terminal changes nothing. Windows only hands a variable to
#   WSL if it is named in WSLENV. Fix: add it to WSLENV, or just export it in
#   ~/.bashrc and drop the Windows side entirely.
is_wsl() { [[ -n "${WSL_DISTRO_NAME:-}" ]] || grep -qi microsoft /proc/version 2>/dev/null; }

_reg_query() {   # $1 = value name under HKCU\Environment
  local bin=""
  # WSL reaches Windows tools as reg.exe via interop; Git Bash has plain `reg`.
  command -v reg.exe >/dev/null 2>&1 && bin=reg.exe
  [[ -z "$bin" ]] && command -v reg >/dev/null 2>&1 && bin=reg
  [[ -z "$bin" ]] && return 1
  if is_wsl; then
    "$bin" query 'HKCU\Environment' /v "$1" 2>/dev/null
  else
    # MSYS rewrites a leading /v into a path; // is the documented escape.
    "$bin" query 'HKCU\Environment' //v "$1" 2>/dev/null
  fi
}
in_user_registry() { _reg_query "$1" | grep -qi REG_SZ; }
in_wslenv()        { _reg_query WSLENV | grep -q "$1"; }

if [[ "$(gval ENABLE_GOOGLE_LOGIN | tr 'A-Z' 'a-z')" == "true" ]]; then
  g_id="$(gval GOOGLE_CLIENT_ID)"; g_secret="$(gval GOOGLE_CLIENT_SECRET)"
  if [[ -n "$g_id" && -n "$g_secret" ]]; then
    pass "Google sign-in" "configured"
  else
    missing=""; in_win=""; not_forwarded=""
    for v in GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET; do
      [[ -n "$(gval "$v")" ]] && continue
      missing="${missing:+$missing and }$v"
      if in_user_registry "$v"; then
        in_win="${in_win:+$in_win and }$v"
        in_wslenv "$v" || not_forwarded="${not_forwarded:+$not_forwarded and }$v"
      fi
    done

    if is_wsl && [[ -n "$in_win" ]]; then
      if [[ -n "$not_forwarded" ]]; then
        warno "Google sign-in" "$in_win set in Windows, not forwarded to WSL" \
              "You are running under WSL, which does not read Windows environment variables. $not_forwarded is missing from WSLENV, so it never reaches this shell or any docker compose started from it. Either (a) in Windows PowerShell: setx WSLENV \"\$env:WSLENV;GOOGLE_CLIENT_ID/u:GOOGLE_CLIENT_SECRET/u\" then run 'wsl --shutdown' and reopen, or (b) simpler — export them in ~/.bashrc inside WSL and ignore the Windows copy."
      else
        warno "Google sign-in" "$in_win in WSLENV, but this WSL session is older" \
              "$in_win is set in Windows and listed in WSLENV, but WSLENV is applied when the WSL session STARTS and this one predates it. Run 'wsl --shutdown' from PowerShell, open a new terminal, then: docker compose up -d gateway"
      fi
    elif [[ -n "$in_win" ]]; then
      warno "Google sign-in" "$in_win set in Windows, but not in this shell" \
            "$in_win exists in your user environment variables, but this terminal was opened BEFORE it was added so it cannot see them — and neither can any docker compose you run from here, which is what makes the sign-in button disappear. Close this terminal, open a new one, and re-run: docker compose up -d gateway"
    elif is_wsl; then
      warno "Google sign-in" "$missing not set in WSL" \
            "ENABLE_GOOGLE_LOGIN=true but $missing is missing, so the sign-in button stays hidden. You are under WSL: add 'export $missing=...' to ~/.bashrc, then 'source ~/.bashrc' — see .env.example."
    else
      warno "Google sign-in" "$missing not set" \
            "ENABLE_GOOGLE_LOGIN=true but $missing is missing, so the sign-in button stays hidden. Set it in your system environment, then open a NEW terminal before running docker compose — see .env.example."
    fi
  fi
else
  skip "Google sign-in" "off — set ENABLE_GOOGLE_LOGIN=true in .env"
fi

if [[ "$MODE" == "docker" && ${#COMPOSE[@]} -gt 0 ]]; then
  models="$(docker exec ainxt-ollama ollama list 2>/dev/null | tail -n +2 | grep -c . )"
  if [[ -n "$models" && "$models" -gt 0 ]]; then pass "local models pulled" "$models model(s)"
  else skip "local models pulled" "none — docker compose exec ollama ollama pull llama3.2"; fi
fi

# Desktop features (Buddy, Code) spawn this binary. Informational: a web-only
# install neither needs nor benefits from it.
cli=""
for c in "${BUDDY_CLI_BIN:-}" "${AINXT_BIN_DIR:-$HOME/.ainxt/bin}/ainxt"; do
  [[ -n "$c" && -x "$c" ]] && { cli="$c"; break; }
done
[[ -z "$cli" ]] && have ainxt && cli="$(command -v ainxt)"
if [[ -n "$cli" ]]; then
  pass "AiNxt CLI present" "$cli"
else
  skip "AiNxt CLI present" "not installed — only needed for Buddy and Code in the desktop app"
fi

# ── summary ──────────────────────────────────────────────────────────────────
rm -f /tmp/.doctor_body /tmp/.doctor_code /tmp/.doctor_topics

if [[ "$JSON" == "yes" ]]; then
  printf '{"mode":"%s","summary":{"pass":%d,"fail":%d,"warn":%d,"skip":%d},"checks":[' \
    "$MODE" "$PASS" "$FAIL" "$WARN" "$SKIP"
  first=1
  for row in "${JSON_ROWS[@]}"; do
    [[ $first -eq 1 ]] && first=0 || printf ','
    printf '%s' "$row"
  done
  printf ']}\n'
else
  printf '\n%s%s' "$B" "$CYN"
  printf '──────────────────────────────────────────────────────────────\n'
  printf '%s' "$R"
  printf '  %s%d passed%s   %s%d failed%s   %s%d warnings%s   %s%d not enabled%s\n' \
    "$GRN" "$PASS" "$R" "$([[ $FAIL -gt 0 ]] && printf '%s' "$RED" || printf '%s' "$DIM")" "$FAIL" "$R" \
    "$YLW" "$WARN" "$R" "$DIM" "$SKIP" "$R"
  if [[ $FAIL -eq 0 ]]; then
    printf '\n  %s✓ Nothing required is broken.%s\n' "$GRN" "$R"
    [[ $WARN -gt 0 ]] && printf '  %sWarnings above are worth reading but do not stop the platform.%s\n' "$DIM" "$R"
    printf '\n  Open %s%s/portal/%s\n\n' "$B" "$UI" "$R"
  else
    printf '\n  %s✗ %d required check(s) failed:%s\n' "$RED" "$FAIL" "$R"
    for t in "${FAILED_TITLES[@]}"; do printf '      • %s\n' "$t"; done
    printf '\n  %sEach line above has a → with the command to run.%s\n\n' "$DIM" "$R"
  fi
fi

[[ $FAIL -eq 0 ]] || exit 1
exit 0
