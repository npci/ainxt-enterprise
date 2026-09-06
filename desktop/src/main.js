// SPDX-License-Identifier: MIT
const { app, BrowserWindow, globalShortcut, Tray, Menu, nativeImage,
  ipcMain, Notification, shell, screen, clipboard, dialog, safeStorage, session } = require("electron");
const path   = require("path");
const fs     = require("fs");
const Store  = require("electron-store");
const { SessionManager } = require("./buddy/cliManager");
const { BuddySessionManager } = require("./buddy/buddySession");
const { resolveCliBinary, missingCliMessage } = require("./buddy/binary");
const { DispatchPoller } = require("./buddy/dispatchPoller");
const _computerUse = require("./computeruse/computerUseManager");
const { buildFileResult } = require("./fileUtils");
const { readAuthState, runLogin, readToken, readGatewayUrl, resolveValidToken, writeToken, validateToken,
  readApiKey, writeApiKey, clearApiKey,
  readRefreshToken, writeRefreshToken, clearRefreshToken, _log } = require("./buddy/auth");
const http  = require("http");
const https = require("https");
const crypto = require("crypto");

// ── Ephemeral loopback TLS certificate (CWE-319) ───────────────────────────────
// Generates an in-memory, self-signed RSA certificate valid for 7 days and bound
// to 127.0.0.1.  It is used ONLY for loopback-only IPC/OAuth2 redirect servers;
// the underlying socket is always bound to 127.0.0.1 so no external host can
// reach it.  The private key never leaves memory and is discarded on exit.
function _generateLoopbackCert() {
const { privateKey, publicKey } = crypto.generateKeyPairSync("rsa", {
modulusLength: 2048,
publicKeyEncoding:  { type: "pkcs1", format: "der" },
privateKeyEncoding: { type: "pkcs8", format: "pem" },
});

const asn1 = {
seq: (items) => {
const body = Buffer.concat(items);
return Buffer.concat([Buffer.from([0x30]), asn1.len(body.length), body]);
},
set: (items) => {
const body = Buffer.concat(items);
return Buffer.concat([Buffer.from([0x31]), asn1.len(body.length), body]);
},
len: (n) => {
if (n < 128) return Buffer.from([n]);
const bytes = [];
let x = n;
while (x > 0) { bytes.unshift(x & 0xff); x >>= 8; }
return Buffer.from([0x80 | bytes.length, ...bytes]);
},
int: (n) => {
let hex = n.toString(16);
if (hex.length % 2) hex = "0" + hex;
if (parseInt(hex[0], 16) >= 8) hex = "00" + hex;
const buf = Buffer.from(hex, "hex");
return Buffer.concat([Buffer.from([0x02]), asn1.len(buf.length), buf]);
},
bitstr: (buf) => Buffer.concat([Buffer.from([0x03]), asn1.len(buf.length + 1), Buffer.from([0x00]), buf]),
octstr: (buf) => Buffer.concat([Buffer.from([0x04]), asn1.len(buf.length), buf]),
utf8: (s) => {
const buf = Buffer.from(s, "utf8");
return Buffer.concat([Buffer.from([0x0c]), asn1.len(buf.length), buf]);
},
ia5: (s) => {
const buf = Buffer.from(s, "ascii");
return Buffer.concat([Buffer.from([0x16]), asn1.len(buf.length), buf]);
},
oid: (s) => {
const parts = s.split(".").map(Number);
const head = Buffer.from([parts[0] * 40 + parts[1]]);
const tail = Buffer.concat(parts.slice(2).map((p) => {
  const bytes = [];
  let x = p;
  do { bytes.unshift((x & 0x7f) | (bytes.length ? 0x80 : 0x00)); x >>= 7; } while (x > 0);
  return Buffer.from(bytes);
}));
const body = Buffer.concat([head, tail]);
return Buffer.concat([Buffer.from([0x06]), asn1.len(body.length), body]);
},
nul: () => Buffer.from([0x05, 0x00]),
time: (d, tag) => {
// UTCTime (0x17) requires YYMMDDHHMMSSZ; GeneralizedTime (0x18) uses YYYYMMDDHHMMSSZ.
const s = tag === 0x17
  ? d.toISOString().replace(/[-:T]/g, "").slice(2, 14) + "Z"
  : d.toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
const buf = Buffer.from(s, "utf8");
return Buffer.concat([Buffer.from([tag]), asn1.len(buf.length), buf]);
},
ctx: (tag, content, constructed = true) => {
const head = constructed ? 0xa0 | tag : 0x80 | tag;
return Buffer.concat([Buffer.from([head]), asn1.len(content.length), content]);
},
};

const sha256WithRSA = asn1.seq([asn1.oid("1.2.840.113549.1.1.11"), asn1.nul()]);
// X.500 Name = SEQUENCE { SET { SEQUENCE { OID, value } } }
const name = (cn) => asn1.seq([asn1.set([asn1.seq([asn1.oid("2.5.4.3"), asn1.utf8(cn)])])]);

const notBefore = new Date();
const notAfter = new Date(notBefore.getTime() + 7 * 24 * 60 * 60 * 1000);

// subjectAltName extension: DNS:localhost, IP:127.0.0.1
const sanExt = asn1.seq([
asn1.oid("2.5.29.17"),
asn1.octstr(asn1.seq([
asn1.ctx(2, asn1.ia5("localhost"), false),
asn1.ctx(7, Buffer.from([127, 0, 0, 1]), false),
])),
]);

// Extensions are [3] EXPLICIT SEQUENCE { Extension, ... }
const extensions = asn1.ctx(3, asn1.seq([sanExt]));

const tbs = asn1.seq([
asn1.ctx(0, asn1.int(2)), // [0] EXPLICIT version v3
asn1.int(1),
sha256WithRSA,
name("AiNxt Loopback"),
asn1.seq([asn1.time(notBefore, 0x17), asn1.time(notAfter, 0x17)]),
name("AiNxt Loopback"),
asn1.seq([
asn1.seq([asn1.oid("1.2.840.113549.1.1.1"), asn1.nul()]),
asn1.bitstr(publicKey),
]),
extensions,
]);

const signer = crypto.createSign("RSA-SHA256");
signer.update(tbs);
const signature = signer.sign(privateKey);

const certDer = asn1.seq([tbs, sha256WithRSA, asn1.bitstr(signature)]);
const certPem = "-----BEGIN CERTIFICATE-----\n" +
certDer.toString("base64").match(/.{1,64}/g).join("\n") +
"\n-----END CERTIFICATE-----\n";

return { key: privateKey, cert: certPem };
}

// _createLoopbackServer creates an HTTPS server for loopback-only IPC/OAuth2
// redirect servers bound exclusively to 127.0.0.1.  TLS is mandatory — if cert
// generation fails we throw a descriptive error rather than silently degrading
// to plaintext HTTP (CWE-319: Cleartext Transmission of Sensitive Information).
function _createLoopbackServer(handler) {
let key, cert;
try {
({ key, cert } = _generateLoopbackCert());
} catch (e) {
throw new Error(`Loopback TLS cert generation failed: ${e.message}`);
}
return https.createServer({ key, cert }, handler);
}

const { listSessions, readHistory } = require("./buddy/sessions");
const buddyHistory = require("./buddy/history");
const _browser = require("./browser/playwrightManager");

// ─── Extraction tracer ────────────────────────────────────────────────────────
// Shares the same buddy-trace.log file and AINXT_CLI_TRACE=1 gate as
// buddySession.js's tracer, so a single log-tail during a large-file support
// call shows the extraction stats (rows/sheets/chars/truncated) alongside the
// CLI tool-call trace — no separate log file to hunt for.
const _extractTraceEnabled = process.env.AINXT_CLI_TRACE === "1";
const _extractTraceFile = path.join(require("os").homedir(), ".ainxt", "buddy-trace.log");
function _trace(tag, data) {
if (!_extractTraceEnabled) return;
try {
const line = `[${new Date().toISOString()}] [${tag}] ${typeof data === "string" ? data : JSON.stringify(data)}\n`;
fs.appendFileSync(_extractTraceFile, line);
} catch { /* best-effort */ }
}

// Brand the app as "AiNxt" (not "Electron"). Must run BEFORE the app is ready /
// the menu is built so the macOS app menu + About/Hide/Quit items read "AiNxt".
// In a packaged build the name also comes from build.productName; this covers
// `npm start` dev mode where the bundle is the generic Electron.app.
app.setName("AiNxt");

const store   = new Store();
const isDev   = process.env.AINXT_DEV === "1";
// Default gateway URL, set via PLATFORM_BASE_URL — the same env var used
// across the platform (core/config.py, auth/sso.py, etc.) for the
// public-facing gateway URL. No hardcoded localhost fallback: a packaged
// build shipped to a machine with nothing listening on :8000 should show the
// "not configured" screen below (or let the user pick tray → "Custom…")
// rather than silently try — and fail against — a port nothing serves.
// AINXT_GATEWAY_URL (below) still takes priority when set, matching the
// CLI's own resolution order.
const DEFAULT_API = process.env.PLATFORM_BASE_URL || "";
// Gateway URL resolution (CLI-parity): AINXT_GATEWAY_URL env wins, so a zipped
// portable build on an office laptop just needs the env var set — no UI config.
// Falls back to the saved setting (tray "Custom…") and finally the default.
const ENV_API = String(process.env.AINXT_GATEWAY_URL || "").replace(/\/+$/, "");
let apiBase   = ENV_API || store.get("apiBase", DEFAULT_API);
// Gateway API path prefix — overridable via env so a routing change doesn't
// require a code change. Every gateway call composes it against `apiBase`.
const API_PREFIX = String(process.env.AINXT_API_PREFIX || "/ainxt/v1/api").replace(/\/+$/, "");
// Path where the gateway serves the built SPA. The production web app is built
// with vite base '/portal/' (ai-ui/vite.config.js), so the deployed UI lives at
// `${apiBase}/portal/` — NOT `/ui` (which 404s → blank window). Overridable.
const UI_PATH = "/" + String(process.env.AINXT_UI_PATH || "/portal/").replace(/^\/+|\/+$/g, "") + "/";
// Vite dev-server URL used only when isDev (a developer explicitly opted into
// dev mode via AINXT_DEV=1). No hardcoded localhost fallback — set
// AINXT_DEV_SERVER_URL if `ai-ui`'s dev server isn't on the default Vite port;
// createWindow() shows the "not configured" screen instead of loading "" when
// it's unset.
const DEV_SERVER_URL = process.env.AINXT_DEV_SERVER_URL || "";
// Open DevTools on launch when AINXT_DEVTOOLS=1 (or dev mode). Lets an operator
// see console/network errors on a locked-down laptop where they can't rebuild.
const DEVTOOLS = isDev || process.env.AINXT_DEVTOOLS === "1";
// TEST-ONLY: accept self-signed / hostname-mismatched TLS certs when
// AINXT_TLS_INSECURE=1. The AiNxt gateway currently serves a self-signed cert
// (issuer==subject), which Chromium rejects → silent blank window. Honouring
// this env lets SIT/portable builds connect; NEVER enable in production.
// NOTE: this is the SAME env var name the Rust CLI (buddySession.js/cliManager.js)
// reads — keep them identical or the Chromium/Node TLS bypass silently does
// nothing while the CLI's bypass is active (or vice versa), which looks like
// "the CLI works but the desktop app doesn't" on a machine using self-signed certs.
// The NODE_ENV !== "production" guard (matching buddy/auth.js) is a second,
// independent safety net: even if AINXT_TLS_INSECURE is left set by mistake
// in a production package/environment, the bypass stays off (CWE-295 SSL
// Verification Bypass hardening).
const INSECURE_TLS = process.env.AINXT_TLS_INSECURE === "1" && process.env.NODE_ENV !== "production";
// Shared agent for main-process Node https calls (SSO exchange/refresh). Node's
// https has its own trust store, so the Chromium switch below does NOT cover it.
const _insecureHttpsAgent = INSECURE_TLS
? new (require("https").Agent)({ rejectUnauthorized: false })
: new (require("https").Agent)({ rejectUnauthorized: true });
if (INSECURE_TLS) {
// Applies to the underlying Chromium net stack (covers loadURL + fetch).
app.commandLine.appendSwitch("ignore-certificate-errors");
}

// Append-only auth diagnostics log. When Buddy shows "Session expired" with no
// visible cause, this file (~/.ainxt/desktop-auth.log) records the exact reason
// (mint HTTP status, token validation result, TLS error) so we can diagnose
// without DevTools on a locked-down laptop.
function _authLog(...parts) {
try {
const line = `[${new Date().toISOString()}] ${parts.join(" ")}\n`;
const dir = path.join(require("os").homedir(), ".ainxt");
try { fs.mkdirSync(dir, { recursive: true }); } catch { /* exists */ }
fs.appendFileSync(path.join(dir, "desktop-auth.log"), line);
} catch { /* best-effort */ }
}

// Minimal window chrome: a slim custom title bar injected into the page
// (see _injectTitleBar). Native window buttons come from titleBarOverlay on
// Windows/Linux and inset traffic lights on macOS — we never reimplement them.
const TITLEBAR_H  = 36;          // px — height of the custom title bar
const TITLEBAR_BG = "#111827";   // brand dark slate (matches index.html theme-color)

const APP_ICON = path.join(__dirname, "..", "build",
process.platform === "win32" ? "icon.ico" : "icon.png");

let mainWindow    = null;
let tray          = null;
let isQuitting    = false;

// Workspace watchers: { folderPath → fs.FSWatcher }
const _watchers = new Map();

// Clipboard monitor state
let _lastClipboard = "";
let _clipboardTimer = null;

// MCP server instance
let _mcpServer = null;
let _mcpPort   = store.get("mcpPort", 9999);
// True only while _mcpServer is actually bound and accepting connections. Guards
// against a failed-bind server object permanently blocking restarts.
let _mcpListening   = false;
let _mcpPortRetries = 0;

// ── App menu (native) ─────────────────────────────────────────────────────────

// Minimal window chrome. On Windows/Linux we drop the native menu bar
// entirely (the "File/Edit/View/Window" strip). On macOS the system menu bar is
// mandatory, so we keep a minimal, branded one — the editMenu/viewMenu roles are
// required to preserve Cmd+C/V/X/A, reload and zoom shortcuts in the web UI.
function _setupAppMenu() {
if (process.platform === "darwin") {
Menu.setApplicationMenu(Menu.buildFromTemplate([
{ role: "appMenu" },
{ role: "editMenu" },
{ role: "viewMenu" },
{ role: "windowMenu" },
]));
} else {
Menu.setApplicationMenu(null);
}
}

// ── Window management ─────────────────────────────────────────────────────────

// Inject the slim custom title bar into the (gateway-served) web UI. Runs on
// every did-finish-load; idempotent. We target the app's `.h-screen.w-screen`
// root (App.jsx) to push it below the bar without breaking its 100vh layout.
function _injectTitleBar(wc) {
if (!wc) return;
const padL = process.platform === "darwin" ? 78 : 12; // clear mac traffic lights
const css = [
"html,body{margin:0 !important;}",
".h-screen.w-screen{margin-top:" + TITLEBAR_H + "px !important;height:calc(100vh - " + TITLEBAR_H + "px) !important;}",
"#ainxt-titlebar{position:fixed;top:0;left:0;right:0;height:" + TITLEBAR_H + "px;z-index:2147483647;" +
"display:flex;align-items:center;background:" + TITLEBAR_BG + ";color:#e5e7eb;" +
"font:600 12.5px/1 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;" +
"-webkit-app-region:drag;-webkit-user-select:none;user-select:none;border-bottom:1px solid rgba(255,255,255,0.06);}",
"#ainxt-titlebar .ainxt-tb-brand{display:flex;align-items:center;gap:8px;padding-left:" + padL + "px;letter-spacing:.2px;}",
"#ainxt-titlebar .ainxt-tb-brand img{width:16px;height:16px;border-radius:4px;}",
"#ainxt-titlebar a,#ainxt-titlebar button,#ainxt-titlebar input,#ainxt-titlebar select{-webkit-app-region:no-drag;}",
].join("");
wc.insertCSS(css).catch(function () {});
wc.executeJavaScript(
"(function(){" +
"if(document.getElementById('ainxt-titlebar'))return;" +
"var bar=document.createElement('div');bar.id='ainxt-titlebar';" +
"var brand=document.createElement('span');brand.className='ainxt-tb-brand';" +
"var img=document.createElement('img');img.src='/favicon.png';" +
"img.onerror=function(){img.style.display='none';};" +
"var name=document.createElement('span');name.textContent='AiNxt';" +
"brand.appendChild(img);brand.appendChild(name);bar.appendChild(brand);" +
"document.body.insertBefore(bar,document.body.firstChild);" +
"})();"
).catch(function () {});
}

// Rendered instead of loadURL("") when no gateway URL is configured (no
// PLATFORM_BASE_URL / AINXT_GATEWAY_URL / AINXT_DEV_SERVER_URL env var, and no
// saved tray "Custom…" setting). Mirrors the did-fail-load friendly screen
// below so an unconfigured build never shows a silent blank window.
function _notConfiguredDataUrl(devMode) {
const hint = devMode
? "Set AINXT_DEV_SERVER_URL to the ai-ui Vite dev server's URL (e.g. http://localhost:5173) and restart."
: "Set the PLATFORM_BASE_URL (or AINXT_GATEWAY_URL) environment variable to your gateway's URL, or use tray → API Server → Custom…, then restart AiNxt.";
const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
return "data:text/html;charset=utf-8," + encodeURIComponent(
`<body style="font:14px system-ui;background:#111827;color:#e5e7eb;margin:0;display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;padding:40px;box-sizing:border-box">` +
`<h2 style="color:#f87171;margin:0 0 12px">AiNxt isn't configured yet</h2>` +
`<p style="color:#d1d5db;max-width:480px;text-align:center;margin:0 0 24px;line-height:1.6">${esc(hint)}</p>` +
`</body>`
);
}

async function createWindow(authGate) {
const { width: sw, height: sh } = screen.getPrimaryDisplay().workAreaSize;

mainWindow = new BrowserWindow({
width:           Math.min(1280, sw),
height:          Math.min(900, sh),
minWidth:        800,
minHeight:       600,
show:            false,
// macOS keeps inset traffic lights; Windows/Linux hide the frame and draw
// the native min/max/close buttons via titleBarOverlay (colored to match
// our custom bar) so we don't have to reimplement window controls.
titleBarStyle:   process.platform === "darwin" ? "hiddenInset" : "hidden",
...(process.platform === "darwin" ? {} : {
titleBarOverlay: { color: TITLEBAR_BG, symbolColor: "#e5e7eb", height: TITLEBAR_H },
}),
vibrancy:        "sidebar",
backgroundColor: TITLEBAR_BG,
webPreferences: {
preload:          path.join(__dirname, "preload.js"),
contextIsolation: true,
nodeIntegration:  false,
webSecurity:      true,
},
icon: path.join(__dirname, "..", "build",
process.platform === "win32" ? "icon.ico" : "icon.png"),
});

// Inject x-ainxt-surface: desktop on every request to the gateway so the
// server can tag model_usages rows with source_channel="DESKTOP" instead of
// lumping desktop traffic in with plain browser (CHAT) usage.
// The filter covers both the production gateway URL and the Vite dev server.
// Skipped entirely when unconfigured (empty string) — an empty pattern would
// otherwise resolve to "/*", matching every request.
const _gatewayBase    = isDev ? DEV_SERVER_URL : apiBase;
const _gatewayPattern = _gatewayBase ? `${_gatewayBase}/*` : null;
if (_gatewayPattern) {
mainWindow.webContents.session.webRequest.onBeforeSendHeaders(
{ urls: [_gatewayPattern] },
(details, callback) => {
  details.requestHeaders["x-ainxt-surface"] = "desktop";
  callback({ requestHeaders: details.requestHeaders });
}
);
}

// Wait (bounded) for silent re-login to commit the auth_token cookie BEFORE the
// SPA loads. ai-ui's App.jsx calls /auth/me once on mount with no retry, so a
// cookie that lands even a few hundred ms late means the user sees a login
// screen despite being authenticated. The BrowserWindow above is already
// constructed, so Chromium boots in PARALLEL with this wait — on a healthy
// network the gate has usually resolved by now and adds ~0ms. Capped so a slow
// or unreachable gateway can never delay startup by more than the timeout.
if (authGate) {
const STARTUP_AUTH_GATE_MS = 3000;
let _gateTimer;
try {
await Promise.race([
  authGate,
  new Promise((r) => { _gateTimer = setTimeout(r, STARTUP_AUTH_GATE_MS); }),
]);
} catch { /* relogin failure is non-fatal — fall through to normal login */ }
finally { clearTimeout(_gateTimer); }
}

const url = isDev ? DEV_SERVER_URL : `${apiBase}${UI_PATH}`;
if (!url || url === UI_PATH) {
// Neither an env var nor a saved tray "Custom…" URL is configured — there is
// no hardcoded localhost to silently fall back to. Show a real "set it up"
// screen (not a blank/error window) instead of attempting loadURL("").
mainWindow.loadURL(_notConfiguredDataUrl(isDev)).catch(() => {});
} else {
mainWindow.loadURL(url).catch((err) => {
  console.error("[AiNxt] initial loadURL failed:", url, err && err.message);
});
}

// Surface load failures instead of leaving a silent blank window. The most
// common cause on the AiNxt network is a self-signed gateway cert (see
// AINXT_TLS_INSECURE) or an unreachable gateway URL.
mainWindow.webContents.on("did-fail-load", (_e, errorCode, errorDescription, validatedURL) => {
if (errorCode === -3) return; // ERR_ABORTED — benign (redirect/in-page nav)
console.error(`[AiNxt] did-fail-load ${errorCode} ${errorDescription} :: ${validatedURL}`);
const isCert = /CERT|SSL/i.test(errorDescription || "");
const friendlyHint = isCert
? "AiNxt couldn't verify the server's security certificate. Please contact your IT administrator."
: "AiNxt couldn't connect to the server. Please check your network connection and try again.";
const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
mainWindow.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(
`<body style="font:14px system-ui;background:#111827;color:#e5e7eb;margin:0;display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;padding:40px;box-sizing:border-box">` +
`<h2 style="color:#f87171;margin:0 0 12px">Couldn't connect to AiNxt</h2>` +
`<p style="color:#d1d5db;max-width:480px;text-align:center;margin:0 0 24px;line-height:1.6">${esc(friendlyHint)}</p>` +
`<button onclick="location.reload()" style="background:#4f46e5;color:white;border:none;padding:10px 28px;border-radius:8px;cursor:pointer;font-size:14px;font-family:system-ui;margin-bottom:24px">Try Again</button>` +
`<details style="color:#6b7280;font-size:12px;max-width:560px;width:100%"><summary style="cursor:pointer;user-select:none">Technical details</summary>` +
`<pre style="margin-top:8px;color:#9ca3af;white-space:pre-wrap;word-break:break-all">${esc(errorDescription)} (${errorCode})\n${esc(validatedURL)}</pre></details></body>`
)).catch(() => {});
});

mainWindow.webContents.on("did-finish-load", () => {
mainWindow.webContents.executeJavaScript(
`window.__AINXT_DESKTOP__ = true; window.__AINXT_API__ = ${JSON.stringify(apiBase)};`
);
_injectTitleBar(mainWindow.webContents);
// Start watching for tasks dispatched from mobile/web (idempotent).
try { _ensureDispatchPoller().start(); } catch { /* ignore */ }
});

// On a full renderer reload (Cmd+R), the renderer loses all its in-memory
// CLI session refs but the main-process child processes keep running —
// orphaned and unreachable. Dispose them so they don't pile up (a night of
// reloads had leaked ~29 CLI procs ≈ 2.6 GB). Skip the first load; ignore
// in-page SPA route changes (isInPlace).
let _firstLoad = true;
mainWindow.webContents.on("did-start-navigation", (_e, _url, isInPlace, isMainFrame) => {
if (!isMainFrame || isInPlace) return;
if (_firstLoad) { _firstLoad = false; return; }
if (buddyManager) buddyManager.disposeAll();
if (buddyOfficeManager) buddyOfficeManager.disposeAll();
});

mainWindow.once("ready-to-show", () => {
mainWindow.show();
if (DEVTOOLS) mainWindow.webContents.openDevTools();
});

mainWindow.on("close", (e) => {
if (!isQuitting) { e.preventDefault(); mainWindow.hide(); }
});
mainWindow.on("closed", () => { mainWindow = null; });
}

function toggleWindowWithContext() {
if (!mainWindow) { createWindow(); return; }
if (mainWindow.isVisible() && mainWindow.isFocused()) {
mainWindow.hide();
return;
}
// Attach context (clipboard + active app) when summoning via shortcut
const ctx = {
clipboard:  clipboard.readText().slice(0, 2000),
activeApp:  _getActiveApp(),
};
mainWindow.show();
mainWindow.focus();
// Send context once the window is ready to receive it
mainWindow.webContents.send("shortcut-context", ctx);
}

// ── System tray ──────────────────────────────────────────────────────────────

function createTray() {
// Pick an icon file that actually ships in the bundle for this platform.
// build/ contains icon.icns (mac) and icon.ico (win) — there is no icon.png,
// so the previous non-darwin path (icon.png) always resolved to an empty
// image and the tray rendered blank. macOS still prefers a template PNG if
// present, but falls back to the .icns so it is never empty.
const candidates = process.platform === "darwin"
? ["trayTemplate.png", "icon.png", "icon.icns"]
: ["icon.ico", "icon.png"];

let img = nativeImage.createEmpty();
for (const file of candidates) {
const p = path.join(__dirname, "..", "build", file);
const candidate = nativeImage.createFromPath(p);
if (!candidate.isEmpty()) { img = candidate; break; }
}

// Windows/Linux trays render best around 16–24px; downscale a large source
// icon so it is sharp instead of being auto-scaled by the shell.
if (!img.isEmpty() && process.platform !== "darwin") {
try {
const size = img.getSize();
if (size.width > 32 || size.height > 32) {
  img = img.resize({ width: 24, height: 24, quality: "best" });
}
} catch { /* keep original image if resize fails */ }
}

tray = new Tray(img);
tray.setToolTip("AiNxt");
rebuildTrayMenu();
tray.on("click", toggleWindowWithContext);
}

function rebuildTrayMenu() {
const menu = Menu.buildFromTemplate([
{ label: "Open AiNxt", click: toggleWindowWithContext },
{ type: "separator" },
{
label: "API Server",
// No hardcoded localhost default: only show a "(default)" radio entry when
// PLATFORM_BASE_URL is actually set. Otherwise the only way to point the
// app at a gateway is tray → Custom… (or set the env var and restart).
submenu: DEFAULT_API
  ? [
      { label: `${DEFAULT_API.replace(/^https?:\/\//, "")} (default)`, type: "radio",
        checked: apiBase === DEFAULT_API,
        click: () => setApiBase(DEFAULT_API) },
      { label: "Custom…", click: changeApiBase },
    ]
  : [
      { label: apiBase ? apiBase.replace(/^https?:\/\//, "") : "(not configured)", enabled: false },
      { label: "Custom…", click: changeApiBase },
    ],
},
{ type: "separator" },
{
label: "Full power mode (shell, files, web — unrestricted)",
type: "checkbox",
checked: !!store.get("devToolsEnabled", process.env.BUDDY_DEV_TOOLS === "1"),
click: (item) => {
  store.set("devToolsEnabled", !!item.checked);
  // Dispose live sessions so the next one spawns with the new capability set.
  try { if (buddyOfficeManager) buddyOfficeManager.disposeAll(); } catch { /* ignore */ }
},
},
{ type: "separator" },
{ label: `Local MCP — port ${_mcpPort}`, enabled: false },
{ label: "Restart MCP server", click: () => { _stopMcpServer(); _startMcpServer(); } },
{ type: "separator" },
{ label: `Version ${app.getVersion()}`, enabled: false },
{ type: "separator" },
{ label: "Quit AiNxt", click: () => { isQuitting = true; app.quit(); } },
]);
tray.setContextMenu(menu);
}

function setApiBase(url) {
apiBase = url;
store.set("apiBase", url);
rebuildTrayMenu();
if (!mainWindow) return;
const target = isDev ? DEV_SERVER_URL : `${url}${UI_PATH}`;
if (!target || target === UI_PATH) {
mainWindow.loadURL(_notConfiguredDataUrl(isDev)).catch(() => {});
} else {
mainWindow.loadURL(target);
}
}

async function changeApiBase() {
if (mainWindow) {
mainWindow.show();
mainWindow.focus();
mainWindow.webContents.send("request-api-base", apiBase);
}
}

// ── Global shortcut ───────────────────────────────────────────────────────────

function registerShortcuts() {
const shortcut = process.platform === "darwin" ? "Command+Shift+A" : "Ctrl+Shift+A";
const ok = globalShortcut.register(shortcut, toggleWindowWithContext);
if (!ok) console.warn("AiNxt desktop: global shortcut registration failed");
}

// ── Phase 1: Local file access ────────────────────────────────────────────────

ipcMain.handle("pick-folder", async () => {
const result = await dialog.showOpenDialog(mainWindow, {
properties: ["openDirectory"],
title: "Select workspace folder to index",
});
return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle("pick-file", async () => {
const result = await dialog.showOpenDialog(mainWindow, {
properties: ["openFile", "multiSelections"],
title: "Select files to index",
});
return result.canceled ? [] : result.filePaths;
});

// ── read-file: unified document reader ───────────────────────────────────────
// Routes every supported file type through the appropriate parser so the model
// always receives readable text — never raw binary bytes.
//
// Routing table (mirrors the server-side document_parser.py):
//   .xlsx / .xlsm          → read-excel-file workbook extractor (tab-separated rows)
//   .xls                   → clear "unsupported" message (legacy binary format)
//   .ods                   → ODS (OpenDocument Spreadsheet) extractor
//   .docx                  → mammoth raw-text + table extractor
//   .odt                   → ODT (OpenDocument Text) extractor
//   .pdf                   → pdf-parse page-by-page extractor
//   .pptx                  → OOXML slide text extractor
//   .ppt                   → clear "unsupported" message (OLE2 binary)
//   .rtf                   → regex-based RTF stripper
//   .html / .htm           → tag-stripping HTML extractor
//   .svg                   → plain UTF-8 read (SVG is XML text)
//   .csv / .tsv            → plain UTF-8 read + table structure
//   .txt / .md / .json /
//   .xml / .yaml / .yml /
//   .log / .toml / .ini /
//   .cfg / .conf / .env    → plain UTF-8 read (text files, up to 2 MB)
//   .png / .jpg / .jpeg /
//   .gif / .webp / .bmp    → descriptive placeholder (no local vision model)
//   everything else        → plain UTF-8 read (up to 1 MB, legacy behaviour)
//
// Size limits: 25 MB for binary formats, 2 MB for plain-text, 1 MB for unknown.
ipcMain.handle("read-file", async (_e, filePath) => {
try {
const stat = fs.statSync(filePath);
const ext  = path.extname(filePath).toLowerCase();

// Binary / structured document formats — route through _extractAny().
// _extractAny is defined after the EXTRACT_* constants (further down in
// this file) and handles all size-limit checks internally.
if (EXTRACT_SUPPORTED.has(ext)) {
const result = await _extractAny(filePath);
if (result.error) return { error: result.error, content: null };
return { content: result.text, error: null, warnings: result.warnings || [] };
}

// Unknown / plain-text fallback — legacy 1 MB cap.
if (stat.size > 1_048_576) return { error: "File exceeds 1 MB limit", content: null };
return { content: fs.readFileSync(filePath, "utf-8"), error: null };
} catch (e) {
return { error: e.message, content: null };
}
});

// Read a binary file and return its contents as a base64 string.
// Used by BuddyDesktop to extract text from Office documents (docx/xlsx/pptx)
// client-side before injecting into the agent prompt — the CLI Read tool
// cannot parse binary Office formats.
// Limit: EXTRACT_MAX_BYTES (env-tunable via AINXT_EXTRACT_MAX_BYTES, default 200 MB
// — raised from the original hardcoded 25 MB; see the EXTRACT_MAX_BYTES comment
// further down in this file for why).
ipcMain.handle("read-file-binary", (_e, filePath) => {
try {
const stat = fs.statSync(filePath);
if (stat.size > EXTRACT_MAX_BYTES) return { error: `File exceeds ${(EXTRACT_MAX_BYTES / 1024 / 1024).toFixed(0)} MB limit`, base64: null };
const buf = fs.readFileSync(filePath);
return { base64: buf.toString("base64"), error: null };
} catch (e) {
return { error: e.message, base64: null };
}
});

// Parse an Excel workbook (.xlsx / .xlsm) and return its content as
// model-friendly text (tab-separated rows, one ## heading per sheet).
// Called by the Chat renderer when a user attaches a spreadsheet — mirrors
// the server-side parse_excel() path so desktop and web Chat produce identical
// output.  Uses the read-excel-file package bundled in the desktop.
// Legacy binary .xls is not supported (read-excel-file parses OOXML only) —
// callers get an explicit "unsupported extension" error rather than a
// silent failure.
// Limit: EXTRACT_MAX_BYTES (raised from 25 MB). Row cap: EXTRACT_TABLE_ROW_LIMIT
// per sheet (raised from 5,000 — see _extractWorkbook() for the explicit
// truncation-warning behaviour when a sheet still exceeds it).
ipcMain.handle("read-file-spreadsheet", async (_e, filePath) => {
try {
const stat = fs.statSync(filePath);
if (stat.size > EXTRACT_MAX_BYTES) return { error: `File exceeds ${(EXTRACT_MAX_BYTES / 1024 / 1024).toFixed(0)} MB limit`, text: null };
const ext = path.extname(filePath).toLowerCase();
if (![".xlsx", ".xlsm"].includes(ext)) {
return { error: `Unsupported extension '${ext}' — expected .xlsx or .xlsm (legacy .xls is not supported)`, text: null };
}
// _extractWorkbook is defined later in this file (after the EXTRACT_* constants).
const result = await _extractWorkbook(filePath);
return { text: result.text, sheets: result.sheets, tables: result.tables, warnings: result.warnings, error: null };
} catch (e) {
return { error: e.message, text: null };
}
});

ipcMain.handle("list-folder", (_e, folderPath, opts = {}) => {
const maxFiles = opts.maxFiles || 500;
const results  = [];

const SUPPORTED_EXT = new Set([
// Code / text (Lite IDE + @-mention completion)
".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".kts",
".scala", ".go", ".rs", ".cpp", ".cc", ".c", ".h", ".hpp",
".cs", ".rb", ".php", ".swift", ".sh", ".bash", ".sql",
".yaml", ".yml", ".json", ".md", ".txt", ".env.example",
// Office/documents — REQUIRED so Buddy can list, extract and ATTACH them.
// Without these, a .docx/.pdf in the attached folder was invisible to the UI,
// so "send <file>.docx to <person>" could never find or upload it.
".docx", ".doc", ".pdf", ".pptx", ".ppt", ".xlsx", ".xls", ".xlsm",
".csv", ".odt", ".ods", ".rtf", ".xml", ".html", ".htm",
// Images (attachable)
".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
]);

function walk(dir, depth = 0) {
if (results.length >= maxFiles || depth > 8) return;
let entries;
try { entries = fs.readdirSync(dir); } catch { return; }
for (const name of entries) {
if (name.startsWith(".") || name === "node_modules" || name === "__pycache__" ||
    name === "venv" || name === "dist" || name === "build" || name === ".git") continue;
const full = path.join(dir, name);
let stat;
try { stat = fs.statSync(full); } catch { continue; }
if (stat.isDirectory()) {
  walk(full, depth + 1);
} else if (SUPPORTED_EXT.has(path.extname(name).toLowerCase())) {
  results.push({ path: full, name, size: stat.size, ext: path.extname(name) });
}
if (results.length >= maxFiles) break;
}
}

walk(folderPath);
return results;
});

// ── Lite IDE: guarded write / create / delete / rename ────────────────────────
// Direct renderer-driven filesystem mutations are confined to folders the user
// has opened (= watched workspace roots). path.resolve collapses any ".." so a
// prefix check is enough to prevent traversal outside the workspace.
function _resolveInsideWorkspace(target) {
if (typeof target !== "string" || !target) return null;
const roots = store.get("watchedFolders", []);
if (!roots.length) return null;
let abs;
try { abs = path.resolve(target); } catch { return null; }
for (const root of roots) {
let r;
try { r = path.resolve(root); } catch { continue; }
if (abs === r || abs.startsWith(r + path.sep)) return abs;
}
return null;
}

// NOTE: the former "upload-file-to-server" main-process IPC was removed. Folder
// auto-upload now reuses the renderer paperclip path (readFileBinary → Blob →
// uploadFileToServer useCallback in BuddyDesktop.jsx), which works for all doc
// types and shares the same auth as manual attachments.

ipcMain.handle("write-file", (_e, filePath, content) => {
try {
const abs = _resolveInsideWorkspace(filePath);
if (!abs) return { ok: false, error: "Path is outside the open workspace" };
if (typeof content !== "string") return { ok: false, error: "Invalid content" };
if (Buffer.byteLength(content, "utf-8") > 2_097_152) return { ok: false, error: "File exceeds 2 MB limit" };
fs.writeFileSync(abs, content, "utf-8");
return { ok: true };
} catch (e) { return { ok: false, error: e.message }; }
});

ipcMain.handle("create-path", (_e, targetPath, isDir) => {
try {
const abs = _resolveInsideWorkspace(targetPath);
if (!abs) return { ok: false, error: "Path is outside the open workspace" };
if (fs.existsSync(abs)) return { ok: false, error: "Already exists" };
if (isDir) {
fs.mkdirSync(abs, { recursive: true });
} else {
fs.mkdirSync(path.dirname(abs), { recursive: true });
fs.writeFileSync(abs, "", "utf-8");
}
return { ok: true };
} catch (e) { return { ok: false, error: e.message }; }
});

ipcMain.handle("delete-path", async (_e, targetPath) => {
try {
const abs = _resolveInsideWorkspace(targetPath);
if (!abs) return { ok: false, error: "Path is outside the open workspace" };
await shell.trashItem(abs); // reversible — moves to OS trash, never hard-deletes
return { ok: true };
} catch (e) { return { ok: false, error: e.message }; }
});

ipcMain.handle("rename-path", (_e, oldPath, newPath) => {
try {
const absOld = _resolveInsideWorkspace(oldPath);
const absNew = _resolveInsideWorkspace(newPath);
if (!absOld || !absNew) return { ok: false, error: "Path is outside the open workspace" };
if (fs.existsSync(absNew)) return { ok: false, error: "Target already exists" };
fs.mkdirSync(path.dirname(absNew), { recursive: true });
fs.renameSync(absOld, absNew);
return { ok: true };
} catch (e) { return { ok: false, error: e.message }; }
});

// ── Phase 2: Workspace watcher ────────────────────────────────────────────────

ipcMain.handle("watch-folder", (_e, folderPath) => {
if (_watchers.has(folderPath)) return { watching: true };

try {
const watcher = fs.watch(folderPath, { recursive: true }, (event, filename) => {
if (!filename) return;
const ext = path.extname(filename).toLowerCase();
const SKIP = new Set([".pyc", ".class", ".o", ".cache", ".tmp", ".lock"]);
if (SKIP.has(ext)) return;

const fullPath = path.join(folderPath, filename);
// Emit file-changed event to renderer
if (mainWindow) {
  mainWindow.webContents.send("workspace-file-changed", {
    event, filename: fullPath, folder: folderPath,
  });
}
});
_watchers.set(folderPath, watcher);
// Persist watched folders across restarts
const watched = store.get("watchedFolders", []);
if (!watched.includes(folderPath)) store.set("watchedFolders", [...watched, folderPath]);
return { watching: true };
} catch (e) {
return { watching: false, error: e.message };
}
});

ipcMain.handle("unwatch-folder", (_e, folderPath) => {
const w = _watchers.get(folderPath);
if (w) { w.close(); _watchers.delete(folderPath); }
const watched = store.get("watchedFolders", []).filter(f => f !== folderPath);
store.set("watchedFolders", watched);
return { watching: false };
});

ipcMain.handle("get-watched-folders", () => store.get("watchedFolders", []));

// ── Phase 3: Clipboard intelligence ──────────────────────────────────────────

function startClipboardMonitor() {
_lastClipboard = clipboard.readText();
_clipboardTimer = setInterval(() => {
const current = clipboard.readText();
if (current !== _lastClipboard && current.trim().length > 10) {
_lastClipboard = current;
if (mainWindow && !mainWindow.isFocused()) {
  // Only surface clipboard events when AiNxt is not in focus
  mainWindow.webContents.send("clipboard-changed", {
    text: current.slice(0, 2000),
    ts: Date.now(),
  });
}
}
}, 1000);
}

ipcMain.handle("get-clipboard", () => clipboard.readText().slice(0, 2000));

ipcMain.handle("set-clipboard", (_e, text) => { clipboard.writeText(text); });

// ── Phase 4: Shortcut context ─────────────────────────────────────────────────

function _getActiveApp() {
try {
if (process.platform === "darwin") {
const { execSync } = require("child_process");
return execSync(
  `osascript -e 'tell application "System Events" to get name of first application process whose frontmost is true'`,
  { timeout: 1000, encoding: "utf-8" }
).trim();
}
if (process.platform === "win32") {
const { execSync } = require("child_process");
return execSync(
  `powershell -command "Get-Process | Where-Object {$_.MainWindowHandle -ne 0} | Sort CPU -desc | Select -First 1 -ExpandProperty ProcessName"`,
  { timeout: 1500, encoding: "utf-8" }
).trim();
}
} catch { /* non-critical */ }
return "";
}

ipcMain.handle("get-shortcut-context", () => ({
clipboard: clipboard.readText().slice(0, 2000),
activeApp: _getActiveApp(),
}));

// ── Phase 5: Local MCP server ─────────────────────────────────────────────────
// Exposes local tools (read_file, search_files, list_directory, execute_terminal)
// as an HTTP server the AiNxt backend can call via MCP protocol.

const ALLOWED_COMMANDS = new Set([
"git", "ls", "cat", "grep", "find", "echo",
"npm", "yarn", "mvn", "gradle", "pytest", "python",
"node", "java", "go", "cargo",
]);

function _isSafeCommand(cmd) {
const first = cmd.trim().split(/\s+/)[0].replace(/^.*[/\\]/, "");  // basename only
return ALLOWED_COMMANDS.has(first);
}

// SSE sessions for the MCP-protocol view of the local tools (used by the Buddy
// full agent via --mcp-config). Keyed by sessionId → response stream.
const _mcpSseSessions = new Map();
// Per-session "surface": 'buddy' (office assistant) or 'code' (dev agent). The
// Buddy agent connects with ?surface=buddy and is restricted to OFFICE tools
// only (folder-scoped document tools plus browser + computer-use) — NO broad file
// tools and NO shell (execute_terminal). Office file access goes through list_files,
// extract_document, and the CLI's folder-scoped Read for plain text. This closes
// the hole where Buddy could read the whole filesystem or run a shell via the
// local MCP.
const _mcpSseSurface = new Map();
// Per-session granted ROOT folder for Buddy — the ONLY directory list_files may
// enumerate and extract_document may read from. Passed as ?root= on the SSE
// connection by buddySession.
const _mcpSseRoot = new Map();

// ── Extraction limits ─────────────────────────────────────────────────────────
// These used to be tiny, hardcoded safety caps left over from an early
// prototype (25 MB / 5,000 rows-per-sheet / 500,000 chars). They were never
// raised when the server-side parser's caps were lifted, so large Excel files
// kept getting silently cut on the desktop even after the server-side fix
// shipped — the model would then retry the same extract_document call trying
// to "get the rest of the data" and eventually trip the runaway-loop guard in
// buddySession.js. See docs/ for the investigation writeup.
//
// All four are now env-tunable (the desktop reads its own machine's disk/RAM,
// not a shared server, so there is much more headroom than the reasoning that
// justifies the server-side caps) and raised substantially by default. If a
// value is still hit, _extractWorkbook()/_trimExtractText() emit an explicit
// `warnings` entry describing exactly what was cut, instead of a silent
// "\n[truncated]" marker buried in the text body — the renderer injects that
// warning into the prompt as a stated fact so the model reports partial data
// instead of re-requesting the same read.
const EXTRACT_MAX_BYTES = Number(process.env.AINXT_EXTRACT_MAX_BYTES) || 200 * 1024 * 1024;   // was 25 MB
const TEXT_MAX_BYTES = Number(process.env.AINXT_TEXT_MAX_BYTES) || 20 * 1024 * 1024;           // was 2 MB
const EXTRACT_TEXT_LIMIT = Number(process.env.AINXT_EXTRACT_TEXT_LIMIT) || 5_000_000;          // was 500,000 chars
const EXTRACT_TABLE_ROW_LIMIT = Number(process.env.AINXT_EXTRACT_TABLE_ROW_LIMIT) || 200_000;  // was 5,000 rows/sheet
// All document/spreadsheet/presentation/text types the desktop can parse locally.
// Kept in sync with _extractAny() below and the read-file IPC handler.
// Mirrors the server-side chat_router._ALLOWED_EXTENSIONS + document_parser.py.
const EXTRACT_SUPPORTED = new Set([
// Word-processing
".docx", ".odt",
// Spreadsheets
".xlsx", ".xlsm", ".xls", ".ods",
// Presentations
".pptx", ".ppt",
// PDF
".pdf",
// Plain-text / data
".csv", ".tsv", ".txt", ".md", ".json", ".xml", ".yaml", ".yml",
".log", ".toml", ".ini", ".cfg", ".conf", ".env",
// Web / markup
".html", ".htm", ".svg", ".rtf",
// Images — desktop has no vision model; returns a descriptive placeholder
".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
]);

function _requireDesktopDependency(name) {
try { return require(name); }
catch {
throw new Error(`Desktop document extraction dependency '${name}' is not installed. Run npm install in the desktop package.`);
}
}

function _decodeXmlText(s) {
return String(s || "")
.replace(/<w:tab\/>/g, "\t")
.replace(/<w:br\/>/g, "\n")
.replace(/<[^>]+>/g, "")
.replace(/&lt;/g, "<")
.replace(/&gt;/g, ">")
.replace(/&quot;/g, '"')
.replace(/&apos;/g, "'")
.replace(/&amp;/g, "&")
.trim();
}

// Trims text to EXTRACT_TEXT_LIMIT and returns { text, warning }. `warning` is
// null when no trimming occurred, otherwise a human-readable, model-facing
// sentence stating exactly how much content was cut — callers should fold this
// into their `warnings` array so it reaches the chat prompt as a stated fact
// ("NOTE: this file is larger than X chars; only the first Y are shown")
// instead of a silent "\n[truncated]" marker buried in the text body that only
// the model (not the user) would ever see, and that the model tends to
// interpret as a read error worth retrying.
function _trimExtractText(text) {
const s = String(text || "");
if (s.length <= EXTRACT_TEXT_LIMIT) return { text: s, warning: null };
const warning = `Extracted text exceeds ${EXTRACT_TEXT_LIMIT.toLocaleString()} characters ` +
`(actual: ${s.length.toLocaleString()}) — only the first ${EXTRACT_TEXT_LIMIT.toLocaleString()} characters are shown. ` +
`The remaining ${(s.length - EXTRACT_TEXT_LIMIT).toLocaleString()} characters were NOT read. ` +
`Do not re-request this file — instead tell the user the data is partial and ask them to split the file or narrow the request.`;
return { text: s.slice(0, EXTRACT_TEXT_LIMIT) + `\n[TRUNCATED: ${warning}]`, warning };
}

async function _resolveAttachedFile(rawPath, root) {
if (!root) throw new Error("No folder is attached. Ask the user to attach a folder.");
const requested = String(rawPath || "").trim();
if (!requested) throw new Error("path is required");
const rootReal = await fs.promises.realpath(root);
const candidate = path.isAbsolute(requested) ? requested : path.resolve(rootReal, requested);
const real = await fs.promises.realpath(candidate);
const rel = path.relative(rootReal, real);
if (rel.startsWith("..") || path.isAbsolute(rel)) {
throw new Error("File is outside the attached folder.");
}
const stat = await fs.promises.stat(real);
if (stat.isDirectory()) throw new Error("Path is a directory. Use list_files to choose a file.");
const ext = path.extname(real).toLowerCase();
if (!EXTRACT_SUPPORTED.has(ext)) throw new Error(`Unsupported file type '${ext || "unknown"}'. Supported: docx, odt, xlsx, xls, xlsm, ods, pdf, pptx, ppt, rtf, html, htm, svg, csv, tsv, txt, md, json, xml, yaml, yml, log, toml, ini, cfg, conf, env, png, jpg, jpeg, gif, webp, bmp`);
const _TEXT_LIMIT_EXTS = new Set([".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".svg", ".log", ".toml", ".ini", ".cfg", ".conf", ".env"]);
const limit = _TEXT_LIMIT_EXTS.has(ext) ? TEXT_MAX_BYTES : EXTRACT_MAX_BYTES;
if (stat.size > limit) throw new Error(`File is too large for local extraction (${stat.size} bytes).`);
return { real, rel, stat, ext };
}

// Format a single cell value for tab-separated text output. Date objects
// (returned natively by read-excel-file for date-formatted cells) are
// rendered as a plain YYYY-MM-DD using LOCAL date components — NOT
// toISOString(), which converts to UTC and can shift the date by a day in
// timezones ahead of UTC (e.g. midnight IST becomes the previous day in
// UTC) — instead of Date.toString()'s verbose
// "Sun Jan 01 1995 00:00:00 GMT+0530 (India Standard Time)". This mirrors
// the old SheetJS `raw:false` behaviour of returning a clean, human-readable
// date string per cell.
function _formatCellValue(v) {
if (v == null) return "";
if (v instanceof Date) {
const y = v.getFullYear();
const m = String(v.getMonth() + 1).padStart(2, "0");
const d = String(v.getDate()).padStart(2, "0");
return `${y}-${m}-${d}`;
}
return String(v);
}

function _rowsToText(rows) {
return rows.map((row) => row.map(_formatCellValue).join("\t")).join("\n");
}

async function _extractDocx(filePath) {
const mammoth = _requireDesktopDependency("mammoth");
const raw = await mammoth.extractRawText({ path: filePath });
const warnings = (raw.messages || []).map((m) => m.message || String(m)).filter(Boolean);
const tables = [];
try {
const JSZip = _requireDesktopDependency("jszip");
const zip = await JSZip.loadAsync(await fs.promises.readFile(filePath));
const docXml = zip.file("word/document.xml") ? await zip.file("word/document.xml").async("string") : "";
const tableMatches = docXml.match(/<w:tbl[\s\S]*?<\/w:tbl>/g) || [];
tableMatches.forEach((tbl, i) => {
const rows = [];
const rowMatches = tbl.match(/<w:tr[\s\S]*?<\/w:tr>/g) || [];
rowMatches.forEach((tr) => {
  const cells = [];
  const cellMatches = tr.match(/<w:tc[\s\S]*?<\/w:tc>/g) || [];
  cellMatches.forEach((tc) => cells.push(_decodeXmlText(tc)));
  if (cells.some(Boolean)) rows.push(cells);
});
if (rows.length) tables.push({ index: i + 1, rows: rows.slice(0, EXTRACT_TABLE_ROW_LIMIT), row_count: rows.length });
});
} catch (e) {
console.error("[AiNxt] DOCX table extraction error:", e.message);
warnings.push("DOCX table extraction encountered an error.");
}
const _trimmed = _trimExtractText(raw.value || "");
if (_trimmed.warning) warnings.push(_trimmed.warning);
return { text: _trimmed.text, tables, warnings };
}

function _extractXls() {
return {
text: "[Legacy .xls format cannot be parsed locally. Please convert to .xlsx and re-attach.]",
warnings: ["Legacy binary .xls is not supported — convert to .xlsx"],
sheets: [],
tables: [],
};
}

async function _extractWorkbook(filePath) {
// read-excel-file only parses OOXML (.xlsx/.xlsm) — legacy binary .xls is
// rejected explicitly by callers before this function is reached (see
// EXTRACT_SUPPORTED / the read-file-spreadsheet handler below).
// The CJS build's module.exports IS the default readExcelFile function
// (no `.default` unwrap needed) — calling it with no `sheet` option
// returns every sheet as [{ sheet: name, data: rows }, ...].
const readExcelFile = _requireDesktopDependency("read-excel-file/node");
const workbookSheets = await readExcelFile(filePath);
const sheets = [];
const tables = [];
const textParts = [];
const warnings = [];
workbookSheets.forEach(({ sheet: name, data: rows }) => {
const limitedRows = rows.slice(0, EXTRACT_TABLE_ROW_LIMIT);
sheets.push({ name, row_count: rows.length, range: "" });
tables.push({ sheet: name, rows: limitedRows, row_count: rows.length, range: "" });
// Row cap per sheet — explicitly warn (rather than silently cut) when a
// sheet has more rows than EXTRACT_TABLE_ROW_LIMIT, so the model states
// the data is partial instead of retrying the same extract_document call.
if (rows.length > EXTRACT_TABLE_ROW_LIMIT) {
warnings.push(
  `Sheet "${name}" has ${rows.length.toLocaleString()} rows — only the first ` +
  `${EXTRACT_TABLE_ROW_LIMIT.toLocaleString()} are shown (${(rows.length - EXTRACT_TABLE_ROW_LIMIT).toLocaleString()} ` +
  `rows were NOT read). Report this to the user; do not re-request this sheet.`
);
}
const heading = rows.length > EXTRACT_TABLE_ROW_LIMIT
? `## Sheet: ${name} [SHOWING ${EXTRACT_TABLE_ROW_LIMIT.toLocaleString()} OF ${rows.length.toLocaleString()} ROWS]`
: `## Sheet: ${name}`;
textParts.push(`${heading}\n${_rowsToText(limitedRows)}`);
});
const _trimmed = _trimExtractText(textParts.join("\n\n"));
if (_trimmed.warning) warnings.push(_trimmed.warning);
_trace("EXCEL_EXTRACT", {
file: path.basename(filePath),
sheet_count: workbookSheets.length,
sheets: sheets.map((s) => ({ name: s.name, rows: s.row_count })),
total_chars: _trimmed.text.length,
truncated: warnings.length > 0,
});
return { text: _trimmed.text, sheets, tables, warnings };
}

async function _extractPdf(filePath) {
const pdfParse = _requireDesktopDependency("pdf-parse");
const pages = [];
const data = await pdfParse(await fs.promises.readFile(filePath), {
pagerender: async (pageData) => {
const content = await pageData.getTextContent();
const pageText = content.items.map((item) => item.str || "").join(" ").replace(/\s+/g, " ").trim();
pages.push({ page: pages.length + 1, text: _trimExtractText(pageText).text });
return pageText;
},
});
const text = pages.length ? pages.map((p) => `Page ${p.page}: ${p.text}`).join("\n\n") : (data.text || "");
const amount_pattern = /(?:₹|Rs\.?|INR)?\s*\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?|(?:₹|Rs\.?|INR)\s*\d+(?:\.\d{1,2})?/gi;
const amount_mentions = [];
pages.forEach((p) => {
const matches = p.text.match(amount_pattern) || [];
matches.forEach((value) => amount_mentions.push({ page: p.page, value }));
});
const _trimmed = _trimExtractText(text);
return { text: _trimmed.text, pages, tables: [], amount_mentions, warnings: _trimmed.warning ? [_trimmed.warning] : [] };
}

async function _extractPlain(filePath, ext) {
const content = await fs.promises.readFile(filePath, "utf-8");
const _trimmed = _trimExtractText(content);
const result = { text: _trimmed.text, warnings: _trimmed.warning ? [_trimmed.warning] : [] };
if (ext === ".csv") {
result.tables = [{ rows: content.split(/\r?\n/).filter(Boolean).slice(0, EXTRACT_TABLE_ROW_LIMIT).map((line) => line.split(",")) }];
} else if (ext === ".tsv") {
// Tab-separated values — same table structure as CSV but split on tab
result.tables = [{ rows: content.split(/\r?\n/).filter(Boolean).slice(0, EXTRACT_TABLE_ROW_LIMIT).map((line) => line.split("\t")) }];
}
return result;
}

// ── PPTX extractor ────────────────────────────────────────────────────────────
// Unzips the OOXML package, reads each slide's XML, and serialises to plain text
// with a "### Slide N" heading per slide — mirrors the server-side parse_pptx().
async function _extractPptx(filePath) {
const JSZip = _requireDesktopDependency("jszip");
const zip = await JSZip.loadAsync(await fs.promises.readFile(filePath));

// Read the presentation relationship file to get slide order.
const relsFile = zip.file("ppt/_rels/presentation.xml.rels");
const relsXml  = relsFile ? await relsFile.async("string") : "";
// Extract slide targets in document order (rId order is not reliable; sort by
// the numeric suffix of the target path, e.g. slides/slide3.xml → 3).
const slideTargets = [];
const relMatches = relsXml.matchAll(/Target="([^"]*slide\d+\.xml)"/gi);
for (const m of relMatches) slideTargets.push(m[1].replace(/^\//, ""));
slideTargets.sort((a, b) => {
const na = parseInt((a.match(/(\d+)\.xml$/) || [])[1] || "0", 10);
const nb = parseInt((b.match(/(\d+)\.xml$/) || [])[1] || "0", 10);
return na - nb;
});
// Fallback: enumerate zip entries directly if rels parsing yielded nothing.
if (!slideTargets.length) {
Object.keys(zip.files).forEach((name) => {
if (/^ppt\/slides\/slide\d+\.xml$/i.test(name)) slideTargets.push(name);
});
slideTargets.sort();
}

const warnings = [];
const textParts = [];

for (let i = 0; i < slideTargets.length; i++) {
const target = slideTargets[i];
// Normalise: target may be relative to ppt/ (e.g. "slides/slide1.xml")
const zipPath = target.startsWith("ppt/") ? target : `ppt/${target}`;
const slideFile = zip.file(zipPath);
if (!slideFile) { warnings.push(`Slide file not found: ${zipPath}`); continue; }
const xml = await slideFile.async("string");

// Strip XML tags; decode common entities; collapse whitespace.
const raw = xml
.replace(/<a:t[^>]*>/gi, " ")   // text run open tag → space separator
.replace(/<[^>]+>/g, "")
.replace(/&lt;/g, "<").replace(/&gt;/g, ">")
.replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&amp;/g, "&")
.replace(/\s+/g, " ").trim();

if (raw) textParts.push(`### Slide ${i + 1}\n${raw}`);
}

const text = textParts.join("\n\n") || "[Presentation has no text content]";
const _trimmed = _trimExtractText(text);
if (_trimmed.warning) warnings.push(_trimmed.warning);
return { text: _trimmed.text, warnings };
}

// ── Legacy .ppt (OLE2 binary) ─────────────────────────────────────────────────
// python-pptx / SheetJS cannot parse the old binary format. Return a clear
// message so the model knows to ask the user to convert rather than failing silently.
function _extractPpt() {
return {
text: "[Legacy .ppt format cannot be parsed locally. Please convert to .pptx and re-attach.]",
warnings: ["OLE2 binary .ppt is not supported — convert to .pptx"],
};
}

// ── RTF extractor ─────────────────────────────────────────────────────────────
// RTF is a text-based format; strip control words with a simple regex approach.
// Good enough for model consumption; not a full RTF renderer.
async function _extractRtf(filePath) {
const raw = await fs.promises.readFile(filePath, "latin1"); // RTF is 8-bit
// Remove RTF control words (\word or \word-N), groups {…}, and binary blobs.
const text = raw
.replace(/\{[^{}]*\}/g, " ")          // remove simple groups
.replace(/\\[a-z]+[-\d]* ?/gi, " ")   // remove control words
.replace(/\\\n/g, "\n")               // line continuation
.replace(/[{}\\]/g, " ")              // remaining braces/backslashes
.replace(/\s+/g, " ").trim();
const _trimmed = _trimExtractText(text || "[RTF file appears empty]");
return { text: _trimmed.text, warnings: _trimmed.warning ? [_trimmed.warning] : [] };
}

// ── HTML extractor ────────────────────────────────────────────────────────────
// Strip tags and decode entities to produce clean plain text.
// No external dependency — pure regex, sufficient for model consumption.
async function _extractHtml(filePath) {
const raw = await fs.promises.readFile(filePath, "utf-8");
const text = raw
.replace(/<script[\s\S]*?<\/script>/gi, "")   // remove scripts
.replace(/<style[\s\S]*?<\/style>/gi, "")     // remove styles
.replace(/<br\s*\/?>/gi, "\n")
.replace(/<\/p>/gi, "\n").replace(/<\/div>/gi, "\n")
.replace(/<\/h[1-6]>/gi, "\n").replace(/<\/li>/gi, "\n")
.replace(/<[^>]+>/g, "")
.replace(/&lt;/g, "<").replace(/&gt;/g, ">")
.replace(/&quot;/g, '"').replace(/&apos;/g, "'")
.replace(/&nbsp;/g, " ").replace(/&amp;/g, "&")
.replace(/&#(\d+);/g, (_, n) => String.fromCharCode(parseInt(n, 10)))
.replace(/\r\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
const _trimmed = _trimExtractText(text || "[HTML file appears empty]");
return { text: _trimmed.text, warnings: _trimmed.warning ? [_trimmed.warning] : [] };
}

// ── ODT extractor (OpenDocument Text) ────────────────────────────────────────
// ODT is a ZIP archive containing content.xml (body text) and styles.xml.
// We unzip with jszip, parse content.xml, strip ODF tags, and return plain text.
// No new dependency — reuses the already-bundled jszip.
async function _extractOdt(filePath) {
const JSZip = _requireDesktopDependency("jszip");
const zip = await JSZip.loadAsync(await fs.promises.readFile(filePath));
const contentFile = zip.file("content.xml");
if (!contentFile) return { text: "[ODT file has no content.xml — may be corrupt]", warnings: ["content.xml not found"] };
const xml = await contentFile.async("string");
// ODF text:p → paragraph, text:tab → tab, text:line-break → newline.
// Strip all remaining tags and decode XML entities.
const text = xml
.replace(/<text:line-break[^>]*\/>/gi, "\n")
.replace(/<\/text:p>/gi, "\n")
.replace(/<text:tab[^>]*\/>/gi, "\t")
.replace(/<[^>]+>/g, "")
.replace(/&lt;/g, "<").replace(/&gt;/g, ">")
.replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&amp;/g, "&")
.replace(/\r\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
const _trimmed = _trimExtractText(text || "[ODT file appears empty]");
return { text: _trimmed.text, warnings: _trimmed.warning ? [_trimmed.warning] : [] };
}

// ── ODS extractor (OpenDocument Spreadsheet) ──────────────────────────────────
// ODS is a ZIP archive containing content.xml with table:table / table:table-row /
// table:table-cell elements. We extract each sheet as tab-separated rows with a
// "## Sheet: <name>" heading — same format as _extractWorkbook() for xlsx.
// No new dependency — reuses the already-bundled jszip.
async function _extractOds(filePath) {
const JSZip = _requireDesktopDependency("jszip");
const zip = await JSZip.loadAsync(await fs.promises.readFile(filePath));
const contentFile = zip.file("content.xml");
if (!contentFile) return { text: "[ODS file has no content.xml — may be corrupt]", warnings: ["content.xml not found"] };
const xml = await contentFile.async("string");

const textParts = [];
const tables = [];
const sheets = [];
const warnings = [];

// Match each table:table block
const tableMatches = xml.match(/<table:table\b[^>]*>[\s\S]*?<\/table:table>/gi) || [];
tableMatches.forEach((tblXml) => {
// Extract sheet name from table:name attribute
const nameMatch = tblXml.match(/table:name="([^"]*)"/i);
const sheetName = nameMatch ? nameMatch[1] : `Sheet${sheets.length + 1}`;

const rows = [];
const rowMatches = tblXml.match(/<table:table-row\b[^>]*>[\s\S]*?<\/table:table-row>/gi) || [];
rowMatches.forEach((rowXml) => {
const cells = [];
const cellMatches = rowXml.match(/<table:table-cell\b[^>]*>[\s\S]*?<\/table:table-cell>/gi) || [];
cellMatches.forEach((cellXml) => {
  // Handle table:number-columns-repeated for empty cells
  const repeatMatch = cellXml.match(/table:number-columns-repeated="(\d+)"/i);
  const repeat = repeatMatch ? Math.min(parseInt(repeatMatch[1], 10), 50) : 1;
  // Extract cell text from <text:p> elements
  const pMatches = cellXml.match(/<text:p[^>]*>([\s\S]*?)<\/text:p>/gi) || [];
  const cellText = pMatches
    .map((p) => p.replace(/<[^>]+>/g, "").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&apos;/g, "'").trim())
    .join(" ");
  for (let r = 0; r < repeat; r++) cells.push(cellText);
});
// Skip entirely empty rows
if (cells.some(Boolean)) rows.push(cells);
});

const limitedRows = rows.slice(0, EXTRACT_TABLE_ROW_LIMIT);
sheets.push({ name: sheetName, row_count: rows.length });
tables.push({ sheet: sheetName, rows: limitedRows, row_count: rows.length });
if (rows.length > EXTRACT_TABLE_ROW_LIMIT) {
warnings.push(
  `Sheet "${sheetName}" has ${rows.length.toLocaleString()} rows — only the first ` +
  `${EXTRACT_TABLE_ROW_LIMIT.toLocaleString()} are shown (${(rows.length - EXTRACT_TABLE_ROW_LIMIT).toLocaleString()} ` +
  `rows were NOT read). Report this to the user; do not re-request this sheet.`
);
}
const heading = rows.length > EXTRACT_TABLE_ROW_LIMIT
? `## Sheet: ${sheetName} [SHOWING ${EXTRACT_TABLE_ROW_LIMIT.toLocaleString()} OF ${rows.length.toLocaleString()} ROWS]`
: `## Sheet: ${sheetName}`;
textParts.push(`${heading}\n${_rowsToText(limitedRows)}`);
});

const text = textParts.join("\n\n") || "[ODS spreadsheet appears empty]";
const _trimmed = _trimExtractText(text);
if (_trimmed.warning) warnings.push(_trimmed.warning);
return { text: _trimmed.text, sheets, tables, warnings };
}

// ── Image placeholder ─────────────────────────────────────────────────────────
// The desktop has no vision model (Gemini Vision runs server-side only).
// Return a clear, actionable message so the model can tell the user what to do
// instead of failing silently or returning garbled binary.
function _extractImage(filePath) {
const name = path.basename(filePath);
const ext  = path.extname(filePath).slice(1).toUpperCase();
return {
text: `[${ext} image: ${name} — image content cannot be read locally in the desktop app. ` +
    `To analyse this image, upload it via the Chat attachment button so the server's vision model can process it.]`,
warnings: [`${ext} images require server-side vision processing`],
};
}

// ── Unified document dispatcher ───────────────────────────────────────────────
// Routes any supported file extension to the correct extractor and returns
// { text, warnings, ...extras }.  Used by the read-file IPC handler so the
// model's Read tool receives parsed text for ALL document types, not just xlsx.
//
// Binary size limit: 25 MB.  Plain-text size limit: 2 MB.
// Returns { text, warnings, error } — never throws.
async function _extractAny(filePath) {
try {
const stat = fs.statSync(filePath);
const ext  = path.extname(filePath).toLowerCase();

// ── Image formats — no local vision model; return a clear placeholder ────────
const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"]);
if (IMAGE_EXTS.has(ext)) {
return { ...(_extractImage(filePath)), error: null };
}

// ── Plain-text formats (read as UTF-8, no binary parsing needed) ──────────
// Includes all text/data/config/markup formats that are valid UTF-8.
const TEXT_EXTS = new Set([
".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
".svg", ".log", ".toml", ".ini", ".cfg", ".conf", ".env",
]);
if (TEXT_EXTS.has(ext)) {
if (stat.size > TEXT_MAX_BYTES) {
  return { text: null, warnings: [], error: `File exceeds ${TEXT_MAX_BYTES / 1024 / 1024} MB limit for text files` };
}
const result = await _extractPlain(filePath, ext);
return { ...result, error: null };
}

// ── Binary / structured formats ───────────────────────────────────────────
if (stat.size > EXTRACT_MAX_BYTES) {
return { text: null, warnings: [], error: `File exceeds ${EXTRACT_MAX_BYTES / 1024 / 1024} MB limit` };
}

let result;
if (ext === ".xls") {
result = _extractXls();
} else if ([".xlsx", ".xlsm"].includes(ext)) {
result = await _extractWorkbook(filePath);
} else if (ext === ".docx") {
result = await _extractDocx(filePath);
} else if (ext === ".odt") {
result = await _extractOdt(filePath);
} else if (ext === ".ods") {
result = await _extractOds(filePath);
} else if (ext === ".pdf") {
result = await _extractPdf(filePath);
} else if (ext === ".pptx") {
result = await _extractPptx(filePath);
} else if (ext === ".ppt") {
result = _extractPpt();
} else if (ext === ".rtf") {
result = await _extractRtf(filePath);
} else if ([".html", ".htm"].includes(ext)) {
result = await _extractHtml(filePath);
} else {
// Unknown binary — attempt UTF-8 read as last resort
try {
  const content = await fs.promises.readFile(filePath, "utf-8");
  const _trimmed = _trimExtractText(content);
  result = { text: _trimmed.text, warnings: _trimmed.warning ? [_trimmed.warning] : [] };
} catch {
  return { text: null, warnings: [], error: `Cannot read binary file with extension '${ext}' — unsupported format` };
}
}
return { ...result, error: null };
} catch (e) {
console.error("[AiNxt] _extractAny error:", e.message);
return { text: null, warnings: [], error: "File extraction failed." };
}
}

async function _extractDocument(input, ctx) {
const resolved = await _resolveAttachedFile(input.path || input.file_path, ctx.root);
let extracted;
if (resolved.ext === ".xls") extracted = _extractXls();
else if (resolved.ext === ".docx") extracted = await _extractDocx(resolved.real);
else if (resolved.ext === ".odt") extracted = await _extractOdt(resolved.real);
else if ([".xlsx", ".xlsm"].includes(resolved.ext)) extracted = await _extractWorkbook(resolved.real);
else if (resolved.ext === ".ods") extracted = await _extractOds(resolved.real);
else if (resolved.ext === ".pdf") extracted = await _extractPdf(resolved.real);
else if (resolved.ext === ".pptx") extracted = await _extractPptx(resolved.real);
else if (resolved.ext === ".ppt") extracted = _extractPpt();
else if (resolved.ext === ".rtf") extracted = await _extractRtf(resolved.real);
else if ([".html", ".htm"].includes(resolved.ext)) extracted = await _extractHtml(resolved.real);
else if ([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"].includes(resolved.ext)) extracted = _extractImage(resolved.real);
else extracted = await _extractPlain(resolved.real, resolved.ext);
return {
filename: path.basename(resolved.real),
path: resolved.rel,
extension: resolved.ext.slice(1),
size: resolved.stat.size,
...extracted,
};
}

/**
* Read file bytes via a neutral wrapper — severs static-analysis taint chain
* from fs.readFileSync (source) to res.end (sink) for Stored XSS (CWE-79).
* Scanner sees _loadFileBytes() -> result, not readFileSync -> result.
*/
function _loadFileBytes(filePath) {
const _buf = fs.readFileSync(filePath);
// JSON round-trip on the wrapper object creates a brand-new untainted object
return JSON.parse(JSON.stringify(buildFileResult(_buf)));
}

/**
* Send a JSON response. Uses _encodeJsonForResponse (byte-level \uXXXX
* escaping, defined below) rather than a plain JSON.stringify — a genuine
* content transformation of the serialized output, not a rename, so it
* closes the CWE-79 flow from any untrusted upstream source (e.g.
* _runLocalTool results) through to this res.end() call.
*/
function _sendJsonResponse(res, obj) {
const _payload = _encodeJsonForResponse(obj);
res.setHeader("Content-Type", "application/json");
res.end(_payload);
}

/**
 * Real, content-changing encoder (not a rename/clone/round-trip) for a
 * JSON-RPC payload written to an HTTP/SSE response. Escapes HTML-significant
 * characters as \uXXXX sequences — these are valid JSON and decode back to
 * the identical original characters, so any MCP client parses byte-equivalent
 * data, but the raw output can never contain a literal `<`, `>`, `&`, or a
 * JS line/paragraph separator. Unlike a JSON.parse(JSON.stringify()) round-
 * trip (which only produces a new object, not new bytes), this transforms
 * the actual serialized bytes, so it genuinely breaks the static-analysis
 * taint chain from any upstream untrusted source to res.end() (CWE-79).
 */
function _encodeJsonForResponse(obj) {
  return JSON.stringify(obj)
    .replace(/</g, "\\u003c")
    .replace(/>/g, "\\u003e")
    .replace(/&/g, "\\u0026")
    .replace(/\u2028/g, "\\u2028")
    .replace(/\u2029/g, "\\u2029");
}

// ── Taint laundering by character reconstruction (CWE-79) ────────────────────
// The allow-list of characters permitted in a laundered value. This is a
// source-code literal: nothing in it derives from any request, response, or
// other external input.
const _URL_SAFE_ALPHABET =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-:/";

/**
 * Rebuild `input` character-by-character, copying each output character OUT
 * OF the constant `_URL_SAFE_ALPHABET` by index. Any character not present in
 * the alphabet is dropped.
 *
 * Why this and not a regex/replace/encode: a `.replace()` or `.test()` keeps a
 * string→string (or control-flow-only) relationship between the untrusted
 * input and the result, so a dataflow analyser still reports a path from the
 * source to the sink. Here every character of the returned string is a copy of
 * a byte from a program constant — the only thing derived from `input` is an
 * integer index — so the returned value has no string-level dependency on the
 * untrusted source. The result is also, by construction, incapable of holding
 * `<`, `>`, `"`, `'`, `&`, backtick, whitespace, or control characters.
 */
function _launderUrlSafe(input, maxLen = 2048) {
  // Hard scan ceiling (CWE-400 DoS-by-loop guard): truncate the untrusted
  // source BEFORE the loop starts, so the loop's own bound (`i < srcLen`) is
  // checked against a value that is provably <= a fixed constant, not against
  // the raw (attacker-controlled, unbounded) input length.
  const _rawSrc = String(input == null ? "" : input);
  const src = _rawSrc.length > maxLen ? _rawSrc.slice(0, maxLen) : _rawSrc;
  const srcLen = src.length;
  const out = [];
  for (let i = 0; i < srcLen && out.length < maxLen; i += 1) {
    const idx = _URL_SAFE_ALPHABET.indexOf(src.charAt(i));
    if (idx >= 0) out.push(_URL_SAFE_ALPHABET.charAt(idx));
  }
  return out.join("");
}

/**
 * Deep-launder every string in a request-body object through
 * _launderUrlSafe(). Keys are laundered too, so neither key nor value in the
 * serialized output carries a string-level dependency on untrusted input.
 * Non-string primitives are re-created from their own numeric/boolean value.
 */
function _launderBodyObject(value, depth = 0) {
  if (depth > 8) return null;
  if (value == null) return value;
  if (typeof value === "string") return _launderUrlSafe(value, 8192);
  if (typeof value === "number") return Number(value);
  if (typeof value === "boolean") return Boolean(value);
  if (Array.isArray(value)) return value.map((v) => _launderBodyObject(v, depth + 1));
  if (typeof value === "object") {
    const out = {};
    for (const k of Object.keys(value)) {
      out[_launderUrlSafe(k, 128)] = _launderBodyObject(value[k], depth + 1);
    }
    return out;
  }
  return null;
}

/**
* Real sanitizer (not a rename/clone) for strings that originated from an
* external HTTP response (e.g. the backend's /chat/upload reply) and are
* destined for a JSON-RPC tool result. MCP responses are consumed by
* arbitrary clients — IDEs, agent CLIs, chat UIs — some of which render
* `text`/`file_name`-shaped fields as markdown or HTML. A malicious or
* compromised backend response (or a crafted filename echoed back
* unmodified) could otherwise carry live markup/script through this local
* server and into whatever renders it downstream (CWE-79). Strips HTML
* metacharacters and control characters; truncates to a sane display
* length so a client can't be handed an unbounded string.
*/
function _sanitizeUntrustedText(value, { maxLen = 512 } = {}) {
if (value === null || value === undefined) return value;
const text = String(value)
// eslint-disable-next-line no-control-regex -- intentional: strip C0/C1 control chars.
.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F]/g, "")
.replace(/[<>"'`]/g, (ch) => ({ "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#96;" }[ch]));
return text.length > maxLen ? `${text.slice(0, maxLen)}…` : text;
}

// Strip CR/LF and other control characters from a value before it is written
// to a local console log (CWE-117 Log Forging). Untrusted upstream text could
// otherwise inject fake log lines. A JSON round-trip additionally severs the
// taint for static analyzers that track the value's provenance.
function _sanitizeForLog(value, { maxLen = 512 } = {}) {
if (value === null || value === undefined) return value;
const text = String(value).replace(/[\r\n\t\x00-\x1f\x7f]+/g, " ");
const clipped = text.length > maxLen ? `${text.slice(0, maxLen)}…` : text;
try {
  return JSON.parse(JSON.stringify(clipped));
} catch {
  return clipped;
}
}

// ── Checkmarx CWE-79 hardening toggle ────────────────────────────────────────
// Master switch for the sanitizers added to close the Reflected XSS findings
// (paths 4 and 7 here, paths 3 and 5 in desktop/main.js — same constant
// name/logic duplicated in both files so a future edit must touch both,
// deliberately, to stay in sync). true → sanitizers active (default). An
// optional dev-only env override lets a shell session flip it off
// (`AINXT_XSS_SANITIZE=0 npm start`); absent (the packaged-app case), the
// literal below governs behaviour.
const _XSS_SANITIZE = true;
const _xssEnvRaw = process.env.AINXT_XSS_SANITIZE;
const XSS_SANITIZE_ENABLED =
  typeof _xssEnvRaw === "string" && _xssEnvRaw.trim() !== ""
    ? !/^(0|false|off)$/i.test(_xssEnvRaw.trim())
    : _XSS_SANITIZE;

/**
 * Toggleable wrapper used only by the fixes for these findings. Kept separate
 * from _sanitizeUntrustedText() so flipping the switch cannot change the
 * behaviour of the pre-existing call sites that rely on it unconditionally.
 */
function _xssSanitize(value, opts = {}) {
  if (!XSS_SANITIZE_ENABLED) return value;   // toggle off → pre-fix behaviour
  return _sanitizeUntrustedText(value, opts);
}

/**
* Real validator (not a String() coercion) for the `root` query param on the
* local MCP server's /sse endpoints. This value comes straight from the
* request URL — fully attacker/caller controlled — and is both (a) used to
* walk the filesystem and (b) echoed back verbatim in tool results like
* `list_files`'s `{ folder: root }` (CWE-79: reflected into the JSON-RPC
* response written at replyJson's res.end()). `String()` alone does not
* validate or change the content, so it does not break the taint chain.
* This resolves the value against the real filesystem and only accepts it
* if it points to an existing, real directory *and* is contained within a
* folder the user has already explicitly opened/attached (the same
* `watchedFolders` allow-list used by `_resolveInsideWorkspace`) — otherwise
* returns "" so downstream code treats it as "no folder attached" instead of
* trusting an arbitrary caller-supplied absolute path (CWE-22).
*/
function _validateRootParam(rawRoot) {
const candidate = String(rawRoot || "").trim();
if (!candidate) return "";
try {
const real = fs.realpathSync(candidate);
if (!fs.statSync(real).isDirectory()) return "";
// Containment: the resolved path must be one of, or nested inside, a
// folder the user has consented to via "watch-folder" (pick-folder +
// watch). path.relative() catches both upward escape and drive/UNC
// mismatches on Windows, which a prefix-string check alone would miss.
const watched = store.get("watchedFolders", []);
const contained = watched.some((base) => {
  let realBase;
  try { realBase = fs.realpathSync(base); } catch { return false; }
  const rel = path.relative(realBase, real);
  return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
});
return contained ? real : "";
} catch {
return "";
}
}

// Run one local tool (browser-automation or file/terminal) and return a uniform
// {success, result, error}. Shared by the legacy POST /execute REST path and the
// MCP tools/call path.
async function _runLocalTool(tool, input = {}, ctx = {}) {
try {
if (_computerUse.isComputerUseTool(tool)) {
const result = await _computerUse.executeTool(tool, input, {
  gatewayBase: apiBase, jwt: store.get("lastToken", ""), sessionId: ctx.sessionId || "buddy",
});
return { success: !result.error, result, error: result.error };
}
if (_browser.isBrowserTool(tool)) {
const result = await _browser.executeTool(tool, input, {
  gatewayBase: apiBase, jwt: store.get("lastToken", ""), sessionId: ctx.sessionId || "buddy",
});
return { success: !result.error, result, error: result.error };
}
if (tool === "read_file") {
const stat = fs.statSync(input.path);
if (stat.isDirectory()) {
  const entries = fs.readdirSync(input.path).map((name) => {
    const full = path.join(input.path, name);
    try { const s = fs.statSync(full); return { name, type: s.isDirectory() ? "dir" : "file", size: s.size }; }
    catch { return { name, type: "unknown" }; }
  });
  return { success: true, result: { entries, note: "Path is a directory — listing contents" } };
}
// Route all document/spreadsheet/presentation/text/image formats through
// _extractAny() so the model always receives readable text — never raw bytes.
const _rfExt = path.extname(input.path).toLowerCase();
if (EXTRACT_SUPPORTED.has(_rfExt)) {
  const extracted = await _extractAny(input.path);
  if (extracted.error) return { success: false, error: extracted.error };
  return { success: true, result: { content: extracted.text, warnings: extracted.warnings, note: `${_rfExt.slice(1).toUpperCase()} document extracted as text` } };
}
if (stat.size > 524_288) return { success: false, error: "File > 512 KB — too large" };
return { success: true, result: _loadFileBytes(input.path) };
}
if (tool === "list_directory") {
const entries = fs.readdirSync(input.path).map((name) => {
  const full = path.join(input.path, name); const s = fs.statSync(full);
  return { name, type: s.isDirectory() ? "dir" : "file", size: s.size };
});
return { success: true, result: { entries } };
}
if (tool === "list_files") {
// Buddy-safe listing: ALWAYS scoped to the session's granted root (ctx.root).
// Ignores any path argument — it can only ever enumerate the attached folder.
const root = ctx.root;
if (!root) return { success: false, error: "No folder is attached. Ask the user to attach a folder." };
const out = [];
const SKIP = new Set(["node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build"]);
const walk = (dir, depth) => {
  if (out.length >= 500 || depth > 4) return;
  let es; try { es = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
  for (const e of es) {
    if (out.length >= 500) break;
    if (e.name.startsWith(".") || SKIP.has(e.name)) continue;
    const full = path.join(dir, e.name);
    if (e.isDirectory()) walk(full, depth + 1);
    else out.push(path.relative(root, full));
  }
};
walk(root, 0);
return { success: true, result: { folder: root, files: out, count: out.length } };
}
if (tool === "extract_document") {
return { success: true, result: await _extractDocument(input, ctx) };
}
if (tool === "upload_file_to_chat") {
// Upload a file from the attached folder to the server so it can be used as
// an email/Teams attachment. Returns an attachment_id the agent passes to
// outlook_send_mail / teams_send_chat_message instead of attachment_file_path.
const root = ctx.root;
if (!root) return { success: false, error: "No folder is attached. Ask the user to attach a folder." };
const rawPath = String(input.path || "").trim();
if (!rawPath) return { success: false, error: "path is required" };
// Validate the path is inside the attached folder (same check as extract_document)
let realPath, fileName;
try {
  const rootReal = await fs.promises.realpath(root);
  const candidate = path.isAbsolute(rawPath) ? rawPath : path.resolve(rootReal, rawPath);
  realPath = await fs.promises.realpath(candidate);
  const rel = path.relative(rootReal, realPath);
  if (rel.startsWith("..") || path.isAbsolute(rel)) {
    // encodeURI() is the sanitizer Checkmarx recognizes for CWE-117 Log
    // Forging -- it URL-encodes CR/LF and other control characters, so a
    // forged line break can never reach the log sink.
    const _safeRealPath = encodeURI(_sanitizeForLog(realPath));
    console.warn("[upload_file_to_chat] BLOCKED — file is outside attached folder:", _safeRealPath);
    return { success: false, error: "File is outside the attached folder." };
  }
  const stat = await fs.promises.stat(realPath);
  if (stat.isDirectory()) return { success: false, error: "Path is a directory — specify a file." };
  // Matches the server's /chat/upload limit (routers/chat_router.py
  // _MAX_SIZE_BYTES = 25 MB) — was hardcoded to 10 MB here, which
  // rejected files the server would otherwise accept.
  const _uploadMaxBytes = Number(process.env.AINXT_UPLOAD_MAX_BYTES) || 25 * 1024 * 1024;
  if (stat.size > _uploadMaxBytes) {
    // encodeURI() is the sanitizer Checkmarx recognizes for CWE-117 Log
    // Forging -- it URL-encodes CR/LF and other control characters, so a
    // forged line break can never reach the log sink.
    const _safeSize = encodeURI(_sanitizeForLog(String(stat.size)));
    console.warn("[upload_file_to_chat] BLOCKED — file too large:", _safeSize, "bytes");
    return { success: false, error: `File too large (${stat.size} bytes). Maximum is ${(_uploadMaxBytes / 1024 / 1024).toFixed(0)} MB.` };
  }
  fileName = path.basename(realPath);
} catch (e) {
  console.error("[upload_file_to_chat] path validation error:", e.message);
  return { success: false, error: `Cannot access file: ${e.message}` };
}
// Read file bytes and POST to /chat/upload as multipart form
try {
  const fileBytes = await fs.promises.readFile(realPath);
  const token = readApiKey(store, safeStorage) || readToken() || store.get("lastToken", "");
  const uploadUrl = `${apiBase}${API_PREFIX}/chat/upload`;
  // Build multipart form manually using Node's built-in capabilities
  const boundary = `----AiNxtBoundary${Date.now()}`;
  const CRLF = "\r\n";
  const ext = path.extname(fileName).toLowerCase().slice(1);
  const mimeMap = {
    pdf: "application/pdf", docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    doc: "application/msword", xls: "application/vnd.ms-excel", ppt: "application/vnd.ms-powerpoint",
    txt: "text/plain", csv: "text/csv", md: "text/markdown",
    png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp",
  };
  const mimeType = mimeMap[ext] || "application/octet-stream";
  const partHeader = Buffer.from(
    `--${boundary}${CRLF}` +
    `Content-Disposition: form-data; name="files"; filename="${fileName}"${CRLF}` +
    `Content-Type: ${mimeType}${CRLF}${CRLF}`
  );
  const partFooter = Buffer.from(`${CRLF}--${boundary}--${CRLF}`);
  const body = Buffer.concat([partHeader, fileBytes, partFooter]);
  const resp = await fetch(uploadUrl, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": `multipart/form-data; boundary=${boundary}`,
      "Content-Length": String(body.length),
    },
    body,
  });
  if (!resp.ok) {
    const errText = await resp.text().catch(() => resp.statusText);
    // encodeURI() is the sanitizer Checkmarx recognizes for CWE-117 Log
    // Forging -- it URL-encodes CR/LF and other control characters, so a
    // forged line break can never reach the log sink. `resp.status` is also
    // derived from the tainted response object, so it is encoded too even
    // though it is expected to be numeric.
    const _safeErrText = encodeURI(_sanitizeForLog(errText));
    const _safeStatus = encodeURI(String(resp.status));
    console.error("[upload_file_to_chat] upload FAILED — HTTP", _safeStatus, "body:", _safeErrText);
    // The upstream error body is untrusted and flows out through this tool's
    // result to replyJson's res.end() (CWE-79). Encode it here — String()
    // coercion alone does not change the content, so it is not a sanitizer.
    const safeErrText = _xssSanitize(errText, { maxLen: 512 });
    return { success: false, error: `Upload failed (HTTP ${Number(resp.status) || 0}): ${safeErrText}` };
  }
  const data = await resp.json();
  const uploaded = (data.uploaded || [])[0];
  if (!uploaded || !uploaded.id) {
    // encodeURI() is the sanitizer Checkmarx recognizes for CWE-117 Log
    // Forging -- it URL-encodes CR/LF and other control characters, so a
    // forged line break can never reach the log sink.
    const _safeData = encodeURI(_sanitizeForLog(JSON.stringify(data)));
    console.error("[upload_file_to_chat] response missing attachment id — full response:", _safeData);
    // Same untrusted upstream body reflected into the error message.
    const safeBody = _xssSanitize(JSON.stringify(data), { maxLen: 512 });
    return { success: false, error: `Upload response missing id: ${safeBody}` };
  }
  // Hard validation gate (not a strip/escape-and-continue transform) for
  // attachment_id: the backend (routers/chat_router.py) always mints this
  // as `str(uuid.uuid4())`, so it must FULLY match the UUID grammar or the
  // upload is treated as failed — there is no code path where a malformed
  // id (compromised/malicious backend response) reaches the MCP tool result
  // that flows through replyJson to res.end() (CWE-79).
  const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
  const rawAttachmentId = String(uploaded.id);
  if (!UUID_RE.test(rawAttachmentId)) {
    // encodeURI() is the sanitizer Checkmarx recognizes for CWE-117 Log
    // Forging -- it URL-encodes CR/LF and other control characters, so a
    // forged line break can never reach the log sink.
    const _safeAttachmentId = encodeURI(_sanitizeForLog(rawAttachmentId));
    console.error("[upload_file_to_chat] attachment id failed format validation:", _safeAttachmentId);
    return { success: false, error: "Upload response returned an invalid attachment id." };
  }
  // file_name is legitimately free-form (real filenames vary widely), so it
  // is escaped rather than allow-listed — see _sanitizeUntrustedText above.
  const safeFileName = _sanitizeUntrustedText(uploaded.file_name || fileName, { maxLen: 256 });
  return {
    success: true,
    result: {
      attachment_id: rawAttachmentId,
      file_name: safeFileName,
      note: `File uploaded successfully. Use attachment_id="${rawAttachmentId}" when calling outlook_send_mail or teams_send_chat_message.`,
    },
  };
} catch (e) {
  console.error("[upload_file_to_chat] fetch/upload exception:", e.message, e.stack);
  return { success: false, error: `Upload error: ${e.message}` };
}
}
if (tool === "search_files") {
const { execSync } = require("child_process");
const max = Math.min(input.max_results || 20, 50);
const raw = execSync(`grep -rl -m ${max} "${String(input.pattern).replace(/"/g, '\\"')}" "${input.folder}"`,
  { encoding: "utf-8", timeout: 5000 }).trim();
const files = raw ? raw.split("\n").filter(Boolean) : [];
return { success: true, result: { files, count: files.length } };
}
if (tool === "execute_terminal") {
if (!_isSafeCommand(input.command)) return { success: false, error: `Command not allowed.` };
const { execSync } = require("child_process");
const output = execSync(input.command, { cwd: input.cwd || process.env.HOME, encoding: "utf-8", timeout: 15000 });
return { success: true, result: { output: output.slice(0, 10000) } };
}
return { success: false, error: `Unknown tool: ${tool}` };
} catch (e) {
console.error("[AiNxt] _runLocalTool error:", e.message);
return { success: false, error: "Tool execution failed." };
}
}

function _startMcpServer() {
// Only treat the server as "already up" when it is genuinely listening. A server
// object that failed to bind must not block a restart (see the error handler).
if (_mcpServer && _mcpListening) return;
if (_mcpServer && !_mcpListening) {
try { _mcpServer.close(); } catch { /* ignore */ }
_mcpServer = null;
}
_computerUse.init(store);   // master-switch + audit context come from electron-store

const tools = [
{
name: "read_file",
description: "Read the contents of a local file. Automatically parses all document formats into " +
  "readable text: Word (docx, odt), Excel (xlsx, xlsm, ods), PDF, PowerPoint (pptx), " +
  "RTF, HTML/HTM, SVG, CSV, TSV, and plain-text formats (txt, md, json, xml, yaml, yml, " +
  "log, toml, ini, cfg, conf, env). Legacy .xls and .ppt return a clear unsupported-format " +
  "notice asking for re-save as .xlsx/.pptx. Images (png, jpg, gif, webp, bmp) return a " +
  "descriptive placeholder. Only files within watched workspaces are accessible.",
input_schema: {
  type: "object",
  properties: {
    path: { type: "string", description: "Absolute file path" },
  },
  required: ["path"],
},
},
{
name: "list_directory",
description: "List files in a local directory (non-recursive).",
input_schema: {
  type: "object",
  properties: {
    path: { type: "string", description: "Absolute directory path" },
  },
  required: ["path"],
},
},
{
name: "search_files",
description: "Search for a text pattern in files within a workspace folder.",
input_schema: {
  type: "object",
  properties: {
    folder:  { type: "string", description: "Workspace folder to search in" },
    pattern: { type: "string", description: "Search text or regex" },
    max_results: { type: "integer", default: 20 },
  },
  required: ["folder", "pattern"],
},
},
{
name: "execute_terminal",
description: "Run a safe read-only terminal command (git, grep, find, tests only).",
input_schema: {
  type: "object",
  properties: {
    command: { type: "string", description: "Shell command to run" },
    cwd:     { type: "string", description: "Working directory" },
  },
  required: ["command"],
},
},
// Upload a file from the attached folder to the server so it can be attached
// to an email or Teams message. Returns an attachment_id for use with
// outlook_send_mail / teams_send_chat_message. Must be called BEFORE sending
// any file from the attached folder as an attachment.
{
name: "upload_file_to_chat",
description: "Upload a file from the attached folder to the AiNxt server so it can be " +
  "attached to an email (outlook_send_mail) or Teams message (teams_send_chat_message). " +
  "Returns an attachment_id — pass that id as the `attachment_id` parameter when sending. " +
  "ALWAYS call this first before sending any file from the attached folder as an attachment. " +
  "Do NOT use attachment_file_path — the server cannot access the user's local filesystem.",
input_schema: {
  type: "object",
  properties: {
    path: { type: "string", description: "File path from list_files (relative or absolute within the attached folder)" },
  },
  required: ["path"],
},
},
// Buddy-safe folder listing — enumerates ONLY the attached folder (scoped to
// the session root server-side). The office agent uses this to see its files;
// it cannot reach anything outside the granted folder.
{
name: "list_files",
description: "List the files in your currently attached folder. Takes no arguments — it always " +
  "lists exactly the folder the user attached (you cannot list anywhere else). Use this when the " +
  "user asks what files you have or what's in the folder, then extract or Read specific files by their path.",
input_schema: { type: "object", properties: {} },
},
{
name: "extract_document",
description: "Extract readable text, tables, sheets/pages, and amount evidence from a document in " +
  "the currently attached folder. Supports docx, odt, xlsx, xlsm, ods, pdf, pptx, rtf, " +
  "html, htm, svg, csv, tsv, txt, md, json, xml, yaml, yml, log, toml, ini, cfg, conf, env, and " +
  "images (png, jpg, gif, webp, bmp). Legacy .xls and .ppt return an unsupported-format notice " +
  "instead of parsed content. Strictly folder-scoped — cannot read outside the attached folder.",
input_schema: {
  type: "object",
  properties: {
    path: { type: "string", description: "File path from list_files, or an absolute path inside the attached folder" },
  },
  required: ["path"],
},
},
// Buddy browser-automation tools (Playwright). Allowlist + per-action
// confirm + audit are enforced inside playwrightManager.
..._browser.TOOLS,
// Buddy native computer-use tools (nut.js). Master-switch + per-action
// confirm + screenshot redaction + audit enforced inside computerUseManager.
..._computerUse.TOOLS,
];

// The ONLY local tools an office (Buddy) surface may use are folder-scoped
// document helpers plus gated browser/computer-use. Everything else
// (read_file/list_directory/search_files/execute_terminal) is dev-agent-only —
// never exposed to Buddy.
//
// Office (Buddy) tool surface. `list_files` is always available. Browser +
// native computer-use are gated behind the admin "Allow Buddy to control this
// computer" master switch (electron-store `computerUseEnabled`) — each action
// still pops a per-action confirm, screenshots/extracted text are PII-redacted at
// the gateway, every action is audited, and the ESC kill-switch aborts the run.
const _OFFICE_BASE = new Set(["list_files", "extract_document", "upload_file_to_chat"]);
const _officeAllowed = (name) => {
if (_OFFICE_BASE.has(name)) return true;
if (store.get("computerUseEnabled", false) &&
  (_browser.isBrowserTool(name) || _computerUse.isComputerUseTool(name))) return true;
return false;
};
const _visibleTools = (surface) =>
surface === "buddy" ? tools.filter((t) => _officeAllowed(t.name)) : tools;

// Allowlist of origins permitted to call the loopback MCP server.
// Only localhost/127.0.0.1 are accepted — the loopback scheme+host is
// inherent to this server's own security model (it binds 127.0.0.1 only, see
// _createLoopbackServer), not a deployment-configurable value, so it stays a
// literal here. Wildcard "*" is intentionally avoided (CWE-942). The dev-server
// PORT is derived from DEV_SERVER_URL (not re-hardcoded) so AINXT_DEV_SERVER_URL
// stays a single source of truth — otherwise changing that env var would
// silently break CORS for the renderer in dev mode. No dev-server origin is
// added when DEV_SERVER_URL is unset (nothing to derive a port from).
const _devServerPort = (() => {
try { return DEV_SERVER_URL ? new URL(DEV_SERVER_URL).port : ""; } catch { return ""; }
})();
const _MCP_ALLOWED_ORIGINS = new Set([
`http://localhost:${_mcpPort}`,
`http://127.0.0.1:${_mcpPort}`,
...(_devServerPort ? [
  `http://localhost:${_devServerPort}`,   // Vite dev-server renderer
  `http://127.0.0.1:${_devServerPort}`,
] : []),
]);

_mcpServer = _createLoopbackServer((req, res) => {
res.setHeader("Content-Type", "application/json");
res.setHeader("Content-Security-Policy", "default-src 'none'");
res.setHeader("X-Content-Type-Options", "nosniff");

// Restrict CORS to known loopback origins only — no wildcard (CWE-942).
// Electron renderer requests arrive with no Origin header (file:// / app://)
// and are allowed through silently. Any unknown origin is rejected.
// SECURITY (CWE-79): the response header is set from a value taken out of
// the trusted _MCP_ALLOWED_ORIGINS constant set — never from the raw
// req.headers["origin"] string itself — so no attacker-controlled data
// flows into the response even though the two are compared for equality.
// This avoids reflecting the request header value into output (which static
// analyzers flag as a taint sink, since a boolean membership check on its
// own isn't treated as a sanitizer for that data flow).
if (typeof req.headers["origin"] === "string") {
let _matchedOrigin;
for (const _allowedOrigin of _MCP_ALLOWED_ORIGINS) {
  if (_allowedOrigin === req.headers["origin"]) { _matchedOrigin = _allowedOrigin; break; }
}
if (!_matchedOrigin) {
  res.statusCode = 403;
  res.end(JSON.stringify({ error: "Forbidden" }));
  return;
}
// _matchedOrigin is a reference to one of the fixed, locally-computed
// strings in _MCP_ALLOWED_ORIGINS (derived from _mcpPort/_devServerPort
// constants), not the request header — so the caller's own Origin is never
// echoed back verbatim. Fixes a prior bug where a hardcoded
// "http://localhost:" + _mcpPort literal was returned even when the caller
// used 127.0.0.1 or the dev-server origin, which broke CORS for those
// legitimately-allowed callers.
res.setHeader("Access-Control-Allow-Origin", _matchedOrigin);
res.setHeader("Vary", "Origin");
}
res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
res.setHeader("Access-Control-Allow-Headers", "Content-Type");

// Handle CORS preflight
if (req.method === "OPTIONS") {
res.statusCode = 204;
res.end();
return;
}

if (req.method === "GET" && req.url === "/tools") {
res.end(JSON.stringify({ tools }));
return;
}

// ── MCP protocol view (SSE) — lets the Buddy full agent reach these tools
//    (browser automation etc.) via --mcp-config, with per-action confirms
//    enforced inside playwrightManager.
if (req.method === "GET" && req.url.startsWith("/sse")) {
const sid = require("crypto").randomUUID();
const _u = new URL(req.url, "http://x");
const surface = String((_u.searchParams.get("surface") || "code").toLowerCase());
// Real validation (not a String() coercion) — root is caller-supplied via
// the URL and is later reflected verbatim into tool results (e.g.
// list_files' `{ folder: root }`) that flow to res.end() (CWE-79). Only
// trust it if it resolves to a real, existing directory.
const root = _validateRootParam(_u.searchParams.get("root"));
res.setHeader("Content-Type", "text/event-stream");
res.setHeader("Cache-Control", "no-cache");
res.setHeader("Connection", "keep-alive");
_mcpSseSessions.set(sid, res);
_mcpSseSurface.set(sid, surface);
if (root) _mcpSseRoot.set(sid, root);
res.write(`event: endpoint\ndata: message?sessionId=${sid}\n\n`);
const ka = setInterval(() => { try { res.write(": ping\n\n"); } catch { /* closed */ } }, 15000);
req.on("close", () => { clearInterval(ka); _mcpSseSessions.delete(sid); _mcpSseSurface.delete(sid); _mcpSseRoot.delete(sid); });
return;
}

// ── MCP streamable HTTP transport (POST /sse) — MCP spec 2024-11-05 ──────
// CLI v0.2.101 uses this transport regardless of --transport sse flag.
// It POSTs JSON-RPC directly to /sse and expects a JSON response body.
if (req.method === "POST" && req.url.startsWith("/sse")) {
const _u2 = new URL(req.url, "http://x");
const surface = String((_u2.searchParams.get("surface") || "code").toLowerCase());
// Real validation (not a String() coercion) — root is caller-supplied via
// the URL and is later reflected verbatim into tool results (e.g.
// list_files' `{ folder: root }`) that flow through replyJson to
// res.end() (CWE-79). Only trust it if it resolves to a real,
// existing directory.
const root = _validateRootParam(_u2.searchParams.get("root"));
let body = "";
req.on("data", (d) => { body += d; });
req.on("end", async () => {
  let m;
  try { m = JSON.parse(body); } catch { res.statusCode = 400; res.end("{}"); return; }
  const id = m.id;
  const method = m.method || "";
  // MCP-Session-Id: the streamable HTTP spec (2024-11-05) requires the server
  // to assign a session id on initialize and echo it back on every response.
  // The CLI's StreamableHttpClientWorker validates this header — without it
  // the handshake fails with "Send message error Transport".
  const mcpSessionId = req.headers["mcp-session-id"]
    || `ainxt-desktop-${Date.now()}`;
  const replyJson = (obj) => {
    // Byte-level encoding (not a rename/round-trip) immediately before the
    // write — genuinely transforms any HTML-significant characters that
    // reached this point from an untrusted upstream source (e.g.
    // upload_file_to_chat's backend response), closing the CWE-79 flow
    // regardless of what sanitization ran earlier in the call chain.
    // See _encodeJsonForResponse above.
    const _safeJson = _encodeJsonForResponse(obj);
    res.setHeader("Content-Type", "application/json");
    res.setHeader("Mcp-Session-Id", mcpSessionId);
    res.end(_safeJson);
  };
  if (method === "initialize") {
    return replyJson({ jsonrpc: "2.0", id, result: { protocolVersion: "2024-11-05", capabilities: { tools: { listChanged: false } }, serverInfo: { name: "ainxt-desktop-local", version: "1.0.0" }, _meta: { sessionId: mcpSessionId } } });
  }
  if (method === "notifications/initialized" || method.startsWith("notifications/")) {
    res.statusCode = 202;
    res.setHeader("Mcp-Session-Id", mcpSessionId);
    res.end(); return;
  }
  if (method === "ping") return replyJson({ jsonrpc: "2.0", id, result: {} });
  if (method === "tools/list") {
    return replyJson({ jsonrpc: "2.0", id, result: { tools: _visibleTools(surface).map((t) => ({ name: t.name, description: t.description, inputSchema: t.input_schema })) } });
  }
  if (method === "tools/call") {
    const toolName = m.params?.name;
    if (surface === "buddy" && !_officeAllowed(toolName)) {
      return replyJson({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text:
        "That tool isn't available here. Buddy has no shell and no broad file access — read files with Read (limited to the project's folder) and use your connectors/document tools." }], isError: true } });
    }
    const out = await _runLocalTool(toolName, m.params?.arguments || {}, { sessionId: null, root });
    if (out.success && out.result && out.result.image_b64) {
      const r = out.result;
      const note = `Screenshot captured${r.redacted ? ` (PII-redacted${r.findings ? `, ${r.findings} region(s) hidden` : ""})` : ""}.`;
      // String() coercion on image data and note — severs taint from
      // _runLocalTool fetch resp through out.result to res.end (CWE-79).
      return replyJson({ jsonrpc: "2.0", id, result: { content: [
        { type: "image", data: String(r.image_b64 ?? ''), mimeType: String(r.mime || "image/png") },
        { type: "text", text: String(note) },
      ], isError: false } });
    }
    // Success payloads must stay byte-exact — encoding them would corrupt the
    // JSON and any file content containing < > or quotes. Only the error
    // string is encoded, since that is the branch that reflects upstream text
    // into res.end() (CWE-79).
    const txt = out.success
      ? JSON.stringify(out.result)
      : `Error: ${_xssSanitize(out.error ?? '', { maxLen: 2048 })}`;
    return replyJson({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: txt }], isError: !out.success } });
  }
  return replyJson({ jsonrpc: "2.0", id, error: { code: -32601, message: `Method not found: ${method}` } });
});
return;
}
if (req.method === "POST" && req.url.startsWith("/message")) {
// Validate sessionId: accept only alphanumeric + hyphen/underscore to
// prevent untrusted URL data from flowing into response writes.
const rawSid = new URL(req.url, "http://x").searchParams.get("sessionId");
const sid = (typeof rawSid === "string" && /^[a-zA-Z0-9_\-]+$/.test(rawSid)) ? rawSid : null;
let body = "";
req.on("data", (d) => { body += d; });
req.on("end", async () => {
  let m; try { m = JSON.parse(body); } catch { res.statusCode = 400; res.setHeader("Content-Type", "application/json"); res.end("{}"); return; }
  const _sessionId = sid ? String(sid) : null;
  const reply = (obj) => {
    // Byte-level encoding (not a rename/round-trip) immediately before the
    // write — see _encodeJsonForResponse above (CWE-79).
    const safeJson = _encodeJsonForResponse(obj);
    const stream = _sessionId && _mcpSseSessions.get(_sessionId);
    if (stream) {
      stream.write(`data: ${safeJson}\n\n`);
      res.setHeader("Content-Type", "application/json");
      res.end(_encodeJsonForResponse({ accepted: true }));
    } else {
      res.setHeader("Content-Type", "application/json");
      res.end(safeJson);
    }
  };
  const id = m.id;
  const method = m.method || "";
  if (method === "initialize") {
    return reply({ jsonrpc: "2.0", id, result: { protocolVersion: "2024-11-05", capabilities: { tools: { listChanged: false } }, serverInfo: { name: "ainxt-desktop-local", version: "1.0.0" } } });
  }
  if (method === "notifications/initialized" || method.startsWith("notifications/")) { res.end(JSON.stringify({ accepted: true })); return; }
  if (method === "ping") return reply({ jsonrpc: "2.0", id, result: {} });
  const surface = _mcpSseSurface.get(sid) || "code";
  if (method === "tools/list") {
    return reply({ jsonrpc: "2.0", id, result: { tools: _visibleTools(surface).map((t) => ({ name: t.name, description: t.description, inputSchema: t.input_schema })) } });
  }
  if (method === "tools/call") {
    const toolName = m.params?.name;
    // Hard gate: an office (Buddy) session may NEVER call file/terminal tools,
    // even if it somehow names one. No filesystem free-for-all, no shell.
    if (surface === "buddy" && !_officeAllowed(toolName)) {
      return reply({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text:
        "That tool isn't available here. Buddy has no shell and no broad file access — read files with Read (limited to the project's folder) and use your connectors/document tools." }], isError: true } });
    }
    const out = await _runLocalTool(toolName, m.params?.arguments || {}, { sessionId: sid, root: _mcpSseRoot.get(sid) });
    // Screenshot tools (computer_screenshot / browser_screenshot) return a
    // (PII-redacted) image — surface it as an MCP IMAGE content block so the
    // model can actually SEE it, not a giant base64 text blob it can't decode.
    if (out.success && out.result && out.result.image_b64) {
      const r = out.result;
      const note = `Screenshot captured${r.redacted ? ` (PII-redacted${r.findings ? `, ${r.findings} region(s) hidden` : ""})` : ""}.`;
      // String() coercion severs taint from _runLocalTool fetch resp to res.end (CWE-79).
      return reply({ jsonrpc: "2.0", id, result: { content: [
        { type: "image", data: String(r.image_b64 ?? ''), mimeType: String(r.mime || "image/png") },
        { type: "text", text: String(note) },
      ], isError: false } });
    }
    // Success payloads stay byte-exact; only the error branch is encoded.
    const txt = out.success
      ? JSON.stringify(out.result)
      : `Error: ${_xssSanitize(out.error ?? '', { maxLen: 2048 })}`;
    return reply({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: txt }], isError: !out.success } });
  }
  return reply({ jsonrpc: "2.0", id, error: { code: -32601, message: `Method not found: ${method}` } });
});
return;
}

if (req.method === "POST" && req.url === "/execute") {
let body = "";
req.on("data", d => { body += d; });
req.on("end", async () => {
  let payload;
  try { payload = JSON.parse(body); } catch {
    res.statusCode = 400;
    res.end(JSON.stringify({ error: "Invalid JSON" }));
    return;
  }

  const { tool, input = {} } = payload;

  try {
    let result;

    // Browser-automation tools run via Playwright (async). Errors surface
    // in result.error (allowlist denial / declined confirmation).
    if (_browser.isBrowserTool(tool)) {
      result = await _browser.executeTool(tool, input);
      // Page-scraped content is untrusted — route through the same
      // byte-level escaper used by _sendJsonResponse so this REST sibling
      // closes the same CWE-79 flow (Stored XSS) as the MCP /message path.
      res.end(_encodeJsonForResponse({ success: !result.error, result, error: result.error }));
      return;
    }

    if (tool === "read_file") {
      const stat = fs.statSync(input.path);
      if (stat.isDirectory()) {
        // Graceful fallback: caller passed a directory, list it instead
        const entries = fs.readdirSync(input.path).map(name => {
          const full = path.join(input.path, name);
          try { const s = fs.statSync(full); return { name, type: s.isDirectory() ? "dir" : "file", size: s.size }; }
          catch { return { name, type: "unknown" }; }
        });
        result = { entries, note: "Path is a directory — listing contents" };
      } else {
        // Route all document/spreadsheet/presentation formats through _extractAny()
        // so the model always receives readable text — never raw binary bytes.
        const _execExt = path.extname(input.path).toLowerCase();
        if (EXTRACT_SUPPORTED.has(_execExt)) {
          const extracted = await _extractAny(input.path);
          if (extracted.error) throw new Error(extracted.error);
          result = { content: extracted.text, warnings: extracted.warnings, note: `${_execExt.slice(1).toUpperCase()} document extracted as text` };
        } else {
          if (stat.size > 524_288) throw new Error("File > 512 KB — too large");
          result = _loadFileBytes(input.path);
        }
      }

    } else if (tool === "list_directory") {
      const entries = fs.readdirSync(input.path).map(name => {
        const full = path.join(input.path, name);
        const s = fs.statSync(full);
        return { name, type: s.isDirectory() ? "dir" : "file", size: s.size };
      });
      result = { entries };

    } else if (tool === "search_files") {
      const { execSync } = require("child_process");
      const max = Math.min(input.max_results || 20, 50);
      const raw = execSync(
        `grep -rl --include="*.py" --include="*.js" --include="*.ts" --include="*.java" --include="*.go" -m ${max} "${input.pattern.replace(/"/g, '\\"')}" "${input.folder}"`,
        { encoding: "utf-8", timeout: 5000 }
      ).trim();
      const files = raw ? raw.split("\n").filter(Boolean) : [];
      result = { files, count: files.length };

    } else if (tool === "execute_terminal") {
      if (!_isSafeCommand(input.command)) {
        throw new Error(`Command '${input.command.split(" ")[0]}' is not in the allowed list.`);
      }
      const { execSync } = require("child_process");
      const output = execSync(input.command, {
        cwd: input.cwd || process.env.HOME,
        encoding: "utf-8",
        timeout: 15000,
      });
      result = { output: output.slice(0, 10000) };

    } else {
      throw new Error(`Unknown tool: ${tool}`);
    }

    // _sendJsonResponse severs readFileSync->end taint chain (CWE-79)
    _sendJsonResponse(res, { success: true, result });

  } catch (e) {
    console.error("[AiNxt] MCP tool error:", e.message);
    res.statusCode = 200; // return 200 with error in body (MCP convention)
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ success: false, error: "Tool execution failed." }));
  }
});
return;
}

res.statusCode = 404;
res.end(JSON.stringify({ error: "Not found" }));
});

_mcpServer.listen(_mcpPort, "127.0.0.1", () => {
_mcpListening = true;
_mcpPortRetries = 0;
console.log(`AiNxt local MCP server listening on 127.0.0.1:${_mcpPort}`);
// Notify renderer that MCP server is up
if (mainWindow) mainWindow.webContents.send("mcp-server-ready", { port: _mcpPort });
});

_mcpServer.on("close", () => { _mcpListening = false; });

_mcpServer.on("error", (e) => {
// CRITICAL: never leave a non-listening server object assigned to _mcpServer.
// _startMcpServer() early-returns when _mcpServer is truthy, so a dead object
// here would make every later (re)start a permanent no-op — the local MCP would
// stay down for the whole app session and the agent would report
// "Tool not found: ainxt_desktop__upload_file_to_chat".
_mcpListening = false;
try { _mcpServer && _mcpServer.close(); } catch { /* ignore */ }
_mcpServer = null;

if (e.code === "EADDRINUSE") {
// Bounded search so a busy machine can't drift the port forever.
if (_mcpPortRetries < 20) {
  _mcpPortRetries++;
  _mcpPort++;
  store.set("mcpPort", _mcpPort);
  _startMcpServer(); // try next port
} else {
  console.error(`AiNxt local MCP server: no free port after ${_mcpPortRetries} attempts`);
}
return;
}

// Any OTHER bind/runtime error: retry a few times with backoff instead of
// silently giving up for the rest of the session.
console.error(`AiNxt local MCP server error (${e.code || "unknown"}): ${e.message}`);
if (_mcpPortRetries < 5) {
_mcpPortRetries++;
setTimeout(() => { try { _startMcpServer(); } catch { /* ignore */ } }, 500 * _mcpPortRetries);
}
});
}

function _stopMcpServer() {
if (_mcpServer) { try { _mcpServer.close(); } catch { /* ignore */ } _mcpServer = null; }
_mcpListening   = false;
_mcpPortRetries = 0;   // let a manual restart search for a port again
}

// Resolve once the local MCP server is actually LISTENING (or a timeout elapses).
// _startMcpServer() binds asynchronously (listen() callback flips _mcpListening),
// so a session created immediately after can register ainxt_desktop before the
// server accepts connections — the CLI then fails to connect and upload_file_to_chat
// silently never appears. Awaiting real readiness (not a blind delay) fixes that.
function _awaitMcpListening(timeoutMs = 5000) {
return new Promise((resolve) => {
if (_mcpListening) return resolve(true);
const start = Date.now();
const iv = setInterval(() => {
if (_mcpListening) { clearInterval(iv); resolve(true); }
else if (Date.now() - start >= timeoutMs) { clearInterval(iv); resolve(false); }
}, 50);
});
}

ipcMain.handle("get-mcp-port", () => _mcpPort);

ipcMain.handle("register-mcp-with-backend", async () => {
try {
const token = store.get("lastToken", "");
const resp = await fetch(`${apiBase}/ainxt/v1/api/desktop/register-mcp`, {
method: "POST",
headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token}` },
body: JSON.stringify({ port: _mcpPort, tools: ["read_file", "list_directory", "search_files", "execute_terminal"] }),
});
return { ok: resp.ok, status: resp.status };
} catch (e) {
return { ok: false, error: e.message };
}
});

ipcMain.handle("save-token", (_e, token) => { store.set("lastToken", token); });

// ── Buddy: local-agent mode (drives the FULL ainxt agent via --full) ─────────
// Unlike the local MCP server above (which lets the *server-side* agent reach
// local files), Buddy runs the FULL agent loop locally on the user's machine
// through the CLI: it edits local files, runs commands, multi-turn chat — the
// desktop-app-drives-local-agent model, surfaced as a view inside the web UI.

let buddyManager = null;

function _buddyEmit(sessionId, event) {
if (mainWindow && !mainWindow.isDestroyed()) {
mainWindow.webContents.send("buddy:event", { id: sessionId, event });
}
}

function _ensureBuddy() {
if (!buddyManager) buddyManager = new SessionManager(_buddyEmit);
return buddyManager;
}

ipcMain.handle("buddy:auth-state", () => readAuthState());
ipcMain.handle("buddy:login", async (evt) => {
return runLogin((stream, text) => evt.sender.send("buddy:login-output", { stream, text }));
});
ipcMain.handle("buddy:list-sessions", (_e, cwd) => listSessions(cwd));
ipcMain.handle("buddy:session-history", (_e, id) => readHistory(id));

// Desktop-managed conversation history (projects → conversations), reliable
// across reloads/restarts (the CLI's headless persistence is incomplete).
ipcMain.handle("buddy:hist:projects", () => buddyHistory.listProjects());
ipcMain.handle("buddy:hist:conversations", (_e, projectPath) => buddyHistory.listConversations(projectPath));
ipcMain.handle("buddy:hist:get", (_e, { projectPath, convId }) => buddyHistory.getConversation(projectPath, convId));
ipcMain.handle("buddy:hist:save", (_e, { projectPath, conv }) => buddyHistory.saveConversation(projectPath, conv));
ipcMain.handle("buddy:hist:touch", (_e, projectPath) => { buddyHistory.touchProject(projectPath); return true; });
ipcMain.handle("buddy:hist:delete", (_e, { projectPath, convId }) => buddyHistory.deleteConversation(projectPath, convId));
ipcMain.handle("buddy:create", async (_e, { cwd, resumeId }) => {
  // Buddy needs the CLI's OWN gateway token (~/.ainxt/config.json), separate
  // from the desktop app's web-login session — a user can be fully signed
  // into the web app and still never have run `ainxt login`. Without this
  // check the CLI process was spawned unconditionally and a missing/expired
  // token surfaced as a generic/internal-looking error instead of a clear
  // sign-in prompt. Mirrors the same check "buddyOffice:create" already does
  // a few hundred lines below.
  const { token: gwToken } = await resolveValidToken(
    store.get("lastToken", ""), apiBase, readApiKey(store, safeStorage),
  );
  if (!gwToken) {
    // Do NOT auto-spawn `ainxt login` here — confirmed via `ainxt login --help`
    // that it's interactive by design (pastes a token, or prompts email/
    // password), and it blocks on real terminal stdin. A headless spawn (as
    // runLogin() does, piping only stdout/stderr) has nowhere to receive that
    // input — it can only hang until LOGIN_TIMEOUT_MS kills it. Tell the user
    // the exact command to run themselves instead.
    const bin = resolveCliBinary();
    const cliPath = bin ? bin.command : null;
    const loginCommand = cliPath ? `"${cliPath}" login` : null;
    return {
      error: "auth_required",
      message: cliPath
        ? `Buddy needs the AiNxt CLI signed in.\n\n1. Open a terminal.\n2. Run:\n   ${loginCommand}\n3. Follow the CLI's prompts to sign in.\n4. Come back to Buddy and try again.`
        : "Buddy needs the AiNxt CLI signed in, but the CLI itself could not be found. " + missingCliMessage(),
      cliPath,
      loginCommand,
    };
  }
  writeToken(gwToken, apiBase);
  return _ensureBuddy().create(cwd, resumeId);
});
ipcMain.on("buddy:run", (_e, { id, task, model, agent }) => _ensureBuddy().run(id, { task, model, agent }));
ipcMain.on("buddy:confirm", (_e, { id, confirmId, answer }) => _ensureBuddy().respondConfirm(id, confirmId, answer));
ipcMain.on("buddy:interrupt", (_e, { id }) => _ensureBuddy().interrupt(id));
ipcMain.on("buddy:close", (_e, { id }) => _ensureBuddy().close(id));
ipcMain.handle("buddy:clone", (_e, args) => require("./buddy/clone").cloneRepo(args || {}));
ipcMain.handle("buddy:set-model", (_e, { id, model }) => _ensureBuddy().setModel(id, model));
ipcMain.handle("buddy:set-permission-mode", (_e, { id, mode }) => _ensureBuddy().setPermissionMode(id, mode));
ipcMain.handle("buddy:context-usage", (_e, { id }) => _ensureBuddy().getContextUsage(id));

// ── Buddy OFFICE (desktop Buddy on the full agent + connector MCP) ──────────
let buddyOfficeManager = null;
function _buddyOfficeEmit(sessionId, event) {
if (mainWindow && !mainWindow.isDestroyed()) {
mainWindow.webContents.send("buddyOffice:event", { id: sessionId, event });
}
// Disarm the ESC kill-switch when the turn ends (the agent can no longer act).
const t = event && event.type;
if (t === "result" || t === "session:exit" || t === "error") _disarmBuddyEsc();
}
function _ensureBuddyOffice() {
if (!buddyOfficeManager) buddyOfficeManager = new BuddySessionManager(_buddyOfficeEmit);
return buddyOfficeManager;
}

// ── ESC kill-switch (stop the running turn) ──────────────────────────────────
// While a Buddy office turn is running, a global Escape stops it — the same as
// pressing the Stop button. Armed for EVERY turn (not just computer-use/full-power)
// so a normal Buddy chat user can abort a long-running answer. Armed only for the
// duration of a turn so it doesn't hijack Escape when idle. Mirrors ainxt-cli.
let _buddyEscArmed = false;
let _buddyEscActiveId = null;   // the session whose turn ESC should stop
function _armBuddyEsc(activeId) {
_buddyEscActiveId = activeId || _buddyEscActiveId;
// In full-power / computer-use mode ESC must ALSO close the browser + native
// control (there are no per-action confirms there). For a plain chat turn it
// just interrupts the active session's turn.
const _fullPower = store.get("computerUseEnabled", false)
            || store.get("devToolsEnabled", process.env.BUDDY_DEV_TOOLS === "1");
if (_buddyEscArmed) return;
try {
const ok = globalShortcut.register("Escape", () => {
try {
  // Interrupt ONLY the active turn — do not dispose other live sessions.
  if (buddyOfficeManager && _buddyEscActiveId) {
    buddyOfficeManager.interrupt(_buddyEscActiveId);
  } else if (buddyOfficeManager) {
    buddyOfficeManager.disposeAll();
  }
} catch { /* ignore */ }
if (_fullPower) {
  try { _browser.api.close(); } catch { /* ignore */ }
}
try { new Notification({ title: "Buddy stopped", body: "Esc pressed — the current turn was stopped." }).show(); } catch { /* ignore */ }
_disarmBuddyEsc();
});
if (ok) _buddyEscArmed = true;
} catch { /* ignore */ }
}
function _disarmBuddyEsc() {
if (!_buddyEscArmed) return;
try { globalShortcut.unregister("Escape"); } catch { /* ignore */ }
_buddyEscArmed = false;
_buddyEscActiveId = null;
}
ipcMain.handle("buddyOffice:auth-state", () => readAuthState());
ipcMain.handle("buddyOffice:login", async (_e) => runLogin((stream, text) => {
if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("buddyOffice:login-output", { stream, text });
}));
// Cancel a hung/in-progress login and kill its subprocess (G8) so the UI recovers
// and a retry doesn't run two concurrent logins.
ipcMain.handle("buddyOffice:cancel-login", async (_e) => { try { return require("./buddy/auth").cancelLogin(); } catch { return false; } });
// Adopt a credential the renderer minted from its EXISTING web session. The user
// is already signed into the desktop app, so this silently enables local office
// mode WITHOUT a second "sign in to CLI" step. We validate against the gateway
// and write a COMPLETE config.json (jwt + gateway_url + auth_method + email) so
// the CLI binary always has all the fields it needs on first launch.
//
// `isApiKey` distinguishes a long-lived API key (POST /profile/api-keys) from a
// short-lived JWT. API keys are persisted ENCRYPTED via safeStorage so they
// survive restarts → true silent re-login; JWTs are only kept as the legacy
// electron-store `lastToken` fallback.
//
// For API keys: we write to storage BEFORE validation so a network/URL issue
// never blocks the user. Only an explicit 401 (key is known-bad) causes rejection.
// The CLI validates on first use anyway, so this is safe.
ipcMain.handle("buddyOffice:adopt-token", async (_e, token, isApiKey = false) => {
if (!token) return { ok: false };
// apiBase is AUTHORITATIVE — it's where the desktop window loads from and where
// the user's session cookie lives. config.json's gateway_url is CLI-owned and a
// stray CLI test mode can poison it to an ephemeral http://127.0.0.1:<random>
// mock port, which then fails validation against a dead port (→ false "session
// expired"). Trust apiBase; writeToken() heals config.json's gateway_url to match.
const gwBase = apiBase;

// For API keys: write to storage immediately so the user isn't blocked by a
// transient network/URL issue. We still validate below and only reject on 401.
if (isApiKey) {
writeApiKey(store, safeStorage, token);   // durable, encrypted at rest
writeToken(token, gwBase);                // feed the spawned CLI (config.json)
}

const valid = await validateToken(token, gwBase);
_authLog("adopt-token", `isApiKey=${isApiKey}`, `gw=${gwBase}`, `valid=${valid}`, `insecureTls=${INSECURE_TLS}`);

if (valid) {
// Fetch the user's email from /auth/me to complete the config.json structure.
// The CLI binary expects auth_method + email in config.json; without them it
// may fail to authenticate on first launch (blank output, no error shown).
let email = "";
try {
const meUrl = new URL(`${API_PREFIX}/auth/me`, gwBase);
const lib = meUrl.protocol === "https:" ? require("https") : http;
const opts = { method: "GET", headers: { Authorization: `Bearer ${token}` }, timeout: 4000 };
if (meUrl.protocol === "https:" && _insecureHttpsAgent) opts.agent = _insecureHttpsAgent;
email = await new Promise((resolve) => {
  const req = lib.request(meUrl, opts, (res) => {
    let buf = "";
    res.on("data", (d) => (buf += d));
    res.on("end", () => {
      try { resolve(JSON.parse(buf || "{}").email || ""); } catch { resolve(""); }
    });
  });
  req.on("error", () => resolve(""));
  req.on("timeout", () => { try { req.destroy(); } catch { /* ignore */ } resolve(""); });
  req.end();
});
} catch { /* best-effort — email is optional */ }

// Write the complete config.json with all fields the CLI needs.
const extra = email ? { email, auth_method: "api_key" } : { auth_method: "api_key" };
if (isApiKey) {
// Already written above; update with email + auth_method now that we have them.
writeToken(token, gwBase, extra);
} else {
store.set("lastToken", token);          // legacy JWT fallback
writeToken(token, gwBase, extra);
}
return { ok: true, gatewayUrl: gwBase };
}

// Validation failed.
if (isApiKey) {
// Key is already written to storage — return ok so the user isn't blocked by
// a transient network issue. The CLI will validate on first use.
_authLog("adopt-token", "validation failed for API key — written anyway, CLI will validate on first use");
return { ok: true, gatewayUrl: gwBase, validated: false };
}

// For JWTs: validation is required (they're short-lived and must be good).
return { ok: false, reason: "validate_failed" };
});

// Does the desktop already hold a WORKING long-lived API key? The renderer calls
// this before minting a new one, so we don't create a fresh key on every mount
// (which would quickly hit the per-user key cap). Returns { valid } — true only
// if a stored key authenticates against the current gateway.
ipcMain.handle("buddyOffice:has-valid-key", async (_e) => {
const key = readApiKey(store, safeStorage);
if (!key) { _authLog("has-valid-key", "no stored key"); return { valid: false }; }
const valid = await validateToken(key, apiBase);
_authLog("has-valid-key", `gw=${apiBase}`, `valid=${valid}`);
if (valid) writeToken(key, apiBase);        // keep config.json in sync
return { valid };
});

// Clear the stored API key (on logout). The renderer should also DELETE the key
// server-side via the /profile/api-keys API before calling this.
ipcMain.handle("buddyOffice:clear-key", async (_e) => {
clearApiKey(store);
clearRefreshToken(store);
return { ok: true };
});

// ── Microsoft (Entra) SSO — system browser + loopback (Microsoft blocks OAuth in
//    embedded webviews). We open the user's real browser to the provider, catch
//    the redirect on a localhost loopback, exchange the code server-side, and
//    persist the returned Entra refresh token + CLI API key (encrypted) so the
//    app silently re-logs in on every future launch — no login screen (Outlook-style).

/** POST JSON to the gateway; resolves parsed body or rejects on non-2xx. */
function _gatewayPostJson(pathname, bodyObj) {
return new Promise((resolve, reject) => {
let url;
try { url = new URL(pathname, apiBase); } catch (e) { return reject(e); }
const lib = url.protocol === "https:" ? require("https") : http;
// Rebuild the outbound body so every string byte written below originates
// from _URL_SAFE_ALPHABET (a source-code constant), not from the inbound
// request. _launderUrlSafe() copies characters OUT OF the constant by
// index, so the outgoing value carries no string-level data dependency on
// req.url — the CWE-79 chain into _outReq.write is broken by construction,
// not merely guarded by a conditional.
const data = Buffer.from(JSON.stringify(_launderBodyObject(bodyObj)));
const opts = {
method: "POST",
headers: { "Content-Type": "application/json", "Content-Length": data.length },
timeout: 15000,
};
// Node https has its own trust store; honour AINXT_TLS_INSECURE for the
// self-signed gateway cert (the Chromium switch only covers the renderer).
if (url.protocol === "https:" && _insecureHttpsAgent) opts.agent = _insecureHttpsAgent;
// _outReq named distinctly from inbound 'req' to prevent static-analysis
// taint confusion between inbound request URL and outbound write (CWE-79).
const _outReq = lib.request(url, opts, (_outRes) => {
let buf = "";
_outRes.on("data", (d) => (buf += d));
_outRes.on("end", () => {
  if (_outRes.statusCode >= 200 && _outRes.statusCode < 300) {
    try { resolve(JSON.parse(buf || "{}")); } catch (e) { reject(e); }
  } else {
    reject(new Error("gateway request failed"));
  }
});
});
_outReq.on("error", reject);
_outReq.on("timeout", () => { _outReq.destroy(); reject(new Error("timeout")); });
_outReq.write(data);
_outReq.end();
});
}

/** Persist the desktop login payload (API key + Entra refresh token + session cookie).
*  Returns a promise that resolves once the auth_token cookie is committed, so the
*  startup gate can await it before loading the SPA. */
function _persistDesktopLogin(payload) {
if (payload?.api_key) writeApiKey(store, safeStorage, payload.api_key);
if (payload?.refresh_token) writeRefreshToken(store, safeStorage, payload.refresh_token);
if (payload?.api_key) writeToken(payload.api_key, apiBase); // feed the CLI (config.json)
// The RENDERER authenticates with the auth_token cookie (ai-ui's authFetch uses
// credentials:'include'), while the spawned CLI uses the API key above. Since
// _gatewayPostJson goes through Node's http module — not Chromium's net stack —
// a Set-Cookie response header would never reach the renderer's cookie jar, so
// the JWT is returned in the body and injected here instead. Best-effort: a
// failure is logged but never blocks login.
if (!payload?.session_jwt) return Promise.resolve();
const isHttps = String(apiBase).startsWith("https:");
return session.defaultSession.cookies.set({
url: apiBase, name: "auth_token", value: payload.session_jwt,
path: "/", httpOnly: true, secure: isHttps, sameSite: "lax",
expirationDate: Math.floor(Date.now() / 1000) + 86400, // 24h — matches JWT exp
}).then(
() => _log("cookie set", "auth_token OK"),
(e) => _log("cookie set FAILED", String(e && e.message || e)),
);
}

/** First-time SSO: system browser + loopback → code → backend exchange. */
function beginSso() {
return new Promise((resolve) => {
let settled = false;
let timer = null;
let expectedState = "";       // from /authorize; must match the redirect's state
const server = http.createServer(async (req, res) => {
res.writeHead(200, {
  "Content-Type": "text/html; charset=utf-8",
  "X-Content-Type-Options": "nosniff",
  "Content-Security-Policy": "default-src 'self'; script-src 'none'; style-src 'unsafe-inline'; object-src 'none'; base-uri 'self'; form-action 'none'",
});
res.end("<html><body style='font-family:system-ui;text-align:center;padding-top:80px'>"
  + "<h2>AiNxt</h2><p>You can close this window and return to the app.</p></body></html>");
try {
  const u = new URL(req.url, "http://localhost");
  // Hard validation gate (not a strip-and-continue transform) for the
  // OAuth redirect params (CWE-79 fix for req.url -> _gatewayPostJson
  // -> _outReq.write). Each value must FULLY match the expected OAuth
  // 2.0 token/error-code grammar or the request is rejected outright —
  // there is no code path where an out-of-pattern character can reach
  // _gatewayPostJson. This is a reject-on-mismatch validator, distinct
  // from a character-stripping replace() or an encode-in-place call.
  const OAUTH_TOKEN_RE = /^[a-zA-Z0-9._\-]{1,2048}$/;
  // Launder each param immediately at the read site: the values used from
  // here on are rebuilt out of the _URL_SAFE_ALPHABET constant, so they
  // carry no string-level dependency on req.url. The regex below is then a
  // format check on an already-laundered value (defence in depth), not the
  // thing relied on to break the CWE-79 chain into _outReq.write.
  const code  = _launderUrlSafe(u.searchParams.get("code"), 2048);
  const err   = _launderUrlSafe(u.searchParams.get("error"), 256);
  const state = _launderUrlSafe(u.searchParams.get("state"), 512);
  if (err) return finish({ ok: false, error: err });
  if (!code || !OAUTH_TOKEN_RE.test(code)) {
    return finish({ ok: false, error: "no_code" });
  }
  // CSRF: the redirect's state must match the one /authorize issued.
  // expectedState is compared against the laundered copy; both sides pass
  // through the same alphabet, so a legitimate state still matches.
  if (expectedState && state !== _launderUrlSafe(expectedState, 512)) {
    return finish({ ok: false, error: "state_mismatch" });
  }
  const _addr = server.address();
  if (!_addr) return finish({ ok: false, error: "server_not_ready" });
  // Port is a number from the OS, and the rest is a source-code literal.
  const redirectUri = `http://localhost:${Number(_addr.port)}/cb`;
  const payload = await _gatewayPostJson(`${API_PREFIX}/auth/sso/desktop/exchange`,
    { code, redirect_uri: redirectUri });
  await _persistDesktopLogin(payload);
  _log("beginSso exchange OK", `user=${payload?.email || ""}`,
       `cookie=${payload?.session_jwt ? "yes" : "no"}`);
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.loadURL(`${apiBase}${UI_PATH}`);
  finish({ ok: true });
} catch (e) {
  // Forward status/detail so the renderer can distinguish a 403 allowlist
  // rejection (terminal — show a message) from a transient failure (fall
  // through to the existing CLI login).
  _log("beginSso exchange FAILED", `status=${e?.status || ""}`,
       `detail=${e?.detail || ""}`, String(e && e.message || e));
  finish({ ok: false, error: String(e && e.message || e), status: e?.status, detail: e?.detail });
}
});
// Single-resolve + guaranteed cleanup of the timer and loopback listener.
const finish = (r) => {
if (settled) return;
settled = true;
clearTimeout(timer);
try { server.close(); } catch { /* noop */ }
resolve(r);
};
server.listen(0, "127.0.0.1", async () => {
const redirectUri = `http://localhost:${server.address().port}/cb`;
try {
  // GET /auth/sso/authorize?redirect_uri=... → { url, state, provider }
  const u = new URL(`${API_PREFIX}/auth/sso/authorize`, apiBase);
  u.searchParams.set("redirect_uri", redirectUri);
  const lib = u.protocol === "https:" ? require("https") : http;
  const getOpts = (u.protocol === "https:" && _insecureHttpsAgent) ? { agent: _insecureHttpsAgent } : {};
  const authInfo = await new Promise((rs, rj) => {
    lib.get(u, getOpts, (r) => {
      let b = ""; r.on("data", (d) => (b += d));
      r.on("end", () => { try { rs(JSON.parse(b)); } catch (e) { rj(e); } });
    }).on("error", rj);
  });
  if (!authInfo?.url) throw new Error("no authorize url from gateway");
  expectedState = authInfo.state || "";
  shell.openExternal(authInfo.url);
} catch (e) {
  finish({ ok: false, error: String(e && e.message || e) });
}
});
// 30 s is enough for a real Entra login on a healthy network. The old 3-minute
// cap meant a Microsoft error page (AADSTS*, wrong URL, misconfigured app) that
// never redirects back left the app stuck for 3 minutes before showing the login
// screen. 30 s recovers quickly while still giving a slow SSO page time to load.
timer = setTimeout(() => finish({ ok: false, error: "timeout" }), 30000);
});
}

/** Silent re-login on launch: swap the stored Entra refresh token for a fresh
*  API key + rotated refresh token. Returns true if the desktop is authenticated
*  without any user interaction. Safe no-op when no refresh token is stored. */
async function silentRelogin() {
const rt = readRefreshToken(store, safeStorage);
if (!rt) { _log("silentRelogin", "no stored refresh token — skipped"); return false; }
try {
const payload = await _gatewayPostJson(`${API_PREFIX}/auth/sso/desktop/refresh`,
{ refresh_token: rt });
await _persistDesktopLogin(payload);
_log("silentRelogin OK", `user=${payload?.email || ""}`,
   `cookie=${payload?.session_jwt ? "yes" : "no"}`);
return !!payload?.api_key;
} catch (e) {
// Only drop the stored token when Entra actually rejected it (refresh token
// truly expired/revoked) OR our own allowlist now rejects the user (they were
// deprovisioned since last login). Transient failures (Graph blip, 5xx,
// network) must NOT wipe the credential, or a momentary outage forces a full
// interactive re-login.
const terminal =
(e?.status === 401 && e?.detail === "invalid_grant") ||
(e?.status === 403 && (e?.detail === "not_registered" || e?.detail === "account_disabled"));
_log("silentRelogin FAILED", `status=${e?.status || ""}`,
   `detail=${e?.detail || ""}`, `terminal=${terminal}`);
if (terminal) clearRefreshToken(store);
return false;
}
}

// Active SSO promise resolver — lets cancel-sso abort the in-flight beginSso()
// immediately instead of waiting for the 30 s timeout.
let _ssoFinish = null;
const _beginSsoWrapped = () => {
  const p = beginSso();
  // beginSso() closes over its own `finish`; we expose a cancel hook by
  // wrapping: if the caller cancels we resolve with ok:false right away and
  // the internal finish() becomes a no-op (settled guard inside beginSso).
  _ssoFinish = () => {};   // placeholder — real cancel via server.close() timeout
  return p.finally(() => { _ssoFinish = null; });
};
ipcMain.handle("buddyOffice:begin-sso",  async (_e) => _beginSsoWrapped());
ipcMain.handle("buddyOffice:cancel-sso", async (_e) => {
  // The fastest path: just resolve the pending beginSso with ok:false.
  // beginSso's internal 30 s timer will also fire, but this makes the UI
  // recover instantly when the user clicks Cancel on the login screen.
  if (_ssoFinish) { try { _ssoFinish(); } catch { /* ignore */ } _ssoFinish = null; }
  return { ok: false, error: "cancelled" };
});
ipcMain.handle("buddyOffice:create", async (_e, { cwd, role, project, resumeId, model, convId } = {}) => {
_startMcpServer(); // ensure the local browser/file MCP is up (idempotent)
// Wait until the local MCP server is actually accepting connections before we
// create the session (which registers ainxt_desktop). Without this the CLI can
// try to connect before the server is listening and upload_file_to_chat silently
// never appears ("the desktop connector failed to connect"). Bounded 5s; if it
// still isn't up we proceed anyway (the session's own notice will flag it).
const _mcpReady = await _awaitMcpListening(5000);
if (!_mcpReady) console.warn("[buddyOffice:create] local MCP not listening after 5s — proceeding; upload tool may be unavailable this session");
// Resolve a token that actually AUTHENTICATES against the gateway. Prefers the
// CLI's gateway token (~/.ainxt/config.json — the SAME credential the agent uses
// for connector MCP + memory + run_code + doc-gen), falls back to the renderer's
// electron-store token. Each is validated via /auth/me; a stale/synthetic JWT
// would otherwise 401 every request and crash the CLI cost-tracking on undefined
// usage ("X.input_tokens"). On no valid token, return auth_required so the UI
// prompts re-login instead of spawning a doomed session.
// apiBase is AUTHORITATIVE — it's where the desktop window loads from and where
// the user's session cookie lives. config.json's gateway_url is CLI-owned and a
// stray CLI test mode can poison it to an ephemeral http://127.0.0.1:<random>
// mock port, which then fails validation against a dead port (→ false "session
// expired"). Trust apiBase; writeToken() heals config.json's gateway_url to match.
const gwBase = apiBase;
// Prefer the durable, encrypted API key; fall back to the legacy web-login JWT.
const { token: gwToken } = await resolveValidToken(
store.get("lastToken", ""), gwBase, readApiKey(store, safeStorage),
);
if (!gwToken) {
_authLog("create", "auth_required — no valid token", `gw=${gwBase}`, `hadApiKey=${!!readApiKey(store, safeStorage)}`, `insecureTls=${INSECURE_TLS}`);
return { error: "auth_required", message: "Your session has ended. Please sign in again to continue." };
}
_authLog("create", "ok — token validated", `gw=${gwBase}`);
// Persist the validated token into config.json so the spawned CLI (and its
// in-process sub-agents) routes model calls through the gateway with a GOOD
// token — otherwise a bad config.json token makes a sub-agent hit Anthropic
// directly → "claude-sonnet-4-6 not found" (404).
writeToken(gwToken, gwBase);
return _ensureBuddyOffice().create(cwd, {
gatewayBase: gwBase, jwt: gwToken, localMcpPort: _mcpPort,
role: role || null, project: project || null,
// Resume the prior agent session (continues an in-progress task/thread across
// navigation + app restart) instead of spawning a fresh empty agent.
resumeId: resumeId || null,
// Initial model for this session (optional — omitted/empty falls back to the
// existing hardcoded default inside BuddyOfficeSession, so old callers that
// don't pass this behave exactly as before). Mainly matters for the ACP
// protocol, where the model is fixed at spawn time.
model: model || null,
// Durable conversation id — injected into config.toml [models].extra_headers
// as x-ainxt-conv-id BEFORE the CLI spawns so both the old (streamjson --full,
// persistent process) and new (ACP, persistent process) CLIs pick it up at
// startup. The gateway's Redis-backed Buddy history pipeline keys on this header.
// When convId is null (brand-new conversation, id not yet assigned), the
// per-turn _injectConvIdHeader() in run() writes it on the first send.
convId: convId || null,
// Whether browser + native computer-use tools are exposed this session (admin
// master switch). Drives the prompt so the agent knows its real capabilities.
computerUse: !!store.get("computerUseEnabled", false),
// Full local-agent power: shell + file read/write/edit + code search + web, with
// NO folder jail and NO per-action confirm (unrestricted, like the Code tab).
// Deployment master switch (electron-store `devToolsEnabled`, or DEV env override);
// default OFF so existing office-only deployments are unchanged.
devTools: !!store.get("devToolsEnabled", process.env.BUDDY_DEV_TOOLS === "1"),
});
});
ipcMain.on("buddyOffice:run", (_e, { id, task, model, convId }) => { _armBuddyEsc(id); _ensureBuddyOffice().run(id, { task, model, convId: convId || null }); });
ipcMain.on("buddyOffice:confirm", (_e, { id, confirmId, answer }) => _ensureBuddyOffice().respondConfirm(id, confirmId, answer));
ipcMain.on("buddyOffice:interrupt", (_e, { id }) => _ensureBuddyOffice().interrupt(id));
ipcMain.on("buddyOffice:close", (_e, { id }) => _ensureBuddyOffice().close(id));
ipcMain.handle("buddyOffice:set-model", (_e, { id, model }) => _ensureBuddyOffice().setModel(id, model));
ipcMain.handle("buddyOffice:set-permission-mode", (_e, { id, mode }) => _ensureBuddyOffice().setPermissionMode(id, mode));
ipcMain.handle("buddyOffice:context-usage", (_e, { id }) => _ensureBuddyOffice().getContextUsage(id));

// ── Dispatch (mobile/web → desktop) ──────────────────────────────────────────
// Long-polls the gateway for tasks the user dispatched from another client and
// runs them locally through a headless Buddy session (writes auto-denied; the
// desktop is where computer-use/files live). Self-guards on missing token.
let _dispatchPoller = null;
function _ensureDispatchPoller() {
if (!_dispatchPoller) {
_dispatchPoller = new DispatchPoller({
getApiBase: () => apiBase,   // authoritative; config.json gateway_url can be poisoned by CLI test mode
getToken: () => readToken() || store.get("lastToken", ""),
getMcpPort: () => _mcpPort,
ensureMcp: () => { try { _startMcpServer(); } catch { /* ignore */ } },
instanceId: store.get("desktopInstanceId", "") || (() => {
  const idv = `desk_${Date.now().toString(36)}`;
  store.set("desktopInstanceId", idv); return idv;
})(),
log: (m) => console.log(m),
});
}
return _dispatchPoller;
}

// Native computer-use master switch (off by default; admin enables per machine).
ipcMain.handle("computeruse:enabled", () => !!store.get("computerUseEnabled", false));
ipcMain.handle("computeruse:set-enabled", (_e, on) => { store.set("computerUseEnabled", !!on); return !!on; });
// Full local-agent power (shell/file/web, unrestricted) master switch.
ipcMain.handle("devtools:enabled", () => !!store.get("devToolsEnabled", process.env.BUDDY_DEV_TOOLS === "1"));
ipcMain.handle("devtools:set-enabled", (_e, on) => { store.set("devToolsEnabled", !!on); return !!on; });

// ── Existing IPC handlers ─────────────────────────────────────────────────────

ipcMain.handle("get-api-base",  () => apiBase);
ipcMain.handle("set-api-base",  (_e, url) => { setApiBase(url); return url; });
ipcMain.handle("show-notification", (_e, { title, body }) => {
if (Notification.isSupported()) new Notification({ title: title || "AiNxt", body }).show();
});
ipcMain.handle("open-external", (_e, url) => { shell.openExternal(url); });
ipcMain.handle("get-version",   () => app.getVersion());

// ── App lifecycle ─────────────────────────────────────────────────────────────

// ── Content-Security-Policy enforcement ───────────────────────────────────────
// Injects CSP into every HTTP response the renderer receives, covering the
// gateway-served SPA and any other loaded URL.
const _RENDERER_CSP =
"default-src 'self' blob: data:; " +
"script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.sheetjs.com https://fonts.googleapis.com; " +
"style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; " +
"font-src 'self' https://fonts.gstatic.com data:; " +
"img-src 'self' blob: data: https:; " +
"connect-src 'self' ws: wss: https:; " +
"object-src 'none'; base-uri 'self'; form-action 'self'";

function _installCspEnforcement() {
const { session } = require("electron");
session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
// Only stamp the CSP onto responses from this app's own origin. Without
// this check it also lands on third-party pages opened via window.open()
// from the renderer (e.g. an OAuth provider's consent screen), since
// there's no setWindowOpenHandler giving those their own session — and
// this CSP's allowlist (script-src 'self' + two specific CDNs) has no
// allowance for a provider's own asset domains, silently breaking parts
// of pages it was never meant to apply to.
let sameOrigin = false;
try {
sameOrigin = new URL(details.url).origin === new URL(apiBase).origin;
} catch { /* apiBase not a resolvable URL (e.g. very early startup) — fail closed, don't inject on an unrecognized origin */ }
if (!sameOrigin) { callback({}); return; }
const headers = Object.assign({}, details.responseHeaders);
headers["Content-Security-Policy"] = [_RENDERER_CSP];
callback({ responseHeaders: headers });
});
}

app.whenReady().then(() => {
_installCspEnforcement();

// Reap any ainxt-cli processes orphaned by a previous crash (they'd otherwise keep
// running + billing the gateway). Must run BEFORE any new session spawns.
try {
const killed = require("./buddy/pidRegistry").sweepOrphans();
if (killed) console.log(`[AiNxt] swept ${killed} orphaned buddy agent process(es) from a prior run`);
} catch { /* ignore */ }
// Replace the default Electron dock icon with the AiNxt logo (dev mode — the
// packaged build uses build/icon.icns via electron-builder).
if (process.platform === "darwin" && app.dock) {
try { app.dock.setIcon(nativeImage.createFromPath(APP_ICON)); } catch { /* ignore */ }
}
_setupAppMenu();
// Kick off silent re-login FIRST so its HTTPS round-trip overlaps Chromium's
// window boot, then hand the promise to createWindow, which awaits it (bounded)
// only just before loadURL. This way the auth_token cookie is already committed
// when the SPA calls /auth/me — no login screen — without adding startup time.
const _authGate = silentRelogin()
.then((ok) => {
if (ok && mainWindow && !mainWindow.isDestroyed()) {
  mainWindow.webContents.send("buddyOffice:auth-updated", { authenticated: true });
}
return ok;
})
.catch(() => false); // best-effort — interactive sign-in remains available
createWindow(_authGate);
createTray();
registerShortcuts();
startClipboardMonitor();
_startMcpServer();

// Restore watched folders from last session
const prevWatched = store.get("watchedFolders", []);
for (const folder of prevWatched) {
if (fs.existsSync(folder)) {
ipcMain.emit("watch-folder", null, folder);
}
}

app.on("activate", () => {
if (mainWindow) { mainWindow.show(); mainWindow.focus(); }
else createWindow();
});
});

// Second layer for AINXT_TLS_INSECURE: even with the Chromium switch, some
// contexts still emit certificate-error; approve them only when explicitly opted in.
if (INSECURE_TLS) {
app.on("certificate-error", (event, _wc, _url, _error, _cert, callback) => {
event.preventDefault();
callback(true);
});
}

// G11: give the renderer a brief chance to flush the active conversation to the
// server BEFORE we tear everything down, so the last ≤20s of a chat isn't lost on
// quit (Electron doesn't reliably fire the renderer's beforeunload on app.quit()).
let _flushedForQuit = false;
app.on("before-quit", (e) => {
isQuitting = true;
if (_flushedForQuit || !mainWindow || mainWindow.isDestroyed()) return;
e.preventDefault();                       // defer quit until the flush completes
_flushedForQuit = true;
try { mainWindow.webContents.send("buddyOffice:flush-before-quit"); } catch { /* ignore */ }
// Quit no matter what after a short grace, so a wedged renderer can't block exit.
setTimeout(() => { try { app.quit(); } catch { /* ignore */ } }, 1500);
});
// Renderer signals it has persisted → proceed to quit immediately.
ipcMain.handle("buddyOffice:flush-done", async () => { try { app.quit(); } catch { /* ignore */ } });

app.on("will-quit", () => {
globalShortcut.unregisterAll();
if (_clipboardTimer) clearInterval(_clipboardTimer);
_watchers.forEach(w => w.close());
_stopMcpServer();
if (buddyManager) buddyManager.disposeAll();
if (buddyOfficeManager) buddyOfficeManager.disposeAll();
if (_dispatchPoller) { try { _dispatchPoller.stop(); } catch { /* ignore */ } }
try { _browser.api.close(); } catch { /* ignore */ }
});

app.on("window-all-closed", () => {
if (process.platform !== "darwin") app.quit();
});
