// SPDX-License-Identifier: MIT
"use strict";
/**
 * Desktop-managed conversation history for Buddy/Code.
 *
 * The ported CLI's HEADLESS session persistence is unreliable (turns=0, not
 * indexed), so we don't depend on ~/.ainxt/sessions. The desktop has the full
 * conversation (it receives every event + the final answer), so it persists
 * them itself — reliably, across reloads and restarts — organized by PROJECT
 * (= the working folder).
 *
 * Store: ~/.ainxt/buddy-history.json
 *   { version, projects: { "<folderPath>": {
 *       path, name, updatedAt,
 *       conversations: { "<convId>": { id, title, createdAt, updatedAt, messages } }
 *   } } }
 *
 * `messages` is the renderer's conversation array (role + blocks), serialized
 * verbatim so a saved conversation re-renders exactly.
 */
const path = require("path");
const fs = require("fs");
const os = require("os");

function _file() {
  return path.join(os.homedir(), ".ainxt", "buddy-history.json");
}

// One-time migration from the pre-rename filename: if a user's history still
// only exists under the old name, copy it to the new one before the first
// read so upgrading doesn't orphan their existing conversations. Copy (not
// rename) so a downgrade or a crash mid-copy can't lose the original.
function _migrateLegacyFile() {
  const target = _file();
  if (fs.existsSync(target)) return;
  const legacy = path.join(os.homedir(), ".ainxt", "cowork-history.json");
  try {
    if (fs.existsSync(legacy)) {
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.copyFileSync(legacy, target);
    }
  } catch (e) {
    // best-effort; a failed migration just starts the user with empty history
    console.error("[buddy-history] legacy migration failed:", e.message);
  }
}

function _load() {
  try {
    _migrateLegacyFile();
    return JSON.parse(fs.readFileSync(_file(), "utf-8")) || { version: 1, projects: {} };
  } catch {
    return { version: 1, projects: {} };
  }
}

function _save(store) {
  try {
    fs.mkdirSync(path.dirname(_file()), { recursive: true });
    fs.writeFileSync(_file(), JSON.stringify(store), "utf-8");
  } catch (e) {
    // best-effort; never throw into the IPC caller
    console.error("[buddy-history] save failed:", e.message);
  }
}

function _project(store, projectPath) {
  if (!store.projects[projectPath]) {
    store.projects[projectPath] = {
      path: projectPath,
      name: path.basename(projectPath) || projectPath,
      updatedAt: 0,
      conversations: {},
    };
  }
  return store.projects[projectPath];
}

/** Register a project (folder) so it shows in the list even before any chat. */
function touchProject(projectPath) {
  if (!projectPath) return;
  const store = _load();
  const p = _project(store, projectPath);
  p.updatedAt = Math.max(p.updatedAt || 0, Date.now());
  _save(store);
}

function listProjects() {
  const store = _load();
  return Object.values(store.projects)
    .map((p) => ({
      path: p.path,
      name: p.name,
      updatedAt: p.updatedAt || 0,
      convCount: Object.keys(p.conversations || {}).length,
    }))
    .sort((a, b) => b.updatedAt - a.updatedAt);
}

function listConversations(projectPath) {
  const store = _load();
  const p = store.projects[projectPath];
  if (!p) return [];
  return Object.values(p.conversations || {})
    .map((c) => ({ id: c.id, title: c.title || "Untitled", updatedAt: c.updatedAt || 0 }))
    .sort((a, b) => b.updatedAt - a.updatedAt);
}

function getConversation(projectPath, convId) {
  const store = _load();
  const c = store.projects[projectPath]?.conversations?.[convId];
  return c ? { id: c.id, title: c.title, messages: c.messages || [] } : null;
}

/** Upsert a conversation under a project. conv = { id, title, messages }. */
function saveConversation(projectPath, conv) {
  if (!projectPath || !conv || !conv.id) return false;
  const store = _load();
  const p = _project(store, projectPath);
  const now = Date.now();
  const existing = p.conversations[conv.id];
  p.conversations[conv.id] = {
    id: conv.id,
    title: conv.title || existing?.title || "Untitled",
    createdAt: existing?.createdAt || now,
    updatedAt: now,
    messages: Array.isArray(conv.messages) ? conv.messages : [],
  };
  p.updatedAt = now;
  _save(store);
  return true;
}

function deleteConversation(projectPath, convId) {
  const store = _load();
  const p = store.projects[projectPath];
  if (p && p.conversations[convId]) { delete p.conversations[convId]; _save(store); }
  return true;
}

module.exports = {
  touchProject, listProjects, listConversations, getConversation, saveConversation, deleteConversation,
};
