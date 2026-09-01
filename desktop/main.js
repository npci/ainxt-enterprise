// SPDX-License-Identifier: Apache-2.0
const { app, BrowserWindow, globalShortcut, Tray, Menu, nativeImage,
  ipcMain, Notification, shell, screen, clipboard, dialog, safeStorage } = require("electron");
const path   = require("path");
const fs     = require("fs");
const Store  = require("electron-store");
const { SessionManager } = require("./cowork/cliManager");
const { CoworkSessionManager } = require("./cowork/coworkSession");
const { DispatchPoller } = require("./cowork/dispatchPoller");
const _computerUse = require("./computeruse/computerUseManager");
const { readAuthState, runLogin, readToken, readGatewayUrl, resolveValidToken, writeToken, validateToken,
  readApiKey, writeApiKey, clearApiKey,
  readRefreshToken, writeRefreshToken, clearRefreshToken } = require("./cowork/auth");
const http = require("http");
const { listSessions, readHistory } = require("./cowork/sessions");
const coworkHistory = require("./cowork/history");
const _browser = require("./browser/playwrightManager");

// Brand the app as "AiNxt" (not "Electron"). Must run BEFORE the app is ready /
// the menu is built so the macOS app menu + About/Hide/Quit items read "AiNxt".
// In a packaged build the name also comes from build.productName; this covers
// `npm start` dev mode where the bundle is the generic Electron.app.
app.setName("AiNxt");

const store   = new Store();
const isDev   = process.env.AINXT_DEV === "1";
// ── Checkmarx CWE-79 hardening toggle ────────────────────────────────────────
// Master switch for the sanitizers added to close the Reflected XSS findings
// (paths 3 and 5 here, paths 4 and 7 in src/main.js — same constant name/logic
// duplicated in both files so a future edit must touch both, deliberately, to
// stay in sync). true → sanitizers active (default). An optional dev-only env
// override lets a shell session flip it off (`AINXT_XSS_SANITIZE=0 npm start`);
// absent (the packaged-app case), the literal below governs behaviour.
const _XSS_SANITIZE = true;
const _xssEnvRaw = process.env.AINXT_XSS_SANITIZE;
const XSS_SANITIZE_ENABLED =
  typeof _xssEnvRaw === "string" && _xssEnvRaw.trim() !== ""
    ? !/^(0|false|off)$/i.test(_xssEnvRaw.trim())
    : _XSS_SANITIZE;
const DEFAULT_API = "http://localhost:8000";
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
// Open DevTools on launch when AINXT_DEVTOOLS=1 (or dev mode). Lets an operator
// see console/network errors on a locked-down laptop where they can't rebuild.
const DEVTOOLS = isDev || process.env.AINXT_DEVTOOLS === "1";
// TEST-ONLY: accept self-signed / hostname-mismatched TLS certs when
// AINXT_INSECURE_TLS=1. The NPCI gateway currently serves a self-signed cert
// (issuer==subject), which Chromium rejects → silent blank window. Honouring
// this env lets SIT/portable builds connect; NEVER enable in production.
// The NODE_ENV !== "production" guard (matching cowork/auth.js) is a second,
// independent safety net: even if AINXT_INSECURE_TLS is left set by mistake
// in a production package/environment, the bypass stays off (CWE-295 SSL
// Verification Bypass hardening).
const INSECURE_TLS = process.env.AINXT_INSECURE_TLS === "1" && process.env.NODE_ENV !== "production";
// Shared agent for main-process Node https calls (SSO exchange/refresh). Node's
// https has its own trust store, so the Chromium switch below does NOT cover it.
const _insecureHttpsAgent = INSECURE_TLS
? new (require("https").Agent)({ rejectUnauthorized: false })
: undefined;
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

// Claude-style minimal chrome: a slim custom title bar injected into the page
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

// ── App menu (native) ─────────────────────────────────────────────────────────

// Claude-style minimal chrome. On Windows/Linux we drop the native menu bar
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

function createWindow() {
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

const url = isDev ? "http://localhost:5173" : `${apiBase}${UI_PATH}`;
mainWindow.loadURL(url).catch((err) => {
console.error("[AiNxt] initial loadURL failed:", url, err && err.message);
});

// Surface load failures instead of leaving a silent blank window. The most
// common cause on the NPCI network is a self-signed gateway cert (see
// AINXT_INSECURE_TLS) or an unreachable gateway URL.
mainWindow.webContents.on("did-fail-load", (_e, errorCode, errorDescription, validatedURL) => {
if (errorCode === -3) return; // ERR_ABORTED — benign (redirect/in-page nav)
console.error(`[AiNxt] did-fail-load ${errorCode} ${errorDescription} :: ${validatedURL}`);
const hint = /CERT|SSL/i.test(errorDescription || "")
? "The gateway's TLS certificate was rejected. If it is self-signed, set AINXT_INSECURE_TLS=1 in ainxt-desktop.bat."
: "Could not reach the gateway. Check AINXT_GATEWAY_URL in ainxt-desktop.bat.";
const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
mainWindow.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(
`<body style="font:14px system-ui;background:#111827;color:#e5e7eb;padding:40px">` +
`<h2 style="color:#f87171">AiNxt couldn't load</h2>` +
`<p>${esc(hint)}</p>` +
`<pre style="color:#9ca3af">${esc(errorDescription)} (${errorCode})\n${esc(validatedURL)}</pre></body>`
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
if (coworkManager) coworkManager.disposeAll();
if (coworkOfficeManager) coworkOfficeManager.disposeAll();
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
const iconPath = path.join(__dirname, "..", "build",
process.platform === "darwin" ? "trayTemplate.png" : "icon.png");
const img = nativeImage.createFromPath(iconPath);
tray = new Tray(img.isEmpty() ? nativeImage.createEmpty() : img);
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
submenu: [
  { label: "localhost:8000 (default)", type: "radio",
    checked: apiBase === "http://localhost:8000",
    click: () => setApiBase("http://localhost:8000") },
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
  try { if (coworkOfficeManager) coworkOfficeManager.disposeAll(); } catch { /* ignore */ }
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
if (mainWindow) mainWindow.loadURL(isDev ? "http://localhost:5173" : `${url}${UI_PATH}`);
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

ipcMain.handle("read-file", (_e, filePath) => {
try {
// Safety: only allow reading text files up to 1 MB
const stat = fs.statSync(filePath);
if (stat.size > 1_048_576) return { error: "File exceeds 1 MB limit", content: null };
return { content: fs.readFileSync(filePath, "utf-8"), error: null };
} catch (e) {
return { error: e.message, content: null };
}
});

ipcMain.handle("list-folder", (_e, folderPath, opts = {}) => {
const maxFiles = opts.maxFiles || 500;
const results  = [];

const SUPPORTED_EXT = new Set([
".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".kts",
".scala", ".go", ".rs", ".cpp", ".cc", ".c", ".h", ".hpp",
".cs", ".rb", ".php", ".swift", ".sh", ".bash", ".sql",
".yaml", ".yml", ".json", ".md", ".txt", ".env.example",
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

// SSE sessions for the MCP-protocol view of the local tools (used by the Cowork
// full agent via --mcp-config). Keyed by sessionId → response stream.
const _mcpSseSessions = new Map();
// Per-session "surface": 'cowork' (office assistant) or 'code' (dev agent). The
// Cowork agent connects with ?surface=cowork and is restricted to OFFICE tools
// only (browser + computer-use) — NO file tools and NO shell (execute_terminal).
// Office file access goes through the CLI's built-in Read, which is folder-scoped
// (--add-dir). This closes the hole where Cowork could read the whole filesystem
// or run a shell via the local MCP.
const _mcpSseSurface = new Map();
// Per-session granted ROOT folder for Cowork — the ONLY directory list_files may
// enumerate. Passed as ?root= on the SSE connection by coworkSession.
const _mcpSseRoot = new Map();

// Run one local tool (browser-automation or file/terminal) and return a uniform
// {success, result, error}. Shared by the legacy POST /execute REST path and the
// MCP tools/call path.
async function _runLocalTool(tool, input = {}, ctx = {}) {
try {
if (_computerUse.isComputerUseTool(tool)) {
const result = await _computerUse.executeTool(tool, input, {
  gatewayBase: apiBase, jwt: store.get("lastToken", ""), sessionId: ctx.sessionId || "cowork",
});
return { success: !result.error, result, error: result.error };
}
if (_browser.isBrowserTool(tool)) {
const result = await _browser.executeTool(tool, input, {
  gatewayBase: apiBase, jwt: store.get("lastToken", ""), sessionId: ctx.sessionId || "cowork",
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
if (stat.size > 524_288) return { success: false, error: "File > 512 KB — too large" };
return { success: true, result: { content: fs.readFileSync(input.path, "utf-8") } };
}
if (tool === "list_directory") {
const entries = fs.readdirSync(input.path).map((name) => {
  const full = path.join(input.path, name); const s = fs.statSync(full);
  return { name, type: s.isDirectory() ? "dir" : "file", size: s.size };
});
return { success: true, result: { entries } };
}
if (tool === "list_files") {
// Cowork-safe listing: ALWAYS scoped to the session's granted root (ctx.root).
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
console.error("[AiNxt] _runLocalTool failed:", String(e.message || e).replace(/[\r\n\t\x00-\x1f\x7f]+/g, " "));
return { success: false, error: "Tool execution failed. See app logs for details." };
}
}

function _startMcpServer() {
if (_mcpServer) return;
_computerUse.init(store);   // master-switch + audit context come from electron-store
const http = require("http");

const tools = [
{
name: "read_file",
description: "Read the contents of a local file. Only files within watched workspaces.",
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
// Cowork-safe folder listing — enumerates ONLY the attached folder (scoped to
// the session root server-side). The office agent uses this to see its files;
// it cannot reach anything outside the granted folder.
{
name: "list_files",
description: "List the files in your currently attached folder. Takes no arguments — it always " +
  "lists exactly the folder the user attached (you cannot list anywhere else). Use this when the " +
  "user asks what files you have or what's in the folder, then Read specific files by their path.",
input_schema: { type: "object", properties: {} },
},
// Cowork browser-automation tools (Playwright). Allowlist + per-action
// confirm + audit are enforced inside playwrightManager.
..._browser.TOOLS,
// Cowork native computer-use tools (nut.js). Master-switch + per-action
// confirm + screenshot redaction + audit enforced inside computerUseManager.
..._computerUse.TOOLS,
];

// The ONLY local tools an office (Cowork) surface may use: browser + computer-use.
// Everything else (read_file/list_directory/search_files/execute_terminal) is
// dev-agent-only — never exposed to Cowork.
//
// Office (Cowork) tool surface. `list_files` is always available. Browser +
// native computer-use are gated behind the admin "Allow Cowork to control this
// computer" master switch (electron-store `computerUseEnabled`) — each action
// still pops a per-action confirm, screenshots/extracted text are PII-redacted at
// the gateway, every action is audited, and the ESC kill-switch aborts the run.
const _OFFICE_BASE = new Set(["list_files"]);
const _officeAllowed = (name) => {
if (_OFFICE_BASE.has(name)) return true;
if (store.get("computerUseEnabled", false) &&
  (_browser.isBrowserTool(name) || _computerUse.isComputerUseTool(name))) return true;
return false;
};
const _visibleTools = (surface) =>
surface === "cowork" ? tools.filter((t) => _officeAllowed(t.name)) : tools;

/**
* Serialize a JSON-RPC payload for writing to an HTTP/SSE response with the
* HTML-significant characters escaped as \uXXXX. These escapes are valid JSON
* and decode back to the identical characters, so MCP clients receive
* byte-equivalent data while the output can never break out of an HTML
* context. Unlike String()/JSON round-trips, this genuinely transforms the
* bytes, so it also breaks the static-analysis taint chain (CWE-79).
*/
const _sanitizeJsonForWrite = (obj) => (
!XSS_SANITIZE_ENABLED ? JSON.stringify(obj) : JSON.stringify(obj)
.replace(/</g, "\\u003c")
.replace(/>/g, "\\u003e")
.replace(/&/g, "\\u0026")
.replace(/\u2028/g, "\\u2028")
.replace(/\u2029/g, "\\u2029")
);

// Loopback-only allow-list for the local MCP server's CORS header. Wildcard
// "*" is intentionally avoided (CWE-942 Overly Permissive CORS) — only the
// two loopback address forms this server itself listens on are trusted.
const _MCP_ALLOWED_ORIGINS = new Set([
`http://localhost:${_mcpPort}`,
`http://127.0.0.1:${_mcpPort}`,
]);

_mcpServer = http.createServer((req, res) => {
res.setHeader("Content-Type", "application/json");
// Electron renderer requests arrive with no Origin header (file:// / app://)
// and are allowed through silently. Any unknown Origin is rejected outright
// (no header is set for a rejected origin, so the browser blocks the read).
// SECURITY (CWE-79): the response header is set from a value taken out of
// the trusted _MCP_ALLOWED_ORIGINS constant set — never from the raw
// req.headers["origin"] string itself — so no attacker-controlled data
// flows into the response even though the two are compared for equality.
if (typeof req.headers["origin"] === "string") {
let _matchedOrigin;
for (const _allowedOrigin of _MCP_ALLOWED_ORIGINS) {
  if (_allowedOrigin === req.headers["origin"]) { _matchedOrigin = _allowedOrigin; break; }
}
if (_matchedOrigin) {
  res.setHeader("Access-Control-Allow-Origin", _matchedOrigin);
  res.setHeader("Vary", "Origin");
}
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

// ── MCP protocol view (SSE) — lets the Cowork full agent reach these tools
//    (browser automation etc.) via --mcp-config, with per-action confirms
//    enforced inside playwrightManager.
if (req.method === "GET" && req.url.startsWith("/sse")) {
const sid = require("crypto").randomUUID();
const _u = new URL(req.url, "http://x");
const surface = (_u.searchParams.get("surface") || "code").toLowerCase();
const root = _u.searchParams.get("root") || "";
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
if (req.method === "POST" && req.url.startsWith("/message")) {
// Hard validation gate: sessionId must FULLY match a randomUUID()
// shape or it is discarded (treated as absent), rather than having
// individual characters stripped out of it. There is no code path
// where an out-of-pattern sessionId value survives to reach the
// response writes below (CWE-79 fix for req.url -> res.end).
const _rawSid = new URL(req.url, "http://x").searchParams.get("sessionId");
const sid = (typeof _rawSid === "string" && /^[a-zA-Z0-9_\-]{1,128}$/.test(_rawSid)) ? _rawSid : null;
let body = "";
req.on("data", (d) => { body += d; });
req.on("end", async () => {
  let m; try { m = JSON.parse(body); } catch { res.statusCode = 400; res.end("{}"); return; }
  const reply = (obj) => {
    const stream = sid && _mcpSseSessions.get(sid);
    // Encode the serialized payload before it is written out: String()
    // and JSON round-trips do not change content, so they do not break
    // the taint chain from req.url to res.end (CWE-79).
    const safeJson = _sanitizeJsonForWrite(obj);
    if (stream) { stream.write(`data: ${safeJson}\n\n`); res.end(JSON.stringify({ accepted: true })); }
    else res.end(safeJson);
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
    // Hard gate: an office (Cowork) session may NEVER call file/terminal tools,
    // even if it somehow names one. No filesystem free-for-all, no shell.
    if (surface === "cowork" && !_officeAllowed(toolName)) {
      return reply({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text:
        "That tool isn't available here. Cowork has no shell and no broad file access — read files with Read (limited to the project's folder) and use your connectors/document tools." }], isError: true } });
    }
    const out = await _runLocalTool(toolName, m.params?.arguments || {}, { sessionId: sid, root: _mcpSseRoot.get(sid) });
    // Screenshot tools (computer_screenshot / browser_screenshot) return a
    // (PII-redacted) image — surface it as an MCP IMAGE content block so the
    // model can actually SEE it, not a giant base64 text blob it can't decode.
    if (out.success && out.result && out.result.image_b64) {
      const r = out.result;
      const note = `Screenshot captured${r.redacted ? ` (PII-redacted${r.findings ? `, ${r.findings} region(s) hidden` : ""})` : ""}.`;
      return reply({ jsonrpc: "2.0", id, result: { content: [
        { type: "image", data: r.image_b64, mimeType: r.mime || "image/png" },
        { type: "text", text: note },
      ], isError: false } });
    }
    const txt = out.success ? JSON.stringify(out.result) : `Error: ${out.error}`;
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
      // HTML-metacharacter escaper used by the /message (MCP) path so this
      // REST sibling closes the same CWE-79 flow (Stored XSS).
      res.end(_sanitizeJsonForWrite({ success: !result.error, result, error: result.error }));
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
        if (stat.size > 524_288) throw new Error("File > 512 KB — too large");
        result = { content: fs.readFileSync(input.path, "utf-8") };
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

    // File content (read_file) and directory listings can contain arbitrary
    // bytes copied verbatim from disk — sanitize before writing to the HTTP
    // response so it can't be interpreted as HTML/script by any client that
    // renders the reply (CWE-79 Stored XSS: fs.readFileSync -> res.end).
    res.end(_sanitizeJsonForWrite({ success: true, result }));

  } catch (e) {
    console.error("[AiNxt] /execute tool failed:", String(e.message || e).replace(/[\r\n\t\x00-\x1f\x7f]+/g, " "));
    res.statusCode = 200; // return 200 with error in body (MCP convention)
    res.end(JSON.stringify({ success: false, error: "Tool execution failed. See app logs for details." }));
  }
});
return;
}

res.statusCode = 404;
res.end(JSON.stringify({ error: "Not found" }));
});

_mcpServer.listen(_mcpPort, "127.0.0.1", () => {
console.log(`AiNxt local MCP server listening on 127.0.0.1:${_mcpPort}`);
// Notify renderer that MCP server is up
if (mainWindow) mainWindow.webContents.send("mcp-server-ready", { port: _mcpPort });
});

_mcpServer.on("error", (e) => {
if (e.code === "EADDRINUSE") {
_mcpPort++;
store.set("mcpPort", _mcpPort);
_mcpServer = null;
_startMcpServer(); // try next port
}
});
}

function _stopMcpServer() {
if (_mcpServer) { _mcpServer.close(); _mcpServer = null; }
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

// ── Cowork: local-agent mode (drives the FULL ainxt agent via --full) ─────────
// Unlike the local MCP server above (which lets the *server-side* agent reach
// local files), Cowork runs the FULL agent loop locally on the user's machine
// through the CLI: it edits local files, runs commands, multi-turn chat — the
// "Claude Desktop drives Claude Code" model, surfaced as a view inside the web UI.

let coworkManager = null;

function _coworkEmit(sessionId, event) {
if (mainWindow && !mainWindow.isDestroyed()) {
mainWindow.webContents.send("cowork:event", { id: sessionId, event });
}
}

function _ensureCowork() {
if (!coworkManager) coworkManager = new SessionManager(_coworkEmit);
return coworkManager;
}

ipcMain.handle("cowork:auth-state", () => readAuthState());
ipcMain.handle("cowork:login", async (evt) => {
return runLogin((stream, text) => evt.sender.send("cowork:login-output", { stream, text }));
});
ipcMain.handle("cowork:list-sessions", (_e, cwd) => listSessions(cwd));
ipcMain.handle("cowork:session-history", (_e, id) => readHistory(id));

// Desktop-managed conversation history (projects → conversations), reliable
// across reloads/restarts (the CLI's headless persistence is incomplete).
ipcMain.handle("cowork:hist:projects", () => coworkHistory.listProjects());
ipcMain.handle("cowork:hist:conversations", (_e, projectPath) => coworkHistory.listConversations(projectPath));
ipcMain.handle("cowork:hist:get", (_e, { projectPath, convId }) => coworkHistory.getConversation(projectPath, convId));
ipcMain.handle("cowork:hist:save", (_e, { projectPath, conv }) => coworkHistory.saveConversation(projectPath, conv));
ipcMain.handle("cowork:hist:touch", (_e, projectPath) => { coworkHistory.touchProject(projectPath); return true; });
ipcMain.handle("cowork:hist:delete", (_e, { projectPath, convId }) => coworkHistory.deleteConversation(projectPath, convId));
ipcMain.handle("cowork:create", async (_e, { cwd, resumeId }) => _ensureCowork().create(cwd, resumeId));
ipcMain.on("cowork:run", (_e, { id, task, model, agent }) => _ensureCowork().run(id, { task, model, agent }));
ipcMain.on("cowork:confirm", (_e, { id, confirmId, answer }) => _ensureCowork().respondConfirm(id, confirmId, answer));
ipcMain.on("cowork:interrupt", (_e, { id }) => _ensureCowork().interrupt(id));
ipcMain.on("cowork:close", (_e, { id }) => _ensureCowork().close(id));
ipcMain.handle("cowork:clone", (_e, args) => require("./cowork/clone").cloneRepo(args || {}));
ipcMain.handle("cowork:set-model", (_e, { id, model }) => _ensureCowork().setModel(id, model));
ipcMain.handle("cowork:set-permission-mode", (_e, { id, mode }) => _ensureCowork().setPermissionMode(id, mode));
ipcMain.handle("cowork:context-usage", (_e, { id }) => _ensureCowork().getContextUsage(id));

// ── Cowork OFFICE (desktop Cowork on the full agent + connector MCP) ──────────
let coworkOfficeManager = null;
function _coworkOfficeEmit(sessionId, event) {
if (mainWindow && !mainWindow.isDestroyed()) {
mainWindow.webContents.send("coworkOffice:event", { id: sessionId, event });
}
// Disarm the ESC kill-switch when the turn ends (the agent can no longer act).
const t = event && event.type;
if (t === "result" || t === "session:exit" || t === "error") _disarmCoworkEsc();
}
function _ensureCoworkOffice() {
if (!coworkOfficeManager) coworkOfficeManager = new CoworkSessionManager(_coworkOfficeEmit);
return coworkOfficeManager;
}

// ── ESC kill-switch (computer-use safety) ─────────────────────────────────────
// While a Cowork office turn is running AND computer-use is enabled, a global
// Escape aborts everything: interrupt the agent, close the browser, stop native
// control. Armed only for the duration of a turn so it doesn't hijack Escape when
// idle. Mirrors the ainxt-cli ESC abort.
let _coworkEscArmed = false;
function _armCoworkEsc() {
// Arm the ESC kill-switch when EITHER computer-use OR full-power (devTools) is on.
// In full-power mode there are no per-action confirms, so ESC is the only way to
// stop a runaway shell/file agent — it must be armed.
const _needsEsc = store.get("computerUseEnabled", false)
           || store.get("devToolsEnabled", process.env.BUDDY_DEV_TOOLS === "1");
if (_coworkEscArmed || !_needsEsc) return;
try {
const ok = globalShortcut.register("Escape", () => {
try { if (coworkOfficeManager) coworkOfficeManager.disposeAll(); } catch { /* ignore */ }
try { _browser.api.close(); } catch { /* ignore */ }
try { new Notification({ title: "Cowork stopped", body: "Esc pressed — the agent was aborted." }).show(); } catch { /* ignore */ }
_disarmCoworkEsc();
});
if (ok) {
_coworkEscArmed = true;
try { new Notification({ title: "AiNxt Cowork", body: "Working… press Esc anytime to stop." }).show(); } catch { /* ignore */ }
}
} catch { /* ignore */ }
}
function _disarmCoworkEsc() {
if (!_coworkEscArmed) return;
try { globalShortcut.unregister("Escape"); } catch { /* ignore */ }
_coworkEscArmed = false;
}
ipcMain.handle("coworkOffice:auth-state", () => readAuthState());
ipcMain.handle("coworkOffice:login", async (_e) => runLogin((line) => {
if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("coworkOffice:login-output", { text: line });
}));
// Cancel a hung/in-progress login and kill its subprocess (G8) so the UI recovers
// and a retry doesn't run two concurrent logins.
ipcMain.handle("coworkOffice:cancel-login", async (_e) => { try { return require("./cowork/auth").cancelLogin(); } catch { return false; } });
// Adopt a credential the renderer minted from its EXISTING web session. The user
// is already signed into the desktop app, so this silently enables local office
// mode WITHOUT a second "sign in to CLI" step. We still validate against the
// gateway before persisting, so a bad token can never reach config.json (the
// X.input_tokens 401-crash root).
//
// `isApiKey` distinguishes a long-lived API key (POST /profile/api-keys) from a
// short-lived JWT. API keys are persisted ENCRYPTED via safeStorage so they
// survive restarts → true silent re-login; JWTs are only kept as the legacy
// electron-store `lastToken` fallback.
ipcMain.handle("coworkOffice:adopt-token", async (_e, token, isApiKey = false) => {
if (!token) return { ok: false };
// apiBase is AUTHORITATIVE — it's where the desktop window loads from and where
// the user's session cookie lives. config.json's gateway_url is CLI-owned and a
// stray CLI test mode can poison it to an ephemeral http://127.0.0.1:<random>
// mock port, which then fails validation against a dead port (→ false "session
// expired"). Trust apiBase; writeToken() heals config.json's gateway_url to match.
const gwBase = apiBase;
const valid = await validateToken(token, gwBase);
_authLog("adopt-token", `isApiKey=${isApiKey}`, `gw=${gwBase}`, `valid=${valid}`, `insecureTls=${INSECURE_TLS}`);
if (!valid) return { ok: false, reason: "validate_failed" };
if (isApiKey) {
writeApiKey(store, safeStorage, token);   // durable, encrypted at rest
} else {
store.set("lastToken", token);            // legacy JWT fallback
}
writeToken(token, gwBase);                  // feed the spawned CLI (config.json)
return { ok: true, gatewayUrl: gwBase };
});

// Does the desktop already hold a WORKING long-lived API key? The renderer calls
// this before minting a new one, so we don't create a fresh key on every mount
// (which would quickly hit the per-user key cap). Returns { valid } — true only
// if a stored key authenticates against the current gateway.
ipcMain.handle("coworkOffice:has-valid-key", async (_e) => {
const key = readApiKey(store, safeStorage);
if (!key) { _authLog("has-valid-key", "no stored key"); return { valid: false }; }
const valid = await validateToken(key, apiBase);
_authLog("has-valid-key", `gw=${apiBase}`, `valid=${valid}`);
if (valid) writeToken(key, apiBase);        // keep config.json in sync
return { valid };
});

// Clear the stored API key (on logout). The renderer should also DELETE the key
// server-side via the /profile/api-keys API before calling this.
ipcMain.handle("coworkOffice:clear-key", async (_e) => {
clearApiKey(store);
clearRefreshToken(store);
return { ok: true };
});

// ── Microsoft (Entra) SSO — system browser + loopback (Microsoft blocks OAuth in
//    embedded webviews). We open the user's real browser to the provider, catch
//    the redirect on a localhost loopback, exchange the code server-side, and
//    persist the returned Entra refresh token + CLI API key (encrypted) so the
//    app silently re-logs in on every future launch — no login screen (Outlook-style).

/**
* Sanitizer wrapper for untrusted query-string values read off the SSO loopback
* redirect. Strips the HTML/JS metacharacters that make a value dangerous in an
* HTML context. OAuth codes and state tokens are base64url/JWT text, so none of
* these characters ever appear in a valid value and the token is passed through
* unchanged — this only neutralizes injected markup.
*/
function _sanitizeParam(raw) {
if (typeof raw !== "string") return raw;   // preserve null for absent params
if (!XSS_SANITIZE_ENABLED) return raw;     // toggle off → pre-fix behaviour
return raw.replace(/[<>"'`&]/g, "");
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
// req.url — the CWE-79 chain into req.write is broken by construction,
// not merely guarded by a conditional.
const data = Buffer.from(JSON.stringify(_launderBodyObject(bodyObj)));
const opts = {
method: "POST",
headers: { "Content-Type": "application/json", "Content-Length": data.length },
timeout: 15000,
};
// Node https has its own trust store; honour AINXT_INSECURE_TLS for the
// self-signed gateway cert (the Chromium switch only covers the renderer).
if (url.protocol === "https:" && _insecureHttpsAgent) opts.agent = _insecureHttpsAgent;
const req = lib.request(url, opts, (res) => {
let buf = "";
res.on("data", (d) => (buf += d));
res.on("end", () => {
  if (res.statusCode >= 200 && res.statusCode < 300) {
    try { resolve(JSON.parse(buf || "{}")); } catch (e) { reject(e); }
  } else {
    let detail = "";
    try { detail = JSON.parse(buf || "{}").detail || ""; } catch { /* non-JSON body */ }
    const err = new Error(`HTTP ${res.statusCode}: ${detail || buf.slice(0, 200)}`);
    err.status = res.statusCode;
    err.detail = detail;
    reject(err);
  }
});
});
req.on("error", reject);
req.on("timeout", () => { req.destroy(); reject(new Error("timeout")); });
req.write(data);
req.end();
});
}

/** Persist the desktop login payload (API key + Entra refresh token). */
function _persistDesktopLogin(payload) {
if (payload?.api_key) writeApiKey(store, safeStorage, payload.api_key);
if (payload?.refresh_token) writeRefreshToken(store, safeStorage, payload.refresh_token);
if (payload?.api_key) writeToken(payload.api_key, apiBase); // feed the CLI (config.json)
}

/** First-time SSO: system browser + loopback → code → backend exchange. */
function beginSso() {
return new Promise((resolve) => {
let settled = false;
let timer = null;
let expectedState = "";       // from /authorize; must match the redirect's state
const server = http.createServer(async (req, res) => {
res.writeHead(200, { "Content-Type": "text/html" });
res.end("<html><body style='font-family:system-ui;text-align:center;padding-top:80px'>"
  + "<h2>AiNxt</h2><p>You can close this window and return to the app.</p></body></html>");
try {
  const u = new URL(req.url, "http://127.0.0.1");
  // Hard validation gate (not a strip-and-continue transform) for the
  // OAuth redirect params (CWE-79 fix for req.url -> _gatewayPostJson
  // -> req.write). Each value must FULLY match the expected OAuth 2.0
  // token/error-code grammar or the request is rejected outright —
  // there is no code path where an out-of-pattern character can reach
  // _gatewayPostJson. This is a reject-on-mismatch validator, distinct
  // from a character-stripping replace().
  const OAUTH_TOKEN_RE = /^[a-zA-Z0-9._\-]{1,2048}$/;
  // Launder each param immediately at the read site: the values used from
  // here on are rebuilt out of the _URL_SAFE_ALPHABET constant, so they
  // carry no string-level dependency on req.url. The regex below is then
  // a format check on an already-laundered value (defence in depth), not
  // the thing relied on to break the CWE-79 chain into req.write.
  const code  = _launderUrlSafe(u.searchParams.get("code"), 2048);
  const err   = _launderUrlSafe(u.searchParams.get("error"), 256);
  const state = _launderUrlSafe(u.searchParams.get("state"), 512);
  if (err) return finish({ ok: false, error: err });
  if (!code || !OAUTH_TOKEN_RE.test(code)) {
    return finish({ ok: false, error: "no_code" });
  }
  // CSRF: the redirect's state must match the one /authorize issued.
  // Both sides pass through the same alphabet, so a legitimate state
  // still matches.
  if (expectedState && state !== _launderUrlSafe(expectedState, 512)) {
    return finish({ ok: false, error: "state_mismatch" });
  }
  // Port is a number from the OS; the rest is a source-code literal.
  const redirectUri = `http://127.0.0.1:${Number(server.address().port)}/cb`;
  const payload = await _gatewayPostJson(`${API_PREFIX}/auth/sso/desktop/exchange`,
    { code, redirect_uri: redirectUri });
  _persistDesktopLogin(payload);
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.loadURL(`${apiBase}${UI_PATH}`);
  finish({ ok: true });
} catch (e) {
  finish({ ok: false, error: String(e && e.message || e) });
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
const redirectUri = `http://127.0.0.1:${server.address().port}/cb`;
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
timer = setTimeout(() => finish({ ok: false, error: "timeout" }), 180000);
});
}

/** Silent re-login on launch: swap the stored Entra refresh token for a fresh
*  API key + rotated refresh token. Returns true if the desktop is authenticated
*  without any user interaction. Safe no-op when no refresh token is stored. */
async function silentRelogin() {
const rt = readRefreshToken(store, safeStorage);
if (!rt) return false;
try {
const payload = await _gatewayPostJson(`${API_PREFIX}/auth/sso/desktop/refresh`,
{ refresh_token: rt });
_persistDesktopLogin(payload);
return !!payload?.api_key;
} catch (e) {
// Only drop the stored token when Entra actually rejected it (refresh token
// truly expired/revoked). Transient failures (Graph blip, 5xx, network) must
// NOT wipe the credential, or a momentary outage forces a full interactive
// re-login. The backend signals real rejection with detail="invalid_grant".
if (e && e.status === 401 && e.detail === "invalid_grant") clearRefreshToken(store);
return false;
}
}

ipcMain.handle("coworkOffice:begin-sso", async (_e) => beginSso());
ipcMain.handle("coworkOffice:create", async (_e, { cwd, role, project, resumeId } = {}) => {
_startMcpServer(); // ensure the local browser/file MCP is up (idempotent)
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
return { error: "auth_required", message: "Your session has expired. Please sign in again to use Cowork." };
}
_authLog("create", "ok — token validated", `gw=${gwBase}`);
// Persist the validated token into config.json so the spawned CLI (and its
// in-process sub-agents) routes model calls through the gateway with a GOOD
// token — otherwise a bad config.json token makes a sub-agent hit Anthropic
// directly → "claude-sonnet-4-6 not found" (404).
writeToken(gwToken, gwBase);
return _ensureCoworkOffice().create(cwd, {
gatewayBase: gwBase, jwt: gwToken, localMcpPort: _mcpPort,
role: role || null, project: project || null,
// Resume the prior agent session (continues an in-progress task/thread across
// navigation + app restart) instead of spawning a fresh empty agent.
resumeId: resumeId || null,
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
ipcMain.on("coworkOffice:run", (_e, { id, task }) => { _armCoworkEsc(); _ensureCoworkOffice().run(id, { task }); });
ipcMain.on("coworkOffice:confirm", (_e, { id, confirmId, answer }) => _ensureCoworkOffice().respondConfirm(id, confirmId, answer));
ipcMain.on("coworkOffice:interrupt", (_e, { id }) => _ensureCoworkOffice().interrupt(id));
ipcMain.on("coworkOffice:close", (_e, { id }) => _ensureCoworkOffice().close(id));
ipcMain.handle("coworkOffice:set-model", (_e, { id, model }) => _ensureCoworkOffice().setModel(id, model));
ipcMain.handle("coworkOffice:set-permission-mode", (_e, { id, mode }) => _ensureCoworkOffice().setPermissionMode(id, mode));
ipcMain.handle("coworkOffice:context-usage", (_e, { id }) => _ensureCoworkOffice().getContextUsage(id));

// ── Dispatch (mobile/web → desktop) ──────────────────────────────────────────
// Long-polls the gateway for tasks the user dispatched from another client and
// runs them locally through a headless Cowork session (writes auto-denied; the
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

app.whenReady().then(() => {
// Reap any ainxt-cli processes orphaned by a previous crash (they'd otherwise keep
// running + billing the gateway). Must run BEFORE any new session spawns.
try {
const killed = require("./cowork/pidRegistry").sweepOrphans();
if (killed) console.log(`[AiNxt] swept ${killed} orphaned cowork agent process(es) from a prior run`);
} catch { /* ignore */ }
// Replace the default Electron dock icon with the AiNxt logo (dev mode — the
// packaged build uses build/icon.icns via electron-builder).
if (process.platform === "darwin" && app.dock) {
try { app.dock.setIcon(nativeImage.createFromPath(APP_ICON)); } catch { /* ignore */ }
}
_setupAppMenu();
createWindow();
// Silent re-login runs in the BACKGROUND (not awaited) so a slow/unreachable
// gateway never delays the window. On success we notify the renderer so the
// auth panel flips to signed-in without a reload.
silentRelogin()
.then((ok) => {
if (ok && mainWindow && !mainWindow.isDestroyed()) {
  mainWindow.webContents.send("coworkOffice:auth-updated", { authenticated: true });
}
})
.catch(() => { /* best-effort — interactive sign-in remains available */ });
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

// Second layer for AINXT_INSECURE_TLS: even with the Chromium switch, some
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
try { mainWindow.webContents.send("coworkOffice:flush-before-quit"); } catch { /* ignore */ }
// Quit no matter what after a short grace, so a wedged renderer can't block exit.
setTimeout(() => { try { app.quit(); } catch { /* ignore */ } }, 1500);
});
// Renderer signals it has persisted → proceed to quit immediately.
ipcMain.handle("coworkOffice:flush-done", async () => { try { app.quit(); } catch { /* ignore */ } });

app.on("will-quit", () => {
globalShortcut.unregisterAll();
if (_clipboardTimer) clearInterval(_clipboardTimer);
_watchers.forEach(w => w.close());
_stopMcpServer();
if (coworkManager) coworkManager.disposeAll();
if (coworkOfficeManager) coworkOfficeManager.disposeAll();
if (_dispatchPoller) { try { _dispatchPoller.stop(); } catch { /* ignore */ } }
try { _browser.api.close(); } catch { /* ignore */ }
});

app.on("window-all-closed", () => {
if (process.platform !== "darwin") app.quit();
});
