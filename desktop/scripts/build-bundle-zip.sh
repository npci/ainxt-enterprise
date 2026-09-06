#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# =============================================================================
# desktop/scripts/build-bundle-zip.sh
#
# Produce a distributable AiNxt desktop bundle ZIP via electron-builder.
#
# The `win`/`mac` electron-builder targets are configured as `zip` in
# desktop/package.json, so a successful build drops the bundle ZIP straight into
# desktop/dist/  (e.g. AiNxt-1.0.0-win-x64.zip).
#
# IMPORTANT — what the desktop bundle does and does NOT contain:
#   * The renderer UI is NOT bundled. At runtime the app loads the web UI from
#     the gateway (`${apiBase}/portal/`, default http://localhost:8000). So your
#     ai-ui (React) fixes ship by DEPLOYING ai-ui behind the gateway — NOT by
#     rebuilding the desktop bundle. Rebuild the desktop bundle only when files
#     under desktop/src/** (main/preload/IPC) change.
#   * The bundle DOES embed the AiNxt CLI binary — fetched at build time by
#     scripts/fetch-cli.mjs (pinned in cli-version.json), staged into
#     resources/bin/ — and resources/code-skills (see build.extraResources in
#     package.json).
#
# Usage:
#   cd desktop
#   bash scripts/build-bundle-zip.sh            # default: windows x64 (electron-builder)
#   bash scripts/build-bundle-zip.sh win        # windows x64
#   bash scripts/build-bundle-zip.sh mac        # macOS arm64 + x64
#   bash scripts/build-bundle-zip.sh all        # win + mac
#   bash scripts/build-bundle-zip.sh asar-swap  # FAST: repack app.asar into the
#                                               # prebuilt bundle + re-zip (no
#                                               # GitHub download — works behind
#                                               # a restricted network).
#
# Env toggles:
#   SKIP_PRECHECKS=1   skip the prerequisite checks (not recommended)
#   CLEAN_DIST=1       remove desktop/dist before building
# =============================================================================
set -euo pipefail

# ── Resolve paths (script may be invoked from anywhere) ──────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$DESKTOP_DIR/.." && pwd)"
cd "$DESKTOP_DIR"

TARGET="${1:-win}"

log()  { printf '\033[1;36m[build-bundle]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[build-bundle] WARN:\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[build-bundle] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# =============================================================================
# ASAR HOT-SWAP MODE
# -----------------------------------------------------------------------------
# electron-builder needs to download helper binaries (winCodeSign / nsis /
# app-builder-bin) from github.com, which some corporate networks block (TLS: "unable
# to get local issuer certificate"). When a previously-built extracted bundle is
# available, we can skip that entirely: repack the app.asar from the current
# desktop tree and drop it into that bundle's resources/, refresh bin/ +
# code-skills, then re-zip. This produces a shippable ZIP with only the app code
# updated (the Electron runtime is reused as-is).
# =============================================================================
if [[ "$TARGET" == "asar-swap" ]]; then
  # Locate the extracted prebuilt bundle (dir containing AiNxt.exe + resources/).
  BUNDLE_DIR="${BUNDLE_DIR:-}"
  if [[ -z "$BUNDLE_DIR" ]]; then
    for c in "AiNxt-win-browserfix" "AiNxt-win-x64-latest/AiNxt-win-browserfix" "AiNxt-win-x64-latest"; do
      if [[ -f "$c/AiNxt.exe" && -d "$c/resources" ]]; then BUNDLE_DIR="$c"; break; fi
    done
  fi
  [[ -n "$BUNDLE_DIR" && -f "$BUNDLE_DIR/AiNxt.exe" ]] \
    || die "No prebuilt bundle found (looked for AiNxt.exe + resources/). Set BUNDLE_DIR=/path/to/extracted/bundle."
  log "Using prebuilt bundle: $BUNDLE_DIR"

  ASAR_BIN="./node_modules/.bin/asar"
  [[ -x "$ASAR_BIN" || -f "${ASAR_BIN}.cmd" ]] || die "asar not installed (cd $DESKTOP_DIR && npm install)."

  # 1. Pack a fresh app.asar from a STAGING dir containing ONLY what belongs
  #    (electron-builder's build.files = src/** + build/**, plus node_modules and the
  #    root JS entry helpers). Packing the desktop/ root directly is WRONG — it would
  #    recurse into the extracted bundle dirs (AiNxt-win-*), dist/, etc. and produce a
  #    multi-GB corrupt asar that fills the disk. Always stage.
  log "Staging + packing fresh app.asar…"
  STAGE="$(mktemp -d)"
  cp -r src "$STAGE/"
  [[ -d build ]] && cp -r build "$STAGE/"
  cp -r node_modules "$STAGE/"
  cp package.json "$STAGE/" 2>/dev/null || true
  "$ASAR_BIN" pack "$STAGE" app.asar.NEW --unpack "*.node"
  rm -rf "$STAGE"
  # Sanity: a valid bundle asar is tens/low-hundreds of MB, never multi-GB.
  _asz=$(stat -c%s app.asar.NEW 2>/dev/null || echo 0)
  if [[ "$_asz" -gt 800000000 ]]; then
    rm -f app.asar.NEW; rm -rf app.asar.NEW.unpacked
    die "packed app.asar is ${_asz} bytes (>800MB) — staging captured junk; aborting to protect the disk."
  fi
  log "packed app.asar.NEW ($_asz bytes)"

  # 2. Swap the asar (+ its unpacked native-module sibling) into the bundle.
  #    No in-bundle backup (it bloats the zip and, on a full disk, corrupts the asar).
  RES="$BUNDLE_DIR/resources"
  # Remove any stray in-bundle backups from prior manual swaps (never ship them).
  rm -f "$RES"/app.asar.BAK "$RES"/app.asar.bak_* 2>/dev/null || true
  cp app.asar.NEW "$RES/app.asar"
  if [[ -d app.asar.NEW.unpacked ]]; then
    rm -rf "$RES/app.asar.unpacked"
    cp -r app.asar.NEW.unpacked "$RES/app.asar.unpacked"
  fi
  log "Swapped app.asar into $RES."

  # 3. Refresh embedded CLI binary + code-skills if newer copies exist locally.
  if [[ -f "resources/bin/ainxt-windows-x64.exe" ]]; then
    mkdir -p "$RES/bin"; cp resources/bin/ainxt-windows-x64.exe "$RES/bin/ainxt-windows-x64.exe"
    log "Refreshed resources/bin/ainxt-windows-x64.exe"
  fi
  if [[ -d "resources/code-skills" ]]; then
    rm -rf "$RES/code-skills"; cp -r resources/code-skills "$RES/code-skills"
    log "Refreshed resources/code-skills"
  fi

  # 4. Zip the bundle. Prefer 7-Zip, fall back to jar (both handle the size fine).
  mkdir -p dist
  APP_VERSION="$(node -p "require('./package.json').version" 2>/dev/null || echo 1.0.0)"
  OUT="dist/AiNxt-${APP_VERSION}-win-x64-$(date +%Y%m%d).zip"
  rm -f "$OUT"
  SEVENZIP="/c/Program Files/7-Zip/7z.exe"
  log "Zipping → $OUT"
  if [[ -x "$SEVENZIP" ]]; then
    ( cd "$BUNDLE_DIR" && "$SEVENZIP" a -tzip -mx=5 "$(cygpath -w "$DESKTOP_DIR/$OUT" 2>/dev/null || echo "$DESKTOP_DIR/$OUT")" "." >/dev/null )
  elif command -v jar >/dev/null; then
    ( cd "$BUNDLE_DIR" && jar -cfM "$DESKTOP_DIR/$OUT" . )
  else
    die "No zip tool found (need 7-Zip at '$SEVENZIP' or 'jar')."
  fi
  [[ -f "$OUT" ]] && log "Bundle ZIP created: $OUT  ($(du -h "$OUT" | cut -f1))" || die "zip step produced no file."
  log "Done (asar-swap)."
  exit 0
fi

# ── Map target → electron-builder flags + expected CLI binary ────────────────
case "$TARGET" in
  win) EB_FLAGS="--win --x64";            CLI_GLOB="ainxt-windows-x64.exe" ;;
  mac) EB_FLAGS="--mac --arm64 --x64";    CLI_GLOB="ainxt-macos-*"        ;;
  all) EB_FLAGS="--win --mac --x64";      CLI_GLOB="ainxt-windows-x64.exe" ;;
  *)   die "unknown target '$TARGET' (use: win | mac | all)" ;;
esac

APP_VERSION="$(node -p "require('./package.json').version" 2>/dev/null || echo "?")"
log "Target=$TARGET  version=$APP_VERSION  desktop=$DESKTOP_DIR"

# ── Prerequisite checks ──────────────────────────────────────────────────────
if [[ "${SKIP_PRECHECKS:-0}" != "1" ]]; then
  log "Running prerequisite checks…"

  command -v node >/dev/null || die "node not found on PATH"

  # electron-builder is a devDependency; must be installed.
  if [[ ! -x "node_modules/.bin/electron-builder" && ! -f "node_modules/.bin/electron-builder.cmd" ]]; then
    die "electron-builder not installed. Run: (cd $DESKTOP_DIR && npm install)"
  fi

  # CLI binary is embedded via build.extraResources. Stage it now (the same
  # step electron-builder's beforePack hook runs). Honors AINXT_CLI_BIN_SRC /
  # AINXT_CLI_FROM_SOURCE / AINXT_CLI_SKIP_FETCH (see scripts/fetch-cli.mjs).
  if [[ "${AINXT_CLI_SKIP_FETCH:-0}" != "1" ]]; then
    log "Staging ainxt CLI (target=$TARGET)..."
    node scripts/fetch-cli.mjs --target="$TARGET" \
      || die "fetch-cli.mjs failed — cannot embed the CLI. Set AINXT_CLI_SKIP_FETCH=1 to build without it."
  else
    warn "AINXT_CLI_SKIP_FETCH=1 — building without an embedded CLI."
  fi

  # code-skills resources are referenced by every platform's extraResources.
  if [[ ! -d "resources/code-skills" ]]; then
    warn "resources/code-skills is missing but is referenced by build.extraResources.
     electron-builder will error on the missing 'from'. Create/populate it, or
     temporarily remove that extraResources entry from package.json."
  fi

  # Restricted networks may block github.com; electron-builder pulls helper binaries
  # (winCodeSign, nsis, app-builder-bin) from there unless mirrors are set.
  if [[ -z "${ELECTRON_BUILDER_BINARIES_MIRROR:-}" ]] \
      && ! grep -q '^electron_builder_binaries_mirror=' .npmrc 2>/dev/null; then
    warn "No electron-builder binaries mirror configured (.npmrc / env). If this host
     cannot reach github.com the build may hang/fail downloading helper binaries.
     Set ELECTRON_BUILDER_BINARIES_MIRROR or uncomment the mirror in desktop/.npmrc."
  fi

  log "Prechecks done."
fi

# ── Clean (optional) ─────────────────────────────────────────────────────────
if [[ "${CLEAN_DIST:-0}" == "1" ]]; then
  log "Cleaning desktop/dist…"
  rm -rf dist
fi

# ── Build ────────────────────────────────────────────────────────────────────
# publish=never so it never tries to push a release; we only want local artifacts.
log "Running: electron-builder $EB_FLAGS --publish never"
./node_modules/.bin/electron-builder $EB_FLAGS --publish never

# ── Report artifacts ─────────────────────────────────────────────────────────
log "Build complete. ZIP artifacts in desktop/dist/:"
if ls dist/*.zip >/dev/null 2>&1; then
  ls -lh dist/*.zip | awk '{print "   " $9 "  (" $5 ")"}'
else
  warn "No .zip found in desktop/dist/. Check electron-builder output above."
fi

log "Done."
