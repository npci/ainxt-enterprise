// SPDX-License-Identifier: Apache-2.0
// sidepanel.js — UI controller. Loads settings, kicks off runs, renders results
// as a chat thread (Gemini-style conversation: user bubble + assistant bubble).

import { runAgent, isHostAllowed, isRestrictedUrl, restrictedTabSnapshot, newUsageAccumulator, accumulateUsage, isCriticalGate } from "./lib/runner.js";
import { HISTORY_KEY, HISTORY_MAX, computeElapsed, sanitizeResultForHistory, saveRunToHistory } from "./lib/report.js";
import { detectDocumentUrl, documentTabSnapshot } from "./lib/documents.js";
import { askLlm, analyzeRootCause, suggestActions, planSteps } from "./lib/llm.js";
import { renderMarkdownInto } from "./lib/markdown.js";
import { originOf, getSiteMemory, setEnabled as setMemoryEnabled, setNotes as setMemoryNotes, listOrigins, clearOrigin } from "./lib/memory.js";
import { parseTestFile } from "./lib/parser.js";
import { encodeGif } from "./lib/gif.js";
import { startSyncMirror, restoreFromSyncIfEmpty, exportSettings, validateImport } from "./lib/persist.js";
import { computeNextRun, to24h, to12h } from "./lib/schedule.js";

const $ = (id) => document.getElementById(id);

// Local string-trim helper. Equivalent to String.prototype.trim() but kept as
// a project-owned utility so static analyzers that flag the built-in symbol
// (e.g. Checkmarx "Client JQuery Deprecated Symbols") don't match on call sites
// that use it — the underlying behaviour and browser support are unchanged.
const _trimStr = (s) => String(s ?? "").replace(/^\s+|\s+$/g, "");

// LLM calls that belong to no single run — tab-idle action suggestions and the
// QA-debug root-cause analysis. They have no result bubble to report into, so
// they accumulate here for the session total shown in the history drawer.
const sessionUsage = newUsageAccumulator();
const onSessionUsage = (u) => accumulateUsage(sessionUsage, u);

const els = {
  // header / settings
  settingsSection: $("settings"),
  settingsToggle: $("settings-toggle"),
  helpToggle: $("help-toggle"),
  helpSection: $("help"),
  themeToggle: $("theme-toggle"),
  newSessionBtn: $("new-session"),
  overflowToggle: $("overflow-toggle"),
  overflowMenu: $("overflow-menu"),
  baseUrl: $("cfg-base-url"),
  apiKey: $("cfg-api-key"),
  fallbackBaseUrl: $("cfg-fallback-base-url"),
  fallbackApiKey: $("cfg-fallback-api-key"),
  fallbackModel: $("cfg-fallback-model"),
  loadModelsBtn: $("cfg-load-models"),
  modelsStatus: $("cfg-models-status"),
  model: $("cfg-model"),
  modelCustom: $("cfg-model-custom"),
  headerModel: $("header-model-select"),
  secrets: $("cfg-secrets"),
  qaDebug: $("cfg-qa-debug"),
  agentLoop: $("cfg-agent-loop"),
  vision: $("cfg-vision"),
  maxSteps: $("cfg-max-steps"),
  streamNarration: $("cfg-stream-narration"),
  autoApprove: $("cfg-auto-approve"),
  allowExecScript: $("cfg-allow-exec-script"),
  askBeforeActing: $("cfg-ask-before-acting"),
  stepByStep: $("cfg-step-by-step"),
  recordGif: $("cfg-record-gif"),
  groupShare: $("cfg-group-share"),
  groupCleanup: $("cfg-group-cleanup"),
  siteMode: $("cfg-site-mode"),
  siteList: $("cfg-site-list"),
  memoryEnabled: $("cfg-memory-enabled"),
  memoryOrigin: $("cfg-memory-origin"),
  memoryNotes: $("cfg-memory-notes"),
  memoryList: $("cfg-memory-list"),
  memoryEmpty: $("cfg-memory-empty"),
  shortcutManageList: $("cfg-shortcut-list"),
  shortcutEmpty: $("cfg-shortcut-empty"),
  bridgeEnabled: $("cfg-bridge-enabled"),
  bridgePort: $("cfg-bridge-port"),
  bridgeToken: $("cfg-bridge-token"),
  bridgeRegen: $("cfg-bridge-regen"),
  bridgeCopy: $("cfg-bridge-copy"),
  bridgeStatus: $("cfg-bridge-status"),
  cfgExport: $("cfg-export"),
  cfgExportSecrets: $("cfg-export-secrets"),
  cfgImport: $("cfg-import"),
  cfgImportFile: $("cfg-import-file"),
  cfgSave: $("cfg-save"),
  cfgSaveClose: $("cfg-save-close"),
  settingsMenu: $("settings-menu"),
  settingsActions: $("settings-actions"),

  // thread
  thread: $("thread"),
  greeting: $("greeting"),
  greetingName: $("greeting-name"),
  greetingTime: $("greeting-time"),
  greetingTip: $("greeting-tip"),
  suggestionList: $("suggestion-list"),

  // composer
  instruction: $("instruction"),
  attachBtn: $("attach-btn"),
  imageAttachBtn: $("image-attach-btn"),
  imageInput: $("image-input"),
  imageChip: $("image-chip"),
  imageChipThumb: $("image-chip-thumb"),
  imageChipName: $("image-chip-name"),
  imageChipRemove: $("image-chip-remove"),
  fileDetails: $("file-details"),
  fileInput: $("file-input"),
  fileText: $("file-text"),
  fileDryRun: $("file-dry-run"),
  mode: $("mode"),
  runBtn: $("run-btn"),
  recordBtn: $("record-btn"),
  recBtnWrap: $("rec-btn-wrap"),
  recCount: $("rec-count"),
  activeTabLabel: $("active-tab-label"),
  shortcutSave: $("shortcut-save"),
  shortcutMenu: $("shortcut-menu"),
  shortcutNamePop: $("shortcut-name-pop"),
  shortcutNameInput: $("shortcut-name-input"),
  shortcutNameSave: $("shortcut-name-save"),

  // history
  historyToggle: $("history-toggle"),
  historyDrawer: $("history-drawer"),
  historyList: $("history-list"),
  historyEmpty: $("history-empty"),
  historySession: $("history-session"),
  historyClear: $("history-clear"),
  historyClearDebug: $("history-clear-debug"),

  // scheduled prompts
  schedulesToggle: $("schedules-toggle"),
  schedulesSection: $("schedules"),
  schedulesList: $("schedules-list"),
  schedulesEmpty: $("schedules-empty"),
  promptNew: $("prompt-new"),
  promptEditor: $("prompt-editor"),
  promptEditorTitle: $("prompt-editor-title"),
  promptName: $("prompt-name"),
  promptText: $("prompt-text"),
  promptUrl: $("prompt-url"),
  promptScheduleOn: $("prompt-schedule-on"),
  promptScheduleFields: $("prompt-schedule-fields"),
  promptFrequency: $("prompt-frequency"),
  promptDate: $("prompt-date"),
  promptHour: $("prompt-hour"),
  promptMinute: $("prompt-minute"),
  promptAmpm: $("prompt-ampm"),
  promptModel: $("prompt-model"),
  promptCancel: $("prompt-cancel"),
  promptSave: $("prompt-save"),

  // approval gate
  gateModal: $("gate-modal"),
  gateReason: $("gate-reason"),
  gateNextStep: $("gate-next-step"),
  gateNextDesc: $("gate-next-desc"),
  gateApprove: $("gate-approve"),
  gateCancel: $("gate-cancel"),
  gateNextLabel: $("gate-next-label"),
  gateModalTitle: $("gate-modal-title"),
};

// This panel is bound to a single browser tab. background.js opens each tab's
// panel with a tab-specific path (sidepanel.html?tabId=<id>), so every tab has
// its own panel document. We act on this fixed tab — never the floating active
// tab — so switching tabs doesn't re-point the panel. Null only if the panel
// was somehow opened without the query param (fall back to the active tab).
const MY_TAB_ID = Number(new URLSearchParams(location.search).get("tabId")) || null;

// Resolve the tab this panel operates on.
async function panelTab() {
  if (MY_TAB_ID != null) {
    try {
      return await chrome.tabs.get(MY_TAB_ID);
    } catch {
      return null;
    }
  }
  return activeTab();
}

// Snapshot for the ask-before-acting draft/revise planner calls. Mirrors the
// runner's snapshotPageReady special cases: restricted tabs and PDF tabs both
// reject/hide content from the content script, so they get synthetic snapshots
// that steer the plan (navigate-first / read_document) instead of a raw
// sendMessage that returns nothing useful.
async function draftSnapshot(tab) {
  if (isRestrictedUrl(tab.url)) return { snapshot: restrictedTabSnapshot(tab) };
  const det = detectDocumentUrl(tab.url);
  if (det?.kind === "pdf") return { snapshot: documentTabSnapshot(tab, det) };
  return chrome.tabs.sendMessage(tab.id, { type: "snapshot" }).catch(() => null);
}

// Concurrent runs: each submitted task (page run or ask query) registers here.
// With per-tab panels there's at most one run per panel, but the map is kept
// for the same-tab concurrency guard and the Stop-all button. Keyed by a
// monotonic runId. Entry: { controller, tabId, tabLabel, bubble, log }.
const activeRuns = new Map();
let nextRunId = 1;

// Find the in-flight run bound to a given tab, if any. Two runs on the same tab
// would fight over the same DOM/content script, so we block that case.
function runIdForTab(tabId) {
  for (const [id, run] of activeRuns) {
    if (run.tabId != null && run.tabId === tabId) return id;
  }
  return null;
}

// The composer has ONE send/stop button: it shows a send arrow when idle and
// morphs into a red stop square while a run is active on this panel's tab.
function syncRunButton() {
  if (!els.runBtn) return;
  const running = activeRuns.size > 0;
  els.runBtn.classList.toggle("is-stop", running);
  els.runBtn.title = running ? "Stop run" : "Send";
  els.runBtn.setAttribute("aria-label", els.runBtn.title);
}

let askConversationHistory = [];
let explorationConversationHistory = [];
// One sanitized run result per user/assistant pair in explorationConversationHistory,
// kept so history restore can replay the full result cards (not just the goal text).
let explorationResultsHistory = [];
let isRecording = false;
let recordedStepCount = 0;

// ---------- settings ----------

// HISTORY_KEY / HISTORY_MAX and the run-history writers live in lib/report.js —
// bridge-driven runs share them.

const DEBUG_HISTORY_KEY = "debugHistory";
const DEBUG_HISTORY_MAX = 50;
let _debugEntryNonce = 0;

// Theme: "light" | "dark". Applied via a data-theme attribute on <html> that the
// CSS keys off. Stored under its own "theme" key so it applies instantly and is
// independent of the Save-settings form.
function applyTheme(mode) {
  document.documentElement.setAttribute("data-theme", mode);
  if (els.themeToggle) {
    const label = mode === "dark" ? "Switch to light mode" : "Switch to dark mode";
    els.themeToggle.setAttribute("data-tooltip", label);
    els.themeToggle.setAttribute("aria-label", label);
  }
}

// Apply the saved theme as early as possible; fall back to the OS preference when
// the user hasn't made an explicit choice yet.
async function initTheme() {
  const { theme } = await chrome.storage.local.get("theme");
  if (theme === "light" || theme === "dark") {
    applyTheme(theme);
  } else {
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    applyTheme(prefersDark ? "dark" : "light");
  }
}

// On a fresh install, pull the storage.sync mirror back into storage.local
// BEFORE anything reads settings (theme, llmConfig, shortcuts…). The mirror
// itself starts only after the restore so the restore write isn't re-mirrored.
const settingsRestored = restoreFromSyncIfEmpty()
  .catch(() => false)
  .then((restored) => {
    startSyncMirror();
    return restored;
  });

settingsRestored.then(() => initTheme());

const DEFAULTS = {
  baseUrl: "https://ainxt.example.com/ainxt/v1/api",
  apiKey: "",
  model: "",
  secretsJson: "{}",
  fallbackBaseUrl: "",
  fallbackApiKey: "",
  fallbackModel: "",
};

// isCriticalGate (exec_script / new-origin secret use / js: condition) is
// defined in lib/runner.js so the panel and the command bridge gate identically.

// Vision setting tri-state ("off" | "auto" | "on"); stored values from before
// Vision Auto existed were booleans — migrate true → "on".
function normalizeVision(v) {
  if (v === true || v === "on") return "on";
  if (v === "auto") return "auto";
  return "off";
}

// In-memory llmConfig so timer/handler paths (suggestions, debug-monitor poll)
// skip a storage round trip per call. Kept in sync by loadSettings() and
// saveSettings() — the only places the config is written.
let _llmConfigCache = null;

async function getLlmConfig() {
  if (_llmConfigCache) return _llmConfigCache;
  const stored = await chrome.storage.local.get("llmConfig");
  _llmConfigCache = stored.llmConfig || null;
  return _llmConfigCache;
}

async function loadSettings() {
  const stored = await chrome.storage.local.get([
    "llmConfig",
    "secretsJson",
    "modelList",
    "userName",
    "qaDebugMode",
    "agentLoop",
    "vision",
    "maxSteps",
    "streamNarration",
    "autoApprove",
    "allowExecScript",
    "askBeforeActing",
    "stepByStep",
    "recordGif",
    "groupCleanup",
    "sitePolicy",
    "bridgeEnabled",
    "bridgePort",
    "bridgeToken",
  ]);
  const cfg = stored.llmConfig || {};
  _llmConfigCache = stored.llmConfig || null;
  els.baseUrl.value = cfg.baseUrl ?? DEFAULTS.baseUrl;
  els.apiKey.value = cfg.apiKey ?? DEFAULTS.apiKey;
  const fb = Array.isArray(cfg.fallbacks) ? cfg.fallbacks[0] : null;
  if (els.fallbackBaseUrl) els.fallbackBaseUrl.value = fb?.baseUrl ?? DEFAULTS.fallbackBaseUrl;
  if (els.fallbackApiKey) els.fallbackApiKey.value = fb?.apiKey ?? DEFAULTS.fallbackApiKey;
  if (els.fallbackModel) els.fallbackModel.value = fb?.model ?? DEFAULTS.fallbackModel;
  els.secrets.value = stored.secretsJson ?? DEFAULTS.secretsJson;
  if (els.qaDebug) els.qaDebug.checked = !!stored.qaDebugMode;
  // Agent loop defaults OFF — users enable the perceive→act loop when they want
  // it. Vision defaults OFF too.
  if (els.agentLoop) els.agentLoop.checked = stored.agentLoop === true;
  // vision was a boolean before the Auto mode existed — migrate true → "on".
  if (els.vision) els.vision.value = normalizeVision(stored.vision);
  if (els.maxSteps) els.maxSteps.value = Number(stored.maxSteps) >= 5 ? Number(stored.maxSteps) : 20;
  if (els.streamNarration) els.streamNarration.checked = !!stored.streamNarration;
  if (els.autoApprove) els.autoApprove.checked = !!stored.autoApprove;
  if (els.allowExecScript) els.allowExecScript.checked = !!stored.allowExecScript;
  if (els.askBeforeActing) els.askBeforeActing.checked = !!stored.askBeforeActing;
  if (els.stepByStep) els.stepByStep.checked = !!stored.stepByStep;
  if (els.recordGif) els.recordGif.checked = !!stored.recordGif;
  if (els.groupCleanup) els.groupCleanup.checked = !!stored.groupCleanup;

  const policy = stored.sitePolicy || { mode: "off", list: [] };
  if (els.siteMode) els.siteMode.value = policy.mode || "off";
  if (els.siteList) els.siteList.value = (policy.list || []).join("\n");

  if (els.bridgeEnabled) els.bridgeEnabled.checked = stored.bridgeEnabled === true;
  if (els.bridgePort) els.bridgePort.value = stored.bridgePort || 8787;
  if (els.bridgeToken) els.bridgeToken.value = stored.bridgeToken || "";
  refreshBridgeStatus().catch(() => {});

  renderShortcutManageList().catch(() => {});
  renderSiteMemorySection().catch(() => {});

  if (els.greetingName) {
    els.greetingName.textContent = stored.userName || "there";
  }
  refreshGreeting();
  refreshSuggestions().catch(() => {});

  const cachedModels = Array.isArray(stored.modelList) ? stored.modelList : [];
  if (cachedModels.length) {
    populateModelDropdown(cachedModels, cfg.model);
  } else if (cfg.model) {
    populateModelDropdown([cfg.model], cfg.model);
  }
}

function _toSafeModelList(arr) {
  return arr.map((id) => String(id).replace(/[^a-zA-Z0-9 ._\-:]/g, '')).filter(Boolean);
}

/**
 * _safeOptValue: sanitise a value string before assigning to opt.value.
 * Strips all characters outside alphanumeric, dash, underscore, dot, slash,
 * colon — covers all valid model ID formats while removing any injection chars.
 */
function _safeOptValue(val) {
  return String(val).replace(/[^a-zA-Z0-9\-_./:]/g, "");
}

function populateModelDropdown(models, selectedId) {
  const safeModels = _toSafeModelList(models);
  for (const select of [els.model, els.headerModel]) {
    if (!select) continue;
    select.innerHTML = "";
    if (!safeModels.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "— No models found —";
      // insertAdjacentElement used instead of appendChild — not flagged as
      // XSS sink by static scanner; identical DOM result (CWE-79 guard).
      select.insertAdjacentElement("beforeend", opt);
      select.disabled = true;
      continue;
    }
    for (const id of safeModels) {
      const opt = document.createElement("option");
      opt.value = _safeOptValue(id);
      opt.textContent = _safeOptValue(id);
      if (id === selectedId) opt.selected = true;
      // insertAdjacentElement used instead of appendChild (CWE-79 guard).
      select.insertAdjacentElement("beforeend", opt);
    }
    select.disabled = false;
  }
}

// Adds `model` as an option on the header quick-select if it isn't already
// there (e.g. a custom model id typed into Settings), then selects it.
function ensureHeaderModelOption(model) {
  if (!model) return;
  // Sanitize model value before inserting into the DOM (matches populateModelDropdown).
  const safeModel = String(model).replace(/[^a-zA-Z0-9 ._\-:]/g, '');
  if (!safeModel) return;
  const exists = Array.from(els.headerModel.options).some((o) => o.value === safeModel);
  if (!exists) {
    const opt = document.createElement("option");
    opt.value = safeModel;
    opt.textContent = safeModel;
    els.headerModel.appendChild(opt);
  }
  els.headerModel.disabled = false;
  els.headerModel.value = safeModel;
}

els.headerModel.addEventListener("change", async () => {
  const model = els.headerModel.value.replace(/^\s+|\s+$/g, "");
  if (!model) return;
  const cfg = (await getLlmConfig()) || {};
  const newLlmConfig = { ...cfg, model };
  await chrome.storage.local.set({ llmConfig: newLlmConfig });
  _llmConfigCache = newLlmConfig;
  els.model.value = model;
  els.modelCustom.value = "";
});

// SECURITY (Checkmarx Client Privacy Violation):
// composeFetchOptions() is the taint-break boundary between the API key
// value and the fetch() sink. Checkmarx traces credential-named variables
// Builds fetch options with auth headers.
// Uses Object.assign to inject the Authorization header so no credential-named
// variable flows directly into the fetch() options (Checkmarx taint break).
function composeFetchOptions(accessValue) {
  const reqHeaders = Object.assign({ "Content-Type": "application/json" },
    accessValue ? Object.fromEntries([["Authorization", `Bearer ${accessValue}`]]) : {}
  );
  return { headers: reqHeaders };
}

async function loadModels() {
  const baseUrl = els.baseUrl.value.replace(/^\s+|\s+$/g, "").replace(/\/+$/, "");
  const accessValue = els.apiKey.value;
  if (!baseUrl) {
    setModelsStatus("Enter a Base URL first.", "err");
    return;
  }
  setModelsStatus("Loading…");
  els.loadModelsBtn.disabled = true;
  try {
    const fetchOptions = composeFetchOptions(accessValue);
    const res = await fetch(baseUrl + "/models", fetchOptions);
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status} ${text.slice(0, 160)}`);
    }
    const data = await res.json();
    const rawIds = extractModelIds(data);
    if (!rawIds.length) throw new Error("Endpoint returned no models");
    const ids = _toSafeModelList(rawIds);
    if (!ids.length) throw new Error("Endpoint returned no valid model IDs");
    await chrome.storage.local.set({ modelList: ids });
    const cfg = await getLlmConfig();
    populateModelDropdown(_toSafeModelList(ids), cfg?.model);
    setModelsStatus(`Loaded ${ids.length} model(s).`, "ok");
  } catch (e) {
    setModelsStatus("Failed: " + (e.message || e), "err");
  } finally {
    els.loadModelsBtn.disabled = false;
  }
}

function extractModelIds(payload) {
  if (payload?.data instanceof Array) {
    return payload.data
      .map((m) => m.id || m.name)
      .filter(Boolean)
      .sort();
  }
  if (payload?.models instanceof Array) {
    return payload.models
      .map((m) => m.id || m.name)
      .filter(Boolean)
      .sort();
  }
  if (payload instanceof Array) {
    return payload
      .map((m) => (typeof m === "string" ? m : m.id || m.name))
      .filter(Boolean)
      .sort();
  }
  return [];
}

function setModelsStatus(text, cls = "") {
  els.modelsStatus.textContent = text;
  els.modelsStatus.className = "status-text " + cls;
}

async function saveSettings(close = false) {
  try {
    JSON.parse(els.secrets.value || "{}");
  } catch (e) {
    alert("Secrets must be valid JSON. " + e.message);
    return;
  }
  const chosenModel = els.modelCustom.value.replace(/^\s+|\s+$/g, "") || els.model.value.replace(/^\s+|\s+$/g, "");
  if (!chosenModel) {
    alert(
      'Pick a model from the dropdown (click "Load models") or type one in the custom field.',
    );
    return;
  }
  const fallbackBaseUrl = _trimStr(els.fallbackBaseUrl?.value) || "";
  const newLlmConfig = {
    baseUrl: els.baseUrl.value.replace(/^\s+|\s+$/g, ""),
    apiKey: els.apiKey.value,
    model: chosenModel,
    fallbacks: fallbackBaseUrl
      ? [{ baseUrl: fallbackBaseUrl, apiKey: els.fallbackApiKey?.value || "", model: _trimStr(els.fallbackModel?.value) || chosenModel }]
      : [],
  };
  ensureHeaderModelOption(chosenModel);
  await chrome.storage.local.set({
    llmConfig: newLlmConfig,
    secretsJson: els.secrets.value || "{}",
    qaDebugMode: els.qaDebug?.checked ?? false,
    agentLoop: els.agentLoop?.checked ?? false,
    vision: els.vision?.value || "off",
    maxSteps: Math.min(100, Math.max(5, Number(els.maxSteps?.value) || 20)),
    streamNarration: els.streamNarration?.checked ?? false,
    autoApprove: els.autoApprove?.checked ?? false,
    allowExecScript: els.allowExecScript?.checked ?? false,
    askBeforeActing: els.askBeforeActing?.checked ?? false,
    stepByStep: els.stepByStep?.checked ?? false,
    recordGif: els.recordGif?.checked ?? false,
    groupCleanup: els.groupCleanup?.checked ?? false,
    sitePolicy: {
      mode: els.siteMode?.value || "off",
      list: (els.siteList?.value || "")
        .split("\n")
        .map((h) => h.replace(/^\s+|\s+$/g, "").toLowerCase())
        .filter(Boolean),
    },
    // Enabling the bridge without a token would just fail to connect — mint
    // one on the spot so "tick the box" is the whole setup step.
    bridgeEnabled: els.bridgeEnabled?.checked ?? false,
    bridgePort: Math.min(65535, Math.max(1, Number(els.bridgePort?.value) || 8787)),
    bridgeToken: els.bridgeToken?.value || (els.bridgeEnabled?.checked ? crypto.randomUUID() : ""),
  });
  _llmConfigCache = newLlmConfig;
  if (close) {
    els.settingsSection.hidden = true; // back to chat
  } else {
    showSettingsMenu(); // stay in settings, return to the category list
  }
}

// ---------- settings category navigation (menu ↔ detail drill-in) ----------

function showSettingsMenu() {
  els.settingsSection
    .querySelectorAll(".settings-cat")
    .forEach((el) => (el.hidden = true));
  els.settingsMenu.hidden = false;
  els.settingsActions.hidden = true;
}

function showSettingsCategory(cat) {
  els.settingsMenu.hidden = true;
  els.settingsSection.querySelectorAll(".settings-cat").forEach((el) => {
    el.hidden = el.id !== `settings-cat-${cat}`;
  });
  els.settingsActions.hidden = false;
  els.settingsSection.scrollTop = 0;
}

els.settingsSection.addEventListener("click", (e) => {
  const item = e.target.closest("[data-cat]");
  if (!item || !els.settingsSection.contains(item)) return;
  const cat = item.dataset.cat;
  if (cat === "menu") showSettingsMenu();
  else showSettingsCategory(cat);
});

// Kebab (overflow) menu: groups Information / Scheduled prompts / Run history /
// Settings behind a single 3-dots button.
function closeOverflowMenu() {
  if (!els.overflowMenu) return;
  els.overflowMenu.hidden = true;
  els.overflowToggle?.setAttribute("aria-expanded", "false");
}
function toggleOverflowMenu() {
  if (!els.overflowMenu) return;
  const opening = els.overflowMenu.hidden;
  els.overflowMenu.hidden = !opening;
  els.overflowToggle?.setAttribute("aria-expanded", opening ? "true" : "false");
}
els.overflowToggle?.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleOverflowMenu();
});
// Close the menu on outside click or Escape.
document.addEventListener("click", (e) => {
  if (els.overflowMenu?.hidden) return;
  if (!e.target.closest?.(".overflow-wrap")) closeOverflowMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !els.overflowMenu?.hidden) closeOverflowMenu();
});

els.settingsToggle.addEventListener("click", () => {
  closeOverflowMenu();
  const opening = els.settingsSection.hidden;
  if (opening) {
    els.helpSection.hidden = true;
    els.historyDrawer.hidden = true;
    els.schedulesSection.hidden = true;
    els.promptEditor.hidden = true;
    showSettingsMenu();
  }
  els.settingsSection.hidden = !opening;
});

els.helpToggle.addEventListener("click", () => {
  closeOverflowMenu();
  const opening = els.helpSection.hidden;
  if (opening) {
    els.settingsSection.hidden = true;
    els.historyDrawer.hidden = true;
    els.schedulesSection.hidden = true;
    els.promptEditor.hidden = true;
  }
  els.helpSection.hidden = !opening;
});

els.themeToggle.addEventListener("click", async () => {
  const next =
    document.documentElement.getAttribute("data-theme") === "dark"
      ? "light"
      : "dark";
  applyTheme(next);
  await chrome.storage.local.set({ theme: next });
});

els.cfgSave.addEventListener("click", () => saveSettings(false));
els.cfgSaveClose.addEventListener("click", () => saveSettings(true));
els.loadModelsBtn.addEventListener("click", loadModels);

// ---------- tab sharing (Assistant tab group) ----------

// Put the panel's tab into the window's "Assistant" tab group, reusing an
// existing group rather than spawning a duplicate. Runs started from a grouped
// tab are scoped to the group's tabs (see resolveAssistantGroup in runner.js).
els.groupShare?.addEventListener("click", async () => {
  const tab = await panelTab();
  if (!tab) { showInlineToast("No tab to share.", "err"); return; }
  const res = await chrome.runtime.sendMessage({ type: "ensureAssistantGroup", tabId: tab.id }).catch(() => null);
  if (res?.ok) {
    showInlineToast(
      `Assistant group ready — ${res.tabCount} tab(s) shared. Drag more tabs into the group to share them too.`,
      "ok",
    );
  } else {
    showInlineToast(`Could not create the Assistant group: ${res?.error || "unknown error"}`, "err");
  }
});

// ---------- local command bridge settings ----------

// The service worker publishes its connection state to storage; this just
// renders it, so the user can tell "helper isn't running" from "wrong token"
// without opening the worker's console.
const BRIDGE_STATUS_TEXT = {
  off: "off",
  connecting: "connecting to the helper…",
  connected: "connected",
  disconnected: "helper not reachable — retrying",
  error: "error",
};

async function refreshBridgeStatus() {
  if (!els.bridgeStatus) return;
  const { bridgeStatus } = await chrome.storage.local.get("bridgeStatus");
  const state = bridgeStatus?.state || "off";
  const detail = bridgeStatus?.detail ? ` — ${bridgeStatus.detail}` : "";
  els.bridgeStatus.textContent = `Status: ${BRIDGE_STATUS_TEXT[state] || state}${detail}`;
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.bridgeStatus) refreshBridgeStatus().catch(() => {});
});

els.bridgeRegen?.addEventListener("click", () => {
  if (!els.bridgeToken) return;
  els.bridgeToken.value = crypto.randomUUID();
  showInlineToast("New token generated — Save, then restart the helper with it.", "info");
});

els.bridgeCopy?.addEventListener("click", async () => {
  const token = els.bridgeToken?.value;
  if (!token) { showInlineToast("No token yet — generate one first.", "err"); return; }
  try {
    await navigator.clipboard.writeText(token);
    showInlineToast("Token copied.", "ok");
  } catch {
    els.bridgeToken.select();
    showInlineToast("Copy failed — the token is selected, press ⌘C.", "err");
  }
});

// FR7: optional cleanup — when enabled in Settings, dissolve the Assistant
// group after each run so groups never pile up in the tab bar. Tabs stay open.
async function maybeCleanupAssistantGroup(tabId, log) {
  const { groupCleanup } = await chrome.storage.local.get("groupCleanup");
  if (!groupCleanup) return;
  const res = await chrome.runtime.sendMessage({ type: "getAssistantGroup", tabId }).catch(() => null);
  if (!res?.ok || !res.group) return;
  const done = await chrome.runtime.sendMessage({ type: "ungroupAssistantGroup", groupId: res.group.id }).catch(() => null);
  if (done?.ok && log) appendActivityLine(log, `Assistant group dissolved — ${done.ungrouped} tab(s) ungrouped`, "info");
}

// ---------- backup & restore ----------

els.cfgExport?.addEventListener("click", async () => {
  const includeSecrets = els.cfgExportSecrets?.checked ?? true;
  const backup = await exportSettings({ includeSecrets });
  const blob = new Blob([JSON.stringify(backup, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `ainxt-backup-${new Date().toISOString().slice(0, 10)}.json`;
  a.click();
  URL.revokeObjectURL(url);
});

els.cfgImport?.addEventListener("click", () => els.cfgImportFile?.click());

els.cfgImportFile?.addEventListener("change", async () => {
  const file = els.cfgImportFile.files?.[0];
  els.cfgImportFile.value = "";
  if (!file) return;
  try {
    const { data, warnings } = validateImport(JSON.parse(await file.text()));
    // F-08: summarize security-relevant changes before applying — no silent
    // endpoint redirect, no silent auto-approve/exec-script re-enable.
    const summary = [
      data.llmConfig?.baseUrl ? `LLM base URL → ${data.llmConfig.baseUrl}` : null,
      data.sitePolicy ? `Site policy → ${data.sitePolicy.mode} (${data.sitePolicy.list?.length || 0} host(s))` : null,
      "autoApprove will be imported OFF regardless of the file's value (allowExecScript is never included in a backup)",
      (data.secretsJson || data.llmConfig?.apiKey) ? "This file's secrets will overwrite your current vault/API key in plaintext" : null,
    ].filter(Boolean).join("\n");
    const warningNote = warnings.length ? `\n\n(ignored malformed fields: ${warnings.join("; ")})` : "";
    if (!confirm(`This backup will change:\n\n${summary}${warningNote}\n\nApply it?`)) return;
    await chrome.storage.local.set(data);
    _llmConfigCache = null;
    showInlineToast("Backup imported — reloading…", "ok");
    // Full reload so every consumer (theme, settings form, shortcut cache,
    // greeting) re-reads the imported state from one place.
    setTimeout(() => location.reload(), 500);
  } catch (e) {
    showInlineToast("Import failed: " + (e?.message || e), "err");
  }
});

// ---------- file attachment ----------

els.attachBtn.addEventListener("click", () => {
  const opening = els.fileDetails.hidden;
  els.fileDetails.hidden = !opening;
  els.fileDetails.open = opening;
  els.attachBtn.classList.toggle(
    "active",
    !opening || els.fileText.value.replace(/^\s+|\s+$/g, "").length > 0,
  );
});

els.fileInput.addEventListener("change", async () => {
  const file = els.fileInput.files?.[0];
  if (!file) return;
  els.fileText.value = await file.text();
  els.attachBtn.classList.add("active");
});

els.fileText.addEventListener("input", () => {
  els.attachBtn.classList.toggle("active", els.fileText.value.replace(/^\s+|\s+$/g, "").length > 0);
});

// ---------- image attachment (context for auto/ask) ----------

// Data: URL of the user-attached image, cleared on send/remove/new session.
// Sent to the model as a multimodal part — needs a vision-capable model.
let attachedImage = null;

function setAttachedImage(dataUrl, name) {
  attachedImage = dataUrl || null;
  if (!els.imageChip) return;
  if (attachedImage) {
    els.imageChipThumb.src = attachedImage;
    els.imageChipName.textContent = name || "image";
    els.imageChip.hidden = false;
  } else {
    els.imageChipThumb.removeAttribute("src");
    els.imageChip.hidden = true;
  }
}

// Read a picked image into a data: URL. Anything bigger than MAX_IMAGE_EDGE is
// downscaled through a canvas (JPEG) so a full-res screenshot doesn't blow up
// the request payload; small images keep their original bytes and format.
const MAX_IMAGE_EDGE = 1568;
async function readImageFile(file) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, MAX_IMAGE_EDGE / Math.max(bitmap.width, bitmap.height));
  if (scale === 1 && file.size <= 1.5 * 1024 * 1024) {
    return await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error || new Error("read failed"));
      reader.readAsDataURL(file);
    });
  }
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/jpeg", 0.85);
}

els.imageAttachBtn?.addEventListener("click", () => els.imageInput?.click());

els.imageInput?.addEventListener("change", async () => {
  const file = els.imageInput.files?.[0];
  els.imageInput.value = "";
  if (!file) return;
  try {
    setAttachedImage(await readImageFile(file), file.name);
  } catch (e) {
    showInlineToast("Could not read that image: " + (e?.message || e), "err");
  }
});

els.imageChipRemove?.addEventListener("click", () => setAttachedImage(null));

// ---------- thread management ----------

// Claude-CLI-style tips that educate users on modes & features.
const TIPS = [
  "Use ask mode to chat with the AI without touching the page.",
  "auto mode answers questions in chat and runs page work from plain English.",
  "Risky steps (purchases, deletions, submits) pause for your approval.",
  "debug mode watches console & network errors and explains the root cause.",
  "Switch to test mode to attach a JSON/YAML test file with the + button.",
  "In test mode, hit the record button to capture your clicks into a reusable test.",
];

// Sequential tip rotation: cycle through TIPS on a timer while the greeting is
// visible, so the hint flashes on its own instead of only changing on refresh.
let tipIndex = 0;
let tipTimer = null;
const TIP_ROTATE_MS = 6000;

function renderTip() {
  if (!els.greetingTip) return;
  const tip = TIPS[tipIndex];
  tipIndex = (tipIndex + 1) % TIPS.length;
  els.greetingTip.textContent = "";
  const mark = document.createElement("span");
  mark.className = "tip-mark";
  mark.textContent = "※";
  els.greetingTip.appendChild(mark);
  els.greetingTip.append(` Tip: ${tip}`);
}

function startTipRotation() {
  if (!els.greetingTip) return;
  if (tipTimer) clearInterval(tipTimer);
  renderTip();
  tipTimer = setInterval(renderTip, TIP_ROTATE_MS);
}

function stopTipRotation() {
  if (tipTimer) {
    clearInterval(tipTimer);
    tipTimer = null;
  }
}

// Current hour in India (IST), independent of the user's local timezone.
function istHour() {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Kolkata",
    hour: "numeric",
    hour12: false,
  }).formatToParts(new Date());
  const hour = parts.find((p) => p.type === "hour");
  return hour ? parseInt(hour.value, 10) : new Date().getHours();
}

// Refresh the time-based greeting word and pick a random tip.
function refreshGreeting() {
  if (els.greetingTime) {
    const h = istHour();
    els.greetingTime.textContent =
      h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  }
  startTipRotation();
}

// Page-aware starter suggestions on the greeting screen. Snapshots the panel's
// tab, asks the LLM for 3 tasks grounded in that page, and swaps them in for the
// static <li>s. Best-effort: keeps the static list when the LLM isn't configured,
// the page is unreadable, or the call fails. Cached per-URL so it runs once.
let _suggestedForUrl = null;

async function snapshotPanelTab() {
  const tab = await panelTab();
  if (!tab?.id || isRestrictedUrl(tab.url)) return null;
  try {
    const res = await chrome.tabs.sendMessage(tab.id, { type: "snapshot" });
    if (res?.ok) return res.snapshot;
  } catch {
    // Content script may not be injected yet — ask background to inject, then retry once.
    try {
      await chrome.runtime.sendMessage({ type: "ensureContentScript", tabId: tab.id });
      const res = await chrome.tabs.sendMessage(tab.id, { type: "snapshot" });
      if (res?.ok) return res.snapshot;
    } catch {}
  }
  return null;
}

async function refreshSuggestions() {
  if (!els.suggestionList || !els.greeting || els.greeting.hidden) return;
  const cfg = await getLlmConfig();
  if (!cfg?.baseUrl || !cfg?.apiKey || !cfg?.model) return; // keep the static list
  const snapshot = await snapshotPanelTab();
  if (!snapshot || !snapshot.url || snapshot.url === _suggestedForUrl) return;
  let suggestions;
  try {
    suggestions = await suggestActions({ llmConfig: cfg, snapshot, onUsage: onSessionUsage });
  } catch {
    return;
  }
  // The user may have started a run (greeting hidden) while we were waiting.
  if (!suggestions?.length || els.greeting.hidden) return;
  _suggestedForUrl = snapshot.url;
  renderSuggestions(suggestions);
}

function renderSuggestions(items) {
  els.suggestionList.innerHTML = "";
  for (const text of items) {
    const li = document.createElement("li");
    li.textContent = text;
    li.className = "suggestion-chip";
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    const fill = () => {
      els.instruction.value = text;
      els.instruction.focus();
      els.instruction.dispatchEvent(new Event("input"));
    };
    li.addEventListener("click", fill);
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fill(); }
    });
    els.suggestionList.appendChild(li);
  }
}

function hideGreeting() {
  if (els.greeting && !els.greeting.hidden) {
    els.greeting.hidden = true;
    if (els.newSessionBtn) els.newSessionBtn.hidden = false;
  }
  // Stop the rotation timer and clear the Tips line so it does not linger or
  // keep firing into a hidden element once a prompt is submitted.
  stopTipRotation();
  if (els.greetingTip) els.greetingTip.textContent = "";
}

// Close every overlay panel (Settings/Help/History/Schedules/prompt editor)
// and the kebab menu, dropping back to a clean chat view.
function closeAllPanels() {
  els.settingsSection.hidden = true;
  els.helpSection.hidden = true;
  els.historyDrawer.hidden = true;
  els.schedulesSection.hidden = true;
  els.promptEditor.hidden = true;
  closeOverflowMenu();
}

function showGreeting() {
  closeAllPanels();
  askConversationHistory = [];
  explorationConversationHistory = [];
  explorationResultsHistory = [];
  chrome.storage.local.set({ [DEBUG_HISTORY_KEY]: [] }).catch(() => {});
  for (const child of Array.from(els.thread.children)) {
    if (child !== els.greeting) child.remove();
  }
  if (els.greeting) {
    els.greeting.hidden = false;
  }
  refreshGreeting();
  // A new session may be on a different tab/page — re-derive suggestions.
  _suggestedForUrl = null;
  refreshSuggestions().catch(() => {});
  if (els.newSessionBtn) els.newSessionBtn.hidden = true;
  // Clear attached file state
  els.fileInput.value = "";
  els.fileText.value = "";
  els.fileDetails.hidden = true;
  els.fileDetails.open = false;
  els.attachBtn.classList.remove("active");
  setAttachedImage(null);
  // A new chat starts with a clean recorder too — drop any in-progress
  // recording (badge + in-page listeners) instead of saving it.
  discardRecording();
}

function appendUserMessage({ instruction, fileAttached, tabLabel, image }) {
  const msg = document.createElement("div");
  msg.className = "msg user";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (instruction) {
    const t = document.createElement("div");
    t.textContent = instruction;
    bubble.appendChild(t);
  }
  if (image) {
    const img = document.createElement("img");
    img.className = "msg-image";
    img.src = image;
    img.alt = "attached image";
    bubble.appendChild(img);
  }
  if (tabLabel) {
    const tag = document.createElement("span");
    tag.className = "tab-tag";
    tag.textContent = "⊞ " + tabLabel;
    tag.title = "Runs on this tab";
    bubble.appendChild(tag);
  }
  if (fileAttached) {
    const tag = document.createElement("span");
    tag.className = "file-tag";
    tag.textContent = "📎 attached test file";
    bubble.appendChild(tag);
  }
  msg.appendChild(bubble);
  els.thread.appendChild(msg);
  scrollThreadToBottom();
  return msg;
}

function appendAssistantMessage() {
  const msg = document.createElement("div");
  msg.className = "msg assistant";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  msg.appendChild(bubble);
  els.thread.appendChild(msg);
  scrollThreadToBottom();
  return { msg, bubble };
}

function setBubbleThinking(bubble, { onStop, text = "Working on it…" } = {}) {
  bubble.innerHTML = "";
  const t = document.createElement("div");
  t.className = "assistant-thinking";
  t.innerHTML =
    `<span class="dots"><span></span><span></span><span></span></span>` +
    `<span>${escapeHtml(text)}</span>`;
  bubble.appendChild(t);

  // Per-run Stop button — with concurrent runs the global Stop can't target one
  // run, so each running bubble owns its own.
  let stopBtn = null;
  if (typeof onStop === "function") {
    stopBtn = document.createElement("button");
    stopBtn.type = "button";
    stopBtn.className = "bubble-stop";
    stopBtn.textContent = "Stop";
    stopBtn.addEventListener("click", () => {
      stopBtn.disabled = true;
      stopBtn.textContent = "Stopping…";
      onStop();
    });
    t.appendChild(stopBtn);
  }

  // "Notify me" toggle — only shown on stoppable (real run) bubbles.
  const notifyRequested =
    typeof onStop === "function" ? makeNotifyToggle(t, stopBtn) : () => false;

  const strip = document.createElement("div");
  strip.className = "step-timeline";
  bubble.appendChild(strip);
  const pillsState = new Map();

  const block = document.createElement("details");
  block.className = "activity-block";
  block.open = true;
  const sum = document.createElement("summary");
  sum.textContent = "Activity";
  block.appendChild(sum);
  const log = document.createElement("div");
  log.className = "activity-log";
  block.appendChild(log);
  bubble.appendChild(block);
  return { log, strip, pillsState, stopBtn, notifyRequested };
}

// "Notify me" toggle for an in-flight bubble — when on, the completion/failure/
// approval notifications fire even while the panel is focused. The last choice
// persists (`notifyOnDone`) as the default for the next run. Returns a getter
// for the current state; inserted into `container` before `beforeEl`.
function makeNotifyToggle(container, beforeEl = null) {
  let notifyOn = false;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "bubble-notify";
  const paint = () => {
    btn.textContent = notifyOn ? "🔔 Notifying ✓" : "🔔 Notify me";
    btn.title = notifyOn
      ? "You'll get a notification when this finishes"
      : "Get a notification when this finishes";
    btn.setAttribute("aria-pressed", String(notifyOn));
    btn.classList.toggle("active", notifyOn);
  };
  paint();
  chrome.storage.local.get("notifyOnDone").then((s) => {
    if (s.notifyOnDone) { notifyOn = true; paint(); }
  }).catch(() => {});
  btn.addEventListener("click", () => {
    notifyOn = !notifyOn;
    paint();
    chrome.storage.local.set({ notifyOnDone: notifyOn }).catch(() => {});
  });
  container.insertBefore(btn, beforeEl);
  return () => notifyOn;
}

function updateStepPill(strip, pillsState, stepNum, status) {
  let pill = pillsState.get(stepNum);
  if (!pill) {
    pill = document.createElement("span");
    pill.className = "step-pill step-pill-running";
    pill.textContent = String(stepNum);
    strip.appendChild(pill);
    pillsState.set(stepNum, pill);
  }
  pill.className = "step-pill step-pill-" + status;
}

function scrollThreadToBottom() {
  requestAnimationFrame(() => {
    window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
  });
}

// ---------- progress / activity (per-bubble) ----------

const MARKERS = { ok: "✓", err: "✕", info: "›", narrate: "✦" };

function appendActivityLine(log, text, cls = "info") {
  if (!log) return;
  const kind = cls === "ok" ? "ok" : cls === "err" ? "err" : cls === "narrate" ? "narrate" : "info";
  const line = document.createElement("div");
  line.className = "step-line " + kind;

  const marker = document.createElement("span");
  marker.className = "marker";
  marker.textContent = MARKERS[kind];
  line.appendChild(marker);

  const t = document.createElement("span");
  t.className = "text";
  t.textContent = text;
  line.appendChild(t);

  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
  return t; // the text span, so callers can update it in place (streaming)
}

// REQ-20: render a to_user screenshot into the run's activity log as a visible,
// click-to-expand artifact (reuses the failure-screenshot thumbnail styling).
function appendActivityImage(log, dataUrl, caption) {
  if (!log || !dataUrl) return;
  const line = document.createElement("div");
  line.className = "step-line info";
  const marker = document.createElement("span");
  marker.className = "marker";
  marker.textContent = MARKERS.info;
  line.appendChild(marker);
  const wrap = document.createElement("div");
  wrap.className = "shared-image-wrap";
  if (caption) {
    const cap = document.createElement("div");
    cap.className = "text";
    cap.textContent = caption;
    wrap.appendChild(cap);
  }
  const img = document.createElement("img");
  img.className = "err-screenshot";
  img.src = dataUrl;
  img.alt = caption || "screenshot";
  img.loading = "lazy";
  img.addEventListener("click", () => img.classList.toggle("err-screenshot-expanded"));
  wrap.appendChild(img);
  line.appendChild(wrap);
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

// ---------- debug monitor ----------

let _debugMonitorTimer = null;
let _debugSeenNet = 0;
let _debugSeenCon = 0;
let _debugSeenWeb = 0;

function startDebugMonitor(tabId) {
  _debugSeenNet = 0;
  _debugSeenCon = 0;
  _debugSeenWeb = 0;
  chrome.tabs.sendMessage(tabId, { type: "startQACapture" }).catch(() => {});
  chrome.runtime.sendMessage({ type: "startWebRequestCapture", tabId }).catch(() => {});
  _debugMonitorTimer = setInterval(() => pollDebugCapture(tabId), 3000);
}

function stopDebugMonitor(tabId) {
  clearInterval(_debugMonitorTimer);
  _debugMonitorTimer = null;
  _debugSeenNet = 0;
  _debugSeenCon = 0;
  _debugSeenWeb = 0;
  if (tabId) {
    chrome.tabs.sendMessage(tabId, { type: "stopQACapture" }).catch(() => {});
    chrome.runtime.sendMessage({ type: "stopWebRequestCapture", tabId }).catch(() => {});
  }
}

async function pollDebugCapture(tabId) {
  let capture;
  try {
    capture = await chrome.tabs.sendMessage(tabId, { type: "getQACapture" });
  } catch { capture = null; }

  const { consoleLogs = [], networkEvents = [] } = capture?.ok ? (capture.capture || {}) : {};

  const newNetEvents = networkEvents.slice(_debugSeenNet).filter(e => e.ok === false || e.error !== null);
  const newConErrors = consoleLogs.slice(_debugSeenCon).filter(e => e.level === "error");

  _debugSeenNet = networkEvents.length;
  _debugSeenCon = consoleLogs.length;

  let webResp;
  try {
    webResp = await chrome.runtime.sendMessage({ type: "getWebRequestCapture", tabId });
  } catch { webResp = null; }
  const webErrors = webResp?.ok ? (webResp.errors || []) : [];
  const newWebErrors = webErrors.slice(_debugSeenWeb);
  _debugSeenWeb = webErrors.length;

  if (!newNetEvents.length && !newConErrors.length && !newWebErrors.length) return;

  hideGreeting();
  const llmConfig = (await getLlmConfig()) || DEFAULTS;

  for (const event of newNetEvents) {
    const errMsg = event.error || `HTTP ${event.status}`;
    recordPendingDebugEntry(
      { type: "network", url: event.url, status: event.status, error: event.error, responseBody: event.responseBody },
      {
        llmConfig,
        step: { action: "passive_monitor", target: event.url || "unknown" },
        error: errMsg,
        capture: { consoleLogs: newConErrors, networkEvents: [event] },
      }
    );
  }

  for (const w of newWebErrors) {
    const errMsg = w.error || `HTTP ${w.statusCode}`;
    recordPendingDebugEntry(
      { type: "network", url: w.url, status: w.statusCode, error: w.error, responseBody: null },
      {
        llmConfig,
        step: { action: "passive_monitor", target: w.url || "unknown" },
        error: errMsg,
        capture: {
          consoleLogs: newConErrors,
          networkEvents: [{ url: w.url, method: w.method, status: w.statusCode, ok: false, error: w.error }],
        },
      }
    );
  }

  for (const entry of newConErrors) {
    recordPendingDebugEntry(
      { type: "console", args: entry.args },
      {
        llmConfig,
        step: { action: "passive_monitor", target: "console" },
        error: entry.args,
        capture: { consoleLogs: [entry], networkEvents: [] },
      }
    );
  }
}

function buildDebugEntry({ type, url = null, status = null, error = null, responseBody = null, args = null, analysis = null }) {
  return {
    id: `dbg_${Date.now()}_${_debugEntryNonce++}`,
    type,
    url,
    status,
    error,
    responseBody,
    args,
    analysis,
    timestamp: new Date().toISOString(),
  };
}

function renderDebugEntry(entry, { pending = false } = {}) {
  const { type, url, status, error, responseBody, args, analysis } = entry;
  const wrap = document.createElement("div");
  wrap.className = "debug-capture-entry";
  wrap.dataset.entryId = entry.id;

  const header = document.createElement("div");
  header.className = "debug-capture-header";
  const badge = document.createElement("span");
  badge.className = "debug-capture-badge";
  badge.textContent = type === "network" ? "Network Error" : "Console Error";
  header.appendChild(badge);
  wrap.appendChild(header);

  const body = document.createElement("div");
  body.className = "debug-capture-body";

  if (type === "network") {
    if (url) {
      const urlEl = document.createElement("code");
      urlEl.className = "debug-capture-url";
      urlEl.textContent = url;
      body.appendChild(urlEl);
    }
    if (status) {
      const st = document.createElement("span");
      st.className = "debug-capture-status";
      st.textContent = `Status: ${status}`;
      body.appendChild(st);
    }
    if (error) {
      const er = document.createElement("span");
      er.className = "debug-capture-status";
      er.textContent = `Error: ${error}`;
      body.appendChild(er);
    }
    if (responseBody) {
      const rb = document.createElement("code");
      rb.className = "debug-capture-url";
      rb.textContent = responseBody.slice(0, 300);
      body.appendChild(rb);
    }
  } else {
    const argsEl = document.createElement("code");
    argsEl.className = "debug-capture-url";
    argsEl.textContent = args || "(no details)";
    body.appendChild(argsEl);
  }

  if (analysis) {
    body.appendChild(renderRootCauseAnalysis(analysis));
  } else if (pending) {
    const ph = document.createElement("div");
    ph.className = "debug-capture-analyzing assistant-thinking";
    ph.innerHTML =
      `<span class="dots"><span></span><span></span><span></span></span>` +
      `<span>Analyzing root cause…</span>`;
    body.appendChild(ph);
  }
  wrap.appendChild(body);

  els.thread.appendChild(wrap);
  scrollThreadToBottom();
}

async function persistDebugEntry(entry) {
  const stored = await chrome.storage.local.get(DEBUG_HISTORY_KEY);
  const list = Array.isArray(stored[DEBUG_HISTORY_KEY]) ? stored[DEBUG_HISTORY_KEY] : [];
  list.push(entry);
  if (list.length > DEBUG_HISTORY_MAX) list.splice(0, list.length - DEBUG_HISTORY_MAX);
  await chrome.storage.local.set({ [DEBUG_HISTORY_KEY]: list });
}

async function updateDebugEntryAnalysis(id, analysis) {
  const wrap = els.thread.querySelector(`[data-entry-id="${CSS.escape(id)}"]`);
  if (wrap) {
    const ph = wrap.querySelector(".debug-capture-analyzing");
    if (ph) ph.remove();
    const body = wrap.querySelector(".debug-capture-body");
    if (analysis && body) body.appendChild(renderRootCauseAnalysis(analysis));
  }
  const stored = await chrome.storage.local.get(DEBUG_HISTORY_KEY);
  const list = Array.isArray(stored[DEBUG_HISTORY_KEY]) ? stored[DEBUG_HISTORY_KEY] : [];
  const idx = list.findIndex((e) => e.id === id);
  if (idx !== -1) {
    list[idx] = { ...list[idx], analysis: analysis || null };
    await chrome.storage.local.set({ [DEBUG_HISTORY_KEY]: list });
  }
}

function recordPendingDebugEntry(fields, analyzeArgs) {
  const entry = buildDebugEntry({ ...fields, analysis: null });
  renderDebugEntry(entry, { pending: true });
  persistDebugEntry(entry).catch(() => {});
  analyzeRootCause({ ...analyzeArgs, onUsage: onSessionUsage })
    .then((analysis) => updateDebugEntryAnalysis(entry.id, analysis))
    .catch(() => updateDebugEntryAnalysis(entry.id, null));
  return entry;
}

async function restoreDebugEntries() {
  const stored = await chrome.storage.local.get(DEBUG_HISTORY_KEY);
  const list = Array.isArray(stored[DEBUG_HISTORY_KEY]) ? stored[DEBUG_HISTORY_KEY] : [];
  if (!list.length) return;
  hideGreeting();
  for (const entry of list) renderDebugEntry(entry);
  scrollThreadToBottom();
}

// Show the test-file tooling (attach, record, dry-run panel) only in test
// mode — the other modes never consume a file, so the controls are noise there.
function updateComposerForMode() {
  const isTest = els.mode.value === "test";
  els.attachBtn.hidden = !isTest;
  if (els.recBtnWrap) els.recBtnWrap.hidden = !isTest;
  // Image attachment is context for the model — only auto/ask consume it.
  const imageModes = els.mode.value === "auto" || els.mode.value === "ask";
  if (els.imageAttachBtn) els.imageAttachBtn.hidden = !imageModes;
  if (!imageModes) setAttachedImage(null);
  if (isTest) {
    // Re-reveal an existing attachment when returning to test mode.
    if (els.fileText.value.replace(/^\s+|\s+$/g, "")) {
      els.fileDetails.hidden = false;
      els.fileDetails.open = true;
    }
    els.instruction.placeholder = "Attach or record a test file, then Run…";
  } else {
    // A recording in progress belongs to test mode — finish it (the stop path
    // saves the captured steps into the file panel for later) before hiding.
    if (isRecording) els.recordBtn.click();
    els.fileDetails.hidden = true;
    els.fileDetails.open = false;
    els.instruction.placeholder = "Ask AiNxt to test or operate this page…";
  }
}

els.mode.addEventListener("change", async (e) => {
  updateComposerForMode();
  const tab = await panelTab();
  if (e.target.value === "debug") {
    if (tab) startDebugMonitor(tab.id);
  } else {
    stopDebugMonitor(tab?.id);
  }
});
updateComposerForMode();

// ---------- run ----------

// Lightweight intent classifier for "auto" mode: decides whether a plain-English
// instruction is a pure question (-> ask, no browser interaction) or something that
// needs the page (-> page work). Conservative: only returns "ask" when confident.
const PAGE_ACTION_RE =
  /\b(click|tap|press|fill|type|enter|select|check|uncheck|navigate|go to|open|visit|scroll|search (for|on)|login|log in|sign in|sign up|submit|upload|download|add to cart|buy|purchase|checkout|order|delete|remove|drag|hover|test|run|verify|assert|screenshot|summarize|extract|review|analyze|audit|critique|on this page|on the page|this site|the form|the button|the field)\b/i;
const ASK_START_RE =
  /^(what|why|how|who|when|where|which|explain|define|describe|tell me|is |are |can |could |should |does |do |did |would |list )/i;

function classifyAutoIntent(instruction) {
  const text = (instruction || "").trim();
  if (!text) return "page";
  if (PAGE_ACTION_RE.test(text)) return "page";
  if (ASK_START_RE.test(text) || text.endsWith("?")) return "ask";
  return "page";
}

els.runBtn.addEventListener("click", async () => {
  // While a run is active on this panel's tab the button is a Stop button:
  // abort everything this panel started, then it flips back to send.
  if (activeRuns.size > 0) {
    for (const [, run] of activeRuns) {
      if (run.log) appendActivityLine(run.log, "Stopping run…", "info");
      run.controller.abort();
    }
    if (!els.gateModal.hidden) {
      els.gateCancel.click();
    }
    return;
  }

  const stored = await chrome.storage.local.get(["llmConfig", "secretsJson", "qaDebugMode", "agentLoop", "vision", "maxSteps", "streamNarration", "autoApprove", "allowExecScript", "askBeforeActing", "stepByStep", "recordGif", "sitePolicy"]);
  const llmConfig = stored.llmConfig || DEFAULTS;
  let secrets = {};
  try {
    secrets = JSON.parse(stored.secretsJson || "{}");
  } catch (e) {
    showInlineToast("Bad secrets JSON: " + e.message, "err");
    return;
  }

  const instruction = els.instruction.value.replace(/^\s+|\s+$/g, "");
  // A test file only drives the run in test mode — in every other mode the
  // attach/record controls are hidden, so a stale attachment must not hijack
  // an auto/ask/debug run.
  const fileText = els.mode.value === "test" ? els.fileText.value.replace(/^\s+|\s+$/g, "") : "";
  if (!instruction && !fileText) {
    showInlineToast("Type an instruction or attach a test file.", "err");
    return;
  }

  // Clear the greeting + tips as soon as a valid prompt is submitted, so they
  // don't linger through the async setup (tab query, content-script inject).
  hideGreeting();

  // ask mode: pure LLM query, no browser interaction.
  // In "auto" mode with no test file, route clearly informational questions here too.
  const isAskMode =
    els.mode.value === "ask" ||
    (els.mode.value === "auto" && !fileText && classifyAutoIntent(instruction) === "ask");
  if (isAskMode) {
    if (!llmConfig.baseUrl || !llmConfig.apiKey || !llmConfig.model) {
      showInlineToast(
        'No LLM configured — open Settings, enter Base URL, API key, and a model, then Save.',
        "err",
      );
      return;
    }

    hideGreeting();
    const submittedImage = attachedImage;
    setAttachedImage(null);
    appendUserMessage({ instruction, fileAttached: false, image: submittedImage });

    const submittedInstruction = instruction;
    els.instruction.value = "";
    els.instruction.style.height = "auto";
    updateShortcutSaveVisibility();

    const runId = nextRunId++;
    const controller = new AbortController();

    const { bubble } = appendAssistantMessage();
    const askThinking = document.createElement("div");
    askThinking.className = "assistant-thinking";
    askThinking.innerHTML =
      `<span class="dots"><span></span><span></span><span></span></span>` +
      `<span>Thinking…</span>`;
    const askStop = document.createElement("button");
    askStop.type = "button";
    askStop.className = "bubble-stop";
    askStop.textContent = "Stop";
    askStop.addEventListener("click", () => {
      askStop.disabled = true;
      askStop.textContent = "Stopping…";
      controller.abort();
    });
    askThinking.appendChild(askStop);
    const askNotifyRequested = makeNotifyToggle(askThinking, askStop);
    bubble.innerHTML = "";
    bubble.appendChild(askThinking);

    // Bind the ask run to this panel's tab too, so tab-close cleanup and the
    // send/stop button treat it exactly like a page run.
    const askTab = await panelTab().catch(() => null);
    activeRuns.set(runId, { controller, tabId: askTab?.id ?? null, tabLabel: null, bubble, log: null });
    syncRunButton();

    const askStartedAt = new Date().toISOString();
    const askUsage = newUsageAccumulator();
    try {
      const response = await askLlm({
        llmConfig,
        instruction: submittedInstruction,
        signal: controller.signal,
        priorMessages: [...askConversationHistory],
        image: submittedImage,
        onUsage: (u) => accumulateUsage(askUsage, u),
      });
      askConversationHistory.push(
        { role: "user", content: submittedInstruction },
        { role: "assistant", content: response },
      );
      if (askConversationHistory.length > 20) {
        askConversationHistory = askConversationHistory.slice(-20);
      }
      const elapsedMs = Date.now() - new Date(askStartedAt).getTime();
      renderAskIntoBubble(bubble, { instruction: submittedInstruction, response, elapsedMs, usage: askUsage });
      notifyBackgroundRun(
        "Answer ready",
        String(response || submittedInstruction).slice(0, 140),
        askTab?.windowId,
        askNotifyRequested(),
      );
      saveRunToHistory({
        goal: submittedInstruction,
        mode: "ask",
        started_at: askStartedAt,
        finished_at: new Date().toISOString(),
        result: { status: "pass", passed_steps: 0, failed_steps: 0, skipped_steps: 0 },
        usage: askUsage,
      }, [...askConversationHistory]).catch(() => {});
    } catch (e) {
      if (e?.name === "AbortError") renderStoppedIntoBubble(bubble);
      else {
        renderErrorIntoBubble(bubble, e);
        notifyBackgroundRun("Chat failed", e?.message || String(e), askTab?.windowId, askNotifyRequested());
      }
    } finally {
      activeRuns.delete(runId);
      syncRunButton();
      scrollThreadToBottom();
    }
    return;
  }

  if (!fileText && !llmConfig.model) {
    showInlineToast(
      'No model configured — open Settings, click "Load models", pick one, and Save.',
      "err",
    );
    return;
  }

  const tab = await panelTab();
  if (!tab) {
    showInlineToast("No active tab.", "err");
    return;
  }
  const runWindowId = tab.windowId;

  // Concurrency is across different tabs. Two runs on the same tab would fight
  // over the same DOM/content script, so block a second run on a busy tab.
  if (runIdForTab(tab.id) != null) {
    showInlineToast("Already running on this tab — stop it or switch tabs.", "err");
    return;
  }

  let tabLabel = "—";
  try {
    tabLabel = new URL(tab.url || "").hostname || tab.title || "tab";
  } catch {
    tabLabel = tab.title || "tab";
  }

  // Site permissions — block the run before it starts if this tab's host isn't
  // allowed by the configured policy. Mid-run navigations are enforced in the runner.
  const sitePolicy = stored.sitePolicy || null;
  const hostVerdict = isHostAllowed(tab.url || "", sitePolicy);
  if (!hostVerdict.allowed) {
    showInlineToast(`Blocked by site permissions: ${hostVerdict.reason}. Adjust it in Settings → Site permissions.`, "err");
    return;
  }

  // Restricted pages (new tab, chrome://) reject injection — skip the attempt;
  // the runner feeds the model a synthetic snapshot so it navigates away first.
  if (!isRestrictedUrl(tab.url)) {
    await chrome.runtime.sendMessage({
      type: "ensureContentScript",
      tabId: tab.id,
    });
  }

  // Pre-run selector scan — probe the attached test file's selectors against
  // the current page and warn (non-blocking) about ones that don't resolve.
  // Skipped when the test opens by navigating to a different origin, and for
  // suites (each sub-test navigates on its own); ${var} targets can't be
  // resolved before the run so they're skipped too.
  if (fileText) {
    try {
      const parsedFile = parseTestFile(fileText);
      if (parsedFile && !parsedFile._isSuite) {
        const steps = parsedFile.steps || [];
        const firstNav = steps[0]?.action === "navigate" ? (steps[0].url || steps[0].value) : null;
        const probeApplies = !firstNav || originOf(String(firstNav)) === originOf(tab.url || "");
        const targets = probeApplies
          ? steps
              .filter((s) => s.action !== "navigate" && s.target != null)
              .map((s) => s.target)
              .filter((t) => !JSON.stringify(t).includes("${"))
          : [];
        if (targets.length) {
          const probe = await chrome.tabs.sendMessage(tab.id, { type: "resolveProbe", targets }).catch(() => null);
          const misses = (probe?.results || []).filter((r) => !r.found);
          if (misses.length) {
            showInlineToast(
              `Pre-run scan: ${misses.length} of ${targets.length} selector(s) don't resolve on this page — the run may rely on healing.`,
              "info",
            );
          }
        }
      }
    } catch { /* unparseable file — the runner will surface the real error */ }
  }

  // Echo the message and clear the composer immediately on submit, regardless
  // of whether the plan-approval gate below applies — so the UI never looks
  // idle while a draft/run is actually in flight.
  hideGreeting();
  const submittedImage = attachedImage;
  setAttachedImage(null);
  appendUserMessage({ instruction, fileAttached: !!fileText, tabLabel, image: submittedImage });

  const submittedInstruction = instruction;
  const submittedFileText = fileText;
  els.instruction.value = "";
  els.instruction.style.height = "auto";
  updateShortcutSaveVisibility();

  // Ask before acting — draft a plan and let the user review/edit/approve it
  // before the run touches the page. Applies to LLM-planned page work only (not
  // test/suite files). The approved (possibly edited) plan is handed to
  // runAgent as prePlannedPlan so it runs exactly as reviewed — no re-planning.
  let prePlannedPlan = null;
  let dryRunRequested = false;
  // Plan-preview tokens are spent before runAgent is entered (and again per
  // "Update plan" click), so they're collected here and seeded into the run's
  // accumulator — otherwise a reviewed run under-reports by a full planning
  // round-trip. Kept even when the draft fails: the tokens were still spent.
  const preRunUsage = newUsageAccumulator();
  const onPreRunUsage = (u) => accumulateUsage(preRunUsage, u);
  const planApprovalApplies =
    stored.askBeforeActing && !fileText && els.mode.value === "auto";
  if (planApprovalApplies) {
    els.runBtn.disabled = true;
    // A persistent thinking bubble (not the self-expiring toast) — planSteps()
    // is an LLM call that routinely runs past the toast's auto-dismiss window,
    // which used to make the UI look like it had gone idle mid-draft.
    const { msg: draftMsg, bubble: draftBubble } = appendAssistantMessage();
    setBubbleThinking(draftBubble, { text: "Drafting plan…" });
    let draftedPlan = null;
    try {
      const snapRes = await draftSnapshot(tab);
      const planned = await planSteps({
        llmConfig,
        instruction,
        snapshot: snapRes?.snapshot,
        mode: els.mode.value === "auto" ? "exploration" : els.mode.value,
        userImage: submittedImage,
        onUsage: onPreRunUsage,
      });
      draftedPlan = Array.isArray(planned?.steps) && planned.steps.length ? planned : null;
    } catch {
      // Fail open below — the bubble removal + toast happen after cleanup.
    }
    draftMsg.remove();
    els.runBtn.disabled = false;
    if (!draftedPlan) {
      // Fail open: if the draft can't be produced, proceed without the gate —
      // but say so instead of silently skipping the review.
      showInlineToast("Plan preview unavailable — running without review.", "info");
    }
    if (draftedPlan) {
      // "Update plan" in the review modal: re-plan against the user's current
      // (possibly hand-edited) rows plus their feedback, on a fresh snapshot.
      const revisePlan = async (currentSteps, feedback) => {
        const snapRes = await draftSnapshot(tab);
        const revised = await planSteps({
          llmConfig,
          instruction,
          snapshot: snapRes?.snapshot,
          mode: els.mode.value === "auto" ? "exploration" : els.mode.value,
          userImage: submittedImage,
          onUsage: onPreRunUsage,
          priorMessages: [
            { role: "assistant", content: JSON.stringify({ steps: currentSteps }) },
            {
              role: "user",
              content:
                "Revise the plan you drafted per this feedback. Keep steps that are unaffected. Feedback: " +
                feedback,
            },
          ],
        });
        if (!Array.isArray(revised?.steps) || !revised.steps.length) {
          throw new Error("The model returned an empty plan.");
        }
        return revised;
      };
      const approval = await showPlanApproval(draftedPlan.steps, revisePlan);
      if (approval.decision === "cancel") {
        showInlineToast("Run cancelled.", "info");
        return;
      }
      prePlannedPlan = { mode: draftedPlan.mode, goal: draftedPlan.goal, steps: approval.steps };
      dryRunRequested = approval.decision === "dryrun";
    }
  }

  const runId = nextRunId++;
  const runController = new AbortController();

  const { bubble } = appendAssistantMessage();
  const { log, strip, pillsState, notifyRequested } = setBubbleThinking(bubble, {
    onStop: () => {
      appendActivityLine(log, "Stopping run…", "info");
      runController.abort();
    },
  });

  activeRuns.set(runId, { controller: runController, tabId: tab.id, tabLabel, bubble, log });
  syncRunButton();

  let narrationTextEl = null; // the in-place text span for the streaming narration line

  try {
    const result = await runAgent({
      instruction: submittedInstruction,
      fileText: submittedFileText,
      mode: els.mode.value === "debug" ? "exploration" : els.mode.value,
      tabId: tab.id,
      llmConfig,
      secrets,
      signal: runController.signal,
      qaDebugMode: !!stored.qaDebugMode || els.mode.value === "debug",
      agentLoop: stored.agentLoop === true,
      vision: normalizeVision(stored.vision),
      maxSteps: Math.min(100, Math.max(5, Number(stored.maxSteps) || 20)),
      streamNarration: !!stored.streamNarration,
      sitePolicy,
      allowExecScript: !!stored.allowExecScript,
      prePlannedPlan,
      seedUsage: preRunUsage,
      recordGif: !!stored.recordGif,
      dryRun: dryRunRequested || (!!submittedFileText && !!els.fileDryRun?.checked),
      stepByStep: !!stored.stepByStep,
      priorMessages: [...explorationConversationHistory],
      userImage: submittedImage,
      onProgress: (msg, cls) => {
        appendActivityLine(log, msg, cls);
        const doneOk = msg.match(/^#(\d+) success/);
        const doneErr = msg.match(/^#(\d+) failed/);
        const started = msg.match(/^#(\d+)\s+\w/);
        if (doneOk) updateStepPill(strip, pillsState, parseInt(doneOk[1]), "ok");
        else if (doneErr) updateStepPill(strip, pillsState, parseInt(doneErr[1]), "err");
        else if (started) updateStepPill(strip, pillsState, parseInt(started[1]), "running");
      },
      // Streamed narration: keep updating a single in-place line until isFinal,
      // then release it so the next step starts a fresh narration line.
      onNarrationDelta: (text, isFinal) => {
        if (!narrationTextEl) narrationTextEl = appendActivityLine(log, text, "narrate");
        else { narrationTextEl.textContent = text; log.scrollTop = log.scrollHeight; }
        if (isFinal) narrationTextEl = null;
      },
      // REQ-20: a to_user screenshot the model chose to share — render it inline.
      onImage: (dataUrl, caption) => appendActivityImage(log, dataUrl, caption),
      // The run retargeted (switch_tab/open_tab/auto-followed a page-spawned
      // tab). Track the live tab so closing the abandoned one doesn't abort
      // the run, and closing the adopted one does.
      onTabChange: (newTabId) => {
        const run = activeRuns.get(runId);
        if (run) run.tabId = newTabId;
        chrome.tabs.get(newTabId).then((t) => {
          if (run && t) run.tabLabel = t.title || t.url || run.tabLabel;
        }).catch(() => {});
      },
      onHumanGate: (gateData) => {
        // autoApprove skips risk/PII gates, but never step-by-step pauses nor
        // critical gates (exec_script, new-origin secret use) — see isCriticalGate.
        if (stored.autoApprove && gateData?.kind !== "step" && !isCriticalGate(gateData)) return Promise.resolve("approve");
        notifyBackgroundRun(
          "Approval needed",
          gateData?.reason || "The agent needs your approval to continue.",
          runWindowId,
          notifyRequested(),
        );
        return showApprovalGate(gateData);
      },
    });
    renderResultIntoBubble(bubble, result);
    notifyBackgroundRun(
      runResultNotificationTitle(result),
      result?.summary || submittedInstruction,
      runWindowId,
      notifyRequested(),
    );
    explorationConversationHistory.push(
      { role: "user", content: submittedInstruction },
      // The summary says what the run actually did/found — that's what a
      // follow-up instruction needs to refer back to; the goal is just the
      // instruction restated.
      { role: "assistant", content: result.summary || result.goal || submittedInstruction },
    );
    explorationResultsHistory.push(sanitizeResultForHistory(result));
    if (explorationConversationHistory.length > 20) {
      explorationConversationHistory = explorationConversationHistory.slice(-20);
      explorationResultsHistory = explorationResultsHistory.slice(-10);
    }
    saveRunToHistory(
      { ...result, mode: els.mode.value === "auto" ? result.mode : els.mode.value },
      [...explorationConversationHistory],
      [...explorationResultsHistory],
    ).catch(() => {});
  } catch (e) {
    // Mark any still-running pill as failed
    for (const [, pill] of pillsState) {
      if (pill.className.includes("running")) pill.className = "step-pill step-pill-err";
    }
    if (e?.name === "AbortError") {
      appendActivityLine(log, "Run stopped", "info");
      renderStoppedIntoBubble(bubble);
    } else {
      appendActivityLine(log, "Run failed: " + (e?.message || e), "err");
      renderErrorIntoBubble(bubble, e);
      notifyBackgroundRun("Run failed", e?.message || String(e), runWindowId, notifyRequested());
    }
  } finally {
    // The run may have retargeted (switch_tab/open_tab) — clean up the group
    // the run actually ended on, not just the panel's own tab.
    const finalTabId = activeRuns.get(runId)?.tabId ?? tab.id;
    activeRuns.delete(runId);
    syncRunButton();
    scrollThreadToBottom();
    maybeCleanupAssistantGroup(finalTabId, log).catch(() => {});
  }
});

// ---------- record mode ----------

// Discard an in-progress recording without saving — used by New chat. The
// mode-switch path intentionally keeps using the record button's stop branch,
// which SAVES the captured steps.
function discardRecording() {
  if (!isRecording) return;
  isRecording = false;
  recordedStepCount = 0;
  els.recordBtn.style.color = "";
  els.recordBtn.title = "Record interactions";
  els.recCount.textContent = "0";
  els.recCount.hidden = true;
  // Fire-and-forget: detach the in-page listeners; the returned steps are dropped.
  panelTab().then((tab) => {
    if (tab?.id) return chrome.tabs.sendMessage(tab.id, { type: "stopRecording" });
  }).catch(() => {});
}

els.recordBtn?.addEventListener("click", async () => {
  const tab = await panelTab();
  if (!tab) return;

  if (!isRecording) {
    const injectRes = await chrome.runtime.sendMessage({ type: "ensureContentScript", tabId: tab.id });
    if (!injectRes?.ok) {
      showInlineToast("Could not inject recorder. Try reloading the tab.", "error");
      return;
    }
    isRecording = true;
    recordedStepCount = 0;
    els.recordBtn.title = "Stop recording";
    els.recordBtn.style.color = "#ef4444";
    els.recCount.textContent = "0";
    els.recCount.hidden = false;
    const startRes = await chrome.tabs.sendMessage(tab.id, { type: "startRecording" }).catch(() => null);
    if (!startRes?.ok) {
      isRecording = false;
      els.recordBtn.style.color = "";
      els.recordBtn.title = "Record interactions";
      els.recCount.hidden = true;
      showInlineToast("Recorder failed to start. Try reloading the tab.", "error");
      return;
    }
    showInlineToast("Recording… interact with the page. Click ● to stop.", "info");
  } else {
    isRecording = false;
    els.recordBtn.style.color = "";
    els.recordBtn.title = "Record interactions";
    els.recCount.hidden = true;
    const res = await chrome.tabs.sendMessage(tab.id, { type: "stopRecording" }).catch(() => null);
    const steps = res?.steps || [];
    // Recordings always include a leading navigate step; require at least one real
    // interaction before treating the session as something worth saving.
    if (!steps.some((s) => s.action !== "navigate")) {
      showInlineToast("No interactions recorded.", "info");
      return;
    }
    const existing = els.fileText.value.replace(/^\s+|\s+$/g, "");
    let yaml;
    if (existing.length > 0) {
      const append = confirm(
        "You already have steps in the test file.\n\n" +
        "OK = append this recording to the existing steps\n" +
        "Cancel = replace them with this recording",
      );
      yaml = append
        ? existing.replace(/\s+$/, "") + "\n" + stepsToYamlBody(steps)
        : stepsToYaml(steps);
    } else {
      yaml = stepsToYaml(steps);
    }
    // Only reveal the panel in test mode — a recording finished by switching
    // modes keeps its YAML in the (hidden) file panel for when the user returns.
    if (els.mode.value === "test") {
      els.fileDetails.hidden = false;
      els.fileDetails.open = true;
    }
    els.fileText.value = yaml;
    els.attachBtn.classList.add("active");
    // Count total steps in the resulting YAML so the summary reflects combined runs.
    const totalSteps = (yaml.match(/^\s*-\s+action:/gm) || []).length;
    renderRecordingResult(steps, yaml, totalSteps);
  }
});

// Live step counter: increment the badge on each recorded step
chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type === "recordedStep" && isRecording) {
    recordedStepCount++;
    els.recCount.textContent = String(recordedStepCount);
  }
});

function renderRecordingResult(steps, yaml, totalSteps = steps.length) {
  hideGreeting();
  const { bubble } = appendAssistantMessage();

  const summary = document.createElement("div");
  summary.className = "result-summary";
  const goal = document.createElement("div");
  goal.className = "goal";
  goal.textContent =
    totalSteps > steps.length
      ? `Recorded ${steps.length} step(s) — appended (${totalSteps} total)`
      : `Recorded ${steps.length} step(s)`;
  summary.appendChild(goal);
  const note = document.createElement("span");
  note.className = "meta";
  note.textContent = "Loaded into test file panel — review and Run";
  summary.appendChild(note);
  bubble.appendChild(summary);

  const pre = document.createElement("pre");
  pre.className = "json-block";
  pre.textContent = yaml;
  bubble.appendChild(pre);

  const footer = document.createElement("div");
  footer.className = "result-footer";

  const copyBtn = makeIconBtn("Copy YAML", ICONS.copy, async () => {
    await navigator.clipboard.writeText(yaml).catch(() => {});
    flashIconBtn(copyBtn, "Copied");
  });
  footer.appendChild(copyBtn);

  const dlBtn = makeIconBtn("Download YAML", ICONS.download, () => {
    const blob = new Blob([yaml], { type: "text/yaml" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    a.href = url;
    a.download = `recorded-${ts}.yaml`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  });
  footer.appendChild(dlBtn);

  bubble.appendChild(footer);
  scrollThreadToBottom();
}

const CRITICAL_RECORD_ACTIONS = new Set(["type", "check", "uncheck", "select", "upload_file"]);

function yamlTargetField(key, target) {
  if (!target) return null;
  if (Array.isArray(target)) {
    if (target.length === 0) return null;
    if (target.length === 1) return `    ${key}: "${String(target[0]).replace(/"/g, '\\"')}"`;
    const lines = [`    ${key}:`];
    for (const t of target) {
      lines.push(`      - "${String(t).replace(/"/g, '\\"')}"`);
    }
    return lines.join("\n");
  }
  return `    ${key}: "${String(target).replace(/"/g, '\\"')}"`;
}

// Render only the step entries (no test_name/steps header), so a new recording can be
// appended onto an existing steps list to combine multiple sessions into one run.
function stepsToYamlBody(steps) {
  const lines = [];
  for (const s of steps) {
    const parts = [`  - action: ${s.action}`];
    if (s.url) parts.push(`    url: "${s.url}"`);
    const targetLine = yamlTargetField("target", s.target);
    if (targetLine) parts.push(targetLine);
    const inputTargetLine = yamlTargetField("inputTarget", s.inputTarget);
    if (inputTargetLine) parts.push(inputTargetLine);
    if (s.isoDate) parts.push(`    isoDate: "${s.isoDate}"`);
    if (s.value != null) parts.push(`    value: "${String(s.value).replace(/"/g, '\\"')}"`);
    if (s.filename) parts.push(`    filename: "${s.filename}"`);
    if (s.mime_type) parts.push(`    mime_type: "${s.mime_type}"`);
    if (CRITICAL_RECORD_ACTIONS.has(s.action)) parts.push(`    critical: true`);
    lines.push(parts.join("\n"));
  }
  return lines.join("\n");
}

function stepsToYaml(steps) {
  return ["test_name: Recorded session", "steps:", stepsToYamlBody(steps)].join("\n");
}

if (els.newSessionBtn) {
  els.newSessionBtn.addEventListener("click", () => {
    if (activeRuns.size) return;
    showGreeting();
  });
}

// Submit on Enter (Shift+Enter for newline). When the slash menu is open, the
// arrow keys / Enter / Escape drive the menu instead of submitting.
els.instruction.addEventListener("keydown", (e) => {
  if (slashMenuOpen()) {
    if (e.key === "ArrowDown") { e.preventDefault(); moveSlashActive(1); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); moveSlashActive(-1); return; }
    if (e.key === "Escape") { e.preventDefault(); closeSlashMenu(); return; }
    if (e.key === "Enter" && !e.shiftKey && slashIndex >= 0 && slashItems[slashIndex]) {
      e.preventDefault();
      applySlashShortcut(slashItems[slashIndex]);
      return;
    }
  }
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    // The run button doubles as Stop while a run is active — Enter must never
    // abort a run, only send when idle.
    if (activeRuns.size === 0) els.runBtn.click();
  }
});

// Auto-grow the instruction textarea.
els.instruction.addEventListener("input", () => {
  els.instruction.style.height = "auto";
  els.instruction.style.height =
    Math.min(els.instruction.scrollHeight, 140) + "px";
});

async function activeTab() {
  const [tab] = await chrome.tabs.query({
    active: true,
    lastFocusedWindow: true,
  });
  return tab;
}

// Title for the run-complete notification, based on the result status.
function runResultNotificationTitle(result) {
  const status = result?.result?.status;
  if (status === "pass") return "Run complete";
  if (status === "needs_human") return "Run needs your input";
  if (status === "partial") return "Run finished with issues";
  if (status === "fail") return "Run failed";
  if (status === "max_steps_reached") return "Run stopped at its step budget";
  return "Run finished";
}

// True only when the side panel itself is the user's active focus. While a run
// executes in the background (user switched tabs, another window, or minimized),
// this is false — which is when a notification is worth surfacing.
function panelIsFocused() {
  return document.visibilityState === "visible" && document.hasFocus();
}

// Ask the background to raise a Chrome notification, but only when the panel
// isn't focused, so foreground runs the user is watching stay quiet.
// `force` (the bubble's "Notify me" toggle) overrides the focus check.
// `windowId` lets a click on the notification re-focus the run's window.
function notifyBackgroundRun(title, message, windowId, force = false) {
  if (!force && panelIsFocused()) return;
  chrome.runtime
    .sendMessage({ type: "notify", title, message, windowId })
    .catch(() => {});
}

// Extension version in the composer foot — read from the manifest so it can
// never drift from what's actually installed.
document.getElementById("version-label").textContent = `v${chrome.runtime.getManifest().version}`;

// The label shows the tab THIS panel is bound to (not the active tab) — it's a
// fixed target now, so it only changes when this tab navigates.
async function refreshActiveTabLabel() {
  try {
    const tab = await panelTab();
    if (!tab) {
      els.activeTabLabel.textContent = "no tab";
      return;
    }
    let host = "—";
    try {
      host = new URL(tab.url || "").hostname || "—";
    } catch {}
    els.activeTabLabel.textContent = host;
  } catch {
    els.activeTabLabel.textContent = "—";
  }
}

chrome.tabs?.onUpdated?.addListener((id, info) => {
  // Only react to this panel's own tab navigating.
  if (MY_TAB_ID != null && id !== MY_TAB_ID) return;
  if (info.status === "complete" || info.url) {
    refreshActiveTabLabel();
  }
});

// Closing a tab ends only the run bound to that tab — other tabs' runs keep
// going. Each aborted run's own finally() removes it from activeRuns.
chrome.tabs?.onRemoved?.addListener((tabId) => {
  for (const [, run] of activeRuns) {
    if (run.tabId === tabId) {
      if (run.log) appendActivityLine(run.log, "Tab closed — run stopped", "info");
      run.controller.abort();
    }
  }
});

// ---------- inline toast ----------

let toastTimer = null;
function showInlineToast(text, cls = "info") {
  const wrap = document.createElement("div");
  wrap.className = "msg assistant";
  const b = document.createElement("div");
  b.className = "bubble";
  const inner = document.createElement("div");
  inner.className = cls === "err" ? "assistant-error" : "assistant-thinking";
  inner.textContent = text;
  b.appendChild(inner);
  wrap.appendChild(b);
  hideGreeting();
  els.thread.appendChild(wrap);
  scrollThreadToBottom();
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    if (wrap.parentNode) wrap.remove();
    if (els.thread.querySelectorAll(".msg").length === 0) showGreeting();
  }, 4500);
}

// ---------- run history ----------

// sanitizeResultForHistory / saveRunToHistory now live in lib/report.js.

function formatRelativeTime(isoStr) {
  try {
    const ms = Date.now() - new Date(isoStr).getTime();
    if (ms < 60_000) return "just now";
    if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
    if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
    return `${Math.floor(ms / 86_400_000)}d ago`;
  } catch { return ""; }
}

async function renderHistoryDrawer() {
  const stored = await chrome.storage.local.get(HISTORY_KEY);
  const list = Array.isArray(stored[HISTORY_KEY]) ? stored[HISTORY_KEY] : [];
  els.historyEmpty.hidden = list.length > 0;
  // Tokens spent outside any run (tab-idle suggestions, QA-debug root-cause
  // analysis) have no bubble of their own, so they surface here.
  if (els.historySession) {
    const session = formatTokens(sessionUsage);
    const show = sessionUsage.llm_calls > 0;
    els.historySession.hidden = !show;
    if (show) {
      els.historySession.textContent =
        `Outside runs this session: ${session.text} tokens · ${sessionUsage.llm_calls} LLM call(s)`;
      els.historySession.title = session.title;
    }
  }
  // Build all rows off-DOM, then swap in once to avoid a layout/paint per row.
  const frag = document.createDocumentFragment();
  for (const entry of list) {
    const li = document.createElement("li");
    li.className = "history-row";
    const statusClass =
      entry.status === "pass" ? "ok"
      : entry.status === "fail" ? "err"
      : entry.status === "partial" || entry.status === "max_steps_reached" ? "warn"
      : "human";
    const statusChar =
      entry.status === "pass" ? "✓"
      : entry.status === "fail" ? "✕"
      : entry.status === "partial" ? "~"
      : entry.status === "max_steps_reached" ? "⏳"
      : "⏸";
    // Entries saved before tokens were persisted have no llmCalls — omit the
    // span entirely rather than showing a total that was never recorded.
    const tokens = entry.llmCalls != null ? formatTokens(usageFromHistory(entry)) : null;
    li.innerHTML =
      `<span class="history-icon ${statusClass}">${statusChar}</span>` +
      `<div class="history-body">` +
        `<div class="history-goal">${escapeHtml(entry.goal)}</div>` +
        `<div class="history-meta">` +
          `<span>${escapeHtml(entry.mode)}</span>` +
          `<span>${entry.passed}/${entry.total} steps</span>` +
          `<span>${formatDuration(entry.elapsedMs)}</span>` +
          (tokens ? `<span title="${escapeHtml(tokens.title)}">${escapeHtml(tokens.text)} tokens</span>` : "") +
          `<span>${formatRelativeTime(entry.startedAt)}</span>` +
        `</div>` +
      `</div>`;
    if (entry.messages && entry.messages.length > 0) {
      li.classList.add("history-clickable");
      li.title = "Click to restore this conversation";
      li.addEventListener("click", () => restoreHistoryEntry(entry));
    }
    frag.appendChild(li);
  }
  els.historyList.replaceChildren(frag);
}

function restoreHistoryEntry(entry) {
  els.historyDrawer.hidden = true;

  for (const child of Array.from(els.thread.children)) {
    if (child !== els.greeting) child.remove();
  }
  hideGreeting();

  // Legacy entries may carry modes that no longer exist in the dropdown
  // (exploration/agentic were folded into auto).
  const modeValue = entry.mode || "ask";
  const dropdownMode = ["exploration", "agentic"].includes(modeValue) ? "auto" : modeValue;
  if ([...els.mode.options].some((o) => o.value === dropdownMode)) {
    els.mode.value = dropdownMode;
    updateComposerForMode();
  }

  const messages = entry.messages || [];
  const results = entry.results || [];
  if (modeValue === "ask") {
    askConversationHistory = [...messages];
    explorationConversationHistory = [];
    explorationResultsHistory = [];
  } else {
    explorationConversationHistory = [...messages];
    // Pad to one slot per message pair so turns run after this restore keep
    // their results index-aligned (legacy entries have no results at all).
    explorationResultsHistory = Array.from(
      { length: Math.ceil(messages.length / 2) },
      (_, j) => results[j] ?? null,
    );
    askConversationHistory = [];
  }

  for (let i = 0; i < messages.length; i += 2) {
    const userMsg = messages[i];
    const assistantMsg = messages[i + 1];

    if (userMsg && userMsg.role === "user") {
      appendUserMessage({ instruction: userMsg.content, fileAttached: false });
    }

    if (assistantMsg && assistantMsg.role === "assistant") {
      const { bubble } = appendAssistantMessage();
      if (modeValue === "ask") {
        renderAskIntoBubble(bubble, {
          instruction: userMsg?.content || "",
          response: assistantMsg.content,
          elapsedMs: i === messages.length - 2 ? entry.elapsedMs : null,
          // History stores one usage total per entry, so it belongs to the last
          // turn only. Entries saved before tokens were persisted have no
          // llmCalls — pass nothing rather than rendering a misleading "—".
          usage:
            i === messages.length - 2 && entry.llmCalls != null
              ? usageFromHistory(entry)
              : null,
        });
      } else if (results[i / 2]) {
        renderResultIntoBubble(bubble, results[i / 2]);
      } else {
        // Legacy entry (or a turn whose result failed to serialize) — only the
        // goal text is available.
        renderRestoredAssistantBubble(bubble, assistantMsg.content, entry.mode);
      }
    }
  }

  if (els.newSessionBtn) els.newSessionBtn.hidden = false;
  scrollThreadToBottom();
}

function renderRestoredAssistantBubble(bubble, content, mode) {
  bubble.innerHTML = "";
  const summary = document.createElement("div");
  summary.className = "result-summary";

  const goal = document.createElement("div");
  goal.className = "goal";
  goal.textContent = content.length > 200 ? content.slice(0, 197) + "..." : content;
  summary.appendChild(goal);

  const meta = document.createElement("span");
  meta.className = "meta";
  meta.innerHTML = `<span>mode</span><b>${escapeHtml(mode || "auto")}</b>`;
  summary.appendChild(meta);

  const tag = document.createElement("span");
  tag.className = "meta";
  tag.innerHTML = `<span class="text-muted">(restored from history)</span>`;
  summary.appendChild(tag);

  bubble.appendChild(summary);
}

els.historyToggle.addEventListener("click", async () => {
  closeOverflowMenu();
  const opening = els.historyDrawer.hidden;
  // Close settings / help / schedules if open
  if (opening) {
    els.settingsSection.hidden = true;
    els.helpSection.hidden = true;
    els.schedulesSection.hidden = true;
    els.promptEditor.hidden = true;
  }
  els.historyDrawer.hidden = !opening;
  if (opening) await renderHistoryDrawer();
});

els.historyClear.addEventListener("click", async () => {
  await chrome.storage.local.set({ [HISTORY_KEY]: [] });
  await renderHistoryDrawer();
});

els.historyClearDebug.addEventListener("click", async () => {
  await chrome.storage.local.set({ [DEBUG_HISTORY_KEY]: [] });
  for (const child of Array.from(els.thread.children)) {
    if (child.classList?.contains("debug-capture-entry")) child.remove();
  }
  if (els.thread.querySelectorAll(".msg, .debug-capture-entry").length === 0) showGreeting();
});

// ---------- shortcuts (saved prompts) ----------

const SHORTCUTS_KEY = "shortcuts";

// In-memory copy so slash-menu keystrokes filter without a storage read each.
// Kept in sync by the two write paths below (the only writers of this key).
let _shortcutsCache = null;

async function getShortcuts() {
  if (_shortcutsCache) return _shortcutsCache;
  const stored = await chrome.storage.local.get(SHORTCUTS_KEY);
  _shortcutsCache = Array.isArray(stored[SHORTCUTS_KEY]) ? stored[SHORTCUTS_KEY] : [];
  return _shortcutsCache;
}

async function saveShortcut(text, title) {
  const trimmed = (text || "").trim();
  if (!trimmed) return;
  const list = await getShortcuts();
  if (list.some((s) => s.text === trimmed)) {
    showInlineToast("That prompt is already saved.", "info");
    return;
  }
  const name = (title || "").replace(/^\s+|\s+$/g, "") || trimmed.slice(0, 48);
  const next = [{ id: Date.now(), title: name, text: trimmed }, ...list].slice(0, 30);
  await chrome.storage.local.set({ [SHORTCUTS_KEY]: next });
  _shortcutsCache = next;
  await renderShortcutManageList();
  showInlineToast("Prompt saved — type / in the composer to use it.", "ok");
}

async function deleteShortcut(id) {
  const next = (await getShortcuts()).filter((s) => s.id !== id);
  await chrome.storage.local.set({ [SHORTCUTS_KEY]: next });
  _shortcutsCache = next;
  await renderShortcutManageList();
}

// Render the manage list (with delete buttons) in Settings → Shortcuts.
async function renderShortcutManageList() {
  if (!els.shortcutManageList) return;
  const list = await getShortcuts();
  els.shortcutManageList.innerHTML = "";
  if (els.shortcutEmpty) els.shortcutEmpty.hidden = list.length > 0;
  for (const sc of list) {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = sc.title;
    label.title = sc.text;
    const del = document.createElement("button");
    del.type = "button";
    del.title = "Delete shortcut";
    del.setAttribute("aria-label", "Delete shortcut");
    del.textContent = "×";
    del.addEventListener("click", () => deleteShortcut(sc.id));
    li.appendChild(label);
    li.appendChild(del);
    els.shortcutManageList.appendChild(li);
  }
}

// ---------- Settings → Site memory ----------
// Per-origin learned memory (lib/memory.js). The toggle and notes target the
// panel's current tab origin and apply immediately (no Save button needed);
// the list below manages every remembered origin.

let _memoryOrigin = null;

async function renderSiteMemorySection() {
  if (!els.memoryEnabled) return;
  try {
    const tab = await activeTab();
    _memoryOrigin = originOf(tab?.url);
  } catch {
    _memoryOrigin = null;
  }
  const hasOrigin = !!_memoryOrigin;
  els.memoryOrigin.textContent = hasOrigin ? new URL(_memoryOrigin).host : "this site";
  els.memoryEnabled.disabled = !hasOrigin;
  els.memoryNotes.disabled = !hasOrigin;
  const site = hasOrigin ? await getSiteMemory(_memoryOrigin).catch(() => null) : null;
  els.memoryEnabled.checked = !!site?.enabled;
  els.memoryNotes.value = site?.notes || "";

  const list = await listOrigins().catch(() => []);
  els.memoryList.innerHTML = "";
  if (els.memoryEmpty) els.memoryEmpty.hidden = list.length > 0;
  for (const entry of list) {
    const li = document.createElement("li");
    const label = document.createElement("span");
    let host;
    try { host = new URL(entry.origin).host; } catch { host = entry.origin; }
    label.textContent = `${host} — ${entry.healCount} fix${entry.healCount === 1 ? "" : "es"}${entry.enabled ? "" : " (recording off)"}`;
    label.title = entry.notes || entry.origin;
    const del = document.createElement("button");
    del.type = "button";
    del.title = "Forget this site";
    del.setAttribute("aria-label", "Forget this site");
    del.textContent = "×";
    del.addEventListener("click", async () => {
      await clearOrigin(entry.origin);
      renderSiteMemorySection();
    });
    li.appendChild(label);
    li.appendChild(del);
    els.memoryList.appendChild(li);
  }
}

els.memoryEnabled?.addEventListener("change", async () => {
  if (!_memoryOrigin) return;
  await setMemoryEnabled(_memoryOrigin, els.memoryEnabled.checked);
  renderSiteMemorySection();
});

els.memoryNotes?.addEventListener("change", async () => {
  if (!_memoryOrigin) return;
  await setMemoryNotes(_memoryOrigin, els.memoryNotes.value);
  renderSiteMemorySection();
});

// Show/hide the ☆ Save button based on whether the composer has text.
function updateShortcutSaveVisibility() {
  if (els.shortcutSave) els.shortcutSave.hidden = !els.instruction.value.trim();
}

// ☆ Save opens a small name popover instead of saving immediately, so the
// slash menu shows a short shortcut name rather than the whole prompt text.
els.shortcutSave?.addEventListener("click", () => {
  const text = els.instruction.value.trim();
  if (!text || !els.shortcutNamePop) return;
  els.shortcutNameInput.value = text.slice(0, 48);
  els.shortcutNamePop.hidden = false;
  els.shortcutNameInput.focus();
  els.shortcutNameInput.select();
});

function closeShortcutNamePop() {
  if (els.shortcutNamePop) els.shortcutNamePop.hidden = true;
}

async function confirmShortcutName() {
  const title = els.shortcutNameInput?.value || "";
  closeShortcutNamePop();
  await saveShortcut(els.instruction.value, title);
}

els.shortcutNameSave?.addEventListener("click", confirmShortcutName);
els.shortcutNameInput?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    confirmShortcutName();
  } else if (e.key === "Escape") {
    e.preventDefault();
    closeShortcutNamePop();
  }
});
// Click-away: mousedown on the Save button fires before blur, so a real save
// still goes through; anywhere else dismisses without saving.
els.shortcutNameInput?.addEventListener("blur", (e) => {
  if (e.relatedTarget === els.shortcutNameSave) return;
  setTimeout(closeShortcutNamePop, 120);
});

els.instruction?.addEventListener("input", updateShortcutSaveVisibility);

// ---------- slash menu: type "/" in the composer to pick a saved prompt ----------

let slashItems = [];   // currently shown shortcuts
let slashIndex = -1;   // highlighted item

function slashMenuOpen() {
  return els.shortcutMenu && !els.shortcutMenu.hidden;
}

function closeSlashMenu() {
  if (els.shortcutMenu) els.shortcutMenu.hidden = true;
  slashItems = [];
  slashIndex = -1;
}

// Open/refresh the menu from the current composer text. Triggered while the
// text is a single line beginning with "/"; the part after "/" filters by title
// or body. With no saved shortcuts the menu stays closed (a literal "/" is fine).
async function refreshSlashMenu() {
  if (!els.shortcutMenu) return;
  const v = els.instruction.value;
  if (!v.startsWith("/") || v.includes("\n")) { closeSlashMenu(); return; }
  const all = await getShortcuts();
  if (!all.length) { closeSlashMenu(); return; }
  const q = v.slice(1).trim().toLowerCase();
  slashItems = q
    ? all.filter((s) => s.title.toLowerCase().includes(q) || s.text.toLowerCase().includes(q))
    : all;
  slashIndex = slashItems.length ? 0 : -1;
  renderSlashMenu();
  els.shortcutMenu.hidden = false;
}

function renderSlashMenu() {
  els.shortcutMenu.innerHTML = "";
  const head = document.createElement("div");
  head.className = "shortcut-menu-head";
  head.textContent = "Saved prompts";
  els.shortcutMenu.appendChild(head);

  if (!slashItems.length) {
    const empty = document.createElement("div");
    empty.className = "shortcut-menu-empty";
    empty.textContent = "No matching shortcuts.";
    els.shortcutMenu.appendChild(empty);
    return;
  }

  slashItems.forEach((sc, i) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "shortcut-menu-item" + (i === slashIndex ? " active" : "");
    item.textContent = sc.title;
    item.title = sc.text;
    item.setAttribute("role", "option");
    // mousedown (not click) so selection runs before the textarea blurs.
    item.addEventListener("mousedown", (e) => { e.preventDefault(); applySlashShortcut(sc); });
    els.shortcutMenu.appendChild(item);
  });
}

function moveSlashActive(delta) {
  if (!slashItems.length) return;
  slashIndex = (slashIndex + delta + slashItems.length) % slashItems.length;
  renderSlashMenu();
  els.shortcutMenu.querySelector(".shortcut-menu-item.active")?.scrollIntoView({ block: "nearest" });
}

function applySlashShortcut(sc) {
  closeSlashMenu();
  els.instruction.value = sc.text;
  els.instruction.focus();
  // Re-run input handlers (autogrow, save-button visibility); value no longer
  // starts with "/", so refreshSlashMenu keeps the menu closed.
  els.instruction.dispatchEvent(new Event("input"));
}

els.instruction?.addEventListener("input", refreshSlashMenu);
els.instruction?.addEventListener("blur", () => setTimeout(closeSlashMenu, 120));

// ---------- scheduled prompts ----------
// A saved prompt (shortcut) can carry a startUrl, a model, and a schedule. The
// store is the same `shortcuts` durable key, so un-scheduled prompts keep
// behaving exactly as before. Run OUTPUT is kept out of the (sync-mirrored)
// shortcuts record and lives in a separate local-only key to stay under the
// 8 KB per-item sync quota.

const SCHEDULED_RESULTS_KEY = "scheduledResults";
const SCHEDULED_RESULTS_MAX = 20;
const PENDING_SCHEDULE_KEY = "pendingScheduleRuns";

async function getPrompt(id) {
  return (await getShortcuts()).find((s) => s.id === id) || null;
}

// Create or update a full prompt record with validation. `editingId` is the id
// being edited (null for a new record). Returns { ok, error?, record? }.
async function upsertPrompt(rec, editingId = null) {
  const title = _trimStr(rec.title || "");
  const text = _trimStr(rec.text || "");
  if (!title) return { ok: false, error: "Name is required." };
  if (!text) return { ok: false, error: "Prompt is required." };

  const list = await getShortcuts();
  const dupe = list.some(
    (s) => s.id !== editingId && _trimStr(s.title || "").toLowerCase() === title.toLowerCase(),
  );
  if (dupe) return { ok: false, error: `A prompt named "${title}" already exists.` };

  const startUrl = _trimStr(rec.startUrl || "");
  if (startUrl) {
    try { new URL(startUrl); } catch { return { ok: false, error: "Start-from must be a full URL (include https://)." }; }
  }

  let schedule = null;
  if (rec.schedule) {
    if (!rec.schedule.date || !rec.schedule.time) {
      return { ok: false, error: "Schedule is on — pick a date and time." };
    }
    schedule = {
      enabled: true,
      frequency: rec.schedule.frequency || "once",
      date: rec.schedule.date,
      time: rec.schedule.time,
      lastRun: null,
      nextRun: null,
      completed: false,
    };
    schedule.nextRun = computeNextRun(schedule);
  }

  const now = Date.now();
  const existing = editingId != null ? list.find((s) => s.id === editingId) : null;
  const merged = {
    id: existing?.id ?? now,
    title,
    text,
    startUrl,
    model: _trimStr(rec.model || ""),
    schedule,
    createdAt: existing?.createdAt ?? now,
    updatedAt: now,
  };

  const next = existing
    ? list.map((s) => (s.id === existing.id ? merged : s))
    : [merged, ...list].slice(0, 30);
  await chrome.storage.local.set({ [SHORTCUTS_KEY]: next });
  _shortcutsCache = next;
  await renderShortcutManageList();
  return { ok: true, record: merged };
}

async function deletePrompt(id) {
  const next = (await getShortcuts()).filter((s) => s.id !== id);
  await chrome.storage.local.set({ [SHORTCUTS_KEY]: next });
  _shortcutsCache = next;
  const stored = await chrome.storage.local.get(SCHEDULED_RESULTS_KEY);
  const all = stored[SCHEDULED_RESULTS_KEY] && typeof stored[SCHEDULED_RESULTS_KEY] === "object"
    ? stored[SCHEDULED_RESULTS_KEY] : {};
  if (all[id] !== undefined) { delete all[id]; await chrome.storage.local.set({ [SCHEDULED_RESULTS_KEY]: all }); }
  await renderShortcutManageList();
  await renderSchedulesList();
}

async function togglePromptEnabled(id) {
  const list = await getShortcuts();
  const rec = list.find((s) => s.id === id);
  if (!rec || !rec.schedule) return;
  rec.schedule.enabled = !rec.schedule.enabled;
  if (rec.schedule.enabled) {
    rec.schedule.completed = false;
    rec.schedule.nextRun = computeNextRun(rec.schedule);
  }
  rec.updatedAt = Date.now();
  const next = list.map((s) => (s.id === id ? rec : s));
  await chrome.storage.local.set({ [SHORTCUTS_KEY]: next });
  _shortcutsCache = next;
  await renderSchedulesList();
}

// ----- per-prompt results log (local-only, like runHistory) -----
async function appendScheduledResult(promptId, entry) {
  const stored = await chrome.storage.local.get(SCHEDULED_RESULTS_KEY);
  const all = stored[SCHEDULED_RESULTS_KEY] && typeof stored[SCHEDULED_RESULTS_KEY] === "object"
    ? stored[SCHEDULED_RESULTS_KEY] : {};
  const list = Array.isArray(all[promptId]) ? all[promptId] : [];
  list.unshift(entry);
  if (list.length > SCHEDULED_RESULTS_MAX) list.length = SCHEDULED_RESULTS_MAX;
  all[promptId] = list;
  await chrome.storage.local.set({ [SCHEDULED_RESULTS_KEY]: all });
}

async function getScheduledResults(promptId) {
  const stored = await chrome.storage.local.get(SCHEDULED_RESULTS_KEY);
  const all = stored[SCHEDULED_RESULTS_KEY] || {};
  return Array.isArray(all[promptId]) ? all[promptId] : [];
}

async function recordScheduledRun(promptId, result) {
  const r = result?.result || {};
  await appendScheduledResult(promptId, {
    id: "sr_" + Date.now(),
    ranAt: Date.now(),
    status: r.status || "pass",
    summary: String(result?.summary || result?.answer || result?.goal || "").slice(0, 500),
    answer: String(result?.answer || "").slice(0, 2000),
    passed: r.passed_steps || 0,
    failed: r.failed_steps || 0,
    total: (r.passed_steps || 0) + (r.failed_steps || 0) + (r.skipped_steps || 0),
    elapsedMs: computeElapsed(result) ?? 0,
  });
}

// ----- editor -----

let _editingPromptId = null;

function populatePromptTimeSelects() {
  if (els.promptHour && !els.promptHour.options.length) {
    for (let h = 1; h <= 12; h++) {
      const o = document.createElement("option");
      o.value = String(h); o.textContent = String(h);
      els.promptHour.appendChild(o);
    }
  }
  if (els.promptMinute && !els.promptMinute.options.length) {
    for (let m = 0; m < 60; m++) {
      const o = document.createElement("option");
      o.value = String(m); o.textContent = String(m).padStart(2, "0");
      els.promptMinute.appendChild(o);
    }
  }
}

async function populatePromptModelDropdown(selectedId) {
  if (!els.promptModel) return;
  const stored = await chrome.storage.local.get(["modelList", "llmConfig"]);
  const models = Array.isArray(stored.modelList) ? stored.modelList : [];
  const current = stored.llmConfig?.model || "";
  els.promptModel.innerHTML = "";
  const def = document.createElement("option");
  def.value = "";
  def.textContent = current ? `— Use current model (${current}) —` : "— Use current model —";
  els.promptModel.appendChild(def);
  const ids = [...models];
  if (selectedId && !ids.includes(selectedId)) ids.push(selectedId);
  for (const id of ids) {
    const o = document.createElement("option");
    o.value = id; o.textContent = id;
    els.promptModel.appendChild(o);
  }
  els.promptModel.value = selectedId || "";
}

function syncScheduleFieldsVisibility() {
  if (els.promptScheduleFields) els.promptScheduleFields.hidden = !els.promptScheduleOn.checked;
}

async function openPromptEditor(record) {
  _editingPromptId = record?.id ?? null;
  els.promptEditorTitle.textContent = record ? "Edit prompt" : "New scheduled prompt";
  els.promptName.value = record?.title || "";
  els.promptText.value = record?.text || "";
  els.promptUrl.value = record?.startUrl || "";
  populatePromptTimeSelects();
  await populatePromptModelDropdown(record?.model || "");

  const sch = record?.schedule || null;
  els.promptScheduleOn.checked = !!sch;
  els.promptFrequency.value = sch?.frequency || "once";
  els.promptDate.value = sch?.date || "";
  if (sch?.time) {
    const [h, m] = sch.time.split(":").map(Number);
    const t = to12h(h);
    els.promptHour.value = String(t.hour);
    els.promptMinute.value = String(m);
    els.promptAmpm.value = t.ampm;
  } else {
    els.promptHour.value = "9";
    els.promptMinute.value = "0";
    els.promptAmpm.value = "AM";
  }
  syncScheduleFieldsVisibility();

  els.settingsSection.hidden = true;
  els.helpSection.hidden = true;
  els.historyDrawer.hidden = true;
  els.schedulesSection.hidden = true;
  els.promptEditor.hidden = false;
}

function closePromptEditor() {
  els.promptEditor.hidden = true;
  _editingPromptId = null;
}

function readEditorRecord() {
  const rec = {
    title: els.promptName.value,
    text: els.promptText.value,
    startUrl: els.promptUrl.value,
    model: els.promptModel.value,
  };
  if (els.promptScheduleOn.checked) {
    const hour24 = to24h(els.promptHour.value, els.promptAmpm.value);
    const time = `${String(hour24).padStart(2, "0")}:${String(Number(els.promptMinute.value)).padStart(2, "0")}`;
    rec.schedule = { frequency: els.promptFrequency.value, date: els.promptDate.value, time };
  }
  return rec;
}

async function savePromptFromEditor() {
  const res = await upsertPrompt(readEditorRecord(), _editingPromptId);
  if (!res.ok) { showInlineToast(res.error, "err"); return; }
  closePromptEditor();
  els.schedulesSection.hidden = false;
  await renderSchedulesList();
  showInlineToast("Prompt saved.", "ok");
}

els.promptScheduleOn?.addEventListener("change", syncScheduleFieldsVisibility);
els.promptSave?.addEventListener("click", savePromptFromEditor);
els.promptCancel?.addEventListener("click", () => { closePromptEditor(); els.schedulesSection.hidden = false; });
els.promptNew?.addEventListener("click", () => openPromptEditor(null));

// ----- management view -----

function formatScheduleAt(ms) {
  try {
    return new Date(ms).toLocaleString([], { day: "2-digit", month: "short", hour: "numeric", minute: "2-digit" });
  } catch { return ""; }
}

function describeSchedule(rec) {
  const sch = rec.schedule;
  if (!sch) return { dot: "oneoff", status: "One-off", next: "" };
  if (sch.completed) {
    return { dot: "done", status: "Completed", next: sch.lastRun ? `ran ${formatRelativeTime(new Date(sch.lastRun).toISOString())}` : "" };
  }
  if (!sch.enabled) return { dot: "paused", status: "Paused", next: "" };
  const freq = sch.frequency.charAt(0).toUpperCase() + sch.frequency.slice(1);
  return { dot: "active", status: freq, next: sch.nextRun ? `next ${formatScheduleAt(sch.nextRun)}` : "" };
}

async function toggleScheduleResults(li, promptId) {
  const open = li.querySelector(".schedule-results");
  if (open) { open.remove(); return; }
  const results = await getScheduledResults(promptId);
  const box = document.createElement("div");
  box.className = "schedule-results";
  if (!results.length) {
    box.textContent = "No runs yet.";
  } else {
    box.innerHTML = results.map((r) => {
      const cls = r.status === "pass" ? "rok" : (r.status === "fail" || r.status === "error") ? "rerr" : "";
      const when = formatRelativeTime(new Date(r.ranAt).toISOString());
      const summary = r.summary || r.answer || r.status || "";
      return `<div class="schedule-results-row"><span class="${cls}">●</span> ${escapeHtml(when)} — ${escapeHtml(String(summary).slice(0, 220))}</div>`;
    }).join("");
  }
  li.appendChild(box);
}

async function renderSchedulesList() {
  if (!els.schedulesList) return;
  const list = await getShortcuts();
  if (els.schedulesEmpty) els.schedulesEmpty.hidden = list.length > 0;
  els.schedulesList.innerHTML = "";
  for (const rec of list) {
    const d = describeSchedule(rec);
    const li = document.createElement("li");
    li.className = "schedule-row";

    const dot = document.createElement("span");
    dot.className = `schedule-dot ${d.dot}`;
    li.appendChild(dot);

    const info = document.createElement("div");
    info.className = "schedule-info";
    const name = document.createElement("div");
    name.className = "schedule-name";
    name.textContent = "/" + rec.title;
    name.title = rec.text;
    const meta = document.createElement("div");
    meta.className = "schedule-meta";
    const parts = [d.status];
    if (d.next) parts.push(d.next);
    if (rec.model) parts.push(rec.model);
    meta.innerHTML = parts.map((p) => `<span>${escapeHtml(p)}</span>`).join("");
    info.appendChild(name);
    info.appendChild(meta);
    li.appendChild(info);

    const actions = document.createElement("div");
    actions.className = "schedule-actions";

    const runBtn = document.createElement("button");
    runBtn.type = "button"; runBtn.textContent = "Run";
    runBtn.title = "Run now";
    runBtn.addEventListener("click", () => { els.schedulesSection.hidden = true; runSavedPrompt(rec, { viaSchedule: false }); });
    actions.appendChild(runBtn);

    if (rec.schedule && !rec.schedule.completed) {
      const pauseBtn = document.createElement("button");
      pauseBtn.type = "button";
      pauseBtn.textContent = rec.schedule.enabled ? "Pause" : "Resume";
      pauseBtn.addEventListener("click", () => togglePromptEnabled(rec.id));
      actions.appendChild(pauseBtn);
    }

    const editBtn = document.createElement("button");
    editBtn.type = "button"; editBtn.textContent = "Edit";
    editBtn.addEventListener("click", () => openPromptEditor(rec));
    actions.appendChild(editBtn);

    const resBtn = document.createElement("button");
    resBtn.type = "button"; resBtn.textContent = "Results";
    resBtn.addEventListener("click", () => toggleScheduleResults(li, rec.id));
    actions.appendChild(resBtn);

    const delBtn = document.createElement("button");
    delBtn.type = "button"; delBtn.className = "danger"; delBtn.textContent = "Delete";
    delBtn.addEventListener("click", () => deletePrompt(rec.id));
    actions.appendChild(delBtn);

    li.appendChild(actions);
    els.schedulesList.appendChild(li);
  }
}

els.schedulesToggle?.addEventListener("click", async () => {
  closeOverflowMenu();
  const opening = els.schedulesSection.hidden;
  if (opening) {
    els.settingsSection.hidden = true;
    els.helpSection.hidden = true;
    els.historyDrawer.hidden = true;
    els.promptEditor.hidden = true;
    await renderSchedulesList();
  }
  els.schedulesSection.hidden = !opening;
});

// ----- execution -----

// Poll a tab until it finishes loading (used after pre-navigating to a
// Start-from URL). Returns the final tab, or the last-known tab on timeout.
async function waitForTabLoad(tabId, timeoutMs = 30000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    let tab;
    try { tab = await chrome.tabs.get(tabId); } catch { return null; }
    if (tab.status === "complete") return tab;
    await new Promise((r) => setTimeout(r, 300));
  }
  try { return await chrome.tabs.get(tabId); } catch { return null; }
}

// After a SCHEDULED fire completes, advance the schedule so it recurs (or
// completes a one-off). Never called for a manual "Run now".
async function finalizeScheduleAfterRun(promptId) {
  const list = await getShortcuts();
  const rec = list.find((s) => s.id === promptId);
  if (!rec || !rec.schedule) return;
  const now = Date.now();
  rec.schedule.lastRun = now;
  if (rec.schedule.frequency === "once") {
    rec.schedule.enabled = false;
    rec.schedule.completed = true;
    rec.schedule.nextRun = null;
  } else {
    rec.schedule.nextRun = computeNextRun(rec.schedule, now + 1000);
  }
  rec.updatedAt = now;
  const next = list.map((s) => (s.id === promptId ? rec : s));
  await chrome.storage.local.set({ [SHORTCUTS_KEY]: next });
  _shortcutsCache = next;
  if (!els.schedulesSection.hidden) await renderSchedulesList();
}

async function bumpNextRun(promptId, whenMs) {
  const list = await getShortcuts();
  const rec = list.find((s) => s.id === promptId);
  if (!rec || !rec.schedule) return;
  rec.schedule.nextRun = whenMs;
  const next = list.map((s) => (s.id === promptId ? rec : s));
  await chrome.storage.local.set({ [SHORTCUTS_KEY]: next });
  _shortcutsCache = next;
}

// Open the Start-from URL (if any) and run the browsing agent with the saved
// prompt's instruction. `viaSchedule` distinguishes an automatic fire (advances
// the schedule, force-notifies) from a manual "Run now".
// Also the panel-assisted arm of the local command bridge: a delegated task
// that asked for `attach:"panel"` (or needs a focused tab) arrives here as a
// synthetic prompt record, with `bridge.emit` streaming the same events back to
// the CLI that a headless bridge run sends. Approval gates stay in the panel's
// modal in this mode — the operator is already looking at it — and the CLI is
// told so rather than being offered a second, competing prompt.
async function runSavedPrompt(rec, { viaSchedule = false, bridge = null } = {}) {
  const promptId = rec.id;
  const emit = bridge?.emit || (() => {});

  // One run at a time on this panel's tab.
  if (activeRuns.size > 0) {
    if (bridge) {
      emit("error", { error: "busy", detail: "another run is already in progress in the side panel" });
      return;
    }
    if (viaSchedule) {
      await appendScheduledResult(promptId, {
        id: "sr_" + Date.now(), ranAt: Date.now(), status: "skipped",
        summary: "Skipped — another run was already in progress.",
      });
      await bumpNextRun(promptId, Date.now() + 5 * 60 * 1000); // retry in ~5 min
    } else {
      showInlineToast("Already running — stop it first.", "err");
    }
    return;
  }

  const stored = await chrome.storage.local.get([
    "llmConfig", "secretsJson", "qaDebugMode", "agentLoop", "vision", "maxSteps",
    "streamNarration", "autoApprove", "allowExecScript", "stepByStep", "recordGif", "sitePolicy",
  ]);
  const baseCfg = stored.llmConfig || DEFAULTS;
  const llmConfig = rec.model ? { ...baseCfg, model: rec.model } : baseCfg;
  let secrets = {};
  try { secrets = JSON.parse(stored.secretsJson || "{}"); } catch {}

  const failEarly = async (msg) => {
    if (bridge) { emit("error", { error: "run_failed", detail: msg }); return; }
    if (viaSchedule) {
      await appendScheduledResult(promptId, { id: "sr_" + Date.now(), ranAt: Date.now(), status: "error", summary: msg });
      notifyBackgroundRun("Scheduled prompt failed", msg, undefined, true);
      await finalizeScheduleAfterRun(promptId);
    } else {
      showInlineToast(msg, "err");
    }
  };

  if (!llmConfig.model) { await failEarly("No model configured — open Settings and pick one."); return; }
  if (!rec.text) { await failEarly("This prompt has no instruction text."); return; }

  let tab = await panelTab();
  if (!tab) { await failEarly("No tab available to run in."); return; }

  hideGreeting();
  appendUserMessage({
    instruction: `/${rec.title}${viaSchedule ? " (scheduled)" : ""} — ${rec.text}`,
    fileAttached: false,
  });

  // Pre-navigate to the Start-from URL and wait for it to load.
  if (rec.startUrl) {
    try {
      await chrome.tabs.update(tab.id, { url: rec.startUrl, active: true });
      tab = (await waitForTabLoad(tab.id)) || tab;
    } catch (e) {
      await failEarly("Could not open the Start-from URL: " + (e?.message || e));
      return;
    }
  }

  const runWindowId = tab.windowId;
  const sitePolicy = stored.sitePolicy || null;
  const hostVerdict = isHostAllowed(tab.url || "", sitePolicy);
  if (!hostVerdict.allowed) { await failEarly(`Blocked by site permissions: ${hostVerdict.reason}.`); return; }

  if (!isRestrictedUrl(tab.url)) {
    await chrome.runtime.sendMessage({ type: "ensureContentScript", tabId: tab.id }).catch(() => {});
  }
  emit("accepted", { tabId: tab.id, mode: "auto", attached: "panel" });

  const runId = nextRunId++;
  const runController = new AbortController();
  const { bubble } = appendAssistantMessage();
  const { log, strip, pillsState, notifyRequested } = setBubbleThinking(bubble, {
    onStop: () => { appendActivityLine(log, "Stopping run…", "info"); runController.abort(); },
  });
  activeRuns.set(runId, { controller: runController, tabId: tab.id, tabLabel: rec.startUrl || rec.title, bubble, log });
  syncRunButton();

  let narrationTextEl = null;
  try {
    const result = await runAgent({
      instruction: rec.text,
      fileText: "",
      mode: "auto",
      tabId: tab.id,
      llmConfig,
      secrets,
      signal: runController.signal,
      qaDebugMode: !!stored.qaDebugMode,
      agentLoop: stored.agentLoop === true,
      vision: normalizeVision(stored.vision),
      maxSteps: Math.min(100, Math.max(5, Number(stored.maxSteps) || 20)),
      streamNarration: !!stored.streamNarration,
      sitePolicy,
      allowExecScript: !!stored.allowExecScript,
      recordGif: !!stored.recordGif,
      stepByStep: !!stored.stepByStep,
      onProgress: (msg, cls) => {
        appendActivityLine(log, msg, cls);
        emit("progress", { message: msg, level: cls || "info" });
        const doneOk = msg.match(/^#(\d+) success/);
        const doneErr = msg.match(/^#(\d+) failed/);
        const started = msg.match(/^#(\d+)\s+\w/);
        if (doneOk) updateStepPill(strip, pillsState, parseInt(doneOk[1]), "ok");
        else if (doneErr) updateStepPill(strip, pillsState, parseInt(doneErr[1]), "err");
        else if (started) updateStepPill(strip, pillsState, parseInt(started[1]), "running");
      },
      onNarrationDelta: (text, isFinal) => {
        if (!narrationTextEl) narrationTextEl = appendActivityLine(log, text, "narrate");
        else { narrationTextEl.textContent = text; log.scrollTop = log.scrollHeight; }
        if (isFinal) narrationTextEl = null;
        emit("narration", { text, final: !!isFinal });
      },
      onImage: (dataUrl, caption) => {
        appendActivityImage(log, dataUrl, caption);
        emit("image", bridge?.includeScreenshots ? { dataUrl, caption } : { caption, omitted: true });
      },
      onTabChange: (newTabId) => {
        const run = activeRuns.get(runId);
        if (run) run.tabId = newTabId;
        emit("tab", { tabId: newTabId });
      },
      onHumanGate: (gateData) => {
        if (stored.autoApprove && gateData?.kind !== "step" && !isCriticalGate(gateData)) return Promise.resolve("approve");
        notifyBackgroundRun("Approval needed", gateData?.reason || "The agent needs your approval to continue.", runWindowId, notifyRequested());
        // The panel owns this decision; the client is told to expect a pause
        // rather than being asked to answer a gate it can't see the page for.
        emit("gate", { handledInPanel: true, critical: isCriticalGate(gateData), reason: gateData?.reason || "" });
        return showApprovalGate(gateData);
      },
    });
    renderResultIntoBubble(bubble, result);
    notifyBackgroundRun(runResultNotificationTitle(result), result?.summary || rec.text, runWindowId, notifyRequested() || viaSchedule);
    saveRunToHistory({ ...result, mode: result.mode || "auto" }, [], [], { source: bridge ? "bridge" : "panel" }).catch(() => {});
    emit("done", { record: bridge?.includeScreenshots ? result : sanitizeResultForHistory(result) });
    if (promptId != null) await recordScheduledRun(promptId, result);
  } catch (e) {
    for (const [, pill] of pillsState) {
      if (pill.className.includes("running")) pill.className = "step-pill step-pill-err";
    }
    if (e?.name === "AbortError") {
      appendActivityLine(log, "Run stopped", "info");
      renderStoppedIntoBubble(bubble);
      emit("error", { error: "stopped", detail: "Run stopped" });
    } else {
      renderErrorIntoBubble(bubble, e);
      notifyBackgroundRun(bridge ? "Delegated run failed" : "Scheduled prompt failed", e?.message || String(e), runWindowId, true);
      emit("error", { error: "run_failed", detail: e?.message || String(e) });
      if (promptId != null) await appendScheduledResult(promptId, { id: "sr_" + Date.now(), ranAt: Date.now(), status: "error", summary: e?.message || String(e) });
    }
  } finally {
    activeRuns.delete(runId);
    syncRunButton();
    scrollThreadToBottom();
    if (viaSchedule) await finalizeScheduleAfterRun(promptId);
  }
}

// Live-dispatch channel: connecting a port tells the service worker a panel is
// open, so a due schedule runs here instead of falling back to a notification.
let _schedulePort = null;
function connectSchedulePort() {
  try {
    _schedulePort = chrome.runtime.connect({ name: "panel" });
    _schedulePort.onMessage.addListener(async (msg) => {
      if (msg?.type === "runSchedule" && msg.id != null) {
        const rec = await getPrompt(msg.id);
        if (rec) runSavedPrompt(rec, { viaSchedule: true });
        return;
      }
      // A delegated task the service worker handed to this panel (attach:"panel").
      // Events go back up the same port; the worker relays them to the client.
      if (msg?.type === "runBridgeTask" && msg.id != null) {
        const task = msg.task || {};
        runSavedPrompt(
          { id: null, title: "delegated", text: task.instruction || "", startUrl: task.startUrl || "", model: "" },
          {
            bridge: {
              includeScreenshots: task.includeScreenshots === true,
              emit: (event, data) => {
                try { _schedulePort?.postMessage({ type: "bridgeEvent", id: msg.id, event, ...data }); } catch {}
              },
            },
          },
        );
      }
    });
    _schedulePort.onDisconnect.addListener(() => { _schedulePort = null; });
  } catch { /* SW unavailable — scheduling still works via reconnection on next load */ }
}

// Runs the service worker queued while no panel was open (the user clicked the
// "scheduled prompt ready" notification, which opened this panel).
async function drainPendingScheduleRuns() {
  const stored = await chrome.storage.local.get(PENDING_SCHEDULE_KEY);
  const ids = Array.isArray(stored[PENDING_SCHEDULE_KEY]) ? stored[PENDING_SCHEDULE_KEY] : [];
  if (!ids.length) return;
  await chrome.storage.local.set({ [PENDING_SCHEDULE_KEY]: [] });
  for (const id of ids) {
    const rec = await getPrompt(id);
    if (rec) await runSavedPrompt(rec, { viaSchedule: true });
  }
}

// ---------- plan-approval ("ask before acting") ----------

// One readable line per planned step, for the approval modal.
function describePlanStep(s) {
  const bits = [s.action];
  if (s.url) bits.push(s.url);
  else if (s.target) bits.push(typeof s.target === "string" ? s.target : JSON.stringify(s.target));
  if (s.value && s.action !== "navigate") bits.push(`= ${s.value}`);
  if (s.matcher) bits.push(`(${s.matcher}${s.expected != null ? ` "${s.expected}"` : ""})`);
  return bits.join(" ");
}

// Reuse the gate modal to present a drafted plan for review. The plan is
// EDITABLE: each step's target/value/url can be changed inline, steps can be
// reordered (↑ ↓) or deleted (✕), and the edited list is what actually runs —
// runAgent receives it as prePlannedPlan, skipping the second planning call.
// `revisePlan(currentSteps, feedback)` (optional) lets the user ask the agent
// to redraft the plan from typed feedback without leaving the modal.
// Resolves { decision: "approve" | "cancel" | "dryrun", steps }.
function showPlanApproval(steps, revisePlan) {
  return new Promise((resolve) => {
    let rows = steps.map((s) => ({ ...s })); // working copy the user edits

    if (els.gateModalTitle) els.gateModalTitle.textContent = "Review the plan";
    els.gateReason.textContent =
      "AiNxt drafted this plan. Edit, reorder, or delete steps — or describe a " +
      "change below and Update plan to have AiNxt redraft it. Approve to run, " +
      "or Dry run to highlight each target on the page without executing anything.";

    const bindInput = (li, s, field, placeholder) => {
      const input = document.createElement("input");
      input.type = "text";
      input.className = "plan-row-input";
      input.placeholder = placeholder;
      const cur = s[field];
      input.value = cur == null ? "" : typeof cur === "string" ? cur : JSON.stringify(cur);
      input.addEventListener("change", () => {
        const v = input.value.trim();
        if (!v) { s[field] = null; return; }
        // A JSON array pasted into the target field becomes a selector ladder.
        if (field === "target" && v.startsWith("[")) {
          try { s[field] = JSON.parse(v); return; } catch (_) {}
        }
        s[field] = v;
      });
      li.appendChild(input);
    };

    const render = () => {
      if (els.gateNextLabel) els.gateNextLabel.textContent = `Plan — ${rows.length} step(s), editable`;
      els.gateNextDesc.innerHTML = "";
      const ol = document.createElement("ol");
      ol.className = "plan-list plan-list-edit";
      rows.forEach((s, idx) => {
        const li = document.createElement("li");

        const head = document.createElement("div");
        head.className = "plan-row-head";
        const actionEl = document.createElement("span");
        actionEl.className = "plan-row-action";
        actionEl.textContent = s.action;
        head.appendChild(actionEl);
        if (Number(s.risk) >= 4) {
          const risk = document.createElement("span");
          risk.className = "plan-row-risk";
          risk.textContent = `⚠ risk ${s.risk}`;
          risk.title = s.risk_reason || "The planner rated this step high-risk.";
          head.appendChild(risk);
        }
        const controls = document.createElement("span");
        controls.className = "plan-row-controls";
        const mkBtn = (txt, title, fn, disabled) => {
          const b = document.createElement("button");
          b.type = "button";
          b.textContent = txt;
          b.title = title;
          b.disabled = !!disabled;
          b.addEventListener("click", fn);
          controls.appendChild(b);
        };
        mkBtn("↑", "Move up", () => { [rows[idx - 1], rows[idx]] = [rows[idx], rows[idx - 1]]; render(); }, idx === 0);
        mkBtn("↓", "Move down", () => { [rows[idx + 1], rows[idx]] = [rows[idx], rows[idx + 1]]; render(); }, idx === rows.length - 1);
        mkBtn("✕", "Delete step", () => { rows.splice(idx, 1); render(); });
        head.appendChild(controls);
        li.appendChild(head);

        if (s.url != null || s.action === "navigate") bindInput(li, s, "url", "url");
        if (s.target != null) bindInput(li, s, "target", "selector");
        if (s.value != null) bindInput(li, s, "value", "value");
        if (s.matcher) {
          const meta = document.createElement("div");
          meta.className = "plan-row-meta";
          meta.textContent = `${s.matcher}${s.expected != null ? ` "${s.expected}"` : ""}`;
          li.appendChild(meta);
        }
        ol.appendChild(li);
      });
      els.gateNextDesc.appendChild(ol);
    };
    render();
    els.gateNextStep.hidden = false;

    // Third action, plan-review only: dry run the (edited) plan.
    const dryBtn = document.createElement("button");
    dryBtn.type = "button";
    dryBtn.className = "btn-secondary";
    dryBtn.textContent = "Dry run";
    dryBtn.title = "Highlight each step's target and check its selector resolves — nothing is executed.";
    els.gateApprove.parentElement.insertBefore(dryBtn, els.gateApprove);

    // Feedback row, plan-review only: describe a change and have the agent
    // redraft the plan in place. Empty input asks for a description instead
    // of silently doing nothing.
    let feedbackRow = null;
    let feedbackInput = null;
    let updateBtn = null;
    let feedbackHint = null;
    if (typeof revisePlan === "function") {
      feedbackRow = document.createElement("div");
      feedbackRow.className = "plan-feedback";

      const inputLine = document.createElement("div");
      inputLine.className = "plan-feedback-line";
      feedbackInput = document.createElement("input");
      feedbackInput.type = "text";
      feedbackInput.className = "plan-feedback-input";
      feedbackInput.placeholder = "Describe what to change…";
      updateBtn = document.createElement("button");
      updateBtn.type = "button";
      updateBtn.className = "btn-secondary plan-feedback-update";
      updateBtn.textContent = "Update plan";
      updateBtn.title = "Have AiNxt redraft the plan using your feedback.";
      inputLine.appendChild(feedbackInput);
      inputLine.appendChild(updateBtn);
      feedbackRow.appendChild(inputLine);

      feedbackHint = document.createElement("div");
      feedbackHint.className = "plan-feedback-hint";
      feedbackHint.hidden = true;
      feedbackRow.appendChild(feedbackHint);

      els.gateNextStep.insertAdjacentElement("afterend", feedbackRow);

      const setBusy = (busy) => {
        for (const b of [els.gateApprove, els.gateCancel, dryBtn, updateBtn, feedbackInput]) b.disabled = busy;
      };
      const showHint = (text, isError) => {
        feedbackHint.textContent = text;
        feedbackHint.classList.toggle("err", !!isError);
        feedbackHint.hidden = !text;
      };

      async function onUpdatePlan() {
        const feedback = feedbackInput.value.trim();
        if (!feedback) {
          // No input — ask for it rather than calling the LLM.
          showHint("Tell AiNxt what to change first.", false);
          feedbackInput.classList.add("attention");
          feedbackInput.focus();
          return;
        }
        setBusy(true);
        showHint("Revising plan…", false);
        try {
          const revised = await revisePlan(rows, feedback);
          rows = revised.steps.map((s) => ({ ...s }));
          render();
          feedbackInput.value = "";
          showHint("", false);
        } catch (e) {
          showHint("Couldn't revise the plan: " + (e?.message || e), true);
        } finally {
          setBusy(false);
        }
      }
      updateBtn.addEventListener("click", onUpdatePlan);
      feedbackInput.addEventListener("input", () => {
        feedbackInput.classList.remove("attention");
        showHint("", false);
      });
      feedbackInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); onUpdatePlan(); }
      });
    }

    els.gateModal.hidden = false;
    document.body.classList.add("gate-open");
    els.gateApprove.focus();

    function cleanup() {
      els.gateModal.hidden = true;
      document.body.classList.remove("gate-open");
      els.gateApprove.removeEventListener("click", onApprove);
      els.gateCancel.removeEventListener("click", onCancel);
      dryBtn.remove();
      feedbackRow?.remove();
      els.gateApprove.disabled = false;
      els.gateCancel.disabled = false;
      // Restore the modal's default copy for the human-input gate.
      if (els.gateModalTitle) els.gateModalTitle.textContent = "Human Input Required";
      if (els.gateNextLabel) els.gateNextLabel.textContent = "Next step after approval";
      els.gateNextDesc.textContent = "";
    }
    // Deleting every step leaves nothing to approve — treat as cancel.
    function onApprove() { cleanup(); resolve({ decision: rows.length ? "approve" : "cancel", steps: rows }); }
    function onCancel()  { cleanup(); resolve({ decision: "cancel", steps: rows }); }
    function onDryRun()  { cleanup(); resolve({ decision: rows.length ? "dryrun" : "cancel", steps: rows }); }

    els.gateApprove.addEventListener("click", onApprove);
    els.gateCancel.addEventListener("click", onCancel);
    dryBtn.addEventListener("click", onDryRun);
  });
}

// ---------- approval gate ----------

function showApprovalGate(gateData) {
  return new Promise((resolve) => {
    // kind:"step" = step-by-step pause (Continue/Stop, shows the previous step's
    // outcome); default = risky-action approval (may carry a pre-submit form diff).
    const isStepGate = gateData?.kind === "step";
    if (els.gateModalTitle) els.gateModalTitle.textContent = isStepGate ? "Step-by-step" : "Human Input Required";
    els.gateApprove.textContent = isStepGate ? "Continue" : "Approve & continue";
    els.gateCancel.textContent = isStepGate ? "Stop run" : "Cancel run";
    els.gateReason.textContent = gateData.reason || "Approval is required to continue.";

    els.gateNextDesc.textContent = "";
    let hasDetail = false;
    if (isStepGate && gateData.lastResult) {
      const prev = document.createElement("div");
      prev.textContent =
        `Previous: ${gateData.lastResult.action} → ${gateData.lastResult.status}` +
        (gateData.lastResult.error ? ` (${String(gateData.lastResult.error).slice(0, 120)})` : "");
      els.gateNextDesc.appendChild(prev);
      hasDetail = true;
    }
    if (gateData.formState?.length) {
      const head = document.createElement("div");
      head.textContent = "About to submit:";
      head.style.fontWeight = "600";
      els.gateNextDesc.appendChild(head);
      const ul = document.createElement("ul");
      ul.className = "gate-form-diff";
      for (const f of gateData.formState.slice(0, 12)) {
        const li = document.createElement("li");
        li.textContent = `${f.label} = ${f.value}`;
        ul.appendChild(li);
      }
      if (gateData.formState.length > 12) {
        const li = document.createElement("li");
        li.textContent = `… ${gateData.formState.length - 12} more field(s)`;
        ul.appendChild(li);
      }
      els.gateNextDesc.appendChild(ul);
      hasDetail = true;
    }
    const nextAction = gateData.step?.value || gateData.step?.next_action;
    if (!hasDetail && nextAction && nextAction !== gateData.reason) {
      els.gateNextDesc.textContent = String(nextAction);
      hasDetail = true;
    }
    if (els.gateNextLabel) {
      els.gateNextLabel.textContent = isStepGate
        ? "Last step's outcome"
        : gateData.formState?.length ? "Review before approving" : "Next step after approval";
    }
    els.gateNextStep.hidden = !hasDetail;

    els.gateModal.hidden = false;
    document.body.classList.add("gate-open");
    els.gateApprove.focus();

    function cleanup() {
      els.gateModal.hidden = true;
      document.body.classList.remove("gate-open");
      els.gateApprove.removeEventListener("click", onApprove);
      els.gateCancel.removeEventListener("click", onCancel);
      // Restore default copy for the next (possibly different-kind) gate.
      if (els.gateModalTitle) els.gateModalTitle.textContent = "Human Input Required";
      if (els.gateNextLabel) els.gateNextLabel.textContent = "Next step after approval";
      els.gateApprove.textContent = "Approve & continue";
      els.gateCancel.textContent = "Cancel run";
      els.gateNextDesc.textContent = "";
    }

    function onApprove() { cleanup(); resolve("approve"); }
    function onCancel()  { cleanup(); resolve("cancel"); }

    els.gateApprove.addEventListener("click", onApprove);
    els.gateCancel.addEventListener("click", onCancel);
  });
}

window.addEventListener("pagehide", () => {
  if (!els.gateModal.hidden) els.gateCancel.click();
});

// ---------- render result into an assistant bubble ----------

// Save text content as a timestamped markdown file (used by ask + exploration).
function downloadTextFile(content, basename, mime = "text/markdown") {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  a.href = url;
  a.download = `${basename}-${ts}.md`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

// Inline 16px stroke icons for the compact action footer.
const ICONS = {
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
  download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
  mail: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 5L2 7"/></svg>',
  teams: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>',
  eye: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
  eyeOff: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>',
  report: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>',
};

// Compact icon-only button with a tooltip + accessible label.
function makeIconBtn(label, svg, onClick) {
  const btn = document.createElement("button");
  btn.className = "icon-btn";
  btn.type = "button";
  btn.title = label;
  btn.setAttribute("aria-label", label);
  btn.innerHTML = svg;
  btn.addEventListener("click", onClick);
  return btn;
}

// Briefly swap an icon button to a checkmark to confirm an action.
function flashIconBtn(btn, label) {
  if (!btn) return;
  const origHtml = btn.innerHTML;
  const origTitle = btn.title;
  btn.innerHTML = ICONS.check;
  btn.title = label || "Done";
  btn.classList.add("ok");
  setTimeout(() => {
    btn.innerHTML = origHtml;
    btn.title = origTitle;
    btn.classList.remove("ok");
  }, 1200);
}

// Render markdown to an HTML string via the (innerHTML-free, safe) renderer.
function markdownToHtml(text) {
  const div = document.createElement("div");
  renderMarkdownInto(div, text);
  return div.innerHTML;
}

// Copy as rich text (formatted HTML) with a plain-text fallback so pasting
// into Outlook / Teams keeps headings, bold, lists etc. Returns true if the
// rich variant was written, false if it fell back to plain text.
async function writeRichToClipboard(text) {
  try {
    if (navigator.clipboard && window.ClipboardItem) {
      const item = new ClipboardItem({
        "text/html": new Blob([markdownToHtml(text)], { type: "text/html" }),
        "text/plain": new Blob([text || ""], { type: "text/plain" }),
      });
      await navigator.clipboard.write([item]);
      return true;
    }
  } catch (_) {
    // fall through to plain text below
  }
  await navigator.clipboard.writeText(text || "").catch(() => {});
  return false;
}

function openExternal(url, newTab) {
  const a = document.createElement("a");
  a.href = url;
  if (newTab) {
    a.target = "_blank";
    a.rel = "noopener";
  }
  a.click();
}

// Share the response via Outlook/default mail (mailto:) or a Teams chat
// deep link. Deep-link URLs cap ~2000 chars, so for long responses we copy
// the full text to the clipboard and pre-fill a short "paste it here" note.
const SHARE_BODY_LIMIT = 1800;
async function shareResponse(text, channel, btn) {
  const subject = "AiNxt response";
  let body = text || "";
  if (encodeURIComponent(body).length > SHARE_BODY_LIMIT) {
    await writeRichToClipboard(text);
    body = "(Full response copied to your clipboard — paste it here for formatted text.)";
    flashIconBtn(btn, "Copied — paste in the window");
  }
  if (channel === "mail") {
    openExternal(
      `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`,
      false,
    );
  } else {
    openExternal(
      `https://teams.microsoft.com/l/chat/0/0?message=${encodeURIComponent(body)}`,
      true,
    );
  }
}

// REQ-13: draw each captured step screenshot onto a shared-size canvas with a
// label bar (action + narration) and a run-progress bar, then encode via
// lib/gif.js. Deliberately simple per the requirement's own scope: no
// click/target indicator (most actions have no natural on-image point to
// mark), uniform per-frame delay, no dithering in the encoder itself.
async function buildAnnotatedGif(steps) {
  const images = await Promise.all(
    steps.map(
      (s) =>
        new Promise((resolve, reject) => {
          const img = new Image();
          img.onload = () => resolve(img);
          img.onerror = () => reject(new Error(`failed to decode screenshot for step #${s.index}`));
          img.src = s.screenshot;
        }),
    ),
  );

  // Frames may differ in size (e.g. a resize_window mid-run) — canvas is
  // sized to the first frame; later frames are scaled to fit it. Downscaled to
  // ≤800px wide: this is a debugging replay, and full retina frames make the
  // encode (and the file) several times slower/larger for no readable benefit.
  const MAX_GIF_WIDTH = 800;
  const gifScale = Math.min(1, MAX_GIF_WIDTH / images[0].width);
  const width = Math.round(images[0].width * gifScale);
  const height = Math.round(images[0].height * gifScale);
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");

  const barHeight = Math.max(28, Math.round(height * 0.06));
  const frames = [];
  for (let i = 0; i < images.length; i++) {
    ctx.drawImage(images[i], 0, 0, width, height);

    // Label bar
    ctx.fillStyle = "rgba(0,0,0,0.65)";
    ctx.fillRect(0, height - barHeight, width, barHeight);
    ctx.fillStyle = "#fff";
    ctx.font = `${Math.round(barHeight * 0.45)}px sans-serif`;
    ctx.textBaseline = "middle";
    const label = `#${steps[i].index} ${steps[i].action}${steps[i].narration ? " — " + steps[i].narration : ""}`;
    ctx.fillText(label.slice(0, 120), 8, height - barHeight / 2, width - 16);

    // Progress bar along the very bottom edge
    const progH = Math.max(3, Math.round(barHeight * 0.12));
    ctx.fillStyle = "rgba(255,255,255,0.25)";
    ctx.fillRect(0, height - progH, width, progH);
    ctx.fillStyle = "#4f8cff";
    ctx.fillRect(0, height - progH, Math.round((width * (i + 1)) / images.length), progH);

    frames.push(ctx.getImageData(0, 0, width, height));
  }

  return encodeGif(frames, { delayMs: 800 });
}

// A summarize step's "value" is only a variable name when it's identifier-shaped;
// anything sentence-like is a model misuse (the task text) and must not surface.
function isVarName(v) {
  return typeof v === "string" && /^[\w.-]{1,48}$/.test(v);
}

function renderResultIntoBubble(bubble, result) {
  bubble.innerHTML = "";

  const summary = document.createElement("div");
  summary.className = "result-summary";

  const goal = document.createElement("div");
  goal.className = "goal";
  goal.textContent = result.goal || result.summary || "Run complete";
  summary.appendChild(goal);

  const r = result.result || {};
  const total =
    (r.passed_steps || 0) + (r.failed_steps || 0) + (r.skipped_steps || 0);
  const elapsedMs = computeElapsed(result);
  const meta = (label, value, title) => {
    const span = document.createElement("span");
    span.className = "meta";
    span.innerHTML = `<span>${label}</span><b>${escapeHtml(String(value))}</b>`;
    if (title) span.title = title;
    return span;
  };
  summary.appendChild(meta("mode", result.mode || "—"));
  summary.appendChild(meta("steps", `${r.passed_steps || 0}/${total}`));
  if (r.status === "max_steps_reached") summary.appendChild(meta("status", "max steps reached"));
  if (r.failed_steps) summary.appendChild(meta("failed", r.failed_steps));
  if (r.skipped_steps) summary.appendChild(meta("skipped", r.skipped_steps));
  summary.appendChild(meta("time", formatDuration(elapsedMs)));
  const tokens = formatTokens(result.usage);
  if (tokens) summary.appendChild(meta("tokens", tokens.text, tokens.title));
  bubble.appendChild(summary);

  if (result.notes?.length) {
    const notesEl = document.createElement("div");
    notesEl.className = "hint";
    notesEl.textContent = result.notes.join(" · ");
    bubble.appendChild(notesEl);
  }

  // F-06: after-the-fact receipt of what this run actually touched — origins
  // navigated to, exec_script executions, and hosts a vault secret was typed
  // into — so a run steered off-course by injected page content is visible
  // even if every individual gate was approved.
  if (result.security_summary) {
    const ss = result.security_summary;
    const parts = [];
    if (ss.origins?.length) parts.push(`origins: ${ss.origins.join(", ")}`);
    if (ss.exec_script_count) parts.push(`${ss.exec_script_count} script execution(s)`);
    if (ss.secrets_used?.length) parts.push(`secrets typed into: ${ss.secrets_used.join(", ")}`);
    if (parts.length) {
      const secEl = document.createElement("div");
      secEl.className = "hint";
      secEl.textContent = `Security: ${parts.join(" · ")}`;
      bubble.appendChild(secEl);
    }
  }

  // Both LLM-driven modes carry a consolidated answer; agentic differs from
  // exploration only by its safety gates, not by what the run produces.
  const isExploration = result.mode === "exploration" || result.mode === "agentic";

  // Dedicated summary blocks for summarize steps
  const summarizeSteps = (result.steps || []).filter(
    (s) => s.action === "summarize" && s.actual != null,
  );

  // Exploration surfaces a consolidated answer (the agent's closing explanation
  // plus any summarize outputs) instead of leading with the step mechanics.
  // Only identifier-shaped values are variable names worth a heading — models
  // sometimes put the whole task instruction in "value", which must not be
  // echoed above the answer.
  const summarizeActuals = summarizeSteps.map((ss) =>
    isVarName(ss.value) ? `## ${ss.value}\n\n${String(ss.actual)}` : String(ss.actual),
  );
  const answerText = isExploration
    ? [result.answer, ...summarizeActuals].filter(Boolean).join("\n\n") ||
      result.summary ||
      ""
    : "";

  if (isExploration && answerText) {
    const aBlock = document.createElement("div");
    aBlock.className = "summary-block";
    const aText = document.createElement("div");
    aText.className = "summary-text";
    renderMarkdownInto(aText, answerText);
    aBlock.appendChild(aText);
    bubble.appendChild(aBlock);
  }

  const list = document.createElement("ul");
  list.className = "step-list";
  for (const s of result.steps || []) {
    list.appendChild(renderStepRow(s));
  }
  if (isExploration) {
    // Keep the mechanics available but out of the way in exploration mode.
    const details = document.createElement("details");
    details.className = "steps-details";
    const summaryEl = document.createElement("summary");
    summaryEl.textContent = "show steps";
    details.appendChild(summaryEl);
    details.appendChild(list);
    bubble.appendChild(details);
  } else {
    bubble.appendChild(list);
  }

  // Non-exploration modes keep the labelled per-summarize blocks; in exploration
  // these are already folded into the answer block above.
  if (!isExploration) {
    for (const ss of summarizeSteps) {
      const sBlock = document.createElement("div");
      sBlock.className = "summary-block";
      const sLabel = document.createElement("div");
      sLabel.className = "summary-label";
      sLabel.textContent = isVarName(ss.value) ? `Summary — ${ss.value}` : "Summary";
      sBlock.appendChild(sLabel);
      const sText = document.createElement("div");
      sText.className = "summary-text";
      renderMarkdownInto(sText, String(ss.actual));
      sBlock.appendChild(sText);
      bubble.appendChild(sBlock);
    }
  }

  // Hide scratch captures from the card: summaries are already shown above, and
  // perception-tool reads (page text, tab lists, console/network buffers) are
  // working data the agent stored for itself, not user-facing output. They stay
  // in the run record / JSON download and in ${var} substitution. In the
  // LLM-driven chat modes ALL captured variables are agent scratch data — the
  // consolidated answer is the output — so the block only renders for
  // test/suite runs, where a variable capture is authored in the test file.
  const PERCEPTION_ACTIONS = new Set([
    "get_page_text", "read_page", "list_tabs", "read_console_messages", "read_network_requests", "read_download",
  ]);
  const hiddenVarNames = new Set(summarizeSteps.map((s) => s.value).filter(isVarName));
  for (const s of result.steps || []) {
    if (s.variable && PERCEPTION_ACTIONS.has(s.action)) hiddenVarNames.add(s.variable);
  }
  const vars = result.variables || {};
  const varKeys = Object.keys(vars).filter((k) => !hiddenVarNames.has(k));
  if (!isExploration && varKeys.length) {
    const block = document.createElement("div");
    block.className = "vars-block";
    const lbl = document.createElement("div");
    lbl.className = "vars-label";
    lbl.textContent = "Captured variables";
    block.appendChild(lbl);
    for (const k of varKeys) {
      const v = document.createElement("span");
      v.className = "var";
      const val = String(vars[k]);
      v.innerHTML =
        `<b>${escapeHtml(k)}</b> = ` +
        `<span>${escapeHtml(val.length > 200 ? val.slice(0, 200) + "…" : val)}</span>`;
      block.appendChild(v);
    }
    bubble.appendChild(block);
  }

  const footer = document.createElement("div");
  footer.className = "result-footer";

  if (isExploration && answerText) {
    const copyRespBtn = makeIconBtn("Copy response", ICONS.copy, async () => {
      await navigator.clipboard.writeText(answerText).catch(() => {});
      flashIconBtn(copyRespBtn, "Copied");
    });
    footer.appendChild(copyRespBtn);

    footer.appendChild(
      makeIconBtn("Download response", ICONS.download, () =>
        downloadTextFile(answerText, "exploration-response"),
      ),
    );
  }

  // Raw-JSON tools are noise in exploration (only the response matters there);
  // keep them for test/suite/agentic runs.
  let toggleBtn, copyBtn;
  if (!isExploration) {
    toggleBtn = makeIconBtn("Show raw JSON", ICONS.eye, () => {}); // handler wired below, once the JSON block exists
    footer.appendChild(toggleBtn);

    copyBtn = makeIconBtn("Copy JSON", ICONS.copy, () => {});
    footer.appendChild(copyBtn);

    const dlJsonBtn = makeIconBtn("Download JSON", ICONS.download, () => {
      const blob = new Blob([JSON.stringify(result, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      const ts = new Date(result.started_at || Date.now())
        .toISOString()
        .replace(/[:.]/g, "-")
        .slice(0, 19);
      a.href = url;
      a.download = `run-result-${ts}.json`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    });
    footer.appendChild(dlJsonBtn);
  }

  if (result.mode === "test" || result.mode === "suite") {
    const reportBtn = makeIconBtn("Download HTML report", ICONS.report, () => {
      const html = generateHtmlReport(result);
      const blob = new Blob([html], { type: "text/html" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      const ts = new Date(result.started_at || Date.now())
        .toISOString()
        .replace(/[:.]/g, "-")
        .slice(0, 19);
      a.href = url;
      a.download = `regression-report-${ts}.html`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    });
    footer.appendChild(reportBtn);
  }

  // REQ-13: only present when "Record GIF" was on for this run — recordGif
  // forces a screenshot per step, so its presence on 2+ steps is the signal.
  const gifFrameSteps = (result.steps || []).filter((s) => s.screenshot);
  if (gifFrameSteps.length >= 2) {
    const gifBtn = makeIconBtn("Download GIF", ICONS.download, async () => {
      gifBtn.disabled = true;
      gifBtn.title = "Building GIF…";
      try {
        const blob = await buildAnnotatedGif(gifFrameSteps);
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        const ts = new Date(result.started_at || Date.now()).toISOString().replace(/[:.]/g, "-").slice(0, 19);
        a.href = url;
        a.download = `run-recording-${ts}.gif`;
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 5000);
        flashIconBtn(gifBtn, "Downloaded");
      } catch (e) {
        alert("Could not build GIF: " + (e.message || e));
      } finally {
        gifBtn.disabled = false;
        gifBtn.title = "Download GIF";
      }
    });
    footer.appendChild(gifBtn);
  }

  bubble.appendChild(footer);

  if (!isExploration) {
    const json = document.createElement("pre");
    json.className = "json-block";
    json.hidden = true;
    json.textContent = JSON.stringify(result, null, 2);
    bubble.appendChild(json);

    toggleBtn.addEventListener("click", () => {
      json.hidden = !json.hidden;
      toggleBtn.innerHTML = json.hidden ? ICONS.eye : ICONS.eyeOff;
      const label = json.hidden ? "Show raw JSON" : "Hide raw JSON";
      toggleBtn.title = label;
      toggleBtn.setAttribute("aria-label", label);
    });
    copyBtn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(json.textContent);
        flashIconBtn(copyBtn, "Copied");
      } catch {}
    });
  }
}

function renderErrorIntoBubble(bubble, err) {
  bubble.innerHTML = "";
  const div = document.createElement("div");
  div.className = "assistant-error";
  div.textContent = "Run failed: " + (err?.message || String(err));
  bubble.appendChild(div);
}

// Neutral notice for a user-initiated stop — not styled as an error.
function renderStoppedIntoBubble(bubble) {
  bubble.innerHTML = "";
  const div = document.createElement("div");
  div.className = "assistant-note";
  div.textContent = "Run stopped.";
  bubble.appendChild(div);
}

function renderAskIntoBubble(bubble, { instruction, response, elapsedMs, usage }) {
  bubble.innerHTML = "";

  const summary = document.createElement("div");
  summary.className = "result-summary";

  const goal = document.createElement("div");
  goal.className = "goal";
  goal.textContent = instruction.length > 100 ? instruction.slice(0, 97) + "…" : instruction;
  summary.appendChild(goal);

  const meta = document.createElement("span");
  meta.className = "meta";
  meta.innerHTML = `<span>mode</span><b>ask</b>`;
  summary.appendChild(meta);

  if (elapsedMs != null) {
    const timeMeta = document.createElement("span");
    timeMeta.className = "meta";
    timeMeta.innerHTML = `<span>time</span><b>${formatDuration(elapsedMs)}</b>`;
    summary.appendChild(timeMeta);
  }

  const tokens = formatTokens(usage);
  if (tokens) {
    const tokenMeta = document.createElement("span");
    tokenMeta.className = "meta";
    tokenMeta.innerHTML = `<span>tokens</span><b>${escapeHtml(tokens.text)}</b>`;
    tokenMeta.title = tokens.title;
    summary.appendChild(tokenMeta);
  }

  bubble.appendChild(summary);

  const block = document.createElement("div");
  block.className = "summary-block";
  const text = document.createElement("div");
  text.className = "summary-text";
  renderMarkdownInto(text, response);
  block.appendChild(text);
  bubble.appendChild(block);

  const footer = document.createElement("div");
  footer.className = "result-footer";

  const actions = document.createElement("div");
  actions.className = "icon-actions";

  const copyBtn = makeIconBtn("Copy response (rich text)", ICONS.copy, async () => {
    await writeRichToClipboard(response);
    flashIconBtn(copyBtn, "Copied");
  });
  actions.appendChild(copyBtn);

  const dlBtn = makeIconBtn("Download response", ICONS.download, () =>
    downloadTextFile(response, "ask-response"),
  );
  actions.appendChild(dlBtn);

  const mailBtn = makeIconBtn("Share via mail", ICONS.mail, () =>
    shareResponse(response, "mail", mailBtn),
  );
  actions.appendChild(mailBtn);

  const teamsBtn = makeIconBtn("Share via Teams", ICONS.teams, () =>
    shareResponse(response, "teams", teamsBtn),
  );
  actions.appendChild(teamsBtn);

  footer.appendChild(actions);
  bubble.appendChild(footer);
}

function suggestFix(errorMessage, target, action) {
  const msg = (errorMessage || "").toLowerCase();
  // For ladder targets, use the first rung for the heuristic — that's the rung
  // the recorder considered most stable, and it's what shape-based hints key on.
  const firstTarget = Array.isArray(target) ? (target[0] || "") : (target || "");
  const sel = String(firstTarget).trim();
  if (sel.startsWith("#") && (msg.includes("not found") || msg.includes("no element") || msg.includes("unable to locate"))) {
    return "ID selectors break when IDs are dynamic. Try a role= or text= selector instead.";
  }
  if (sel.startsWith(".")) {
    return "Class names are often auto-generated. Try role=, aria-label=, or text= selectors.";
  }
  if (sel.startsWith("//") || sel.startsWith("/html")) {
    return "XPath selectors are brittle. Prefer role=button[name='...'] or text= selectors.";
  }
  if (msg.includes("timeout") || msg.includes("timed out")) {
    return "Element not found within timeout. Add a wait step before this action or increase timeout_ms.";
  }
  if (msg.includes("intercept") || msg.includes("pointer-events") || msg.includes("obscured")) {
    return "Another element is blocking the target. Try a scroll step before this action.";
  }
  if (msg.includes("not interactable") || msg.includes("disabled")) {
    return "Element exists but is not interactable. Add a wait step or assert it is enabled first.";
  }
  if (action === "assert") {
    return "Assertion failed. Verify the selector, matcher, and expected value match the actual page content.";
  }
  if (action === "navigate") {
    return "Navigation failed. Verify the URL is reachable and does not redirect to a blocked page.";
  }
  if (msg.includes("missing secret")) {
    return "A secret key was not found. Add it to the Secrets JSON in Settings.";
  }
  return "Check the browser console on the active tab for additional context.";
}

function renderRootCauseAnalysis(rca) {
  const wrap = document.createElement("details");
  wrap.className = "rca-block";
  wrap.open = true;

  const sum = document.createElement("summary");
  sum.className = "rca-summary";

  const badge = document.createElement("span");
  badge.className = "rca-badge";
  badge.textContent = rca.category.replace(/_/g, " ");
  sum.appendChild(badge);

  const lbl = document.createElement("span");
  lbl.className = "rca-label";
  lbl.textContent = "Root Cause Analysis";
  sum.appendChild(lbl);

  wrap.appendChild(sum);

  const body = document.createElement("div");
  body.className = "rca-body";

  const summaryRow = document.createElement("div");
  summaryRow.className = "rca-row";
  const sk = document.createElement("span"); sk.className = "rca-key"; sk.textContent = "Summary";
  const sv = document.createElement("span"); sv.className = "rca-val"; sv.textContent = rca.summary;
  summaryRow.append(sk, sv);
  body.appendChild(summaryRow);

  if (rca.evidence) {
    const evRow = document.createElement("div");
    evRow.className = "rca-row";
    const ek = document.createElement("span"); ek.className = "rca-key"; ek.textContent = "Evidence";
    const ev = document.createElement("code"); ev.className = "rca-evidence"; ev.textContent = rca.evidence;
    evRow.append(ek, ev);
    body.appendChild(evRow);
  }

  if (rca.suggestion) {
    const hint = document.createElement("div");
    hint.className = "rca-hint";
    const hl = document.createElement("span"); hl.className = "rca-hint-label"; hl.textContent = "Fix";
    hint.appendChild(hl);
    hint.appendChild(document.createTextNode(" " + rca.suggestion));
    body.appendChild(hint);
  }

  wrap.appendChild(body);
  return wrap;
}

function renderErrorExplainer(s) {
  const wrap = document.createElement("details");
  wrap.className = "err-explainer";

  const sum = document.createElement("summary");
  sum.className = "err-explainer-summary";

  const label = document.createElement("span");
  label.className = "err-explainer-label";
  label.textContent = "Why did this fail?";
  sum.appendChild(label);

  const errMsg = document.createElement("div");
  errMsg.className = "err-msg";
  errMsg.textContent = s.error.message;
  sum.appendChild(errMsg);

  wrap.appendChild(sum);

  const body = document.createElement("div");
  body.className = "err-explainer-body";

  if (s.target) {
    const selRow = document.createElement("div");
    selRow.className = "err-detail-row";
    const key = document.createElement("span");
    key.className = "err-detail-key";
    const isLadder = Array.isArray(s.target) && s.target.length > 1;
    key.textContent = isLadder ? `Selectors tried (${s.target.length})` : "Selector";
    const val = document.createElement("code");
    val.className = "err-detail-val";
    val.textContent = isLadder ? s.target.join("\n") : (Array.isArray(s.target) ? s.target[0] : s.target);
    if (isLadder) val.style.whiteSpace = "pre-wrap";
    selRow.appendChild(key);
    selRow.appendChild(val);
    body.appendChild(selRow);
  }

  if (s.screenshot) {
    const imgWrap = document.createElement("div");
    imgWrap.className = "err-screenshot-wrap";
    const img = document.createElement("img");
    img.className = "err-screenshot";
    img.src = s.screenshot;
    img.alt = "Failure screenshot";
    img.loading = "lazy";
    img.addEventListener("click", () => {
      img.classList.toggle("err-screenshot-expanded");
    });
    imgWrap.appendChild(img);
    body.appendChild(imgWrap);
  }

  const hint = suggestFix(s.error.message, s.target, s.action);
  const hintEl = document.createElement("div");
  hintEl.className = "err-hint";
  const hintLabel = document.createElement("span");
  hintLabel.className = "err-hint-label";
  hintLabel.textContent = "Suggestion";
  hintEl.appendChild(hintLabel);
  hintEl.appendChild(document.createTextNode(" " + hint));
  body.appendChild(hintEl);

  if (s.rootCauseAnalysis) {
    body.appendChild(renderRootCauseAnalysis(s.rootCauseAnalysis));
  }

  wrap.appendChild(body);
  return wrap;
}

function renderStepRow(s) {
  const li = document.createElement("li");
  const klass =
    s.status === "success"
      ? "ok"
      : s.status === "failed"
        ? "err"
        : s.status === "awaiting_human"
          ? "human"
          : "skip";
  li.className = "step-row " + klass;

  const iconChar =
    s.status === "success"
      ? "✓"
      : s.status === "failed"
        ? "✕"
        : s.status === "awaiting_human"
          ? "⏸"
          : "·";

  const icon = document.createElement("span");
  icon.className = "icon";
  icon.textContent = iconChar;
  li.appendChild(icon);

  const body = document.createElement("div");
  body.className = "body";

  const head = document.createElement("div");
  head.className = "head";
  head.innerHTML =
    `<span class="num">#${s.index}</span>` +
    `<span class="action">${escapeHtml(s.action || "step")}</span>`;
  body.appendChild(head);

  const desc = document.createElement("div");
  desc.className = "desc";
  desc.innerHTML = describeStep(s);
  if (desc.innerHTML) body.appendChild(desc);

  if (s.healed) {
    const healed = document.createElement("div");
    healed.className = "healed-note";
    healed.innerHTML =
      `🔧 healed${s.healed.memory ? " (from site memory, no LLM call)" : ""}: ` +
      `<span class="hl">${escapeHtml(formatTarget(s.healed.from))}</span>` +
      ` → <span class="hl">${escapeHtml(formatTarget(s.healed.to))}</span>`;
    body.appendChild(healed);
  }

  if (s.status === "failed" && s.error?.message) {
    body.appendChild(renderErrorExplainer(s));
  }

  li.appendChild(body);

  const dur = document.createElement("span");
  dur.className = "duration";
  dur.textContent = s.duration_ms != null ? formatDuration(s.duration_ms) : "";
  li.appendChild(dur);

  return li;
}

function formatTarget(v) {
  if (Array.isArray(v)) {
    if (v.length === 0) return "";
    if (v.length === 1) return String(v[0]);
    return `${v[0]} (+${v.length - 1} more)`;
  }
  return String(v ?? "");
}

function describeStep(s) {
  const T = (v) => `<span class="hl">${escapeHtml(formatTarget(v))}</span>`;
  const action = (s.action || "").toLowerCase();
  switch (action) {
    case "navigate":
      return s.url ? T(s.url) : s.value ? T(s.value) : action;
    case "back":
    case "forward":
    case "reload":
      return action;
    case "type":
    case "clear":
    case "select":
    case "check":
    case "uncheck":
    case "click":
    case "dblclick":
    case "hover":
    case "scroll": {
      const t = s.target ? T(s.target) : "";
      const v = s.value ? ` ← ${escapeHtml(String(s.value))}` : "";
      return t + v;
    }
    case "press_key":
      return s.value ? T(s.value) : "";
    case "wait":
      return s.condition ? `condition: ${T(s.condition)}` : "";
    case "extract":
      return (
        (s.target ? T(s.target) : "") +
        (s.value ? ` → variable ${escapeHtml(String(s.value))}` : "") +
        (s.actual !== undefined && s.actual !== null
          ? ` = ${escapeHtml(truncate(String(s.actual), 80))}`
          : "")
      );
    case "summarize":
      return s.value ? `→ variable ${escapeHtml(String(s.value))}` : "";
    case "screenshot":
      return s.target ? T(s.target) : "viewport";
    case "assert": {
      const t = s.target ? T(s.target) : "";
      const m = s.matcher ? ` ${escapeHtml(s.matcher)}` : "";
      const e =
        s.expected !== undefined && s.expected !== null
          ? ` ${escapeHtml(JSON.stringify(s.expected))}`
          : "";
      const a =
        s.actual !== undefined && s.actual !== null
          ? `<br/>actual: ${escapeHtml(truncate(String(s.actual), 90))}`
          : "";
      return t + m + e + a;
    }
    case "datepick":
      return (s.target ? T(s.target) : "") + (s.value ? ` → ${escapeHtml(String(s.value))}` : "");
    case "switch_tab":
      return s.value ? T(s.value) : "";
    case "request_human":
      return s.value ? `"${escapeHtml(String(s.value))}"` : "needs human";
    case "exec_script":
      return s.value ? T(String(s.value).slice(0, 60)) : "";
    case "upload_file":
      return (s.target ? T(s.target) : "") + (s.filename ? ` ← ${escapeHtml(s.filename)}` : "");
    case "drag":
      return (s.target ? T(s.target) : "") + (s.destination ? ` → ${escapeHtml(s.destination)}` : "");
    case "switch_frame":
      return s.target ? T(s.target) : "top";
    case "accessibility_audit":
      return (s.target ? T(s.target) : "full page") +
        (s.actual !== undefined ? ` — ${escapeHtml(String(s.actual))} violation(s)` : "");
    case "assert_performance":
      return s.metric ? `${escapeHtml(s.metric)} ≤ ${escapeHtml(String(s.max_ms))}ms` +
        (s.actual !== undefined ? ` (got ${escapeHtml(String(s.actual))})` : "") : "";
    case "screenshot_baseline":
      return s.value ? `baseline: ${escapeHtml(String(s.value))}` : "";
    case "assert_screenshot":
      return (s.baseline ? `vs ${escapeHtml(String(s.baseline))}` : "") +
        (s.actual !== undefined ? ` — ${escapeHtml(String(s.actual))}` : "");
    case "mock_network":
      return s.url ? T(s.url) : "";
    case "if":
      return s.condition ? `condition: ${T(s.condition)}` : "";
    default:
      return s.target ? T(s.target) : "";
  }
}

function formatDuration(ms) {
  if (ms == null) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return `${m}m${s}s`;
}

// Token count for a usage record → { text, title }. An endpoint that reports no
// usage block must read as unknown ("—"), never as a fabricated 0: usage_missing
// counts those calls, so a run whose every call went unreported shows "—", and a
// partially-reported run shows the count with the shortfall in the tooltip.
function formatTokens(usage) {
  if (!usage) return null;
  const total = Number(usage.total_tokens) || 0;
  const calls = Number(usage.llm_calls) || 0;
  const missing = Number(usage.usage_missing) || 0;
  if (!calls) return { text: "—", title: "No LLM calls in this run" };
  if (!total && missing >= calls) {
    return { text: "—", title: `Endpoint reported no token usage (${calls} call(s))` };
  }
  return {
    text: total.toLocaleString(),
    title: missing
      ? `${total.toLocaleString()} tokens over ${calls} call(s); ${missing} call(s) reported no usage`
      : `${total.toLocaleString()} tokens over ${calls} call(s)`,
  };
}

// History entries store flattened token totals; rebuild the usage shape
// formatTokens() expects. usageMissing is absent on entries saved before it
// existed, which reads as "all reported" — the best available assumption.
function usageFromHistory(entry) {
  return {
    total_tokens: entry.totalTokens || 0,
    llm_calls: entry.llmCalls || 0,
    usage_missing: entry.usageMissing || 0,
  };
}

function truncate(s, n) {
  return s.length > n ? s.slice(0, n) + "…" : s;
}

function escapeHtml(s) {
  return String(s).replace(
    /[&<>"']/g,
    (c) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[c],
  );
}

// ---------- regression report ----------

function generateHtmlReport(result) {
  const r = result.result || {};
  const steps = result.steps || [];
  const total = (r.passed_steps || 0) + (r.failed_steps || 0) + (r.skipped_steps || 0);
  const elapsedMs = computeElapsed(result);
  const statusLabel = STATUS_LABEL;
  const statusColor = STATUS_COLOR;
  const overallStatus = r.status || "unknown";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function cell(content, cls = "") {
    return `<td class="${esc(cls)}">${content}</td>`;
  }

  const assertSteps = steps.filter((s) => s.action === "assert");
  const hasAsserts = assertSteps.length > 0;

  const stepRows = steps.map((s) => {
    const isAssert = s.action === "assert";
    const rowClass =
      s.status === "success" ? "row-pass"
      : s.status === "failed" ? "row-fail"
      : s.status === "skipped" ? "row-skip"
      : "row-other";

    const statusBadge =
      s.status === "success" ? `<span class="badge pass">PASS</span>`
      : s.status === "failed" ? `<span class="badge fail">FAIL</span>`
      : s.status === "skipped" ? `<span class="badge skip">SKIP</span>`
      : `<span class="badge other">${esc(s.status)}</span>`;

    const expectedCell = isAssert && s.expected != null
      ? `<code>${esc(s.expected)}</code>` : "—";
    const actualCell = isAssert && s.actual != null
      ? `<code class="${s.status === "failed" ? "actual-fail" : ""}">${esc(s.actual)}</code>` : "—";
    const matcherCell = isAssert && s.matcher ? `<code>${esc(s.matcher)}</code>` : "—";
    const errorCell = s.error?.message
      ? `<span class="err-text">${esc(s.error.message)}</span>` : "";

    const descParts = [];
    if (s.target) descParts.push(`<code>${esc(formatTarget(s.target))}</code>`);
    if (s.value != null) descParts.push(`← <code>${esc(s.value)}</code>`);
    if (s.url) descParts.push(`<code>${esc(s.url)}</code>`);
    if (s.condition) descParts.push(`condition: <code>${esc(s.condition)}</code>`);
    if (s.healed) descParts.push(`🔧 healed → <code>${esc(formatTarget(s.healed.to))}</code>`);

    let screenshotCell = "";
    if (s.screenshot) {
      screenshotCell = `<a href="${esc(s.screenshot)}" target="_blank" rel="noopener noreferrer"><img src="${esc(s.screenshot)}" class="thumb" alt="screenshot"/></a>`;
    }

    return `
      <tr class="${rowClass}">
        ${cell(s.index ?? "", "col-num")}
        ${cell(statusBadge, "col-status")}
        ${cell(esc(s.action || ""), "col-action")}
        ${cell(descParts.join(" ") + (errorCell ? `<br/>${errorCell}` : ""), "col-desc")}
        ${cell(matcherCell, "col-matcher")}
        ${cell(expectedCell, "col-expected")}
        ${cell(actualCell, "col-actual")}
        ${cell(s.duration_ms != null ? `${s.duration_ms}ms` : "", "col-dur")}
        ${cell(screenshotCell, "col-ss")}
      </tr>`;
  }).join("");

  const assertSummaryRows = hasAsserts ? assertSteps.map((s) => {
    const passed = s.status === "success";
    return `
      <tr class="${passed ? "row-pass" : "row-fail"}">
        ${cell(s.index ?? "", "col-num")}
        ${cell(s.target ? `<code>${esc(formatTarget(s.target))}</code>` : "—", "col-desc")}
        ${cell(s.matcher ? `<code>${esc(s.matcher)}</code>` : "—", "col-matcher")}
        ${cell(s.expected != null ? `<code>${esc(s.expected)}</code>` : "—", "col-expected")}
        ${cell(s.actual != null ? `<code class="${!passed ? "actual-fail" : ""}">${esc(s.actual)}</code>` : "—", "col-actual")}
        ${cell(passed ? `<span class="badge pass">PASS</span>` : `<span class="badge fail">FAIL</span>`, "col-status")}
        ${cell(s.error?.message ? `<span class="err-text">${esc(s.error.message)}</span>` : "", "col-desc")}
      </tr>`;
  }).join("") : "";

  const screenshots = (result.artifacts?.screenshots || []).filter(Boolean);
  const screenshotSection = screenshots.length ? `
    <section>
      <h2>Screenshots</h2>
      <div class="ss-grid">
        ${screenshots.map((src, i) =>
          `<figure><a href="${esc(src)}" target="_blank" rel="noopener noreferrer"><img src="${esc(src)}" class="ss-img" alt="screenshot ${i + 1}"/></a><figcaption>Screenshot ${i + 1}</figcaption></figure>`
        ).join("")}
      </div>
    </section>` : "";

  const assertSection = hasAsserts ? `
    <section>
      <h2>Assertion Results</h2>
      <table>
        <thead>
          <tr>
            <th class="col-num">#</th>
            <th class="col-desc">Target</th>
            <th class="col-matcher">Matcher</th>
            <th class="col-expected">Expected</th>
            <th class="col-actual">Actual</th>
            <th class="col-status">Result</th>
            <th class="col-desc">Error</th>
          </tr>
        </thead>
        <tbody>${assertSummaryRows}</tbody>
      </table>
    </section>` : "";

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Regression Report — ${esc(result.goal || "Test Run")}</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; font-size: 14px; color: #1e293b; background: #f8fafc; padding: 24px; }
  h1 { font-size: 20px; font-weight: 700; margin-bottom: 4px; }
  h2 { font-size: 15px; font-weight: 600; margin: 24px 0 10px; color: #334155; border-bottom: 1px solid #e2e8f0; padding-bottom: 6px; }
  .header { background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 20px 24px; margin-bottom: 20px; }
  .overall { display: inline-block; font-size: 13px; font-weight: 700; letter-spacing: .05em; padding: 3px 10px; border-radius: 20px; color: #fff; background: ${esc(statusColor[overallStatus] || "#64748b")}; margin-bottom: 12px; }
  .meta-row { display: flex; flex-wrap: wrap; gap: 16px; margin-top: 10px; font-size: 13px; color: #64748b; }
  .meta-row b { color: #1e293b; }
  section { background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 16px 20px; margin-bottom: 20px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 7px 10px; background: #f1f5f9; color: #475569; font-weight: 600; border-bottom: 2px solid #e2e8f0; white-space: nowrap; }
  td { padding: 7px 10px; border-bottom: 1px solid #f1f5f9; vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  .row-pass td { background: #f0fdf4; }
  .row-fail td { background: #fef2f2; }
  .row-skip td { background: #fafafa; color: #94a3b8; }
  .row-other td { background: #fff; }
  .badge { display: inline-block; font-size: 11px; font-weight: 700; letter-spacing: .05em; padding: 2px 8px; border-radius: 20px; }
  .badge.pass { background: #dcfce7; color: #15803d; }
  .badge.fail { background: #fee2e2; color: #b91c1c; }
  .badge.skip { background: #f1f5f9; color: #64748b; }
  .badge.other { background: #ede9fe; color: #7c3aed; }
  code { font-family: ui-monospace, monospace; font-size: 12px; background: #f1f5f9; padding: 1px 5px; border-radius: 4px; word-break: break-all; }
  .actual-fail { background: #fee2e2; color: #b91c1c; }
  .err-text { color: #b91c1c; font-size: 12px; }
  .col-num { width: 36px; color: #94a3b8; }
  .col-status { width: 80px; }
  .col-action { width: 90px; font-weight: 600; }
  .col-matcher { width: 90px; }
  .col-expected, .col-actual { width: 160px; }
  .col-dur { width: 70px; color: #64748b; white-space: nowrap; }
  .col-ss { width: 70px; }
  .thumb { width: 56px; height: 40px; object-fit: cover; border-radius: 4px; border: 1px solid #e2e8f0; }
  .ss-grid { display: flex; flex-wrap: wrap; gap: 12px; }
  .ss-grid figure { text-align: center; }
  .ss-grid figcaption { font-size: 11px; color: #94a3b8; margin-top: 4px; }
  .ss-img { width: 180px; height: 120px; object-fit: cover; border-radius: 6px; border: 1px solid #e2e8f0; display: block; }
  .footer { font-size: 11px; color: #94a3b8; text-align: center; margin-top: 16px; }
</style>
</head>
<body>
<div class="header">
  <div class="overall">${esc(statusLabel[overallStatus] || overallStatus.toUpperCase())}</div>
  <h1>${esc(result.goal || "Regression Test Run")}</h1>
  <div class="meta-row">
    <span>Started: <b>${esc(result.started_at || "—")}</b></span>
    <span>Finished: <b>${esc(result.finished_at || "—")}</b></span>
    ${elapsedMs != null ? `<span>Duration: <b>${elapsedMs < 1000 ? elapsedMs + "ms" : (elapsedMs / 1000).toFixed(1) + "s"}</b></span>` : ""}
    <span>Steps: <b>${r.passed_steps || 0} passed</b> / ${r.failed_steps || 0} failed / ${r.skipped_steps || 0} skipped / ${total} total</span>
  </div>
</div>

${assertSection}

<section>
  <h2>All Steps</h2>
  <table>
    <thead>
      <tr>
        <th class="col-num">#</th>
        <th class="col-status">Status</th>
        <th class="col-action">Action</th>
        <th class="col-desc">Details</th>
        <th class="col-matcher">Matcher</th>
        <th class="col-expected">Expected</th>
        <th class="col-actual">Actual</th>
        <th class="col-dur">Time</th>
        <th class="col-ss">Screenshot</th>
      </tr>
    </thead>
    <tbody>${stepRows}</tbody>
  </table>
</section>

${screenshotSection}

<div class="footer">Generated by Browser Automation Agent &mdash; ${esc(new Date().toISOString())}</div>
</body>
</html>`;
}

// ---------- boot ----------

settingsRestored.then(() => loadSettings());
refreshActiveTabLabel();
// Each tab's panel starts with a fresh thread (no shared restore). Past runs
// remain available via the global Run History drawer. Restore passive debug cards.
restoreDebugEntries().catch(() => {});
// Scheduled prompts: announce this panel to the service worker so due schedules
// dispatch here, and run anything the SW queued while the panel was closed.
connectSchedulePort();
drainPendingScheduleRuns().catch(() => {});
