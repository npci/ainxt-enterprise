#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build the composition-wrapper OVERLAY on top of the existing ainxt-doc-sandbox image.
# Use this on locked-down / air-gapped hosts where the full build.sh cannot reach
# the apt/npm registries. It adds ONLY the ainxt-doc / ainxt-deck / ainxt_sheet
# wrappers to the already-built base image — no base packages are re-downloaded,
# so no internet is required.
#
#   bash docker/doc-sandbox/build-overlay.sh
#
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="ainxt-doc-sandbox:latest"
BASE_TAG="ainxt-doc-sandbox:base-prewrappers"

cd "$REPO_ROOT"
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker is not running." >&2
  exit 1
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "ERROR: base image '$IMAGE' not found. This overlay must be applied on top" >&2
  echo "       of an existing ainxt-doc-sandbox image. Build the full image once" >&2
  echo "       on a networked host (bash docker/doc-sandbox/build.sh) first." >&2
  exit 1
fi

# Preserve the current image under a base tag so the overlay's FROM is stable and
# re-runnable (a second run then layers on the ORIGINAL base, not on itself).
if ! docker image inspect "$BASE_TAG" >/dev/null 2>&1; then
  echo "Tagging current $IMAGE as $BASE_TAG (one-time snapshot of the base)…"
  docker tag "$IMAGE" "$BASE_TAG"
fi

echo "Building composition-wrapper overlay on top of $BASE_TAG (no network needed)…"
# Always layer on the stable base snapshot (BASE_IMAGE arg), then move the
# :latest tag to the result — so re-running never stacks overlay-on-overlay.
# We do NOT pass --network none: `npm install -g --offline` already guarantees no
# registry fetch, and some Docker/BuildKit setups error oddly under --network none
# even for purely-local installs. If your daemon lacks internet the --offline flag
# is what keeps this working; nothing here needs to reach out.
DOCKER_BUILDKIT=1 docker build \
  --build-arg "BASE_IMAGE=$BASE_TAG" \
  -f docker/doc-sandbox/Dockerfile.overlay \
  -t "$IMAGE" \
  "$REPO_ROOT"

echo
echo "✓ Built overlay → $IMAGE"
echo "Smoke test (composition libs present, run from /work):"
docker run --rm -w /work "$IMAGE" sh -c '
  node -e "require(\"docx\"); console.log(\"  docx-js OK\")" &&
  node -e "require(\"pptxgenjs\"); console.log(\"  pptxgenjs OK\")" &&
  python3 -c "import docx, openpyxl, pypdf; print(\"  python libs OK\")" &&
  command -v soffice >/dev/null && echo "  libreoffice OK" &&
  command -v pdftoppm >/dev/null && echo "  poppler OK" &&
  node -e "require(\"ainxt-deck\")" && echo "  ainxt-deck OK" &&
  node -e "require(\"ainxt-doc\")"  && echo "  ainxt-doc OK" &&
  python3 -c "import ainxt_sheet; ainxt_sheet.Book" && echo "  ainxt_sheet OK"
'
