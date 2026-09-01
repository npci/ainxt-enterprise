// SPDX-License-Identifier: Apache-2.0
// GateSignalRow — trust-calibrated signal strip shown ABOVE the verified diff
// at AWAITING_CODE_APPROVAL (legacy: AWAITING_DESIGN_APPROVAL) / AWAITING_SOLUTION_APPROVAL gates.
//
// PRIMARY signals (green/amber/red, prominent): Coverage, Manifest Validation,
//   Review (Opus diff-only review), Compiled, Tests.
// SECONDARY signals (muted gray, small): consistency / model confidence —
//   advisory only, never a headline.
//
// Each signal is fetched and rendered independently; a fetch failure → "—",
// never crashes the row.

import { useState, useEffect } from "react";
import {
  Hand,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Minus,
} from "lucide-react";
import { API_BASE as API, apiFetch } from "../../config";

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/** True if value is non-null / non-empty. */
function isPopulated(val) {
  if (val == null) return false;
  if (typeof val === "string") return val.trim().length > 0;
  if (Array.isArray(val)) return val.length > 0;
  if (typeof val === "object") return Object.keys(val).length > 0;
  return Boolean(val);
}

// Required keys that must be non-empty for full coverage.
const COVERAGE_KEYS = [
  "files_to_change",
  "sub_tasks",
  "implementation_spec",
  "solution_approach",
  "implementation_plan",
  "code_structure",
  "testing_strategy",
  "rollback_strategy",
];

function computeCoverage(payload) {
  if (!payload) return null;
  const populated = COVERAGE_KEYS.filter((k) => isPopulated(payload[k]));
  return { count: populated.length, total: COVERAGE_KEYS.length };
}

/** Defensive boolean coercion for compile/tests verdicts across payload shapes. */
function coerceBool(val) {
  if (val == null) return null;
  if (typeof val === "boolean") return val;
  if (typeof val === "string") {
    const lc = val.toLowerCase();
    if (lc === "true" || lc === "pass" || lc === "passed") return true;
    if (lc === "false" || lc === "fail" || lc === "failed") return false;
  }
  return Boolean(val);
}

/** Extract manifest validation pass/reject from an artifact payload (various field shapes). */
function extractManifestVerdict(payload) {
  if (!payload) return null;
  // Try in order of specificity
  for (const k of ["verdict", "passed", "result", "status", "pass"]) {
    const v = payload[k];
    if (v != null) {
      if (typeof v === "boolean") return v;
      const lc = String(v).toLowerCase();
      if (lc === "pass" || lc === "true" || lc === "passed" || lc === "ok") return true;
      if (lc === "reject" || lc === "false" || lc === "failed" || lc === "fail") return false;
    }
  }
  // Fallback: if struct_pass and openai_pass are present (ManifestValidationPanel shape)
  const { struct_pass, openai_pass } = payload;
  if (struct_pass != null || openai_pass != null) {
    return struct_pass !== false && openai_pass !== false;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Badge building blocks
// ---------------------------------------------------------------------------

/** A loading placeholder badge (skeleton). */
function SkeletonBadge() {
  return (
    <span className="inline-block h-5 w-20 rounded-full bg-gray-100 animate-pulse" />
  );
}

/**
 * Primary badge: green / amber / red with icon.
 * status: "ok" | "warn" | "error" | "unknown"
 */
function PrimaryBadge({ status, label, title }) {
  const styles = {
    ok:      "bg-green-100 text-green-700 border border-green-200",
    warn:    "bg-amber-100 text-amber-700 border border-amber-200",
    error:   "bg-red-100 text-red-700 border border-red-200",
    unknown: "bg-gray-100 text-gray-400 border border-gray-200",
  };
  const icons = {
    ok:      <CheckCircle2 size={11} className="text-green-500 flex-shrink-0" />,
    warn:    <AlertTriangle size={11} className="text-amber-500 flex-shrink-0" />,
    error:   <XCircle size={11} className="text-red-500 flex-shrink-0" />,
    unknown: <Minus size={11} className="text-gray-400 flex-shrink-0" />,
  };
  const cls = styles[status] || styles.unknown;
  const icon = icons[status] || icons.unknown;
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${cls}`}
      title={title}
    >
      {icon}
      {label}
    </span>
  );
}

/**
 * Secondary (muted) badge — advisory only, clearly subordinate.
 * Low-contrast gray; no icon prominence.
 */
function SecondaryBadge({ label, title }) {
  return (
    <span
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-normal bg-gray-50 text-gray-400 border border-gray-150"
      title={title}
    >
      {label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Individual signal fetchers / renderers
// ---------------------------------------------------------------------------

/** Signal 1: Coverage — fetches the PLAN artifact (three-phase CLI engine: PLAN
 * is the single planning stage; ANALYZING/DESIGNING no longer exist). */
function CoverageSignal({ runId }) {
  const [state, setState] = useState("loading"); // "loading" | "ok" | "warn" | "unknown"
  const [count, setCount] = useState(0);
  const total = COVERAGE_KEYS.length;

  useEffect(() => {
    if (!runId) { setState("unknown"); return; }
    let cancelled = false;

    async function load() {
      try {
        let payload = null;

        const planResp = await apiFetch(`${API}/sdlc/runs/${runId}/stages/PLAN/artifact`);
        if (planResp.ok) {
          const planData = await planResp.json().catch(() => null);
          payload = planData?.payload ?? null;
        }

        if (cancelled) return;

        if (!payload) {
          setState("unknown");
          return;
        }

        const cov = computeCoverage(payload);
        if (!cov) { setState("unknown"); return; }
        setCount(cov.count);
        setState(cov.count === total ? "ok" : "warn");
      } catch {
        if (!cancelled) setState("unknown");
      }
    }

    load();
    return () => { cancelled = true; };
  }, [runId]);

  if (state === "loading") return <SkeletonBadge />;
  if (state === "unknown") {
    return <PrimaryBadge status="unknown" label="Coverage —" title="Coverage data unavailable" />;
  }
  if (state === "ok") {
    return (
      <PrimaryBadge
        status="ok"
        label={`Coverage ✓`}
        title={`All ${total}/${total} required planning keys populated`}
      />
    );
  }
  return (
    <PrimaryBadge
      status="warn"
      label={`Coverage ${count}/${total}`}
      title={`Only ${count} of ${total} required planning keys populated`}
    />
  );
}

/** Signal 2: Manifest Validation verdict. */
function ManifestValidationSignal({ runId }) {
  const [state, setState] = useState("loading"); // "loading" | "pass" | "reject" | "unknown"
  const [detail, setDetail] = useState("");

  useEffect(() => {
    if (!runId) { setState("unknown"); return; }
    let cancelled = false;

    async function load() {
      try {
        const resp = await apiFetch(`${API}/sdlc/runs/${runId}/stages/MANIFEST_VALIDATION/artifact`);
        if (!resp.ok) { if (!cancelled) setState("unknown"); return; }
        const data = await resp.json().catch(() => null);
        if (cancelled) return;
        if (!data) { setState("unknown"); return; }

        const payload = data?.payload ?? data;
        const verdict = extractManifestVerdict(payload);

        if (verdict == null) { setState("unknown"); return; }

        // Collect a brief reason string for tooltip
        const issues = payload?.openai_issues ?? payload?.struct_failures ?? [];
        if (!verdict && Array.isArray(issues) && issues.length > 0) {
          setDetail(issues.slice(0, 2).join("; "));
        }

        setState(verdict ? "pass" : "reject");
      } catch {
        if (!cancelled) setState("unknown");
      }
    }

    load();
    return () => { cancelled = true; };
  }, [runId]);

  if (state === "loading") return <SkeletonBadge />;
  if (state === "unknown") {
    return <PrimaryBadge status="unknown" label="Manifest —" title="Manifest validation result unavailable" />;
  }
  if (state === "pass") {
    return <PrimaryBadge status="ok" label="Manifest PASS" title="Cross-provider manifest validation passed" />;
  }
  return (
    <PrimaryBadge
      status="error"
      label="Manifest REJECT"
      title={detail ? `Manifest rejected: ${detail}` : "Cross-provider manifest validation rejected"}
    />
  );
}

/** Signal 3: REVIEW verdict — the platform Opus diff-only review (three-phase CLI
 * engine). There is no stored REVIEW stage artifact (the verdict is emitted only
 * as a run event: stage="REVIEW", data={approved, blocking}), so this reads the
 * existing run-events trail and takes the most recent REVIEW entry. */
function ReviewSignal({ runId }) {
  const [state, setState] = useState("loading"); // "loading" | "pass" | "reject" | "unknown"
  const [detail, setDetail] = useState("");

  useEffect(() => {
    if (!runId) { setState("unknown"); return; }
    let cancelled = false;

    async function load() {
      try {
        const resp = await apiFetch(`${API}/sdlc/runs/${runId}/events`);
        if (!resp.ok) { if (!cancelled) setState("unknown"); return; }
        const data = await resp.json().catch(() => null);
        if (cancelled) return;

        const events = Array.isArray(data?.events) ? data.events : [];
        const reviewEvents = events.filter((e) => e?.stage === "REVIEW");
        if (reviewEvents.length === 0) { setState("unknown"); return; }

        const last = reviewEvents[reviewEvents.length - 1];
        const approved = last?.data?.approved;
        if (typeof approved !== "boolean") { setState("unknown"); return; }

        if (!approved) {
          setDetail(last?.output || "");
        }
        setState(approved ? "pass" : "reject");
      } catch {
        if (!cancelled) setState("unknown");
      }
    }

    load();
    return () => { cancelled = true; };
  }, [runId]);

  if (state === "loading") return <SkeletonBadge />;
  if (state === "unknown") {
    return <PrimaryBadge status="unknown" label="Review —" title="Opus diff review result unavailable" />;
  }
  if (state === "pass") {
    return <PrimaryBadge status="ok" label="Review PASS" title="Opus diff-only review approved" />;
  }
  return (
    <PrimaryBadge
      status="error"
      label="Review REJECT"
      title={detail ? `Opus review unresolved: ${detail}` : "Opus diff-only review found blocking issues"}
    />
  );
}

/** Signals 4+5: Compile + Tests from verified-diff; also emits waiver chips and secondary confidence. */
function VerifiedDiffSignals({ runId, onSecondary }) {
  const [signals, setSignals] = useState(null); // null = loading
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!runId) { setFailed(true); return; }
    let cancelled = false;

    async function load() {
      try {
        const resp = await apiFetch(`${API}/sdlc/runs/${runId}/verified-diff`);
        if (!resp.ok) { if (!cancelled) setFailed(true); return; }
        const data = await resp.json().catch(() => null);
        if (cancelled) return;
        if (!data) { setFailed(true); return; }

        const vd = data?.verified_diff ?? {};
        const banners = data?.waiver_banners ?? [];

        // Normalise compile
        const compileRaw = vd?.compile ?? {};
        const compilePassed = coerceBool(
          compileRaw?.passed ?? compileRaw?.compile_ok ?? compileRaw?.ok ?? compileRaw
        );
        const compileSkipped = coerceBool(compileRaw?.skipped);

        // Normalise tests
        const testsRaw = vd?.tests ?? {};
        const testsPassed = coerceBool(
          testsRaw?.passed ?? testsRaw?.tests_passed ?? testsRaw?.ok ?? testsRaw
        );
        const testsSkipped = coerceBool(testsRaw?.skipped);

        // Secondary: confidence / consistency from either payload
        const confidence =
          vd?.confidence ??
          vd?.model_confidence ??
          data?.confidence ??
          null;
        const consistency =
          vd?.consistency ?? data?.consistency ?? null;

        if (onSecondary) {
          onSecondary({ confidence, consistency });
        }

        setSignals({
          compilePassed,
          compileSkipped,
          testsPassed,
          testsSkipped,
          banners,
        });
      } catch {
        if (!cancelled) setFailed(true);
      }
    }

    load();
    return () => { cancelled = true; };
  }, [runId]); // eslint-disable-line react-hooks/exhaustive-deps

  if (signals === null && !failed) {
    return (
      <>
        <SkeletonBadge />
        <SkeletonBadge />
      </>
    );
  }

  if (failed || !signals) {
    return (
      <>
        <PrimaryBadge status="unknown" label="Compile —" title="Compile status unavailable" />
        <PrimaryBadge status="unknown" label="Tests —" title="Test status unavailable" />
      </>
    );
  }

  const { compilePassed, compileSkipped, testsPassed, testsSkipped, banners } = signals;

  let compileStatus = "unknown";
  let compileLabel = "Compile —";
  if (compileSkipped) {
    compileStatus = "warn";
    compileLabel = "Compile waived";
  } else if (compilePassed === true) {
    compileStatus = "ok";
    compileLabel = "Compiled ✓";
  } else if (compilePassed === false) {
    compileStatus = "error";
    compileLabel = "Compile failed";
  }

  let testsStatus = "unknown";
  let testsLabel = "Tests —";
  if (testsSkipped) {
    testsStatus = "warn";
    testsLabel = "Tests waived";
  } else if (testsPassed === true) {
    testsStatus = "ok";
    testsLabel = "Tests green ✓";
  } else if (testsPassed === false) {
    testsStatus = "error";
    testsLabel = "Tests failed";
  }

  return (
    <>
      <PrimaryBadge status={compileStatus} label={compileLabel} />
      <PrimaryBadge status={testsStatus} label={testsLabel} />
      {banners.length > 0 &&
        banners.map((b, i) => (
          <span
            key={i}
            className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-amber-100 text-amber-700 border border-amber-200"
            title={b}
          >
            <AlertTriangle size={10} className="text-amber-500 flex-shrink-0" />
            {String(b).length > 30 ? String(b).slice(0, 28) + "…" : b}
          </span>
        ))}
    </>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

/**
 * GateSignalRow — trust-calibrated HITL signal strip.
 *
 * Props:
 *   run     {object}  The SDLC run object (used for run.id if runId not given).
 *   runId   {string}  Explicit run ID (overrides run.id).
 *   runType {string}  "feature" | "bug" — reserved for future per-type filtering.
 */
export default function GateSignalRow({ run, runId: runIdProp, runType: _runType }) {
  const runId = runIdProp ?? run?.id;

  // Secondary signal state, populated by VerifiedDiffSignals callback
  const [secondary, setSecondary] = useState({ confidence: null, consistency: null });

  if (!runId) return null;

  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5">
      {/* Header: raised-hand affordance */}
      <div className="flex items-center gap-1.5 mb-2">
        <Hand size={14} className="text-amber-600 flex-shrink-0" />
        <span className="text-xs font-semibold text-amber-800">Needs your review</span>
        <span className="text-[10px] text-amber-600 ml-1">
          — verify these signals before approving
        </span>
      </div>

      {/* Signal badges row */}
      <div className="flex items-center gap-1.5 flex-wrap">
        {/* PRIMARY signals — prominent, colour-coded */}
        <CoverageSignal runId={runId} />
        <ManifestValidationSignal runId={runId} />
        <ReviewSignal runId={runId} />
        <VerifiedDiffSignals runId={runId} onSecondary={setSecondary} />

        {/* Visual divider before secondary */}
        {(secondary.confidence != null || secondary.consistency != null) && (
          <span className="text-gray-200 select-none mx-0.5">|</span>
        )}

        {/* SECONDARY signals — muted gray, advisory, clearly subordinate */}
        {secondary.consistency != null && (
          <SecondaryBadge
            label={`consistency: ${
              typeof secondary.consistency === "number"
                ? `${Math.round(secondary.consistency * 100)}%`
                : secondary.consistency
            }`}
            title="Advisory — model self-reported consistency score (not a primary gate signal)"
          />
        )}
        {secondary.confidence != null && (
          <SecondaryBadge
            label={`conf: ${
              typeof secondary.confidence === "number"
                ? `${Math.round(secondary.confidence * 100)}%`
                : secondary.confidence
            }`}
            title="Advisory — model confidence (≤15% weight; not a primary gate signal)"
          />
        )}
      </div>
    </div>
  );
}
