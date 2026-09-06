// SPDX-License-Identifier: MIT
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
  beginSso:     ()        => ipcRenderer.invoke("buddyOffice:begin-sso"),
  cancelSso:    ()        => ipcRenderer.invoke("buddyOffice:cancel-sso"),

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

  // ── Buddy: local-agent mode (drives the ainxt CLI locally) ───────────────
  // NOTE: exposed key stays `cowork` — the ai-ui frontend (useDesktop.js and
  // ~15 other files, outside this app's scope) reads window.ainxtDesktop.cowork
  // directly. Renaming this key without also updating every ai-ui call site
  // would break the app; everything else in this file/desktop app uses the
  // "buddy" naming, only this external contract boundary keeps the old name.
  cowork: {
    getAuthState:  ()                       => ipcRenderer.invoke("buddy:auth-state"),
    login:         ()                       => ipcRenderer.invoke("buddy:login"),
    onLoginOutput: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("buddy:login-output", h);
      return () => ipcRenderer.removeListener("buddy:login-output", h);
    },

    // Silent-adopt credential bridge. The Code (local-agent) screen calls these to
    // mint/store an API key without a second sign-in — but they were missing here,
    // so buddyAdoptToken() short-circuited to {ok:false} ("Couldn't save your
    // access key locally") and Code could NEVER authenticate silently while Buddy
    // (buddyOffice) could. The credential store (encrypted key) and config.json are
    // shared and single-user, so we reuse the buddyOffice:* handlers directly.
    adoptToken:    (token, isApiKey = false) => ipcRenderer.invoke("buddyOffice:adopt-token", token, isApiKey),
    hasValidKey:   ()                        => ipcRenderer.invoke("buddyOffice:has-valid-key"),
    clearKey:      ()                        => ipcRenderer.invoke("buddyOffice:clear-key"),
    onAuthUpdated: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("buddyOffice:auth-updated", h);
      return () => ipcRenderer.removeListener("buddyOffice:auth-updated", h);
    },

    listSessions:     (cwd) => ipcRenderer.invoke("buddy:list-sessions", cwd),
    getSessionHistory:(id)  => ipcRenderer.invoke("buddy:session-history", id),

    // desktop-managed history (projects → conversations)
    history: {
      listProjects:      ()                       => ipcRenderer.invoke("buddy:hist:projects"),
      listConversations: (projectPath)            => ipcRenderer.invoke("buddy:hist:conversations", projectPath),
      getConversation:   (projectPath, convId)    => ipcRenderer.invoke("buddy:hist:get", { projectPath, convId }),
      saveConversation:  (projectPath, conv)      => ipcRenderer.invoke("buddy:hist:save", { projectPath, conv }),
      touchProject:      (projectPath)            => ipcRenderer.invoke("buddy:hist:touch", projectPath),
      deleteConversation:(projectPath, convId)    => ipcRenderer.invoke("buddy:hist:delete", { projectPath, convId }),
    },

    createSession: (cwd, resumeId)          => ipcRenderer.invoke("buddy:create", { cwd, resumeId }),
    run:           (id, task, model, agent) => ipcRenderer.send("buddy:run", { id, task, model, agent }),
    respondConfirm:(id, confirmId, answer)  => ipcRenderer.send("buddy:confirm", { id, confirmId, answer }),
    interrupt:     (id)                     => ipcRenderer.send("buddy:interrupt", { id }),
    closeSession:  (id)                     => ipcRenderer.send("buddy:close", { id }),
    clone:           (args)      => ipcRenderer.invoke("buddy:clone", args),
    setModel:        (id, model) => ipcRenderer.invoke("buddy:set-model", { id, model }),
    setPermissionMode:(id, mode) => ipcRenderer.invoke("buddy:set-permission-mode", { id, mode }),
    contextUsage:    (id)        => ipcRenderer.invoke("buddy:context-usage", { id }),

    // CLI → renderer event stream; returns an unsubscribe fn
    onEvent: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("buddy:event", h);
      return () => ipcRenderer.removeListener("buddy:event", h);
    },
  },

  // ── Native computer-use master switch ──────────────────────────────────────
  computerUse: {
    isEnabled:  ()   => ipcRenderer.invoke("computeruse:enabled"),
    setEnabled: (on) => ipcRenderer.invoke("computeruse:set-enabled", on),
  },

  // ── Buddy OFFICE: desktop Buddy on the full agent (connectors via MCP) ──
  // NOTE: exposed key stays `coworkOffice` — same external-contract reason as
  // the `cowork` key above (ai-ui/src/hooks/useDesktop.js and others read it
  // directly; renaming needs a matching ai-ui-side pass, out of this scope).
  coworkOffice: {
    getAuthState:  ()  => ipcRenderer.invoke("buddyOffice:auth-state"),
    adoptToken:    (token, isApiKey = false) => ipcRenderer.invoke("buddyOffice:adopt-token", token, isApiKey),
    hasValidKey:   ()  => ipcRenderer.invoke("buddyOffice:has-valid-key"),
    clearKey:      ()  => ipcRenderer.invoke("buddyOffice:clear-key"),
    onAuthUpdated: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("buddyOffice:auth-updated", h);
      return () => ipcRenderer.removeListener("buddyOffice:auth-updated", h);
    },
    login:         ()  => ipcRenderer.invoke("buddyOffice:login"),
    cancelLogin:   ()  => ipcRenderer.invoke("buddyOffice:cancel-login"),
    // G11: main asks the renderer to persist the active conversation before quit;
    // the renderer calls flushDone() when saved so the app can exit.
    onFlushBeforeQuit: (cb) => {
      const h = () => cb();
      ipcRenderer.on("buddyOffice:flush-before-quit", h);
      return () => ipcRenderer.removeListener("buddyOffice:flush-before-quit", h);
    },
    flushDone:     ()  => ipcRenderer.invoke("buddyOffice:flush-done"),
    onLoginOutput: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("buddyOffice:login-output", h);
      return () => ipcRenderer.removeListener("buddyOffice:login-output", h);
    },
    createSession:  (cwd, role, project, resumeId, model, convId) => ipcRenderer.invoke("buddyOffice:create", { cwd, role, project, resumeId, model, convId: convId || null }),
    run:            (id, task, model, convId) => ipcRenderer.send("buddyOffice:run", { id, task, model, convId: convId || null }),
    respondConfirm: (id, confirmId, answer) => ipcRenderer.send("buddyOffice:confirm", { id, confirmId, answer }),
    interrupt:      (id)                    => ipcRenderer.send("buddyOffice:interrupt", { id }),
    closeSession:   (id)                    => ipcRenderer.send("buddyOffice:close", { id }),
    setModel:         (id, model) => ipcRenderer.invoke("buddyOffice:set-model", { id, model }),
    setPermissionMode:(id, mode)  => ipcRenderer.invoke("buddyOffice:set-permission-mode", { id, mode }),
    contextUsage:     (id)        => ipcRenderer.invoke("buddyOffice:context-usage", { id }),
    onEvent: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("buddyOffice:event", h);
      return () => ipcRenderer.removeListener("buddyOffice:event", h);
    },
  },
});
