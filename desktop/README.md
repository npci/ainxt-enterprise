# AiNxt Desktop

Electron wrapper for the AiNxt web app — adds native OS integration on top of the existing React SPA.

## Features

- **Global hotkey** `Cmd+Shift+A` (macOS) / `Ctrl+Shift+A` (Win/Linux) — summon or hide the window from anywhere
- **System tray** — lives in the menu bar; right-click for quick actions and API server config
- **Native notifications** — SDLC completions and HITL approvals surface as OS notifications
- **Hide-to-tray** — closing the window hides it (doesn't quit); use Quit from tray menu
- **macOS**: vibrancy sidebar, native traffic-light buttons, DMG + ZIP packaging
- **Windows**: portable ZIP (x64)

## Dev setup

```bash
cd desktop
npm install

# Option 1 — load from running Vite dev server (ai-ui/)
AINXT_DEV=1 npm start

# Option 2 — load from running gateway (production UI)
npm start
```

## Production build

```bash
# macOS (arm64 + x64)
npm run build:mac

# Windows (x64)
npm run build:win

# macOS + Windows
npm run build:all

# Linux AppImage — configured in package.json but has no npm script
npx electron-builder --linux
```

Artifacts appear in `desktop/dist/`. `build:all` covers macOS and Windows only;
Linux needs the explicit command above.

The CLI is bundled into the packaged app via `extraResources`
(`package.json`'s `build.win.extraResources`, filtering for
`ainxt-windows-x64.exe` in `resources/bin/`) — place a real, Windows-built
`ainxt` binary there before packaging. If that folder is empty at build
time, electron-builder skips it silently (a "file source doesn't exist"
warning, not a build failure); the app then falls back to locating `ainxt`
at runtime (see `src/buddy/binary.js`'s resolution order — `BUDDY_CLI_BIN`,
bundled `resources/bin/`, `~/.ainxt/bin/`, then `PATH`) and can offer to
install it if none of those are found.

## Architecture

```
desktop/
  src/
    main.js      — Electron main process: window, tray, global shortcut, IPC handlers
    preload.js   — Context bridge: exposes window.ainxtDesktop API to renderer
  build/
    icon.icns    — macOS app icon (place here before building)
    icon.ico     — Windows app icon
    icon.png     — Linux / tray fallback
    trayTemplate.png — macOS menu bar template image (16×16, white, @2x recommended)
```

## Environment

The desktop app has no hardcoded gateway URL. Set `PLATFORM_BASE_URL` (or
`AINXT_GATEWAY_URL`, which takes priority) before launching, or use tray →
API Server → Custom… to save one via `electron-store` key `apiBase`. With
none of these set, the app shows a "not configured" screen instead of
silently trying `localhost:8000`.

**Point this at your frontend, not an API-only backend.** In a split
deployment (e.g. this repo's `docker-compose.yml`, where `gateway` serves
only the API on one port and `ai-ui` serves the built SPA and proxies
`/ainxt/v1/api` to `gateway` on another), `AINXT_GATEWAY_URL` needs to be
the `ai-ui` port (e.g. `http://localhost:5173`) — the app loads
`${apiBase}/portal/`, and `gateway` alone has no `/portal/` route to serve
it. Pointing this at the backend's own port instead loads a blank/"not
found" screen even though the value looks correct at a glance. This same
value is also what gets passed to the bundled `ainxt` CLI subprocess
(Buddy) as its own `AINXT_GATEWAY_URL`, so one correct value covers both
the main chat window and Buddy.

The React app detects Electron via `window.ainxtDesktop.isDesktop === true`
and can call `window.ainxtDesktop.notify(title, body)` for native notifications.
