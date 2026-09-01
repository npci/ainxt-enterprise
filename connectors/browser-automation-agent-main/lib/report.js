// SPDX-License-Identifier: Apache-2.0
// lib/report.js — turning a finished run record into something storable.
//
// Split out of sidepanel.js so a headless run driven by the local command
// bridge lands in exactly the same run history, with the same shape, the same
// screenshot stripping and the same caps as a run the user typed into the
// panel. One definition of "what a stored run looks like", two hosts.

export const HISTORY_KEY = "runHistory";
export const HISTORY_MAX = 50;

export function computeElapsed(result) {
  try {
    const a = new Date(result.started_at).getTime();
    const b = new Date(result.finished_at).getTime();
    if (!isFinite(a) || !isFinite(b)) return null;
    return Math.max(0, b - a);
  } catch {
    return null;
  }
}

// Deep-copy a run result and strip the data-URL screenshots (steps, artifacts,
// and — for suites — each nested test result). They dwarf the ~10MB
// chrome.storage.local quota; everything textual is kept so a restored entry can
// re-render the full result card.
//
// The bridge reuses this for its wire payload too: a recordGif run can carry 60
// full-viewport PNGs, which no CLI wants by default.
export function sanitizeResultForHistory(result) {
  // Strip DURING serialization (replacer) rather than clone-then-delete: the
  // old JSON round trip serialized every base64 screenshot into the string and
  // held a second full copy in memory just to delete it again. The key-based
  // replacer also covers suite-nested test results for free.
  try {
    return JSON.parse(JSON.stringify(result, (key, value) => {
      if (key === "screenshot") return undefined;
      if (key === "screenshots") return [];
      return value;
    }));
  } catch {
    return null;
  }
}

// `source` marks who started the run: "panel" (a person typed it) or "bridge"
// (a CLI/desktop client delegated it). Kept on the history entry so an
// unattended run is always attributable after the fact.
export async function saveRunToHistory(result, messages = [], results = [], { source = "panel" } = {}) {
  const r = result.result || {};
  const elapsedMs = computeElapsed(result);
  const passed = r.passed_steps || 0;
  const failed = r.failed_steps || 0;
  const skipped = r.skipped_steps || 0;
  const entry = {
    id: "run_" + Date.now(),
    goal: (result.goal || result.summary || "Run").slice(0, 120),
    mode: result.mode || "auto",
    source,
    status: r.status || (failed > 0 ? "fail" : "pass"),
    passed,
    failed,
    total: passed + failed + skipped,
    startedAt: result.started_at || new Date().toISOString(),
    elapsedMs: elapsedMs ?? 0,
    totalTokens: result.usage?.total_tokens ?? null,
    llmCalls: result.usage?.llm_calls ?? null,
    usageMissing: result.usage?.usage_missing ?? null,
    messages: messages.slice(-20),
    // Index-aligned with message pairs (results[j] belongs to messages[2j..2j+1]);
    // a null slot means that turn's result couldn't be serialized.
    results: results.slice(-10),
  };
  const stored = await chrome.storage.local.get(HISTORY_KEY);
  const list = Array.isArray(stored[HISTORY_KEY]) ? stored[HISTORY_KEY] : [];
  list.unshift(entry);
  if (list.length > HISTORY_MAX) list.length = HISTORY_MAX;
  for (let i = 10; i < list.length; i++) {
    delete list[i].messages;
    delete list[i].results;
  }
  await chrome.storage.local.set({ [HISTORY_KEY]: list });
}
