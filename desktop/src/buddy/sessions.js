// SPDX-License-Identifier: MIT
"use strict";
/**
 * Reads the FULL agent's persisted sessions for the history sidebar + resume.
 *
 * Layout (written by app/main.tsx session storage):
 *   ~/.ainxt/sessions/index.json   → { version, sessions: [{id, cwd, updatedAt,
 *                                       name, model, turnCount, source}] }
 *   ~/.ainxt/sessions/{id}.json     → { id, cwd, turns: [{role, content, ...}] }
 *
 * listSessions(cwd) returns the conversations for a given working folder (so the
 * Code/Buddy sidebar shows what's relevant to the repo you've opened), newest
 * first. readHistory(id) returns [{role, content}] to render the past transcript.
 */
const path = require("path");
const fs = require("fs");
const os = require("os");

function sessionsDir() {
  return path.join(os.homedir(), ".ainxt", "sessions");
}

function _readIndex() {
  try {
    const raw = fs.readFileSync(path.join(sessionsDir(), "index.json"), "utf-8");
    const idx = JSON.parse(raw);
    return Array.isArray(idx.sessions) ? idx.sessions : [];
  } catch {
    return [];
  }
}

/**
 * @param {string} [cwd] — if given, only sessions started in this folder.
 * Returns [{id, title, cwd, mtime, turnCount}] newest-first.
 */
function listSessions(cwd) {
  const norm = (p) => (p || "").replace(/\/+$/, "");
  const want = cwd ? norm(cwd) : null;
  const out = _readIndex()
    .filter((s) => s && s.id && (!want || norm(s.cwd) === want))
    .map((s) => ({
      id: s.id,
      title: (s.name && s.name.trim()) || "Untitled",
      cwd: s.cwd || "",
      turnCount: s.turnCount || 0,
      mtime: Date.parse(s.updatedAt || s.startedAt || 0) || 0,
    }))
    .sort((a, b) => b.mtime - a.mtime);
  return out;
}

/** Reconstruct [{role, content}] from a session's turns for display. */
function readHistory(id) {
  try {
    const raw = fs.readFileSync(path.join(sessionsDir(), `${id}.json`), "utf-8");
    const data = JSON.parse(raw);
    const turns = Array.isArray(data.turns) ? data.turns : [];
    return turns
      .filter((t) => t && (t.role === "user" || t.role === "assistant") && typeof t.content === "string")
      .map((t) => ({ role: t.role, content: t.content }));
  } catch {
    return [];
  }
}

module.exports = { listSessions, readHistory, sessionsDir };
