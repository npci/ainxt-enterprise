// SPDX-License-Identifier: MIT
// background.js — service worker
// Opens the side panel on action click and proxies a few APIs that
// the side panel can't call directly (screenshots, scripting injection).

import { computeNextRun } from "./lib/schedule.js";
import { registerLocalHandler } from "./lib/swbus.js";
import { initBridge, watchBridgeSettings, sendFrame } from "./lib/bridge.js";

// Per-tab side panels. The side panel is a per-WINDOW surface, so if every tab
// is enabled the panel stays visible on every tab once opened. To make it appear
// ONLY on the tab where the user opened it (Gemini-style), we:
//   1. disable the global panel, so tabs the user never opened it on show nothing;
//   2. on action click, give that one tab its own panel document
//      (sidepanel.html?tabId=<id>, enabled:true) and open it.
// Switching to a tab with no tab-specific panel falls back to the disabled global
// → the panel hides; switching back to an opened tab shows it again.
chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: false })
  .catch((err) => console.warn("sidePanel.setPanelBehavior failed:", err));

// Disable the global panel so it never leaks onto untouched tabs. Idempotent;
// options persist, but we re-assert on every service-worker start and on install.
function disableGlobalPanel() {
  chrome.sidePanel
    .setOptions({ enabled: false })
    .catch((err) => console.warn("sidePanel.setOptions(global) failed:", err));
}

disableGlobalPanel();
chrome.runtime.onInstalled.addListener(disableGlobalPanel);

// REQ-15: custom headers on navigate, via a declarativeNetRequest SESSION rule
// (cleared automatically when the browser session ends) scoped to one exact
// tab + URL. installHeaderRule/removeHeaderRule below install/remove a rule
// per navigate; this map just tracks the id so removal targets the right one.
let _headerRuleNextId = 1;
const _headerRuleByTab = new Map(); // tabId -> ruleId

// Startup housekeeping: a crash or extension reload mid-navigation could leave
// a stale session rule attached to a tabId Chrome later reuses for an
// unrelated tab. Sweep once at service-worker start and drop anything whose
// tab no longer exists.
(async () => {
  try {
    const rules = await chrome.declarativeNetRequest.getSessionRules();
    const staleIds = [];
    for (const rule of rules) {
      for (const tid of rule.condition?.tabIds || []) {
        try { await chrome.tabs.get(tid); } catch { staleIds.push(rule.id); break; }
      }
    }
    if (staleIds.length) await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: staleIds });
  } catch (e) { console.warn("header-rule startup sweep failed:", e); }
})();

chrome.action.onClicked.addListener((tab) => {
  if (typeof tab.id !== "number") return;
  // Scope a per-tab document to the clicked tab, then open it. Do NOT await
  // before open(): awaiting drops the user gesture and Chrome rejects the open.
  // Firing setOptions first queues the tab-specific path ahead of the open.
  chrome.sidePanel.setOptions({
    tabId: tab.id,
    path: `sidepanel.html?tabId=${tab.id}`,
    enabled: true,
  });
  chrome.sidePanel
    .open({ tabId: tab.id })
    .catch((e) => console.warn("Failed to open side panel:", e));
});

// Per-tab web request capture used by the side panel's debug monitor.
// Records browser-level network failures (CORS, X-Frame-Options, DNS, 4xx/5xx, etc.)
// that the in-page fetch/console wrappers can't see.
const _webRequestCapture = new Map();
const WEB_REQUEST_MAX = 50;

function _wrShouldIgnore(details) {
  if (typeof details.tabId !== "number" || details.tabId < 0) return true;
  const url = details.url || "";
  return /^(chrome|chrome-extension|devtools|edge|about):/i.test(url);
}

function _wrPush(tabId, entry) {
  const slot = _webRequestCapture.get(tabId);
  if (!slot || !slot.capturing) return;
  if (slot.errors.length >= WEB_REQUEST_MAX) slot.errors.shift();
  slot.errors.push(entry);
}

chrome.webRequest.onErrorOccurred.addListener(
  (details) => {
    if (_wrShouldIgnore(details)) return;
    _wrPush(details.tabId, {
      url: details.url,
      error: details.error || "net::ERR_FAILED",
      statusCode: null,
      type: details.type,
      method: details.method,
      timeStamp: details.timeStamp,
    });
  },
  { urls: ["<all_urls>"] }
);

chrome.webRequest.onCompleted.addListener(
  (details) => {
    if (_wrShouldIgnore(details)) return;
    if (typeof details.statusCode !== "number" || details.statusCode < 400) return;
    _wrPush(details.tabId, {
      url: details.url,
      error: null,
      statusCode: details.statusCode,
      type: details.type,
      method: details.method,
      timeStamp: details.timeStamp,
    });
  },
  { urls: ["<all_urls>"] }
);

chrome.tabs.onRemoved.addListener((tabId) => {
  _webRequestCapture.delete(tabId);
  const headerRuleId = _headerRuleByTab.get(tabId);
  if (headerRuleId) {
    chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: [headerRuleId] }).catch(() => {});
    _headerRuleByTab.delete(tabId);
  }
  for (let i = _spawnedTabs.length - 1; i >= 0; i--) {
    if (_spawnedTabs[i].tabId === tabId || _spawnedTabs[i].openerTabId === tabId) _spawnedTabs.splice(i, 1);
  }
});

// Spawned-tab tracker: which tabs did a page open, and from where? Feeds the
// runner's auto-follow after click-like steps (pull model: takeSpawnedTabs
// returns-and-clears matches for an opener). In-memory only — the buffer just
// has to survive the sub-second gap between the click and the runner's
// post-step check, during which the run's message traffic keeps the SW alive;
// a SW restart merely misses one adoption.
const _spawnedTabs = []; // { tabId, openerTabId, url, ts }
const SPAWNED_TABS_MAX = 20;
const SPAWNED_TABS_TTL_MS = 30_000;

function _pushSpawnedTab(entry) {
  const now = Date.now();
  while (
    _spawnedTabs.length &&
    (now - _spawnedTabs[0].ts > SPAWNED_TABS_TTL_MS || _spawnedTabs.length >= SPAWNED_TABS_MAX)
  ) _spawnedTabs.shift();
  // Both listeners fire for most spawns — dedupe by tabId, keep the best URL.
  const existing = _spawnedTabs.find((e) => e.tabId === entry.tabId);
  if (existing) {
    if (entry.url && !existing.url) existing.url = entry.url;
    return;
  }
  _spawnedTabs.push(entry);
}

// Covers target=_blank and window.open(url).
chrome.webNavigation.onCreatedNavigationTarget.addListener((d) => {
  if (typeof d.sourceTabId !== "number" || d.sourceTabId < 0) return;
  _pushSpawnedTab({ tabId: d.tabId, openerTabId: d.sourceTabId, url: d.url || "", ts: Date.now() });
});
// Catches window.open('')/about:blank popups that onCreatedNavigationTarget misses.
chrome.tabs.onCreated.addListener((tab) => {
  if (typeof tab.openerTabId !== "number") return;
  _pushSpawnedTab({ tabId: tab.id, openerTabId: tab.openerTabId, url: tab.pendingUrl || tab.url || "", ts: Date.now() });
});

// Assistant tab group: the user-curated visibility scope. A run whose tab sits
// inside a tab group with this title is scoped to that group — the agent can
// only see/act on grouped tabs. Membership is always queried live from the
// browser (never cached) so the tab bar stays the single source of truth.
const ASSISTANT_GROUP_TITLE = "assistant"; // compared case-insensitively
const isAssistantGroupTitle = (title) =>
  String(title || "").trim().toLowerCase() === ASSISTANT_GROUP_TITLE;

// Notifications. The side panel asks the background (which owns the
// chrome.notifications API) to alert the user about a backgrounded run.
// We remember the originating window so a click can re-focus it.
const _notificationWindows = new Map();

async function _showNotification({ title, message, windowId }) {
  const id = await new Promise((resolve, reject) => {
    chrome.notifications.create(
      {
        type: "basic",
        iconUrl: "icons/ainxt_logo_128X128.png",
        title,
        message,
        priority: 1,
      },
      (createdId) => {
        if (chrome.runtime.lastError || !createdId) {
          const err = chrome.runtime.lastError?.message || "no notification id returned";
          console.warn("notifications.create failed:", err);
          reject(new Error(err));
          return;
        }
        resolve(createdId);
      },
    );
  });
  if (typeof windowId === "number") _notificationWindows.set(id, windowId);
  return id;
}

chrome.notifications.onClicked.addListener(async (id) => {
  // A "scheduled prompt ready" notification: open the side panel so its
  // load-time drain runs the queued prompt (the click is the user gesture that
  // chrome.sidePanel.open requires).
  if (_scheduleNotifications.has(id)) {
    _scheduleNotifications.delete(id);
    chrome.notifications.clear(id);
    try {
      const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      if (tab && typeof tab.id === "number") {
        chrome.sidePanel.setOptions({ tabId: tab.id, path: `sidepanel.html?tabId=${tab.id}`, enabled: true });
        await chrome.sidePanel.open({ tabId: tab.id });
      }
    } catch (e) {
      // Gesture may not carry through the async query on every Chrome build.
      // The queued run still executes when the user opens the panel manually.
      console.warn("Failed to open panel from schedule notification:", e);
    }
    return;
  }
  const windowId = _notificationWindows.get(id);
  _notificationWindows.delete(id);
  chrome.notifications.clear(id);
  if (typeof windowId === "number") {
    try {
      await chrome.windows.update(windowId, { focused: true, drawAttention: true });
    } catch (e) {
      console.warn("Failed to focus window from notification:", e);
    }
  }
});

chrome.notifications.onClosed.addListener((id) => {
  _notificationWindows.delete(id);
  _scheduleNotifications.delete(id);
});

// ---------- scheduled prompts ----------
// A saved prompt (in the `shortcuts` store) may carry a schedule. The SW keeps a
// chrome.alarms alarm per enabled schedule, reconciled from storage (never from
// cached in-memory state — the SW can suspend). On fire it dispatches to an open
// panel, or notifies the user to click and run it.
const SHORTCUTS_KEY = "shortcuts";
const PENDING_SCHEDULE_KEY = "pendingScheduleRuns";
const SCHED_ALARM_PREFIX = "sched:";

// Live panel connections. A due schedule prefers a panel over a notification.
const _panelPorts = new Set();
// Notification ids we raised for a due schedule (so a click opens the panel).
const _scheduleNotifications = new Set();

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "panel") return;
  // Extension-internal only. Ports from a web page would require
  // externally_connectable, which this extension deliberately does not declare
  // (F-13) — but check the sender anyway so that stays true by construction.
  if (port.sender?.id !== chrome.runtime.id) { try { port.disconnect(); } catch {} return; }
  _panelPorts.add(port);
  port.onDisconnect.addListener(() => _panelPorts.delete(port));
  // The panel-assisted arm of the command bridge streams its run events back
  // up this port; relay them to the client on the far end of the socket.
  port.onMessage.addListener((msg) => {
    if (msg?.type !== "bridgeEvent") return;
    const { type, ...rest } = msg;
    sendFrame({ type: "event", ...rest });
  });
});

// Hand a delegated task to an open side panel. Returns no_panel when there
// isn't one — the client gets a clear failure instead of a silent hang.
function dispatchTaskToPanel(id, task) {
  for (const port of _panelPorts) {
    try { port.postMessage({ type: "runBridgeTask", id, task }); return true; }
    catch { _panelPorts.delete(port); }
  }
  return false;
}

const schedAlarmName = (id) => SCHED_ALARM_PREFIX + id;

async function readSchedules() {
  try {
    const stored = await chrome.storage.local.get(SHORTCUTS_KEY);
    return Array.isArray(stored[SHORTCUTS_KEY]) ? stored[SHORTCUTS_KEY] : [];
  } catch {
    return [];
  }
}

// Create/clear alarms so the live set matches the enabled, non-completed
// schedules currently in storage. Idempotent and safe to call on every change.
async function reconcileScheduleAlarms() {
  const list = await readSchedules();
  const wanted = new Map(); // alarm name -> fire instant (ms)
  for (const rec of list) {
    const sch = rec?.schedule;
    if (sch && sch.enabled && !sch.completed && typeof sch.nextRun === "number") {
      wanted.set(schedAlarmName(rec.id), sch.nextRun);
    }
  }
  let existing = [];
  try { existing = await chrome.alarms.getAll(); } catch { return; }
  for (const al of existing) {
    if (al.name.startsWith(SCHED_ALARM_PREFIX) && !wanted.has(al.name)) {
      chrome.alarms.clear(al.name);
    }
  }
  for (const [name, when] of wanted) {
    const cur = existing.find((a) => a.name === name);
    if (!cur || cur.scheduledTime !== when) {
      // A past `when` fires almost immediately; nudge it just ahead of now.
      chrome.alarms.create(name, { when: Math.max(when, Date.now() + 1000) });
    }
  }
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes[SHORTCUTS_KEY]) reconcileScheduleAlarms();
});
chrome.runtime.onStartup?.addListener(reconcileScheduleAlarms);
chrome.runtime.onInstalled.addListener(reconcileScheduleAlarms);
reconcileScheduleAlarms();

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (!alarm.name.startsWith(SCHED_ALARM_PREFIX)) return;
  const id = Number(alarm.name.slice(SCHED_ALARM_PREFIX.length));
  const list = await readSchedules();
  const rec = list.find((s) => s.id === id);
  if (!rec || !rec.schedule || !rec.schedule.enabled || rec.schedule.completed) return;

  // Prefer a live panel — it has everything runAgent needs.
  for (const port of _panelPorts) {
    try { port.postMessage({ type: "runSchedule", id }); return; }
    catch { _panelPorts.delete(port); }
  }

  // No panel open: queue the run and advance the schedule so a later SW restart
  // won't re-fire this same occurrence. The queued run executes when a panel
  // next opens (via the notification click or the toolbar icon).
  if (rec.schedule.frequency === "once") {
    rec.schedule.nextRun = null; // the pending run still executes it
  } else {
    rec.schedule.nextRun = computeNextRun(rec.schedule, Date.now() + 1000);
  }
  const nextList = list.map((s) => (s.id === id ? rec : s));
  await chrome.storage.local.set({ [SHORTCUTS_KEY]: nextList });

  const pend = await chrome.storage.local.get(PENDING_SCHEDULE_KEY);
  const ids = Array.isArray(pend[PENDING_SCHEDULE_KEY]) ? pend[PENDING_SCHEDULE_KEY] : [];
  if (!ids.includes(id)) ids.push(id);
  await chrome.storage.local.set({ [PENDING_SCHEDULE_KEY]: ids });

  try {
    const nid = await _showNotification({
      title: "Scheduled prompt ready",
      message: `“/${rec.title}” is due — click to open and run it.`,
    });
    _scheduleNotifications.add(nid);
  } catch (e) {
    console.warn("schedule notification failed:", e);
  }
});

// Shared by captureVisibleTab/captureZoom: capture the run's OWN tab, and only
// when it's actually visible — a backgrounded run must get a clean
// "not_visible" miss, never another tab's screenshot. format "jpeg" is the
// LLM-bound variant (screenshots shipped to a vision model as base64, where a
// full-resolution retina PNG is a multi-MB payload every turn); PNG stays the
// default so zoom crops, upload artifacts, and assert_screenshot pixel diffs
// are unaffected.
const JPEG_QUALITY = 65;
const MAX_LLM_CAPTURE_WIDTH = 1440; // device px; retina captures downscale to this

async function _captureVisibleTabDataUrl(tabId, { format = "png" } = {}) {
  let windowId;
  if (typeof tabId === "number") {
    let tab;
    try {
      tab = await chrome.tabs.get(tabId);
    } catch {
      return { ok: false, reason: "no_tab" };
    }
    if (!tab.active) return { ok: false, reason: "not_visible" };
    windowId = tab.windowId;
  }
  const opts = format === "jpeg" ? { format: "jpeg", quality: JPEG_QUALITY } : { format: "png" };
  let dataUrl = await chrome.tabs.captureVisibleTab(windowId, opts);
  if (format === "jpeg") dataUrl = await _downscaleJpeg(dataUrl, MAX_LLM_CAPTURE_WIDTH);
  return { ok: true, dataUrl };
}

// Downscale an LLM-bound capture when it exceeds maxWidth device pixels
// (retina viewports capture at 2× CSS size). Returns the input unchanged when
// it's already small enough or if decode/re-encode fails — a full-size
// screenshot is always better than none.
async function _downscaleJpeg(dataUrl, maxWidth) {
  try {
    const blob = await (await fetch(dataUrl)).blob();
    const bitmap = await createImageBitmap(blob);
    if (bitmap.width <= maxWidth) return dataUrl;
    const scale = maxWidth / bitmap.width;
    const canvas = new OffscreenCanvas(Math.round(bitmap.width * scale), Math.round(bitmap.height * scale));
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const outBlob = await canvas.convertToBlob({ type: "image/jpeg", quality: JPEG_QUALITY / 100 });
    return await _blobToDataUrl(outBlob);
  } catch {
    return dataUrl;
  }
}

// Blob → data: URL without manual base64 chunking (FileReader is available in
// MV3 module service workers).
function _blobToDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error || new Error("FileReader failed"));
    reader.readAsDataURL(blob);
  });
}

// Message router. The side panel calls into the background for things that
// require service-worker-only APIs.
//
// F-13: defense-in-depth — every branch below is privileged (executeScript,
// tab navigation/removal, header injection, capture draining). No
// externally_connectable is declared today so this isn't reachable from a
// remote site yet, but this guard means it stays that way if one ever is.
// The local command bridge does NOT weaken this: it is an outbound socket this
// worker dials, never an inbound sender, so it can't reach this listener at all.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id) {
    sendResponse({ ok: false, error: "unauthorized sender" });
    return true;
  }
  handleCommand(msg).then(sendResponse);
  return true; // keep the port open for async sendResponse
});

// The command table itself, split out from the listener so an in-worker caller
// can invoke it directly — chrome.runtime.sendMessage never delivers to the
// context that sent it, so a headless runAgent() living in this worker would
// otherwise get no answer. See lib/swbus.js.
function handleCommand(msg) {
  let sendResponse;
  const answered = new Promise((resolve) => { sendResponse = resolve; });
  (async () => {
    try {
      if (msg?.type === "notify") {
        // Surface a Chrome notification for a backgrounded run (complete,
        // error, or an approval gate). Clicking it re-focuses the run's window.
        const id = await _showNotification({
          title: msg.title || "AiNxt",
          message: msg.message || "",
          windowId: msg.windowId,
        });
        sendResponse({ ok: true, id });
        return;
      }

      if (msg?.type === "captureVisibleTab") {
        // captureVisibleTab can only grab the active tab of a window. Capture the
        // run's OWN tab, and only when it's actually visible — a backgrounded run
        // must get a clean "not visible" miss, never another tab's screenshot.
        const capture = await _captureVisibleTabDataUrl(msg.tabId, { format: msg.format });
        sendResponse(capture);
        return;
      }

      if (msg?.type === "captureZoom") {
        // REQ-08: crop (and upscale) a small region of the viewport for close
        // inspection — an icon or dense table cell — without re-sending the
        // whole page image. Crop must happen here (only the background page
        // can call captureVisibleTab); content.js only resolved the rect+DPR.
        const capture = await _captureVisibleTabDataUrl(msg.tabId);
        if (!capture.ok) { sendResponse(capture); return; }
        try {
          const { region, dpr = 1, upscale } = msg;
          const blob = await (await fetch(capture.dataUrl)).blob();
          const bitmap = await createImageBitmap(blob);
          const sx = Math.max(0, Math.round(region.x * dpr));
          const sy = Math.max(0, Math.round(region.y * dpr));
          const sw = Math.max(1, Math.min(bitmap.width - sx, Math.round(region.width * dpr)));
          const sh = Math.max(1, Math.min(bitmap.height - sy, Math.round(region.height * dpr)));
          const scale = Math.max(1, Number(upscale) || 2);
          const dw = Math.round(sw * scale), dh = Math.round(sh * scale);
          const canvas = new OffscreenCanvas(dw, dh);
          const ctx = canvas.getContext("2d");
          ctx.drawImage(bitmap, sx, sy, sw, sh, 0, 0, dw, dh);
          const outBlob = await canvas.convertToBlob({ type: "image/png" });
          const dataUrl = await _blobToDataUrl(outBlob);
          sendResponse({ ok: true, dataUrl, width: dw, height: dh });
        } catch (e) {
          sendResponse({ ok: false, error: String(e) });
        }
        return;
      }

      if (msg?.type === "ensureContentScript") {
        // Re-inject the content script into the active tab if the page was
        // loaded before the extension was installed. With a frameId, injects
        // into that specific (possibly cross-origin) iframe instead — the
        // runner routes messages there for switch_frame support.
        const { tabId, frameId } = msg;
        try {
          await chrome.scripting.executeScript({
            target: frameId ? { tabId, frameIds: [frameId] } : { tabId },
            files: ["content.js"],
          });
          sendResponse({ ok: true });
        } catch (e) {
          sendResponse({ ok: false, error: String(e) });
        }
        return;
      }

      if (msg?.type === "navigateTab") {
        const { tabId, url } = msg;
        await chrome.tabs.update(tabId, { url });
        sendResponse({ ok: true });
        return;
      }

      if (msg?.type === "getWindowSize") {
        // Original-dimensions capture for resize_window's restore path.
        const { tabId } = msg;
        const tab = await chrome.tabs.get(tabId);
        const win = await chrome.windows.get(tab.windowId);
        sendResponse({ ok: true, windowId: tab.windowId, width: win.width, height: win.height });
        return;
      }

      if (msg?.type === "resizeWindow") {
        // Resizes the WHOLE window (chrome.windows.update sets outer bounds),
        // affecting every tab in it, not just the run's own tab.
        const { tabId, width, height } = msg;
        const tab = await chrome.tabs.get(tabId);
        await chrome.windows.update(tab.windowId, { width, height });
        sendResponse({ ok: true, windowId: tab.windowId });
        return;
      }

      if (msg?.type === "installHeaderRule") {
        const { tabId, url, headers } = msg;
        try {
          // Replace any rule already tracked for this tab rather than stacking.
          const existing = _headerRuleByTab.get(tabId);
          if (existing) {
            await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: [existing] });
            _headerRuleByTab.delete(tabId);
          }
          const ruleId = _headerRuleNextId++;
          const requestHeaders = Object.entries(headers || {}).map(([header, value]) => ({
            header, operation: "set", value: String(value),
          }));
          if (!requestHeaders.length) { sendResponse({ ok: false, error: "no headers given" }); return; }
          await chrome.declarativeNetRequest.updateSessionRules({
            addRules: [{
              id: ruleId,
              priority: 1,
              // Exact-URL anchor (| = start/end of URL) so the header can't leak
              // onto unrelated same-origin requests the page fires after load.
              condition: { urlFilter: `|${url}|`, tabIds: [tabId], resourceTypes: ["main_frame"] },
              action: { type: "modifyHeaders", requestHeaders },
            }],
          });
          _headerRuleByTab.set(tabId, ruleId);
          sendResponse({ ok: true, ruleId });
        } catch (e) {
          sendResponse({ ok: false, error: String(e) });
        }
        return;
      }

      if (msg?.type === "removeHeaderRule") {
        const { tabId } = msg;
        const ruleId = _headerRuleByTab.get(tabId);
        if (ruleId) {
          try { await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: [ruleId] }); } catch (e) { console.warn("removeHeaderRule failed:", e); }
          _headerRuleByTab.delete(tabId);
        }
        sendResponse({ ok: true });
        return;
      }

      if (msg?.type === "listTabs") {
        // Tab-context primitive for the agent's list_tabs action. With a
        // groupId, lists only that tab group's members (the Assistant-group
        // visibility scope) instead of the whole window.
        const tabs = await chrome.tabs.query(
          typeof msg.groupId === "number" ? { groupId: msg.groupId } : { currentWindow: true },
        );
        sendResponse({
          ok: true,
          tabs: tabs.map((t) => ({
            index: t.index,
            id: t.id,
            title: (t.title || "").slice(0, 120),
            url: t.url || "",
            active: !!t.active,
          })),
        });
        return;
      }

      if (msg?.type === "getAssistantGroup") {
        // Group-scope probe: is this tab inside an "Assistant" tab group? The
        // runner asks once at run start; null means the run stays unscoped.
        const tab = await chrome.tabs.get(msg.tabId);
        if (typeof tab.groupId !== "number" || tab.groupId === chrome.tabGroups.TAB_GROUP_ID_NONE) {
          sendResponse({ ok: true, group: null });
          return;
        }
        const group = await chrome.tabGroups.get(tab.groupId);
        sendResponse({
          ok: true,
          group: isAssistantGroupTitle(group.title)
            ? { id: group.id, title: group.title || "Assistant", color: group.color }
            : null,
        });
        return;
      }

      if (msg?.type === "removeTab") {
        // REQ-19: close a tab (close_tab action). The runner enforces the
        // group-scope and don't-orphan-the-run guards before calling; this just
        // performs the removal, which needs the extension tabs API.
        await chrome.tabs.remove(msg.tabId);
        sendResponse({ ok: true });
        return;
      }

      if (msg?.type === "groupTab") {
        // Keep agent-opened / page-spawned tabs inside the run's visibility
        // scope (FR11: no silent expansion into ungrouped tabs).
        await chrome.tabs.group({ tabIds: [msg.tabId], groupId: msg.groupId });
        sendResponse({ ok: true });
        return;
      }

      if (msg?.type === "findAssistantGroup") {
        // Tab-less lookup: is there an "Assistant" group anywhere? The command
        // bridge needs this before it has a tab — getAssistantGroup above is
        // the opposite question ("is THIS tab in one?") and needs a tabId.
        const groups = await chrome.tabGroups.query({});
        const found = groups.find((g) => isAssistantGroupTitle(g.title));
        sendResponse({
          ok: true,
          group: found ? { id: found.id, title: found.title || "Assistant", color: found.color } : null,
        });
        return;
      }

      if (msg?.type === "ensureAssistantGroup") {
        // Find-or-create the window's "Assistant" group and put the tab in it.
        // Reuses an existing group (FR5) so repeated sessions never spawn
        // duplicate groups in the tab bar.
        const tab = await chrome.tabs.get(msg.tabId);
        const groups = await chrome.tabGroups.query({ windowId: tab.windowId });
        const existing = groups.find((g) => isAssistantGroupTitle(g.title));
        let groupId;
        if (existing) {
          groupId = existing.id;
          if (tab.groupId !== groupId) await chrome.tabs.group({ tabIds: [tab.id], groupId });
        } else {
          groupId = await chrome.tabs.group({ tabIds: [tab.id] });
          await chrome.tabGroups.update(groupId, { title: "Assistant", color: "blue" });
        }
        const tabs = await chrome.tabs.query({ groupId });
        sendResponse({ ok: true, group: { id: groupId, title: "Assistant" }, tabCount: tabs.length });
        return;
      }

      if (msg?.type === "ungroupAssistantGroup") {
        // Session-end cleanup (FR7): dissolve the group — its tabs stay open,
        // they just leave the assistant's scope.
        const tabs = await chrome.tabs.query({ groupId: msg.groupId });
        if (tabs.length) await chrome.tabs.ungroup(tabs.map((t) => t.id));
        sendResponse({ ok: true, ungrouped: tabs.length });
        return;
      }

      if (msg?.type === "takeSpawnedTabs") {
        // Auto-follow primitive: hand the runner every tab this opener spawned
        // since the step started, and forget them (take-and-clear).
        const { openerTabId, sinceTs = 0 } = msg;
        const taken = [];
        const kept = [];
        for (const e of _spawnedTabs) {
          (e.openerTabId === openerTabId && e.ts >= sinceTs ? taken : kept).push(e);
        }
        _spawnedTabs.length = 0;
        _spawnedTabs.push(...kept);
        sendResponse({ ok: true, tabs: taken });
        return;
      }

      if (msg?.type === "startWebRequestCapture") {
        _webRequestCapture.set(msg.tabId, { capturing: true, errors: [] });
        sendResponse({ ok: true });
        return;
      }

      if (msg?.type === "ensureWebRequestCapture") {
        // Non-destructive arm used by the agent loop: never clobbers a buffer
        // the debug monitor (or an earlier run) is already filling.
        if (!_webRequestCapture.get(msg.tabId)?.capturing) {
          _webRequestCapture.set(msg.tabId, { capturing: true, errors: [] });
        }
        sendResponse({ ok: true });
        return;
      }

      if (msg?.type === "stopWebRequestCapture") {
        _webRequestCapture.delete(msg.tabId);
        sendResponse({ ok: true });
        return;
      }

      if (msg?.type === "getWebRequestCapture") {
        const slot = _webRequestCapture.get(msg.tabId);
        const errors = slot?.errors ?? [];
        // clear: drain-on-read for the read_network_requests action, keeping
        // the slot armed so capture continues.
        if (msg.clear && slot) slot.errors = [];
        sendResponse({ ok: true, errors });
        return;
      }

      if (msg?.type === "dispatchToPanel") {
        const taken = dispatchTaskToPanel(msg.id, msg.task);
        sendResponse(taken ? { ok: true } : { ok: false, error: "no_panel" });
        return;
      }

      sendResponse({ ok: false, error: "unknown message type" });
    } catch (err) {
      sendResponse({ ok: false, error: String(err) });
    }
  })();
  return answered;
}

// In-worker callers (the command bridge's headless runAgent) route here.
registerLocalHandler(handleCommand);

// ---------- local command bridge ----------
// Off unless the user enabled it. When on, this worker dials a loopback helper
// so a CLI or desktop app can delegate a task; it never listens for anyone.
watchBridgeSettings();
initBridge();
chrome.runtime.onStartup?.addListener(initBridge);
chrome.runtime.onInstalled.addListener(initBridge);
