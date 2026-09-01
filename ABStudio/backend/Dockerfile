# ============================================================================
# DEVELOPMENT IMAGE — NOT FOR PRODUCTION
# ============================================================================
# Production serves ABStudio through the gateway: the root Dockerfile runs
# `gateway:app` on :8000, which imports ABStudio's routers under
# /ainxt/v1/api/abs. This image runs ABStudio's own app on :8002 and is not
# referenced by any compose file or service unit.
#
# Do not promote this image to production without first addressing:
#   * --reload below (dev-only; single worker, watches the filesystem).
#   * Standalone auth: with the gateway's auth package unavailable,
#     app/api/deps.py falls back to require_framework_access, whose stub in
#     app/models.py returns a hardcoded role="admin" user WITHOUT
#     authenticating. Combined with --host 0.0.0.0 that is an unauthenticated
#     admin API on the network.
#   * CORS_ALLOW_ORIGINS must be set explicitly (app/main.py now refuses to
#     start without it unless APP_ENV=development).
# ============================================================================
# Base image pinned by DIGEST, not just tag: a tag is mutable, so an unpinned
# build is not reproducible and silently picks up whatever the tag points at
# today. The tag is kept alongside the digest for readability. Refresh with:
#   docker buildx imagetools inspect python:3.11-slim
FROM python:3.11-slim@sha256:be1575ed968de893bd54f4c56315ff7c4736ce522c1bca08fd521731aafc0d76

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# Declares this image as development. app/main.py fails closed on CORS when
# APP_ENV is anything else and CORS_ALLOW_ORIGINS is unset. If you ever build
# this for a real environment, remove this line and set CORS_ALLOW_ORIGINS.
ENV APP_ENV=development

WORKDIR /app

# bubblewrap enables optional OS-level network isolation for the
# code_executor/execute_code sandbox subprocess (SANDBOX_ISOLATION=bwrap,
# see agent_factory/pipeline.py ToolDispatcher — security review F-02).
# Disabled by default; installing it here just makes the opt-in available.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl bubblewrap \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8002

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002", "--reload"]
