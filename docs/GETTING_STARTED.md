# Getting Started

Everything needed to take AiNxt Enterprise from nothing to a working platform.
Every command and value here was verified against a from-scratch install.

- [Prerequisites](#prerequisites)
- [Quickest path (Docker)](#quickest-path-docker)
- [Running natively instead](#running-natively-instead)
- [LLM configuration](#llm-configuration)
- [Seed data and the first login](#seed-data-and-the-first-login)
- [Optional features](#optional-features)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

**Docker install** — the recommended path — needs only:

| | |
|---|---|
| Docker | 20+, with the daemon running |
| Memory | ~8 GB available to Docker. Below ~6 GB the gateway is likely to be OOM-killed while loading the ML stack |
| Disk | ~15 GB. The gateway image is **10.7 GB**, mostly PyTorch and the CUDA libraries |
| Time | 5–15 minutes on first run while images build |

No local Python, Node or PostgreSQL required.

**Native install** additionally needs Python **3.10+** and Node **18+**.

### Platform notes

| Platform | Status | What to know |
|---|---|---|
| macOS | Verified (macOS 14, arm64) | Docker Desktop |
| Linux | Verified (Ubuntu 24.04) | `lsof` and `openssl` are optional; the installer falls back to Python and `/dev/urandom`. Native mode on Debian/Ubuntu also needs `python3-venv` |
| Windows | Via WSL2 | `install.sh` is bash — run it from a WSL2 shell with Docker Desktop's WSL integration enabled. It will not run in PowerShell or `cmd` |

On Linux, if `docker info` reports `permission denied`, you are not in the
`docker` group rather than the daemon being stopped:

```bash
sudo usermod -aG docker "$USER"
newgrp docker
```

The default stack uses named volumes only, so no host-path translation is needed
on any platform.

---

## Quickest path (Docker)

```bash
curl -fsSL https://raw.githubusercontent.com/npci/ainxt-enterprise/main/install.sh | bash
```

The installer checks Docker, asks which model provider you want, generates your
secrets, starts everything, applies the database schema, and prints your login.

It starts **five** services:

| Service | Port | Why |
|---|---|---|
| `postgres` | 5432 | Main database **and** the pgvector store |
| `redis` | 6379 | Cache, RQ job queues, 9 KV logical DBs, login-lockout counters |
| `ollama` | 11434 | Free local model |
| `gateway` | 8000 | The API. Runs migrations, then gunicorn |
| `ai-ui` | 5173 | Web UI (nginx, serving the built bundle at `/portal/`) |

Kafka, Prometheus/Grafana and the embedding service are **not** started — see
[Optional features](#optional-features).

If any of those ports is already taken, the installer moves the container to the
next free port and records it in `.env`.

When it finishes, open **<http://localhost:5173/portal/>**.

### Everyday commands

```bash
docker compose ps                 # what is running
docker compose logs -f gateway    # follow the API log
docker compose down               # stop, keep data
docker compose down -v            # stop and DELETE all data
docker compose up -d --build      # rebuild after pulling changes

docker compose run --rm gateway python db/migrate.py   # migrations on their own
```

---

## Running natively instead

```bash
./install.sh --local
```

Runs the API and UI as ordinary processes (vite dev server with hot reload, and a
`.venv` you can attach a debugger to), with only PostgreSQL, Redis and Ollama in
Docker. The UI is then at **<http://localhost:5173/>** — no `/portal/` prefix,
which only applies to the production build.

```bash
./stop-local.sh                             # stop API + UI
tail -f log/gateway.out                     # API log
tail -f log/ai-ui.out                       # UI log
source .venv/bin/activate                   # use the virtualenv
docker compose stop postgres redis ollama   # stop the datastores too
```

Three things differ from the container path, all handled automatically:

- **Ports.** If you already run PostgreSQL on 5432, `localhost` resolves to `::1`
  first and the backend would silently connect to **your** database. Native mode
  relocates the containers and connects over `127.0.0.1`.
- **File storage.** `.env.example` points at `/var/lib/ainxt`, which a non-root
  user cannot create. Native mode stores under `./data`.
- **`gunicorn.conf.py` does not read `.env`.** The installer exports it first. If
  you start gunicorn by hand, do the same: `set -a; . ./.env; set +a`.

---

## LLM configuration

AiNxt bundles no model. Four providers are implemented — **there is no xAI/Grok,
Mistral, Cohere or Bedrock gateway in this release**:

| Provider | Variable | Notes |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | |
| OpenAI | `OPENAI_API_KEY` | |
| Google | `GEMINI_API_KEY` | |
| Local / in-house | `LOCAL_LLM_BASE_URL` | Any OpenAI-compatible server. Ollama is easiest |

The installer prompts for these and can take **more than one** — a team commonly
holds an Anthropic key and an OpenAI key and switches per request in the model
picker.

Keys are **instance-level**, set once in `.env` and shared by every user on the
deployment. Users do not supply their own; per-user spend is bounded by the
budget instead (default $50, shown bottom-left in the UI). To add a key later:

```bash
# edit .env, then
docker compose up -d gateway
```

### Using the free local model

```bash
docker exec ainxt-ollama ollama pull llama3.2   # ~2 GB
```

`LOCAL_LLM_BASE_URL` must point at the server **without** a trailing `/v1` — the
gateway appends it. In Docker that is `http://ollama:11434`; the installer sets
it for you.

A request for model `local` is served locally and will **never** silently fall
back to a cloud provider. If the local model is unavailable the request fails
with an explicit error instead of sending your prompt off the machine.

### The OpenAI-compatible endpoint

`POST /ainxt/v1/api/v1/chat/completions` — note the doubled `v1`; there is no
route at the bare `/v1/chat/completions`. It is **disabled by default** and
returns 403 `direct_access_disabled`. Enable it with:

```env
ENABLE_RAW_OPENAI_API=true
```

The web UI does not need this; it uses its own `/ask` route.

`GET /ainxt/v1/api/v1/models` lists the ids you can pass as `model`.

---

## Seed data and the first login

On first boot, when the database has no users, the gateway creates
`admin@ainxt.local`, generates a random password, and prints it **once**:

```
============================================================
  AiNxt — Default admin user created
============================================================
  Email    : admin@ainxt.local
  Password : <generated — shown once>
```

`install.sh` captures it and shows it at the end. To choose it yourself, set
`SEED_ADMIN_PASSWORD` in `.env` **before** first boot. If you lose it, use
**Forgot password** on the login page, or reset the stack with
`docker compose down -v`.

The password is **not** regenerated on restart, and changing it in
**Profile → Security** persists.

### Example agents and skills

Boot also seeds ~28 example agents, a set of skills, model rate cards and
department budget rows. To re-run seeding by hand:

```bash
docker compose run --rm gateway python scripts/seed.py
```

Set `SEED_ADMIN_PASSWORD` / `SEED_USER_PASSWORD` first if you want stable
credentials; otherwise generated ones are not applied to accounts that already
exist.

### How login works

1. `GET /auth/ui-config` — the page asks which features and auth methods are on.
2. Email + password + a CAPTCHA. **The CAPTCHA is browser-side only**; the real
   brute-force protection is server-side — rate limiting plus a **15-minute
   lockout after 5 failed attempts** (`LOGIN_LOCKOUT_SECONDS`).
3. `POST /auth/login` returns a JWT (HS256, signed with `JWT_SECRET`, valid
   `JWT_EXPIRE_HOURS`, default 24) as both a cookie and a body field.
4. Every later call sends `Authorization: Bearer <jwt>`. `POST /auth/refresh`
   renews it; `POST /auth/logout` blacklists the token immediately.

`ENABLE_SELF_REGISTRATION` is **`true`** by default — anyone who can reach the
page can create an account. Set it to `false` before exposing an instance.

---

## Optional features

Each is off by default and started explicitly.

### Kafka

```bash
docker compose --profile kafka up -d
```
Then set `KAFKA_ENABLED=true` and `KAFKA_BOOTSTRAP=localhost:9092` in `.env` and
restart the gateway.

### Prometheus + Grafana

```bash
docker compose --profile observability up -d     # :9090 and :3000
```

### Embedding service (semantic search / RAG)

```bash
docker compose --profile embed up -d
```
**Note:** its image `ghcr.io/ainxt/embed-svc:latest` is not published yet, so
this will fail to pull until it is. Leave `EMBED_SVC_URL` empty to run without
semantic search. This is why `/health` reports `degraded` on a default install —
`embed_svc` is absent by design.

### SMTP (email notifications)

```env
AINXT_SMTP_HOST=smtp.example.com
AINXT_SMTP_PORT=587
AINXT_SMTP_USER=...
AINXT_SMTP_PASSWORD=...
```
Without these, email features are disabled and the platform logs
`SMTP: not configured` at startup. Nothing else is affected.

### LDAP / Active Directory

```env
LDAP_URL=ldap://dc.example.com
LDAP_BASE_DN=dc=example,dc=com
LDAP_BIND_PASSWORD=...
```
Leave unset for local password auth.

### Object storage for chat attachments

MinIO is **not** bundled. `core/storage.py` defaults `MINIO_ENDPOINT` to
`localhost:9000`, so with nothing there you will see connection-refused retries
at startup. Attachment upload/download is the only thing affected. Leave the
`MINIO_*` variables commented out to run without it.

### SDLC templates

`/ainxt/v1/api/templates*` need content from the separate **ainxt-os**
repository. Without it they return 503 with an explanatory message. Point
`AINXT_OS_ROOT` at a checkout, or place one at `./ainxt`.

---

## Troubleshooting

**Start here.** Before working through the symptoms below, run the check script
from the repository root:

```bash
./doctor.sh
```

It goes through configuration, containers, database, API, web UI and optional
features, and prints each as ✓ working, ✗ broken or · not enabled. Every ✗ is
followed by the command that addresses it, so in most cases you will not need
the rest of this section.

Two things worth knowing about what it checks:

- It counts tables in the **`ainxt`** schema, not `public`. The platform's tables
  are all in `ainxt`, so a query against `public` returns zero on a perfectly
  healthy install.
- It sends a login with deliberately wrong credentials and expects a **401**. A
  500 there means the authentication path is raising rather than rejecting, which
  no amount of "the container is healthy" will reveal.

Use `./doctor.sh --local` if you installed with `--local`, and `--json` if you
want to parse the result.

**Docker Desktop will not start, or `docker info` cannot connect.**
A stale `com.docker.backend` can hold the socket with no GUI behind it:
```bash
pgrep -fl "Docker Desktop.app/Contents/MacOS"    # no output = orphaned backend
pkill -9 -f "MacOS/com.docker.backend"
open -a Docker
```

**`docker compose up` fails to bind a port.**
Something already listens there. Set `POSTGRES_PORT`, `REDIS_PORT`,
`OLLAMA_PORT`, `GATEWAY_PORT` or `UI_PORT` in `.env`. `install.sh` does this
automatically. If you change `POSTGRES_PORT`, change `PGVECTOR_PORT` to match —
one server hosts both.

**Gateway exits immediately with `ENABLE_INJECTION_SCAN=true but INJECTION_SCAN_URL is not set`.**
The prompt-injection scanner is a separate service not included here. Set
`ENABLE_INJECTION_SCAN=false` (the installer does).

**`gunicorn: '/app/log/error.log' isn't writable`.**
`GUNICORN_LOG_DIR` is not writable. Leave it unset to log to the console, or
point it somewhere writable.

**`"ollama": "error: [Errno 111] Connection refused"` in `/health`.**
`OLLAMA_URL` is pointing at `localhost` from inside a container, where that means
the gateway itself. It must be `http://ollama:11434` in Docker. Also note the
health check reads `OLLAMA_URL`, not `OLLAMA_BASE_URL`.

**Chat returns `Error: no gateway available`.**
No provider is usable: no cloud key set, and no local model reachable. Add a key
to `.env` and `docker compose up -d gateway`, or
`docker exec ainxt-ollama ollama pull llama3.2` and select the `local` model.

**`Account temporarily locked after 5 failed attempts`.**
Working as intended. Wait 15 minutes, or clear it:
`docker exec ainxt-redis redis-cli DEL "login:fail:admin@ainxt.local"`.

**`/health` says `degraded` but everything works.**
Expected on a default install: `embed_svc` is an opt-in service, and `docker`
shows `unavailable (fallback: subprocess executor)` because the gateway has no
Docker socket. Neither blocks normal use.

**Migrations fail.**
`db/migrate.py` exits non-zero and prints exactly which statements failed and
which required objects are missing. Re-run it directly to see the full output:
```bash
docker compose run --rm gateway python db/migrate.py
```
`MIGRATE_ALLOW_PARTIAL=true` downgrades failures to warnings — only for
deliberately partial legacy databases.

**Where are the logs?**
Container: `docker compose logs -f gateway`. Application logs are structured JSON
at `/app/log/app/agent.log` inside the container (`log/app/agent.log` natively) —
`docker compose logs` shows the gunicorn/stdout stream, which does not include
every application log line.
