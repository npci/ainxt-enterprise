# syntax=docker/dockerfile:1
# The line above is a BuildKit frontend directive, not a comment — it MUST be
# the first line of the file. Required for `RUN --mount=type=cache` below,
# which persists pip's download cache across separate builds/invocations
# (unlike Docker's normal layer cache, which only survives while the
# preceding COPY's content is unchanged) so a rebuild after any
# requirements.txt change — or a build cache prune, or a fresh CI runner —
# doesn't re-download the same wheels from PyPI every time. The cache lives
# outside the image layer, so it costs nothing in final image size.
# ── Build stage ────────────────────────────────────────────────────────────────
FROM docker:27-cli AS docker_cli

FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Upgrade the packaging toolchain itself before installing anything else — the
# python:3.11-slim base image's bundled pip/setuptools/wheel had known
# path-traversal/symlink CVEs (GHSA-4xh5-x5gv-qwph, GHSA-h35f-9h28-mq5c,
# GHSA-8rrh-rw8j-w5fx) exercised when extracting sdists/wheels.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip setuptools wheel
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --user -r requirements.txt

# Optional OCR engine, off by default. Build with --build-arg WITH_OCR=1 (or
# `./install.sh --with-ocr`) to include it. Kept as a separate layer after the
# main install so toggling it does not invalidate the expensive layer above.
ARG WITH_OCR=0
COPY requirements-ocr.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$WITH_OCR" = "1" ]; then         pip install --user -r requirements-ocr.txt;     else         echo "PaddleOCR not installed (WITH_OCR=0). rapidocr-onnxruntime provides OCR.";     fi

# ── CodeWiki build stage (optional) ─────────────────────────────────────────
# codewiki (github.com/FSoft-AI4Code/CodeWiki, the engine behind the
# CodeWiki panel's `codewiki generate` CLI call — see workers/codewiki_worker.py)
# requires Python >=3.12; the rest of the platform pins to 3.11 (see the
# builder stage above), so it can't share the main app's site-packages.
#
# Installed straight into this stage's OWN system Python (no venv) and then
# the whole /usr/local prefix is copied into the runtime stage below at a
# private path. A venv's bin/python is normally just a symlink to the
# system interpreter it was created against (see pyvenv.cfg's `home`) —
# copying only the venv across stages leaves that symlink pointing at
# /usr/local/bin/python, which in the RUNTIME stage resolves to the
# platform's own Python 3.11, not 3.12, silently running the wrong
# interpreter against the wrong sys.path (confirmed: `import codewiki`
# failed after the venv-only copy, using site-packages the venv never
# pip-installed anything into). Copying the whole self-contained
# installation (interpreter + shared lib + stdlib + site-packages) avoids
# depending on any path that only resolves correctly inside this stage.
#
# ON by default — unlike WITH_OCR above, most deployments DO want the
# CodeWiki panel to actually work rather than silently accept requests it
# can never fulfil. It's still a heavy, network-dependent install (git+https
# clone, GraphRAG-related deps); opt out with --build-arg WITH_CODEWIKI=0
# (or `./install.sh --without-codewiki`) if you don't plan to use it and
# want a smaller/faster build. Without it, this stage's Python has no
# codewiki installed, so CodeWiki generation fails with a clear
# "No module named codewiki" in the job's logs rather than the container
# failing to build/start over an optional feature.
FROM python:3.12-slim AS codewiki_builder
# nodejs/npm: one of codewiki's transitive deps (pythonmonkey, pulled in via
# the `pminit` build hook) needs `npm` on PATH to build its JS component at
# pip-install time — without it, "PythonMonkey build error: Unable to find
# npm on the system" fails the whole install.
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential nodejs npm && rm -rf /var/lib/apt/lists/*
ARG WITH_CODEWIKI=1
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$WITH_CODEWIKI" = "1" ]; then \
        pip install git+https://github.com/FSoft-AI4Code/CodeWiki.git; \
    else \
        echo "codewiki not installed (WITH_CODEWIKI=0) — CodeWiki panel generation will fail until built with --build-arg WITH_CODEWIKI=1"; \
    fi

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Runtime dependencies only
#
# bubblewrap: OS-level sandbox for the code_executor / execute_code subprocess
# (agent_factory/pipeline.py::_wrap_with_bwrap). Installing it only makes the
# option AVAILABLE — it stays inert until SANDBOX_ISOLATION=bwrap is set.
# Without the binary present, that flag silently no-ops (pipeline.py logs a
# warning and falls back to the unsandboxed path), so the package must ship
# here for the flag to mean anything in production.
#
# What it buys once enabled (see _wrap_with_bwrap):
#   --unshare-net  no network namespace -> no egress, no reaching internal
#                  services (Postgres/Redis/embed-svc) from tool-run code.
#   --ro-bind / /  whole filesystem read-only except the run's output dir and
#                  /tmp -> user-authored skill scripts can no longer overwrite
#                  built-in skills under AgentStudio/skills/, application source,
#                  or .env. This is the control that stops one user's custom
#                  skill from poisoning code that every other user executes.
#
# libgl1 + libglib2.0-0: native libraries OpenCV links against. Without them
# `import cv2` fails with "libGL.so.1: cannot open shared object file", and
# because BOTH OCR engines import cv2 transitively (rapidocr-onnxruntime and
# paddleocr), OCR was silently unavailable in every container build — the code
# catches the ImportError and logs a warning, so nothing surfaced it. No AiNxt
# code calls cv2 directly; it is only used inside those OCR libraries.
#
# git: workers/index_worker.py shells out to the `git` binary (subprocess.run)
# to clone repos for codebase indexing. Without it, every index job reaches
# _resolve_path() and fails with FileNotFoundError: 'git' — invisible until a
# worker actually consumes an index_queue job, since nothing surfaced it at
# build or gateway-boot time.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    git \
    bubblewrap \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for security. Created before the copies below so that both the
# dependency tree and the application code can be chowned to it in one pass.
RUN useradd -m -u 1000 appuser

# Copy installed packages from builder into the RUNTIME USER's home.
#
# These were installed with `pip install --user`, so they live under the
# installing user's ~/.local. They must not be copied to /root/.local here:
# /root is mode 700, so appuser cannot read it, and Python derives the per-user
# site-packages path from $HOME — /root/.local/lib/... would never be added to
# appuser's sys.path even if it were readable. Both faults together made every
# dependency unimportable and the container exited immediately on start.
COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local

# Self-contained Python 3.12 installation for codewiki (see codewiki_builder
# stage above) — a separate interpreter + stdlib + site-packages from the
# app's own 3.11, invoked via CODEWIKI_PYTHON by workers/codewiki_worker.py's
# subprocess calls. Copied to a private path, not /usr/local, so it can't
# collide with or shadow the runtime image's own Python 3.11 install.
COPY --from=codewiki_builder --chown=appuser:appuser /usr/local /opt/codewiki-python

# Copy application code
COPY --chown=appuser:appuser . .

# The document worker starts short-lived, network-isolated document-sandbox
# containers. It needs only the Docker client; the daemon remains on the host.
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker

# Make sure scripts are executable
RUN chmod +x scripts/setup.sh 2>/dev/null || true

# Writable log directory for gunicorn. `log/` is gitignored, so it never
# arrives with the source and must be created here, owned by the runtime user.
# /app/storage is the local-backend upload tree (core/storage.py) and is
# mounted as the ainxt_uploads volume in docker-compose.yml. It must exist here,
# owned by appuser: Docker only inherits ownership for a named-volume mountpoint
# that already exists in the image — otherwise it creates it root-owned and
# every upload fails with EACCES under USER appuser.
RUN mkdir -p /app/log /app/storage/chat_attachments /var/lib/ainxt/docs && \
    chown -R appuser:appuser /app/log /app/storage && \
    chown appuser:appuser /app /var/lib/ainxt/docs

# Pre-create the model cache dir owned by appuser, BEFORE the runtime `USER`
# switch below. Docker copies a mount point's existing ownership into a named
# volume the first time it's created (services/embed_svc mounts one here for
# downloaded Docling/HF/reranker weights) — without this, the volume is
# created root-owned, appuser can't write into it, and every model download
# fails silently (sentence-transformers/docling fall back or error out).
RUN mkdir -p /home/appuser/.cache && chown -R appuser:appuser /home/appuser/.cache
# Same reasoning, for the gateway's KB document storage (KB_DOC_STORAGE_PATH,
# see docker-compose.yml's gateway.volumes). /var/lib is root-owned; without
# this, the volume Docker creates there on first mount is root-owned too, and
# appuser can't write into it.
RUN mkdir -p /var/lib/ainxt/kb_docs && chown -R appuser:appuser /var/lib/ainxt

USER appuser

# Ensure local pip packages are in PATH
ENV PATH=/home/appuser/.local/bin:$PATH
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV CODEWIKI_PYTHON=/opt/codewiki-python/bin/python3.12

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/ainxt/v1/api/health || exit 1

CMD ["python", "-m", "uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
