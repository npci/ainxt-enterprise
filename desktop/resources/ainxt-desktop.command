#!/bin/bash
# ============================================================================
# AiNxt Desktop first-run helper (macOS)
#
# Lives inside the DMG next to the AiNxt.app icon — double-click it ONCE,
# from the mounted DMG, before dragging AiNxt.app to Applications. It clears
# the Gatekeeper quarantine flag on an unsigned build and launches the app
# with the settings below.
#
# You do NOT need this again after the first launch: settings you actually
# care about (the gateway URL) persist afterward via the app's own
# tray -> API Server -> Custom... menu — this script does not travel with
# the app once it's in /Applications, and normally it doesn't need to.
#
# This is the mac counterpart to ainxt-desktop.bat (Windows portable ZIP) —
# see that file for the full explanation of each variable below.
# ============================================================================

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/AiNxt.app"

# --- REQUIRED (first launch only): the gateway this desktop app talks to ---
# Must be wherever the web app UI itself is served from — NOT necessarily
# your backend API's own port (see ainxt-desktop.bat's comment for the full
# split-deployment explanation). Once set here and launched once, you can
# change it later from the app's own tray menu instead of running this again.
export AINXT_GATEWAY_URL=""

# --- Set to 1 to auto-open Developer Tools on launch (for debugging) -------
export AINXT_DEVTOOLS=0

# --- TLS certificate verification -------------------------------------------
# LEAVE THESE AT 0. See ainxt-desktop.bat for why — never ship or use these
# against a real deployment.
export AINXT_TLS_INSECURE=0
export AINXT_INSECURE_TLS=0

# --- CLI Protocol Tracer -----------------------------------------------
# Logs every message between desktop and CLI to ~/.ainxt/cli-trace.log
# Set to 1 only when debugging — leave 0 otherwise.
export AINXT_CLI_TRACE=0

# --- How to drive the CLI (which protocol) ---------------------------------
# Unset (or "acp") -> ACP JSON-RPC [DEFAULT]. "streamjson" -> legacy protocol.
# Set this only if the embedded CLI doesn't speak ACP.
# export AINXT_CLI_PROTOCOL=streamjson

# --- Point at a specific CLI binary instead of the bundled one -------------
# Not needed normally — the app finds the embedded CLI in
# AiNxt.app/Contents/Resources/bin/ automatically. Only set this to override
# it with a different build.
# export BUDDY_CLI_BIN="$APP/Contents/Resources/bin/<cli-binary>"

# --- Clear Gatekeeper quarantine --------------------------------------------
# Only needed until this build is signed + notarized. Safe to run even if
# the app isn't quarantined.
xattr -dr com.apple.quarantine "$APP" 2>/dev/null

open "$APP"
