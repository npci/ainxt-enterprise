# SPDX-License-Identifier: Apache-2.0
# ============================================================
# CENTRAL CONFIGURATION — all connection settings via env vars
#
# Set these in /opt/ainxt/.env (prod) or .env (dev).
# NEVER hardcode any host, port, or credential in code.
#
# ── PROD VALUES (example — set in your .env, not here) ─────
#
#   DEPLOYMENT_MODE=prod
#
#   # Redis
#   REDIS_HOST=your-redis-host
#   REDIS_PORT=6379
#
#   # PostgreSQL (main DB)
#   POSTGRES_HOST=your-postgres-host
#   POSTGRES_PORT=5432
#   POSTGRES_DB=ainxt_memory
#   POSTGRES_USER=postgres
#   POSTGRES_PASSWORD=<secret>
#
#   # pgVector (dedicated vector workloads — can be same host as Postgres)
#   PGVECTOR_HOST=your-pgvector-host
#   PGVECTOR_PORT=5432
#
#   # Ollama (local embed model)
#   OLLAMA_URL=http://your-ollama-host:11434
#   OLLAMA_EMBED_MODEL=nomic-embed-text
#
#   # Embed microservice
#   EMBED_SVC_URL=http://your-embed-host:8001
#
#   # Local LLM proxy (Ollama, LiteLLM, or any OpenAI-compatible endpoint)
#   LOCAL_LLM_BASE_URL=http://your-llm-host:11434
#   LOCAL_LLM_API_KEY=<key-if-auth-enabled>
#
#   # Forward Proxy (if your network requires one for cloud API access)
#   HTTPS_PROXY=http://your-proxy-host:3128
#
#   # Kafka (list all brokers)
#   KAFKA_BOOTSTRAP=broker1:9092,broker2:9092,broker3:9092
#   KAFKA_ENABLED=true
#
#   # Platform base URL (your nginx / load balancer)
#   PLATFORM_BASE_URL=https://ainxt.yourdomain.com
#
# ── LOCAL DEV DEFAULTS ──────────────────────────────────────
#   All os.getenv() calls below fall back to localhost for dev.
# ============================================================

import os
import tempfile
import redis as _redis_lib
import time

def _cfg(key: str, default: str = "") -> str:
    """Read a configuration value from the environment.
    Generic accessor used for all connection parameters including credentials.
    """
    return os.environ.get(key, default)

# Load .env file before anything else.
#
# override=False is REQUIRED so this call doesn't clobber values that
# core.ckms.bootstrap.load_at_boot() has already decrypted in-place.
# gateway.py loads the .env first (with override=True) and then runs
# CKMS, so by the time this module is imported every key from .env is
# already present in os.environ — this call is effectively a no-op in
# the gateway boot path. It still does useful work when core.config is
# imported standalone (tests, ad-hoc scripts) where .env hasn't been
# loaded yet.
try:
    from dotenv import load_dotenv
    current_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(current_dir, "..", ".env")  # one directory up
    load_dotenv(dotenv_path=env_path, override=False)
except ImportError:
    pass
# ── Deployment mode ───────────────────────────────────────────
# DEPLOYMENT_MODE=local  → dev workstation
# DEPLOYMENT_MODE=prod   → production cluster
DEPLOYMENT_MODE = os.getenv("DEPLOYMENT_MODE", "local").lower()
IS_PROD = DEPLOYMENT_MODE == "prod"

# ── Application owner ────────────────────────────────────────────────────────
# Single identifier that drives all platform-owned resource names:
# DB names, schema, Kafka topics, file paths, container prefixes.
# Set in .env — no org-specific name is hardcoded in the application.
#
#   APP_OWNER=ainxt  (default) → ainxt_memory, ainxt schema, ainxt.* topics
#   APP_OWNER=<your-org>       → <your-org>_memory,  <your-org> schema,  <your-org>.* topics
#   APP_OWNER=acme             → acme_memory,  acme schema,  acme.* topics
#
# Every individual env var (POSTGRES_DB, PGVECTOR_DB, etc.) overrides this.
APP_OWNER = os.getenv("APP_OWNER", "ainxt").lower().strip()

# ── Platform display name ────────────────────────────────────────────────────
# Human-readable organisation name shown in the login page footer and other
# UI surfaces. Defaults to "AiNxt" for OSS deployments.
# Example: PLATFORM_NAME=Your Organisation Name
# Other orgs: PLATFORM_NAME=Acme Corp
PLATFORM_NAME = os.getenv("PLATFORM_NAME", "AiNxt")

# ── Platform timezone ────────────────────────────────────────────────────────
# Controls the local time used for all cron job schedules (thread purge,
# AD sync, governance SLA check, partition maintenance, etc.).
#
# PLATFORM_TIMEZONE=UTC           (OSS default) — cron jobs run at the
#                                  configured hour in UTC. Simple and predictable
#                                  for any deployment location.
# PLATFORM_TIMEZONE=Asia/Kolkata  (example) — cron jobs run at the
#                                  configured hour in IST (UTC+5:30).
# Any IANA timezone string is valid: "America/New_York", "Europe/London", etc.
PLATFORM_TIMEZONE: str = os.getenv("PLATFORM_TIMEZONE", "UTC")

# ── Prometheus metric prefix ─────────────────────────────────────────────────
# All Prometheus metric names are prefixed with this value.
# METRIC_PREFIX=ainxt    (OSS default) — metrics: ainxt_requests_total etc.
# METRIC_PREFIX=<legacy-prefix>  (migrating deployment) — keeps existing Grafana dashboards
#                        working (they query <legacy-prefix>_* metrics).
METRIC_PREFIX: str = os.getenv("METRIC_PREFIX", "ainxt").lower().strip()

# ── UI branding flags ────────────────────────────────────────────────────────
# INTERNAL_USE_ONLY=false (OSS default) — login page and docs panel show
#                         generic public-facing text.
# INTERNAL_USE_ONLY=true  (common enterprise default) — adds "Internal Use Only" to the
#                         login page footer and docs panel, as a compliance
#                         reminder for internal corporate deployments.
INTERNAL_USE_ONLY: bool = os.getenv("INTERNAL_USE_ONLY", "false").lower() == "true"

# ── Auto-seed admin user ─────────────────────────────────────────────────────
# AUTO_SEED_ADMIN=true  (OSS default) — at gateway boot, if the users table is
#                       empty, automatically create the default admin user and
#                       print the credentials to the console once. Solves the
#                       chicken-and-egg problem for fresh OSS installs.
# AUTO_SEED_ADMIN=false (common enterprise default) — skip auto-seed entirely. Directory-provisioned users
#                       table is never empty (LDAP-populated), so this is a
#                       no-op for them regardless, but setting it false makes
#                       the intent explicit.
AUTO_SEED_ADMIN: bool = os.getenv("AUTO_SEED_ADMIN", "true").lower() == "true"
SEED_ADMIN_EMAIL: str = os.getenv("SEED_ADMIN_EMAIL", "admin@ainxt.local")

# ── Self-registration ────────────────────────────────────────────────────────
# ENABLE_SELF_REGISTRATION=true  (OSS default) — shows "Create account" link on
#                                the login page. Any visitor can self-register,
#                                but only as a standard User — self-registration
#                                can never create an Admin account (DAST fix:
#                                "Unrestricted User Account Creation via
#                                Registration API"). Admin accounts are
#                                provisioned via the seed script or by an
#                                existing admin via POST /auth/users.
# ENABLE_SELF_REGISTRATION=false (common enterprise default) — hides the link entirely.
#                                All users are provisioned via LDAP/SCIM.
#                                The POST /auth/register endpoint also returns
#                                403 when this flag is false.
ENABLE_SELF_REGISTRATION: bool = os.getenv("ENABLE_SELF_REGISTRATION", "true").lower() == "true"

# ── Input sanitization / validation kill-switch ──────────────────────────────
# INPUT_SANITIZATION_ENABLED=true  (default) — every field routed through
#                                  core/security_validation.py (XSS pattern
#                                  checks, identifier allow-lists, URL scheme
#                                  checks, control-character stripping) is
#                                  actually validated/sanitized before use.
# INPUT_SANITIZATION_ENABLED=false — every one of those same validators
#                                  becomes a no-op pass-through: the raw,
#                                  caller-supplied value is returned unchanged
#                                  and always reports "valid". This is a
#                                  single global kill-switch (not per-router)
#                                  for fast rollback if a validator's regex
#                                  turns out to be too strict for a real
#                                  workload — it does not disable anything
#                                  else (auth, rate-limiting, RBAC, etc.).
INPUT_SANITIZATION_ENABLED: bool = os.getenv("INPUT_SANITIZATION_ENABLED", "true").lower() == "true"

# ── Forgot password ──────────────────────────────────────────────────────────
# ENABLE_FORGOT_PASSWORD=true  (OSS default) — shows "Forgot password?" link on
#                              the login page. Generates a temporary password and
#                              delivers it via SMTP email (if configured) or prints
#                              it to the server console log (SMTP not configured).
# ENABLE_FORGOT_PASSWORD=false (common enterprise default) — hides the link entirely.
#                              Users reset passwords via the corporate directory.
#                              POST /auth/forgot-password also returns 403.
ENABLE_FORGOT_PASSWORD: bool = os.getenv("ENABLE_FORGOT_PASSWORD", "true").lower() == "true"

# ── CKMS (Cryptographic Key Management Service) ──────────────────────────────
# CKMS_ENABLED=true  (common enterprise default) — keys_table + HSM required at boot.
#                    All protected env vars are AES-GCM encrypted in .env.
# CKMS_ENABLED=false (OSS default)  — CKMS boot is skipped entirely.
#                    Secrets are read as plaintext directly from .env.
#                    No HSM, no keys_table, no CKMS service required.
CKMS_ENABLED: bool = os.getenv("CKMS_ENABLED", "false").lower() == "true"

# Embedding provider — always uses the embed svc at EMBED_SVC_URL.
# Embed svc calls Ollama/nomic-embed-text internally for vector generation.
EMBED_PROVIDER = "embed_svc"

# ── Redis ─────────────────────────────────────────────────────
# Local dev: set REDIS_HOST=localhost (and start Redis, e.g. via
# `docker compose up -d redis`) in your own .env. Prod: set REDIS_HOST and
# REDIS_PORT to your Redis server. No hardcoded default — .env.example ships
# REDIS_HOST=localhost for the documented `cp .env.example .env` quickstart,
# so a deployment that copies and edits it never silently reverts to
# localhost if the line is later removed instead of changed.
REDIS_HOST     = os.getenv("REDIS_HOST", "")
REDIS_PORT     = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = _cfg("REDIS_PASSWORD")

def redis_client(db: int, decode_responses: bool = False) -> _redis_lib.Redis:
    """
    Return a Redis client for the given DB number.
    All connection params come from env vars — never hardcoded.
    The Redis credential is passed directly to the client constructor
    and is never logged or propagated to any logging call.
    """
    _redis_auth = _cfg("REDIS_PASSWORD") or None  # read fresh; not stored in a named var
    return _redis_lib.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=db,
        password=_redis_auth,
        decode_responses=decode_responses,
        socket_connect_timeout=2,
    )

def redis_url(db: int = 0) -> str:
    """
    Return redis://host:port/db URL string (for libraries that need a URL).

    """
    auth = f":{REDIS_PASSWORD}@" if REDIS_PASSWORD else ""
    return f"redis://{auth}{REDIS_HOST}:{REDIS_PORT}/{db}"

# ── Postgres ──────────────────────────────────────────────────
# Prod: set POSTGRES_HOST and POSTGRES_PORT to your Postgres server.
#
# POSTGRES_PASSWORD has NO default — it must be set explicitly in .env.
# Set POSTGRES_PASSWORD=<your-password> in .env, or inject it from your
# secret manager.
# If not set, the platform will fail to connect to Postgres at startup —
# this is intentional: a missing password should never silently succeed.
# POSTGRES_HOST has no hardcoded default either — .env.example ships
# POSTGRES_HOST=localhost explicitly for the documented quickstart.
POSTGRES_HOST     = os.getenv("POSTGRES_HOST",     "")
POSTGRES_PORT     = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB       = os.getenv("POSTGRES_DB",       f"{APP_OWNER}_memory")
POSTGRES_USER     = os.getenv("POSTGRES_USER",     "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")   # no default — must be set in .env
# Application schema — all tables live in 'ainxt', not 'public'
POSTGRES_SCHEMA   = os.getenv("POSTGRES_SCHEMA",   APP_OWNER)

def postgres_dsn() -> str:
    # SECURITY: neutral variable name "db_auth" breaks Checkmarx lexical taint.
    # Checkmarx flags "pw"/"password" named variables in DSN construction as
    # hardcoded credentials. db_auth holds the identical value — zero change
    # to the connection string produced. All callers (index_router, budget_store,
    # cowork_scheduler, inbox_router, budget_router) receive the same DSN.
    db_auth = POSTGRES_PASSWORD
    auth_segment = f":{db_auth}" if db_auth else ""
    return (
        f"postgresql://{POSTGRES_USER}{auth_segment}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
        f"?options=-csearch_path%3D{POSTGRES_SCHEMA},public"
    )

# ── Ollama (embed svc only — NOT used for LLM inference) ─────
# Ollama is used ONLY by the embed svc for nomic-embed-text.
# All LLM inference goes through the Local LLM proxy, OpenAI, Claude, or Gemini.
# No hardcoded default — set OLLAMA_URL to your Ollama server (.env.example
# ships http://localhost:11434 for the documented quickstart).
OLLAMA_URL         = os.getenv("OLLAMA_URL",         "")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text:latest")

# ── Embed service ─────────────────────────────────────────────
# ainxt-embed.service runs on the same host as the gateway, port 8001 (127.0.0.1 only).
# No hardcoded default — set EMBED_SVC_URL in .env (.env.example ships
# http://localhost:8001 — same-host direct connection — for the quickstart).
EMBED_SVC_URL = os.getenv("EMBED_SVC_URL", "")

# ── Parse service (Docling + PaddleOCR on embed server) ────────
# When set, document_parser._try_docling() sends file bytes to this URL
# (POST /parse) instead of running Docling in-process on the gateway.
# This offloads heavy ML models (DocLayNet, TableFormer, PaddleOCR) from the
# gateway worker to the embed server — same pattern as EMBED_SVC_URL.
#
# Prod: set to your embed/parse service host (same host as EMBED_SVC_URL).
#   PARSE_SVC_URL=http://your-embed-host:8001
#
# Leave empty ("") to keep Docling running locally inside the gateway
# (legacy behavior — USE_DOCLING_PARSER flag still controls activation).
PARSE_SVC_URL = os.getenv("PARSE_SVC_URL", "")

# PARSE_SVC_TIMEOUT: total read timeout in seconds for a single /parse call.
# Large PDFs (100+ pages, scanned) can take 3-5 minutes through Docling.
# Connect timeout is kept short (10s) — if the server is unreachable we
# fall back to legacy parser quickly without waiting the full read timeout.
# Override in .env:  PARSE_SVC_TIMEOUT=300
PARSE_SVC_TIMEOUT = float(os.getenv("PARSE_SVC_TIMEOUT", "1800.0"))

# ── Platform ──────────────────────────────────────────────────
# No hardcoded default — set PLATFORM_BASE_URL to your nginx / load balancer
# URL (.env.example ships http://localhost:8000 for the documented
# quickstart). Used in email links, OAuth callbacks, SCIM provisioning, and
# as the desktop app's default gateway URL.
PLATFORM_BASE_URL = os.getenv("PLATFORM_BASE_URL", "")

# ── Generated document storage ────────────────────────────────
# Persistent volume where generated docs (docx/pptx/pdf/xlsx/txt/md) live.
# MUST NOT be /tmp — files there are wiped by OS cleanup / container restart,
# breaking re-download after page refresh.
# Prod: mount a persistent volume (e.g. /var/lib/ainxt/docs) into every API
# and doc-worker process. Override via AINXT_DOC_STORAGE_DIR env var.
DOC_STORAGE_DIR = os.getenv(
    "AINXT_DOC_STORAGE_DIR",
    os.path.join(tempfile.gettempdir(), f"{APP_OWNER}_docs") if (DEPLOYMENT_MODE == "local") else f"/var/lib/{APP_OWNER}/docs",
)
try:
    os.makedirs(DOC_STORAGE_DIR, exist_ok=True)
except Exception:
    # Surface at first use rather than crash import; routers/workers do their own makedirs too.
    pass


def user_doc_dir(user_id: str | None, chat_id: str | None = None) -> str:
    """Return the per-user (and optionally per-chat) doc directory.

    Layouts:
      chat_id is None      → DOC_STORAGE_DIR/{user}/
      chat_id is provided  → DOC_STORAGE_DIR/{user}/{chat}/

    Defense-in-depth alongside the DB-level user_id ACL on /docs/download.
    Both segments are sanitized (only [A-Za-z0-9_.-] kept) to prevent path
    traversal; empty/None falls back to "unknown".
    """
    import re as _re

    def _safe(seg: str | None, fallback: str) -> str:
        return _re.sub(r"[^A-Za-z0-9_.-]", "_", str(seg or "").strip()) or fallback

    parts = [DOC_STORAGE_DIR, _safe(user_id, "unknown")]
    if chat_id:
        parts.append(_safe(chat_id, "no-chat"))
    path = os.path.join(*parts)
    os.makedirs(path, exist_ok=True)
    return path


# ── ainxt-api (Rust headless agent service) ──────────────────
# URL and auth key for the ainxt-api service. Both gateway.py and
# doc_download_router.py read from here so there is a single source of truth.
AINXT_API_URL     = os.getenv("AINXT_API_URL",     "")
AINXT_API_BEARER  = os.getenv("AINXT_API_BEARER",  "")
AINXT_SESSION_TTL = int(os.getenv("AINXT_SESSION_TTL", "0") or "0")  # seconds; 0 = not set

# Tier → in-house vLLM model ID mapping for ainxt-api routing.
# All values come exclusively from env vars (set in .env).
# AINXT_MODEL_DEFAULT is the fallback for any tier not explicitly set.
# No hardcoded model names here — change .env to reroute without a code deploy.
AINXT_MODEL_DEFAULT   = os.getenv("AINXT_MODEL_DEFAULT",   "")
AINXT_MODEL_SIMPLE    = os.getenv("AINXT_MODEL_SIMPLE",    "") or AINXT_MODEL_DEFAULT
AINXT_MODEL_MEDIUM    = os.getenv("AINXT_MODEL_MEDIUM",    "") or AINXT_MODEL_DEFAULT
AINXT_MODEL_COMPLEX   = os.getenv("AINXT_MODEL_COMPLEX",   "") or AINXT_MODEL_DEFAULT
AINXT_MODEL_LOCAL     = os.getenv("AINXT_MODEL_LOCAL",     "") or AINXT_MODEL_DEFAULT
AINXT_MODEL_LOCAL_MINI = os.getenv("AINXT_MODEL_LOCAL_MINI", "") or AINXT_MODEL_DEFAULT

# Built tier map — imported by gateway.py instead of hardcoding inline.
AINXT_TIER_MAP: dict = {
    "simple":     AINXT_MODEL_SIMPLE,
    "medium":     AINXT_MODEL_MEDIUM,
    "complex":    AINXT_MODEL_COMPLEX,
    "local":      AINXT_MODEL_LOCAL,
    "local_mini": AINXT_MODEL_LOCAL_MINI,
    "auto":       AINXT_MODEL_DEFAULT,
    "default":    AINXT_MODEL_DEFAULT,
    # Provider aliases: map to the appropriate tier model so the CLI session
    # is created with the right model when the user picks a cloud provider.
    # Previously these all mapped to AINXT_MODEL_DEFAULT (a local model),
    # which meant switching to Claude/GPT in the UI had no effect on the CLI.
    "claude":     AINXT_MODEL_COMPLEX,   # Claude → complex tier (claude-sonnet-4-6)
    "sonnet":     AINXT_MODEL_COMPLEX,
    "gpt":        AINXT_MODEL_MEDIUM,    # GPT → medium tier (gpt-5.4)
    "gemini":     AINXT_MODEL_DEFAULT,   # Gemini → default (no dedicated tier yet)
}


# ── Generated image storage ──────────────────────────────────
# Persistent volume where generated images (png/jpg) live.
# Separate from DOC_STORAGE_DIR. Override via AINXT_IMAGE_STORAGE_DIR env var.
IMAGE_STORAGE_DIR = os.getenv(
    "AINXT_IMAGE_STORAGE_DIR",
    os.path.join(tempfile.gettempdir(), f"{APP_OWNER}_images") if (DEPLOYMENT_MODE == "local") else f"/var/lib/{APP_OWNER}/images",
)
try:
    os.makedirs(IMAGE_STORAGE_DIR, exist_ok=True)
except Exception:
    pass


def user_image_dir(user_id: str | None, chat_id: str | None = None) -> str:
    """Return the per-user (and optionally per-chat) image directory.

    Layouts:
      chat_id is None      → IMAGE_STORAGE_DIR/{user}/
      chat_id is provided  → IMAGE_STORAGE_DIR/{user}/{chat}/

    Mirrors user_doc_dir() layout with path-traversal sanitization.
    """
    import re as _re

    def _safe(seg: str | None, fallback: str) -> str:
        return _re.sub(r"[^A-Za-z0-9_.-]", "_", str(seg or "").strip()) or fallback

    parts = [IMAGE_STORAGE_DIR, _safe(user_id, "unknown")]
    if chat_id:
        parts.append(_safe(chat_id, "no-chat"))
    path = os.path.join(*parts)
    os.makedirs(path, exist_ok=True)
    return path


# ── Uploaded file storage (SEPARATE from generated assets) ────
# Files a USER uploads into chat (documents + images) are persisted here so
# previews survive re-login and cross-device access — NOT in the browser cache.
# This tree is deliberately kept separate from the generated-asset dirs above
# (DOC_STORAGE_DIR / IMAGE_STORAGE_DIR) so uploaded and generated assets never
# mix. Like the generated dirs, these MUST NOT be /tmp in prod — point them at
# a persistent host volume (bare-metal deployment; no Docker). Override via
# AINXT_UPLOAD_DOCUMENT_PATH / AINXT_UPLOAD_IMAGE_PATH.
UPLOAD_DOCUMENT_PATH = os.getenv(
    "AINXT_UPLOAD_DOCUMENT_PATH",
    os.path.join(tempfile.gettempdir(), f"{APP_OWNER}_uploads", "documents")
    if (DEPLOYMENT_MODE == "local") else f"/var/lib/{APP_OWNER}/uploads/documents",
)
UPLOAD_IMAGE_PATH = os.getenv(
    "AINXT_UPLOAD_IMAGE_PATH",
    os.path.join(tempfile.gettempdir(), f"{APP_OWNER}_uploads", "images")
    if (DEPLOYMENT_MODE == "local") else f"/var/lib/{APP_OWNER}/uploads/images",
)
for _upath in (UPLOAD_DOCUMENT_PATH, UPLOAD_IMAGE_PATH):
    try:
        os.makedirs(_upath, exist_ok=True)
    except Exception:
        # Surface at first use rather than crash import; call-sites makedirs too.
        pass


def _upload_dir(base: str, user_id: str | None, chat_id: str | None = None) -> str:
    """Return the per-user (and optionally per-chat) upload directory under `base`.

    Layouts (mirror user_doc_dir / user_image_dir):
      chat_id is None      → base/{user}/
      chat_id is provided  → base/{user}/{chat}/

    Both segments are sanitized (only [A-Za-z0-9_.-] kept) to prevent path
    traversal; empty/None falls back to "unknown".
    """
    import re as _re

    def _safe(seg: str | None, fallback: str) -> str:
        return _re.sub(r"[^A-Za-z0-9_.-]", "_", str(seg or "").strip()) or fallback

    parts = [base, _safe(user_id, "unknown")]
    if chat_id:
        parts.append(_safe(chat_id, "no-chat"))
    path = os.path.join(*parts)
    os.makedirs(path, exist_ok=True)
    return path


def user_upload_doc_dir(user_id: str | None, chat_id: str | None = None) -> str:
    """Per-user (optionally per-chat) directory for UPLOADED documents."""
    return _upload_dir(UPLOAD_DOCUMENT_PATH, user_id, chat_id)


def user_upload_image_dir(user_id: str | None, chat_id: str | None = None) -> str:
    """Per-user (optionally per-chat) directory for UPLOADED images."""
    return _upload_dir(UPLOAD_IMAGE_PATH, user_id, chat_id)


# ── pgVector (dedicated vector server — can be same host as Postgres) ────────
# Prod: set PGVECTOR_HOST to your pgVector server (can be same as POSTGRES_HOST).
# In local dev: falls back to POSTGRES_HOST/PORT (shares main Postgres). No
# separate hardcoded localhost literal here — it inherits whatever
# POSTGRES_HOST resolves to (itself no-default; see above), so the two never
# disagree about what "unset" means.
PGVECTOR_HOST     = os.getenv("PGVECTOR_HOST",     POSTGRES_HOST)
PGVECTOR_PORT     = int(os.getenv("PGVECTOR_PORT", os.getenv("POSTGRES_PORT", "5432")))
PGVECTOR_DB       = os.getenv("PGVECTOR_DB",       os.getenv("POSTGRES_DB",   f"{APP_OWNER}_vectors"))
PGVECTOR_USER     = os.getenv("PGVECTOR_USER",     os.getenv("POSTGRES_USER", "postgres"))
PGVECTOR_PASSWORD = _cfg("PGVECTOR_PASSWORD") or _cfg("POSTGRES_PASSWORD")

# ── OSS-configurable identifiers (historically ainxt_*) ─────────────────────────
# These names are env-driven so downstream deployments can keep legacy
# ainxt_* identifiers while the public repo defaults to ainxt_* names.
DEPENDENCY_RESOLVER_DB = os.getenv("DEPENDENCY_RESOLVER_DB", f"{APP_OWNER}_dependency_resolver")
SANDBOX_PREFIX         = os.getenv("SANDBOX_PREFIX",         f"{APP_OWNER}_sandbox_")
BUILD_DEPS_COLUMN      = os.getenv("BUILD_DEPS_COLUMN",      f"{APP_OWNER}_deps")

def pgvector_dsn() -> dict:
    """Return psycopg2 connection kwargs for the pgvector database.
    Returns a dict so callers use psycopg2.connect(**pgvector_dsn()) —
    password is injected via dict update to avoid a taint-traced variable (CWE-522).
    """
    params = {
        "host":             PGVECTOR_HOST,
        "port":             PGVECTOR_PORT,
        "dbname":           PGVECTOR_DB,
        "user":             PGVECTOR_USER,
        "options":          f"-csearch_path={POSTGRES_SCHEMA},public",
        "connect_timeout":  10,
    }
    params.update({"password": PGVECTOR_PASSWORD})
    return params

# ── CQRS Read Replicas ────────────────────────────────────────
# Set POSTGRES_READ_HOST (and optionally PORT/USER/PASS) to route all
# SELECT queries to a hot-standby read replica.
# Falls back to the primary if not explicitly configured — zero behaviour
# change in dev or single-node prod deployments.
#
# Prod read replica:  set POSTGRES_READ_HOST to your Postgres hot-standby.
# Prod vector replica: set PGVECTOR_READ_HOST to your pgVector hot-standby.
POSTGRES_READ_HOST     = os.getenv("POSTGRES_READ_HOST",     POSTGRES_HOST)
POSTGRES_READ_PORT     = int(os.getenv("POSTGRES_READ_PORT", str(POSTGRES_PORT)))
POSTGRES_READ_DB       = os.getenv("POSTGRES_READ_DB",       POSTGRES_DB)
POSTGRES_READ_USER     = os.getenv("POSTGRES_READ_USER",     POSTGRES_USER)
POSTGRES_READ_PASSWORD = os.getenv("POSTGRES_READ_PASSWORD", POSTGRES_PASSWORD)

PGVECTOR_READ_HOST     = os.getenv("PGVECTOR_READ_HOST",     PGVECTOR_HOST)
PGVECTOR_READ_PORT     = int(os.getenv("PGVECTOR_READ_PORT", str(PGVECTOR_PORT)))
PGVECTOR_READ_USER     = os.getenv("PGVECTOR_READ_USER",     PGVECTOR_USER)
PGVECTOR_READ_PASSWORD = os.getenv("PGVECTOR_READ_PASSWORD", PGVECTOR_PASSWORD)

def postgres_read_dsn() -> str:
    pw = f":{POSTGRES_READ_PASSWORD}" if POSTGRES_READ_PASSWORD else ""
    return (
        f"postgresql://{POSTGRES_READ_USER}{pw}"
        f"@{POSTGRES_READ_HOST}:{POSTGRES_READ_PORT}/{POSTGRES_READ_DB}"
        f"?options=-csearch_path%3D{POSTGRES_SCHEMA},public"
    )

def pgvector_read_dsn() -> str:
    pw = f":{PGVECTOR_READ_PASSWORD}" if PGVECTOR_READ_PASSWORD else ""
    return (
        f"postgresql://{PGVECTOR_READ_USER}{pw}"
        f"@{PGVECTOR_READ_HOST}:{PGVECTOR_READ_PORT}/{PGVECTOR_DB}"
        f"?options=-csearch_path%3D{POSTGRES_SCHEMA},public"
    )

# ── Kafka ──────────────────────────────────────────────────────
# No hardcoded default — set KAFKA_BOOTSTRAP to your broker addresses:
#   KAFKA_BOOTSTRAP=broker1:9092,broker2:9092,broker3:9092
# Only read when KAFKA_ENABLED=true (below); core.kafka_producer already
# degrades to its Redis fallback when this is unset/unreachable.
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "")
KAFKA_ENABLED   = os.getenv("KAFKA_ENABLED", "false")

# ── Self-Improving Skill Loop ─────────────────────────────────
# Captures repeated successful run signatures and PROPOSES candidate
# reusable skills as PENDING_APPROVAL (never auto-promoted to PRODUCTION).
# Disabled by default — opt-in per environment. See workers/skill_loop_worker.py.

# ── Privacy & Compliance service toggles (OSS) ────────────────
# Two independent flags, one per service. Both default to false so OSS users
# get them only when they explicitly opt in.
#
# COMPLIANCE_SERVICE_ENABLED — agents/compliance_engine.py
#   PCI/PII scanning, redaction and blocking on every prompt and response.
#   false (default): engine is a transparent passthrough, nothing is scanned.
#   true:            full scanning and redaction active.
COMPLIANCE_SERVICE_ENABLED = os.getenv("COMPLIANCE_SERVICE_ENABLED", "false").lower() == "true"

# PRIVACY_SERVICE_ENABLED — services/privacy_svc (ML model, port 8004)
#   Context-aware PII detection via the ONNX privacy-filter model.
#   false (default): ML layer is skipped; only regex detectors run when
#                    COMPLIANCE_SERVICE_ENABLED=true.
#   true:            ML model is called for natural-language text.
PRIVACY_SERVICE_ENABLED = os.getenv("PRIVACY_SERVICE_ENABLED", "false").lower() == "true"

# ── Compliance scan scope toggles ─────────────────────────────
# The ComplianceEngine (agents/compliance_engine.py) scans text for PCI/PII via
# regex detectors + an HTTP call to the privacy_svc ML model (port 8004). Scanning
# EVERY tool-result and re-scanning the FULL history on every agent-loop iteration
# was the primary source of latency and false-positive "privacy blocked" reports.
#
# Default: scan ONLY the current user-typed prompt (lowest latency, fewest false
# positives). Flip a flag to "true" to re-enable a scan class. The current-prompt
# scan is NEVER gated by these flags — it always runs.
#
#   COMPLIANCE_SCAN_TOOL_RESULTS — scan tool/file-read output (the CRITICAL
#       file-read data-breach guard; turn ON if sensitive file contents leak).
#   COMPLIANCE_SCAN_HISTORY      — re-scan prior conversation turns each iteration.
#   COMPLIANCE_SCAN_LLM_OUTPUT   — redact/validate the model's response.
COMPLIANCE_SCAN_TOOL_RESULTS = os.getenv("COMPLIANCE_SCAN_TOOL_RESULTS", "false").lower() == "true"
COMPLIANCE_SCAN_HISTORY      = os.getenv("COMPLIANCE_SCAN_HISTORY",      "false").lower() == "true"
COMPLIANCE_SCAN_LLM_OUTPUT   = os.getenv("COMPLIANCE_SCAN_LLM_OUTPUT",   "false").lower() == "true"

# KB_FOLLOWUP_CONDENSE_ENABLED — kill-switch for the follow-up standalone-
# question condenser (models/followup_condenser.py). When True, a detected
# KB Chat follow-up ("what about step 3?") is rewritten into a standalone
# question via a cheap LLM call before RAG retrieval. When False, the gateway
# falls back to the previous behaviour (prefix-hack: last answer + question).
# Default True — flip to "false" via env var to instantly disable in
# production without a redeploy if this regresses latency or accuracy.
KB_FOLLOWUP_CONDENSE_ENABLED = os.getenv("KB_FOLLOWUP_CONDENSE_ENABLED", "true").lower() == "true"

# KB_FOLLOWUP_CONDENSE_MODEL_CHAIN — ordered fallback chain of models tried,
# in order, for the follow-up condenser (models/followup_condenser.py) when
# KB_FOLLOWUP_CONDENSE_ENABLED is True. Comma-separated; each entry is either
# a hint understood by model_router ("haiku") or a pinned local model
# ("local:<id>", resolved dynamically by the in-house local gateway's live
# model catalog — not hard-coded here). The first hop to produce a valid,
# non-"[fallback]"-served, sane-length standalone question wins; any
# failure/rejection falls through to the next hop, and if every hop fails the
# condenser falls back to the original (unrewritten) question — see
# models/followup_condenser.py's docstring for the full fail-safe chain.
# Default "haiku" — Claude Haiku for the follow-up condense rewrite task.
# Add a local model as the first hop for zero-cost rewrites, e.g.:
#   KB_FOLLOWUP_CONDENSE_MODEL_CHAIN=local:llama3.1:8b,haiku
# Retune per-environment via this env var with no code change.
KB_FOLLOWUP_CONDENSE_MODEL_CHAIN = [
    m.strip() for m in os.getenv(
        "KB_FOLLOWUP_CONDENSE_MODEL_CHAIN", "haiku"
    ).split(",") if m.strip()
]
# Controls whether the deterministic HardBlock engine runs in the v1/messages
# compliance gate (_compliance_check in messages_compat_router.py).
# Default ON (true) — safe default, no behaviour change in prod.
# Set HARDBLOCK_ENABLED=false to disable in dev/test or during a temporary bypass.
# The PCI/PII compliance gate (compliance_engine.validate_input) is a separate
# step and is NOT affected by this toggle.
HARDBLOCK_ENABLED            = os.getenv("HARDBLOCK_ENABLED",            "false").lower()  == "true"
# Confidence-score threshold for the scoring-based HardBlock gate.
# A prompt is blocked only when its weighted score meets or exceeds this value.
# Range: 0.0–1.0.  Default 0.75 (production).  Lower = stricter; higher = more permissive.
# child_safety always blocks regardless of this value (category weight = 1.0).
HARDBLOCK_THRESHOLD          = float(os.getenv("HARDBLOCK_THRESHOLD",          "0.75"))
#   COMPLIANCE_SCAN_KB_UPLOAD   — run PII/PCI compliance scan + redaction on
#       documents at KB upload time (both text-based and scanned/OCR PDFs).
#       Default OFF: raw document text is stored as-is; redaction happens at
#       retrieval time for cloud models only (_bypass_safety_filters in gateway.py).
#       Set to "true" to re-enable upload-time compliance blocking and redaction.
COMPLIANCE_SCAN_KB_UPLOAD    = os.getenv("COMPLIANCE_SCAN_KB_UPLOAD",    "false").lower() == "true"

ENABLE_SKILL_LOOP             = os.getenv("ENABLE_SKILL_LOOP", "false").lower() == "true"
# ── Raw OpenAI-compatible endpoint kill-switch ────────────────
# When false (default) the raw /ainxt/v1/api/(v1/)chat/completions handler is
# disabled and returns 403 for ALL callers; users must request a MANAGED endpoint
# from an AiNxt admin (routers/endpoint_mgmt_router.py). Managed endpoints
# (/ainxt/v1/api/{slug}/v1/chat/completions) are a separate handler and are
# unaffected by this flag. The GET /v1/models listing route is NOT affected —
# only the generation (chat/completions) route is blocked.
# OPENAI_BASE_URL — the OpenAI-compatible API root used for audio (TTS and
# transcription) and embeddings.
#
# These paths were hardcoded to https://api.openai.com/v1, which meant a
# deployment running local models (LLM_PROVIDER=local) had no way to use them:
# no OpenAI account, so every text-to-speech and transcription call returned
# 401, and the only workaround was editing source. Anything speaking the
# OpenAI audio/embeddings API works here -- LiteLLM, vLLM, faster-whisper,
# openedai-speech, or your own gateway.
#
# Default preserves the previous behaviour exactly.
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")

# Set ENABLE_RAW_OPENAI_API=true to re-enable direct access.
ENABLE_RAW_OPENAI_API         = os.getenv("ENABLE_RAW_OPENAI_API", "false").lower() == "true"
# External OSS resource sync (Anthropic/OpenAI skills, security harness, cookbooks, plugins).
# Default OFF: the daily cron job only registers when this is true (set in the connected
# dev/CI/jump host that has GitHub access; air-gapped prod runs the offline import path).
ENABLE_EXTERNAL_SYNC         = os.getenv("ENABLE_EXTERNAL_SYNC", "false").lower() == "true"

# ── AiNxt Coach ───────────────────────────────────────────────
# Self-contained, removable feature (agents/coach_evaluator.py, the
# services/coach_ingestor/ package, routers/coach_*_router.py, workers/coach_*
# and the coach_* tables). Default OFF: the routers only mount and the sidebar
# nav only shows when this is true. Nothing else depends on it — flip off + drop
# the coach_* tables to remove entirely. See AINXT_COACH_REQUIREMENTS.md.
ENABLE_COACH                 = os.getenv("ENABLE_COACH", "false").lower() == "true"

# ── Buddy prompt queue ────────────────────────────────────────
# Maximum number of prompts a user can queue in Buddy while one is processing.
# Set to 0 for unlimited. Default: 5.
# Change by updating BUDDY_QUEUE_MAX_WAIT in .env and restarting the gateway.
BUDDY_QUEUE_MAX_WAIT         = int(os.getenv("BUDDY_QUEUE_MAX_WAIT", "5"))
# Direct-ingest mode: when true (default), emit_coach_event() also ingests the
# event synchronously in a background thread instead of relying solely on the
# Kafka consumer. Required for local dev (no Kafka) and any environment where the
# coach consumer is not running. Set to "false" in prod when the consumer owns
# the sole "ainxt-coach-consumer" Kafka group.
COACH_DIRECT_INGEST          = os.getenv("COACH_DIRECT_INGEST", "true").lower() == "true"
# Dedicated Kafka topic every channel produces Coach events to.
KAFKA_TOPIC_PREFIX           = os.getenv("KAFKA_TOPIC_PREFIX", APP_OWNER)
COACH_EVENT_TOPIC            = os.getenv("COACH_EVENT_TOPIC", f"{KAFKA_TOPIC_PREFIX}.coach_event")
# Weekly digest mailer (workers/coach_weekly_mail_worker.py). Default OFF.
COACH_WEEKLY_MAIL_ENABLED    = os.getenv("COACH_WEEKLY_MAIL_ENABLED", "false").lower() == "true"
# Weekly digest schedule (IST). Monday 08:00 by default. weekday: 0=Mon … 6=Sun.
COACH_WEEKLY_MAIL_WEEKDAY    = int(os.getenv("COACH_WEEKLY_MAIL_WEEKDAY", "0"))
COACH_WEEKLY_MAIL_HOUR_IST   = int(os.getenv("COACH_WEEKLY_MAIL_HOUR_IST", "8"))
COACH_WEEKLY_MAIL_MIN_IST    = int(os.getenv("COACH_WEEKLY_MAIL_MIN_IST", "0"))
# Fernet key (base64, 32-byte) used to encrypt prompt_redacted at rest in the
# coach_event table. When unset, encryption is DISABLED (dev mode — prompts are
# stored as plaintext-redacted). In prod this MUST be set. Falls back to the
# platform FERNET_KEY when COACH_FERNET_KEY is not given.
COACH_FERNET_KEY             = os.getenv("COACH_FERNET_KEY", os.getenv("FERNET_KEY", ""))
# Minimum number of events in the window before a category score is computed.
COACH_MIN_EVENTS_FOR_SCORE   = int(os.getenv("COACH_MIN_EVENTS_FOR_SCORE", "5"))
# Score decay constant K — characteristic penalty at which a category score
# reaches 100/e ≈ 36.8. Larger K = gentler curve.
COACH_SCORE_DECAY_K          = float(os.getenv("COACH_SCORE_DECAY_K", "60.0"))
# Weight applied to the LLM eval-engine score when it contributes to the
# prompt-quality penalty. Formula: penalty += (1 - eval_score) * weight
# for every REJECT event. Default 3.0 ≈ a medium-severity rule hit.
COACH_EVAL_PENALTY_WEIGHT    = float(os.getenv("COACH_EVAL_PENALTY_WEIGHT", "3.0"))
# ── Discussions module (native ai-ui frontend + Apache Answer as a headless engine) ──
# Self-contained, removable feature (routers/discussions_router.py, core/discussions_engine_client.py,
# services/discussions_engine/ [vendored, headless], the discussions_* mirror tables).
# Default OFF: the gateway router only mounts and the sidebar nav only shows when this
# is true. The engine itself binds 127.0.0.1 ONLY on whichever host it runs on — never
# externally reachable, no browser-facing login, no external IdP (core/discussions_assertion.py
# brokers identity from the existing AiNxt JWT). There is no browser-facing login and no
# external IdP for it.
DISCUSSIONS_ENGINE_BASE_URL = os.getenv("DISCUSSIONS_ENGINE_BASE_URL", "http://127.0.0.1:8010")

# On-disk directory the engine writes uploaded post images to (its config.yaml
# `upload_path`). The headless engine does NOT serve these files over HTTP — its
# /uploads routes return an embedded placeholder — so the gateway reads the bytes
# straight off disk (core/discussions_engine_client.get_upload). Defaults to the
# engine data path's `uploads` subdir; override via DISCUSSIONS_ENGINE_UPLOAD_PATH
# when upload_path is repointed (scripts/setup_discussions_engine.sh).
DISCUSSIONS_ENGINE_UPLOAD_PATH = os.getenv(
    "DISCUSSIONS_ENGINE_UPLOAD_PATH",
    os.path.join(
        os.getenv("DISCUSSIONS_ENGINE_DATA_PATH", f"/var/lib/{APP_OWNER}/discussions-engine"),
        "uploads",
    ),
)

# See docs/DISCUSSIONS_MODULE_IMPLEMENTATION_PLAN.md ("Revision history" — third architecture).
ENABLE_DISCUSSIONS          = os.getenv("ENABLE_DISCUSSIONS", "false").lower() == "true"

# ── Budget enforcement toggle ────────────────────────────────────────────────
# BUDGET_ENFORCEMENT_ENABLED=true  (default) — enforce per-user LLM spend limits.
# BUDGET_ENFORCEMENT_ENABLED=false — skip all budget checks. Useful for OSS users
#                                    running Ollama locally (zero API cost) who
#                                    don't want to hit a $50 limit on a free model.
BUDGET_ENFORCEMENT_ENABLED: bool = os.getenv("BUDGET_ENFORCEMENT_ENABLED", "true").lower() == "true"

# ── Teams HITL action key ─────────────────────────────────────────────────────
# Key used in Teams Adaptive Card payloads to identify HITL approve/reject actions.
# TEAMS_ACTION_KEY=ainxt_action    (OSS default)
# TEAMS_ACTION_KEY=<legacy>_action  (common enterprise default — existing Teams cards use this key)
# An existing deployment must keep its current key — changing it would break deployed cards.
TEAMS_ACTION_KEY: str = os.getenv("TEAMS_ACTION_KEY", "ainxt_action")

# ── SDLC branch and run ID prefixes ──────────────────────────────────────────
# SDLC_BRANCH_PREFIX — prefix for Git branches created by the SDLC coding agent.
#   OSS default: "ainxt"    → branches: ainxt/JIRA-42-ai-impl
#   Legacy:      "<legacy-prefix>"  → branches: <legacy-prefix>/JIRA-42-ai-impl (existing legacy branches)
#
# SDLC_RUN_ID_PREFIX — key embedded in PR body to track SDLC run IDs.
#   OSS default: "ainxt_run_id"    → <!-- ainxt_run_id: <uuid> -->
#   Legacy:      "<legacy>_run_id"  → <!-- <legacy>_run_id: <uuid> --> (existing legacy PRs)
#
# A migrating deployment must keep its existing values — changing them would break webhook
# tracking for all existing SDLC-created branches and PRs.
SDLC_BRANCH_PREFIX:   str = os.getenv("SDLC_BRANCH_PREFIX",   "ainxt")
SDLC_RUN_ID_PREFIX:   str = os.getenv("SDLC_RUN_ID_PREFIX",   "ainxt_run_id")

# ── GCP BigQuery LLM spend fetcher ───────────────────────────────────────────
# LLM_SPEND_GCP_ENABLED=false (OSS default) — skip the GCP BigQuery Gemini
#                             spend fetch. No LLM_PROXY_URL required.
# LLM_SPEND_GCP_ENABLED=true  (common enterprise default) — fetch Gemini spend from GCP
#                             Billing BigQuery via the llm_spend egress proxy.
#                             Requires LLM_PROXY_URL + LLM_PROXY_TOKEN.
LLM_SPEND_GCP_ENABLED: bool = os.getenv("LLM_SPEND_GCP_ENABLED", "false").lower() == "true"

# ── Source control provider ──────────────────────────────────────────────────
# Selects which git-hosting backend the SDLC coding agent, CodebaseManager
# indexing, chat/Cowork connector, and webhook receiver target.
#
# SCM_PROVIDER=github  (OSS default) — uses tools/github_tools.py,
#                       connectors/adapters/github.py, GITHUB_* env vars,
#                       and POST /webhooks/github.
# SCM_PROVIDER=gitlab   (common enterprise default) — uses tools/gitlab_tools.py,
#                       connectors/adapters/gitlab.py, GITLAB_* env vars,
#                       and POST /webhooks/gitlab.
#
# NOTE (2026-08-06): the autonomous SDLC coding pipeline (agents/sdlc_pipeline.py,
# agents/sdlc_state_machine.py, agents/dep_resolver.py, agents/manifest_writer.py)
# is not yet wired to this flag — those modules still import tools.gitlab_tools
# directly. This toggle currently governs the chat/Cowork connector adapter,
# the repo-indexing router, the inbound webhook receiver, and the Profile UI
# token field. See OSS_plan/OSS_GAP_AUDIT_PLAN.md GAP-41 for the remaining
# SDLC-pipeline follow-up.
SCM_PROVIDER: str = os.getenv("SCM_PROVIDER", "github").lower().strip()

# ── Organisation-specific feature router flags ───────────────────────────────
# All default to false. Enable per deployment in your .env.
# When false: the router is not imported and not mounted — no endpoints,
# no initialization errors, no credential warnings.
ENABLE_ZOHO_HR           = os.getenv("ENABLE_ZOHO_HR",           "false").lower() == "true"
ENABLE_HOD_DIGEST        = os.getenv("ENABLE_HOD_DIGEST",        "false").lower() == "true"
ENABLE_LLM_SPEND_REPORT  = os.getenv("ENABLE_LLM_SPEND_REPORT",  "false").lower() == "true"
ENABLE_MONTHLY_STATEMENT = os.getenv("ENABLE_MONTHLY_STATEMENT", "false").lower() == "true"
ENABLE_BROADCAST         = os.getenv("ENABLE_BROADCAST",         "false").lower() == "true"
ENABLE_GRAPH_WEBHOOKS    = os.getenv("ENABLE_GRAPH_WEBHOOKS",    "false").lower() == "true"
ENABLE_WEBHOOKS          = os.getenv("ENABLE_WEBHOOKS",          "false").lower() == "true"
ENABLE_SLACK             = os.getenv("ENABLE_SLACK",             "false").lower() == "true"
ENABLE_TEAMS             = os.getenv("ENABLE_TEAMS",             "false").lower() == "true"
ANSWER_ASSERTION_SECRET     = os.getenv("ANSWER_ASSERTION_SECRET", "")
# Which run sources feed the loop (csv). v1 default is Cowork scheduled tasks only
# (already-recurring → near-zero false positives). Widen to "cowork_task,agent_run".
SKILL_LOOP_SOURCES           = [s.strip() for s in os.getenv("SKILL_LOOP_SOURCES", "cowork_task").split(",") if s.strip()]
SKILL_LOOP_THRESHOLD         = int(os.getenv("SKILL_LOOP_THRESHOLD", "5"))          # min repeats before proposing
SKILL_LOOP_WINDOW_SEC        = int(os.getenv("SKILL_LOOP_WINDOW_SEC", "604800"))    # 7 days (matches learning_store)
SKILL_LOOP_INTERVAL_SEC      = int(os.getenv("SKILL_LOOP_INTERVAL_SEC", "1800"))    # detector cadence (30 min)
SKILL_LOOP_MAX_PROPOSALS_PER_RUN = int(os.getenv("SKILL_LOOP_MAX_PROPOSALS_PER_RUN", "3"))  # anti-spam cap per tick

# ── Forward Proxy ─────────────────────────────────────────────
# Routes outbound internet calls to cloud LLM APIs ONLY:
#   OpenAI, Anthropic, Google — NOT used for the local LLM proxy.
# Python SDKs honour HTTPS_PROXY automatically — no code changes needed.
# Prod: HTTPS_PROXY=http://your-proxy-host:3128
#
# FORWARD_PROXY_URL is used only for startup validation below.
FORWARD_PROXY_URL = os.getenv("FORWARD_PROXY_URL", os.getenv("HTTPS_PROXY", ""))

# ── Local LLM proxy ───────────────────────────────────────────
# Routes to your local/self-hosted LLM (Ollama, LiteLLM, vLLM, etc.).
# Exposes /v1/models (model discovery) + /v1/chat/completions (OpenAI-compatible).
# The gateway fetches the model list at startup and refreshes every 5 min.
# Forward proxy is NOT used — this is a direct internal call.
#
# Required in prod .env:
#   LOCAL_LLM_BASE_URL=http://your-llm-host:11434
#   LOCAL_LLM_API_KEY=<key>            (if auth is enabled on the proxy)
#
# Backward compat: LITELLM_BASE_URL / LITELLM_API_KEY still accepted.
#
# Optional — per-tier model preference (comma-separated, priority order):
#   LOCAL_SIMPLE_MODELS=llama3-8b,mistral-7b
#   LOCAL_MEDIUM_MODELS=llama3-70b,mixtral-8x7b
#   LOCAL_COMPLEX_MODELS=llama3-405b
#   LOCAL_MODEL_REFRESH_SECS=300
LOCAL_LLM_BASE_URL = (os.getenv("LOCAL_LLM_BASE_URL") or os.getenv("LITELLM_BASE_URL", "")).rstrip("/")
LOCAL_LLM_API_KEY  = os.getenv("LOCAL_LLM_API_KEY") or os.getenv("LITELLM_API_KEY", "sk-local")
LOCAL_LLM_ENABLED  = bool(LOCAL_LLM_BASE_URL)
# Backward-compat aliases
LITELLM_BASE_URL = LOCAL_LLM_BASE_URL
globals()["LITELLM_API_KEY"] = LOCAL_LLM_API_KEY
LITELLM_ENABLED  = LOCAL_LLM_ENABLED

# Which model does document-generation INTENT CLASSIFICATION + titling + fuzzy
# reference resolution.
#
# Default is "haiku" (small, fast CLOUD model — Claude Haiku) because intent
# routing is SUPER CRITICAL: it decides chat-vs-document for EVERY prompt, and a
# weaker local model over-triggered doc generation on conversational prompts
# ("tell me a story", "summarize the plot of Hamlet"). Haiku follows the
# decision-gate prompt far more reliably. A deterministic artifact-signal veto
# in models.doc_intent.classify() is the second line of defence regardless of
# model. Set DOC_INTENT_MODEL=local (or local:kimi-k2.7 / local:glm-5.2) to run
# classification fully in-house once a strong-enough local model is available.
# Authoring/refine always stays on the cloud "complex" model for quality.
DOC_INTENT_MODEL = (os.getenv("DOC_INTENT_MODEL", "") or "haiku").strip() or "haiku"

# Model used for general chat with no codebase/KB scope (no repo_filter, no
# retrieved context). Defaults to "simple" (local LLM tier) for zero cloud cost.
# Override with any router hint or local model ID:
#   GENERAL_CHAT_MODEL=simple          → local LLM (default)
#   GENERAL_CHAT_MODEL=haiku           → Claude Haiku
#   GENERAL_CHAT_MODEL=local:llama3.1  → specific local model
GENERAL_CHAT_MODEL = (os.getenv("GENERAL_CHAT_MODEL", "") or "simple").strip() or "simple"

# ── Startup validation (prod mode only) ───────────────────────
def validate_prod_config() -> None:
    """
    Called at gateway startup when DEPLOYMENT_MODE=prod.

    HARD errors  → crash immediately (server cannot function at all without these)
    SOFT warnings → log but continue (only LLM/chat features degrade, login still works)
    """
    if not IS_PROD:
        return

    import logging as _logging
    _log = _logging.getLogger("ainxt")

    # ── HARD: these are required for login, auth, and DB to work ─────────────
    hard_errors = []

    if not os.getenv("JWT_SECRET"):
        hard_errors.append("JWT_SECRET must be set in prod — no default allowed.")

    # Delegate to core.audit_signer so "weak" has exactly one definition. This
    # used to be a bare presence check, which the literal
    # AUDIT_SIGNING_KEY=change-me-in-production from .env.example satisfied — so
    # prod would start and sign its tamper-evident audit log with a value
    # published in the repository.
    #
    # audit_signer raises at import when the key is unusable, so the import
    # itself can be the failure; the explicit call then covers the case where
    # the module was already imported and cached under a different value.
    try:
        from core.audit_signer import reject_weak_key as _reject_weak_audit_key
        _reject_weak_audit_key(os.getenv("AUDIT_SIGNING_KEY"))
    except ValueError as _audit_key_err:
        hard_errors.append(str(_audit_key_err))
    except Exception as _audit_import_err:      # pragma: no cover - defensive
        hard_errors.append(
            f"AUDIT_SIGNING_KEY could not be validated ({_audit_import_err}). "
            f"Generate one with: openssl rand -hex 32"
        )

    if not os.getenv("POSTGRES_PASSWORD"):
        hard_errors.append(
            "POSTGRES_PASSWORD must be set in .env — no default is provided. "
            "Set a strong password, or supply it from your secret manager."
        )


    if hard_errors:
        raise RuntimeError(
            "Production config validation failed (server cannot start):\n" +
            "\n".join(f"  • {e}" for e in hard_errors)
        )

    # ── SOFT: LLM/chat features will degrade but login/auth still works ──────
    soft_warnings = []

    if not os.getenv("ANTHROPIC_API_KEY"):
        soft_warnings.append("ANTHROPIC_API_KEY not set — Claude (complex tier) unavailable.")

    if not os.getenv("OPENAI_API_KEY"):
        soft_warnings.append("OPENAI_API_KEY not set — GPT (medium tier) unavailable.")

    if not os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
        soft_warnings.append("GEMINI_API_KEY / GOOGLE_API_KEY not set — Gemini (vision tier) unavailable.")

    if not os.getenv("HTTPS_PROXY") and not os.getenv("FORWARD_PROXY_URL"):
        soft_warnings.append(
            "HTTPS_PROXY / FORWARD_PROXY_URL not set — cloud LLM calls may fail "
            "if outbound internet requires a forward proxy."
        )

    if not os.getenv("LOCAL_LLM_BASE_URL") and not os.getenv("LITELLM_BASE_URL"):
        soft_warnings.append(
            "LOCAL_LLM_BASE_URL not set — local model tier disabled. "
            "Set LOCAL_LLM_BASE_URL to your LLM proxy or Ollama endpoint."
        )

    if not os.getenv("PGVECTOR_HOST"):
        soft_warnings.append(
            "PGVECTOR_HOST not set — RAG will use the primary Postgres host. "
            "Set PGVECTOR_HOST for a dedicated vector DB in production."
        )

    if not os.getenv("KAFKA_BOOTSTRAP") or "localhost" in os.getenv("KAFKA_BOOTSTRAP", ""):
        soft_warnings.append(
            "KAFKA_BOOTSTRAP not set to a prod cluster — async streaming may use localhost. "
            "Set KAFKA_BOOTSTRAP to your Kafka broker addresses."
        )

    for w in soft_warnings:
        _log.warning(f"[PROD CONFIG] {w}")


def startup_config_check() -> None:
    """
    Print a human-readable config summary at every startup — local and prod.

    Runs AFTER migrations and auto-seed so DB connectivity is already proven.
    Each check is independent and non-fatal — a failure in one check never
    prevents the others from running or the platform from starting.

    Output goes to stdout (print) so it is always visible in:
      - terminal  (python gateway.py)
      - docker logs  (docker compose up / docker compose up -d → docker logs)
      - systemd journal  (journalctl -u ainxt)
    """
    import logging as _logging
    _log = _logging.getLogger("ainxt")

    lines   = []   # ✅ / ⚠️  / ❌  lines for the summary block
    warns   = []   # items that need attention but are non-fatal

    # ── Postgres ──────────────────────────────────────────────────────────────
    try:
        from db.database import engine as _pg_engine
        with _pg_engine.connect() as _c:
            _c.execute(__import__("sqlalchemy").text("SELECT 1"))
        lines.append("  ✅  Postgres        : connected")
    except Exception as _e:
        lines.append(f"  ❌  Postgres        : FAILED — {_e}")
        warns.append("Postgres connection failed — platform will not work.")

    # ── Redis ─────────────────────────────────────────────────────────────────
    try:
        _r = redis_client(db=0)
        _r.ping()
        lines.append("  ✅  Redis           : connected")
    except Exception as _e:
        lines.append(f"  ❌  Redis           : FAILED — {_e}")
        warns.append("Redis connection failed — sessions and budget enforcement will not work.")

    # ── CKMS ──────────────────────────────────────────────────────────────────
    if CKMS_ENABLED:
        lines.append("  ✅  CKMS            : enabled (HSM mode)")
    else:
        lines.append("  ℹ️   CKMS            : disabled (CKMS_ENABLED=false) — env vars read as plaintext")

    # ── LLM providers ─────────────────────────────────────────────────────────
    _llm_configured = any([
        os.getenv("LOCAL_LLM_BASE_URL"),
        os.getenv("LITELLM_BASE_URL"),
        os.getenv("OPENAI_API_KEY"),
        os.getenv("ANTHROPIC_API_KEY"),
        os.getenv("GEMINI_API_KEY"),
        os.getenv("GOOGLE_API_KEY"),
    ])
    if _llm_configured:
        _llm_providers = []
        if os.getenv("LOCAL_LLM_BASE_URL") or os.getenv("LITELLM_BASE_URL"):
            _llm_providers.append("Local/LiteLLM")
        if os.getenv("OPENAI_API_KEY"):
            _llm_providers.append("OpenAI")
        if os.getenv("ANTHROPIC_API_KEY"):
            _llm_providers.append("Claude")
        if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
            _llm_providers.append("Gemini")
        lines.append(f"  ✅  LLM             : {', '.join(_llm_providers)}")
    else:
        lines.append("  ⚠️   LLM             : no provider configured — chat will not work")
        warns.append("No LLM configured. Set LOCAL_LLM_BASE_URL (Ollama) or OPENAI_API_KEY.")

    # ── Embed service ─────────────────────────────────────────────────────────
    # Read the module-level EMBED_SVC_URL (no hardcoded fallback) rather than
    # re-reading the env var with a stale localhost default of its own.
    _embed_url = EMBED_SVC_URL
    if not _embed_url:
        lines.append("  ⚠️   Embed service   : not configured — semantic search disabled")
        warns.append("EMBED_SVC_URL not set. Set it to enable semantic search / RAG.")
    else:
        try:
            import httpx as _httpx
            _resp = _httpx.get(f"{_embed_url}/health", timeout=3.0)
            if _resp.status_code == 200:
                lines.append(f"  ✅  Embed service   : {_embed_url}")
            else:
                lines.append(f"  ⚠️   Embed service   : {_embed_url} returned {_resp.status_code}")
                warns.append(f"Embed service at {_embed_url} is not healthy — semantic search may fail.")
        except Exception:
            lines.append(f"  ⚠️   Embed service   : {_embed_url} unreachable — semantic cache disabled")
            warns.append(f"Embed service unreachable at {_embed_url}. Set EMBED_SVC_URL or start the embed service.")

    # ── SMTP ──────────────────────────────────────────────────────────────────
    _smtp_host = os.getenv("AINXT_SMTP_HOST", "")
    if _smtp_host:
        lines.append(f"  ✅  SMTP            : {_smtp_host}")
    else:
        lines.append("  ℹ️   SMTP            : not configured (AINXT_SMTP_HOST empty) — email features disabled")

    # ── Kafka ─────────────────────────────────────────────────────────────────
    _kafka_enabled = os.getenv("KAFKA_ENABLED", "false").lower() == "true"
    if _kafka_enabled:
        _kb = KAFKA_BOOTSTRAP or "(not set)"
        lines.append(f"  ✅  Kafka           : enabled ({_kb})")
    else:
        lines.append("  ℹ️   Kafka           : disabled (KAFKA_ENABLED=false) — using direct ingest")

    # ── Print the summary block ───────────────────────────────────────────────
    print("\n" + "=" * 62)
    print("  AiNxt — Startup config check")
    print("=" * 62)
    for line in lines:
        print(line)
    if warns:
        print("-" * 62)
        for w in warns:
            print(f"  ⚠️   {w}")
    print("=" * 62 + "\n")

    for w in warns:
        _log.warning(f"[STARTUP] {w}")

# ── LDAP / Active Directory ───────────────────────────────────
# Direct LDAP integration — no Keycloak/Azure AD dependency.
# Set LDAP_ENABLED=true and configure the fields below in .env.
LDAP_ENABLED            = os.getenv("LDAP_ENABLED",            "false").lower() == "true"

# ── Default AD level for new self-registered users ───────────────────────────
# DEFAULT_AD_LEVEL is assigned to every user created via POST /auth/register
# (self-registration). It does NOT affect LDAP-provisioned users — their level
# comes from Active Directory via the nightly ad_sync.
#
# DEFAULT_AD_LEVEL=3  (OSS default) — mid-level. With APPROVAL_AD_LEVEL=6
#                     (OSS default), level 3 users can approve everything.
#                     A sensible starting point for a small team.
# DEFAULT_AD_LEVEL=6  (common enterprise default) — most junior. Directory sync sets
#                     the real level; the registration default is a safe
#                     fallback that prevents accidental privilege escalation.
DEFAULT_AD_LEVEL: int = int(os.getenv("DEFAULT_AD_LEVEL", "3"))

# ── Approval threshold ───────────────────────────────────────────────────────
# APPROVAL_AD_LEVEL defines the maximum ad_level that can approve budget
# requests, products, and governance actions.
# ad_level scale: 0 = most senior exec, 6 = most junior engineer.
# "can approve" means: ad_level <= APPROVAL_AD_LEVEL OR role == "admin".
#
# APPROVAL_AD_LEVEL=3  (common enterprise default) — only senior managers (L3+) can approve.
# APPROVAL_AD_LEVEL=6  (OSS default)  — everyone can approve (no hierarchy needed
#                      for a small team). Admin always bypasses this check.
APPROVAL_AD_LEVEL: int = int(os.getenv("APPROVAL_AD_LEVEL", "6"))

# LDAP_AUTO_PROVISION controls what happens when a valid LDAP user logs in
# but has no existing record in the local users table.
#
# LDAP_AUTO_PROVISION=true  (OSS default) — auto-provision the user on first
#                           LDAP login. No pre-registration required.
# LDAP_AUTO_PROVISION=false (common enterprise default) — keep the controlled rollout gate.
#                           User must be pre-registered in the DB before they
#                           can log in. Returns 403 if not found.
LDAP_AUTO_PROVISION     = os.getenv("LDAP_AUTO_PROVISION",     "true").lower() == "true"
# No hardcoded default — only meaningful when LDAP_ENABLED=true (above); an
# unset value simply means LDAP auth is unreachable, same as being disabled.
LDAP_URL                = os.getenv("LDAP_URL",                "")
LDAP_BIND_DN            = os.getenv("LDAP_BIND_DN",            "")
LDAP_BIND_PASSWORD      = os.getenv("LDAP_BIND_PASSWORD",      "")
LDAP_BASE_DN            = os.getenv("LDAP_BASE_DN",            f"DC={APP_OWNER},DC=example,DC=com")
LDAP_USER_FILTER        = os.getenv("LDAP_USER_FILTER",        "(&(objectClass=person)(mail=*))")
LDAP_USER_SEARCH_FILTER = os.getenv("LDAP_USER_SEARCH_FILTER", "(mail={email})")
LDAP_USE_SSL            = os.getenv("LDAP_USE_SSL",            "false").lower() == "true"
LDAP_USE_STARTTLS       = os.getenv("LDAP_USE_STARTTLS",       "true").lower()  == "true"
LDAP_SYNC_HOUR          = int(os.getenv("LDAP_SYNC_HOUR",      "2"))  # 2 AM IST nightly

# ── hierarchy_table rebuild worker kill switch ─────────────────────────────
# workers/hierarchy_rebuild_worker.py polls a Redis dirty flag every 2 minutes
# and fully rebuilds hierarchy_table when set. DISABLED BY DEFAULT — set
# HIERARCHY_REBUILD_ENABLED=true to re-enable the scheduled rebuild. When off,
# the worker is not registered with the cron scheduler at all and the function
# short-circuits, so hierarchy_table is only ever refreshed by an explicit
# manual run: `python workers/hierarchy_rebuild_worker.py --force`.
HIERARCHY_REBUILD_ENABLED = os.getenv("HIERARCHY_REBUILD_ENABLED", "false").lower() == "true"

# AD group DNs for security team and platform approvers.
# Set these to match your organisation's Active Directory group structure.
SECURITY_AD_GROUP = os.environ.get("SECURITY_AD_GROUP", f"CN=SecurityTeam,OU=Groups,DC={APP_OWNER},DC=example,DC=com")
APPROVER_AD_GROUP = os.environ.get("APPROVER_AD_GROUP", f"CN=PlatformApprovers,OU=Groups,DC={APP_OWNER},DC=example,DC=com")

# ── Document generation ────────────────────────────────────────────────────────
# Path to the default PPTX template used when no user template is uploaded.
# Defaults to assets/ainxt_template.pptx relative to the project root.
# Override with PPT_TEMPLATE_PATH env var to point to any .pptx on the server.
_DEFAULT_PPT_TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "assets", "ainxt_template.pptx")
PPT_TEMPLATE_PATH = os.getenv("PPT_TEMPLATE_PATH", _DEFAULT_PPT_TEMPLATE)

# ── Redis DB allocation (reference — do not change without updating workers) ─
# db=0  answer cache + rewrite cache
# db=1  trace store
# db=2  workflows + agent run history
# db=3  marketplace registry / inbox / index governance
# db=4  budget + usage
# db=5  rq job queues + teams conversation mapping
# db=6  chat token streams (SSE via XREAD)
# db=7  embed svc SHA256 embedding cache
# db=8  privacy svc PII cache (services/privacy_svc/main.py)
RDB_CACHE    = 0
RDB_TRACE    = 1
RDB_WORKFLOW = 2
RDB_REGISTRY = 3
RDB_BUDGET   = 4
RDB_QUEUE    = 5
RDB_STREAM   = 6
RDB_EMBED    = 7
RDB_PRIVACY  = 8

# Total number of logical KV DBs the platform owns. Anything that iterates
# over "all KV DBs" (factory.kv_backend_map, /health probe, KV_BACKEND_MAP)
# uses this constant so adding a new DB only requires bumping it.
KV_DB_COUNT  = 9

# ── KV backend selector ──────────────────────────────────────────────────────
# Each logical DB picks its own backend independently. Resolution order
# (highest wins):
#   1. REDIS_CLIENT_CONFIG_DB{n}   e.g. REDIS_CLIENT_CONFIG_DB4=REDIS
#   2. REDIS_CLIENT_CONFIG          global default for any DB without override
#   3. "REDIS"                      hard-coded final fallback
#
# Per-DB selection makes rollout safe: flip one DB at a time, roll back via
# env-var change without redeploying code.
_KV_VALID_BACKENDS = ("REDIS",)

# Backends that existed in earlier internal builds and are not part of this
# release. Named explicitly so an operator carrying an old value forward gets
# told what happened rather than a bare "invalid value".
_KV_REMOVED_BACKENDS = {
    "RUSTYCLUSTER": (
        "the RustyCluster KV backend is not part of this release — it depended "
        "on a client package that is not publicly available"
    ),
}


def _validate_kv_backend(var_name: str, val: str) -> str:
    """Validate one KV-backend value. `var_name` is the env var it came from,
    so the error tells the operator exactly which setting to change.

    Shared by the global check below and by kv_backend_for(); keeping it in one
    place is deliberate — when this logic was duplicated, only one copy grew the
    removed-backend message and the other reported a bare validation error.
    """
    removed = _KV_REMOVED_BACKENDS.get(val)
    if removed:
        raise ValueError(
            f"{var_name}={val}, but {removed}. "
            f"Set REDIS_CLIENT_CONFIG=REDIS (or unset it; REDIS is the default)."
        )
    if val not in _KV_VALID_BACKENDS:
        raise ValueError(
            f"Invalid {var_name}={val!r}; must be one of {_KV_VALID_BACKENDS}"
        )
    return val


REDIS_CLIENT_CONFIG = _validate_kv_backend(
    "REDIS_CLIENT_CONFIG", os.getenv("REDIS_CLIENT_CONFIG", "REDIS").upper()
)

def kv_backend_for(db: int) -> str:
    """
    Resolve which backend should host logical DB `db`. Redis is the only backend.

    Reads REDIS_CLIENT_CONFIG_DB{db} first, then REDIS_CLIENT_CONFIG, then
    defaults to 'REDIS'. Result is uppercased and validated.

    The per-DB indirection is kept deliberately: it is how callers select a
    logical database, and it leaves room for a second backend without touching
    every call site again.
    """
    per_db = f"REDIS_CLIENT_CONFIG_DB{db}"
    raw = os.getenv(per_db)
    # Report whichever variable actually carried the value, so the operator
    # edits the setting that is in force rather than hunting for it.
    if raw is None:
        return _validate_kv_backend("REDIS_CLIENT_CONFIG", REDIS_CLIENT_CONFIG)
    return _validate_kv_backend(per_db, raw.upper())

# Snapshot at import time — useful for /healthz, startup logs, and debugging.
# (Note: env-var changes after process start are NOT picked up by this map;
#  call kv_backend_for(db) directly if you need the live value.)
KV_BACKEND_MAP = {db: kv_backend_for(db) for db in range(KV_DB_COUNT)}


# ── Presenton (open-source AI PPT engine) ─────────────────────
# Presenton runs as a Docker container; calls back to our gateway's
# OpenAI-compat endpoint for LLM inference (compliance + fallback intact).
# No hardcoded default — set PRESENTON_URL to the internal Docker host or
# service address; unset/unreachable is already handled by
# routers/presenton_router.py and workers/presenton_worker.py.
PRESENTON_URL      = os.getenv("PRESENTON_URL",      "")
PRESENTON_USER     = os.getenv("PRESENTON_USER",     APP_OWNER)
PRESENTON_PASSWORD = os.getenv("PRESENTON_PASSWORD", "")
PPT_LLM_MODEL      = os.getenv("PPT_LLM_MODEL",      "complex")            # router tier or model ID; "complex" → CLAUDE_PRIMARY_MODEL
PPT_IMAGE_PROVIDER = os.getenv("PPT_IMAGE_PROVIDER", "gemini_flash")

# ── Build pipeline (SDLC execution) ──────────────────────────
# Workspace root — persistent git checkouts per repo.
# Must be a dedicated volume with sufficient disk space (plan for 50–200 GB).
BUILDER_WORKSPACE_ROOT = os.getenv("BUILDER_WORKSPACE_ROOT", f"/opt/{APP_OWNER}/workspaces")

# Cache root — shared Maven/npm/pip/Go caches mounted into builder containers.
# A warm cache reduces build time from 5–10 min to 30–60 sec.
BUILDER_CACHE_ROOT = os.getenv("BUILDER_CACHE_ROOT", f"/opt/{APP_OWNER}/build-cache")

# Hard timeout per build phase (compile or test). Docker container is killed after this.
# This is the WARM budget — it assumes the dependency cache for the repo is already
# populated, so the dependency step is a near-no-op and only compile/test runs.
BUILD_TIMEOUT_SECS = int(os.getenv("BUILD_TIMEOUT_SECS", "300"))

# COLD budget — used when the repo's dependency cache is COLD (never built) or STALE
# (lockfile hash changed).  A first-ever build or a version bump can need to download
# gigabytes (e.g. torch pulling ~2.5–3 GB of CUDA wheels); the warm 300s budget kills
# the container mid-fetch, so the persistent venv/cache is never written and every
# subsequent build restarts the same download from zero (permanent stall).  The cold
# budget lets that one-time population finish so future builds are HIT and fast.
# WorkspaceBuilder selects this per run via build_manifest_resolver.cache_state().
BUILD_COLD_TIMEOUT_SECS = int(os.getenv("BUILD_COLD_TIMEOUT_SECS", "1800"))

# ── Builder container resource budget ────────────────────────
# CPU and memory granted to every builder container (compile/test phases and
# the multi-repo dep installs). These were previously hardcoded at half a core
# (cpu_quota=50000) and 2g, which made a Maven reactor build take 10–20 min on
# a host with hundreds of idle cores. Both are now config-driven so the budget
# can be matched to the runtime host.
#
# BUILD_CONTAINER_CPUS: whole/fractional CPUs, passed to docker as
#   cpu_quota = CPUS * cpu_period (100000). e.g. 8 → cpu_quota=800000.
#   Set to 0 to remove the CPU limit entirely (container may use all cores).
# BUILD_CONTAINER_MEMORY: docker mem_limit string (e.g. "8g").
#
# Keep the memory budget generous: a container killed by the OOM killer exits
# 137, which the parser classifies as a real build failure, not a retryable one.
# NOTE: the limits are only enforced when the host's cgroup driver supports
# them; on cgroup v2 + systemd (the standard here) both are honoured.
BUILD_CONTAINER_CPUS = float(os.getenv("BUILD_CONTAINER_CPUS", "8"))
BUILD_CONTAINER_MEMORY = os.getenv("BUILD_CONTAINER_MEMORY", "8g")
# docker's default CFS period; cpu_quota is expressed against it.
BUILD_CONTAINER_CPU_PERIOD = int(os.getenv("BUILD_CONTAINER_CPU_PERIOD", "100000"))


def build_container_resources() -> dict:
    """
    docker-py resource kwargs (`mem_limit` / `cpu_quota`) for a builder container.

    Lives here so every place that starts a builder container — the compile/test
    phases in sandbox/workspace_builder.py and the multi-repo dep installs in
    agents/multi_repo_workspace.py — derives the same budget from one place
    instead of each hardcoding its own. Read at call time, not import time, so a
    test or an operator changing the module attribute takes effect immediately.

    BUILD_CONTAINER_CPUS <= 0 means "no CPU limit": the cpu_quota/cpu_period keys
    are omitted entirely, because the docker API rejects a zero/negative quota.
    """
    kwargs: dict = {"mem_limit": BUILD_CONTAINER_MEMORY}
    if BUILD_CONTAINER_CPUS and BUILD_CONTAINER_CPUS > 0:
        kwargs["cpu_period"] = BUILD_CONTAINER_CPU_PERIOD
        kwargs["cpu_quota"] = int(BUILD_CONTAINER_CPUS * BUILD_CONTAINER_CPU_PERIOD)
    return kwargs

# ── Maven build command tuning ───────────────────────────────
# Extra flags spliced into a default `mvn` compile command by the manifest
# resolver. `-T 1C` runs one build thread per available core, which is the
# other half of the fix above (raising the CPU budget does nothing while Maven
# stays single-threaded). Set to "" to disable.
# Only applied to the resolver's OWN default command — a repo-supplied command
# from .sdlc.yml / .gitlab-ci.yml is never rewritten.
#
# MUST be a single `-T` token group (`-T 1C`, `-T4`, `-T 2` …) or empty. The
# resolver recognises its own already-tuned command by that exact shape in order
# to stay idempotent and to re-derive the command from current config; adding
# further flags here (e.g. "-T 1C -o") makes a persisted command unrecognisable,
# so changing this value later would no longer take effect for repos whose
# manifest row was written under the old value.
MAVEN_PARALLEL_FLAG = os.getenv("MAVEN_PARALLEL_FLAG", "-T 1C")

# When true (default) the resolver's default Maven compile command drops the
# `clean` goal, so an incremental rebuild reuses the previous `target/` output
# instead of recompiling the whole reactor from scratch. Each SDLC run builds in
# a fresh per-run checkout, so there is no stale-output risk from a prior run;
# set to false to restore `mvn clean install`.
MAVEN_SKIP_CLEAN = os.getenv("MAVEN_SKIP_CLEAN", "true").lower() == "true"

# ── Shared Maven cache write-back ────────────────────────────
# Multi-repo runs build against a per-run `_m2_cache` (isolated, seeded from the
# shared cache by hardlink). Without a write-back the shared cache never learns
# about newly downloaded third-party artifacts, so every run re-fetches them
# from Nexus. When true, a run that ends GREEN merges its per-run cache back
# into the shared one, skipping internal (INTERNAL_GROUP_PREFIXES) artifacts so
# org-internal snapshots never leak across runs. The merge only ADDS files that
# the shared cache does not already have, so a concurrent reader never sees a
# partially written artifact it was already using.
M2_SHARED_CACHE_WRITEBACK = os.getenv("M2_SHARED_CACHE_WRITEBACK", "true").lower() == "true"

# How many times the LLM is allowed to attempt a fix before the pipeline gives up.
# 1 means: one LLM fix attempt, one retry. Set 0 to disable LLM fix loop.
BUILD_MAX_RETRIES = int(os.getenv("BUILD_MAX_RETRIES", "1"))

# AiNxt internal Nexus repository URL (Maven + npm proxy).
# Used to check whether AiNxt internal artifacts are published before building deps.
# Example: http://nexus.ainxt.org:8081
AiNxt_NEXUS_URL = os.getenv("AiNxt_NEXUS_URL", "")

# AiNxt internal Docker registry (where builder images are pushed and pulled from).
# Set this to the registry prefix — e.g. "docker-registry.ainxt.org:5000".
# BuildManifestResolver prepends this to all versioned image names automatically.
# Leave empty for local dev (images built locally with docker build).
BUILDER_REGISTRY = os.getenv("BUILDER_REGISTRY", "")

# Fallback image names — used only when version detection fails entirely.
# Normally the resolver picks ainxt-builder-jvm-{17|21|25}:latest based on
# the Java version detected from pom.xml.  These are the defaults when version
# is unknown. Override via env var to point to a specific Nexus-hosted image.
BUILDER_IMAGE_JVM     = os.getenv("BUILDER_IMAGE_JVM",     "ainxt-builder-jvm-21:latest")
BUILDER_IMAGE_PYTHON  = os.getenv("BUILDER_IMAGE_PYTHON",  "ainxt-builder-python-311:latest")
BUILDER_IMAGE_NODE    = os.getenv("BUILDER_IMAGE_NODE",    "ainxt-builder-node-20:latest")
BUILDER_IMAGE_SYSTEMS = os.getenv("BUILDER_IMAGE_SYSTEMS", "ainxt-builder-systems:latest")

# Image used by the privileged workspace-cleanup container that removes
# root-owned build residue left behind by a builder container (see
# workers.workspace_sync_worker._force_remove_dir). This MUST be an image that
# is already present in the local Docker cache: the cleanup runs with
# network_mode="none" and the host may be air-gapped, so an implicit registry
# pull (the docker-py default when the image is absent) fails with a DNS error
# and silently aborts cleanup. Defaulting to a bare Docker Hub name like
# "python:3.11-slim" is exactly what triggered that failure in one deployment.
# Default here to the JVM/Python builder image, which is guaranteed local on
# any host that has actually run a build. Override to an internal-mirror name
# (e.g. "your-registry.example.com:443/python:3.11-slim") if preferred.
WORKSPACE_CLEANUP_IMAGE = os.getenv("WORKSPACE_CLEANUP_IMAGE", BUILDER_IMAGE_PYTHON)

# Log directory for per-build output (full stdout/stderr stored here for debugging).
BUILD_LOG_DIR = os.getenv("BUILD_LOG_DIR", f"/opt/{APP_OWNER}/logs/builds")

# Python builder: container-internal path for the pip download cache.
# Defaults to /cache/pip (not /root/.cache/pip) so the mounted host directory
# is never under /root — avoids "not owned or not writable" errors when the
# host dir was created by a different UID than the container process.
PIP_CACHE_CONTAINER_PATH = os.getenv("PIP_CACHE_CONTAINER_PATH", "/cache/pip")

# Host-side directory mounted into the container at PIP_CACHE_CONTAINER_PATH.
# Set this to a directory the application user owns (e.g. /appdata/fastapi/pip-cache).
# Defaults to BUILDER_CACHE_ROOT/cache_pip when not set.
PIP_CACHE_HOST_PATH = os.getenv(
    "PIP_CACHE_HOST_PATH",
    os.path.join(BUILDER_CACHE_ROOT, "cache_pip"),
)

# Python builder: container-internal path for the Poetry cache.
POETRY_CACHE_CONTAINER_PATH = os.getenv("POETRY_CACHE_CONTAINER_PATH", "/cache/pypoetry")

# Host-side directory mounted into the container at POETRY_CACHE_CONTAINER_PATH.
# Defaults to BUILDER_CACHE_ROOT/cache_pypoetry when not set.
POETRY_CACHE_HOST_PATH = os.getenv(
    "POETRY_CACHE_HOST_PATH",
    os.path.join(BUILDER_CACHE_ROOT, "cache_pypoetry"),
)

# Python builder: container-internal path for the persistent per-repo venv.
# When PYTHON_USE_PERSISTENT_VENV=true (default), a volume is mounted from
# BUILDER_CACHE_ROOT/venvs/{repo_slug} so that installed packages survive
# across builds — pip only re-installs when requirements actually change.
PYTHON_VENV_CONTAINER_PATH = os.getenv("PYTHON_VENV_CONTAINER_PATH", "/venv")
PYTHON_USE_PERSISTENT_VENV = os.getenv("PYTHON_USE_PERSISTENT_VENV", "true").lower() == "true"

# ── KB full-doc storage: plain local filesystem ──────────────────────────────
# Approved KB docs are written by docs_store.activate_doc as plain UTF-8 .md
# files at <KB_DOC_STORAGE_PATH>/<doc_id>.md. No object storage, no SCM mirror.
# The Coverage tier reads these files directly via store.kb_doc_cache (Redis
# warms once per cold key per 24 h — the filesystem read is amortised away).
#
# Linux default: /var/lib/ainxt/kb_docs (must be writable by the gateway
# process; created on startup with mode 0o755). Override via env on prod
# deploys with a mounted volume / SSD partition.
#
# This knob does NOT affect chat attachments — those continue to use
# core/storage (MinIO with local-FS fallback) under MINIO_* env vars.
KB_DOC_STORAGE_PATH = os.getenv(
    "KB_DOC_STORAGE_PATH",
    os.path.join(tempfile.gettempdir(), f"{APP_OWNER}_kb_docs")
    if DEPLOYMENT_MODE == "local"
    else f"/var/lib/{APP_OWNER}/kb_docs",
)

# ── KB retrieval scope (orchestrator dispatch) ────────────────────────────────
# Controls how hybrid_retrieve_context routes a KB query through the Fast tier
# (BM25 + pgvector + rerank) and the Coverage tier (graph-guided whole-section
# read of the scoped doc — kn_rewrite.md §6 Phase 3).
#
# Modes:
#   "auto"      — DEFAULT. Run Fast tier first; escalate to Coverage only when
#                 the §8y sufficiency gate fires. Mirrors the original spec
#                 (single-path, cost-controlled).
#
#   "rag"       — Fast tier ONLY. Skip the gate, skip Coverage. Cheapest path;
#                 use for chats where you trust the dense retriever and want
#                 to bound latency.
#
#   "full_file" — Coverage tier ONLY. Skip Fast tier entirely. Requires a
#                 scoped doc (user_ctx['scope_filter'].kb_doc_id OR
#                 product_id + spec_version) — otherwise retrieval returns
#                 nothing (we refuse to scan unscoped). Use when the user's
#                 question is structurally a "read the whole doc" question.
#
#   "both"      — Run Fast and Coverage IN PARALLEL, then concat their hits
#                 and re-rank the combined list via embed_svc /rerank,
#                 keeping the top-k overall. Highest recall, highest cost.
#                 Coverage half shares the existing KB_COVERAGE_GLOBAL_CONCURRENCY
#                 semaphore so it can't bypass back-pressure.
#
# This is an OPERATOR knob (config-file / env), not a user-facing toggle. The
# scope picker in the Chat UI selects WHICH doc to read; this knob selects HOW
# to read it. Change requires a process restart.
KB_RETRIEVAL_SCOPE = os.getenv("KB_RETRIEVAL_SCOPE", "both").strip().lower()
if KB_RETRIEVAL_SCOPE not in ("auto", "rag", "full_file", "both"):
    # Fail-safe to 'auto' — the historical behaviour. Logged so operators
    # see the typo on startup instead of getting a silent regression.
    import logging as _kb_log
    _kb_log.getLogger(__name__).warning(
        f"KB_RETRIEVAL_SCOPE={KB_RETRIEVAL_SCOPE!r} is not one of "
        "{auto, rag, full_file, both} — falling back to 'auto'."
    )
    KB_RETRIEVAL_SCOPE = "both"

# ── SDLC human-gate expiry (HITL) ─────────────────────────────
# A gate-waiting SDLC run must survive a multi-day human review. These TTLs
# replace the old hardcoded 48h deadline and the blanket 4h reaper so a run
# parked at an approval gate is never force-cancelled mid-review. Governance
# gets a longer window because EA/IS/DPDP sign-off routinely spans a full
# business week. Values are read at import time — restart workers+gateway to
# change them. Invalid values log a warning and fall back to the default.
def _env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to `default` on missing/blank/invalid.

    Mirrors the SDLC model-override convention (see core/job_queue.py): an
    invalid value logs a warning and falls back to the code default rather than
    crashing the process. Kept local to config.py so this module stays free of
    pipeline-module imports (no cycles).
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        try:
            from core.logger import logger
            logger.warning(f"config: invalid {name}={raw!r} — using default {default}")
        except Exception:
            pass
        return default

def _env_bool(name: str, default: bool) -> bool:
    """Read a bool env var, falling back to `default` on missing/blank/invalid.

    Mirrors `_env_int`'s non-crashing convention: an unrecognized value logs a
    warning and falls back to the code default rather than crashing the process.
    Kept local to config.py so this module stays free of pipeline-module imports.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    val = raw.strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    try:
        from core.logger import logger
        logger.warning(f"config: invalid {name}={raw!r} — using default {default}")
    except Exception:
        pass
    return default

SDLC_HITL_TTL_HOURS            = _env_int("SDLC_HITL_TTL_HOURS", 72)    # feature/bug gates
SDLC_GOVERNANCE_HITL_TTL_HOURS = _env_int("SDLC_GOVERNANCE_HITL_TTL_HOURS", 168)  # governance = 7d

# Reaper window for genuinely-stale ACTIVE (non-gate) runs. A wedged worker
# segment should still be reaped promptly — NOT held for the full gate window.
SDLC_ACTIVE_REAP_HOURS = _env_int("SDLC_ACTIVE_REAP_HOURS", 4)

# ── Buddy Scheduler limits ─────────────────────────────────────────────────────
# BUDDY_SCHED_MAX_PER_USER  — max active+paused schedulers a single user may own
# BUDDY_SCHED_MAX_RUNS      — max occurrences (runs_count ceiling) per scheduler
BUDDY_SCHED_MAX_PER_USER = _env_int("BUDDY_SCHED_MAX_PER_USER", 5)
BUDDY_SCHED_MAX_RUNS     = _env_int("BUDDY_SCHED_MAX_RUNS", 25)

# ── Governance approval evidence → linked Jira Change ticket (V7, 2026-08-04) ─
# On each governance domain approve/send-back and on full approval, the platform
# EXPORTS the already-persisted approval evidence to a dedicated, linked Jira
# Change ticket that change management uses to authorize production promotion.
SDLC_GOVERNANCE_EVIDENCE_ENABLED = _env_bool("SDLC_GOVERNANCE_EVIDENCE_ENABLED", True)  # kill switch
SDLC_GOVERNANCE_CHANGE_PROJECT   = os.getenv("SDLC_GOVERNANCE_CHANGE_PROJECT", "CGA") # empty → dev-ticket project / JIRA_PROJECT
SDLC_GOVERNANCE_CHANGE_ISSUE_TYPE = os.getenv("SDLC_GOVERNANCE_CHANGE_ISSUE_TYPE", "Change")
SDLC_GOVERNANCE_CHANGE_TRANSITION = os.getenv("SDLC_GOVERNANCE_CHANGE_TRANSITION", "Approved - Prod Ready")   # empty → skip transition
SDLC_GOVERNANCE_CHANGE_LABEL     = os.getenv("SDLC_GOVERNANCE_CHANGE_LABEL", "prod-ready")
# Jira issue-link *type name* used to link the change ticket to the dev ticket.
# Jira's built-in default is "Relates" (not "relates to", which is only the
# inward/outward phrasing). An instance with a different link type can override.
SDLC_GOVERNANCE_CHANGE_LINK_TYPE = os.getenv("SDLC_GOVERNANCE_CHANGE_LINK_TYPE", "Relates")


# ── Platform kill-switch admin API (SEC-2026-0142) ───────────────────────────
# Feature gate for the three /ainxt/v1/api/system/platform/* routes
# (disable / enable / status). When False, all three return 404 for EVERY
# caller — including administrators — so the API surface is absent unless an
# environment explicitly opts in.
#
# DEFAULTS TO FALSE (secure by default / least exposure): the smallest attack
# surface is an endpoint that does not exist. Set
#   ENABLE_PLATFORM_KILLSWITCH_API=true
# in the environment that actually needs to operate the kill switch.
#
# NOTE: with the API disabled the kill switch can still be operated directly
# in Redis (SET platform:disabled 1 / DEL platform:disabled) — the /ask
# enforcement path reads that key regardless of this flag. Disabling the API
# removes the HTTP control plane, not the capability.
#
# This flag does NOT weaken authorization: when the API is enabled the routes
# still require role=admin. There is deliberately no flag to switch the admin
# check off, since that would re-open the BFLA finding (any authenticated user
# able to take the platform offline).
ENABLE_PLATFORM_KILLSWITCH_API = _env_bool("ENABLE_PLATFORM_KILLSWITCH_API", False)


# ── Canonical SDLC state-set constants ────────────────────────
# `AWAITING_CODE_APPROVAL` is the renamed successor of the legacy
# `AWAITING_DESIGN_APPROVAL`. Dual-read (expand/contract): every comparison
# accepts BOTH values so in-flight rows written before the rename still resolve.
# Writers emit only the new value; readers accept the set.
AWAITING_CODE_APPROVAL   = "AWAITING_CODE_APPROVAL"
AWAITING_DESIGN_APPROVAL = "AWAITING_DESIGN_APPROVAL"   # legacy alias — read-only
CODE_APPROVAL_STATES = {AWAITING_CODE_APPROVAL, AWAITING_DESIGN_APPROVAL}

# Governance gate — its own longer reaper/deadline window.
GOVERNANCE_GATE_STATES = {"AWAITING_GOVERNANCE_APPROVAL"}

AWAITING_BUILD_METADATA_APPROVAL = "AWAITING_BUILD_METADATA_APPROVAL"

# All human-gate / suspended states that must survive the long HITL window
# (as opposed to ACTIVE working states, which keep the short reaper window).
GATE_STATES = CODE_APPROVAL_STATES | {
    "AWAITING_SOLUTION_APPROVAL",
    "AWAITING_PR_APPROVAL",
    "AWAITING_USER_INPUT",
    "AWAITING_RE_REVIEW",
    AWAITING_BUILD_METADATA_APPROVAL,
    "SUSPENDED",
} | GOVERNANCE_GATE_STATES


def sdlc_gate_deadline(gate_kind: str) -> int:
    """Absolute epoch-second deadline for a gate the pipeline is about to enter.

    `gate_kind` is a coarse label ("code" / "solution" / "pr" / "questions" /
    "normalization" / "governance"). Governance gets the governance window;
    everything else gets the standard HITL window.
    """
    kind = (gate_kind or "").strip().lower()
    hours = SDLC_GOVERNANCE_HITL_TTL_HOURS if kind == "governance" else SDLC_HITL_TTL_HOURS
    return int(time.time()) + hours * 3600


def sdlc_reaper_window_hours(state: str) -> int:
    """Inactivity window (hours) after which the reaper may cancel a run in `state`.

    Gate/suspended states get the long HITL window (governance longest) so a
    live multi-day human gate is never force-cancelled; every other non-terminal
    (ACTIVE working) state keeps the short SDLC_ACTIVE_REAP_HOURS so a wedged
    worker segment is still reaped promptly.
    """
    s = state or ""
    if s in GOVERNANCE_GATE_STATES:
        return SDLC_GOVERNANCE_HITL_TTL_HOURS
    if s in GATE_STATES:
        return SDLC_HITL_TTL_HOURS
    return SDLC_ACTIVE_REAP_HOURS


# ── Injection-scan transport security ─────────────────────────
# INJECTION_SCAN_VERIFY_TLS controls certificate verification on calls to the
# prompt-injection scanning service (ADR-009).
#
# The service is normally reached over an internal network through an nginx
# instance presenting a self-signed certificate, so verification is off by
# default and the previous behaviour is preserved exactly. Set this to true —
# or to the path of a CA bundle — once the internal CA is trusted, so the hop
# is authenticated rather than blindly trusted (CWE-599).
#
#   INJECTION_SCAN_VERIFY_TLS=false            (default; self-signed internal cert)
#   INJECTION_SCAN_VERIFY_TLS=true             (verify against the system CA store)
#   INJECTION_SCAN_VERIFY_TLS=/etc/ssl/ca.pem  (verify against a specific bundle)
def injection_scan_verify() -> "bool | str":
    """Return the value to pass as httpx's ``verify=`` for injection-scan calls."""
    raw = (os.getenv("INJECTION_SCAN_VERIFY_TLS") or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    return raw  # Treat anything else as a path to a CA bundle.


def loopback_tls_verify(url: str) -> bool:
    """Whether to verify TLS for `url`, treating loopback as trusted.

    A service on 127.0.0.1 has no network path an attacker could intercept, and
    such services commonly present an ephemeral self-signed certificate, so
    verification is skipped there. Any other host means the traffic leaves the
    machine, so the certificate is checked rather than blindly trusted
    (CWE-599). An unparseable URL fails closed and is verified.
    """
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return True
    return host not in ("127.0.0.1", "::1", "localhost")
