// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
// lib/swbus.js — one hop for "ask the service worker to do a privileged thing".
//
// lib/runner.js reaches the service worker ~18 times per run (captureVisibleTab,
// ensureContentScript, listTabs, getAssistantGroup, resizeWindow, …). From the
// side panel that is a plain chrome.runtime.sendMessage. From INSIDE the service
// worker it can't be: Chrome never delivers a runtime message back to the context
// that sent it, so background.js's own onMessage listener would never fire and
// every one of those calls would hang or resolve undefined.
//
// So the worker registers its command handler here at startup and sendSW() calls
// it directly; every other context (side panel) falls through to the real
// cross-context message. Callers see the same promise-of-response either way.
let localHandler = null;

// Called once by background.js. Anything else leaves localHandler null.
export function registerLocalHandler(fn) {
  localHandler = fn;
}

export function sendSW(msg) {
  return localHandler ? localHandler(msg) : chrome.runtime.sendMessage(msg);
}
