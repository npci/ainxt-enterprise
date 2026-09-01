// SPDX-License-Identifier: Apache-2.0
// =============================================================================
// AiNxt Coach — single-page dashboard
// Spec: AINXT_ENGINEER_COACH_REQUIREMENTS.md (Sec 6 - IA, FR-OBS/MEAS/IMP/ENT)
//
// Tabs (Sec 6 sidebar map; rendered as inner tabs to keep main sidebar tidy):
//   - Overview                (FR-OBS-2)
//   - Practice Scores         (FR-MEAS-1)
//   - Anti-Patterns           (FR-MEAS-2)
//   - Query Explorer          (FR-OBS-4)
//   - Rule Playground         (FR-IMP-2)
//   - Org Rollups (admin)     (FR-ENT-1)
//
// All data comes from /coach/* APIs; no mock content.
// =============================================================================
import { useEffect, useMemo, useState } from "react";
import { API_BASE, authFetch } from "../config";
import {
  Activity, Target, AlertTriangle, Search,
  ShieldAlert, CheckCircle, Cpu,
  Sparkles, ChevronRight, Clock, Zap, DollarSign,
  BarChart2, Award, TrendingUp, BookOpen,
} from "lucide-react";
import CoachAdmin from "./CoachAdmin";
import useToggleSet from "../hooks/useToggleSet";
import { toISTDate, toISTShort, toISTTimeShort } from "../utils/time";

// ── Channel label map — stored value "mcp" is displayed as "IDE/API" ─────────
const CHANNEL_LABEL = {
  mcp:    "IDE/API",
  web:    "Web",
  cli:    "CLI",
  api:    "API",
  teams:  "Teams",
  voice:  "Voice",
  mobile: "Mobile",
  embed:  "Browser Ext",
  workflow: "Workflow",
  agent:  "Agent",
  slack:  "Slack",
  sdlc:   "SDLC",
};
const fmtChannel = c => CHANNEL_LABEL[(c || "").toLowerCase()] || c;

// ── Helpers ──────────────────────────────────────────────────────────────────
function scoreColor(score) {
  if (score >= 80) return { bar: "bg-emerald-400", text: "text-emerald-600", chip: "bg-emerald-100 text-emerald-700" };
  if (score >= 50) return { bar: "bg-amber-400",   text: "text-amber-600",   chip: "bg-amber-100 text-amber-700" };
  return                  { bar: "bg-red-400",     text: "text-red-600",     chip: "bg-red-100 text-red-700" };
}

const SEVERITY_STYLE = {
  low:      "bg-slate-100 text-slate-700",
  medium:   "bg-amber-100 text-amber-700",
  high:     "bg-orange-100 text-orange-800",
  critical: "bg-red-100 text-red-800",
};

const CATEGORY_LABEL = {
  "prompt-quality":     "Prompt Quality",
  "session-hygiene":    "Session Hygiene",
  "review-discipline":  "Review Discipline",
  "tool-mastery":       "Tool Mastery",
  "context-management": "Context Management",
  "security":           "Security",
};

// Per-category metadata: short hint (card label), long hint (tooltip).
const CATEGORY_META = {
  "prompt-quality":     { hint: "Clear, specific prompts.",        tooltip: "How clear and specific your prompts are — intent, constraints, success criteria." },
  "session-hygiene":    { hint: "Focused, on-topic threads.",      tooltip: "How well your threads stay on-topic and don't drift, restart, or balloon." },
  "review-discipline":  { hint: "Inspecting AI output.",           tooltip: "Whether you inspect, accept or reject AI output — especially for sensitive changes." },
  "tool-mastery":       { hint: "Right model & tools.",            tooltip: "Picking the right model, skill or tool for the job; avoiding retry loops." },
  "context-management": { hint: "Good grounding, no duplicates.",  tooltip: "Using the right grounding — KB, instructions, no duplicates, no over-stuffed context." },
  "security":           { hint: "No PII, secrets, or bypasses.",   tooltip: "Keeping PII, secrets and confidential data out of prompts; respecting governance gates." },
};

// Tabs intentionally trimmed:
//   * Practice Scores → removed: identical numbers already rendered on Overview.
//   * Anti-Patterns   → removed: each hit is shown in context inside Query Explorer.
// The backend endpoints for both remain for API/CLI consumers.
const TABS = [
  { key: "overview",   label: "Overview",        icon: Activity     },
  { key: "models",     label: "Models",          icon: Cpu          },
  { key: "explorer",   label: "Query Explorer",  icon: Search       },
  { key: "digest",     label: "My Digest",       icon: BarChart2    },
  { key: "admin",      label: "Admin",           icon: ShieldAlert,  adminOnly: true },
];

// =============================================================================
export default function Coach({ user }) {
  const isAdmin = user?.role === "admin";
  const [tab, setTab] = useState("overview");
  const [days, setDays] = useState(7);

  const visibleTabs = TABS.filter(t => !t.adminOnly || isAdmin);

  return (
    <div className="flex flex-col h-screen bg-gradient-to-br from-slate-50 via-white to-indigo-50/40 overflow-hidden">
      {/* ── Header ─────────────────────────────────────────────────── */}
      <div className="flex-shrink-0 border-b border-slate-200/70 bg-white/70 backdrop-blur-md">
        <div className="px-6 pt-5 pb-2 flex items-start justify-between gap-4">
          <div className="flex items-start gap-3 min-w-0">
            {/* Gradient icon badge for visual anchor */}
            <div className="w-9 h-9 rounded-xl brand-grad-vivid flex items-center justify-center shadow-sm flex-shrink-0">
              <Target className="text-white" size={18} />
            </div>
            <div className="min-w-0">
              <h1 className="text-[17px] font-bold text-slate-900 tracking-tight leading-tight">
                AiNxt Coach
              </h1>
              <p className="text-[11px] text-slate-500 mt-0.5 max-w-xl leading-snug">
                Personalised coaching from your AiNxt usage. Only you see your prompts.
              </p>
            </div>
          </div>

          {/* Pill-style time window switcher */}
          <div className="inline-flex p-0.5 rounded-lg bg-slate-100 border border-slate-200/70 flex-shrink-0">
            {[7, 30, 90].map(d => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={`px-2.5 py-1 text-[11px] font-medium rounded-md transition cursor-pointer ${
                  days === d
                    ? "bg-white text-slate-900 shadow-sm"
                    : "text-slate-500 hover:text-slate-700"
                }`}
              >
                {d}d
              </button>
            ))}
          </div>
        </div>

        {/* ── Tab bar ──────────────────────────────────────────────── */}
        <div className="px-4 pt-1 flex gap-0.5 overflow-x-auto">
          {visibleTabs.map(t => {
            const Icon = t.icon;
            const active = t.key === tab;
            return (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                className={`relative px-3 py-2.5 text-[12px] font-medium flex items-center gap-1.5 transition cursor-pointer whitespace-nowrap ${
                  active
                    ? "text-indigo-700"
                    : "text-slate-500 hover:text-slate-800"
                }`}
              >
                <Icon size={13} className={active ? "text-indigo-500" : ""} />
                {t.label}
                {/* Underline indicator */}
                <span className={`absolute left-2 right-2 -bottom-px h-0.5 rounded-full transition ${
                  active ? "brand-grad-r" : "bg-transparent"
                }`} />
              </button>
            );
          })}
        </div>
      </div>

      {/* ── Body ───────────────────────────────────────────────────── */}
      <div className="flex-1 overflow-auto px-6 py-6">
        {tab === "overview"   && <OverviewTab   days={days} />}
        {tab === "digest"     && <MyDigestTab   days={days} />}
        {tab === "models"     && <ModelsTab     days={days} />}
        {tab === "explorer"   && <ExplorerTab   days={days} />}
        {tab === "admin"      && isAdmin && <CoachAdmin user={user} days={days} />}
      </div>
    </div>
  );
}

// =============================================================================
// Overview tab — single dashboard call (FR-OBS-2)
// =============================================================================
function OverviewTab({ days }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  // Which anti-pattern rows are expanded to show the full advice. Uses a set
  // so multiple rows can be open at once and each toggles independently.
  const [openRules, toggleRule] = useToggleSet();

  useEffect(() => {
    setLoading(true); setErr(null);
    authFetch(`${API_BASE}/coach/dashboard?days=${days}`)
      .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
      .then(setData)
      .catch(e => setErr(String(e)))
      .finally(() => setLoading(false));
  }, [days]);

  if (loading)  return <Skeleton />;
  if (err)      return <ErrorBox msg={err} />;
  if (!data)    return null;

  // overall can be null when there aren't enough events yet — show "—".
  const overall    = data.overall;
  const overallNum = overall ?? 0;
  const color      = scoreColor(overallNum);
  const noData     = data.insufficient_data || overall == null;

  return (
    <div className="space-y-5 max-w-6xl mx-auto">
      {/* ── Hero strip: Practice Score (ring) + KPI stack ──────────── */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {/* Practice Score — visually dominant, ring gauge */}
        <div className="md:col-span-1 relative overflow-hidden p-5 bg-white border border-slate-200 rounded-2xl shadow-sm">
          {/* Subtle gradient backdrop tinted by score colour */}
          <div className={`absolute inset-0 opacity-[0.04] ${
            noData ? "bg-slate-400"
                   : overallNum >= 80 ? "bg-emerald-400"
                   : overallNum >= 50 ? "bg-amber-400" : "bg-red-400"
          }`} />
          <div className="relative flex items-center gap-4">
            <ScoreRing score={overallNum} disabled={noData} size={84} />
            <div className="min-w-0">
              <div className="text-[10px] uppercase tracking-widest text-slate-400 font-bold">Practice Score</div>
              <div className={`text-3xl font-bold mt-0.5 leading-none ${noData ? "text-slate-300" : color.text}`}>
                {noData ? "—" : overall}
                <span className="text-base text-slate-400 font-medium ml-1">/100</span>
              </div>
              <div className="text-[11px] text-slate-500 mt-1.5">
                {noData ? "Not enough events yet" : `over last ${data.window_days} days`}
              </div>
            </div>
          </div>
        </div>

        {/* Smaller stat tiles, stacked on the right */}
        <Stat label="Events"
              value={data.totals?.events ?? 0}
              icon={Activity} />
        <Stat label="Anti-pattern hits"
              value={data.totals?.hits ?? 0}
              icon={AlertTriangle}
              accent={(data.totals?.hits ?? 0) > 0 ? "amber" : null}
              hint={
                data.totals?.events
                  ? `${(data.totals.hits / data.totals.events).toFixed(2)} avg per event`
                  : null
              } />
      </div>

      {/* Practice scores per category */}
      <Card title="Practice Scores by Category"
            subtle={data.insufficient_data
              ? "Not enough events yet — scores appear once you have ~3+ events in the window."
              : "Each tile shows the score and the raw penalty points subtracted from 100."}>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
          {Object.entries(data.scores || {}).map(([cat, score]) => (
            <CategoryMini key={cat} category={cat} score={score} />
          ))}
        </div>
        {!data.insufficient_data && (
          <ScoreFormulaPanel />
        )}
      </Card>

      <Card title="Top Anti-Patterns" subtle="Most-fired rules in this window. Click a rule to see how to fix it.">
        {data.top_rules?.length ? (
          <ul className="space-y-1.5">
            {data.top_rules.map(r => {
              const isOpen = openRules.has(r.rule_id);
              const hasAdvice = r.advice && r.advice.trim();
              return (
                <li key={r.rule_id}
                    className="rounded-lg border border-slate-200 overflow-hidden">
                  <button
                    type="button"
                    onClick={() => hasAdvice && toggleRule(r.rule_id)}
                    className={`w-full flex items-center gap-2 text-left px-2.5 py-1.5 transition ${
                      hasAdvice ? "hover:bg-slate-50 cursor-pointer" : "cursor-default"
                    }`}
                  >
                    {/* Rule code — monospace, the stable identifier */}
                    <code className="text-[11px] text-slate-700 font-mono flex-shrink-0">{r.code || r.rule_id}</code>
                    {/* Title — what the rule actually means */}
                    {r.title && (
                      <span className="text-[11px] text-slate-600 truncate flex-1 min-w-0">{r.title}</span>
                    )}
                    {/* Severity badge */}
                    {r.severity && (
                      <span className={`text-[9px] font-bold uppercase px-1.5 py-0.5 rounded-full border flex-shrink-0 ${
                        SEV_STYLE[r.severity] || SEV_STYLE.low
                      }`}>
                        {r.severity}
                      </span>
                    )}
                    {/* Hit count */}
                    <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-slate-100 text-slate-600 font-mono flex-shrink-0">
                      ×{r.count}
                    </span>
                    {/* Expand chevron — only when there's advice to show */}
                    {hasAdvice && (
                      <ChevronRight size={12}
                        className={`flex-shrink-0 text-slate-400 transition-transform duration-200 ${isOpen ? "rotate-90" : ""}`} />
                    )}
                  </button>
                  {/* Expanded advice — the "how to fix it" description */}
                  {isOpen && hasAdvice && (
                    <div className="px-2.5 pb-2.5 pt-1 bg-slate-50/60 border-t border-slate-100">
                      <p className="text-[11px] text-slate-600 leading-relaxed">{r.advice}</p>
                      {r.category && (
                        <span className="inline-block mt-1.5 text-[9.5px] text-slate-400 capitalize">
                          Category: {r.category.replace(/-/g, " ")}
                        </span>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        ) : <EmptyText>No anti-patterns flagged. Nice work. 🎯</EmptyText>}
      </Card>
    </div>
  );
}

// Restyled category tile: label + hint on the left, score on the right, slim bar below.
// Below the bar we surface the raw penalty derived from the backend formula
//   score = 100 * exp(−penalty / 60)
// inverted as
//   penalty = −60 * ln(score / 100)
// Every new hit always moves the score — there is no saturation floor in this
// model — so we no longer render a "saturated" tag. When the score is exactly
// 100 (no penalty), we suppress the chip entirely to keep the tile clean.
function CategoryMini({ category, score }) {
  const color = scoreColor(score);
  const meta  = CATEGORY_META[category] || {};
  const hint  = meta.hint;
  const long  = meta.tooltip;
  const noScore = score == null;
  const display = noScore ? "—" : score;

  // Penalty in raw points — must match SCORE_DECAY_K in agents/coach_evaluator.py.
  const DECAY_K = 60.0;
  let penalty = null;
  if (!noScore && score > 0 && score < 100) {
    penalty = Math.round(-DECAY_K * Math.log(score / 100) * 10) / 10;
  } else if (!noScore && score <= 0) {
    // Math edge: ln(0) = −∞. Backend clamps to >= 0; in practice this branch
    // will not fire because exp(−penalty/K) > 0 for any finite penalty.
    // Defensive fallback so the UI never renders NaN.
    penalty = Infinity;
  }
  const penaltyLabel = noScore
    ? null
    : penalty == null || penalty <= 0 ? null
    : !isFinite(penalty) ? "very high"
    : `−${penalty} pts`;

  return (
    <div className="group relative p-3 border border-slate-200 rounded-xl bg-white hover:border-slate-300 hover:shadow-sm transition"
         title={long}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-[10px] uppercase tracking-wider text-slate-500 font-bold truncate">
            {CATEGORY_LABEL[category] || category}
          </div>
          {hint && (
            <div className="text-[10px] text-slate-400 leading-tight mt-0.5 truncate">
              {hint}
            </div>
          )}
        </div>
        <div className={`text-xl font-bold leading-none ${noScore ? "text-slate-300" : color.text}`}>
          {display}
        </div>
      </div>
      <div className="mt-2.5 h-1 bg-slate-100 rounded-full overflow-hidden">
        <div className={`h-full transition-all duration-500 ${noScore ? "bg-slate-200" : color.bar}`}
             style={{ width: noScore ? "5%" : `${Math.max(3, score)}%` }} />
      </div>
      {/* Penalty chip — raw points subtracted (continuous, no saturation) */}
      {penaltyLabel && (
        <div className="mt-2 flex items-center justify-between gap-2"
             title="Total penalty points accumulated in this window. Every new hit moves the score; the score curve never floors at 0 in normal use.">
          <span className="text-[10px] font-mono tracking-tight text-slate-600">
            {penaltyLabel}
          </span>
        </div>
      )}
    </div>
  );
}

// Inline explainer panel — renders the exact formula the backend uses to
// compute each category score, plus the per-severity caps. Lives directly
// beneath the category grid so users can see WHY a number is what it is.
// Mirrors PER_HIT_PENALTY_CAP, SCORE_DECAY_K and COACH_EVAL_PENALTY_WEIGHT
// in agents/coach_evaluator.py and core/config.py.
function ScoreFormulaPanel() {
  const CAPS = [
    { sev: "low",      cap: 4,  example: 93.6 },
    { sev: "medium",   cap: 8,  example: 87.5 },
    { sev: "high",     cap: 15, example: 77.9 },
    { sev: "critical", cap: 25, example: 65.9 },
  ];
  return (
    <div className="mt-4 pt-3 border-t border-dashed border-slate-200">
      <div className="text-[10px] uppercase tracking-widest text-slate-500 font-bold mb-2">
        How the score is calculated
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div className="text-[11px] text-slate-600 leading-relaxed space-y-1.5">
          <div>
            <code className="text-[10.5px] font-mono bg-slate-50 border border-slate-200 rounded px-1 py-0.5">
              score = 100 × exp(−Σ penalty / 60)
            </code>
          </div>
          <div>
            <code className="text-[10.5px] font-mono bg-slate-50 border border-slate-200 rounded px-1 py-0.5">
              penalty = min(cap, cap × events / max(events, 10))
            </code>
          </div>
          <div className="text-slate-500 mt-1.5">
            Each anti-pattern hit adds up to <code className="font-mono">cap</code> penalty points
            for its category. The score is an exponential decay of the cumulative
            penalty, so every new hit always nudges the number down — there is no
            saturation floor. On samples below 10 events the divisor floor
            (<code className="font-mono">10</code>) dampens single hits; once events ≥ 10 the cap is reached.
          </div>
          {/* LLM Judge contribution */}
          <div className="mt-2 p-2 rounded-lg bg-indigo-50 border border-indigo-100">
            <div className="text-[10px] uppercase tracking-wider text-indigo-600 font-bold mb-1 flex items-center gap-1">
              <Sparkles size={9} className="text-indigo-400" /> LLM Judge (Prompt Quality only)
            </div>
            <code className="text-[10.5px] font-mono bg-white border border-indigo-100 rounded px-1 py-0.5 block">
              eval_penalty = (1 − eval_score) × 3.0
            </code>
            <div className="text-[10px] text-indigo-700 mt-1.5 leading-snug">
              For every prompt the LLM judge <span className="font-semibold">REJECTs</span>, an additional penalty is added to <span className="font-semibold">Prompt Quality</span> proportional to how badly it failed — a score of 0.5 adds 1.5 pts, a score of 0.0 adds 3.0 pts. Prompts the judge has not yet evaluated (NULL) are skipped.
            </div>
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold mb-1.5">
            Per-hit cap &amp; first-hit score
          </div>
          <ul className="space-y-1.5">
            {CAPS.map(({ sev, cap, example }) => (
              <li key={sev}
                  className="flex items-center justify-between gap-2 text-[10.5px] px-2 py-1 rounded-md bg-slate-50 border border-slate-100">
                <span className={`px-1.5 py-0.5 rounded-full font-bold text-[9.5px] uppercase tracking-wider ${SEVERITY_STYLE[sev]}`}>
                  {sev}
                </span>
                <span className="font-mono text-slate-700 flex-1 text-right">{cap} pts</span>
                <span className="font-mono text-slate-400 text-[10px]" title="Category score after one hit of this severity, starting from 100.">
                  → {example}
                </span>
              </li>
            ))}
          </ul>
          {/* LLM Judge penalty examples */}
          <div className="mt-2.5">
            <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold mb-1.5">
              LLM Judge penalty examples
            </div>
            <ul className="space-y-1.5">
              {[
                { score: "0.83", label: "5/6 pass", pts: "0.5" },
                { score: "0.67", label: "4/6 pass", pts: "1.0" },
                { score: "0.50", label: "3/6 pass", pts: "1.5" },
                { score: "0.33", label: "2/6 pass", pts: "2.0" },
                { score: "0.00", label: "0/6 pass", pts: "3.0" },
              ].map(({ score, label, pts }) => (
                <li key={score}
                    className="flex items-center justify-between gap-2 text-[10.5px] px-2 py-1 rounded-md bg-indigo-50 border border-indigo-100">
                  <span className="font-mono text-indigo-700">{score}</span>
                  <span className="text-slate-500 flex-1 text-center">{label}</span>
                  <span className="font-mono text-slate-700">−{pts} pts</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// Query Explorer (FR-OBS-4) — two-level view: sessions collapse → expand to
// prompts → expand to coaching panel. Works uniformly for Web, CLI and IDE
// (mcp) channels; client_source distinguishes platform/cli/ide-vscode/
// ide-jetbrains/api so the user can tell same-channel-different-origin apart.
// Backend: GET /coach/events?group_by=thread.
// =============================================================================

// Maps Chat.client_source → display label. Distinct from CHANNEL_LABEL because
// channel == network path (mcp/web/...), client_source == originating client.
//
// NOTE: CoachEvent has no client_source column — the backend derives a coarse
// client_source from the stored channel value via _client_source() in
// coach_router.py.  "mcp" channel maps to the generic "ide" token (not
// "ide-vscode") because we cannot tell which specific IDE client originated the
// event at query time.  "ide-vscode" / "ide-jetbrains" are kept here for
// forward-compatibility if a client_source column is added later.
const CLIENT_SOURCE_LABEL = {
  platform:        "Web",
  ide:             "IDE",        // generic — actual client unknown at query time
  "ide-vscode":    "VS Code",    // future: when client_source column is stored
  "ide-jetbrains": "JetBrains",  // future: when client_source column is stored
  cli:             "CLI",
  api:             "API",
  "browser-ext":   "Browser Ext", // Chrome browser-automation extension
};
const fmtClientSource = s => CLIENT_SOURCE_LABEL[s] || s;

// Distinct from SEVERITY_STYLE so PII/Secret/Compliance read as their own
// category, not as a severity level.
// =============================================================================
// My Digest Tab — rich single-page summary for the user (non-admin)
// Fetches GET /coach/my-digest once and renders everything in one place.
// =============================================================================

const DOMAIN_COLOURS = {
  code:     { bar: "#4f46e5", bg: "bg-indigo-50",  border: "border-indigo-200",  text: "text-indigo-700",  badge: "bg-indigo-100 text-indigo-700"  },
  devops:   { bar: "#7c3aed", bg: "bg-violet-50",  border: "border-violet-200",  text: "text-violet-700",  badge: "bg-violet-100 text-violet-700"  },
  data:     { bar: "#0891b2", bg: "bg-cyan-50",    border: "border-cyan-200",    text: "text-cyan-700",    badge: "bg-cyan-100 text-cyan-700"      },
  security: { bar: "#dc2626", bg: "bg-red-50",     border: "border-red-200",     text: "text-red-700",     badge: "bg-red-100 text-red-700"        },
  finance:  { bar: "#059669", bg: "bg-emerald-50", border: "border-emerald-200", text: "text-emerald-700", badge: "bg-emerald-100 text-emerald-700"},
  hr:       { bar: "#db2777", bg: "bg-pink-50",    border: "border-pink-200",    text: "text-pink-700",    badge: "bg-pink-100 text-pink-700"      },
  legal:    { bar: "#d97706", bg: "bg-amber-50",   border: "border-amber-200",   text: "text-amber-700",   badge: "bg-amber-100 text-amber-700"    },
  general:  { bar: "#64748b", bg: "bg-slate-50",   border: "border-slate-200",   text: "text-slate-600",   badge: "bg-slate-100 text-slate-600"    },
};

const SEV_STYLE = {
  critical: "bg-red-100 text-red-700 border-red-200",
  high:     "bg-orange-100 text-orange-700 border-orange-200",
  medium:   "bg-amber-100 text-amber-700 border-amber-200",
  low:      "bg-slate-100 text-slate-600 border-slate-200",
};

const CAT_COLOUR = {
  "prompt-quality":     "#4f46e5",
  "session-hygiene":    "#7c3aed",
  "review-discipline":  "#d97706",
  "tool-mastery":       "#0891b2",
  "context-management": "#059669",
  "security":           "#dc2626",
};

// ── Shared primitives ─────────────────────────────────────────────────────────

function DigestCard({ title, icon: Icon, iconColor = "text-indigo-500", children, noPad }) {
  return (
    <div className="bg-white border border-slate-200 rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.06)] overflow-hidden">
      <div className="flex items-center gap-2 px-5 py-3.5 border-b border-slate-100">
        {Icon && <Icon size={14} className={iconColor} />}
        <span className="text-[11px] font-bold uppercase tracking-widest text-slate-600">{title}</span>
      </div>
      <div className={noPad ? "" : "px-5 py-4"}>{children}</div>
    </div>
  );
}

function DigestScoreRing({ score }) {
  const valid = typeof score === "number";
  const pct   = valid ? Math.min(100, Math.max(0, score)) : 0;
  const r = 52, cx = 64, cy = 64, sw = 9;
  const circ = 2 * Math.PI * r;
  const dash  = (pct / 100) * circ;
  const colour = pct >= 75 ? "#10b981" : pct >= 50 ? "#f59e0b" : "#ef4444";
  return (
    <div className="flex flex-col items-center">
      <svg width={128} height={128}>
        <circle cx={cx} cy={cy} r={r} fill="none" stroke="#f1f5f9" strokeWidth={sw} />
        <circle cx={cx} cy={cy} r={r} fill="none"
          stroke={colour} strokeWidth={sw}
          strokeDasharray={`${dash} ${circ}`}
          strokeLinecap="round"
          transform={`rotate(-90 ${cx} ${cy})`}
          style={{ transition: "stroke-dasharray 0.7s ease" }}
        />
        <text x={cx} y={cy - 6} textAnchor="middle" dominantBaseline="middle"
              fontSize="22" fontWeight="800" fill={colour}>
          {valid ? Math.round(pct) : "—"}
        </text>
        <text x={cx} y={cy + 14} textAnchor="middle" dominantBaseline="middle"
              fontSize="10" fill="#94a3b8">
          {valid ? "/ 100" : "no data"}
        </text>
      </svg>
      <span className="text-[11px] font-semibold text-slate-500 uppercase tracking-widest -mt-1">
        Practice score
      </span>
    </div>
  );
}

function DigestCategoryBar({ label, value }) {
  const pct    = typeof value === "number" ? Math.min(100, Math.max(0, value)) : 0;
  const colour = pct >= 75 ? "#10b981" : pct >= 50 ? "#f59e0b" : "#ef4444";
  const catCol = CAT_COLOUR[label] || "#64748b";
  const meta   = CATEGORY_META[label] || {};
  return (
    <div className="group flex items-center gap-3 py-2 border-b border-slate-50 last:border-0">
      <div className="w-36 flex-shrink-0">
        <div className="text-[11px] font-semibold text-slate-700 truncate" style={{ color: catCol }}>
          {CATEGORY_LABEL[label] || label}
        </div>
        {meta.hint && (
          <div className="text-[10px] text-slate-400 leading-tight">{meta.hint}</div>
        )}
      </div>
      <div className="flex-1 h-2.5 bg-slate-100 rounded-full overflow-hidden">
        <div className="h-full rounded-full transition-all duration-700"
             style={{ width: `${pct}%`, background: colour }} />
      </div>
      <span className="w-10 text-right text-[12px] font-bold flex-shrink-0"
            style={{ color: colour }}>
        {typeof value === "number" ? Math.round(value) : "—"}
      </span>
    </div>
  );
}

function DigestStatTile({ label, value, icon: Icon, accent = "indigo" }) {
  const MAP = {
    indigo:  { bg: "bg-indigo-50",  icon: "text-indigo-500",  val: "text-indigo-800"  },
    emerald: { bg: "bg-emerald-50", icon: "text-emerald-500", val: "text-emerald-800" },
    violet:  { bg: "bg-violet-50",  icon: "text-violet-500",  val: "text-violet-800"  },
    amber:   { bg: "bg-amber-50",   icon: "text-amber-500",   val: "text-amber-800"   },
    slate:   { bg: "bg-slate-50",   icon: "text-slate-400",   val: "text-slate-700"   },
  };
  const a = MAP[accent] || MAP.indigo;
  return (
    <div className={`rounded-xl p-4 border border-slate-200 ${a.bg}`}>
      <div className="flex items-center justify-between mb-2">
        <span className="text-[10px] uppercase tracking-widest text-slate-500 font-bold leading-tight">{label}</span>
        {Icon && <Icon size={13} className={a.icon} />}
      </div>
      <div className={`text-2xl font-bold leading-none ${a.val}`}>{value ?? "—"}</div>
    </div>
  );
}

function MyDigestTab({ days }) {
  const [data,    setData]    = useState(null);
  const [loading, setLoading] = useState(true);
  const [err,     setErr]     = useState(null);
  const [taOpen,  setTaOpen]  = useState(null);   // expanded domain key
  const [recOpen, setRecOpen] = useState(null);   // expanded rec index

  useEffect(() => {
    setLoading(true); setErr(null); setData(null);
    authFetch(`${API_BASE}/coach/my-digest?days=${days}`)
      .then(r => r.ok ? r.json() : r.json().then(d => Promise.reject(d?.detail || "Failed")))
      .then(d => setData(d))
      .catch(e => setErr(String(e)))
      .finally(() => setLoading(false));
  }, [days]);

  if (loading) return (
    <div className="flex flex-col gap-4 animate-pulse max-w-5xl mx-auto">
      {[1,2,3].map(i => (
        <div key={i} className="h-32 bg-gradient-to-r from-slate-100 to-slate-50 rounded-2xl" style={{ opacity: 1 - i * 0.2 }} />
      ))}
    </div>
  );

  if (err) return (
    <div className="flex items-center gap-2 p-4 bg-red-50 border border-red-200 rounded-xl text-red-700 text-sm max-w-xl mx-auto mt-8">
      <AlertTriangle size={16} /> {err}
    </div>
  );

  if (!data) return null;

  const {
    overall, categories = {}, event_count, insufficient_data,
    recs = [], usage = {}, task_analysis, window_days,
    // top_violations is intentionally NOT destructured — the "Rule violations"
    // card was removed (it duplicated "Top improvement opportunities"). The
    // field still comes back from the API for other consumers.
  } = data;

  const hasCategories  = Object.keys(categories).length > 0;
  const hasRecs        = recs.length > 0;
  const hasTA          = task_analysis && (task_analysis.domains || []).length > 0;
  const hasUsage       = (usage.by_channel || []).length > 0 || (usage.by_model || []).length > 0;

  // ── Derived efficiency + focus metrics ───────────────────────────────
  // Tokens/cost per event turn raw volume into an efficiency signal — a user
  // with 1k events at 50 tok/event is very different from 1k at 5k tok/event.
  const totalTokens = (usage.tokens_in || 0) + (usage.tokens_out || 0);
  const eventsForAvg = event_count || usage.total_events || 0;
  const tokPerEvent  = eventsForAvg > 0 ? Math.round(totalTokens / eventsForAvg) : null;
  const costPerEvent = eventsForAvg > 0 && usage.cost_usd != null
    ? (Number(usage.cost_usd) / eventsForAvg) : null;

  // Single lowest-scoring category to spotlight as the one thing to fix first.
  // Plain derived const (not useMemo) — categories has ≤6 entries, so the sort
  // is trivially cheap and a hook here would have to sit above the early
  // returns to respect the Rules of Hooks. Falls back to null when empty.
  const focusArea = (() => {
    const entries = Object.entries(categories).filter(([, v]) => v != null);
    if (entries.length === 0) return null;
    entries.sort((a, b) => a[1] - b[1]);
    const [cat, score] = entries[0];
    return { cat, score, meta: CATEGORY_META[cat] || null };
  })();

  return (
    <div className="max-w-5xl mx-auto space-y-5">

      {/* ── Hero ──────────────────────────────────────────────────────── */}
      <div className="rounded-2xl p-6 text-white relative overflow-hidden"
           style={{ background: "linear-gradient(135deg,#4f46e5 0%,#7c3aed 60%,#6d28d9 100%)" }}>
        {/* decorative circles */}
        <div className="absolute -top-8 -right-8 w-40 h-40 rounded-full opacity-10"
             style={{ background: "radial-gradient(circle,#fff,transparent)" }} />
        <div className="absolute -bottom-6 -left-6 w-28 h-28 rounded-full opacity-10"
             style={{ background: "radial-gradient(circle,#fff,transparent)" }} />

        <div className="relative flex items-start justify-between gap-6 flex-wrap">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-2">
              <BarChart2 size={18} className="text-indigo-200" />
              <span className="text-[11px] font-bold uppercase tracking-widest text-indigo-200">
                My Digest · last {window_days} days
              </span>
            </div>
            <h2 className="text-xl font-bold leading-tight mb-1">Your AiNxt practice summary</h2>
            <p className="text-indigo-200 text-[12px] leading-relaxed max-w-lg">
              A complete picture of how you use AiNxt — your practice score, what types of work you do,
              where you can improve, and how much you've spent. Only you see this.
            </p>

            {/* Quick stats row */}
            <div className="flex flex-wrap gap-4 mt-4">
              {[
                { label: "Events",    value: event_count ?? usage.total_events ?? "—" },
                { label: "Spend",     value: usage.cost_usd != null ? `$${Number(usage.cost_usd).toFixed(4)}` : "—" },
                { label: "Tokens",    value: usage.tokens_in != null ? `${((usage.tokens_in + usage.tokens_out) / 1000).toFixed(1)}k` : "—" },
                // Efficiency signal: average tokens per event. Lower usually
                // means tighter, better-scoped prompts. Null when no events.
                { label: "Tok/event", value: tokPerEvent != null ? tokPerEvent.toLocaleString() : "—" },
                // Cost efficiency: average $ per event. Useful to compare
                // against the team / model mix. Null when no events or cost.
                { label: "$/event",   value: costPerEvent != null ? `$${costPerEvent.toFixed(4)}` : "—" },
              ].map(({ label, value }) => (
                <div key={label} className="text-center">
                  <div className="text-lg font-bold text-white leading-none">{value}</div>
                  <div className="text-[10px] text-indigo-300 uppercase tracking-wider mt-0.5">{label}</div>
                </div>
              ))}
            </div>
          </div>

          <DigestScoreRing score={overall} />
        </div>

        {insufficient_data && (
          <div className="relative mt-4 px-3 py-2 bg-white/10 rounded-xl text-[11px] text-indigo-100 border border-white/20">
            ℹ Not enough activity yet for a full score — keep using AiNxt and check back soon.
          </div>
        )}
      </div>

      {/* ── Focus area: the one category to improve first ─────────────── */}
      {/* Surfaces the lowest-scoring category with its hint so the user has a
          single concrete next step, not just a list of scores. Skipped when
          there are no category scores yet (insufficient data). */}
      {focusArea && (
        <div className="rounded-2xl p-4 bg-white border border-slate-200 shadow-sm flex items-start gap-3">
          <div className="w-9 h-9 rounded-xl bg-amber-50 border border-amber-200 flex items-center justify-center flex-shrink-0">
            <Target size={16} className="text-amber-600" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[10px] font-bold uppercase tracking-widest text-amber-600">
                Focus area
              </span>
              <span className="text-[13px] font-semibold text-slate-800">
                {(CATEGORY_LABEL[focusArea.cat] || focusArea.cat.replace(/-/g, " "))}
              </span>
              <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded-full ${scoreColor(focusArea.score).chip}`}>
                {focusArea.score}/100
              </span>
            </div>
            <p className="text-[11.5px] text-slate-600 mt-1 leading-relaxed">
              {focusArea.meta?.tooltip || focusArea.meta?.hint || "This is your lowest-scoring category this period."}
            </p>
          </div>
        </div>
      )}

      {/* ── Task-type breakdown ───────────────────────────────────────── */}
      {hasTA && (
        <DigestCard title="What type of work do you do?" icon={BarChart2} iconColor="text-violet-500">
          <p className="text-[11.5px] text-slate-500 mb-4 leading-relaxed">
            {task_analysis.summary}
          </p>

          {/* Domain bars */}
          <div className="space-y-2 mb-4">
            {task_analysis.domains.map(d => {
              const c = DOMAIN_COLOURS[d.domain] || DOMAIN_COLOURS.general;
              const isOpen = taOpen === d.domain;
              return (
                <div key={d.domain} className={`border rounded-xl overflow-hidden transition-all ${c.border}`}>
                  <button
                    onClick={() => setTaOpen(isOpen ? null : d.domain)}
                    className={`w-full flex items-center gap-3 px-4 py-3 text-left ${c.bg} hover:brightness-95 transition`}
                  >
                    {/* Domain badge */}
                    <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full flex-shrink-0 ${c.badge}`}>
                      {d.domain.toUpperCase()}
                    </span>
                    {/* Bar */}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center justify-between mb-1.5">
                        <span className={`text-[12px] font-semibold ${c.text}`}>{d.label}</span>
                        <span className="text-[10.5px] text-slate-500 ml-2 flex-shrink-0">
                          {d.count} interaction{d.count !== 1 ? "s" : ""} · <strong>{d.pct}%</strong>
                        </span>
                      </div>
                      <div className="h-2 bg-white/70 rounded-full overflow-hidden border border-white/40">
                        <div className="h-full rounded-full transition-all duration-700"
                             style={{ width: `${d.pct}%`, background: c.bar }} />
                      </div>
                    </div>
                    <ChevronRight size={14}
                      className={`flex-shrink-0 ${c.text} transition-transform duration-200 ${isOpen ? "rotate-90" : ""}`} />
                  </button>

                  {/* Expanded tips */}
                  {isOpen && (
                    <div className="px-4 pb-4 pt-3 bg-white border-t border-slate-100 space-y-3">
                      <p className="text-[11px] text-slate-500 font-medium">
                        Improvement tips for <strong>{d.label}</strong> work:
                      </p>
                      {(d.top_issues || []).length === 0 ? (
                        <div className="flex items-center gap-2 text-[11.5px] text-emerald-600">
                          <CheckCircle size={13} /> No recurring issues detected — great work!
                        </div>
                      ) : (
                        <div className="space-y-2.5">
                          {d.top_issues.map((issue, idx) => (
                            <div key={idx} className="flex items-start gap-2.5">
                              <span className={`text-[9.5px] font-bold uppercase tracking-wider px-1.5 py-0.5
                                              rounded-full border flex-shrink-0 mt-0.5 ${SEV_STYLE.medium}`}>
                                {issue.category.replace(/-/g, " ")}
                              </span>
                              <div>
                                <p className="text-[11.5px] text-slate-700 leading-relaxed">{issue.tip}</p>
                                <p className="text-[10px] text-slate-400 mt-0.5">
                                  Triggered {issue.count} time{issue.count !== 1 ? "s" : ""} this period
                                </p>
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {/* Compact one-line summary instead of a redundant table — the bars
              above already show count, share and per-domain issues, so a full
              table just repeated them. This line gives the aggregate at a glance. */}
          <div className="flex items-center justify-between gap-3 mt-3 px-4 py-2.5 rounded-xl bg-slate-50/70 border border-slate-100 text-[11px]">
            <span className="text-slate-500">
              <strong className="text-slate-700">{task_analysis.domains.reduce((s, d) => s + (d.count || 0), 0)}</strong> interaction{task_analysis.domains.reduce((s, d) => s + (d.count || 0), 0) !== 1 ? "s" : ""} across{" "}
              <strong className="text-slate-700">{task_analysis.domains.length}</strong> task type{task_analysis.domains.length !== 1 ? "s" : ""}
            </span>
            <span className="text-slate-500">
              {task_analysis.domains.filter(d => (d.top_issues || []).length === 0).length} clean ·{" "}
              <span className="text-amber-600 font-medium">
                {task_analysis.domains.filter(d => (d.top_issues || []).length > 0).length} with issues
              </span>
            </span>
          </div>
        </DigestCard>
      )}

      {/* ── Top recommendations ───────────────────────────────────────── */}
      {hasRecs && (
        <DigestCard title="Top improvement opportunities" icon={Zap} iconColor="text-amber-500">
          <p className="text-[11.5px] text-slate-500 mb-4 leading-relaxed">
            These are the patterns that appear most often in your recent activity.
            Fixing them will have the biggest impact on your practice score.
          </p>
          <div className="space-y-2">
            {recs.map((r, i) => {
              const isOpen = recOpen === i;
              return (
                <div key={i} className="border border-slate-200 rounded-xl overflow-hidden">
                  <button
                    onClick={() => setRecOpen(isOpen ? null : i)}
                    className="w-full flex items-start gap-3 px-4 py-3 text-left hover:bg-slate-50/60 transition"
                  >
                    <span className="w-6 h-6 rounded-full bg-indigo-100 text-indigo-700 text-[10px] font-bold
                                     flex items-center justify-center flex-shrink-0 mt-0.5">
                      {i + 1}
                    </span>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-[12.5px] font-semibold text-slate-800">{r.title}</span>
                        <span className={`text-[9.5px] font-bold uppercase px-1.5 py-0.5 rounded-full border ${SEV_STYLE[r.severity] || SEV_STYLE.low}`}>
                          {r.severity}
                        </span>
                        <span className="text-[10px] text-slate-400">
                          {r.count} occurrence{r.count !== 1 ? "s" : ""}
                        </span>
                      </div>
                      {!isOpen && (
                        <p className="text-[11px] text-slate-500 mt-0.5 line-clamp-1">{r.advice}</p>
                      )}
                    </div>
                    <ChevronRight size={14}
                      className={`flex-shrink-0 text-slate-400 transition-transform duration-200 mt-0.5 ${isOpen ? "rotate-90" : ""}`} />
                  </button>
                  {isOpen && (
                    <div className="px-4 pb-4 pt-1 bg-slate-50/40 border-t border-slate-100">
                      <p className="text-[12px] text-slate-700 leading-relaxed">{r.advice}</p>
                      <div className="flex items-center gap-3 mt-2 flex-wrap">
                        <span className="text-[10px] text-slate-400">
                          Category: <strong className="text-slate-600">{r.category.replace(/-/g, " ")}</strong>
                        </span>
                        <span className="text-[10px] text-slate-400">
                          Impact score: <strong className="text-slate-600">{r.impact}</strong>
                        </span>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </DigestCard>
      )}

      {/* ── Empty state ───────────────────────────────────────────────── */}
      {/* Note: the "Rule violations" card was removed — it duplicated the
          "Top improvement opportunities" card (same CoachRuleHit rows, same
          rule_id grouping). The opportunities card already shows severity,
          count, category and advice, plus the impact score, so the flat
          violations table added nothing. */}
      {!hasCategories && !hasRecs && !hasTA && !hasUsage && (
        <div className="flex flex-col items-center justify-center py-20 text-slate-400 gap-3">
          <BarChart2 size={40} className="text-slate-300" />
          <p className="text-sm font-medium">No activity yet in the last {window_days} days.</p>
          <p className="text-[11px] text-slate-400">Start using AiNxt and your digest will appear here.</p>
        </div>
      )}
    </div>
  );
}

// =============================================================================

const FLAG_FAMILY = {
  pii:        { cls: "bg-amber-100  text-amber-800  border-amber-200",  label: "PII" },
  secret:     { cls: "bg-red-100    text-red-800    border-red-200",    label: "Secret" },
  compliance: { cls: "bg-orange-100 text-orange-800 border-orange-200", label: "Compliance" },
};
// Driving the badge row from this array eliminates 3 near-identical JSX blocks
// and keeps render order stable across re-renders.
const SESSION_FLAG_FAMILIES = ["pii", "secret", "compliance"];

function ExplorerTab({ days }) {
  const [sessions, setSessions] = useState([]);
  const [rules, setRules]   = useState([]);
  const [channel, setChannel] = useState("");
  const [loading, setLoading] = useState(true);
  // Two independent expansion sets so opening a prompt's coaching panel never
  // collapses its parent session.
  const [expandedSessions, toggleSession] = useToggleSet();
  const [expandedEvents,   toggleEvent]   = useToggleSet();

  useEffect(() => {
    setLoading(true);
    // NOTE: recommend is intentionally NOT requested here. Computing a per-prompt
    // model recommendation runs the auto-router (which can call the LLM-backed
    // classifier) once per event — up to 200 LLM calls on a single list load,
    // which made the Query Explorer extremely slow. The recommendation is now
    // fetched lazily per event when the user expands it (see EventCoachingPanel).
    const qs = new URLSearchParams({
      days, limit: 200, group_by: "thread",
    });
    if (channel) qs.set("channel", channel);
    Promise.all([
      authFetch(`${API_BASE}/coach/events?${qs}`).then(r => r.ok ? r.json() : { sessions: [] }),
      authFetch(`${API_BASE}/coach/rules`).then(r => r.ok ? r.json() : { rules: [] }),
    ]).then(([s, r]) => { setSessions(s.sessions || []); setRules(r.rules || []); })
      .finally(() => setLoading(false));
  }, [days, channel]);

  const ruleById = useMemo(() => Object.fromEntries(rules.map(r => [r.id, r])), [rules]);

  const totalEvents = sessions.reduce((acc, s) => acc + (s.event_count || 0), 0);

  return (
    <div className="space-y-3 max-w-6xl mx-auto">
      <div className="flex items-center gap-2 flex-wrap">
        <select value={channel} onChange={e => setChannel(e.target.value)}
                className="text-[11px] border border-slate-200 rounded-lg px-2.5 py-1.5 bg-white shadow-sm cursor-pointer hover:border-slate-300 transition">
          <option value="">All channels</option>
          {Object.keys(CHANNEL_LABEL).map(c =>
            <option key={c} value={c}>{fmtChannel(c)}</option>
          )}
        </select>
        <span className="text-[11px] text-slate-500 font-medium">
          {sessions.length} session{sessions.length === 1 ? "" : "s"} · {totalEvents} prompt{totalEvents === 1 ? "" : "s"}
        </span>
        <span className="ml-auto text-[10.5px] text-slate-400 italic flex items-center gap-1">
          <ChevronRight size={11} /> Click a session to see its prompts.
        </span>
      </div>

      {loading ? <Skeleton /> : (
        <div className="bg-white border border-slate-200 rounded-2xl shadow-[0_1px_2px_rgba(15,23,42,0.04)] overflow-hidden">
          {sessions.length === 0 ? (
            <div className="text-center py-12 text-slate-400 text-xs">No sessions.</div>
          ) : (
            <ul className="divide-y divide-slate-100">
              {sessions.map((s, idx) => {
                // Synthetic key — use thread_id when present; for unthreaded /
                // cross-channel merged sessions use the first event_id + index
                // so multiple null-thread sessions don't collide on the same key.
                const firstEventId = (s.events && s.events[0] && s.events[0].event_id) || idx;
                const key = s.thread_id || `__unthreaded__${firstEventId}`;
                const isOpen = expandedSessions.has(key);
                return (
                  <SessionRow
                    key={key}
                    session={s}
                    isOpen={isOpen}
                    onToggle={() => toggleSession(key)}
                    expandedEvents={expandedEvents}
                    onToggleEvent={toggleEvent}
                    ruleById={ruleById}
                  />
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

// Per-channel colour scheme for the session heading pill. Label text is derived
// from CHANNEL_LABEL at render time so the two maps cannot drift.
const CHANNEL_PILL_CLS = {
  web:      "bg-indigo-50 text-indigo-700 border border-indigo-200",
  cli:      "bg-emerald-50 text-emerald-700 border border-emerald-200",
  mcp:      "bg-sky-50 text-sky-700 border border-sky-200",
  api:      "bg-violet-50 text-violet-700 border border-violet-200",
  teams:    "bg-blue-50 text-blue-700 border border-blue-200",
  slack:    "bg-purple-50 text-purple-700 border border-purple-200",
  voice:    "bg-pink-50 text-pink-700 border border-pink-200",
  workflow: "bg-amber-50 text-amber-700 border border-amber-200",
  agent:    "bg-orange-50 text-orange-700 border border-orange-200",
  embed:    "bg-teal-50 text-teal-700 border border-teal-200",
};

function SessionRow({ session, isOpen, onToggle, expandedEvents, onToggleEvent, ruleById }) {
  const isUnthreaded   = session.thread_id == null;
  const channels       = session.channels        ?? [];
  const clientSources  = session.client_sources  ?? [];
  const ruleHitsUnion  = session.rule_hits_union ?? [];
  const ruleHitCount   = ruleHitsUnion.length;
  const range          = formatSessionRange(session.first_ts, session.last_ts);
  const tokensTotal    = (session.tokens_in_total || 0) + (session.tokens_out_total || 0);
  const costStr        = `$${(session.cost_usd_total || 0).toFixed(4)}`;

  const primaryChannel = (channels[0] || "").toLowerCase();
  const pillCls        = CHANNEL_PILL_CLS[primaryChannel];

  return (
    <li className={isOpen ? "bg-indigo-50/30" : ""}>
      <button
        type="button"
        onClick={onToggle}
        className="w-full text-left px-4 py-3 hover:bg-slate-50/60 cursor-pointer transition flex items-start gap-3"
      >
        <div className={`mt-1 transition-transform ${isOpen ? "rotate-90 text-indigo-500" : "text-slate-400"}`}>
          <ChevronRight size={13} />
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            {pillCls && (
              <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded uppercase tracking-wider flex-shrink-0 ${pillCls}`}>
                {fmtChannel(primaryChannel)}
              </span>
            )}
            <span className={`text-[12.5px] font-semibold truncate ${isUnthreaded ? "text-slate-500 italic" : "text-slate-800"}`}>
              {session.title}
            </span>

            {SESSION_FLAG_FAMILIES.map(family => {
              const items = session[`${family}_flags_union`] ?? [];
              if (items.length === 0) return null;
              return <FlagBadge key={family} family={family} count={items.length} items={items} />;
            })}
            {ruleHitCount > 0 && (
              <span className="text-[9.5px] font-semibold px-1.5 py-0.5 rounded-full border bg-indigo-50 text-indigo-700 border-indigo-200"
                    title={ruleHitsUnion.map(h => h.id || h).join(", ")}>
                {ruleHitCount} rule hit{ruleHitCount === 1 ? "" : "s"}
              </span>
            )}
          </div>

          <div className="mt-1 flex items-center gap-3 flex-wrap text-[10.5px] text-slate-500">
            <span className="font-mono">
              {session.event_count} prompt{session.event_count === 1 ? "" : "s"}
            </span>
            {range && <span className="font-mono">{range}</span>}
            {/* Show extra channel chips only when session spans multiple channels */}
            {channels.length > 1 && (
              <span className="flex items-center gap-1">
                {channels.map(c => (
                  <span key={c} className="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 text-[9.5px] font-medium">
                    {fmtChannel(c)}
                  </span>
                ))}
              </span>
            )}
            <span className="font-mono">{tokensTotal} tok</span>
            <span className="font-mono">{costStr}</span>
          </div>
        </div>
      </button>

      {isOpen && (
        <div className="border-t border-indigo-100/60 bg-slate-50/40">
          <div className="divide-y divide-slate-100">
            {(session.events || []).map((e, idx) => {
              const evOpen  = expandedEvents.has(e.event_id);
              const hasHits = (e.rule_hits || []).length > 0;
              const hits    = e.rule_hits || [];
              // Highest severity for the left accent bar
              const sevOrder = { critical: 0, high: 1, medium: 2, low: 3 };
              const topSev   = hits.length
                ? hits.reduce((a, b) =>
                    (sevOrder[a.severity] ?? 9) < (sevOrder[b.severity] ?? 9) ? a : b
                  ).severity
                : null;
              const accentColor = {
                critical: "bg-red-400",
                high:     "bg-orange-400",
                medium:   "bg-amber-400",
                low:      "bg-indigo-300",
              }[topSev] || "bg-slate-200";

              const tokTotal = (e.tokens_in || 0) + (e.tokens_out || 0);
              const costStr  = _isLocalModel(e.model)
                ? <span className="text-emerald-600">$0.00</span>
                : <span>${(e.cost_usd || 0).toFixed(4)}</span>;

              return (
                <div key={e.event_id}
                     className={`relative transition-colors ${evOpen ? "bg-indigo-50/50" : "bg-white hover:bg-slate-50/80"}`}>
                  {/* Sequence number + left accent bar */}
                  <div className={`absolute left-0 top-0 bottom-0 w-0.5 ${accentColor}`} />
                  <div className="absolute left-3 top-3 text-[9px] font-bold text-slate-300 select-none">
                    {String(idx + 1).padStart(2, "0")}
                  </div>

                  {/* Clickable prompt row */}
                  <button
                    type="button"
                    onClick={() => onToggleEvent(e.event_id)}
                    className="w-full text-left pl-8 pr-4 pt-3 pb-2.5 cursor-pointer"
                  >
                    {/* Prompt text — prominent */}
                    <div className="flex items-start gap-2">
                      <div className={`mt-0.5 flex-shrink-0 transition-transform ${evOpen ? "rotate-90 text-indigo-500" : "text-slate-300"}`}>
                        <ChevronRight size={12} />
                      </div>
                      <p className={`text-[12px] font-medium leading-snug flex-1 ${
                        evOpen ? "text-indigo-700" : "text-slate-800"
                      } ${!evOpen ? "line-clamp-2" : ""}`}>
                        {e.prompt_redacted || <span className="text-slate-300 italic">empty prompt</span>}
                      </p>
                    </div>

                    {/* Meta row — time · channel · model · tokens · cost · rule hits */}
                    <div className="mt-1.5 pl-5 flex items-center gap-2.5 flex-wrap">
                      {/* Time */}
                      <span className="flex items-center gap-1 text-[10px] text-slate-400 font-mono">
                        <Clock size={9} />
                        {e.ts ? toISTTimeShort(e.ts) : "—"}
                      </span>
                      {/* Channel chip */}
                      <span className="text-[9px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 font-medium">
                        {fmtChannel(e.channel)}
                      </span>
                      {/* Model — truncated */}
                      {e.model && (
                        <span className="flex items-center gap-1 text-[10px] text-slate-500 font-mono max-w-[140px] truncate" title={e.model}>
                          <Cpu size={9} className="flex-shrink-0" />
                          {e.model}
                        </span>
                      )}
                      {/* Tokens */}
                      <span className="flex items-center gap-1 text-[10px] text-slate-400 font-mono">
                        <Zap size={9} />
                        {tokTotal.toLocaleString()}
                      </span>
                      {/* Cost */}
                      <span className="flex items-center gap-1 text-[10px] font-mono text-slate-400">
                        <DollarSign size={9} />
                        {costStr}
                      </span>
                      {/* Recommendation mismatch badge */}
                      {e.recommendation && (
                        <RecommendCell rec={e.recommendation} compact />
                      )}
                      {/* Rule hit pills */}
                      {hits.length > 0 && (
                        <span className="flex items-center gap-1 flex-wrap">
                          {hits.slice(0, 3).map(h => (
                            <span key={h.id}
                                  className={`text-[9px] font-semibold px-1.5 py-0.5 rounded-full ${SEVERITY_STYLE[h.severity] || "bg-slate-100 text-slate-700"}`}
                                  title={`${h.name || h.id} (${h.id})`}>
                              {h.code || h.id}
                            </span>
                          ))}
                          {hits.length > 3 && (
                            <span className="text-[9px] text-slate-400 font-mono">+{hits.length - 3}</span>
                          )}
                        </span>
                      )}
                      {/* LLM Judge verdict badge */}
                      <EvalJudgeBadge verdict={e.eval_verdict} score={e.eval_score} />
                    </div>
                  </button>

                  {/* Expanded coaching panel */}
                  {evOpen && (
                    <div className="pl-8 pr-4 pb-4 border-t border-indigo-100/60 bg-gradient-to-r from-indigo-50/30 to-violet-50/20">
                      <div className="pt-3 border-l-2 border-indigo-300 pl-3">
                        <EventCoachingPanel event={e} hasHits={hasHits} ruleById={ruleById} />
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </li>
  );
}

function FlagBadge({ family, count, items }) {
  const style = FLAG_FAMILY[family];
  if (!style) return null;
  // Flag entries are either plain strings (pii/secret/compliance codes) or
  // {id, severity, ...} dicts (rule_hits); fall back to JSON for safety.
  const tooltip = (items || [])
    .map(i => (typeof i === "string" ? i : (i?.id || JSON.stringify(i))))
    .join(", ");
  return (
    <span
      className={`text-[9.5px] font-semibold px-1.5 py-0.5 rounded-full border ${style.cls}`}
      title={tooltip}
    >
      {style.label} · {count}
    </span>
  );
}

// ── ISO-8601 timestamp range → compact IST human label ─────────────────────
// Same day: "09:41 AM → 11:07 AM IST"  ·  Cross-day: "18 Jun 09:41 AM → 19 Jun 02:13 PM IST"
// Day comparison is done in IST (Asia/Kolkata) so a session that straddles UTC
// midnight but stays inside one IST day still reads as same-day.
function formatSessionRange(first, last) {
  if (!first && !last) return "";
  if (!first) return toISTShort(last);
  if (!last)  return toISTShort(first);
  const fDay = toISTDate(first);
  const lDay = toISTDate(last);
  if (fDay === lDay) {
    return `${toISTTimeShort(first)} → ${toISTTimeShort(last)} IST`;
  }
  return `${toISTShort(first)} → ${toISTShort(last)} IST`;
}


// Expanded-row body: shows the full prompt, the rules that fired with their
// remediation + example, and an on-demand LLM-rewrite call.
function EventCoachingPanel({ event, hasHits, ruleById }) {
  const [suggestion, setSuggestion] = useState(null);
  const [loading, setLoading]       = useState(false);
  const [err, setErr]               = useState("");

  // Recommendation is fetched lazily — only when this panel mounts (i.e. the
  // user expanded this event). This avoids running the auto-router (and its
  // LLM-backed classifier) for every event during the Query Explorer list load.
  const [recommendation, setRecommendation] = useState(event.recommendation || null);
  const preloadedRec = event.recommendation;
  useEffect(() => {
    if (preloadedRec || !event.event_id) return;
    let cancelled = false;
    authFetch(`${API_BASE}/coach/events/${event.event_id}/recommendation`)
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (!cancelled && d) setRecommendation(d.recommendation || null); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [event.event_id, preloadedRec]);

  const hitsMeta = (event.rule_hits || [])
    .map(h => ({ ...h, meta: ruleById[h.id] }))
    .filter(h => h.meta);

  const requestSuggestion = () => {
    setLoading(true); setErr(""); setSuggestion(null);
    authFetch(`${API_BASE}/coach/suggest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event_id: event.event_id }),
    })
      .then(r => r.ok ? r.json() : r.text().then(t => Promise.reject(t)))
      .then(setSuggestion)
      .catch(e => setErr(String(e)))
      .finally(() => setLoading(false));
  };

  return (
    <div className="space-y-4">
      {/* ── Original prompt section ─────────────────────────────── */}
      <div>
        <SectionLabel>Original prompt</SectionLabel>
        <div className="p-3 bg-white border border-slate-200 rounded-lg text-[11.5px] text-slate-800 font-mono whitespace-pre-wrap leading-relaxed">
          {event.prompt_redacted || <span className="text-slate-400">(empty)</span>}
        </div>
      </div>

      {/* ── Recommended model for this prompt ──────────────────── */}
      {recommendation && (
        <RecommendBlock used={event.model} rec={recommendation} />
      )}

      {/* ── Rules fired ─────────────────────────────────────────── */}
      {hasHits ? (
        <div>
          <SectionLabel>
            Rules fired
            <span className="ml-1 text-slate-400 font-normal normal-case">({hitsMeta.length})</span>
          </SectionLabel>
          <ul className="space-y-1.5">
            {hitsMeta.map(h => (
              <li key={h.id} className="p-2.5 bg-white border border-slate-200 rounded-lg hover:border-slate-300 transition">
                <div className="flex items-center gap-2 text-[11.5px]">
                  <span className={`text-[9px] px-1.5 py-0.5 rounded-full font-bold uppercase tracking-wider ${SEVERITY_STYLE[h.severity] || "bg-slate-100 text-slate-700"}`}>
                    {h.severity}
                  </span>
                  <code className="text-slate-700 font-mono font-semibold" title={h.id}>{h.code || h.id}</code>
                  <span className="text-slate-600">{h.meta.name}</span>
                </div>
                {h.meta.remediation && (
                  <div className="mt-1.5 text-[11px] text-slate-600 leading-snug pl-1 border-l-2 border-indigo-200 ml-0.5">
                    <span className="ml-2">→ {h.meta.remediation}</span>
                  </div>
                )}
                {h.meta.example_prompt && (
                  <div className="mt-1 text-[10px] text-slate-400 italic pl-3">e.g. {h.meta.example_prompt}</div>
                )}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="text-[11.5px] text-emerald-700 flex items-center gap-1.5 px-2 py-1.5 bg-emerald-50 border border-emerald-100 rounded-lg">
          <CheckCircle size={13} /> No rules fired on this turn — looks clean.
        </div>
      )}

      {/* ── LLM Judge results ───────────────────────────────────── */}
      <EvalJudgePanel
        verdict={event.eval_verdict}
        score={event.eval_score}
        issues={event.eval_issues}
      />

      {/* ── Magic suggest CTA ───────────────────────────────────── */}
      <div>
        {/* Button is disabled while loading OR when all LLMs are known-unavailable */}
        <button
          onClick={requestSuggestion}
          disabled={loading || suggestion?.source === "unavailable"}
          title={suggestion?.source === "unavailable" ? "All LLM services are currently unreachable" : undefined}
          className="group relative text-[11.5px] font-medium px-3 py-2 brand-grad-r text-white rounded-lg hover:shadow-md hover:shadow-indigo-200 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer flex items-center gap-1.5 transition"
        >
          <Sparkles size={12} className="group-hover:rotate-12 transition-transform" />
          {loading
            ? "Asking the model…"
            : suggestion?.source === "unavailable"
              ? "LLM unavailable"
              : suggestion
                ? "Re-generate suggestion"
                : "Suggest a better prompt"}
        </button>
        {err && <div className="text-[11px] text-red-600 mt-2">Could not generate a suggestion. Please try again.</div>}
        {suggestion && suggestion.source !== "unavailable" && (
          <div className="mt-2.5 p-3 bg-white border border-indigo-200 rounded-lg shadow-[0_1px_2px_rgba(79,70,229,0.06)]">
            <div className="text-[10px] uppercase tracking-widest text-indigo-500 font-bold mb-1.5 flex items-center gap-1.5">
              <Sparkles size={10} className="text-indigo-400" />
              Suggested rewrite
              {suggestion.source === "fallback" && (
                <span className="text-slate-400 normal-case italic font-normal">(rule-based)</span>
              )}
            </div>
            <div className="text-[11.5px] text-slate-800 font-mono whitespace-pre-wrap leading-relaxed">
              {suggestion.rewritten || "(no suggestion produced)"}
            </div>
            {suggestion.why && (
              <div className="mt-2 text-[10.5px] text-slate-500 italic">
                <span className="font-semibold not-italic text-slate-600">Why:</span> {suggestion.why}
              </div>
            )}
            {suggestion.notice && (
              <div className="mt-2 text-[10.5px] text-amber-700 bg-amber-50 border border-amber-100 rounded-md px-2 py-1">
                {suggestion.notice}
              </div>
            )}
          </div>
        )}
        {suggestion?.source === "unavailable" && (
          <div className="mt-2 text-[10.5px] text-amber-700 bg-amber-50 border border-amber-100 rounded-md px-2.5 py-1.5 flex items-center gap-1.5">
            <span className="font-semibold">LLM services unreachable.</span>
            {suggestion.notice && <span>{suggestion.notice}</span>}
            <span className="text-amber-600">Try again later.</span>
          </div>
        )}
      </div>
    </div>
  );
}

// ── EvalJudgeBadge — compact ACCEPT / REJECT pill shown in the meta row ──────
// eval_verdict is null until the async judge thread completes (~15 s after
// ingestion). When null we render nothing so the row doesn't flicker.
function EvalJudgeBadge({ verdict, score }) {
  if (!verdict) return null;
  const isAccept = verdict === "ACCEPT";
  return (
    <span
      className={`inline-flex items-center gap-1 text-[9px] font-bold px-1.5 py-0.5 rounded-full border ${
        isAccept
          ? "bg-emerald-50 text-emerald-700 border-emerald-200"
          : "bg-red-50 text-red-700 border-red-200"
      }`}
      title={`LLM Judge: ${verdict}${score != null ? ` (${Math.round(score * 100)}/100)` : ""}`}
    >
      {isAccept ? <CheckCircle size={8} /> : <AlertTriangle size={8} />}
      {isAccept ? "ACCEPT" : "REJECT"}
      {score != null && (
        <span className="opacity-70 font-normal">{Math.round(score * 100)}</span>
      )}
    </span>
  );
}

// ── EvalJudgePanel — full section inside the expanded coaching panel ──────────
// Shows the LLM judge score bar, verdict, and the list of specific issues.
function EvalJudgePanel({ verdict, score, issues }) {
  // Don't render if the judge hasn't run yet (null) or if it's a clean ACCEPT
  // with no issues — keep the panel noise-free.
  if (!verdict) return null;

  const isAccept  = verdict === "ACCEPT";
  const pct       = score != null ? Math.round(score * 100) : null;
  const issueList = Array.isArray(issues) ? issues.filter(Boolean) : [];

  return (
    <div>
      <SectionLabel>
        LLM Judge
        <span className={`ml-2 text-[9px] font-bold px-1.5 py-0.5 rounded-full ${
          isAccept
            ? "bg-emerald-100 text-emerald-700"
            : "bg-red-100 text-red-700"
        }`}>
          {verdict}
        </span>
      </SectionLabel>

      {/* Score bar */}
      {pct != null && (
        <div className="mb-2">
          <div className="flex items-center justify-between mb-1">
            <span className="text-[10px] text-slate-500">Prompt quality score</span>
            <span className={`text-[11px] font-bold ${
              pct >= 70 ? "text-emerald-600" : pct >= 40 ? "text-amber-600" : "text-red-600"
            }`}>{pct}/100</span>
          </div>
          <div className="h-1.5 bg-slate-100 rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all ${
                pct >= 70 ? "bg-emerald-400" : pct >= 40 ? "bg-amber-400" : "bg-red-400"
              }`}
              style={{ width: `${pct}%` }}
            />
          </div>
        </div>
      )}

      {/* Issues list */}
      {issueList.length > 0 ? (
        <ul className="space-y-1">
          {issueList.map((issue, i) => (
            <li key={i} className="flex items-start gap-1.5 text-[11px] text-slate-700 bg-white border border-slate-200 rounded-lg px-2.5 py-1.5">
              <AlertTriangle size={10} className="text-amber-500 flex-shrink-0 mt-0.5" />
              {issue}
            </li>
          ))}
        </ul>
      ) : isAccept ? (
        <div className="text-[11px] text-emerald-700 flex items-center gap-1.5 px-2 py-1.5 bg-emerald-50 border border-emerald-100 rounded-lg">
          <CheckCircle size={12} /> Prompt passed all 6 LLM judge criteria.
        </div>
      ) : null}
    </div>
  );
}

// Small section heading used inside the expanded coaching panel.
function SectionLabel({ children }) {
  return (
    <div className="text-[10px] uppercase tracking-widest text-slate-500 font-bold mb-1.5">
      {children}
    </div>
  );
}

// ── Verdict styles — dot colour is the primary signal in the compact table row.
// A badge only appears for over_spent (actionable cost warning) and
// different_tier (clarifies the mismatch type). Everything else is dot-only;
// the full hint lives in the tooltip and the expanded RecommendBlock panel.
const _VERDICT_STYLE = {
  match:          { dot: "bg-emerald-400", text: "text-slate-600"  },
  good_local:     { dot: "bg-emerald-500", text: "text-slate-600"  },
  over_spent:     { dot: "bg-amber-400",   text: "text-amber-700",
                    badge: "paid model used",  badgeCls: "bg-amber-100 text-amber-700" },
  under_spent:    { dot: "bg-sky-400",     text: "text-slate-600"  },
  different_tier: { dot: "bg-violet-400",  text: "text-slate-600",
                    badge: "≠ tier",           badgeCls: "bg-violet-100 text-violet-700" },
  unknown:        { dot: "bg-slate-300",   text: "text-slate-400"  },
};

// Helper: is this model a local/free model?
function _isLocalModel(modelName) {
  if (!modelName) return false;
  const n = modelName.toLowerCase();
  return n === "local" || n.startsWith("local (") || n.includes("local-llm") ||
         n.includes("ollama") || n.includes("kimi") || n.includes("glm-") ||
         n.includes("qwen") || n.includes("llama") || n.includes("in-house");
}

function RecommendCell({ rec, compact = false }) {
  if (!rec) return compact ? null : <span className="text-slate-300">—</span>;
  const style   = _VERDICT_STYLE[rec.verdict] || _VERDICT_STYLE.unknown;
  const tooltip = rec.hint || rec.reason || "";
  // compact=true: only show the mismatch badge (used in card meta row)
  if (compact) {
    if (!style.badge) return null;
    return (
      <span className={`text-[9px] font-semibold px-1.5 py-0.5 rounded flex-shrink-0 ${style.badgeCls}`}
            title={tooltip}>
        {style.badge}
      </span>
    );
  }
  return (
    <div className="flex items-center gap-1.5 min-w-0" title={tooltip}>
      <span className={`inline-block w-1.5 h-1.5 rounded-full flex-shrink-0 ${style.dot}`} />
      <span className={`font-mono truncate text-[11px] ${style.text}`}>
        {rec.recommended_model}
      </span>
      {style.badge && (
        <span className={`text-[9px] font-semibold px-1.5 py-0.5 rounded flex-shrink-0 ${style.badgeCls}`}>
          {style.badge}
        </span>
      )}
    </div>
  );
}

// ── Recommendation block (full, in the expanded coaching panel) ────────────
function RecommendBlock({ used, rec }) {
  if (!rec) return null;
  const style = _VERDICT_STYLE[rec.verdict] || _VERDICT_STYLE.unknown;
  const blocked = ["budget_blocked", "budget_exceeded", "compliance_blocked"].includes(String(used || "").toLowerCase());

  // Choose callout style by verdict.
  const callout = (() => {
    if (rec.verdict === "good_local")
      return { wrap: "bg-emerald-50 border-emerald-100 text-emerald-800", icon: "✓" };
    if (rec.verdict === "over_spent")
      return { wrap: "bg-amber-50 border-amber-100 text-amber-800",   icon: "💰" };
    if (rec.verdict === "under_spent")
      return { wrap: "bg-sky-50 border-sky-100 text-sky-800",         icon: "💡" };
    if (rec.verdict === "different_tier")
      return { wrap: "bg-violet-50 border-violet-100 text-violet-800", icon: "ℹ" };
    return null;
  })();

  // Show the callout when there's a hint to deliver — including good_local
  // (which has a positive message), so we don't only call out negatives.
  const showCallout = !!rec.hint;

  return (
    <div>
      <SectionLabel>Recommended model for this prompt</SectionLabel>
      <div className="p-3 bg-white border border-slate-200 rounded-lg">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={`inline-block w-2 h-2 rounded-full ${style.dot}`} />
          <code className="font-mono font-semibold text-slate-800 text-[12px]">{rec.recommended_model}</code>
          <span className="text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-slate-100 text-slate-600">
            {rec.tier}
          </span>
          {blocked ? (
            <span className="text-[10.5px] text-amber-600 ml-auto">
              Request was blocked before model selection
            </span>
          ) : used && used !== rec.recommended_model && (
            <span className="text-[10.5px] text-slate-500 ml-auto">
              You used <code className="font-mono text-slate-700">{used}</code>
            </span>
          )}
        </div>
        <div className="mt-2 text-[11px] text-slate-600 leading-snug">
          {rec.reason}
        </div>
        {showCallout && callout && (
          <div className={`mt-2 text-[11px] px-2 py-1.5 rounded-md border leading-snug ${callout.wrap}`}>
            {callout.icon} {rec.hint}
          </div>
        )}
      </div>
    </div>
  );
}

// =============================================================================
// Models tab — per-model + per-channel usage breakdown as donut charts.
// =============================================================================
function ModelsTab({ days }) {
  const [data, setData]       = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr]         = useState(null);

  useEffect(() => {
    setLoading(true); setErr(null);
    authFetch(`${API_BASE}/coach/usage?days=${days}`)
      .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
      .then(setData)
      .catch(e => setErr(String(e)))
      .finally(() => setLoading(false));
  }, [days]);

  if (loading) return <Skeleton />;
  if (err)     return <ErrorBox msg={err} />;
  if (!data)   return null;

  const models   = data.by_model || [];
  const channels = data.by_channel || [];
  const noData   = (data.totals?.events || 0) === 0;

  return (
    <div className="space-y-6 max-w-6xl mx-auto">
      {/* Header KPIs */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Stat label="Total events"  value={data.totals?.events ?? 0} icon={Activity} />
        <Stat label="Total tokens"  value={(data.totals?.tokens ?? 0).toLocaleString()} icon={Cpu} />
        <Stat label="Total cost"    value={`$${(data.totals?.cost_usd ?? 0).toFixed(4)}`} icon={Sparkles} />
      </div>

      {noData ? (
        <Card title="Models">
          <EmptyText>No usage in the last {data.window_days} days yet.</EmptyText>
        </Card>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <Card title="Share by model" subtle="Percentage of events by the model that answered.">
            <DonutWithLegend
              items={models.map(m => ({ name: m.name, value: m.count, pct: m.pct }))}
            />
          </Card>

          {/* Channel breakdown — full-width table: segment / cost / share / requests */}
          <Card title="Channel Breakdown" subtle="Cost, share, and request count per originating channel.">
            <ChannelBreakdown channels={channels} totalCost={data.totals?.cost_usd ?? 0} />
          </Card>
        </div>
      )}

      {/* Detailed per-model table — tokens & cost */}
      {!noData && (
        <Card title="Per-model detail" subtle="Sorted by event count.">
          <div className="overflow-x-auto -mx-1">
            <table className="w-full text-[11.5px]">
              <thead>
                <tr className="text-slate-400 uppercase text-[9.5px] tracking-widest font-semibold border-b border-slate-100">
                  <th className="px-3 py-2.5 text-left">Model</th>
                  <th className="px-3 py-2.5 text-right">Events</th>
                  <th className="px-3 py-2.5 text-right">Tokens in</th>
                  <th className="px-3 py-2.5 text-right">Tokens out</th>
                  <th className="px-3 py-2.5 text-right">Cost (USD)</th>
                </tr>
              </thead>
              <tbody>
                {models.map(m => (
                  <tr key={m.name}
                      className="border-b border-slate-50 last:border-b-0 hover:bg-slate-50/50 transition">
                    <td className="px-3 py-2.5">
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="inline-block w-2 h-2 rounded-full flex-shrink-0"
                              style={{ background: colorForIndex(models.indexOf(m)) }} />
                        <span className="truncate font-mono text-slate-700" title={m.name}>{m.name}</span>
                      </div>
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono text-slate-900 font-semibold">{m.count}</td>
                    <td className="px-3 py-2.5 text-right font-mono text-slate-500">{m.tokens_in.toLocaleString()}</td>
                    <td className="px-3 py-2.5 text-right font-mono text-slate-500">{m.tokens_out.toLocaleString()}</td>
                    <td className="px-3 py-2.5 text-right font-mono text-slate-900 font-semibold">${m.cost_usd.toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}


// ── Channel Breakdown — large donut + table (Budget Manager style) ────────────
// Left: full-size SVG donut with centre total. Right: Segment | Cost | Share | Requests table.
function ChannelBreakdown({ channels, totalCost }) {
  const items = (channels || []).filter(c => (c.count || 0) > 0);
  if (!items.length) return <EmptyText>No channel data yet.</EmptyText>;

  const totalEvents = items.reduce((s, c) => s + c.count, 0);

  // Build donut arcs — same geometry as DonutWithLegend (viewBox 100×100, r=42, stroke=12)
  const cx = 50, cy = 50, r = 42, circ = 2 * Math.PI * r;
  let arcOffset = 0;
  const arcs = items.map((c, i) => {
    const frac = c.count / totalEvents;
    const dash = frac * circ;
    const seg = (
      <circle
        key={c.name}
        cx={cx} cy={cy} r={r}
        fill="none"
        stroke={colorForIndex(i)}
        strokeWidth="12"
        strokeLinecap="butt"
        strokeDasharray={`${Math.max(0, dash - 0.6)} ${circ - dash + 0.6}`}
        strokeDashoffset={-arcOffset}
        transform={`rotate(-90 ${cx} ${cy})`}
      />
    );
    arcOffset += dash;
    return seg;
  });

  return (
    <div className="flex flex-col sm:flex-row items-center gap-5">
      {/* Donut — same size as DonutWithLegend */}
      <svg viewBox="0 0 100 100" className="w-36 h-36 flex-shrink-0">
          {/* Track */}
          <circle cx={cx} cy={cy} r={r} fill="none" stroke="#f1f5f9" strokeWidth="12" />
          {arcs}
          {/* Centre: total events */}
          <text x={cx} y={cy - 4} textAnchor="middle" dominantBaseline="middle"
                fontSize="13" fontWeight="700" fill="#0f172a">
            {totalEvents.toLocaleString()}
          </text>
          <text x={cx} y={cy + 10} textAnchor="middle" dominantBaseline="middle"
                fontSize="5" fontWeight="600" fill="#94a3b8" letterSpacing="0.6">
            REQUESTS
          </text>
        </svg>

      {/* Table */}
      <div className="flex-1 overflow-x-auto min-w-0 w-full">
        <table className="w-full text-[11px]">
          <thead className="bg-gray-50">
            <tr>
              <th className="px-2.5 py-1.5 text-left text-[10px] text-gray-500 font-semibold uppercase tracking-wide">Segment</th>
              <th className="px-2.5 py-1.5 text-right text-[10px] text-gray-500 font-semibold uppercase tracking-wide">Cost (USD)</th>
              <th className="px-2.5 py-1.5 text-right text-[10px] text-gray-500 font-semibold uppercase tracking-wide">Share</th>
              <th className="px-2.5 py-1.5 text-right text-[10px] text-gray-500 font-semibold uppercase tracking-wide">Requests</th>
            </tr>
          </thead>
          <tbody>
            {items.map((c, i) => {
              const pct  = c.pct ?? (totalEvents ? +((c.count / totalEvents) * 100).toFixed(1) : 0);
              const cost = typeof c.cost_usd === "number" ? c.cost_usd : 0;
              return (
                <tr key={c.name} className="border-t border-gray-100 hover:bg-gray-50/60 transition">
                  <td className="px-2.5 py-1.5 text-gray-700">
                    <span className="inline-flex items-center gap-1.5">
                      <span className="inline-block w-2.5 h-2.5 rounded-sm flex-shrink-0"
                            style={{ background: colorForIndex(i) }} />
                      {fmtChannel(c.name)}
                    </span>
                  </td>
                  <td className="px-2.5 py-1.5 text-right font-mono text-gray-600">${cost.toFixed(4)}</td>
                  <td className="px-2.5 py-1.5 text-right font-mono text-gray-500">{pct}%</td>
                  <td className="px-2.5 py-1.5 text-right font-mono text-gray-500">{c.count.toLocaleString()}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Model share table (replaces DonutWithLegend in "Share by model" card) ───
// ── Donut chart primitive (pure SVG — no chart lib) ─────────────────────────
// Renders one ring + a legend with values / percentages next to it.
// Stable colour palette so the same model always gets the same slice colour
// when the user refreshes or changes the window.
const _PALETTE = [
  "#6366f1", "#10b981", "#f59e0b", "#ec4899", "#3b82f6",
  "#8b5cf6", "#14b8a6", "#ef4444", "#84cc16", "#0ea5e9",
];

function colorForIndex(i) {
  return _PALETTE[((i % _PALETTE.length) + _PALETTE.length) % _PALETTE.length];
}

function DonutWithLegend({ items }) {
  // Filter out zero entries and compute total once.
  const filtered = (items || []).filter(it => (it.value || 0) > 0);
  const total    = filtered.reduce((s, it) => s + it.value, 0);

  if (total === 0) {
    return <EmptyText>No data.</EmptyText>;
  }

  // Build SVG arcs. Donut: viewBox 100×100, radius 42, stroke 12 (thinner = lighter, more elegant).
  const cx = 50, cy = 50, r = 42, circ = 2 * Math.PI * r;
  let offset = 0;
  const arcs = filtered.map((it, i) => {
    const frac = it.value / total;
    const dash = frac * circ;
    // Small gap between segments for a cleaner look
    const seg = (
      <circle
        key={it.name}
        cx={cx} cy={cy} r={r}
        fill="none"
        stroke={colorForIndex(i)}
        strokeWidth="12"
        strokeLinecap="butt"
        strokeDasharray={`${Math.max(0, dash - 0.5)} ${circ - dash + 0.5}`}
        strokeDashoffset={-offset}
        transform={`rotate(-90 ${cx} ${cy})`}
      />
    );
    offset += dash;
    return seg;
  });

  return (
    <div className="flex items-center gap-5">
      {/* Donut */}
      <svg viewBox="0 0 100 100" className="w-36 h-36 flex-shrink-0">
        {/* Track */}
        <circle cx={cx} cy={cy} r={r} fill="none" stroke="#f1f5f9" strokeWidth="12" />
        {arcs}
        {/* Centre stack — big total, small label */}
        <text x={cx} y={cy - 1} textAnchor="middle" dominantBaseline="middle"
              fontSize="14" fontWeight="700" fill="#0f172a">
          {total}
        </text>
        <text x={cx} y={cy + 10} textAnchor="middle" dominantBaseline="middle"
              fontSize="5" fontWeight="600" fill="#94a3b8" letterSpacing="0.5">
          EVENTS
        </text>
      </svg>

      {/* Legend with proportional fill bars */}
      <ul className="flex-1 space-y-2 min-w-0 text-[11px]">
        {filtered.map((it, i) => {
          const pct = it.pct ?? +((it.value / total) * 100).toFixed(1);
          return (
            <li key={it.name} className="min-w-0">
              <div className="flex items-center gap-2 min-w-0">
                <span className="inline-block w-2.5 h-2.5 rounded-sm flex-shrink-0"
                      style={{ background: colorForIndex(i) }} />
                <span className="truncate text-slate-700 font-medium" title={it.name}>{it.name}</span>
                <span className="ml-auto text-slate-900 font-mono font-semibold">
                  {pct}%
                </span>
                <span className="text-slate-400 font-mono text-[10px] w-10 text-right">
                  ({it.value})
                </span>
              </div>
              <div className="mt-1 ml-[18px] h-1 bg-slate-100 rounded-full overflow-hidden">
                <div className="h-full rounded-full transition-all duration-500"
                     style={{ width: `${Math.max(3, pct)}%`, background: colorForIndex(i) }} />
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}


// Rule Playground (FR-IMP-2) was previously a top-level tab here; it now lives
// as a card inside the Admin tab — see PlaygroundCard in CoachAdmin.jsx.

// OrgRollupTab removed — component was implemented but not wired into the TABS
// array (FR-ENT-1 deferred to Phase 3). The backend endpoint GET /coach/org/rollup
// is live. Re-add this component and add { key: "org", label: "Org Rollups",
// icon: BarChart2, adminOnly: true } to TABS when Phase 3 ships.

// =============================================================================
// Tiny shared primitives
// =============================================================================
function Card({ title, subtle, children, accent }) {
  // accent: "indigo" | "amber" | "red" — left border + soft tinted header
  const accentBar = {
    indigo: "before:bg-indigo-400",
    amber:  "before:bg-amber-400",
    red:    "before:bg-red-400",
  }[accent];
  return (
    <div className={`relative p-4 bg-white border border-slate-200 rounded-2xl shadow-[0_1px_2px_rgba(15,23,42,0.04)] ${
      accent ? `before:content-[''] before:absolute before:top-3 before:bottom-3 before:left-0 before:w-0.5 before:rounded-r ${accentBar}` : ""
    }`}>
      {(title || subtle) && (
        <div className="mb-3">
          {title && <div className="text-[12px] font-bold text-slate-800 tracking-tight">{title}</div>}
          {subtle && <div className="text-[10.5px] text-slate-400 mt-0.5 leading-snug">{subtle}</div>}
        </div>
      )}
      {children}
    </div>
  );
}

function Stat({ label, value, icon: Icon, hint, accent }) {
  const ACCENTS = {
    amber:  { bg: "bg-amber-50/60", icon: "text-amber-500" },
    indigo: { bg: "bg-indigo-50/60", icon: "text-indigo-500" },
    red:    { bg: "bg-red-50/60", icon: "text-red-500" },
  };
  const a = ACCENTS[accent];
  return (
    <div className="relative overflow-hidden p-4 bg-white border border-slate-200 rounded-2xl shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
      {a && <div className={`absolute inset-0 ${a.bg}`} />}
      <div className="relative">
        <div className="flex items-center justify-between">
          <div className="text-[10px] uppercase tracking-widest text-slate-500 font-bold">{label}</div>
          {Icon && <Icon size={14} className={a ? a.icon : "text-slate-300"} />}
        </div>
        <div className="text-2xl font-bold text-slate-900 mt-2 leading-none tracking-tight">{value}</div>
        {hint && <div className="text-[10.5px] text-slate-500 mt-1.5">{hint}</div>}
      </div>
    </div>
  );
}

// Circular score gauge — pure SVG so no chart library is needed.
let _ringIdCounter = 0;
function ScoreRing({ score, size = 80, disabled = false }) {
  // Stable unique ID per instance so multiple rings on the same page
  // don't share a single <linearGradient> definition.
  const [gradId] = useState(() => `ring-${++_ringIdCounter}`);
  const pct = Math.max(0, Math.min(100, score || 0));
  const stroke = 8;
  const r = (size - stroke) / 2;
  const circ = 2 * Math.PI * r;
  const dash = (pct / 100) * circ;
  const colorStop = disabled
    ? { from: "#cbd5e1", to: "#cbd5e1" }
    : pct >= 80 ? { from: "#34d399", to: "#10b981" }
    : pct >= 50 ? { from: "#fbbf24", to: "#f59e0b" }
    :              { from: "#fb7185", to: "#ef4444" };
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="flex-shrink-0">
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%"  stopColor={colorStop.from} />
          <stop offset="100%" stopColor={colorStop.to} />
        </linearGradient>
      </defs>
      {/* Track */}
      <circle cx={size/2} cy={size/2} r={r} fill="none" stroke="#f1f5f9" strokeWidth={stroke} />
      {/* Progress arc */}
      <circle
        cx={size/2} cy={size/2} r={r}
        fill="none"
        stroke={`url(#${gradId})`}
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={`${dash} ${circ - dash}`}
        strokeDashoffset={circ / 4}
        transform={`rotate(-90 ${size/2} ${size/2})`}
        style={{ transition: "stroke-dasharray 600ms ease" }}
      />
    </svg>
  );
}

function Skeleton() {
  return (
    <div className="space-y-3 max-w-6xl mx-auto">
      <div className="grid grid-cols-3 gap-4">
        <div className="h-28 bg-gradient-to-r from-slate-100 to-slate-50 rounded-2xl animate-pulse" />
        <div className="h-28 bg-gradient-to-r from-slate-100 to-slate-50 rounded-2xl animate-pulse" />
        <div className="h-28 bg-gradient-to-r from-slate-100 to-slate-50 rounded-2xl animate-pulse" />
      </div>
      <div className="h-32 bg-gradient-to-r from-slate-100 to-slate-50 rounded-2xl animate-pulse" />
    </div>
  );
}

function EmptyText({ children }) {
  return <div className="text-[11px] text-slate-400 italic py-2">{children}</div>;
}

function ErrorBox({ msg, onRetry }) {
  return (
    <div className="p-4 bg-red-50 border border-red-200 rounded-2xl flex items-center justify-between">
      <div className="flex items-center gap-2 text-red-700 text-[12px]">
        <ShieldAlert size={14} /> {msg}
      </div>
      {onRetry && (
        <button onClick={onRetry}
                className="text-[11px] px-2 py-1 text-red-700 hover:text-red-800 bg-white border border-red-200 rounded-md cursor-pointer transition">
          Retry
        </button>
      )}
    </div>
  );
}
