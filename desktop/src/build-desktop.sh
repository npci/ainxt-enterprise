#!/bin/bash
# SPDX-License-Identifier: MIT
# Build AiNxt Desktop for macOS (arm64 + x64) and Windows (x64)
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Installing desktop dependencies…"
npm install

echo "==> Building macOS (arm64 + x64)…"
npm run build:mac

echo "==> Building Windows (x64)..."
npm run build:win

echo "==> Building Linux (x64 AppImage)..."
npm run build:linux

echo ""
echo "✅ Desktop builds complete — artifacts in dist/"
ls dist/ 2>/dev/null || true
