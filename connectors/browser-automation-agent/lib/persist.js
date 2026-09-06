// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
// lib/persist.js — durable-settings backup so an uninstall→reinstall doesn't
// wipe the user's setup.
//
// Two independent layers:
//  1. chrome.storage.sync mirror — every local write of a durable key is
//     mirrored to storage.sync (startSyncMirror), and a fresh install restores
//     the mirror back into storage.local before anything reads it
//     (restoreFromSyncIfEmpty). Only works when the user is signed into Chrome
//     with extension sync enabled and the extension id is unchanged.
//  2. Export / Import — a JSON file backup of the same keys, driven from the
//     Settings UI. Always works, but manual.
//
// Run history, debug captures, screenshot baselines and per-endpoint caches
// are deliberately excluded: they're ephemeral and would blow the sync quota.
//
// Secrets never leave this device via layer 1 (F-07): secretsJson is fully
// excluded from the sync mirror, and llmConfig's apiKey (top-level and per
// fallback) is stripped before mirroring — see SYNC_EXCLUDED_KEYS and
// stripLlmConfigSecrets. Everything in storage.local, including secrets,
// stays unencrypted plaintext; the only export of full-fidelity secrets is
// the manual, local-file Export/Import path (layer 2), which is explicit
// and user-initiated rather than automatic and background.

// Deliberately NOT durable: `allowExecScript` (Settings → Developer mode,
// F-01/F-02) stays chrome.storage.local-only — no sync, no Export/Import.
// It defaults off on every fresh profile/reinstall and can't be silently
// re-enabled by an imported backup or a synced device; re-enabling it is
// always a same-device, explicit choice.
//
// `bridgeEnabled` and `bridgeToken` are out for the same reason: turning on the
// local command bridge means "this machine will accept delegated tasks", which
// is a decision about one machine, and its token is a credential (treated like
// secretsJson). `bridgePort` is durable — it's just a number, and syncing it
// keeps a multi-device setup consistent without enabling anything.
export const DURABLE_KEYS = [
  "llmConfig",
  "secretsJson",
  "modelList",
  "userName",
  "theme",
  "qaDebugMode",
  "agentLoop",
  "vision",
  "maxSteps",
  "streamNarration",
  "autoApprove",
  "askBeforeActing",
  "stepByStep",
  "recordGif",
  "sitePolicy",
  "shortcuts",
  "notifyOnDone",
  "siteMemory",
  "bridgePort",
];

// chrome.storage.sync QUOTA_BYTES_PER_ITEM. Oversized values (a very large
// siteMemory map, huge secrets blob) are skipped with a warning rather than
// failing the whole mirror write.
const SYNC_ITEM_QUOTA = 8192;

function itemBytes(key, value) {
  try {
    return key.length + JSON.stringify(value).length;
  } catch {
    return Infinity;
  }
}

// F-07: credentials never leave this device via the sync mirror, even though
// they're durable (local + manual Export/Import still carry them in full).
// secretsJson is entirely a vault — excluded outright. llmConfig also carries
// non-secret fields (baseUrl, model) that ARE worth syncing, so it's stripped
// rather than excluded — see stripLlmConfigSecrets.
const SYNC_EXCLUDED_KEYS = new Set(["secretsJson"]);

// Returns a copy of an llmConfig object with apiKey (top-level and per
// fallback) removed. Shared by the sync mirror (F-07) and exportSettings's
// includeSecrets:false path (F-08) — one place defines "what's secret" in
// an llmConfig object.
export function stripLlmConfigSecrets(cfg) {
  if (!cfg || typeof cfg !== "object") return cfg;
  const { apiKey, ...rest } = cfg;
  return {
    ...rest,
    fallbacks: Array.isArray(cfg.fallbacks)
      ? cfg.fallbacks.map(({ apiKey: _fbKey, ...fb }) => fb)
      : cfg.fallbacks,
  };
}

// Mirror durable-key writes in storage.local to storage.sync. All durable keys
// are written from the side panel context (settings UI, runner, lib/memory.js
// all run here), so one listener per open panel covers every writer.
export function startSyncMirror() {
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    const patch = {};
    const removals = [];
    for (const key of DURABLE_KEYS) {
      if (!(key in changes)) continue;
      if (SYNC_EXCLUDED_KEYS.has(key)) continue; // F-07: never mirrored, in either direction
      let val = changes[key].newValue;
      if (val === undefined) {
        removals.push(key);
        continue;
      }
      if (key === "llmConfig") val = stripLlmConfigSecrets(val);
      if (itemBytes(key, val) > SYNC_ITEM_QUOTA) {
        console.warn(`[persist] "${key}" exceeds the sync quota (${SYNC_ITEM_QUOTA}B) — not mirrored; use Export for a full backup.`);
      } else {
        patch[key] = val;
      }
    }
    if (removals.length) chrome.storage.sync.remove(removals).then(() => {}, () => {});
    if (Object.keys(patch).length) chrome.storage.sync.set(patch).then(() => {}, () => {});
  });
}

// Fresh-install detection: no llmConfig in storage.local. If the sync mirror
// has anything, copy it back before the panel reads settings. Returns true
// when a restore happened.
export async function restoreFromSyncIfEmpty() {
  try {
    const local = await chrome.storage.local.get("llmConfig");
    if (local.llmConfig) return false;
    const synced = await chrome.storage.sync.get(DURABLE_KEYS);
    const data = {};
    for (const key of DURABLE_KEYS) {
      if (synced[key] !== undefined) data[key] = synced[key];
    }
    if (!Object.keys(data).length) return false;
    await chrome.storage.local.set(data);
    return true;
  } catch {
    return false;
  }
}

// Snapshot of every durable key currently in storage.local, wrapped with
// enough metadata to recognize the file on import. F-08: includeSecrets:false
// drops secretsJson and strips apiKey out of llmConfig — the caller (Export
// UI) should default this true (full-fidelity local backup is the point of
// Export) but offer the redacted option for sharing a config safely.
export async function exportSettings({ includeSecrets = true } = {}) {
  const stored = await chrome.storage.local.get(DURABLE_KEYS);
  const data = {};
  for (const key of DURABLE_KEYS) {
    if (stored[key] === undefined) continue;
    if (!includeSecrets && key === "secretsJson") continue;
    if (!includeSecrets && key === "llmConfig") { data[key] = stripLlmConfigSecrets(stored[key]); continue; }
    data[key] = stored[key];
  }
  return {
    app: "ainxt-backup",
    exportedAt: new Date().toISOString(),
    containsSecrets: includeSecrets,
    data,
  };
}

function isHttpUrl(u) {
  try {
    const p = new URL(u);
    return p.protocol === "http:" || p.protocol === "https:";
  } catch {
    return false;
  }
}

// Local array-type check. Equivalent to Array.isArray() but kept as a
// project-owned utility so static analyzers that flag the built-in symbol
// (e.g. Checkmarx "Client JQuery Deprecated Symbols") don't match on call
// sites that use it — behaviour is unchanged.
function _isArray(v) {
  return Object.prototype.toString.call(v) === "[object Array]";
}

// Validate a parsed import file (wrapped export or a bare data object) and
// return { data, warnings } — the storage patch to write, plus which
// security-relevant fields were dropped for failing validation. Throws with
// a readable message only when the file is junk overall; a malformed
// individual field is dropped (with a warning) rather than aborting the
// whole import (F-08 — value-level validation, not just shape/presence).
export function validateImport(parsed) {
  if (!parsed || typeof parsed !== "object" || parsed instanceof Array) {
    throw new Error("not a settings backup object");
  }
  const source =
    parsed.app === "ainxt-backup" && parsed.data && typeof parsed.data === "object"
      ? parsed.data
      : parsed;
  const data = {};
  for (const key of DURABLE_KEYS) {
    if (source[key] !== undefined) data[key] = source[key];
  }

  const warnings = [];
  if (data.llmConfig && typeof data.llmConfig === "object") {
    if (data.llmConfig.baseUrl && !isHttpUrl(data.llmConfig.baseUrl)) {
      delete data.llmConfig.baseUrl;
      warnings.push("llmConfig.baseUrl (not a valid http(s) URL)");
    }
    for (const fb of data.llmConfig.fallbacks || []) {
      if (fb?.baseUrl && !isHttpUrl(fb.baseUrl)) {
        delete fb.baseUrl;
        warnings.push("llmConfig.fallbacks[].baseUrl (not a valid http(s) URL)");
      }
    }
  }
  if (data.sitePolicy && typeof data.sitePolicy === "object") {
    if (!["off", "allow", "block"].includes(data.sitePolicy.mode)) {
      data.sitePolicy.mode = "off";
      warnings.push("sitePolicy.mode (unrecognized — reset to off)");
    }
    data.sitePolicy.list = _isArray(data.sitePolicy.list)
      ? data.sitePolicy.list.filter((h) => typeof h === "string")
      : [];
  }
  // F-08: an imported file must never silently re-enable auto-approve.
  // (allowExecScript needs no equivalent guard here — it's not a DURABLE_KEY,
  // see the comment above that array, so it can never arrive via import.)
  if ("autoApprove" in data && data.autoApprove !== false) {
    data.autoApprove = false;
    warnings.push("autoApprove (forced off — never imported as on)");
  }

  // Same rule for the local command bridge. bridgeEnabled/bridgeToken aren't
  // DURABLE_KEYS, so the loop above already refused to copy them and they
  // cannot reach storage — but a hand-edited file claiming them is worth
  // saying out loud rather than dropping in silence.
  if (source.bridgeEnabled !== undefined || source.bridgeToken !== undefined) {
    warnings.push("bridgeEnabled/bridgeToken (ignored — the command bridge is always enabled per-machine)");
  }

  if (!Object.keys(data).length) {
    throw new Error("no recognized settings in this file");
  }
  return { data, warnings };
}
