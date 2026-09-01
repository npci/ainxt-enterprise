// SPDX-License-Identifier: Apache-2.0
const { contextBridge, ipcRenderer } = require("electron");

/* Secure bridge: exposes only the defined API surface.
   Nothing from Node/Electron leaks into the web context. */
contextBridge.exposeInMainWorld("ainxtDesktop", {
  isDesktop: true,

  // ── Config ──────────────────────────────────────────────────────────────
  getApiBase:   ()        => ipcRenderer.invoke("get-api-base"),
  setApiBase:   (url)     => ipcRenderer.invoke("set-api-base", url),
  getVersion:   ()        => ipcRenderer.invoke("get-version"),
  saveToken:    (token)   => ipcRenderer.invoke("save-token", token),

  // ── SSO (Microsoft Entra) — system-browser + loopback flow, run in main. The
  //    web Login screen calls this when running inside the desktop app. ──────────
  beginSso:     ()        => ipcRenderer.invoke("coworkOffice:begin-sso"),
  cancelSso:    ()        => ipcRenderer.invoke("coworkOffice:cancel-sso"),

  // ── Notifications + links ────────────────────────────────────────────────
  notify:       (title, body) => ipcRenderer.invoke("show-notification", { title, body }),
  openExternal: (url)         => ipcRenderer.invoke("open-external", url),

  // ── Phase 1: Local file access ───────────────────────────────────────────
  pickFolder:   ()             => ipcRenderer.invoke("pick-folder"),
  pickFile:     ()             => ipcRenderer.invoke("pick-file"),
  readFile:     (filePath)     => ipcRenderer.invoke("read-file", filePath),
  readFileBinary: (filePath)   => ipcRenderer.invoke("read-file-binary", filePath),
  // Parse an Excel workbook (.xlsx/.xlsm) and return model-friendly text.
  // Legacy .xls is not supported (see main.js read-file-spreadsheet handler).
  // Returns { text, sheets, tables, warnings, error }.
  readFileSpreadsheet: (filePath) => ipcRenderer.invoke("read-file-spreadsheet", filePath),
  listFolder:   (dir, opts)    => ipcRenderer.invoke("list-folder", dir, opts),

  // ── Lite IDE: guarded filesystem mutations (inside open workspace only) ────
  writeFile:    (filePath, content) => ipcRenderer.invoke("write-file", filePath, content),
  createPath:   (p, isDir)          => ipcRenderer.invoke("create-path", p, isDir),
  deletePath:   (p)                 => ipcRenderer.invoke("delete-path", p),
  renamePath:   (oldP, newP)        => ipcRenderer.invoke("rename-path", oldP, newP),

  // ── Phase 2: Workspace watcher ───────────────────────────────────────────
  watchFolder:        (dir)  => ipcRenderer.invoke("watch-folder", dir),
  unwatchFolder:      (dir)  => ipcRenderer.invoke("unwatch-folder", dir),
  getWatchedFolders:  ()     => ipcRenderer.invoke("get-watched-folders"),

  // Fires when a watched file changes; cb(event) where event = {event, filename, folder}
  onWorkspaceChange:  (cb)   => ipcRenderer.on("workspace-file-changed", (_e, data) => cb(data)),
  offWorkspaceChange: (cb)   => ipcRenderer.off("workspace-file-changed", cb),

  // ── Phase 3: Clipboard ───────────────────────────────────────────────────
  getClipboard:       ()     => ipcRenderer.invoke("get-clipboard"),
  setClipboard:       (text) => ipcRenderer.invoke("set-clipboard", text),

  // Fires when clipboard text changes while AiNxt is not focused; cb({text, ts})
  onClipboardChange:  (cb)   => ipcRenderer.on("clipboard-changed", (_e, data) => cb(data)),
  offClipboardChange: (cb)   => ipcRenderer.off("clipboard-changed", cb),

  // ── Phase 4: Shortcut context ────────────────────────────────────────────
  getShortcutContext: ()     => ipcRenderer.invoke("get-shortcut-context"),

  // Fires on Cmd+Shift+A with {clipboard, activeApp}
  onShortcutContext:  (cb)   => ipcRenderer.on("shortcut-context", (_e, ctx) => cb(ctx)),
  offShortcutContext: (cb)   => ipcRenderer.off("shortcut-context", cb),

  // ── Phase 5: Local MCP server ────────────────────────────────────────────
  getMcpPort:           ()   => ipcRenderer.invoke("get-mcp-port"),
  registerMcpWithBackend: () => ipcRenderer.invoke("register-mcp-with-backend"),

  // Fires when MCP server starts; cb({port})
  onMcpServerReady:   (cb)   => ipcRenderer.on("mcp-server-ready", (_e, data) => cb(data)),

  // ── Main process requests ────────────────────────────────────────────────
  onRequestApiBase:   (cb)   => ipcRenderer.on("request-api-base", (_e, current) => cb(current)),

  // ── Cowork: local-agent mode (drives the ainxt CLI locally) ───────────────
  cowork: {
    getAuthState:  ()                       => ipcRenderer.invoke("cowork:auth-state"),
    login:         ()                       => ipcRenderer.invoke("cowork:login"),
    onLoginOutput: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("cowork:login-output", h);
      return () => ipcRenderer.removeListener("cowork:login-output", h);
    },

    // Silent-adopt credential bridge. The Code (local-agent) screen calls these to
    // mint/store an API key without a second sign-in — but they were missing here,
    // so coworkAdoptToken() short-circuited to {ok:false} ("Couldn't save your
    // access key locally") and Code could NEVER authenticate silently while Buddy
    // (coworkOffice) could. The credential store (encrypted key) and config.json are
    // shared and single-user, so we reuse the coworkOffice:* handlers directly.
    adoptToken:    (token, isApiKey = false) => ipcRenderer.invoke("coworkOffice:adopt-token", token, isApiKey),
    hasValidKey:   ()                        => ipcRenderer.invoke("coworkOffice:has-valid-key"),
    clearKey:      ()                        => ipcRenderer.invoke("coworkOffice:clear-key"),
    onAuthUpdated: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("coworkOffice:auth-updated", h);
      return () => ipcRenderer.removeListener("coworkOffice:auth-updated", h);
    },

    listSessions:     (cwd) => ipcRenderer.invoke("cowork:list-sessions", cwd),
    getSessionHistory:(id)  => ipcRenderer.invoke("cowork:session-history", id),

    // desktop-managed history (projects → conversations)
    history: {
      listProjects:      ()                       => ipcRenderer.invoke("cowork:hist:projects"),
      listConversations: (projectPath)            => ipcRenderer.invoke("cowork:hist:conversations", projectPath),
      getConversation:   (projectPath, convId)    => ipcRenderer.invoke("cowork:hist:get", { projectPath, convId }),
      saveConversation:  (projectPath, conv)      => ipcRenderer.invoke("cowork:hist:save", { projectPath, conv }),
      touchProject:      (projectPath)            => ipcRenderer.invoke("cowork:hist:touch", projectPath),
      deleteConversation:(projectPath, convId)    => ipcRenderer.invoke("cowork:hist:delete", { projectPath, convId }),
    },

    createSession: (cwd, resumeId)          => ipcRenderer.invoke("cowork:create", { cwd, resumeId }),
    run:           (id, task, model, agent) => ipcRenderer.send("cowork:run", { id, task, model, agent }),
    respondConfirm:(id, confirmId, answer)  => ipcRenderer.send("cowork:confirm", { id, confirmId, answer }),
    interrupt:     (id)                     => ipcRenderer.send("cowork:interrupt", { id }),
    closeSession:  (id)                     => ipcRenderer.send("cowork:close", { id }),
    clone:           (args)      => ipcRenderer.invoke("cowork:clone", args),
    setModel:        (id, model) => ipcRenderer.invoke("cowork:set-model", { id, model }),
    setPermissionMode:(id, mode) => ipcRenderer.invoke("cowork:set-permission-mode", { id, mode }),
    contextUsage:    (id)        => ipcRenderer.invoke("cowork:context-usage", { id }),

    // CLI → renderer event stream; returns an unsubscribe fn
    onEvent: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("cowork:event", h);
      return () => ipcRenderer.removeListener("cowork:event", h);
    },
  },

  // ── Native computer-use master switch ──────────────────────────────────────
  computerUse: {
    isEnabled:  ()   => ipcRenderer.invoke("computeruse:enabled"),
    setEnabled: (on) => ipcRenderer.invoke("computeruse:set-enabled", on),
  },

  // ── Cowork OFFICE: desktop Cowork on the full agent (connectors via MCP) ──
  coworkOffice: {
    getAuthState:  ()  => ipcRenderer.invoke("coworkOffice:auth-state"),
    adoptToken:    (token, isApiKey = false) => ipcRenderer.invoke("coworkOffice:adopt-token", token, isApiKey),
    hasValidKey:   ()  => ipcRenderer.invoke("coworkOffice:has-valid-key"),
    clearKey:      ()  => ipcRenderer.invoke("coworkOffice:clear-key"),
    onAuthUpdated: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("coworkOffice:auth-updated", h);
      return () => ipcRenderer.removeListener("coworkOffice:auth-updated", h);
    },
    login:         ()  => ipcRenderer.invoke("coworkOffice:login"),
    cancelLogin:   ()  => ipcRenderer.invoke("coworkOffice:cancel-login"),
    // G11: main asks the renderer to persist the active conversation before quit;
    // the renderer calls flushDone() when saved so the app can exit.
    onFlushBeforeQuit: (cb) => {
      const h = () => cb();
      ipcRenderer.on("coworkOffice:flush-before-quit", h);
      return () => ipcRenderer.removeListener("coworkOffice:flush-before-quit", h);
    },
    flushDone:     ()  => ipcRenderer.invoke("coworkOffice:flush-done"),
    onLoginOutput: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("coworkOffice:login-output", h);
      return () => ipcRenderer.removeListener("coworkOffice:login-output", h);
    },
    createSession:  (cwd, role, project, resumeId, model, convId) => ipcRenderer.invoke("coworkOffice:create", { cwd, role, project, resumeId, model, convId: convId || null }),
    run:            (id, task, model, convId) => ipcRenderer.send("coworkOffice:run", { id, task, model, convId: convId || null }),
    respondConfirm: (id, confirmId, answer) => ipcRenderer.send("coworkOffice:confirm", { id, confirmId, answer }),
    interrupt:      (id)                    => ipcRenderer.send("coworkOffice:interrupt", { id }),
    closeSession:   (id)                    => ipcRenderer.send("coworkOffice:close", { id }),
    setModel:         (id, model) => ipcRenderer.invoke("coworkOffice:set-model", { id, model }),
    setPermissionMode:(id, mode)  => ipcRenderer.invoke("coworkOffice:set-permission-mode", { id, mode }),
    contextUsage:     (id)        => ipcRenderer.invoke("coworkOffice:context-usage", { id }),
    onEvent: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("coworkOffice:event", h);
      return () => ipcRenderer.removeListener("coworkOffice:event", h);
    },
  },
});
