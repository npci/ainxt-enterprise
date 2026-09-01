// SPDX-License-Identifier: Apache-2.0
// lib/bridge-run.js — turns a bridge request into an actual run.
//
// This is the headless host for runAgent: it does, in the service worker, the
// job sidepanel.js's run button does in the panel — read the settings, resolve
// a tab, pre-flight the site policy, wire the callbacks, run, report. The two
// hosts deliberately share their gate policy (isCriticalGate) and their history
// writer (lib/report.js) so an unattended run is governed and recorded exactly
// like one a person typed.
//
// What this host does NOT do is loosen anything. A bridge request cannot enable
// script execution, cannot skip a critical gate, and cannot escape the site
// policy — those all live below this layer, in the runner.

import { runAgent, isHostAllowed, isCriticalGate } from "./runner.js";
import { sanitizeResultForHistory, saveRunToHistory } from "./report.js";
import { sendSW } from "./swbus.js";

const ACTIVE_KEY = "bridge1RunActive";

// One bridge run at a time, mirroring the panel's per-tab guard: two agents
// driving the same browser is not a feature.
let active = null; // { id, controller, tabId }

export function isBridgeRunActive() {
  return !!active;
}

export function cancelBridgeRun(reason = "cancelled", id = null) {
  if (!active) return false;
  if (id && active.id !== id) return false;
  try { active.controller.abort(new Error(reason)); } catch {}
  return true;
}

async function setActiveFlag(value) {
  try { await chrome.storage.local.set({ [ACTIVE_KEY]: value }); } catch {}
}

// ---------- tab resolution ----------

// The panel always has a tab (the one it's docked to). The bridge has to pick
// one. Preference order: an explicit tabId from the request, then the Assistant
// tab group (so a delegated task lands in the user's dedicated workspace and
// inherits its scoping), then a new tab.
async function resolveTargetTab({ tabId, startUrl }) {
  if (Number.isInteger(tabId)) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) throw new Error(`tab ${tabId} no longer exists`);
    if (startUrl) await chrome.tabs.update(tab.id, { url: startUrl });
    return tab.id;
  }

  const found = await sendSW({ type: "findAssistantGroup" }).catch(() => null);
  const group = found?.ok ? found.group : null;
  if (group) {
    const listed = await sendSW({ type: "listTabs", groupId: group.id }).catch(() => null);
    const first = listed?.tabs?.[0];
    if (first?.id != null) {
      if (startUrl) await chrome.tabs.update(first.id, { url: startUrl });
      return first.id;
    }
  }

  const created = await chrome.tabs.create({ url: startUrl || "about:blank", active: false });
  // Join the Assistant group when one exists, so the run stays scoped and the
  // user's group cleanup applies to bridge tabs too.
  if (group) await sendSW({ type: "groupTab", tabId: created.id, groupId: group.id }).catch(() => {});
  return created.id;
}

// Wait for the tab to finish loading before the first snapshot. runAgent does
// its own readiness work per step; this just avoids planning against about:blank.
async function waitForTabLoad(tabId, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) throw new Error("target tab was closed");
    if (tab.status === "complete") return tab;
    await new Promise((r) => setTimeout(r, 250));
  }
  return chrome.tabs.get(tabId).catch(() => null);
}

// ---------- the run ----------

const VISION_MODES = new Set(["off", "auto", "on"]);
const MODES = new Set(["auto", "test", "ask", "debug", "exploration", "agentic", "suite"]);

export async function startBridgeRun({ id, task, emit, requestGate }) {
  if (active) {
    emit("error", { error: "busy", detail: `run ${active.id} is still in progress` });
    return;
  }

  // attach:"panel" — hand the task to an open side panel instead of running it
  // here. The panel has the DOM the headless host lacks (approval modals, the
  // rendered thread) and its tab is focused, so vision-heavy or
  // approval-heavy work belongs there. It emits its own events over the panel
  // port; this host is done as soon as the hand-off is accepted.
  if (task.attach === "panel") {
    const handed = await sendSW({ type: "dispatchToPanel", id, task }).catch(() => null);
    if (!handed?.ok) {
      emit("error", { error: "no_panel", detail: "attach:\"panel\" was requested but no side panel is open" });
    }
    return;
  }

  const controller = new AbortController();
  active = { id, controller, tabId: null };
  await setActiveFlag(true);

  const stored = await chrome.storage.local.get([
    "llmConfig", "secretsJson", "qaDebugMode", "agentLoop", "vision", "maxSteps",
    "streamNarration", "autoApprove", "allowExecScript", "stepByStep", "recordGif", "sitePolicy",
  ]);

  let secrets = {};
  try { secrets = stored.secretsJson ? JSON.parse(stored.secretsJson) : {}; } catch { secrets = {}; }

  const mode = MODES.has(task.mode) ? task.mode : "auto";
  // Tri-state, same migration the panel does for pre-Vision-Auto booleans.
  const storedVision = stored.vision === true ? "on" : String(stored.vision || "off");
  const vision = VISION_MODES.has(task.vision) ? task.vision
    : VISION_MODES.has(storedVision) ? storedVision : "off";
  const includeScreenshots = task.includeScreenshots === true;
  // "deny" is the CI default: a critical gate simply ends the run rather than
  // waiting on a human who isn't there.
  const approvals = task.approvals === "prompt" ? "prompt" : "deny";

  let tabId = null;
  try {
    if (!stored.llmConfig?.baseUrl) throw new Error("no LLM endpoint configured — set one in the side panel first");

    // Site policy pre-flight, same as the panel does before it starts a run.
    if (task.startUrl) {
      const verdict = isHostAllowed(task.startUrl, stored.sitePolicy || null);
      if (!verdict.allowed) throw new Error(`site policy: ${verdict.reason}`);
    }

    tabId = await resolveTargetTab({ tabId: task.tabId, startUrl: task.startUrl });
    active.tabId = tabId;
    await waitForTabLoad(tabId);

    // captureVisibleTab only ever captures the active tab of a window, so a
    // vision run has to be looking at its own tab (background.js returns a
    // clean "not_visible" miss otherwise).
    if (vision !== "off") await chrome.tabs.update(tabId, { active: true }).catch(() => {});

    await sendSW({ type: "ensureContentScript", tabId }).catch(() => {});

    emit("accepted", { tabId, mode, vision });

    const result = await runAgent({
      instruction: task.instruction || "",
      fileText: task.fileText || "",
      mode: mode === "debug" ? "exploration" : mode,
      tabId,
      llmConfig: stored.llmConfig,
      secrets,
      signal: controller.signal,
      qaDebugMode: !!stored.qaDebugMode || mode === "debug",
      agentLoop: task.agentLoop !== undefined ? !!task.agentLoop : stored.agentLoop === true,
      vision,
      maxSteps: Math.min(100, Math.max(5, Number(task.maxSteps) || Number(stored.maxSteps) || 20)),
      streamNarration: !!stored.streamNarration,
      sitePolicy: stored.sitePolicy || null,
      // Not overridable from the wire: Developer mode is a same-device,
      // explicit human choice (F-01/F-02) and a remote caller does not get to
      // make it. If it's off here, exec_script refuses regardless of approval.
      allowExecScript: !!stored.allowExecScript,
      recordGif: !!stored.recordGif,
      dryRun: !!task.dryRun,
      // A step-by-step pause has no meaning without someone watching the panel.
      stepByStep: false,
      _extraVars: task.variables && typeof task.variables === "object" ? task.variables : {},

      onProgress: (msg, cls) => emit("progress", { message: msg, level: cls || "info" }),
      onNarrationDelta: (text, isFinal) => emit("narration", { text, final: !!isFinal }),
      onImage: (dataUrl, caption) => emit("image", includeScreenshots ? { dataUrl, caption } : { caption, omitted: true }),
      onTabChange: (newTabId) => { if (active) active.tabId = newTabId; emit("tab", { tabId: newTabId }); },
      onHumanGate: (gateData) => forwardGate({ gateData, approvals, autoApprove: !!stored.autoApprove, requestGate }),
    });

    const record = includeScreenshots ? result : sanitizeResultForHistory(result);
    emit("done", { record });

    await saveRunToHistory(result, [], [], { source: "bridge" }).catch(() => {});
    await notifyDone(result, task);
  } catch (e) {
    const message = e?.message || String(e);
    // A cancel (client hung up, socket dropped, explicit stop) isn't a failure
    // — the CLI shouldn't report it as one.
    if (e?.name === "AbortError" || controller.signal.aborted) {
      emit("error", { error: "stopped", detail: message });
    } else {
      emit("error", { error: "run_failed", detail: message });
      await sendSW({ type: "notify", title: "Delegated run failed", message: message.slice(0, 140) }).catch(() => {});
    }
  } finally {
    active = null;
    await setActiveFlag(false);
  }
}

// A run nobody is watching still deserves to be visible to the person at the
// machine — the browser just did work on their behalf.
async function notifyDone(result, task) {
  const status = result?.result?.status || "done";
  await sendSW({
    type: "notify",
    title: `Delegated run ${status}`,
    message: String(task.instruction || result?.goal || "Run finished").slice(0, 140),
  }).catch(() => {});
}

// The gate policy for an unattended run.
//
//   - step gates          never reached (stepByStep is forced off)
//   - critical gates      ALWAYS forwarded to the operator, never auto-resolved;
//                         "deny" mode refuses them outright
//   - everything else     follows the same autoApprove setting the panel uses
//
// This is the same ordering as sidepanel.js's onHumanGate, expressed over a
// socket instead of a modal.
function forwardGate({ gateData, approvals, autoApprove, requestGate }) {
  const critical = isCriticalGate(gateData);

  if (!critical && autoApprove && gateData?.kind !== "step") return Promise.resolve("approve");
  if (approvals === "deny") return Promise.resolve("cancel");

  return requestGate({
    critical,
    reason: gateData?.reason || "Approval needed to continue.",
    // The secret's KEY and destination HOST travel; its value never does.
    secretKey: gateData?.secretKey || null,
    secretHost: gateData?.host || gateData?.secretHost || null,
    jsCondition: gateData?.jsCondition === true,
    step: gateData?.step
      ? { action: gateData.step.action, target: gateData.step.target, value: maskValue(gateData.step) }
      : null,
    formState: gateData?.formState || null,
  });
}

// The gate fires on the RAW step, so a secret is still the literal
// "${secrets.KEY}" placeholder here and stays that way on the wire. Everything
// else is truncated: the operator needs to recognise the action, not receive
// the page's full payload.
function maskValue(step) {
  const v = step?.value;
  if (typeof v !== "string" || !v) return v ?? null;
  return v.length > 80 ? `${v.slice(0, 80)}…` : v;
}
