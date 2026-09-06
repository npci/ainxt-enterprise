// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
// lib/memory.js — per-origin learned memory in chrome.storage.local.
//
// One storage key ("siteMemory") holds a map keyed by page origin:
//   { [origin]: { enabled, notes, heals: { [fromKey]: { to, hits, lastUsed, createdAt } }, updatedAt } }
//
// "Heals" are selector fixes learned at runtime: when the LLM repairs a broken
// selector on a remembered site, the fix is stored so the next run tries it as
// an extra ladder rung BEFORE paying for another LLM call. Recording is opt-in
// per origin ("Remember fixes for this site" in Settings); lookups always work
// for whatever has been stored.
//
// Quota discipline: heals are tiny strings, but the map is LRU-capped anyway
// (MAX_ORIGINS × MAX_HEALS_PER_ORIGIN) so it can never crowd the 10 MB
// storage.local budget shared with run history and screenshot baselines.
// Writes re-read storage first; concurrent runs can still race (last writer
// wins for that origin) — acceptable for v1.

const KEY = "siteMemory";
const MAX_ORIGINS = 50;
const MAX_HEALS_PER_ORIGIN = 40;
const MAX_NOTES_CHARS = 600;
const MAX_HINT_CHARS = 400;

// Origin of a page URL, or null for non-http(s) schemes (about:, chrome:).
export function originOf(url) {
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:" ? u.origin : null;
  } catch {
    return null;
  }
}

// The memory key for a step target: a plain selector is itself; a ladder is
// keyed by its first rung (the author's original intent).
function normKey(target) {
  const t = Array.isArray(target) ? target[0] : target;
  return typeof t === "string" && t ? t : null;
}

// Selectors worth persisting across runs. ref=N is snapshot-scoped and xpath
// tends to break on any DOM change — neither is a durable fix.
function isDurable(sel) {
  return typeof sel === "string" && !!sel && !sel.startsWith("ref=") && !sel.startsWith("xpath=") && !sel.startsWith("click_at(");
}

async function readAll() {
  const stored = await chrome.storage.local.get(KEY);
  return stored[KEY] || {};
}

async function writeAll(mem) {
  await chrome.storage.local.set({ [KEY]: mem });
}

function emptySite() {
  return { enabled: false, notes: "", heals: {}, updatedAt: Date.now() };
}

// Evict least-recently-used entries beyond the caps. Mutates and returns mem.
function enforceCaps(mem) {
  for (const origin of Object.keys(mem)) {
    const heals = mem[origin].heals || {};
    const keys = Object.keys(heals);
    if (keys.length > MAX_HEALS_PER_ORIGIN) {
      keys
        .sort((a, b) => (heals[a].lastUsed || 0) - (heals[b].lastUsed || 0))
        .slice(0, keys.length - MAX_HEALS_PER_ORIGIN)
        .forEach((k) => delete heals[k]);
    }
  }
  const origins = Object.keys(mem);
  if (origins.length > MAX_ORIGINS) {
    origins
      .sort((a, b) => (mem[a].updatedAt || 0) - (mem[b].updatedAt || 0))
      .slice(0, origins.length - MAX_ORIGINS)
      .forEach((o) => delete mem[o]);
  }
  return mem;
}

export async function getSiteMemory(origin) {
  if (!origin) return null;
  const mem = await readAll();
  return mem[origin] || null;
}

export async function setEnabled(origin, enabled) {
  if (!origin) return;
  const mem = await readAll();
  mem[origin] = mem[origin] || emptySite();
  mem[origin].enabled = !!enabled;
  mem[origin].updatedAt = Date.now();
  await writeAll(enforceCaps(mem));
}

export async function setNotes(origin, notes) {
  if (!origin) return;
  const mem = await readAll();
  mem[origin] = mem[origin] || emptySite();
  mem[origin].notes = String(notes || "").slice(0, MAX_NOTES_CHARS);
  mem[origin].updatedAt = Date.now();
  await writeAll(enforceCaps(mem));
}

// Store (or refresh) a learned fix. No-op unless the origin has memory enabled
// or the fix already exists (re-recording an existing fix bumps its hit count
// even if the user later toggled recording off — it's already public knowledge
// to this profile). Skips non-durable selectors.
export async function recordHeal(origin, fromTarget, toSelector) {
  if (!origin) return;
  const from = normKey(fromTarget);
  if (!from || !isDurable(toSelector) || from === toSelector) return;
  const mem = await readAll();
  const site = mem[origin];
  const existing = site?.heals?.[from];
  if (!site?.enabled && !existing) return;
  mem[origin] = site || emptySite();
  const now = Date.now();
  mem[origin].heals[from] =
    existing && existing.to === toSelector
      ? { ...existing, hits: (existing.hits || 0) + 1, lastUsed: now }
      : { to: toSelector, hits: 1, lastUsed: now, createdAt: existing?.createdAt || now };
  mem[origin].updatedAt = now;
  await writeAll(enforceCaps(mem));
}

// A remembered fix for this target on this origin, or null.
export async function lookupHeal(origin, target) {
  if (!origin) return null;
  const from = normKey(target);
  if (!from) return null;
  const site = await getSiteMemory(origin);
  const heal = site?.heals?.[from];
  return heal ? { from, to: heal.to } : null;
}

export async function listOrigins() {
  const mem = await readAll();
  return Object.keys(mem)
    .map((origin) => ({
      origin,
      enabled: !!mem[origin].enabled,
      notes: mem[origin].notes || "",
      healCount: Object.keys(mem[origin].heals || {}).length,
      updatedAt: mem[origin].updatedAt || 0,
    }))
    .sort((a, b) => b.updatedAt - a.updatedAt);
}

export async function clearOrigin(origin) {
  if (!origin) return;
  const mem = await readAll();
  delete mem[origin];
  await writeAll(mem);
}

// Compact prompt block for this origin: user notes + the most-used learned
// selector fixes as "old → new" lines. Capped so it never bloats the prompt.
export async function memoryHintFor(origin) {
  const site = origin ? await getSiteMemory(origin) : null;
  if (!site) return "";
  const parts = [];
  if (site.notes) parts.push(site.notes);
  const heals = Object.entries(site.heals || {})
    .sort((a, b) => (b[1].hits || 0) - (a[1].hits || 0))
    .slice(0, 5)
    .map(([from, h]) => `selector ${from} is broken here — use ${h.to}`);
  parts.push(...heals);
  return parts.join("\n").slice(0, MAX_HINT_CHARS);
}
