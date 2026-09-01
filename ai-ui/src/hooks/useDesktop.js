// SPDX-License-Identifier: Apache-2.0
/* Detects whether the app is running inside the AiNxt Electron desktop app
   and exposes helpers that fall back gracefully in browser context. */

const desktop = typeof window !== "undefined" && window.ainxtDesktop?.isDesktop
  ? window.ainxtDesktop
  : null;

export const isDesktop = !!desktop;

// ── Notifications + links ───────────────────────────────────────────────────

export function desktopNotify(title, body) {
  if (desktop) {
    desktop.notify(title, body);
  } else if (typeof Notification !== "undefined" && Notification.permission === "granted") {
    new Notification(title, { body });
  }
}

export function openExternal(url) {
  if (desktop) desktop.openExternal(url);
  else window.open(url, "_blank", "noopener,noreferrer");
}

export async function getApiBase() {
  if (desktop) return desktop.getApiBase();
  return null;
}

// ── Phase 1: File access ─────────────────────────────────────────────────────

export async function pickFolder() {
  if (!desktop) return null;
  return desktop.pickFolder();
}

export async function pickFile() {
  if (!desktop) return [];
  return desktop.pickFile();
}

export async function readFile(filePath) {
  if (!desktop) return { error: "Not running in desktop app", content: null };
  return desktop.readFile(filePath);
}

export async function readFileBinary(filePath) {
  if (!desktop) return { error: "Not running in desktop app", base64: null };
  return desktop.readFileBinary(filePath);
}

/**
 * Parse an Excel workbook (.xlsx / .xls / .xlsm) via the desktop main process
 * (SheetJS) and return model-friendly text.
 *
 * Returns { text, sheets, tables, warnings, error }.
 * `text` is tab-separated rows with a "## Sheet: <name>" heading per sheet —
 * identical to the server-side parse_excel() output so desktop and web Chat
 * produce the same content quality.
 */
export async function readFileSpreadsheet(filePath) {
  if (!desktop) return { error: "Not running in desktop app", text: null };
  return desktop.readFileSpreadsheet(filePath);
}

export async function listFolder(dir, opts) {
  if (!desktop) return [];
  return desktop.listFolder(dir, opts);
}

// ── Lite IDE: guarded filesystem mutations (inside open workspace only) ───────

export async function writeFile(filePath, content) {
  if (!desktop) return { ok: false, error: "not_desktop" };
  return desktop.writeFile(filePath, content);
}

export async function createPath(p, isDir) {
  if (!desktop) return { ok: false, error: "not_desktop" };
  return desktop.createPath(p, !!isDir);
}

export async function deletePath(p) {
  if (!desktop) return { ok: false, error: "not_desktop" };
  return desktop.deletePath(p);
}

export async function renamePath(oldP, newP) {
  if (!desktop) return { ok: false, error: "not_desktop" };
  return desktop.renamePath(oldP, newP);
}

// ── Phase 2: Workspace watcher ───────────────────────────────────────────────

export async function watchFolder(dir) {
  if (!desktop) return { watching: false };
  return desktop.watchFolder(dir);
}

export async function unwatchFolder(dir) {
  if (!desktop) return { watching: false };
  return desktop.unwatchFolder(dir);
}

export async function getWatchedFolders() {
  if (!desktop) return [];
  return desktop.getWatchedFolders();
}

export function onWorkspaceChange(cb) {
  if (desktop) desktop.onWorkspaceChange(cb);
}

export function offWorkspaceChange(cb) {
  if (desktop) desktop.offWorkspaceChange(cb);
}

// ── Phase 3: Clipboard ───────────────────────────────────────────────────────

export async function getClipboard() {
  if (desktop) return desktop.getClipboard();
  try { return await navigator.clipboard.readText(); } catch { return ""; }
}

export function onClipboardChange(cb) {
  if (desktop) desktop.onClipboardChange(cb);
}

export function offClipboardChange(cb) {
  if (desktop) desktop.offClipboardChange(cb);
}

// ── Phase 4: Shortcut context ────────────────────────────────────────────────

export async function getShortcutContext() {
  if (!desktop) return { clipboard: "", activeApp: "" };
  return desktop.getShortcutContext();
}

export function onShortcutContext(cb) {
  if (desktop) desktop.onShortcutContext(cb);
}

export function offShortcutContext(cb) {
  if (desktop) desktop.offShortcutContext(cb);
}

// ── Phase 5: Local MCP ───────────────────────────────────────────────────────

export async function getMcpPort() {
  if (!desktop) return null;
  return desktop.getMcpPort();
}

export async function registerMcpWithBackend() {
  if (!desktop) return { ok: false };
  return desktop.registerMcpWithBackend();
}

export function onMcpServerReady(cb) {
  if (desktop) desktop.onMcpServerReady(cb);
}

// ── Token sync ───────────────────────────────────────────────────────────────

export function syncTokenToDesktop(token) {
  if (desktop) desktop.saveToken(token);
}

// ── Cowork: local-agent mode ──────────────────────────────────────────────────
// Available only in the desktop app (drives the local ainxt CLI). Each call
// no-ops / returns a safe default in the browser so callers don't need guards.

export const cowork = desktop?.cowork || null;
export const isCoworkAvailable = !!cowork;

export async function coworkAuthState() {
  if (!cowork) return { authenticated: false, gatewayUrl: null, error: "not_desktop" };
  return cowork.getAuthState();
}

export async function coworkLogin() {
  if (!cowork) return { authenticated: false, gatewayUrl: null, error: "not_desktop" };
  return cowork.login();
}

export function coworkOnLoginOutput(cb) {
  if (!cowork) return () => {};
  return cowork.onLoginOutput(cb);
}

export async function coworkListSessions(cwd) {
  if (!cowork) return [];
  return cowork.listSessions(cwd);
}

export async function coworkSessionHistory(id) {
  if (!cowork) return [];
  return cowork.getSessionHistory(id);
}

export async function coworkCreateSession(cwd, resumeId) {
  if (!cowork) return null;
  return cowork.createSession(cwd, resumeId);
}

export function coworkRun(id, task, model, agent) {
  if (cowork) cowork.run(id, task, model, agent);
}

export function coworkRespondConfirm(id, confirmId, answer) {
  if (cowork) cowork.respondConfirm(id, confirmId, answer);
}

export function coworkInterrupt(id) {
  if (cowork) cowork.interrupt(id);
}

export async function coworkClone(args) {
  if (!cowork?.clone) return { ok: false, error: "Clone is only available in the desktop app." };
  return cowork.clone(args);
}

// ── Cowork OFFICE (desktop Cowork on the full agent) ──────────────────────────
const coworkOffice = desktop?.coworkOffice || null;
export const isCoworkOfficeAvailable = !!coworkOffice;

export async function coworkOfficeAuthState() {
  if (!coworkOffice) return { authenticated: false, gatewayUrl: null, error: "not_desktop" };
  return coworkOffice.getAuthState();
}
// Hand the desktop a credential minted from the renderer's existing web session
// so local office mode enables WITHOUT a second sign-in. Pass isApiKey=true for a
// long-lived API key (POST /profile/api-keys) — the desktop stores it encrypted
// (safeStorage) for durable silent re-login; a JWT is kept only as fallback.
export async function coworkOfficeAdoptToken(token, isApiKey = false) {
  if (!coworkOffice?.adoptToken || !token) return { ok: false };
  return coworkOffice.adoptToken(token, isApiKey);
}
// Does the desktop already hold a WORKING long-lived API key? Lets the renderer
// skip re-minting on every mount (avoids hitting the per-user key cap).
export async function coworkOfficeHasValidKey() {
  if (!coworkOffice?.hasValidKey) return { valid: false };
  return coworkOffice.hasValidKey();
}
// Clear the desktop's stored API key (on logout).
export async function coworkOfficeClearKey() {
  if (!coworkOffice?.clearKey) return { ok: false };
  return coworkOffice.clearKey();
}
export async function coworkOfficeLogin() {
  if (!coworkOffice) return { authenticated: false, error: "not_desktop" };
  return coworkOffice.login();
}
// Microsoft SSO sign-in (system browser + loopback, see desktop/src/main.js
// beginSso()). Note: exposed at the TOP level of window.ainxtDesktop (sibling
// to coworkOffice), not nested under it — matches preload.js's actual shape.
export async function coworkOfficeBeginSso() {
  if (!desktop?.beginSso) return { ok: false, error: "not_desktop" };
  return desktop.beginSso();
}
export async function coworkOfficeCancelLogin() {
  if (!coworkOffice?.cancelLogin) return false;
  return coworkOffice.cancelLogin();
}
export function coworkOfficeOnFlushBeforeQuit(cb) {
  if (!coworkOffice?.onFlushBeforeQuit) return () => {};
  return coworkOffice.onFlushBeforeQuit(cb);
}
export async function coworkOfficeFlushDone() {
  if (!coworkOffice?.flushDone) return;
  return coworkOffice.flushDone();
}
export function coworkOfficeOnLoginOutput(cb) {
  if (!coworkOffice) return () => {};
  return coworkOffice.onLoginOutput(cb);
}
export async function coworkOfficeCreateSession(cwd, role, project, resumeId, model, convId) {
  if (!coworkOffice) return null;
  return coworkOffice.createSession(cwd, role || null, project || null, resumeId || null, model || null, convId || null);
}
export function coworkOfficeRun(id, task, model, convId) {
  if (coworkOffice) coworkOffice.run(id, task, model || null, convId || null);
}
export function coworkOfficeRespondConfirm(id, confirmId, answer) {
  if (coworkOffice) coworkOffice.respondConfirm(id, confirmId, answer);
}
export function coworkOfficeInterrupt(id) {
  if (coworkOffice) coworkOffice.interrupt(id);
}
export function coworkOfficeCloseSession(id) {
  if (coworkOffice) coworkOffice.closeSession(id);
}
export async function coworkOfficeSetModel(id, model) {
  if (!coworkOffice?.setModel) return false;
  return coworkOffice.setModel(id, model);
}
export async function coworkOfficeSetPermissionMode(id, mode) {
  if (!coworkOffice?.setPermissionMode) return false;
  return coworkOffice.setPermissionMode(id, mode);
}
export function coworkOfficeOnEvent(cb) {
  if (!coworkOffice) return () => {};
  return coworkOffice.onEvent(cb);
}
// Listen for the main-process push sent by silentRelogin() when it successfully
// exchanges a stored Entra refresh token for a fresh API key on app launch.
// Returns an unsubscribe function (call it in useEffect cleanup).
export function coworkOfficeOnAuthUpdated(cb) {
  if (!coworkOffice?.onAuthUpdated) return () => {};
  return coworkOffice.onAuthUpdated(cb);
}

export async function coworkSetModel(id, model) {
  if (!cowork?.setModel) return false;
  return cowork.setModel(id, model);
}

export async function coworkSetPermissionMode(id, mode) {
  if (!cowork?.setPermissionMode) return false;
  return cowork.setPermissionMode(id, mode);
}

export async function coworkContextUsage(id) {
  if (!cowork?.contextUsage) return null;
  return cowork.contextUsage(id);
}

export function coworkCloseSession(id) {
  if (cowork) cowork.closeSession(id);
}

export function coworkOnEvent(cb) {
  if (!cowork) return () => {};
  return cowork.onEvent(cb);
}
// Hand the desktop a credential minted from the renderer's existing web session
// so local code mode enables WITHOUT a second sign-in. Pass isApiKey=true for a
// long-lived API key (POST /profile/api-keys) — the desktop stores it encrypted
// (safeStorage) for durable silent re-login; a JWT is kept only as fallback.
export async function coworkAdoptToken(token, isApiKey = false) {
  if (!cowork?.adoptToken || !token) return { ok: false };
  return cowork.adoptToken(token, isApiKey);
}
// Does the desktop already hold a WORKING long-lived API key? Lets the renderer
// skip re-minting on every mount (avoids hitting the per-user key cap).
export async function coworkHasValidKey() {
  if (!cowork?.hasValidKey) return { valid: false };
  return cowork.hasValidKey();
}
// Listen for the main-process push sent by silentRelogin() when it successfully
// exchanges a stored Entra refresh token for a fresh API key on app launch.
// Returns an unsubscribe function (call it in useEffect cleanup).
export function coworkOnAuthUpdated(cb) {
  if (!cowork?.onAuthUpdated) return () => {};
  return cowork.onAuthUpdated(cb);
}

// ── Desktop-managed history (projects → conversations) ────────────────────────
export async function coworkHistListProjects() {
  if (!cowork?.history) return [];
  return cowork.history.listProjects();
}
export async function coworkHistListConversations(projectPath) {
  if (!cowork?.history) return [];
  return cowork.history.listConversations(projectPath);
}
export async function coworkHistGet(projectPath, convId) {
  if (!cowork?.history) return null;
  return cowork.history.getConversation(projectPath, convId);
}
export async function coworkHistSave(projectPath, conv) {
  if (!cowork?.history) return false;
  return cowork.history.saveConversation(projectPath, conv);
}
export async function coworkHistTouch(projectPath) {
  if (!cowork?.history) return false;
  return cowork.history.touchProject(projectPath);
}
export async function coworkHistDelete(projectPath, convId) {
  if (!cowork?.history) return false;
  return cowork.history.deleteConversation(projectPath, convId);
}
