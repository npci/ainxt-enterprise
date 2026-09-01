#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Build the AiNxt Cowork document-skill sandbox image.
# Build context is the repo root (so the Dockerfile can COPY the brand assets +
# composition libs from skills/ainxt_doc_craft/).
#
#   bash docker/doc-sandbox/build.sh
#
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="ainxt-doc-sandbox:latest"

cd "$REPO_ROOT"
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker is not running. Start Docker Desktop and retry." >&2
  exit 1
fi

echo "Building $IMAGE (this pulls LibreOffice + fonts — first build is a few minutes)…"
docker build -f docker/doc-sandbox/Dockerfile -t "$IMAGE" "$REPO_ROOT"

echo
echo "✓ Built $IMAGE"
echo "Smoke test (tooling + composition libs present):"
# Run from /work as the sandbox user — this reproduces the REAL generation
# invocation (node /work/build.js, cwd=/work), so a module-resolution problem
# with the ainxt-* wrappers surfaces here instead of only at request time.
docker run --rm -w /work "$IMAGE" sh -c '
  node -e "require(\"docx\"); console.log(\"  docx-js OK\")" &&
  node -e "require(\"pptxgenjs\"); console.log(\"  pptxgenjs OK\")" &&
  python3 -c "import docx, openpyxl, pypdf; print(\"  python libs OK\")" &&
  command -v soffice >/dev/null && echo "  libreoffice OK" &&
  command -v pdftoppm >/dev/null && echo "  poppler OK" &&
  command -v pandoc >/dev/null && echo "  pandoc OK" &&
  node -e "require(\"ainxt-deck\")" && echo "  ainxt-deck OK" &&
  node -e "require(\"ainxt-doc\")"  && echo "  ainxt-doc OK" &&
  python3 -c "import ainxt_sheet; ainxt_sheet.Book" && echo "  ainxt_sheet OK"
'
