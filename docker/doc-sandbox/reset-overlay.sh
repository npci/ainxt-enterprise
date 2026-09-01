#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Reset the ainxt-doc-sandbox image back to its ORIGINAL, pre-wrapper state.
#
# build-overlay.sh snapshots the pristine base as ainxt-doc-sandbox:base-prewrappers
# before it layers the ainxt-* wrappers onto :latest. This script simply moves the
# :latest tag back to that snapshot, undoing the overlay — no rebuild, no network.
#
#   bash docker/doc-sandbox/reset-overlay.sh
#
# ⚠ IMPORTANT: the sandbox image and the worker CODE must match. This resets ONLY
# the image (removing the ainxt-* wrappers). If your checked-out code still tells
# the LLM to use ainxt-deck / ainxt_sheet / ainxt-doc, document generation will fail
# with "Cannot find module". So EITHER also check out the pre-wrapper code branch,
# OR re-apply the overlay (bash docker/doc-sandbox/build-overlay.sh) when done.
set -euo pipefail
IMAGE="ainxt-doc-sandbox:latest"
BASE_TAG="ainxt-doc-sandbox:base-prewrappers"

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker is not running." >&2
  exit 1
fi
if ! docker image inspect "$BASE_TAG" >/dev/null 2>&1; then
  echo "ERROR: snapshot '$BASE_TAG' not found — nothing to reset to." >&2
  echo "       (It is created by build-overlay.sh on its first run.)" >&2
  exit 1
fi

echo "Resetting $IMAGE → original base ($BASE_TAG)…"
docker tag "$BASE_TAG" "$IMAGE"

echo "✓ $IMAGE now points at the original pre-wrapper image:"
docker images | grep "ainxt-doc-sandbox" || true

echo
echo "Next:"
echo "  • Restart the doc workers:   pm2 restart ainxt-doc-workers"
echo "  • REMEMBER: match the code to the image. If your branch still uses the"
echo "    ainxt-* wrappers, either checkout the pre-wrapper code or re-run"
echo "    bash docker/doc-sandbox/build-overlay.sh to put the wrappers back."
