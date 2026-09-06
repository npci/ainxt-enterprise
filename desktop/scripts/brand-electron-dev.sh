#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Brand the DEV Electron bundle as "AiNxt" (name + icon) for `npm start`.
# The packaged build already uses build.productName + build/icon.icns; this only
# fixes the macOS dev bundle (node_modules/electron) where the bold menu-bar title
# and dock icon would otherwise read "Electron". Safe to re-run; called from
# postinstall so it survives `npm install`. No-op on non-macOS.
set -e
cd "$(dirname "$0")/.."

APP="node_modules/electron/dist/Electron.app"
PLIST="$APP/Contents/Info.plist"
RES="$APP/Contents/Resources"

[ "$(uname)" = "Darwin" ] || { echo "brand-electron-dev: not macOS, skipping"; exit 0; }
[ -f "$PLIST" ] || { echo "brand-electron-dev: Electron dev bundle not found, skipping"; exit 0; }

/usr/libexec/PlistBuddy -c "Set :CFBundleName AiNxt" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName AiNxt" "$PLIST" 2>/dev/null || true
[ -f build/icon.icns ] && cp build/icon.icns "$RES/electron.icns" 2>/dev/null || true

# Force macOS to re-read the bundle (name + icon) — without this, LaunchServices
# and the Dock keep showing the cached "Electron" name / old icon.
touch "$APP"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
[ -x "$LSREGISTER" ] && "$LSREGISTER" -f "$APP" 2>/dev/null || true

echo "brand-electron-dev: dev bundle branded as AiNxt (re-registered with LaunchServices)"
