# ──────────────────────────────────────────────────────────────────────────────
# AiNxt Cowork — document-skill sandbox
#
# The image the office agent runs its generated document build scripts in
# (docx/pptx/xlsx/pdf). Preloaded with exactly the tooling the craft guidance in
# skills/ainxt_doc_craft/ tells the agent to use:
#   - node 20 + `docx` (docx-js)        → author .docx (and .pdf, via export)
#   - node 20 + `pptxgenjs`             → author .pptx
#   - python3 + openpyxl                → author .xlsx
#   - python3 + python-docx/pypdf/      → inspect and post-process OOXML
#     reportlab/lxml
#   - pandoc                            → read/convert .docx ↔ markdown
#   - LibreOffice (soffice, headless)   → export to .pdf, recalculate .xlsx formulas
#   - poppler-utils (pdftoppm)          → .pdf → page images (in-app preview and
#                                         the input to the visual QA loop)
#
# Runs network-disabled, non-root, memory/CPU capped (see sandbox/doc_executor.py).
#
# The NPCI composition libraries (ainxt-deck / ainxt-doc / ainxt_sheet) are baked in
# so generated build scripts express only CONTENT. The craft guidance (SKILL.md
# etc.) is NOT baked in — it is read from the host filesystem and injected as LLM
# context by workers/doc_worker.py and connectors/mcp_bridge.py, so the image
# carries only what build code actually reads at runtime.
# ──────────────────────────────────────────────────────────────────────────────
# Base image pinned by DIGEST, not just tag: a tag is mutable, so an unpinned
# build is not reproducible and silently picks up whatever the tag points at
# today. The tag is kept alongside the digest for readability. Refresh with:
#   docker buildx imagetools inspect node:20-bookworm-slim
FROM node:20-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/home/sandbox \
    # LibreOffice needs a writable profile dir; keep it inside the work area.
    PYTHONUNBUFFERED=1

# System tooling for the document skills.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        pandoc \
        libreoffice-writer libreoffice-impress libreoffice-calc \
        poppler-utils \
        fonts-liberation fonts-dejavu fonts-noto-core \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# docx-js (Word "Creating New Documents") + pptxgenjs (PowerPoint from scratch).
RUN npm install -g docx@9 pptxgenjs@3 --no-fund --no-audit

# Python libs the skill scripts import (PEP668 → break-system-packages in a sandbox is fine).
RUN pip3 install --no-cache-dir --break-system-packages \
        python-docx openpyxl pypdf reportlab lxml markdown beautifulsoup4

# Brand assets the generated build scripts / npci libs reference by absolute path
# (e.g. /opt/ainxt-brand/npci-mark.png). Every reference is guarded with an
# existence check, so an image built without a logo still produces valid,
# brand-coloured documents — the logo image is simply skipped when absent.
#
# NOTE: the repo-root .dockerignore excludes *.md, so NPCI_BRAND.md is NOT copied
# into the image. That is intentional/harmless — the brand *guidance* markdown is
# only consumed on the host (injected as LLM context), never read at container
# runtime. Only binary brand assets (a logo png, if added later) matter here.
COPY skills/ainxt_doc_craft/brand/ /opt/ainxt-brand/

# NPCI composition libraries. Preinstalled so generated build scripts express only
# CONTENT — `require('ainxt-deck')` / `require('ainxt-doc')` / `from ainxt_sheet import
# Book` instead of reproducing ~300 lines of helper definitions. Small local models
# cannot copy that much scaffolding reliably, so without this the feature is
# frontier-model-only. Kept beside the guidance in skills/ainxt_doc_craft/lib/ so
# the rules and their enforcement are versioned together.
#
# The JS wrappers are INSTALLED GLOBALLY with `npm install -g <dir>` (NOT a bare
# COPY into node_modules). A bare copy is not reliably resolved from a script run
# in /work — Node walks up from the script dir and only falls back to NODE_PATH,
# which is brittle. `npm install -g` registers them in the same global prefix as
# their peers (docx / pptxgenjs), so `require('ainxt-doc')` resolves identically to
# `require('docx')`, regardless of the script's cwd. Their peer deps (docx /
# pptxgenjs) are already global from the step above, so `--no-save --omit=dev`
# avoids a network fetch (the build runs network-disabled).
COPY skills/ainxt_doc_craft/lib/ainxt-deck /opt/ainxt-lib/ainxt-deck
COPY skills/ainxt_doc_craft/lib/ainxt-doc  /opt/ainxt-lib/ainxt-doc
# npm install -g symlinks the global package back to /opt/ainxt-lib, so require()
# reads those source files at runtime — they must be world-readable (the sandbox
# runs as a non-root user), or require() fails with EACCES. chmod a+rX before the
# resolution assertion.
RUN npm install -g --no-save --omit=dev /opt/ainxt-lib/ainxt-deck /opt/ainxt-lib/ainxt-doc \
    && chmod -R a+rX /opt/ainxt-lib \
    && NODE_PATH=/usr/local/lib/node_modules node -e "require('ainxt-deck'); require('ainxt-doc')"   # fail the build early if unresolved

# Python wrapper on PYTHONPATH (this already works for xlsx).
COPY skills/ainxt_doc_craft/lib/ainxt_sheet.py /opt/ainxt-lib/ainxt_sheet.py
ENV PYTHONPATH=/opt/ainxt-lib

# Non-root sandbox user with a writable home (LibreOffice profile) + work dir.
RUN useradd -m -u 10001 -d /home/sandbox sandbox \
    && mkdir -p /work /home/sandbox/.config \
    && chown -R sandbox:sandbox /work /home/sandbox /opt/ainxt-brand /opt/ainxt-lib
USER sandbox
WORKDIR /work

# Node resolves the global `docx` / `pptxgenjs` / `npci-*` packages from scripts
# run in /work. Kept as a belt-and-braces fallback alongside the global install.
ENV NODE_PATH=/usr/local/lib/node_modules

# Default: idle; the executor injects and runs the build command per job.
CMD ["sleep", "infinity"]
