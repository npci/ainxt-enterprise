// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
// lib/runner.js — Orchestrates a run. Hosted by the side panel, or headless by
// the service worker when a run arrives over the local command bridge; every
// service-worker call goes through sendSW so both hosts work (see lib/swbus.js).

import { parseTestFile } from "./parser.js";
import { sendSW } from "./swbus.js";
import { planSteps, planNextAction, summarizeContent, healSelector, identifyElementVisually, mapAutofillFields, analyzeRootCause, findElementsLLM, PERMISSION_TIERS } from "./llm.js";
import { originOf, lookupHeal, recordHeal, memoryHintFor } from "./memory.js";
import { scanPII, maskPII } from "./pii.js";
import { detectDocumentUrl, sniffPdfUrl, documentTabSnapshot, documentSnapshotHint, readDocument, readPdfData, isUrlInScope } from "./documents.js";

// Per-run LLM token accounting (REQUIREMENTS-LLM_OBSERVABILITY.md). One
// accumulator is built per runAgent/runIterativeAgent invocation and threaded
// as `onUsage` into every LLM call in that function; runSuite sums its nested
// runAgent results instead of tracking its own. `notes` collects user-facing
// one-liners (currently just the fallback "served by" annotation, FR11).
//
// `seed` carries tokens already spent on this run's behalf before the runner was
// entered — the plan-preview planSteps() calls in sidepanel.js happen before
// runAgent() and would otherwise never be counted. Exported so sidepanel.js can
// build the same shape instead of hand-rolling it.
//
// `usage_missing` counts calls where the endpoint reported no usage block at all,
// which is what lets the UI render "unknown" instead of a fabricated 0.
export function newUsageAccumulator(seed) {
  return {
    prompt_tokens: Number(seed?.prompt_tokens) || 0,
    completion_tokens: Number(seed?.completion_tokens) || 0,
    total_tokens: Number(seed?.total_tokens) || 0,
    llm_calls: Number(seed?.llm_calls) || 0,
    usage_missing: Number(seed?.usage_missing) || 0,
    notes: Array.isArray(seed?.notes) ? [...seed.notes] : [],
  };
}
export function accumulateUsage(acc, { usage, servedBy, isFallback } = {}) {
  acc.llm_calls++;
  if (usage) {
    acc.prompt_tokens += Number(usage.prompt_tokens) || 0;
    acc.completion_tokens += Number(usage.completion_tokens) || 0;
    acc.total_tokens += Number(usage.total_tokens) || ((Number(usage.prompt_tokens) || 0) + (Number(usage.completion_tokens) || 0));
  } else {
    acc.usage_missing++;
  }
  if (isFallback && !acc.notes.some((n) => n.includes(servedBy))) {
    acc.notes.push(`Primary endpoint failed — served by ${servedBy}`);
  }
}

// The result object's `usage` carries the counters only — `notes` rides at the
// top level of the record (that's what the panel renders), so strip it here
// rather than letting the spread duplicate it into both places.
function usageRecord(acc) {
  const { notes, ...counters } = acc;
  return counters;
}

// DOM actions where a bad selector is the most likely failure — eligible for LLM-guided retry.
const HEALABLE_ACTIONS = new Set([
  "click", "dblclick", "hover", "type", "clear", "select",
  "check", "uncheck", "press_key", "scroll", "scroll_to", "drag", "extract", "assert",
]);
const MAX_HEAL_RETRIES = 2;
const CHEAP_RETRY_DELAY_MS = 600; // wait + re-resolve once before paying for an LLM heal

// REQ-13: recordGif forces one screenshot per step (reusing record.screenshot,
// the same field failure/screenshot/zoom steps already populate) so sidepanel.js
// can encode them into a GIF after the run — capped so a long run doesn't
// capture/hold dozens of full-viewport PNGs in memory for nothing.
const MAX_GIF_FRAMES = 60;

// Assert matchers that imply the target element should exist. When one of these
// reports passed:false with an empty/absent actual, the selector — not the value —
// is the likely culprit, so the assert becomes a heal candidate. Matchers that are
// satisfied by absence (absent/hidden/disabled/count) are deliberately excluded.
const SHOULD_EXIST_MATCHERS = new Set([
  "equals", "contains", "matches", "visible", "present", "enabled",
]);

// True when an assert's failure looks like "the element wasn't found" rather than a
// genuine value mismatch — i.e. a should-exist matcher produced no/empty/false value.
function assertMissedElement(step, result) {
  if (step.action !== "assert" || result?.passed !== false) return false;
  const matcher = step.matcher || "equals";
  if (!SHOULD_EXIST_MATCHERS.has(matcher) && !matcher.startsWith("attribute:")) return false;
  const actual = result.actual;
  return actual === null || actual === undefined || actual === "" || actual === false || actual === 0;
}

const DEFAULT_STEP_TIMEOUT_MS = 15000;

// Page-settle tuning. After a navigation the runner waits for the DOM to go
// quiet (content.js waitForDomSettled) so slow client-side renders aren't
// snapshotted as an empty shell; the perceive snapshot additionally re-polls
// while the page still looks like a loading shell.
const SETTLE_QUIET_MS = 500;
const SETTLE_MAX_MS = 4000;
const SNAPSHOT_STABLE_MAX_MS = 3000;
const SNAPSHOT_POLL_MS = 400;

// Risky/irreversible actions that agentic mode pauses on for human approval.
// Derived from the shared PERMISSION_TIERS structure in lib/llm.js so the gate
// and the prompts' tier section can never disagree. (click_at is in the tier
// because a coordinate carries no text for the intent regex to inspect — a
// blind click must always be surfaced for approval.)
const RISKY_ACTIONS = new Set(PERMISSION_TIERS.explicit_permission.actions);
const RISKY_INTENT_RE = new RegExp(PERMISSION_TIERS.explicit_permission.intent_keywords.join("|"), "i");

function isJsCondition(step) {
  return typeof step.condition === "string" && step.condition.startsWith("js:");
}

function isRiskyStep(step) {
  if (RISKY_ACTIONS.has(step.action)) return true;
  // F-09: a js: condition (wait) is exactly as powerful as exec_script —
  // treat it the same as an explicit_permission-tier action for approval.
  if (isJsCondition(step)) return true;
  // LLM-rated risk (1-5, from the planner prompts): 4+ pauses for approval.
  // The keyword regex below stays as the floor — models under-report.
  if (Number(step.risk) >= 4) return true;
  if (step.action === "click" || step.action === "dblclick") {
    const haystack = [step.target, step.value].flat().filter(Boolean).join(" ");
    return RISKY_INTENT_RE.test(haystack);
  }
  return false;
}

// Gates that Auto-approve must never silently resolve, regardless of the
// setting: exec_script (arbitrary JS in the page, bypasses CSP — F-01/F-02),
// the first use of a vault secret against a new origin (F-03), and a js:
// condition on an if/wait step (F-09 — exactly as powerful as exec_script).
// All ship with autoApprove precisely because a hostile page's injected
// instructions are what would otherwise exploit the bypass.
//
// Exported because every host of a run has to honour it identically: the side
// panel's onHumanGate, and the command bridge, which forwards these — and only
// these — to the CLI operator instead of ever resolving them itself.
export function isCriticalGate(gateData) {
  return gateData?.step?.action === "exec_script" || !!gateData?.secretKey || gateData?.jsCondition === true;
}

// Gate copy for a risky step, citing the planner's own risk reasoning when present.
function riskyReason(step) {
  const desc = isJsCondition(step) ? step.condition : describeStep(step);
  const base = `Risky action: ${step.action} ${desc}`;
  return step.risk_reason ? `${base} — ${step.risk_reason}` : base;
}

// Pre-submit diff for the approval gate: what the enclosing form would submit
// if this click goes ahead. Best-effort; failures just omit the diff.
async function collectFormStateFor(tabId, step) {
  if (step.action !== "click" && step.action !== "dblclick") return null;
  const res = await sendToContent(tabId, { type: "collectFormState", target: step.target }).catch(() => null);
  return res?.ok && res.fields?.length ? res.fields : null;
}

// Host allow/block policy enforcement. policy = { mode, list } where mode is
// "off" | "allow" | "block" and list is an array of hostnames. A host matches
// an entry on exact match or as a subdomain (".example.com"). Returns
// { allowed: boolean, reason?: string }. Non-URL targets (about:, chrome:) and
// an absent/off policy are always allowed — the caller handles those separately.
export function isHostAllowed(url, policy) {
  if (!policy || !policy.mode || policy.mode === "off") return { allowed: true };
  let host;
  try {
    host = new URL(url).hostname.toLowerCase();
  } catch {
    // F-15: "allow" is the one mode where the user has expressed explicit
    // scoping intent — a target that can't even be parsed can't be checked
    // against that allowlist, so it fails closed here specifically. "block"
    // mode is permissive-unless-listed by design (matches "off"); an
    // unparsable URL there can't be *identified* as blocked either, so it
    // keeps that mode's existing permissive default.
    return policy.mode === "allow"
      ? { allowed: false, reason: "URL could not be parsed — allowlist mode fails closed" }
      : { allowed: true };
  }
  const list = (policy.list || []).map((h) => String(h).trim().toLowerCase()).filter(Boolean);
  const matches = list.some((p) => host === p || host.endsWith("." + p));
  if (policy.mode === "allow") {
    return matches ? { allowed: true } : { allowed: false, reason: `${host} is not in the allowlist` };
  }
  if (policy.mode === "block") {
    return matches ? { allowed: false, reason: `${host} is blocked` } : { allowed: true };
  }
  return { allowed: true };
}

export async function runAgent({
  instruction,
  fileText,
  mode,
  tabId,
  llmConfig,
  secrets,
  signal,
  onProgress,
  onHumanGate,        // async (gateData) => "approve" | "cancel"
  onTabChange,        // (newTabId) => void — run retargeted (switch_tab/open_tab/auto-follow)
  onImage,            // REQ-20: (dataUrl, caption) => void — surface a to_user screenshot in the thread
  qaDebugMode = false,
  agentLoop = false,  // exploration/agentic: perceive→act→re-perceive loop (vs plan-once)
  vision = false,     // "off" | "auto" | "on" (legacy booleans accepted) — see runIterativeAgent
  maxSteps = 20,      // agent-loop action budget (Settings → Max steps per run)
  streamNarration = false, // stream each action's narration token-by-token
  onNarrationDelta,   // (partialText, isFinal) => void — in-place narration sink
  priorMessages = [],
  userImage = null,   // user-attached image (data: URL) — extra context for planning
  sitePolicy = null,  // { mode: "off"|"allow"|"block", list: [hosts] } — host guard
  allowExecScript = false, // Settings → Developer mode; exec_script refuses to run unless true
  prePlannedPlan = null, // { mode, goal, steps } approved/edited in the plan modal — skips re-planning
  dryRun = false,     // resolve-only  highlight + probe each target, execute nothing
  stepByStep = false, // pause for Continue/Stop before every step
  recordGif = false,  // REQ-13: force a screenshot per step (capped at MAX_GIF_FRAMES) for GIF export
  seedUsage = null,   // tokens already spent for this run before runAgent (plan preview)
  _testFile = null,   // pre-parsed test object (used by suite runner)
  _extraVars = {},    // variables inherited from suite scope
  _tabGroup,          // group scope already resolved by the suite runner — skips the probe
}) {
  const startedAt = new Date().toISOString();
  const usageAcc = newUsageAccumulator(seedUsage);
  const onUsage = (u) => accumulateUsage(usageAcc, u);
  _frameByTab.delete(tabId); // every run starts in the top frame

  // Assistant tab group: if the run's tab sits inside a tab group titled
  // "Assistant", the run is scoped to that group — list_tabs/switch_tab/
  // read_tab only see grouped tabs, and any tab the run opens joins the group.
  // Only the group id is pinned here; membership is queried live at each use.
  const tabGroup = _tabGroup !== undefined ? _tabGroup : await resolveAssistantGroup(tabId);
  if (tabGroup && _tabGroup === undefined) {
    const res = await sendSW({ type: "listTabs", groupId: tabGroup.id }).catch(() => null);
    const names = (res?.tabs || []).map((t) => t.title || t.url).filter(Boolean);
    onProgress?.(
      `Assistant tab group active — this run sees only its ${names.length} tab(s): ${names.join(" · ").slice(0, 300)}`,
      "info",
    );
  }

  // Normalize vision to its tri-state form ("off" | "auto" | "on"); the stored
  // setting was a boolean before Vision Auto existed.
  vision = vision === true ? "on" : !vision || vision === "off" ? "off" : String(vision);

  let testFile = _testFile;
  if (!testFile && fileText && fileText.replace(/^\s+|\s+$/g, "")) {
    testFile = parseTestFile(fileText);
  }

  // A test file or an approved plan can override the settings-level budget.
  const fileMaxSteps = Number(testFile?.max_steps) || Number(prePlannedPlan?.max_steps);
  if (fileMaxSteps > 0) maxSteps = Math.min(Math.max(fileMaxSteps, 1), 100);

  // Suite mode — run multiple named tests in sequence
  if (testFile?._isSuite) {
    return runSuite({ testFile, tabId, llmConfig, secrets, signal, onProgress, onHumanGate, onTabChange, qaDebugMode, sitePolicy, allowExecScript, dryRun, stepByStep, tabGroup, seedUsage });
  }

  const effectiveMode =
    mode === "auto" ? (testFile ? "test" : "exploration")
    : mode === "debug" ? "exploration"
    : mode;

  // Agent loop: drive exploration/agentic one action at a time against fresh
  // page state, instead of planning the whole run up front. test/suite stay
  // deterministic and never take this path. An approved/edited plan or a dry
  // run also bypasses the loop — both are commitments to a specific step list.
  if (agentLoop && !testFile && !prePlannedPlan && !dryRun &&
      (effectiveMode === "exploration" || effectiveMode === "agentic")) {
    return runIterativeAgent({
      instruction, tabId, llmConfig, secrets, signal, onProgress, onHumanGate, onTabChange, onImage,
      qaDebugMode, vision, streamNarration, onNarrationDelta, effectiveMode, startedAt,
      variables: { ..._extraVars }, sitePolicy, allowExecScript, stepByStep, maxSteps, recordGif,
      priorMessages, userImage, tabGroup, seedUsage,
    });
  }

  let plan;
  let goal = instruction || (testFile?.test_name ?? "Run test file");
  let variables = { ..._extraVars, ...(testFile?.variables || {}) };

  if (testFile) {
    plan = { steps: testFile.steps };
    if (testFile.base_url) variables.base_url = testFile.base_url;
    onProgress?.(`Loaded ${plan.steps.length} step(s) from test file.`, "ok");
  } else if (prePlannedPlan?.steps?.length) {
    // The user already reviewed (and possibly edited) this plan in the approval
    // modal — run it as-is instead of paying for a second planning call.
    plan = prePlannedPlan;
    goal = prePlannedPlan.goal || goal;
    onProgress?.(`Running approved plan: ${plan.steps.length} step(s).`, "ok");
  } else {
    onProgress?.("Asking LLM to plan steps…");
    const snapshot = await snapshotPageReady(tabId, signal, sitePolicy);
    const memoryHint = await memoryHintFor(originOf(snapshot?.url)).catch(() => "");
    const planned = await planSteps({ llmConfig, instruction, snapshot, mode: effectiveMode, priorMessages, memoryHint, signal, userImage, onUsage });
    plan = planned;
    goal = planned.goal || goal;
    onProgress?.(`Plan: ${plan.steps.length} step(s).`, "ok");
  }

  // For "auto", the planner classifies page-work as exploration vs agentic; honor that.
  // (test is decided by file presence above; ask is routed before runAgent is called.)
  const resolvedMode =
    mode === "auto" && !testFile && plan?.mode === "agentic"
      ? "agentic"
      : effectiveMode;

  const stepResults = [];
  let passed = 0;
  let failed = 0;
  let skipped = 0;
  let blocked = false;
  let needsHuman = false;
  const confirmedSecretOrigins = new Set(); // per-run: keys already gate-confirmed for a given origin

  // Sanitize repeat values from untrusted LLM response before flattening.
  const _safeSteps = Array.isArray(plan.steps) ? plan.steps.map(s => ({
    ...s,
    repeat: typeof s.repeat === "number" ? Math.min(Math.max(1, Math.trunc(s.repeat) || 1), 1000) : undefined,
  })) : [];
  // Flatten the step list — handles repeat and if/else inline
  const flatSteps = flattenSteps(_safeSteps);

  // The planner returning zero steps is a real failure to act (not "nothing to
  // do") — surface it plainly instead of silently reporting a "passed" run
  // with 0/0 steps (buildSummary's failed===0 branch can't tell the two apart).
  if (!dryRun && flatSteps.length === 0) {
    onProgress?.("Planner returned no executable steps for this instruction.", "err");
    return {
      mode: resolvedMode,
      goal,
      started_at: startedAt,
      finished_at: new Date().toISOString(),
      steps: [],
      variables: redactObject(variables, secrets),
      artifacts: { screenshots: [], downloads: [], trace: null },
      result: { status: "fail", passed_steps: 0, failed_steps: 0, skipped_steps: 0 },
      summary:
        "The planner returned no executable steps for this instruction — it may need " +
        'rephrasing (e.g. "summarize the current page"), or try enabling Agent loop ' +
        "(Settings → Agent) so the model can perceive and act turn-by-turn instead of " +
        "planning the whole run up front.",
      usage: usageRecord(usageAcc),
      notes: usageAcc.notes,
    };
  }

  for (let i = 0; i < flatSteps.length; i++) {
    if (signal?.aborted) {
      onProgress?.("Aborted before step " + (i + 1), "err");
      break;
    }

    const raw = flatSteps[i];

    // if/else branching
    if (raw.action === "if") {
      const condSteps = await resolveIfBranch(raw, tabId, variables, secrets, { allowExecScript, resolvedMode, onHumanGate, onProgress });
      flatSteps.splice(i + 1, 0, ...condSteps);
      onProgress?.(`#${i + 1} if — branch resolved (${condSteps.length} sub-step(s))`, "ok");
      stepResults.push({
        index: i + 1,
        action: "if",
        status: "success",
        duration_ms: 0,
      });
      passed++;
      continue;
    }

    const step = resolveStep(raw, variables, secrets);

    // Dry run: highlight + resolve-only probe. Nothing executes, no gates fire —
    // it answers "would this plan's selectors work on this page?" for free.
    if (dryRun) {
      const dryStart = performance.now();
      onProgress?.(`#${i + 1} [dry] ${step.action} ${describeStep(step)}…`);
      const record = {
        index: i + 1,
        action: step.action,
        target: redact(step.target, secrets),
        value: maskPII(redact(step.value, secrets)),
        status: "success",
        duration_ms: 0,
        dry: true,
      };
      if (step.target) {
        await sendToContent(tabId, { type: "highlight", target: step.target, label: `#${i + 1} ${step.action}` }).catch(() => {});
        const probe = await sendToContent(tabId, { type: "resolveProbe", targets: [step.target] }).catch(() => null);
        const r = probe?.results?.[0];
        if (r?.found) {
          record.actual = `dry run: target found${r.visible ? "" : " (not visible)"}`;
          passed++;
        } else {
          record.status = "failed";
          record.error = { kind: "deterministic", message: "dry run: selector resolved 0 elements" };
          failed++;
        }
      } else {
        record.actual = `dry run: would ${step.action}${step.url ? ` → ${step.url}` : ""}`;
        passed++;
      }
      await new Promise((r) => setTimeout(r, 400)); // pacing so highlights are followable
      record.duration_ms = Math.round(performance.now() - dryStart);
      stepResults.push(record);
      onProgress?.(
        record.status === "success" ? `#${i + 1} success (dry)` : `#${i + 1} failed: selector not found (dry)`,
        record.status === "success" ? "ok" : "err",
      );
      continue;
    }

    // Step-by-step: wait for Continue before every step. The sidepanel makes
    // sure autoApprove does NOT bypass kind:"step" gates — manual oversight is
    // the whole point of the mode.
    if (stepByStep && onHumanGate) {
      const prev = stepResults[stepResults.length - 1] || null;
      const decision = await onHumanGate({
        kind: "step",
        reason: `Step ${i + 1} of ${flatSteps.length}: ${step.action} ${describeStep(step)}`,
        step,
        lastResult: prev ? { action: prev.action, status: prev.status, error: prev.error?.message } : null,
      });
      if (decision === "cancel") {
        onProgress?.(`Run stopped at step ${i + 1}`, "info");
        break;
      }
    }

    // Agentic mode: pause for human approval before risky/irreversible steps.
    if (resolvedMode === "agentic" && isRiskyStep(step) && onHumanGate) {
      const decision = await onHumanGate({
        reason: riskyReason(step),
        step,
        formState: await collectFormStateFor(tabId, step),
        ...(isJsCondition(step) ? { jsCondition: true } : {}),
      });
      if (decision === "cancel") {
        onProgress?.(`#${i + 1} cancelled at approval gate`, "info");
        throw new Error("Run cancelled by user at approval gate.");
      }
    }

    // Vault-secret / PII guard: gates the first use of each secret per
    // destination origin per run (never bypassed by autoApprove — see
    // sidepanel.js onHumanGate), falls back to the PII shape scan otherwise.
    let stepSecretHost = null;
    if (step.action === "type") {
      const secretGuard = await secretTypeGuard({ step, rawValue: raw.value, tabId, onHumanGate, confirmedSecretOrigins });
      if (secretGuard.cancelled) {
        onProgress?.(`#${i + 1} cancelled at secrets/PII guard`, "info");
        throw new Error("Run cancelled by user at approval gate.");
      }
      stepSecretHost = secretGuard.secretHost;
    }

    const stepStart = performance.now();
    onProgress?.(`#${i + 1} ${step.action} ${describeStep(step)}…`);

    let record = {
      index: i + 1,
      action: step.action,
      target: redact(step.target, secrets),
      value: maskPII(redact(step.value, secrets)),
      ...(step.headers ? { headers: redactObject(step.headers, secrets) } : {}),
      ...(stepSecretHost ? { secretHost: stepSecretHost } : {}),
      condition: step.condition || undefined,
      matcher: step.matcher || undefined,
      expected: step.expected ?? undefined,
      actual: undefined,
      status: "success",
      duration_ms: 0,
      screenshot: null,
      error: undefined,
    };

    if (qaDebugMode) await startQACapture(tabId);

    try {
      const result = await executeStepAndFollowTabs({ step, tabId, variables, secrets, llmConfig, onProgress, onHumanGate, signal, sitePolicy, allowExecScript, vision, tabGroup, onUsage });

      if (result?.newTabId && result.newTabId !== tabId) {
        tabId = result.newTabId; // switch_tab/open_tab or a page-spawned tab — follow it
        onTabChange?.(tabId);
      }
      if (result?.opened_tab) record.opened_tab = result.opened_tab;
      if (result?.actual !== undefined) record.actual = result.actual;
      if (result?.note) record.note = result.note;
      if (result?.variable && result?.value !== undefined) {
        variables[result.variable] = result.value;
        record.variable = result.variable; // lets the UI tell scratch captures from real outputs
      }
      if (result?.screenshot) record.screenshot = result.screenshot;
      if (result?.healed) record.healed = result.healed;
      if (step.to_user && result?.screenshot) onImage?.(result.screenshot, step.action === "zoom" ? "Zoomed region" : "Screenshot");

      if (step.action === "request_human") {
        record.status = "awaiting_human";
        needsHuman = true;
      } else if (isAssertLike(step.action) && result?.passed === false) {
        record.status = "failed";
        record.error = {
          kind: "deterministic",
          message: result.reason || "assertion failed",
        };
        failed++;
        // Auto-capture screenshot on assertion failure
        record.screenshot = await captureFailureScreenshot(tabId);
        if (step.critical) blocked = true;
        if (qaDebugMode) {
          const capture = await collectQACapture(tabId);
          if (capture) {
            onProgress?.(`#${i + 1} analyzing root cause…`);
            const analysis = await analyzeRootCause({ llmConfig, step, error: record.error.message, capture, signal, onUsage }).catch((e) => { if (e?.name === "AbortError") throw e; return null; });
            if (analysis) record.rootCauseAnalysis = analysis;
          }
        }
      } else {
        passed++;
      }
    } catch (err) {
      if (signal?.aborted || err?.name === "AbortError") {
        onProgress?.("Stopped by user", "info");
        break;
      }
      const kind = classifyError(err);
      record.status = "failed";
      record.error = { kind, message: err.message || String(err) };
      failed++;
      // Auto-capture screenshot on any step failure
      record.screenshot = await captureFailureScreenshot(tabId);
      if (kind === "blocking" || step.critical) blocked = true;
      onProgress?.(`#${i + 1} failed: ${err.message}`, "err");
      if (qaDebugMode) {
        const capture = await collectQACapture(tabId);
        if (capture) {
          onProgress?.(`#${i + 1} analyzing root cause…`);
          const analysis = await analyzeRootCause({ llmConfig, step, error: record.error.message, capture, signal, onUsage }).catch((e) => { if (e?.name === "AbortError") throw e; return null; });
          if (analysis) record.rootCauseAnalysis = analysis;
        }
      }
    }

    if (recordGif && !record.screenshot && stepResults.length < MAX_GIF_FRAMES) {
      record.screenshot = await captureScreenshot(tabId).catch(() => null);
    }
    record.duration_ms = Math.round(performance.now() - stepStart);
    stepResults.push(record);
    onProgress?.(
      `#${i + 1} ${record.status} (${record.duration_ms}ms)`,
      record.status === "success" || record.status === "awaiting_human" ? "ok" : "err",
    );

    if (blocked) {
      for (let j = i + 1; j < flatSteps.length; j++) {
        stepResults.push({
          index: j + 1,
          action: flatSteps[j].action,
          status: "skipped",
          duration_ms: 0,
        });
        skipped++;
      }
      break;
    }
    if (needsHuman) break;
  }

  const finishedAt = new Date().toISOString();

  const result = {
    mode: resolvedMode,
    goal,
    dry_run: dryRun || undefined,
    started_at: startedAt,
    finished_at: finishedAt,
    steps: stepResults,
    variables: redactObject(variables, secrets),
    artifacts: {
      screenshots: stepResults.map((s) => s.screenshot).filter(Boolean),
      downloads: [],
      trace: null,
    },
    result: {
      status: needsHuman
        ? "needs_human"
        : failed === 0
          ? "pass"
          : passed > 0
            ? "partial"
            : "fail",
      passed_steps: passed,
      failed_steps: failed,
      skipped_steps: skipped,
    },
    summary: buildSummary({ effectiveMode, passed, failed, skipped, needsHuman, stepResults }),
    security_summary: buildSecuritySummary(stepResults),
    usage: usageRecord(usageAcc),
    notes: usageAcc.notes,
  };
  return result;
}

// ---------- iterative agent loop ----------

const MAX_AGENT_STEPS = 20; // default action budget; Settings → "Max steps per run" overrides
// Stuck-loop guards: stop early instead of burning the whole step budget when the
// agent makes no progress. Tripping either of these breaks the loop.
const MAX_CONSECUTIVE_FAILURES = 3; // N failed steps in a row → give up
const MAX_IDENTICAL_ACTIONS = 3;    // same action+target+value repeated → no-op loop

// Actions whose result the model must be able to READ next turn — their output
// rides back through the history entry's `observation` field.
const OBSERVATION_ACTIONS = new Set([
  "read_page", "find", "get_page_text", "read_console_messages", "read_network_requests",
  "list_tabs", "read_tab", "extract", "exec_script", "summarize", "read_download", "read_document",
]);
// Content reads get more room — truncating an article to 1500 chars starves a
// summary down to its intro. Bounded: formatHistoryForLLM keeps only the most
// recent observation full-size and shrinks older ones to one line.
const CONTENT_OBSERVATION_ACTIONS = new Set(["get_page_text", "read_page", "summarize", "read_download", "read_document", "read_tab"]);
const MAX_OBSERVATION_CHARS = 1500;
const MAX_CONTENT_OBSERVATION_CHARS = 6000;
const observationCap = (action) =>
  CONTENT_OBSERVATION_ACTIONS.has(action) ? MAX_CONTENT_OBSERVATION_CHARS : MAX_OBSERVATION_CHARS;

// REQ-01 FR-01.3: sliding-window cap for the native tool-calling conversation.
// Unlike the flattened-history contract (squeezed via observationCap above),
// a real assistant/tool message sequence has no per-entry cap — a long run
// would otherwise grow the request payload without bound. Keeps the leading
// system message plus the most recent messages verbatim; anything older is
// collapsed into one synthetic marker so the model knows earlier turns exist
// without re-reading their full content (it can always re-run a read tool).
const MAX_NATIVE_MESSAGES = 60;
function capNativeMessages(messages) {
  if (messages.length <= MAX_NATIVE_MESSAGES) return messages;
  const system = messages[0];
  let keepFrom = messages.length - (MAX_NATIVE_MESSAGES - 2); // room for system + summary marker
  // Snap forward to the next user-role message: a turn (user → assistant
  // tool_calls → tool*) must never be split, or a kept tool message could
  // reference a dropped assistant tool_call and the API would reject it.
  while (keepFrom < messages.length && messages[keepFrom].role !== "user") keepFrom++;
  const dropped = messages.slice(1, keepFrom);
  const kept = messages.slice(keepFrom);
  const summary = {
    role: "user",
    content: `[${dropped.length} earlier conversation messages omitted for length — rely on the current snapshot and recent turns for state]`,
  };
  return [system, summary, ...kept];
}

// Perceive→reason→act→re-perceive. One LLM call per turn against a fresh
// snapshot — the behaviour that makes it feel like Claude/Gemini's browser
// agents. Prefers structured tool calls (a turn may carry SEVERAL, executed in
// order); endpoints without tool support fall back to the legacy text-JSON
// contract automatically, cached per endpoint. Reuses executeStep, isRiskyStep,
// and the QA-debug path.
async function runIterativeAgent({
  instruction, tabId, llmConfig, secrets, signal, onProgress, onHumanGate, onTabChange, onImage,
  qaDebugMode = false, vision = "off", streamNarration = false, onNarrationDelta,
  effectiveMode = "exploration", startedAt, variables = {}, sitePolicy = null,
  allowExecScript = false, stepByStep = false, maxSteps = MAX_AGENT_STEPS, recordGif = false,
  priorMessages = [], userImage = null, tabGroup = null, seedUsage = null,
}) {
  const stepResults = [];
  const history = []; // compact action+outcome trail fed back to the planner
  const confirmedSecretOrigins = new Set(); // per-run: keys already gate-confirmed for a given origin
  const usageAcc = newUsageAccumulator(seedUsage);
  const onUsage = (u) => accumulateUsage(usageAcc, u);
  // REQ-01 FR-01.3: native tool-calling conversation (system/user/assistant/
  // tool messages), threaded turn-to-turn like `history`. Only populated/used
  // in toolMode + NATIVE_TOOL_MESSAGES (see lib/llm.js); discarded whenever the
  // run falls back to the text-JSON contract, since the two shapes can't mix.
  let nativeMessages = [];
  let goal = instruction || "Browser task";
  let passed = 0, failed = 0, skipped = 0;
  let blocked = false, needsHuman = false;
  let visionUnavailableNoted = false; // log the "tab not visible" note at most once
  let finalAnswer = ""; // the agent's closing explanation, surfaced as the answer
  let consecutiveFailures = 0; // reset on success; trips MAX_CONSECUTIVE_FAILURES
  const recentSigs = []; // last few action signatures; trips MAX_IDENTICAL_ACTIONS
  let memoryHint = "", memoryHintOrigin; // per-origin site memory, refetched on origin change
  let actionIndex = 0;       // executed actions — the budget counts these, not turns
  let endReason = null;      // "done" | "stopped" | "stuck" | "no_step" | "cap" | null
  let prevStepFailed = false;      // Vision Auto trigger (a)
  let modelWantsScreenshot = false; // Vision Auto trigger (b): request_screenshot / need_screenshot
  let emptyTurns = 0;        // consecutive turns with no executable step
  let pendingZoomImage = null; // REQ-08: last zoom result, fed to the very next turn's screenshot slot then cleared

  // Tool-call capability (FR-01.5): cached per endpoint. Unset → try tools on
  // the first turn; a 400/404 or a silently ignored tools array flips the flag.
  const endpointKey = `toolsUnsupported:${(llmConfig.baseUrl || "https://api.openai.com/v1").replace(/\/+$/, "")}`;
  let toolMode = true;
  try {
    const flags = await chrome.storage.local.get(endpointKey);
    toolMode = !flags[endpointKey];
  } catch { /* storage unavailable — try tools anyway */ }
  const markToolsUnsupported = async (why) => {
    toolMode = false;
    onProgress?.(`Endpoint has no tool-call support (${why}) — using text mode from now on`, "info");
    try { await chrome.storage.local.set({ [endpointKey]: true }); } catch {}
  };

  turnLoop:
  while (actionIndex < maxSteps && !blocked && !needsHuman) {
    if (signal?.aborted) { onProgress?.("Stopped by user", "info"); endReason = "stopped"; break; }
    const isFirstTurn = stepResults.length === 0 && history.length === 0;

    // Observability is armed for the whole run (not only in QA Debug Mode) so
    // the agent can call read_console_messages / read_network_requests at any
    // time. Re-armed every turn because a navigation replaces the content
    // script (and its buffers); reset:false makes the re-arm a no-op while the
    // same page keeps accumulating. ensureWebRequestCapture never clobbers a
    // buffer the passive debug monitor is already filling.
    await sendToContent(tabId, { type: "startQACapture", reset: false }).catch(() => {});
    if (isFirstTurn) await sendSW({ type: "ensureWebRequestCapture", tabId }).catch(() => {});

    // Every turn waits for a real snapshot: snapshotPageReady returns instantly
    // when the page has content and only re-polls while it looks like a
    // loading shell — which is exactly the state right after a navigate on a
    // client-side-rendered page (tab "complete" fires before the content
    // paints); a page still shell-like when the budget runs out is tagged
    // possibly_loading so the prompt warns the model. With vision, the
    // screenshot must come AFTER the snapshot: the Set-of-Marks labels are drawn
    // from the snapshot's element registry, so the two are sequential and the
    // screenshot carries numbered badges matching the [N] element indexes.
    const snapshot = await snapshotPageReady(tabId, signal, sitePolicy);

    // Vision gating: "on" attaches every turn; "auto" only when (a) the last
    // action failed, (b) the model asked for one, or (c) the snapshot can't
    // carry the content — almost nothing interactive, or the page draws its
    // content on canvas/images (visual_content: flip-books, maps, design
    // tools), where the screenshot IS the only way to read it.
    const degenerate = !snapshot?.synthetic &&
      ((snapshot?.interactive?.length ?? 0) < 3 || snapshot?.visual_content === true);
    const wantVision =
      vision === "on" ||
      (vision === "auto" && (prevStepFailed || modelWantsScreenshot || degenerate));
    modelWantsScreenshot = false;
    // REQ-08 FR-08.3: a zoom the model requested last turn preempts the normal
    // full/SoM screenshot for exactly this one turn, then is cleared.
    const screenshot = pendingZoomImage || (wantVision ? await captureSoMScreenshot(tabId) : null);
    pendingZoomImage = null;

    const origin = originOf(snapshot?.url);
    if (origin !== memoryHintOrigin) {
      memoryHintOrigin = origin;
      memoryHint = await memoryHintFor(origin).catch(() => "");
    }
    if (isFirstTurn && snapshotIsEmpty(snapshot)) {
      onProgress?.(
        `Could not read the page${snapshot?.error ? ` (${snapshot.error})` : ""} — is the tab loaded and accessible?`,
        "err",
      );
    }
    if (wantVision && !screenshot && !visionUnavailableNoted) {
      // captureVisibleTab only works on the focused tab. If the run is in the
      // background, degrade gracefully: keep going text-only for this step.
      visionUnavailableNoted = true;
      onProgress?.("Screenshot unavailable (tab not visible) — continuing text-only", "info");
    }

    onProgress?.(`thinking (step ${actionIndex + 1})…`);
    // Stream the narration token-by-token into one in-place line when enabled;
    // otherwise it's emitted as a single line once the action is decided.
    const streaming = streamNarration && typeof onNarrationDelta === "function";
    const onNarration = streaming ? (partial) => onNarrationDelta(partial, false) : undefined;

    // The user-attached image rides on turn 1 only — after that it lives in the
    // native conversation (or has served its purpose in the text fallback).
    const planArgs = { llmConfig, goal, snapshot, screenshot, history, messages: nativeMessages, memoryHint, signal, onNarration, priorMessages, userImage: isFirstTurn ? userImage : null, onUsage };
    let turn;
    try {
      turn = await planNextAction({ ...planArgs, toolMode });
    } catch (e) {
      if (e?.name === "AbortError") throw e;
      if (toolMode && (e?.status === 400 || e?.status === 404)) {
        // Endpoint rejected the tools field outright — fall back transparently.
        await markToolsUnsupported(`HTTP ${e.status}`);
        nativeMessages = []; // native and flattened shapes can't mix on one array
        turn = await planNextAction({ ...planArgs, toolMode: false });
      } else {
        throw e;
      }
    }
    if (toolMode && isFirstTurn && !turn.toolCallsSeen) {
      // The endpoint accepted the request but silently ignored `tools` (typical
      // of older local servers): re-run the turn on the text-JSON contract
      // rather than mistaking prose for completion.
      await markToolsUnsupported("no tool_calls emitted");
      nativeMessages = [];
      turn = await planNextAction({ ...planArgs, toolMode: false });
    }

    const { narration, done, steps: rawSteps = [], needScreenshot, raw } = turn;
    if (needScreenshot) modelWantsScreenshot = true;

    if (narration) {
      if (streaming) onNarrationDelta(narration, true); // finalize the streamed line
      else onProgress?.(narration, "narrate");
    } else if (streaming) {
      onNarrationDelta("", true); // close any open line even if nothing streamed
    }
    if (done) {
      // An empty narration/summary on "done" (e.g. a truncated or malformed
      // tool-call argument that silently decoded to "") must never surface as
      // a blank success — that reads as "nothing happened" with no clue why.
      finalAnswer = narration || (
        actionIndex === 0
          ? "The agent reported the goal complete without taking any action or giving an explanation — the response may have been truncated or malformed. Try again, or rephrase the instruction."
          : "The agent reported the goal complete but did not provide a closing explanation."
      );
      endReason = "done";
      onProgress?.(narration ? "Agent reports the goal is complete." : "Agent reported completion with no explanation.", narration ? "ok" : "err");
      break;
    }
    if (!rawSteps.length) {
      if (modelWantsScreenshot && emptyTurns < 1) {
        // The model only asked for visual context — grant it one extra turn.
        emptyTurns++;
        onProgress?.("Agent requested a screenshot — attaching it next turn", "info");
        continue;
      }
      // No actionable step and the model didn't explicitly say it's done. Surface
      // the raw response so a 0/0 run is never silent — usually means the model
      // returned an unexpected shape we couldn't map to a step.
      endReason = "no_step";
      finalAnswer = `The agent returned no actionable step and did not report completion. Raw response: ${String(raw || "(empty)").slice(0, 300)}`;
      onProgress?.(
        `Agent returned no actionable step. Raw response: ${String(raw || "").slice(0, 300)}`,
        "err",
      );
      break;
    }
    emptyTurns = 0;

    // Execute this turn's tool calls in order. Gates, guards, and records fire
    // per action; a mid-turn failure stops the REST of the turn (those calls
    // assumed a page state that didn't materialize) and lets the next turn
    // re-perceive.
    // REQ-01 FR-01.3/01.4: keyed by __toolCallId, so a native-mode conversation
    // can emit one role:"tool" message per executed call after the turn.
    const toolResultsById = new Map();
    for (const rawStep of rawSteps) {
      if (signal?.aborted) { onProgress?.("Stopped by user", "info"); endReason = "stopped"; break turnLoop; }
      if (actionIndex >= maxSteps) break; // budget consumed mid-turn

      // Malformed tool arguments (wrong type / invalid JSON) become a clear
      // failed step the model can react to — never a thrown parse exception.
      if (rawStep.__argumentsError) {
        actionIndex++;
        const message = `invalid tool arguments: ${rawStep.__argumentsError}`;
        stepResults.push({
          index: actionIndex, action: rawStep.action || "unknown_tool",
          status: "failed", duration_ms: 0,
          error: { kind: "deterministic", message },
        });
        failed++;
        onProgress?.(`#${actionIndex} rejected: ${message}`, "err");
        history.push({ action: rawStep.action || "unknown_tool", status: "failed", error: message });
        if (rawStep.__toolCallId) toolResultsById.set(rawStep.__toolCallId, JSON.stringify({ status: "failed", error: message }));
        prevStepFailed = true;
        if (++consecutiveFailures >= MAX_CONSECUTIVE_FAILURES) {
          finalAnswer = `Stopped: ${MAX_CONSECUTIVE_FAILURES} consecutive failures without progress.`;
          endReason = "stuck";
          onProgress?.(finalAnswer, "err");
          break turnLoop;
        }
        continue;
      }

      const step = resolveStep(rawStep, variables, secrets);

      // No-op loop guard: if the agent keeps proposing the exact same action, the
      // page isn't changing and re-running it won't help — stop before the budget.
      const sig = `${step.action}::${JSON.stringify(step.target ?? "")}::${JSON.stringify(redact(step.value, secrets) ?? "")}`;
      recentSigs.push(sig);
      if (recentSigs.length > MAX_IDENTICAL_ACTIONS) recentSigs.shift();
      if (recentSigs.length === MAX_IDENTICAL_ACTIONS && recentSigs.every((s) => s === sig)) {
        finalAnswer = `Stopped: repeated the same action (${step.action}) ${MAX_IDENTICAL_ACTIONS} times without progress.`;
        endReason = "stuck";
        onProgress?.(finalAnswer, "err");
        break turnLoop;
      }

      // Step-by-step: wait for Continue before every action. The sidepanel makes
      // sure autoApprove does NOT bypass kind:"step" gates.
      if (stepByStep && onHumanGate) {
        const prev = history[history.length - 1] || null;
        const decision = await onHumanGate({
          kind: "step",
          reason: `Next: ${step.action} ${describeStep(step)}`,
          step,
          lastResult: prev ? { action: prev.action, status: prev.status, error: prev.error } : null,
        });
        if (decision === "cancel") {
          onProgress?.(`Run stopped before step ${actionIndex + 1}`, "info");
          endReason = "stopped";
          break turnLoop;
        }
      }

      // Auto safety gate — fires for any risky step, not just agentic mode.
      if (isRiskyStep(step) && onHumanGate) {
        const decision = await onHumanGate({
          reason: riskyReason(step),
          step,
          formState: await collectFormStateFor(tabId, step),
          ...(isJsCondition(step) ? { jsCondition: true } : {}),
        });
        if (decision === "cancel") {
          onProgress?.(`#${actionIndex + 1} cancelled at approval gate`, "info");
          endReason = "stopped";
          break turnLoop;
        }
      }

      // Vault-secret / PII guard: gates the first use of each secret per
      // destination origin per run (never bypassed by autoApprove — see
      // sidepanel.js onHumanGate), falls back to the PII shape scan otherwise.
      let stepSecretHost = null;
      if (step.action === "type") {
        const secretGuard = await secretTypeGuard({ step, rawValue: rawStep?.value, tabId, onHumanGate, confirmedSecretOrigins });
        if (secretGuard.cancelled) {
          onProgress?.(`#${actionIndex + 1} cancelled at secrets/PII guard`, "info");
          endReason = "stopped";
          break turnLoop;
        }
        stepSecretHost = secretGuard.secretHost;
      }

      actionIndex++;
      const stepIndex = actionIndex;

      // Persistent annotation: mark what we're about to touch. It stays on the
      // page after the action (demoted to a greyed "done" badge below), building
      // a visible trail of the run.
      if (step.target) {
        await sendToContent(tabId, {
          type: "annotate", target: step.target, state: "next", label: `#${stepIndex} ${step.action}`,
        }).catch(() => {});
      }

      const stepStart = performance.now();
      onProgress?.(`#${stepIndex} ${step.action} ${describeStep(step)}…`);

      const record = {
        index: stepIndex,
        action: step.action,
        target: redact(step.target, secrets),
        value: maskPII(redact(step.value, secrets)),
        ...(step.headers ? { headers: redactObject(step.headers, secrets) } : {}),
        ...(stepSecretHost ? { secretHost: stepSecretHost } : {}),
        narration: narration || undefined,
        status: "success",
        duration_ms: 0,
        screenshot: null,
        vision_attached: !!screenshot,
        error: undefined,
      };

      if (qaDebugMode) await startQACapture(tabId);

      let observation;
      try {
        const result = await executeStepAndFollowTabs({ step, tabId, variables, secrets, llmConfig, onProgress, onHumanGate, signal, sitePolicy, allowExecScript, vision, tabGroup, onUsage });
        if (result?.newTabId && result.newTabId !== tabId) {
          tabId = result.newTabId; // switch_tab/open_tab or a page-spawned tab — follow it
          onTabChange?.(tabId);
        }
        if (result?.opened_tab) record.opened_tab = result.opened_tab;
        if (result?.actual !== undefined) record.actual = result.actual;
        if (result?.note) record.note = result.note;
        if (result?.variable && result?.value !== undefined) {
          variables[result.variable] = result.value;
          record.variable = result.variable; // lets the UI tell scratch captures from real outputs
          // Float the extracted value as a badge next to its source element.
          if (step.target) {
            sendToContent(tabId, {
              type: "annotate", target: step.target, state: "extracted",
              label: `${result.variable} = ${String(redact(result.value, secrets)).slice(0, 40)}`,
            }).catch(() => {});
          }
        }
        if (result?.screenshot) record.screenshot = result.screenshot;
        if (result?.healed) record.healed = result.healed;
        // REQ-08 FR-08.3: a zoom's cropped image feeds the very next turn's
        // vision slot, so the model actually sees the enlarged region.
        if (step.action === "zoom" && result?.screenshot) pendingZoomImage = result.screenshot;
        // REQ-20: to_user shares the SAME capture into the chat thread — no
        // extra screenshot round-trip when the model both reasons over and
        // shows one shot.
        if (step.to_user && result?.screenshot) {
          onImage?.(result.screenshot, narration || (step.action === "zoom" ? "Zoomed region" : "Screenshot"));
        }

        // Observation channel: read-type results ride back to the model through
        // the next turn's HISTORY block.
        if (OBSERVATION_ACTIONS.has(step.action)) {
          observation = result?.value ?? result?.actual;
          // exec_script's value would otherwise hide the auto-follow note.
          if (result?.opened_tab) {
            observation = _appendNote(
              typeof observation === "string" ? observation : JSON.stringify(observation ?? ""),
              `this action opened a new tab: ${result.opened_tab.url} — the run is now operating on it`
            );
          }
        } else if (result?.actual !== undefined) {
          observation = result.actual;
        }

        if (step.action === "request_human") {
          // Approval granted (a cancel throws and is handled in catch). Record it
          // and keep going — the human said yes, so the run should resume, not stop.
          record.status = "success";
          record.value = maskPII(redact(step.value, secrets));
          passed++;
        } else if (isAssertLike(step.action) && result?.passed === false) {
          record.status = "failed";
          record.error = { kind: "deterministic", message: result.reason || "assertion failed" };
          failed++;
          record.screenshot = await captureFailureScreenshot(tabId);
          if (step.critical) blocked = true;
          await maybeAnalyzeRootCause({ qaDebugMode, tabId, llmConfig, step, record, onProgress, index: stepIndex, signal, onUsage });
        } else {
          passed++;
        }
      } catch (err) {
        if (signal?.aborted || err?.name === "AbortError") { onProgress?.("Stopped by user", "info"); endReason = "stopped"; break turnLoop; }
        if (err?.userCancelled) { onProgress?.(`#${stepIndex} cancelled at approval gate`, "info"); endReason = "stopped"; break turnLoop; }
        const kind = classifyError(err);
        record.status = "failed";
        record.error = { kind, message: err.message || String(err) };
        failed++;
        record.screenshot = await captureFailureScreenshot(tabId);
        if (kind === "blocking") blocked = true;
        onProgress?.(`#${stepIndex} failed: ${err.message}`, "err");
        await maybeAnalyzeRootCause({ qaDebugMode, tabId, llmConfig, step, record, onProgress, index: stepIndex, signal, onUsage });
      }

      // Demote the pulsing "next" badge into the greyed run trail.
      if (step.target) {
        await sendToContent(tabId, {
          type: "annotate", target: step.target, state: "done",
          label: `#${stepIndex} ${step.action}${record.status === "failed" ? " ✗" : ""}`,
        }).catch(() => {});
      }
      if (recordGif && !record.screenshot && stepResults.length < MAX_GIF_FRAMES) {
        record.screenshot = await captureScreenshot(tabId).catch(() => null);
      }
      record.duration_ms = Math.round(performance.now() - stepStart);
      stepResults.push(record);
      onProgress?.(
        `#${stepIndex} ${record.status} (${record.duration_ms}ms)`,
        record.status === "success" || record.status === "awaiting_human" ? "ok" : "err",
      );

      const historyEntry = {
        action: step.action,
        target: redact(step.target, secrets),
        status: record.status,
        ...(record.error ? { error: record.error.message } : {}),
      };
      if (observation !== undefined && observation !== null && observation !== "") {
        const obsStr = maskPII(redact(typeof observation === "string" ? observation : JSON.stringify(observation), secrets));
        const cap = observationCap(step.action);
        historyEntry.observation = obsStr.length > cap
          ? obsStr.slice(0, cap) + "…(truncated)"
          : obsStr;
      }
      history.push(historyEntry);
      if (step.__toolCallId) {
        toolResultsById.set(step.__toolCallId, JSON.stringify({
          status: historyEntry.status,
          ...(historyEntry.error ? { error: historyEntry.error } : {}),
          ...(historyEntry.observation !== undefined ? { observation: historyEntry.observation } : {}),
        }));
      }

      prevStepFailed = record.status === "failed";

      // Give-up guard: too many failures in a row means the agent is thrashing.
      consecutiveFailures = record.status === "failed" ? consecutiveFailures + 1 : 0;
      if (consecutiveFailures >= MAX_CONSECUTIVE_FAILURES) {
        finalAnswer = `Stopped: ${MAX_CONSECUTIVE_FAILURES} consecutive failures without progress.`;
        endReason = "stuck";
        onProgress?.(finalAnswer, "err");
        break turnLoop;
      }

      // A failed call invalidates the rest of this turn's plan — re-perceive.
      if (record.status === "failed") break;
    }

    // REQ-01 FR-01.3/01.4: extend the native conversation with this turn's
    // assistant tool_calls message plus one tool-role message per call — every
    // call the model made gets a response (OpenAI requires this), even ones
    // never executed (turn ended early on a failure/budget/gate cancel) or
    // control calls (done/request_screenshot) that never reach executeStep.
    if (toolMode && Array.isArray(turn.toolCallsRaw) && turn.toolCallsRaw.length) {
      const assistantToolCalls = turn.toolCallsRaw.map((c) => ({
        id: c.id,
        type: "function",
        function: { name: c.name, arguments: JSON.stringify(c.argumentsError ? {} : (c.arguments || {})) },
      }));
      const toolMsgs = turn.toolCallsRaw.map((c) => {
        let content;
        if (c.name === "done") content = JSON.stringify({ status: "run_complete", summary: narration || "" });
        else if (c.name === "request_screenshot") content = JSON.stringify({ status: "ack", note: "screenshot will be attached next turn" });
        else content = toolResultsById.get(c.id) ?? JSON.stringify({ status: "not_executed", note: "a prior action in this turn ended the turn early" });
        return { role: "tool", tool_call_id: c.id, content };
      });
      nativeMessages = capNativeMessages([
        ...(turn.messages || nativeMessages),
        { role: "assistant", content: narration || null, tool_calls: assistantToolCalls },
        ...toolMsgs,
      ]);
    } else if (toolMode) {
      nativeMessages = turn.messages || nativeMessages;
    }
  }

  // Budget exhausted without another exit path: a distinct status so the user
  // knows to raise the budget and continue, not that the run "failed".
  if (!endReason && actionIndex >= maxSteps && !blocked && !needsHuman) {
    endReason = "cap";
    finalAnswer = finalAnswer ||
      `Stopped at the ${maxSteps}-action budget before finishing — ${passed} action(s) succeeded, ${failed} failed. ` +
      `Raise "Max steps per run" in Settings (or set max_steps on the run) and re-run to continue.`;
    onProgress?.(`Max steps reached (${maxSteps})`, "err");
  }

  // The run trail served its purpose — leave the page clean.
  await sendToContent(tabId, { type: "clearAnnotations" }).catch(() => {});
  // Restore the page's console; the passive debug monitor re-arms on its own.
  await sendToContent(tabId, { type: "stopQACapture" }).catch(() => {});

  // REQ-12 FR-12.3: auto-restore the window if this run resized it and never
  // explicitly restored — a natural-language goal is less likely than a
  // scripted test to remember to size back down.
  const origSize = _originalWindowSize.get(tabId);
  if (origSize) {
    await sendSW({ type: "resizeWindow", tabId, width: origSize.width, height: origSize.height }).catch(() => {});
    _originalWindowSize.delete(tabId);
  }

  return {
    mode: effectiveMode,
    goal,
    started_at: startedAt,
    finished_at: new Date().toISOString(),
    steps: stepResults,
    variables: redactObject(variables, secrets),
    artifacts: { screenshots: stepResults.map((s) => s.screenshot).filter(Boolean), downloads: [], trace: null },
    result: {
      status: needsHuman
        ? "needs_human"
        : endReason === "cap"
          ? "max_steps_reached"
          : endReason === "no_step" && passed === 0
            ? "fail"
            : failed === 0 ? "pass" : passed > 0 ? "partial" : "fail",
      passed_steps: passed,
      failed_steps: failed,
      skipped_steps: skipped,
    },
    summary: endReason === "cap"
      ? `Run stopped at the ${maxSteps}-action budget in ${effectiveMode} mode: ${passed} passed, ${failed} failed so far.`
      : endReason === "no_step" && passed === 0
        ? finalAnswer
        : buildSummary({ effectiveMode, passed, failed, skipped, needsHuman, stepResults }),
    security_summary: buildSecuritySummary(stepResults),
    answer: finalAnswer,
    usage: usageRecord(usageAcc),
    notes: usageAcc.notes,
  };
}

// Shared QA-debug root-cause hook used by both run paths.
async function maybeAnalyzeRootCause({ qaDebugMode, tabId, llmConfig, step, record, onProgress, index, signal, onUsage }) {
  if (!qaDebugMode || !record.error) return;
  const capture = await collectQACapture(tabId);
  if (!capture) return;
  onProgress?.(`#${index} analyzing root cause…`);
  const analysis = await analyzeRootCause({ llmConfig, step, error: record.error.message, capture, signal, onUsage })
    .catch((e) => { if (e?.name === "AbortError") throw e; return null; });
  if (analysis) record.rootCauseAnalysis = analysis;
}

// ---------- suite runner ----------

async function runSuite({ testFile, tabId, llmConfig, secrets, signal, onProgress, onHumanGate, onTabChange, qaDebugMode = false, sitePolicy = null, allowExecScript = false, dryRun = false, stepByStep = false, tabGroup = null, seedUsage = null }) {
  const startedAt = new Date().toISOString();
  const suiteResults = [];
  const sharedVars = { ...testFile.shared_variables };

  // A test that retargets (switch_tab/open_tab/auto-follow) hands the next
  // test its final tab, so the suite keeps acting where the run ended up.
  const trackTab = (newTabId) => {
    tabId = newTabId;
    onTabChange?.(newTabId);
  };

  for (const test of testFile.tests) {
    if (signal?.aborted) break;
    onProgress?.(`━━ ${test.test_name} ━━`, "info");
    const result = await runAgent({
      instruction: test.test_name,
      fileText: null,
      mode: "test",
      tabId,
      llmConfig,
      secrets,
      signal,
      onProgress,
      onHumanGate,
      onTabChange: trackTab,
      qaDebugMode,
      sitePolicy,
      allowExecScript,
      dryRun,
      stepByStep,
      _testFile: test,
      _extraVars: sharedVars,
      _tabGroup: tabGroup,
    });
    suiteResults.push(result);
    Object.assign(sharedVars, result.variables || {});
  }

  const finishedAt = new Date().toISOString();
  const totalPassed = suiteResults.reduce((s, r) => s + (r.result?.passed_steps || 0), 0);
  const totalFailed = suiteResults.reduce((s, r) => s + (r.result?.failed_steps || 0), 0);
  const totalSkipped = suiteResults.reduce((s, r) => s + (r.result?.skipped_steps || 0), 0);
  const anyFailed = suiteResults.some((r) => r.result?.status !== "pass");
  // runSuite makes no LLM calls of its own — it delegates entirely to runAgent
  // per test, so the suite's usage is any seeded pre-run spend plus the sum of
  // each nested result's usage. Seeded from a full accumulator so the suite
  // record carries the same keys (usage_missing, notes) as a single run.
  const suiteUsage = suiteResults.reduce((acc, r) => {
    if (r.usage) {
      acc.prompt_tokens += r.usage.prompt_tokens || 0;
      acc.completion_tokens += r.usage.completion_tokens || 0;
      acc.total_tokens += r.usage.total_tokens || 0;
      acc.llm_calls += r.usage.llm_calls || 0;
      acc.usage_missing += r.usage.usage_missing || 0;
    }
    return acc;
  }, newUsageAccumulator(seedUsage));

  return {
    mode: "suite",
    goal: testFile.suite_name,
    started_at: startedAt,
    finished_at: finishedAt,
    tests: suiteResults,
    steps: suiteResults.flatMap((r) => r.steps || []),
    variables: sharedVars,
    artifacts: {
      screenshots: suiteResults.flatMap((r) => r.artifacts?.screenshots || []),
      downloads: [],
      trace: null,
    },
    result: {
      status: anyFailed ? (totalPassed > 0 ? "partial" : "fail") : "pass",
      passed_steps: totalPassed,
      failed_steps: totalFailed,
      skipped_steps: totalSkipped,
    },
    summary: `Suite "${testFile.suite_name}": ${suiteResults.length} test(s), ${totalPassed} passed, ${totalFailed} failed.`,
    usage: usageRecord(suiteUsage),
    notes: [...suiteUsage.notes, ...suiteResults.flatMap((r) => r.notes || [])],
  };
}

// ---------- step expansion ----------

function flattenSteps(steps) {
  // Use Array.from with a constant-bounded length instead of a numeric for-loop
  // so no tainted value from fetch() response is used as a loop condition
  // (Checkmarx: Unchecked Input For Loop Condition / Server DoS by Loop).
  const out = [];
  for (const s of steps) {
    const _repeatRaw = typeof s.repeat === "number" ? s.repeat : 1;
    const _repeatSafe = Math.min(Math.max(1, Math.trunc(_repeatRaw) || 1), 1000);
    Array.from({ length: _repeatSafe }).forEach(() => out.push(s));
  }
  return out;
}

async function resolveIfBranch(ifStep, tabId, variables, secrets, { allowExecScript = false, resolvedMode, onHumanGate, onProgress } = {}) {
  const condition = ifStep.condition;
  let conditionTrue = false;
  try {
    if (condition && condition.startsWith("js:")) {
      // F-09: a js: condition is exactly as powerful as exec_script (arbitrary
      // MAIN-world JS) — it must not be a softer path to the same capability.
      if (!allowExecScript) {
        onProgress?.("if: js: condition blocked — enable Developer mode (Allow script execution) in Settings.", "err");
        return ifStep.else || [];
      }
      if (resolvedMode === "agentic" && onHumanGate) {
        const decision = await onHumanGate({
          reason: `Risky action: if condition "${condition}" — evaluates arbitrary JavaScript in the page`,
          step: ifStep,
          jsCondition: true,
        });
        if (decision === "cancel") throw new Error("Run cancelled by user at approval gate.");
      }
      const script = condition.slice("js:".length);
      const results = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        // eslint-disable-next-line no-new-func
        func: (s) => { try { return !!Function("return (" + s + ")")(); } catch { return false; } },
        args: [script],
      });
      conditionTrue = !!results?.[0]?.result;
    } else if (condition) {
      const res = await sendToContent(tabId, {
        type: "execAction",
        step: { action: "wait", condition, target: ifStep.target, timeout_ms: 500 },
      });
      conditionTrue = !!res?.ok;
    }
  } catch (err) {
    if (err?.message === "Run cancelled by user at approval gate.") throw err;
    onProgress?.(`if: condition check failed — ${err?.message || err}`, "err");
  }
  return conditionTrue
    ? (ifStep.then || [])
    : (ifStep.else || []);
}

// ---------- helpers ----------

// Substring or /regex/ filter for read_network_requests (content.js has its own
// copy for read_console_messages — the two run in different worlds).
function buildRunnerTextFilter(filter) {
  const f = String(filter || "").trim();
  if (!f) return null;
  const m = f.match(/^\/(.+)\/([a-z]*)$/);
  if (m) {
    try {
      const re = new RegExp(m[1], m[2]);
      return (text) => re.test(String(text));
    } catch { /* fall through to substring */ }
  }
  const needle = f.toLowerCase();
  return (text) => String(text).toLowerCase().includes(needle);
}

function isAssertLike(action) {
  return ["assert", "assert_screenshot", "assert_performance", "accessibility_audit"].includes(action);
}

function describeStep(s) {
  if (s.action === "navigate") return s.url || s.value || "";
  if (s.target) {
    if (Array.isArray(s.target)) {
      const head = String(s.target[0] ?? "").slice(0, 60);
      return s.target.length > 1 ? `${head} (+${s.target.length - 1} more)` : head;
    }
    return String(s.target).slice(0, 60);
  }
  return "";
}

function classifyError(err) {
  const msg = String(err?.message || err);
  if (/navigation|net::|HTTP \d{3}/.test(msg)) return "blocking";
  return "deterministic";
}

function buildSummary({ effectiveMode, passed, failed, skipped, needsHuman, stepResults }) {
  if (needsHuman) {
    const ask = stepResults.find((s) => s.status === "awaiting_human");
    return `Awaiting human: ${ask?.value || "approval required"}.`;
  }
  if (failed === 0) {
    return `Run passed in ${effectiveMode} mode. ${passed} step(s) executed cleanly.`;
  }
  return `Run finished with ${failed} failure(s), ${passed} pass(es), ${skipped} skipped in ${effectiveMode} mode.`;
}

// F-06: a run's only deterministic backstop against page-injected instructions
// is what F-01/F-03/F-05 enforce mid-run; this is the after-the-fact receipt —
// distinct origins touched, exec_script executions, and secret hosts used
// (from the Fix 2 audit field) — so a surprising run is visible at a glance
// instead of requiring a step-by-step log read. Derived entirely from data the
// other fixes already produce; null when a run touched none of the above.
const URL_IN_TEXT_RE = /https?:\/\/[^\s)]+/;
function buildSecuritySummary(stepResults) {
  const origins = new Set();
  let execScriptCount = 0;
  const secretHosts = new Set();
  for (const s of stepResults) {
    if ((s.action === "navigate" || s.action === "open_tab") && s.actual) {
      const m = URL_IN_TEXT_RE.exec(s.actual);
      if (m) { try { origins.add(new URL(m[0]).origin); } catch {} }
    }
    if (s.opened_tab?.url) { try { origins.add(new URL(s.opened_tab.url).origin); } catch {} }
    if (s.action === "exec_script") execScriptCount++;
    if (s.secretHost) secretHosts.add(s.secretHost);
  }
  if (!origins.size && !execScriptCount && !secretHosts.size) return null;
  return {
    origins: [...origins],
    exec_script_count: execScriptCount,
    secrets_used: [...secretHosts],
  };
}

// ---------- variable / secret resolution ----------

function resolveStep(step, variables, secrets) {
  const r = (v) => resolveValue(v, variables, secrets);
  return {
    ...step,
    target: r(step.target),
    value: r(step.value),
    url: r(step.url),
    expected: r(step.expected),
    condition: r(step.condition),
    response: r(step.response),
    // navigate's custom headers (REQ-15) may reference ${secrets.*} per value.
    headers: step.headers && typeof step.headers === "object"
      ? Object.fromEntries(Object.entries(step.headers).map(([k, v]) => [k, r(v)]))
      : step.headers,
  };
}

function resolveValue(value, variables, secrets) {
  if (Array.isArray(value)) return value.map((v) => resolveValue(v, variables, secrets));
  if (typeof value !== "string") return value;
  return value.replace(/\$\{([^}]+)\}/g, (_, key) => {
    if (key.startsWith("secrets.")) {
      const k = key.slice("secrets.".length);
      const v = secrets[k];
      if (v === undefined) throw new Error(`Missing secret: ${k}`);
      return v;
    }
    if (variables[key] !== undefined) return variables[key];
    return `\${${key}}`;
  });
}

function redact(value, secrets) {
  if (Array.isArray(value)) return value.map((v) => redact(v, secrets));
  if (typeof value !== "string" || !value) return value;
  let out = value;
  for (const k of Object.keys(secrets)) {
    const v = secrets[k];
    if (v && out.includes(v)) out = out.split(v).join("***");
  }
  return out;
}

function redactObject(obj, secrets) {
  const out = {};
  for (const k of Object.keys(obj)) {
    out[k] = redact(obj[k], secrets);
  }
  return out;
}

// Vault-secret guard: gates the first use of each ${secrets.KEY} against its
// destination origin, once per run — the question isn't "does this value look
// sensitive" (that's scanPII's job, used as a fallback for non-vault values)
// but "is this the origin the user expects this credential typed into."
// Non-bypassable by autoApprove — the caller marks gateData.secretKey so the
// sidepanel's onHumanGate treats it like exec_script. Returns
// { cancelled, secretHost } so the caller can log which host received a secret.
async function secretTypeGuard({ step, rawValue, tabId, onHumanGate, confirmedSecretOrigins }) {
  if (!onHumanGate) return { cancelled: false, secretHost: null };
  const secretKeys = [...String(rawValue || "").matchAll(/\$\{secrets\.([^}]+)\}/g)].map((m) => m[1]);
  if (!secretKeys.length) {
    const hits = scanPII(step.value);
    if (!hits.length) return { cancelled: false, secretHost: null };
    const kinds = [...new Set(hits.map((h) => h.kind))].join(", ");
    const decision = await onHumanGate({
      reason: `About to type what looks like a ${kinds} into the page. Continue?`,
      step,
    });
    return { cancelled: decision === "cancel", secretHost: null };
  }
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  let host = "the current page";
  try { if (tab?.url) host = new URL(tab.url).host || host; } catch {}
  for (const key of secretKeys) {
    const originKey = `${key}@${host}`;
    if (confirmedSecretOrigins.has(originKey)) continue;
    const decision = await onHumanGate({
      reason: `Type secrets.${key} into ${host}?`,
      // Deliberately NOT the resolved step — step.value holds the real secret,
      // and the gate dialog must show the destination host, never the value.
      step: { action: step.action, target: step.target },
      secretKey: key,
    });
    if (decision === "cancel") return { cancelled: true, secretHost: host };
    confirmedSecretOrigins.add(originKey);
  }
  return { cancelled: false, secretHost: host };
}

// ---------- execution ----------

// ---------- Assistant tab group (visibility scope) ----------

// A run whose tab sits inside a tab group titled "Assistant" is scoped to that
// group: list_tabs/switch_tab/read_tab only see grouped tabs, and tabs the run
// opens (open_tab or page-spawned) are added to the group so the scope never
// silently expands. The probe runs once per run; every membership check after
// that queries the live tab-group state, so dragging a tab in or out takes
// effect immediately.
async function resolveAssistantGroup(tabId) {
  const res = await sendSW({ type: "getAssistantGroup", tabId }).catch(() => null);
  return res?.ok && res.group ? res.group : null;
}

async function addTabToGroup(tabId, tabGroup) {
  const res = await sendSW({ type: "groupTab", tabId, groupId: tabGroup.id }).catch(() => null);
  return !!res?.ok;
}

// Actions the page itself can turn into a new tab (target=_blank, window.open).
const TAB_SPAWNING_ACTIONS = new Set(["click", "dblclick", "click_at", "press_key", "exec_script"]);

const _appendNote = (actual, note) => (actual ? `${actual} — ${note}` : note);

// executeStep, plus auto-follow: if the step made the page spawn a new tab
// (target=_blank / window.open), adopt it — activate, wait, settle — and return
// it as newTabId so both run loops retarget through their existing
// `if (result?.newTabId)` path, exactly as switch_tab/open_tab do. Best-effort:
// a successful step never fails because the follow-up did.
async function executeStepAndFollowTabs(args) {
  const { step, tabId, signal, sitePolicy, tabGroup } = args;
  const sinceTs = Date.now() - 250; // slack: tabs.onCreated can beat this timestamp by a tick
  const result = await executeStep(args);
  if (!TAB_SPAWNING_ACTIONS.has(step.action) || result?.newTabId || signal?.aborted) return result;
  try {
    const res = await sendSW({ type: "takeSpawnedTabs", openerTabId: tabId, sinceTs });
    const spawned = res?.ok ? res.tabs : [];
    for (let i = spawned.length - 1; i >= 0; i--) { // newest first
      const tab = await chrome.tabs.get(spawned[i].tabId).catch(() => null);
      if (!tab) continue; // self-closed popup
      const url = tab.url || tab.pendingUrl || spawned[i].url || "";
      const verdict = isHostAllowed(url, sitePolicy);
      if (!verdict.allowed) {
        return {
          ...result,
          actual: _appendNote(result?.actual, `this action opened a new tab (${url}) but ${verdict.reason} — staying on the current tab`),
        };
      }
      // FR11: a page-spawned tab joins the Assistant group before the run
      // adopts it, so the visibility scope never silently expands.
      const grouped = tabGroup ? await addTabToGroup(tab.id, tabGroup) : false;
      await chrome.tabs.update(tab.id, { active: true }); // screenshots need the run tab active
      await waitForTabComplete(tab.id, step.timeout_ms || DEFAULT_STEP_TIMEOUT_MS, signal);
      await settleAfterNavigation(tab.id, signal);
      const finalTab = await chrome.tabs.get(tab.id).catch(() => tab); // url settles after load
      const finalUrl = finalTab.url || url;
      return {
        ...result,
        newTabId: tab.id,
        opened_tab: { id: tab.id, url: finalUrl, title: finalTab.title || "" },
        actual: _appendNote(result?.actual, `this action opened a new tab: ${finalUrl}${grouped ? " (added to the Assistant group)" : ""} — the run is now operating on it (use switch_tab with tab_id ${tabId} to go back)`),
      };
    }
  } catch (err) {
    if (signal?.aborted || err?.name === "AbortError" || err?.userCancelled) throw err;
    // otherwise best-effort — keep the step's own result
  }
  return result;
}

async function executeStep({ step, tabId, variables, secrets, llmConfig, onProgress, onHumanGate, signal, sitePolicy = null, allowExecScript = false, vision = false, tabGroup = null, onUsage }) {
  // F-09: a condition's js: expression is exactly as powerful as exec_script
  // (arbitrary MAIN-world JS via content.js's evalInPageWorld) — single
  // chokepoint for every action that carries a condition field (wait today).
  if (typeof step.condition === "string" && step.condition.startsWith("js:") && !allowExecScript) {
    throw new Error("js: conditions are disabled — enable Developer mode (Allow script execution) in Settings to allow script-execution conditions.");
  }

  // REQ-11: "screenshot:last" / "screenshot:<id>" in upload_file/drop_file's
  // value resolves to a captured artifact's data: URL before dispatch — both
  // actions already accept a literal data: URL, so nothing downstream changes.
  if ((step.action === "upload_file" || step.action === "drop_file") && typeof step.value === "string" && /^screenshot:/i.test(step.value)) {
    const resolved = _resolveScreenshotArtifact(tabId, step.value);
    if (!resolved) throw new Error(`${step.action}: no screenshot artifact found for "${step.value}" — take a screenshot or zoom first`);
    step = { ...step, value: resolved };
  }

  // Text-read actions on a PDF tab hit the viewer's empty outer document and
  // fail with a misleading "still loading" error (and a bogus RACE_CONDITION
  // RCA). Fail fast with the actual fix instead — in the agent loop this error
  // rides back through HISTORY, so even a small model that ignored the snapshot
  // hint recovers by switching to read_document on its next turn. (summarize is
  // handled below by reading the PDF itself instead.)
  if (step.action === "get_page_text" || step.action === "read_page") {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (detectDocumentUrl(tab?.url)?.kind === "pdf") {
      throw new Error(
        `${step.action}: this tab is a PDF — the PDF viewer has no readable page text. Use the read_document action instead (it reads the PDF itself; chunk with pages="1-5").`,
      );
    }
  }

  if (step.action === "navigate") {
    const url = step.url || step.value;
    if (!url) throw new Error("navigate requires a url");
    const verdict = isHostAllowed(url, sitePolicy);
    if (!verdict.allowed) throw new Error(`Navigation blocked by site policy: ${verdict.reason}`);
    _frameByTab.delete(tabId); // navigation destroys any selected subframe

    // REQ-15: custom headers via a declarativeNetRequest session rule scoped
    // to this exact URL + tab, installed just before navigating and always
    // removed afterward (success, failure, or abort — the finally covers all
    // three) so it can never outlive this one navigation.
    const hasHeaders = step.headers && typeof step.headers === "object" && Object.keys(step.headers).length > 0;
    if (hasHeaders) {
      const install = await sendSW({ type: "installHeaderRule", tabId, url, headers: step.headers });
      if (!install?.ok) throw new Error(`navigate: could not install header rule (${install?.error || "unknown error"})`);
    }
    try {
      await chrome.tabs.update(tabId, { url });
      await waitForTabComplete(tabId, step.timeout_ms || DEFAULT_STEP_TIMEOUT_MS, signal);
      await settleAfterNavigation(tabId, signal);
    } finally {
      if (hasHeaders) await sendSW({ type: "removeHeaderRule", tabId }).catch(() => {});
    }
    return { actual: url };
  }

  if (step.action === "back") {
    _frameByTab.delete(tabId);
    await chrome.tabs.goBack(tabId);
    await waitForTabComplete(tabId, step.timeout_ms || DEFAULT_STEP_TIMEOUT_MS, signal);
    await settleAfterNavigation(tabId, signal);
    return {};
  }

  if (step.action === "forward") {
    _frameByTab.delete(tabId);
    await chrome.tabs.goForward(tabId);
    await waitForTabComplete(tabId, step.timeout_ms || DEFAULT_STEP_TIMEOUT_MS, signal);
    await settleAfterNavigation(tabId, signal);
    return {};
  }

  if (step.action === "reload") {
    _frameByTab.delete(tabId);
    await chrome.tabs.reload(tabId);
    await waitForTabComplete(tabId, step.timeout_ms || DEFAULT_STEP_TIMEOUT_MS, signal);
    await settleAfterNavigation(tabId, signal);
    return {};
  }

  // Open a url in a NEW tab and switch the run to it. Both run loops already
  // retarget on newTabId (the switch_tab mechanism), so subsequent steps act on
  // the new tab; switch_tab (by title/url) brings the run back.
  if (step.action === "open_tab") {
    const url = step.url || step.value;
    if (!url) throw new Error("open_tab requires a url");
    const verdict = isHostAllowed(url, sitePolicy);
    if (!verdict.allowed) throw new Error(`Navigation blocked by site policy: ${verdict.reason}`);
    const newTab = await chrome.tabs.create({ url, active: true });
    // FR11: an agent-opened tab joins the Assistant group — the visibility
    // scope follows the run instead of leaking a loose ungrouped tab.
    const grouped = tabGroup ? await addTabToGroup(newTab.id, tabGroup) : false;
    await waitForTabComplete(newTab.id, step.timeout_ms || DEFAULT_STEP_TIMEOUT_MS, signal);
    await settleAfterNavigation(newTab.id, signal);
    return { newTabId: newTab.id, actual: `opened new tab: ${url}${grouped ? " (added to the Assistant group)" : ""}` };
  }

  // Resizes the whole browser WINDOW (outer dimensions), affecting every tab
  // in it — useful for responsive-design breakpoints. restore:true puts the
  // window back to whatever size it was before this run's first resize.
  if (step.action === "resize_window") {
    if (step.restore) {
      const orig = _originalWindowSize.get(tabId);
      if (!orig) throw new Error("resize_window: restore requested but no size was recorded yet for this tab — call resize_window with width/height first");
      const res = await sendSW({ type: "resizeWindow", tabId, width: orig.width, height: orig.height });
      if (!res?.ok) throw new Error(`resize_window restore failed: ${res?.error || "unknown error"}`);
    } else {
      const width = Number(step.width), height = Number(step.height);
      if (!Number.isFinite(width) || !Number.isFinite(height)) throw new Error("resize_window requires numeric width and height");
      if (!_originalWindowSize.has(tabId)) {
        const cur = await sendSW({ type: "getWindowSize", tabId }).catch(() => null);
        if (cur?.ok) _originalWindowSize.set(tabId, { width: cur.width, height: cur.height });
      }
      const res = await sendSW({ type: "resizeWindow", tabId, width, height });
      if (!res?.ok) throw new Error(`resize_window failed: ${res?.error || "unknown error"}`);
    }
    await new Promise((r) => setTimeout(r, 150)); // let the resize land before reading back
    const inner = await sendToContent(tabId, { type: "getViewportSize" }).catch(() => null);
    const actual = inner?.ok
      ? `window resized; viewport is now ${inner.width}x${inner.height}`
      : `resize_window ${step.restore ? "restore" : `${step.width}x${step.height}`} requested`;
    return { actual };
  }

  // Read a recently downloaded file. MV3 reality: extension pages cannot read
  // file:// bytes from disk, so this matches the download via downloads.search
  // and RE-FETCHES its source url (works for directly-linked files — cookies
  // and <all_urls> host permission apply). POST-generated blobs and expired
  // signed urls fall back to metadata-only so the agent can request_human.
  if (step.action === "read_download") {
    const match = String(step.value || step.filename || "").trim();
    const items = await chrome.downloads.search({ orderBy: ["-startTime"], limit: 10 });
    const item = match
      ? items.find(
          (d) =>
            (d.filename || "").toLowerCase().includes(match.toLowerCase()) ||
            (d.url || "").toLowerCase().includes(match.toLowerCase()),
        )
      : items[0];
    if (!item) {
      throw new Error(
        `read_download: no recent download${match ? ` matching "${match}"` : ""}. Recent: ` +
          (items.slice(0, 5).map((d) => (d.filename || "").split(/[\\/]/).pop()).filter(Boolean).join(", ") || "none"),
      );
    }
    const filename = (item.filename || "").split(/[\\/]/).pop() || "download";
    const sourceUrl = item.finalUrl || item.url;
    // F-05: route through the same host check as read_document, for consistency.
    if (!isUrlInScope(sourceUrl, sitePolicy)) {
      throw new Error(`read_download: "${filename}"'s source is blocked by your site policy or is a private/loopback address`);
    }
    const meta = { filename, mime: item.mime, fileSize: item.fileSize, state: item.state, url: sourceUrl };
    const textLike =
      /^(text\/|application\/(json|xml|csv|x-yaml))/.test(item.mime || "") ||
      /\.(txt|csv|tsv|json|xml|ya?ml|md|log|html?)$/i.test(filename);
    const pdfLike = /^application\/pdf/.test(item.mime || "") || /\.pdf$/i.test(filename);
    const metadataOnly = (why) => {
      const actual = `read_download: "${filename}" — ${why}; returning metadata only`;
      return step.variable ? { variable: step.variable, value: JSON.stringify(meta), actual } : { actual };
    };
    if (pdfLike) {
      // Downloaded PDFs go through the same pdf.js path as read_document.
      try {
        const resp = await fetch(sourceUrl, { signal });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const buf = await resp.arrayBuffer();
        if (buf.byteLength > 20 * 1024 * 1024) return metadataOnly("PDF is larger than 20 MB");
        const { header, text } = await readPdfData(new Uint8Array(buf), { pages: step.pages, maxChars: 1_000_000 });
        return {
          variable: step.variable || undefined,
          value: `${header}\n${text}`,
          actual: `read "${filename}" — ${header}`,
        };
      } catch (e) {
        if (e?.name === "AbortError") throw e;
        return metadataOnly(`could not read PDF (${e.message})`);
      }
    }
    if (!textLike) return metadataOnly(`${item.mime || "unknown type"} is not text-like`);
    try {
      const resp = await fetch(sourceUrl, { signal });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const text = (await resp.text()).slice(0, 1_000_000); // 1 MB cap
      return {
        variable: step.variable || undefined,
        value: text,
        actual: `read ${text.length} chars from "${filename}"`,
      };
    } catch (e) {
      if (e?.name === "AbortError") throw e;
      return metadataOnly(`could not re-fetch its source url (${e.message}) — POST-generated or expired links cannot be re-read`);
    }
  }

  // Read a document rendered in a tab (REQUIREMENTS-READ_DOCUMENTS): PDFs in the
  // browser's viewer (no DOM to snapshot) and canvas-rendered Google Docs/Sheets/
  // Slides. documents.js fetches the bytes/export with the user's cookies; the
  // result rides the observation channel like the other content reads.
  if (step.action === "read_document") {
    let url = String(step.url || step.value || "").trim();
    const currentTab = await chrome.tabs.get(tabId).catch(() => null);
    if (!url) url = currentTab?.url || "";
    // F-05: credentials only follow a document fetch that's same-origin as
    // the tab this run is attached to; sitePolicy + private-IP blocking are
    // enforced inside readDocument regardless of origin.
    let tabOrigin = null;
    try { if (currentTab?.url) tabOrigin = new URL(currentTab.url).origin; } catch {}
    const { header, text } = await readDocument({
      url,
      pages: step.pages,
      sheet: step.sheet,
      range: step.range,
      find: step.find,
      maxChars: step.max_chars,
      signal,
      sitePolicy,
      tabOrigin,
    });
    return {
      variable: step.variable || undefined,
      value: `${header}\n${text}`,
      actual: header,
    };
  }

  if (step.action === "screenshot") {
    const dataUrl = await captureScreenshot(tabId);
    _pushScreenshotArtifact(tabId, dataUrl); // REQ-11: addressable as "screenshot:last"
    return { screenshot: dataUrl };
  }

  if (step.action === "screenshot_baseline") {
    const dataUrl = await captureScreenshot(tabId);
    const name = step.value || step.name || "default";
    await chrome.storage.local.set({ [`__baseline_${name}`]: dataUrl });
    return { screenshot: dataUrl };
  }

  if (step.action === "assert_screenshot") {
    const dataUrl = await captureScreenshot(tabId);
    const name = step.baseline || step.value || "default";
    const stored = await chrome.storage.local.get([`__baseline_${name}`]);
    const baseline = stored[`__baseline_${name}`];
    if (!baseline) {
      throw new Error(`No baseline stored as "${name}". Run screenshot_baseline first.`);
    }
    const { passed, diffRatio } = await compareScreenshots(baseline, dataUrl, step.threshold ?? 0.01);
    return {
      screenshot: dataUrl,
      passed,
      actual: `${(diffRatio * 100).toFixed(2)}% diff`,
      reason: passed
        ? undefined
        : `Screenshot differs by ${(diffRatio * 100).toFixed(2)}%, threshold ${((step.threshold ?? 0.01) * 100).toFixed(2)}%`,
    };
  }

  // Region screenshot: crop (and upscale) a small area for close inspection —
  // an icon or dense table cell — without re-sending the whole page image.
  // Resolution happens in content.js (rect + DPR); the crop itself must happen
  // in background.js, since only it can call chrome.tabs.captureVisibleTab.
  if (step.action === "zoom") {
    let rect, dpr = 1;
    if (step.target || step.ref != null) {
      const target = step.target || `ref=${step.ref}`;
      const res = await sendToContent(tabId, { type: "execAction", step: { action: "zoom_region", target, padding: step.padding } });
      if (!res?.ok || !res.result?.rect) throw new Error(`zoom: could not resolve region for "${target}"`);
      rect = res.result.rect;
      dpr = res.result.dpr || 1;
    } else if (step.x0 != null && step.y0 != null && step.x1 != null && step.y1 != null) {
      const x0 = Number(step.x0), y0 = Number(step.y0), x1 = Number(step.x1), y1 = Number(step.y1);
      rect = { x: Math.round(Math.min(x0, x1)), y: Math.round(Math.min(y0, y1)), width: Math.round(Math.abs(x1 - x0)), height: Math.round(Math.abs(y1 - y0)) };
      const vp = await sendToContent(tabId, { type: "getViewportSize" }).catch(() => null);
      dpr = vp?.dpr || 1;
    } else {
      throw new Error("zoom requires target/ref, or x0,y0,x1,y1");
    }
    if (!rect.width || !rect.height) throw new Error("zoom: resolved region has zero size");
    const capture = await sendSW({ type: "captureZoom", tabId, region: rect, dpr, upscale: step.upscale });
    if (!capture?.ok) {
      if (capture?.reason === "not_visible") return { actual: "zoom unavailable (tab not visible)" };
      throw new Error(`zoom failed: ${capture?.error || capture?.reason || "unknown error"}`);
    }
    _pushScreenshotArtifact(tabId, capture.dataUrl); // REQ-11: addressable as "screenshot:last"
    return { screenshot: capture.dataUrl, actual: `zoomed region ${rect.width}x${rect.height} → ${capture.width}x${capture.height}` };
  }

  // Tab-context primitive: structured listing of every tab in the current
  // window, stored in a run variable and fed back as an observation so the
  // agent can reason about the tab landscape instead of remembering it.
  if (step.action === "list_tabs") {
    // Scoped run: list only the Assistant group's members (queried live, so a
    // tab the user just dragged out is already gone from this listing).
    const res = await sendSW({ type: "listTabs", ...(tabGroup ? { groupId: tabGroup.id } : {}) });
    if (!res?.ok) throw new Error(`list_tabs failed: ${res?.error || "no response from background"}`);
    return {
      variable: step.variable || "tabs",
      value: JSON.stringify(res.tabs) + (tabGroup
        ? "\n(note: this run is scoped to the user's Assistant tab group — only these tabs are visible/usable; read_tab reads one without switching)"
        : ""),
      actual: `${res.tabs.length} open tab(s)${tabGroup ? " in the Assistant group" : ""}`,
    };
  }

  if (step.action === "switch_tab") {
    // Scoped run: only the Assistant group's tabs are switch targets (FR4) —
    // the candidate list itself is filtered so a substring can never match an
    // out-of-scope tab.
    const tabs = tabGroup ? await chrome.tabs.query({ groupId: tabGroup.id }) : await chrome.tabs.query({});
    let match = null;
    // Preferred: a tab id as returned by list_tabs (tab_id field, or a purely
    // numeric value). Falls back to the classic title/URL substring match.
    const wantedId = Number(step.tab_id ?? (/^\d+$/.test(String(step.value ?? "")) ? step.value : NaN));
    if (Number.isInteger(wantedId) && wantedId >= 0) {
      match = tabs.find((t) => t.id === wantedId);
    }
    const target = step.value;
    if (!match && target != null && target !== "") {
      match =
        tabs.find((t) => String(t.title) === String(target)) ||
        tabs.find((t) => t.url && t.url.includes(target)) ||
        tabs.find((t) => t.title && t.title.toLowerCase().includes(String(target).toLowerCase()));
    }
    if (!match) {
      throw new Error(
        `No tab matching ${step.tab_id ?? target}` +
        (tabGroup ? ` in the Assistant tab group — this run can only switch between the user's grouped tabs (call list_tabs to see them); ask the user to add the tab to the group if it's needed` : ""),
      );
    }
    await chrome.tabs.update(match.id, { active: true });
    // Tell the caller to retarget subsequent steps at the tab we switched to —
    // otherwise the run keeps acting on the original tab.
    return { newTabId: match.id, actual: `switched to tab ${match.id}: ${(match.title || match.url || "").slice(0, 80)}` };
  }

  // REQ-19: close a tab the run opened. Group-scoped runs may only close tabs
  // INSIDE the Assistant group (FR19.2); the guards below refuse to strand the
  // run by closing its active tab or the last grouped tab (FR19.3).
  if (step.action === "close_tab") {
    // Candidate set queried live (never a cached list) so it can't desync from
    // the real tab-group state.
    const tabs = tabGroup ? await chrome.tabs.query({ groupId: tabGroup.id }) : await chrome.tabs.query({ currentWindow: true });
    const wantedId = Number(step.tab_id ?? (/^\d+$/.test(String(step.value ?? "")) ? step.value : NaN));
    let match = null;
    if (Number.isInteger(wantedId) && wantedId >= 0) match = tabs.find((t) => t.id === wantedId);
    const target = step.value;
    if (!match && target != null && target !== "") {
      match =
        tabs.find((t) => String(t.title) === String(target)) ||
        tabs.find((t) => t.url && t.url.includes(target)) ||
        tabs.find((t) => t.title && t.title.toLowerCase().includes(String(target).toLowerCase()));
    }
    if (!match) {
      throw new Error(
        `close_tab: no tab matching ${step.tab_id ?? target}` +
        (tabGroup ? ` in the Assistant tab group — this run can only close the user's grouped tabs (call list_tabs to see them)` : " (call list_tabs to see open tabs)"),
      );
    }
    if (match.id === tabId) throw new Error("close_tab: refusing to close the tab the run is operating on — that would strand the run; switch_tab away first, or let it stay open");
    if (tabGroup && tabs.length <= 1) throw new Error("close_tab: refusing to close the last tab in the Assistant group — the run needs at least one grouped tab");

    // FR19.4: surface (don't block on) unsaved input we can detect. Best-effort
    // — restricted pages and unloaded tabs simply report nothing.
    let unsavedNote = "";
    try {
      const [{ result: dirty } = {}] = await chrome.scripting.executeScript({
        target: { tabId: match.id },
        func: () => {
          const fields = [...document.querySelectorAll("input, textarea, [contenteditable=true], [contenteditable='']")];
          return fields.some((el) => {
            if (el.isContentEditable) return (el.textContent || "").trim().length > 0;
            const type = (el.type || "").toLowerCase();
            if (["button", "submit", "reset", "hidden", "checkbox", "radio", "file"].includes(type)) return false;
            return (el.value || "").trim().length > 0;
          });
        },
      });
      if (dirty) unsavedNote = " (note: this tab had unsaved form input)";
    } catch { /* restricted/unloaded tab — no detection */ }

    const closedTitle = (match.title || match.url || "").slice(0, 80);
    const res = await sendSW({ type: "removeTab", tabId: match.id });
    if (!res?.ok) throw new Error(`close_tab failed: ${res?.error || "no response from background"}`);
    return { actual: `closed tab ${match.id}: ${closedTitle}${unsavedNote}` };
  }

  // Cross-tab read: pull another tab's text content WITHOUT switching to it,
  // so "compare these 3 tabs" is a few read_tab calls in one turn instead of a
  // switch/read/switch-back dance. Document tabs (PDF viewer, Google/Office
  // editors — no readable DOM text) route through readDocument, like summarize.
  if (step.action === "read_tab") {
    const wantedId = Number(step.tab_id ?? step.value);
    if (!Number.isInteger(wantedId) || wantedId < 0) throw new Error("read_tab requires a numeric tab_id — call list_tabs first");
    const tab = await chrome.tabs.get(wantedId).catch(() => null);
    if (!tab || (tabGroup && tab.groupId !== tabGroup.id)) {
      throw new Error(
        tabGroup
          ? `read_tab: tab ${wantedId} is not in the Assistant tab group — this run can only read the user's grouped tabs (call list_tabs to see them)`
          : `read_tab: no tab with id ${wantedId} (call list_tabs to see open tabs)`,
      );
    }
    if (isRestrictedUrl(tab.url)) throw new Error(`read_tab: tab ${wantedId} is a browser-internal page with no readable content`);
    const maxChars = Number(step.max_chars) > 0 ? Number(step.max_chars) : 20000;
    const doc = detectDocumentUrl(tab.url);
    let text;
    if (doc && doc.kind !== "gmail-attachment") {
      let readTabOrigin = null;
      try { readTabOrigin = new URL(tab.url).origin; } catch {}
      const { header, text: docText } = await readDocument({ url: tab.url, maxChars, signal, sitePolicy, tabOrigin: readTabOrigin });
      text = `${header}\n${docText}`;
    } else {
      const res = await sendToContent(tab.id, { type: "execAction", step: { action: "get_page_text", max_chars: maxChars } })
        .catch((e) => { throw new Error(`read_tab: could not read tab ${tab.id} (${e.message}) — the tab may not be loaded yet; switch_tab to it and read there instead`); });
      if (!res?.ok) throw new Error(`read_tab: ${res?.error || "content script error"}`);
      text = res.result?.value || "";
    }
    return {
      variable: step.variable || undefined,
      value: `Tab ${tab.id}: ${tab.title || ""}\nURL: ${tab.url}\n\n${text}`,
      actual: `read tab ${tab.id} (${(tab.title || tab.url || "").slice(0, 60)}): ${text.length} chars`,
    };
  }

  // Agent-callable network observability: merges the in-page fetch wrapper's
  // buffer (content.js) with the background webRequest slot (browser-level
  // failures the page wrapper can't see: CORS, DNS, blocked loads).
  if (step.action === "read_network_requests") {
    const limit = Number(step.limit) > 0 ? Number(step.limit) : 20;
    const clear = step.clear === true;
    const contentRes = await sendToContent(tabId, { type: "getQANetworkEvents", clear }).catch(() => null);
    const bgRes = await sendSW({ type: "getWebRequestCapture", tabId, clear }).catch(() => null);
    const entries = [];
    for (const e of contentRes?.events || []) {
      entries.push({ method: e.method || "GET", url: e.url || "", status: e.status ?? null, duration_ms: e.durationMs ?? null, error: e.error || null });
    }
    for (const e of bgRes?.errors || []) {
      entries.push({ method: e.method || "GET", url: e.url || "", status: e.statusCode ?? null, duration_ms: null, error: e.error || null });
    }
    // The same failed fetch can appear in both buffers — keep the first.
    const seen = new Set();
    let out = entries.filter((e) => {
      const key = `${e.method} ${e.url} ${e.status ?? e.error ?? ""}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    const match = buildRunnerTextFilter(step.filter);
    if (match) out = out.filter((e) => match(e.url));
    const statusFilter = String(step.status || "").toLowerCase();
    if (statusFilter) {
      out = out.filter((e) => {
        if (statusFilter === "error") return e.error != null || (e.status != null && e.status >= 400);
        if (statusFilter === "4xx") return e.status != null && e.status >= 400 && e.status < 500;
        if (statusFilter === "5xx") return e.status != null && e.status >= 500;
        const code = Number(statusFilter);
        return Number.isFinite(code) && e.status === code;
      });
    }
    out = out.slice(-limit);
    return {
      variable: step.variable || undefined,
      value: out.length ? JSON.stringify(out) : "no matching network requests captured",
      actual: `${out.length} network request(s)`,
    };
  }

  // find: the local (LLM-free) scoring pass runs in content.js; when it isn't
  // confident, one LLM call picks candidates from the compact snapshot and the
  // answers are merged ahead of the local guesses.
  if (step.action === "find") {
    const res = await sendToContent(tabId, { type: "execAction", step });
    if (!res?.ok) throw new Error(res?.error || "content script error");
    const result = res.result || {};
    if (!result.confident && llmConfig) {
      const snapshot = await snapshotPage(tabId);
      const refs = await findElementsLLM({ llmConfig, query: step.query || step.value, snapshot, signal, onUsage })
        .catch((e) => { if (e?.name === "AbortError") throw e; return []; });
      if (refs.length) {
        const byRef = new Map((snapshot.interactive || []).map((el) => [el.ref, el]));
        const llmMatches = refs
          .filter((r) => byRef.has(r))
          .map((r) => {
            const el = byRef.get(r);
            return { ref: r, role: el.role || undefined, name: el.name || undefined, selector: `ref=${r}`, source: "llm" };
          });
        if (llmMatches.length) {
          const matches = [...llmMatches, ...(result.matches || []).filter((m) => !refs.includes(m.ref))];
          const summary = matches
            .map((m) => `[${m.ref}] ${m.role || ""} ${JSON.stringify(m.name || m.snippet || "")} → ${m.selector}`)
            .join("\n");
          return { ...result, matches, value: summary, actual: `find (LLM-assisted): ${matches.length} match(es)` };
        }
      }
    }
    return result;
  }

  if (step.action === "request_human") {
    if (onHumanGate) {
      const decision = await onHumanGate({
        reason: step.value || step.response || "Approval required",
        step,
      });
      if (decision === "cancel") {
        const err = new Error("Run cancelled by user at approval gate.");
        err.userCancelled = true;
        throw err;
      }
    }
    return {};
  }

  if (step.action === "summarize") {
    // The extracted value starts with a "URL: …\nTitle: …" meta block; only the
    // body after it counts as content. An empty body right after a navigation
    // usually means a client-side-rendered page that hasn't painted yet — wait
    // once and re-read before giving up, and never send content-free text to
    // the LLM (it answers with a confusing "there is no content" non-summary).
    const readText = async () => {
      const textRes = await sendToContent(tabId, { type: "execAction", step });
      if (!textRes?.ok) throw new Error(textRes?.error || "content script error");
      return textRes.result?.value || "";
    };
    const bodyOf = (t) => t.replace(/^URL: [^\n]*\nTitle: [^\n]*\n\n?/, "").trim();
    // On a document tab (PDF viewer / Google editor) the page's own text is
    // empty or just app chrome — summarize the document contents instead, so
    // plan-once flows like "navigate → summarize" work on PDFs unchanged.
    const sumTab = await chrome.tabs.get(tabId).catch(() => null);
    const sumDoc = detectDocumentUrl(sumTab?.url);
    if (sumDoc && sumDoc.kind !== "gmail-attachment" && !step.target) {
      let sumTabOrigin = null;
      try { sumTabOrigin = new URL(sumTab.url).origin; } catch {}
      const { header, text } = await readDocument({ url: sumTab.url, maxChars: 15000, signal, sitePolicy, tabOrigin: sumTabOrigin });
      const docText = `URL: ${sumTab.url}\nTitle: ${sumTab.title || ""}\n\n${header}\n${text}`;
      let instruction = step.instruction;
      let varName = step.value || step.variable;
      if (!instruction && varName && !/^[\w.-]{1,48}$/.test(varName)) {
        instruction = varName;
        varName = undefined;
      }
      const summary = await summarizeContent({ llmConfig, text: docText, instruction: instruction || undefined, signal, onUsage });
      return { variable: varName, value: summary, actual: summary };
    }
    let rawText = await readText();
    if (bodyOf(rawText).length < 40) {
      await new Promise((r) => setTimeout(r, 1200));
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      rawText = await readText();
    }
    if (bodyOf(rawText).length < 40) {
      throw new Error("summarize: the page has no readable text yet (still loading, or content is inside an iframe) — wait and retry");
    }
    // step.instruction carries the user's full request when the planner set it;
    // otherwise summarizeContent falls back to its generic summary prompt.
    // Some models put the task text in "value" (meant to be a variable name) —
    // reclaim it as the instruction so it drives the analysis instead of
    // leaking into the result card as a bogus variable/heading.
    let instruction = step.instruction;
    let varName = step.value || step.variable;
    if (!instruction && varName && !/^[\w.-]{1,48}$/.test(varName)) {
      instruction = varName;
      varName = undefined;
    }
    const summary = await summarizeContent({ llmConfig, text: rawText, instruction: instruction || undefined, signal, onUsage });
    return { variable: varName, value: summary, actual: summary };
  }

  // exec_script: run JS in the page's main world with REPL semantics — top-level
  // await works and the last expression's value is returned (multi-statement
  // scripts use `return`). Uses a blob: URL to load the script so pages with
  // strict CSP (no unsafe-eval, no unsafe-inline) work; blobs created in MAIN
  // world inherit the page's origin, matching 'self'. A blob that fails to
  // PARSE (statements passed to the expression wrapper) surfaces as a window
  // "error" event with the blob's URL as filename — that triggers the fallback
  // to the statement form. Results are serialized in-page: DOM nodes become
  // { node, text } summaries, output is capped at 4 KB, and a thrown page
  // exception comes back as structured { error, stack } WITHOUT failing the
  // step, so the agent loop reads the error and continues.
  if (step.action === "exec_script") {
    if (!allowExecScript) {
      throw new Error("exec_script is disabled — enable Developer mode in Settings to allow script-execution steps.");
    }
    const script = String(step.value || step.script || "");
    if (!script) throw new Error("exec_script requires a value or script field");
    const scriptFrameId = _frameByTab.get(tabId) || 0;
    const results = await chrome.scripting.executeScript({
      target: scriptFrameId ? { tabId, frameIds: [scriptFrameId] } : { tabId },
      world: "MAIN",
      func: (s) => new Promise((resolve) => {
        const MAX_RESULT = 4096;
        const summarizeNode = (n) => {
          const cls = typeof n.className === "string" && n.className
            ? "." + n.className.trim().split(/\s+/).slice(0, 3).join(".")
            : "";
          return {
            node: `<${(n.tagName || n.nodeName || "node").toLowerCase()}${n.id ? "#" + n.id : ""}${cls}>`,
            text: (n.textContent || "").trim().slice(0, 120),
          };
        };
        const serialize = (v) => {
          try {
            if (v === undefined) return '"undefined"';
            if (typeof Node !== "undefined" && v instanceof Node) v = summarizeNode(v);
            const json = JSON.stringify(v, (_, val) =>
              typeof Node !== "undefined" && val instanceof Node ? summarizeNode(val)
              : typeof val === "function" ? `[function ${val.name || "anonymous"}]`
              : typeof val === "bigint" ? String(val)
              : val);
            const out = json === undefined ? JSON.stringify(String(v)) : json;
            return out.length > MAX_RESULT ? JSON.stringify(out.slice(0, MAX_RESULT) + "…(truncated at 4 KB)") : out;
          } catch (e) {
            return JSON.stringify({ error: "unserializable result: " + (e.message || e) });
          }
        };
        window.__escSerialize = serialize;

        let settled = false;
        const finish = (payload) => {
          if (settled) return;
          settled = true;
          try { delete window.__escSerialize; } catch {}
          resolve(payload);
        };
        const timeout = setTimeout(() => finish(JSON.stringify({ error: "exec_script timed out after 15s" })), 15000);

        // Two wrapper forms: expression first (last expression auto-returned),
        // statement body as the parse-error fallback.
        const exprForm = (id) =>
          `(async()=>{const __id=${JSON.stringify(id)};const __s=window.__escSerialize||JSON.stringify;` +
          `try{const v=await (${s}\n);document.dispatchEvent(new CustomEvent('__escResult',{detail:{__id,payload:__s(v)}}))}` +
          `catch(e){document.dispatchEvent(new CustomEvent('__escResult',{detail:{__id,payload:JSON.stringify({error:String(e&&e.message||e),stack:String(e&&e.stack||'').slice(0,600)})}}))}})()`;
        const stmtForm = (id) =>
          `(async()=>{const __id=${JSON.stringify(id)};const __s=window.__escSerialize||JSON.stringify;` +
          `try{const v=await (async()=>{${s}\n})();document.dispatchEvent(new CustomEvent('__escResult',{detail:{__id,payload:__s(v)}}))}` +
          `catch(e){document.dispatchEvent(new CustomEvent('__escResult',{detail:{__id,payload:JSON.stringify({error:String(e&&e.message||e),stack:String(e&&e.stack||'').slice(0,600)})}}))}})()`;

        let attempt = 0;
        const run = (form) => {
          const id = "__esc_" + Math.random().toString(36).slice(2);
          const url = URL.createObjectURL(new Blob([form(id)], { type: "application/javascript" }));
          const el = document.createElement("script");
          const cleanup = () => {
            document.removeEventListener("__escResult", onResult);
            window.removeEventListener("error", onParseError, true);
            URL.revokeObjectURL(url);
            el.remove();
          };
          const onResult = (e) => {
            if (e.detail.__id !== id) return;
            cleanup();
            clearTimeout(timeout);
            finish(e.detail.payload);
          };
          const onParseError = (ev) => {
            if (ev.filename !== url) return;
            cleanup();
            if (attempt === 0) { attempt = 1; run(stmtForm); }
            else {
              clearTimeout(timeout);
              finish(JSON.stringify({ error: "SyntaxError: " + (ev.message || "script failed to parse") }));
            }
          };
          document.addEventListener("__escResult", onResult);
          window.addEventListener("error", onParseError, true);
          el.src = url;
          el.onerror = () => {
            cleanup();
            clearTimeout(timeout);
            finish(JSON.stringify({ error: "exec_script: blob script failed to load" }));
          };
          document.documentElement.appendChild(el);
        };
        run(exprForm);
      }),
      args: [script],
    });
    const payload = results?.[0]?.result;
    let value;
    try { value = JSON.parse(payload); } catch { value = payload ?? null; }
    const display = typeof value === "string" ? value : JSON.stringify(value);
    const out = {
      value,
      actual: String(display).slice(0, 200),
      note: "Executed via CSP-bypassing blob: injection in the page's MAIN world.",
    };
    if (step.variable) out.variable = step.variable;
    return out;
  }

  // switch_frame — orchestrated here because cross-origin frames need extension
  // APIs. Same-origin iframes keep the old fast path (content.js adopts the
  // iframe's document). When that fails cross-origin, the frame is located via
  // webNavigation, content.js is injected into it, and all subsequent messages
  // for this tab are routed to that frameId.
  if (step.action === "switch_frame") {
    const target = String(step.target || step.value || "");
    if (!target || target === "top" || target === "main") {
      _frameByTab.delete(tabId);
      await sendToContent(tabId, { type: "resetFrame" }).catch(() => {});
      return { actual: "top frame" };
    }
    // Already in a subframe? Frame targets are resolved from the top.
    _frameByTab.delete(tabId);
    const sameOrigin = await sendToContent(tabId, { type: "execAction", step }).catch((e) => ({ ok: false, error: e.message }));
    if (sameOrigin?.ok) return sameOrigin.result || {};
    const frames = await chrome.webNavigation.getAllFrames({ tabId }).catch(() => null);
    const subframes = (frames || []).filter((f) => f.frameId !== 0 && !String(f.url).startsWith("about:"));
    const idxMatch = target.match(/^frame=(\d+)$/);
    const frame = idxMatch
      ? subframes[Number(idxMatch[1])]
      : subframes.find((f) => (f.url || "").includes(target));
    if (!frame) {
      throw new Error(
        `switch_frame: no frame matching "${target}" (same-origin lookup: ${sameOrigin?.error || "no match"}). ` +
          `Frames on this page: ${subframes.map((f, i) => `frame=${i} ${String(f.url).slice(0, 80)}`).join(" ; ") || "none"}`,
      );
    }
    const inject = await sendSW({ type: "ensureContentScript", tabId, frameId: frame.frameId });
    if (!inject?.ok) throw new Error(`switch_frame: could not inject into frame (${inject?.error || "unknown error"})`);
    _frameByTab.set(tabId, frame.frameId);
    await sendToContent(tabId, { type: "resetFrame" }).catch(() => {});
    return { actual: `switched to cross-origin frame: ${String(frame.url).slice(0, 100)}` };
  }

  // Coordinate actions are meaningless inside a subframe: the screenshot the
  // model saw is tab-global while elementFromPoint is frame-local. drop_file
  // only hits this when it falls back to coordinates (no target given).
  const usesCoordinates =
    step.action === "click_at" || step.action === "hover_at" || step.action === "right_click_at" ||
    (step.action === "drop_file" && step.target == null);
  if (usesCoordinates && _frameByTab.get(tabId)) {
    throw new Error(`${step.action} is unavailable while inside an iframe — switch_frame to "top" first or use a selector.`);
  }

  // Fill a form from structured data: one LLM call maps DATA keys onto the
  // page's form fields (by their [N] ref indexes), then each mapping executes
  // through the normal action dispatch. PII in the data is masked before it
  // enters the prompt — the model returns the data KEY per field and the real
  // value is substituted locally.
  if (step.action === "autofill") {
    let data = step.value;
    if (typeof data === "string") {
      try { data = JSON.parse(data); } catch { throw new Error("autofill: value must be a JSON object of field data"); }
    }
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      throw new Error("autofill: value must be a JSON object of field data");
    }
    const snapshot = await snapshotPage(tabId);
    const formFields = (snapshot.interactive || []).filter((el) =>
      ["input", "select", "textarea"].includes(el.tag),
    );
    if (!formFields.length) throw new Error("autofill: no form fields found on the page");
    const maskedData = {};
    for (const k of Object.keys(data)) maskedData[k] = maskPII(String(data[k]));
    const mappings = await mapAutofillFields({ llmConfig, data: maskedData, fields: formFields, signal, onUsage });
    if (!mappings.length) throw new Error("autofill: the model could not map any data keys to form fields");
    let filled = 0;
    const details = [];
    for (const m of mappings) {
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      const action = ["type", "select", "check"].includes(m.action) ? m.action : "type";
      const rawValue = m.key != null && data[m.key] != null ? String(data[m.key]) : m.value;
      const sub = { action, target: `ref=${m.ref}`, value: rawValue, timeout_ms: step.timeout_ms || 8000 };
      try {
        const res = await sendToContent(tabId, { type: "execAction", step: sub });
        if (res?.ok) { filled++; details.push(`${m.key || `ref=${m.ref}`} ✓`); }
        else details.push(`${m.key || `ref=${m.ref}`} ✗ (${String(res?.error || "").slice(0, 60)})`);
      } catch (e) {
        details.push(`${m.key || `ref=${m.ref}`} ✗ (${String(e.message).slice(0, 60)})`);
      }
    }
    return { actual: `autofill: filled ${filled}/${mappings.length} field(s) — ${details.join(", ")}` };
  }

  // DOM actions — delegate to content script with self-healing selector retry.
  let currentStep = step;
  let lastErr;
  let lastResult = null;      // last ok-but-failed assert result (for graceful return)
  let healInfo = null;        // { from, to } when a heal was applied — surfaced to the UI
  let cheapRetried = false;   // a one-time free wait+retry of the same selector
  const originalTarget = step.target;

  // Site memory: a fix learned on a previous run for this same target rides
  // along as an extra ladder rung from the FIRST dispatch — a still-broken
  // original selector then recovers with zero LLM calls. The original stays
  // first so a reverted site wins immediately.
  let memOrigin = null;
  let rememberedSel = null;
  if (HEALABLE_ACTIONS.has(step.action) && step.target) {
    try {
      const tab = await chrome.tabs.get(tabId);
      memOrigin = originOf(tab?.url);
      const remembered = memOrigin ? await lookupHeal(memOrigin, step.target) : null;
      if (remembered) {
        const existing = Array.isArray(step.target) ? step.target : [step.target];
        if (!existing.includes(remembered.to)) {
          rememberedSel = remembered.to;
          currentStep = { ...step, target: [...existing, remembered.to] };
        }
      }
    } catch { /* tab lookup failed — dispatch below will surface the real error */ }
  }

  for (let attempt = 0; attempt <= MAX_HEAL_RETRIES; attempt++) {
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    const response = await sendToContent(tabId, { type: "execAction", step: currentStep });

    if (response?.ok) {
      const result = response.result || {};
      // An assert that "passed:false" because its element wasn't found is a heal
      // candidate (selector, not value, is wrong). Every other ok result is final —
      // including genuine value mismatches, which must fail rather than heal.
      if (!assertMissedElement(step, result)) {
        // Record the ladder rung that actually matched (not just the first
        // candidate we appended) so heals persist the selector that worked.
        if (healInfo && result.matched_selector) healInfo = { ...healInfo, to: result.matched_selector };
        // A remembered rung that matched counts as a heal for the UI (memory:
        // true = no LLM call was spent) and refreshes its hit count.
        if (!healInfo && rememberedSel && result.matched_selector === rememberedSel) {
          healInfo = { from: originalTarget, to: rememberedSel, memory: true };
        }
        if (memOrigin && healInfo) recordHeal(memOrigin, originalTarget, healInfo.to).catch(() => {});
        return healInfo ? { ...result, healed: healInfo } : result;
      }
      lastResult = result;
      lastErr = new Error(result.reason || "assert: element not found");
    } else {
      lastResult = null;
      lastErr = new Error(response?.error || "content script error");
    }

    if (!HEALABLE_ACTIONS.has(step.action) || !step.target) break;
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");

    // Cheap retry first: many misses are just timing (element not rendered yet). A
    // short wait + re-resolve of the SAME selector fixes those for free, before
    // spending an LLM call. It doesn't count against the heal-attempt budget.
    if (!cheapRetried) {
      cheapRetried = true;
      await new Promise((r) => setTimeout(r, CHEAP_RETRY_DELAY_MS));
      attempt--;
      continue;
    }

    if (attempt < MAX_HEAL_RETRIES) {
      try {
        onProgress?.(`Selector failed — asking LLM to heal (attempt ${attempt + 1})…`);
        const snapshot = await snapshotPage(tabId);
        const healed = await healSelector({ llmConfig, step: currentStep, error: lastErr.message, snapshot, signal, onUsage });
        const candidates = healed?.targets ? [...healed.targets] : [];

        // Text healing is about to run out — one vision call to locate the
        // element on screen. A numbered-label hit becomes a ref= ladder rung;
        // bare coordinates convert click-type steps to click_at as a last resort.
        // (snapshotPage above refreshed the content script's ref registry, so the
        // SoM labels and any ref answer are consistent with it.)
        if (attempt === MAX_HEAL_RETRIES - 1 && vision && vision !== "off") {
          const screenshot = await captureSoMScreenshot(tabId);
          if (screenshot) {
            onProgress?.("Trying visual fallback — locating the element on the screenshot…");
            const found = await identifyElementVisually({
              llmConfig, step: currentStep, error: lastErr.message, snapshot, screenshot, signal, onUsage,
            }).catch((e) => { if (e?.name === "AbortError") throw e; return null; });
            if (found?.ref) {
              candidates.push(`ref=${found.ref}`);
            } else if (found && (step.action === "click" || step.action === "dblclick")) {
              currentStep = { action: "click_at", x: found.x, y: found.y, timeout_ms: currentStep.timeout_ms };
              healInfo = { from: originalTarget, to: `click_at(${found.x},${found.y})` };
              continue;
            }
          }
        }

        if (candidates.length) {
          // Append the healed selectors to the existing ladder (don't replace) so
          // the original candidates remain on subsequent retries, and persist the
          // heal into the source step so a downloaded test reflects it on next run.
          const existing = Array.isArray(currentStep.target)
            ? currentStep.target
            : currentStep.target != null ? [currentStep.target] : [];
          const fresh = candidates.filter((c) => !existing.includes(c));
          if (fresh.length) {
            const newLadder = [...existing, ...fresh];
            currentStep = { ...currentStep, target: newLadder };
            step.target = newLadder;
            healInfo = { from: originalTarget, to: fresh[0] };
            continue;
          }
        }
      } catch (e) {
        if (e?.name === "AbortError") throw e;
        // healing unavailable — fall through to break
      }
    }
    break;
  }
  // Heal exhausted. For an assert, return its (failed) result so the caller records a
  // normal assertion failure with actual/reason; otherwise throw the selector error.
  if (lastResult !== null) return healInfo ? { ...lastResult, healed: healInfo } : lastResult;
  throw lastErr;
}

// ---------- screenshot helpers ----------

// Screenshot the run's OWN tab. captureVisibleTab can only grab the active tab
// of a window, so the background gates on visibility: a backgrounded run gets
// null (→ text-only) instead of another page's screenshot.
// format "jpeg" is for LLM-bound captures (the background also downscales
// retina output) — a fraction of the PNG payload per vision turn. Artifact
// captures (screenshot action, baselines, GIF frames) stay PNG: assert_screenshot's
// pixel diff must not gain JPEG noise, and uploads should be lossless.
async function captureScreenshot(tabId, { format } = {}) {
  const res = await sendSW({ type: "captureVisibleTab", tabId, ...(format ? { format } : {}) });
  if (!res?.ok) return null;
  return res.dataUrl;
}

// Screenshot with Set-of-Marks labels: numbers every element of the page's last
// snapshot registry on-page, captures, then removes the labels. Requires a fresh
// snapshot to have been taken first (that's what fills the registry). Returns
// null when the tab isn't visible, same as captureScreenshot. Always JPEG —
// SoM screenshots exist only to be sent to the model.
async function captureSoMScreenshot(tabId) {
  // Inside a subframe the labels' rects are frame-local while the screenshot is
  // tab-global — numbered labels would land in the wrong place. Plain capture.
  if (_frameByTab.get(tabId)) return captureScreenshot(tabId, { format: "jpeg" });
  try {
    await sendToContent(tabId, { type: "showSoM" });
    return await captureScreenshot(tabId, { format: "jpeg" });
  } catch {
    return null;
  } finally {
    await sendToContent(tabId, { type: "hideSoM" }).catch(() => {});
  }
}

async function captureFailureScreenshot(tabId) {
  try {
    // JPEG: failure screenshots are shown in the panel and kept per step —
    // lossy is fine there, and it keeps long-run memory bounded.
    return await captureScreenshot(tabId, { format: "jpeg" });
  } catch {
    return null;
  }
}

async function startQACapture(tabId) {
  try { await sendToContent(tabId, { type: "startQACapture" }); } catch {}
}

async function collectQACapture(tabId) {
  try {
    await sendToContent(tabId, { type: "stopQACapture" });
    const res = await sendToContent(tabId, { type: "getQACapture" });
    return res?.ok ? res.capture : null;
  } catch { return null; }
}

// Pixel-diff two capture data URLs. Uses createImageBitmap + OffscreenCanvas
// rather than <img>/<canvas> so it works unchanged in the side panel AND in the
// service worker, which has no DOM — this was the last DOM dependency in lib/.
async function decodeDataUrl(dataUrl) {
  const blob = await (await fetch(dataUrl)).blob();
  return createImageBitmap(blob);
}

async function compareScreenshots(baselineDataUrl, currentDataUrl, threshold = 0.01) {
  let img1, img2;
  try {
    [img1, img2] = await Promise.all([decodeDataUrl(baselineDataUrl), decodeDataUrl(currentDataUrl)]);
  } catch (e) {
    // An undecodable baseline/capture is a failed comparison, not a hung run
    // (the old <img> version simply never resolved).
    throw new Error(`screenshot comparison could not decode an image: ${e?.message || e}`);
  }
  const width = Math.max(img1.width, img2.width);
  const height = Math.max(img1.height, img2.height);
  const canvas = new OffscreenCanvas(width, height);
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(img1, 0, 0);
  const d1 = ctx.getImageData(0, 0, width, height).data;
  ctx.clearRect(0, 0, width, height);
  ctx.drawImage(img2, 0, 0);
  const d2 = ctx.getImageData(0, 0, width, height).data;
  img1.close?.();
  img2.close?.();
  let diffPixels = 0;
  const total = d1.length / 4;
  const maxDiffPixels = Math.ceil(threshold * total);
  for (let i = 0; i < d1.length; i += 4) {
    if (
      Math.abs(d1[i] - d2[i]) > 10 ||
      Math.abs(d1[i + 1] - d2[i + 1]) > 10 ||
      Math.abs(d1[i + 2] - d2[i + 2]) > 10
    ) {
      if (++diffPixels > maxDiffPixels) break;
    }
  }
  return { passed: diffPixels / total <= threshold, diffRatio: diffPixels / total };
}

// ---------- content script comms ----------

// Which frame each tab's messages are routed to (0 / absent = top frame).
// Set by switch_frame when it targets a cross-origin iframe: content.js is
// injected into that frame on demand and chrome.tabs.sendMessage delivers
// straight to it via { frameId } — no background relay needed. Reset on any
// navigation and at run start.
const _frameByTab = new Map();

// Outcome of the post-navigation DOM-settle wait, per tab. Consumed (once) by
// snapshotPageReady so the first perceive after an unsettled navigation can
// mark its snapshot `possibly_loading`.
const _lastNavSettle = new Map();

// After a navigation completes at the document level, wait for the page to
// actually settle (client-side rendering, late data fetches) before the run
// moves on. sendToContent's re-injection retry covers pages so slow that the
// document_idle injection hasn't happened yet; failures (chrome:// pages,
// blocked injection) are non-fatal.
async function settleAfterNavigation(tabId, signal) {
  if (signal?.aborted) return;
  const res = await sendToContent(tabId, {
    type: "waitForDomSettled",
    opts: { quietMs: SETTLE_QUIET_MS, maxMs: SETTLE_MAX_MS },
  }).catch(() => null);
  // res === null means we couldn't ask (restricted page) — treat as settled
  // rather than tagging every chrome:// snapshot as "possibly loading".
  _lastNavSettle.set(tabId, { settled: res?.settled !== false });
}

// REQ-12 FR-12.3: original outer window size per tab, captured lazily on the
// first resize_window call a run makes (equivalent to "at run start" for a
// single-tab run, without paying a chrome.windows.get() call on every run that
// never resizes). Consumed by resize_window's restore:true.
const _originalWindowSize = new Map();

// REQ-11: run-scoped registry of screenshot/zoom artifacts, addressable as
// "screenshot:last" or "screenshot:<id>" from upload_file/drop_file's value —
// so a captured/zoomed image can be dropped onto a page without a round trip
// through the model. Capped per tab to bound memory on a long run.
const MAX_SCREENSHOT_ARTIFACTS = 20;
const _screenshotArtifactsByTab = new Map(); // tabId -> { items: [{id, dataUrl}], nextId }

function _pushScreenshotArtifact(tabId, dataUrl) {
  if (!dataUrl) return;
  let slot = _screenshotArtifactsByTab.get(tabId);
  if (!slot) { slot = { items: [], nextId: 1 }; _screenshotArtifactsByTab.set(tabId, slot); }
  slot.items.push({ id: slot.nextId++, dataUrl });
  if (slot.items.length > MAX_SCREENSHOT_ARTIFACTS) slot.items.shift();
}

// Resolves "screenshot:last" or "screenshot:<id>" to a data: URL, or null if
// it doesn't match the artifact-reference grammar or nothing was captured yet.
function _resolveScreenshotArtifact(tabId, ref) {
  const m = /^screenshot:(.+)$/i.exec(String(ref ?? "").trim());
  if (!m) return null;
  const slot = _screenshotArtifactsByTab.get(tabId);
  if (!slot || !slot.items.length) return null;
  const key = m[1].trim().toLowerCase();
  if (key === "last") return slot.items[slot.items.length - 1].dataUrl;
  const idx = Number(key);
  const found = Number.isInteger(idx) ? slot.items.find((a) => a.id === idx) : null;
  return found ? found.dataUrl : null;
}

async function sendToContent(tabId, payload, attempt = 0) {
  const frameId = _frameByTab.get(tabId) || 0;
  try {
    return await chrome.tabs.sendMessage(tabId, payload, frameId ? { frameId } : undefined);
  } catch (e) {
    if (attempt === 0) {
      await sendSW({ type: "ensureContentScript", tabId, frameId });
      return sendToContent(tabId, payload, 1);
    }
    if (frameId) {
      // A frameId goes dead when its iframe is removed or replaced (a re-added
      // iframe gets a NEW id). Fall back to the top frame with a clear error
      // instead of hanging the run.
      _frameByTab.delete(tabId);
      throw new Error(
        `Frame ${frameId} is gone — the iframe was removed or replaced. Returned to the top frame; use switch_frame again. (${e.message})`,
      );
    }
    throw e;
  }
}

async function snapshotPage(tabId) {
  const res = await sendToContent(tabId, { type: "snapshot" });
  if (!res?.ok) {
    return { url: null, title: null, error: res?.error || "snapshot failed" };
  }
  return res.snapshot;
}

// Browser-internal pages where content.js can never be injected. Empty URL
// (a just-created tab) counts as restricted too.
export function isRestrictedUrl(url) {
  const u = String(url || "").trim();
  return !u || /^(chrome|edge|about|chrome-extension|view-source|devtools):/i.test(u);
}

// Stand-in snapshot for a restricted tab: enough shape that snapshotIsEmpty
// stays false and formatSnapshotForLLM renders the navigate-first instruction,
// so the model's first action is a navigate (which needs no content script).
export function restrictedTabSnapshot(tab) {
  return {
    url: tab?.url || "about:blank",
    title: tab?.title || "New Tab",
    headings: [],
    interactive: [],
    page_text:
      "This is an empty browser tab (browser-internal page) with no readable content. " +
      "No page actions are possible here. Use the navigate action with a full URL to open a website first.",
    synthetic: true,
  };
}

// A snapshot is "empty" when the content script returned an error or the page
// has no usable structure yet — typical of a just-activated/just-loaded tab
// whose script isn't ready. Feeding that to the planner makes it think the goal
// is already done (→ a premature 0/0 run), so the first turn retries briefly.
function snapshotIsEmpty(s) {
  if (!s || s.error || !s.url) return true;
  const hasInteractive = Array.isArray(s.interactive) && s.interactive.length > 0;
  const hasHeadings = Array.isArray(s.headings) && s.headings.length > 0;
  const hasText = !!(s.page_text && String(s.page_text).trim());
  return !hasInteractive && !hasHeadings && !hasText;
}

// A snapshot "looks degenerate" when it could be a loading shell rather than
// real content: nothing at all, or a couple of stray elements with almost no
// text (nav bar + spinner). Unlike snapshotIsEmpty this also catches slow
// client-side renders that have painted a skeleton.
function snapshotLooksDegenerate(s) {
  // A painted canvas IS the content — not a loading shell. Skipping the
  // re-poll/possibly_loading path here lets the visual_content NOTE (and the
  // vision-auto screenshot) do the steering instead of a wrong "still
  // loading" hint.
  if (s?.visual_content) return false;
  if (snapshotIsEmpty(s)) return true;
  const interactive = Array.isArray(s.interactive) ? s.interactive.length : 0;
  const textLen = String(s.page_text || "").trim().length;
  return interactive < 3 && textLen < 200;
}

// Cheap change-detector between consecutive snapshots of the same page —
// enough signal to tell "still rendering" from "stopped changing".
function snapshotSignature(s) {
  return [
    s?.url || "",
    Array.isArray(s?.interactive) ? s.interactive.length : 0,
    String(s?.page_text || "").trim().length,
    Array.isArray(s?.headings) ? s.headings.length : 0,
  ].join("|");
}

async function snapshotPageReady(tabId, signal, sitePolicy = null) {
  // Restricted pages (new tab, chrome://, about:) reject content-script
  // injection outright — asking for a real snapshot would throw "Receiving end
  // does not exist" and kill the run before the model could navigate away.
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (isRestrictedUrl(tab?.url)) {
    _lastNavSettle.delete(tabId);
    return restrictedTabSnapshot(tab);
  }
  // Document tabs (REQUIREMENTS-READ_DOCUMENTS): the browser's PDF viewer hosts
  // the document in a plugin surface content scripts can't reach — a real
  // snapshot would read as an empty page (→ premature "done"). Short-circuit to
  // a synthetic snapshot that points the model at read_document instead.
  const docTab = detectDocumentUrl(tab?.url);
  if (docTab?.kind === "pdf") {
    _lastNavSettle.delete(tabId);
    return documentTabSnapshot(tab, docTab);
  }
  let snapshot = await snapshotPage(tabId);
  const navSettle = _lastNavSettle.get(tabId);
  _lastNavSettle.delete(tabId);

  if (snapshotLooksDegenerate(snapshot) && !signal?.aborted) {
    // Possible loading shell: re-poll until the page grows real content and
    // stops changing. A shell that is degenerate AND static for two polls is
    // either truly sparse or stuck loading — stop early either way (the
    // post-navigation settle wait already gave slow pages their budget) and
    // let the possibly_loading flag below tell the model to wait if needed.
    const deadline = Date.now() + SNAPSHOT_STABLE_MAX_MS;
    let prevSig = snapshotSignature(snapshot);
    let stablePolls = 0;
    while (Date.now() < deadline && !signal?.aborted) {
      await new Promise((r) => setTimeout(r, SNAPSHOT_POLL_MS));
      if (signal?.aborted) break;
      snapshot = await snapshotPage(tabId);
      const sig = snapshotSignature(snapshot);
      stablePolls = sig === prevSig ? stablePolls + 1 : 0;
      prevSig = sig;
      const degenerate = snapshotLooksDegenerate(snapshot);
      if (!degenerate && stablePolls >= 1) break;
      if (degenerate && stablePolls >= 2) break;
    }
  }

  // Google editors render the document body on canvas — the snapshot only sees
  // app chrome. Keep the normal snapshot (menus/tabs stay clickable) but tell
  // the model where the real content lives.
  if (docTab && snapshot && !snapshot.error) {
    const hint = documentSnapshotHint(docTab.kind);
    if (hint) {
      snapshot.page_text = `${String(snapshot.page_text || "").trim()}\n\n${hint}`.trim();
      // Google/Office editors are canvas-heavy and would also raise the
      // generic visual_content NOTE — read_document is the better route, so
      // keep only the document hint.
      delete snapshot.visual_content;
    }
  } else if (!docTab && snapshot && snapshotLooksDegenerate(snapshot) && !signal?.aborted) {
    // A PDF served from an extensionless URL (e.g. /invoice/123) still lands in
    // the opaque viewer and stays degenerate through the poll above — one cheap
    // content sniff (first KB) tells it apart from a slow-loading page.
    if (await sniffPdfUrl(tab?.url, signal, sitePolicy)) {
      _lastNavSettle.delete(tabId);
      return documentTabSnapshot(tab, { kind: "pdf" });
    }
  }

  // Still shell-like, or the post-navigation settle budget ran out while the
  // DOM was busy: warn the model so it waits/re-checks instead of concluding
  // the page has no data (formatSnapshotForLLM renders this as a NOTE line).
  if (snapshot && !snapshot.visual_content && (snapshotLooksDegenerate(snapshot) || navSettle?.settled === false)) {
    snapshot.possibly_loading = true;
  }
  return snapshot;
}

function waitForTabComplete(tabId, timeoutMs, signal) {
  return new Promise((resolve) => {
    if (signal?.aborted) { resolve(); return; }

    let graceTimer = null;

    const cleanup = () => {
      clearTimeout(t);
      clearTimeout(graceTimer);
      chrome.tabs.onUpdated.removeListener(listener);
      signal?.removeEventListener("abort", onAbort);
    };

    const t = setTimeout(() => {
      cleanup();
      resolve();
    }, timeoutMs);

    const done = () => {
      cleanup();
      // Small settle margin for last-moment paint/hydration after "complete".
      setTimeout(resolve, 30);
    };

    let sawLoading = false;
    const listener = (updatedTabId, info) => {
      if (updatedTabId !== tabId) return;
      // A (re)navigation is actually starting — the "already complete" status
      // below was the OLD page, so cancel the grace path and wait for the real
      // "complete" as usual.
      if (info.status === "loading") {
        sawLoading = true;
        clearTimeout(graceTimer);
        graceTimer = null;
      }
      if (info.status === "complete") done();
    };
    chrome.tabs.onUpdated.addListener(listener);

    const onAbort = () => {
      cleanup();
      resolve();
    };
    signal?.addEventListener("abort", onAbort, { once: true });

    // "complete" may have fired before the listener attached (fast/cached
    // pages), and BFCache back/forward emits no status event at all — either
    // way we'd stall for the full timeout. If the tab already reads complete,
    // resolve after a short grace window (cancelled above if a `loading`
    // event shows the navigation is only now starting).
    chrome.tabs.get(tabId).then((tab) => {
      if (tab?.status === "complete" && !signal?.aborted && !sawLoading) {
        graceTimer = setTimeout(done, 500);
      }
    }).catch(() => {});
  });
}
