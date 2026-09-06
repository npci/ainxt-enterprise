// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
// lib/parser.js — Normalize attached test files into a consistent shape.

import { parseYaml } from "./yaml.js";

export function parseTestFile(text) {
  const trimmed = (text || "").replace(/^\s+|\s+$/g, "");
  if (!trimmed) return null;

  let obj;
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    obj = JSON.parse(trimmed);
  } else {
    obj = parseYaml(trimmed);
  }

  // Suite format: has suite_name or tests array
  if (obj && (obj.suite_name || obj.tests)) {
    return normalizeSuite(obj);
  }

  return normalize(obj);
}

function normalizeSuite(obj) {
  return {
    _isSuite: true,
    suite_name: obj.suite_name || obj.name || "Unnamed Suite",
    shared_variables: obj.shared_variables || obj.variables || {},
    tests: (obj.tests || []).map(normalize),
  };
}

function normalize(obj) {
  if (obj instanceof Array) {
    obj = { steps: obj };
  }
  return {
    test_name: obj.test_name || obj.name || "untitled",
    base_url: obj.base_url || null,
    timeout_ms: obj.timeout_ms || null,
    variables: obj.variables || {},
    steps: (obj.steps || []).map(normalizeStep),
  };
}

// Back-compat: old recordings may have `target: "string"` + `selectorFallback: "string"`.
// New recordings store the full ladder in `target` directly. Fold legacy fallback in
// so the runner always sees a single source of truth.
function foldTarget(target, fallback) {
  if (target == null) return null;
  if (Array.isArray(target)) {
    return fallback && !target.includes(fallback) ? [...target, fallback] : target;
  }
  if (fallback && fallback !== target) return [target, fallback];
  return target;
}

function normalizeStep(s) {
  if (!s || typeof s !== "object") {
    throw new Error("Invalid step: " + JSON.stringify(s));
  }
  const step = {
    action: s.action,
    target: foldTarget(s.target ?? null, s.selectorFallback ?? null),
    value: s.value ?? null,
    url: s.url ?? null,
    condition: s.condition ?? null,
    matcher: s.matcher ?? null,
    expected: s.expected ?? null,
    timeout_ms: s.timeout_ms ?? null,
    critical: !!s.critical,
    attr: s.attr ?? null,
    // new fields
    variable: s.variable ?? null,
    script: s.script ?? null,
    destination: s.destination ?? null,
    inputTarget: s.inputTarget ?? null,
    isoDate: s.isoDate ?? null,
    filename: s.filename ?? null,
    mime_type: s.mime_type ?? null,
    selectorFallback: null, // deprecated — folded into `target` ladder by foldTarget()
    content: s.content ?? null,
    baseline: s.baseline ?? null,
    threshold: s.threshold ?? null,
    repeat: s.repeat != null ? Number(s.repeat) : null,
    metric: s.metric ?? null,
    max_ms: s.max_ms ?? null,
    // click_at coordinates (CSS pixels)
    x: s.x ?? null,
    y: s.y ?? null,
    method: s.method ?? null,
    response: s.response ?? null,
    status: s.status ?? null,
    // REQ-18 modifier-click / REQ-17 right-click / REQ-20 screenshot share:
    // pass these through so deterministic test files can express them.
    modifiers: s.modifiers ?? null,
    button: s.button ?? null,
    to_user: s.to_user ?? null,
    instruction: s.instruction ?? null,
    name: s.name ?? null,
    // if/else branching
    then: Array.isArray(s.then) ? s.then.map(normalizeStep) : null,
    else: Array.isArray(s.else) ? s.else.map(normalizeStep) : null,
  };
  if (!step.action) {
    throw new Error("Step missing 'action': " + JSON.stringify(s));
  }
  return step;
}
