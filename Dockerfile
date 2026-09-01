# ── Build stage ────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Optional OCR engine, off by default. Build with --build-arg WITH_OCR=1 (or
# `./install.sh --with-ocr`) to include it. Kept as a separate layer after the
# main install so toggling it does not invalidate the expensive layer above.
ARG WITH_OCR=0
COPY requirements-ocr.txt .
RUN if [ "$WITH_OCR" = "1" ]; then         pip install --no-cache-dir --user -r requirements-ocr.txt;     else         echo "PaddleOCR not installed (WITH_OCR=0). rapidocr-onnxruntime provides OCR.";     fi

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
#                  built-in skills under ABStudio/skills/, application source,
#                  or .env. This is the control that stops one user's custom
#                  skill from poisoning code that every other user executes.
#
# libgl1 + libglib2.0-0: native libraries OpenCV links against. Without them
# `import cv2` fails with "libGL.so.1: cannot open shared object file", and
# because BOTH OCR engines import cv2 transitively (rapidocr-onnxruntime and
# paddleocr), OCR was silently unavailable in every container build — the code
# catches the ImportError and logs a warning, so nothing surfaced it. No AiNxt
# code calls cv2 directly; it is only used inside those OCR libraries.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
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

# Copy application code
COPY --chown=appuser:appuser . .

# Make sure scripts are executable
RUN chmod +x scripts/setup.sh 2>/dev/null || true

# Writable log directory for gunicorn. `log/` is gitignored, so it never
# arrives with the source and must be created here, owned by the runtime user.
RUN mkdir -p /app/log && chown appuser:appuser /app/log /app

USER appuser

# Ensure local pip packages are in PATH
ENV PATH=/home/appuser/.local/bin:$PATH
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
