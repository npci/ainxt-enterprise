@echo off
REM ============================================================================
REM AiNxt Desktop launcher (portable / no-install)
REM
REM Usage: unzip anywhere, set AINXT_GATEWAY_URL below to your gateway, then
REM double-click this file OR run "ainxt-desktop.bat" from a command prompt.
REM
REM This is the desktop equivalent of the `ainxt` CLI: it sets the gateway env
REM var and launches the app. You still log in through the normal UI screen.
REM ============================================================================

REM --- REQUIRED: the gateway this desktop app talks to ------------------------
REM No default is provided on purpose: the app must point at YOUR gateway.
set "AINXT_GATEWAY_URL="

REM --- Set to 1 to auto-open Developer Tools on launch (for debugging) ---
set "AINXT_DEVTOOLS=0"

REM --- TLS certificate verification ------------------------------------------
REM LEAVE THESE AT 0.
REM
REM Setting either to 1 disables TLS certificate verification, which makes the
REM connection to your gateway trivially interceptable. It exists only for
REM short-lived local testing against a self-signed certificate, and must never
REM be shipped or used against a real deployment.
REM
REM Two DIFFERENT variable names exist and BOTH are read:
REM   AINXT_TLS_INSECURE  -> desktop app (Electron/Chromium + Node https) and
REM                          the ACP CLI.
REM   AINXT_INSECURE_TLS  -> the legacy CLI.
REM Setting only one silently leaves the other side verifying, which produces
REM confusing partial failures (main call works, MCP/connector calls fail).
set "AINXT_TLS_INSECURE=0"
set "AINXT_INSECURE_TLS=0"

REM --- CLI Protocol Tracer ---------------------------------------------------
REM Logs every message between desktop and CLI to %USERPROFILE%\.ainxt\buddy-trace.log
REM Set to 1 only when debugging — leave 0 otherwise. The trace may contain
REM prompt content, so treat the log as sensitive.
set "AINXT_CLI_TRACE=0"

REM --- How to drive the CLI (which protocol) ---------------------------------
REM Selects how the app talks to the CLI binary in resources\bin\:
REM   unset (or "streamjson") -> legacy single-shot --json protocol
REM   "acp"                   -> ACP JSON-RPC with streamable-HTTP MCP
REM Leave unset unless you know your bundled CLI speaks ACP.
REM set "AINXT_CLI_PROTOCOL=acp"
REM
REM To point at a specific CLI binary (e.g. when testing two builds):
REM   set "AINXT_CLI_PROTOCOL=acp"
REM   set "COWORK_CLI_BIN=%~dp0resources\bin\<cli-binary>.exe"

REM Launch the app from this folder (works regardless of current directory).
start "" "%~dp0AiNxt.exe"
