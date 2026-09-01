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

The CLI is **not** bundled into the packaged app — `extraResources` is not
configured, so the app locates the `ainxt` binary at runtime (see
`src/cowork/binary.js`) and offers to install it if it is missing.

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

The React app detects Electron via `window.ainxtDesktop.isDesktop === true`
and can call `window.ainxtDesktop.notify(title, body)` for native notifications.
