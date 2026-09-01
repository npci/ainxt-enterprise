// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect, useCallback } from "react";
import { API_BASE, authFetch } from "../config";
import { toIST } from "../utils/time";
import {
  CheckCircle, AlertTriangle, XCircle, RefreshCw,
  ChevronDown, ChevronUp, Shield, Brain,
  Zap, TrendingUp, TrendingDown, Minus,
  Target, ThumbsUp, List,
} from "lucide-react";

// ── Eval type metadata ────────────────────────────────────────────────────────

const EVAL_META = {
  groundedness: {
    icon: Shield, color: "red",
    title: "Hallucination Check",
    what: "Did the AI invent function names, file paths, or API details that don't exist in your codebase?",
    good: "The AI is staying grounded in your actual code. No made-up paths or functions.",
    bad:  "The AI may be hallucinating — inventing file paths, function names, or endpoints that don't exist.",
  },
  relevance: {
    icon: Brain, color: "purple",
    title: "Answer Usefulness",
    what: "Did the AI actually answer what was asked, with the right level of detail?",
    good: "Answers are on-topic, technically specific, and actionable.",
    bad:  "Answers may be vague, off-topic, or not specific enough to be useful.",
  },
  coach_prompt: {
    icon: Target, color: "orange",
    title: "Prompt Quality",
    what: "Was the user's prompt to AiNxt Coach clear, safe, and well-structured enough for the AI to act on?",
    good: "Prompts are clear, specific, and safe. Users are providing good context and constraints.",
    bad:  "Prompts are vague, missing context, or contain sensitive data. Coach guidance may help.",
  },
  human_feedback: {
    icon: ThumbsUp, color: "teal",
    title: "Human Feedback",
    what: "Direct thumbs up / thumbs down ratings given by users on AI responses.",
    good: "Users are rating responses positively. The AI is meeting user expectations.",
    bad:  "Users are rating responses negatively. Review recent answers for quality issues.",
  },
};

const COLOR_MAP = {
  // 4 visually distinct colours for the remaining checks
  red:    { card: "bg-red-50 border-red-300",       icon: "text-red-500",     bar: "bg-red-500",      badge: "bg-red-100 text-red-700",       trend: "#ef4444" },  // strong red
  purple: { card: "bg-purple-50 border-purple-300", icon: "text-purple-500",  bar: "bg-purple-500",   badge: "bg-purple-100 text-purple-700", trend: "#a855f7" },  // vivid purple
  orange: { card: "bg-orange-50 border-orange-300", icon: "text-orange-500",  bar: "bg-orange-500",   badge: "bg-orange-100 text-orange-700", trend: "#f97316" },  // deep orange
  teal:   { card: "bg-teal-50 border-teal-300",     icon: "text-teal-600",    bar: "bg-teal-500",     badge: "bg-teal-100 text-teal-700",     trend: "#14b8a6" },  // teal/cyan-green
  // Safe fallback — used when an unknown eval_type has no EVAL_META entry
  indigo: { card: "bg-indigo-50 border-indigo-200", icon: "text-indigo-500",  bar: "bg-indigo-400",   badge: "bg-indigo-100 text-indigo-700", trend: "#6366f1" },
};

const FLAG_STYLES = {
  PASS: { bg: "bg-emerald-50 border-emerald-100", badge: "bg-emerald-100 text-emerald-700", icon: CheckCircle,   color: "text-emerald-500" },
  WARN: { bg: "bg-amber-50 border-amber-100",     badge: "bg-amber-100 text-amber-700",     icon: AlertTriangle, color: "text-amber-500"   },
  FAIL: { bg: "bg-red-50 border-red-100",         badge: "bg-red-100 text-red-700",         icon: XCircle,       color: "text-red-500"     },
};

function scoreFlag(s) { return s >= 0.7 ? "PASS" : s >= 0.4 ? "WARN" : "FAIL"; }

// ── Score bar ─────────────────────────────────────────────────────────────────

function ScoreBar({ score }) {
  const pct = Math.round(score * 100);
  const bar = score >= 0.7 ? "bg-emerald-400" : score >= 0.4 ? "bg-amber-400" : "bg-red-400";
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-gray-100 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${bar} transition-all`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs tabular-nums text-gray-600 w-8 text-right font-medium">{pct}%</span>
    </div>
  );
}

// ── Mini Sparkline (7-day trend for one eval type) ────────────────────────────

function TrendSparkline({ points, color = "#6366f1" }) {
  // Only keep days that actually have data (non-null).
  const nonNull = (points || []).filter(p => p != null);
  if (nonNull.length < 2) {
    return <span className="text-xs text-gray-300 italic">no trend data</span>;
  }

  // Bug-fix: do NOT replace null with 0.
  // Null days (no data) were being drawn as 0 on the graph, creating a
  // false visual dip. Instead, only plot the days that have real data.
  // This also fixes the arrow direction bug: the delta was comparing
  // first-nonNull vs last-nonNull correctly, but the graph visually showed
  // a downward trend because of the 0-filled null days in between.
  const W = 80, H = 24;
  const step = W / (nonNull.length - 1);
  const max  = Math.max(...nonNull, 0.01);
  const coords = nonNull.map((v, i) => `${i * step},${H - (v / max) * (H - 2) - 1}`);

  // Delta: last real data point vs first real data point.
  // Threshold 0.02 = 2 percentage points — below that show neutral (—).
  const delta     = nonNull[nonNull.length - 1] - nonNull[0];
  const DeltaIcon = delta > 0.02 ? TrendingUp : delta < -0.02 ? TrendingDown : Minus;
  const deltaColor = delta > 0.02
    ? "text-emerald-500"   // genuinely going up   → green ↑
    : delta < -0.02
    ? "text-red-500"       // genuinely going down → red ↓
    : "text-gray-400";     // flat                 → grey —

  return (
    <div className="flex items-center gap-2">
      <svg width={W} height={H} className="flex-shrink-0 overflow-visible">
        <polyline
          points={coords.join(" ")}
          fill="none"
          stroke={color}
          strokeWidth="1.5"
          strokeLinejoin="round"
        />
        {nonNull.map((v, i) => (
          <circle
            key={i}
            cx={i * step}
            cy={H - (v / max) * (H - 2) - 1}
            r={i === nonNull.length - 1 ? 2.5 : 1.5}
            fill={color}
          />
        ))}
      </svg>
      <DeltaIcon size={11} className={deltaColor} />
    </div>
  );
}

// ── Delta Badge ───────────────────────────────────────────────────────────────

function DeltaBadge({ todayScore, weekScore }) {
  if (todayScore == null || weekScore == null) return null;
  const delta = ((todayScore - weekScore) * 100).toFixed(0);
  if (Math.abs(delta) < 1) return null;
  const pos = delta > 0;
  return (
    <span className={`flex items-center gap-0.5 text-[10px] font-semibold px-1.5 py-0.5 rounded-full ${
      pos ? "bg-emerald-100 text-emerald-700" : "bg-red-100 text-red-600"
    }`}>
      {pos ? <TrendingUp size={9} /> : <TrendingDown size={9} />}
      {pos ? "+" : ""}{delta}% today
    </span>
  );
}

// ── Summary Card ──────────────────────────────────────────────────────────────

function SummaryCard({ evalType, item, todayItem, trend }) {
  // Always use EVAL_META for display — never show raw key names.
  // `item` may be null when no DB data exists yet for this check type.
  const meta  = EVAL_META[evalType] || { title: evalType, icon: Zap, color: "indigo", what: "", good: "", bad: "" };
  const Icon  = meta.icon;
  const c     = COLOR_MAP[meta.color] || COLOR_MAP.indigo;
  const hasData = !!item;

  // When no data: show a "not yet active" placeholder card in the same style.
  if (!hasData) {
    return (
      <div className={`border rounded-xl p-4 ${c.card} opacity-60`}>
        <div className="flex items-center gap-2 mb-2">
          <Icon size={14} className={c.icon + " flex-shrink-0"} />
          <p className="text-xs font-semibold text-gray-700 truncate">{meta.title}</p>
          <span className="ml-auto text-[10px] px-2 py-0.5 rounded-full bg-gray-100 text-gray-400 font-medium">
            No data yet
          </span>
        </div>
        <p className="text-2xl font-bold text-gray-300 mb-1">—</p>
        <div className="h-1.5 bg-gray-100 rounded-full mb-2" />
        <p className="text-[11px] text-gray-400 leading-relaxed">{meta.what}</p>
      </div>
    );
  }

  const flag  = scoreFlag(item.avg_score || 0);
  const pct   = Math.round((item.avg_score || 0) * 100);
  const fs    = FLAG_STYLES[flag];
  const FIcon = fs.icon;

  return (
    <div className={`border rounded-xl p-4 ${c.card}`}>
      <div className="flex items-start justify-between mb-1 gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <Icon size={14} className={c.icon + " flex-shrink-0"} />
          <p className="text-xs font-semibold text-gray-700 truncate">{meta.title}</p>
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          {todayItem && <DeltaBadge todayScore={todayItem.avg_score} weekScore={item.avg_score} />}
          <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold ${fs.badge}`}>
            <FIcon size={10} /> {flag}
          </span>
        </div>
      </div>

      <p className="text-2xl font-bold text-gray-900 mt-2 mb-1 tabular-nums">{pct}%</p>
      <ScoreBar score={item.avg_score || 0} />

      {/* 7-day trend sparkline */}
      {trend && (
        <div className="mt-2 flex items-center gap-2">
          <span className="text-[10px] text-gray-400">7d trend</span>
          <TrendSparkline points={trend} color={c.trend} />
        </div>
      )}

      <p className="text-[11px] text-gray-500 mt-2 leading-relaxed">
        {flag === "PASS" ? meta.good : meta.bad}
      </p>

      <div className="mt-3 pt-2 border-t border-black/5 flex gap-3 text-[11px] text-gray-400">
        <span><span className="font-semibold text-emerald-600">{item.pass_count}</span> passed</span>
        <span><span className="font-semibold text-amber-600">{item.warn_count}</span> warning</span>
        <span><span className="font-semibold text-red-500">{item.fail_count}</span> failed</span>
        <span className="ml-auto">{item.total} checked</span>
      </div>
    </div>
  );
}

// ── Result Row ────────────────────────────────────────────────────────────────

function ResultRow({ r }) {
  const [expanded, setExpanded] = useState(false);
  const flag  = scoreFlag(r.score);
  const fs    = FLAG_STYLES[flag];
  const FIcon = fs.icon;
  const meta  = EVAL_META[r.eval_type] || { title: r.eval_type, color: "indigo" };

  return (
    <>
      <tr className="hover:bg-gray-50 cursor-pointer transition-colors border-b border-gray-100 last:border-0"
        onClick={() => setExpanded(x => !x)}>
        <td className="px-4 py-3">
          <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold ${fs.badge}`}>
            <FIcon size={10} /> {flag}
          </span>
        </td>
        <td className="px-4 py-3 text-xs text-gray-700 font-medium">{meta.title}</td>
        <td className="px-4 py-3 w-36"><ScoreBar score={r.score} /></td>

        {/* AI Source Model — the model that generated the answer being evaluated */}
        <td className="px-4 py-3 text-[11px] text-gray-500 max-w-[160px] truncate" title={r.model || "Unknown"}>
          {r.model
            ? <span className="inline-flex items-center gap-1 bg-indigo-50 text-indigo-700 border border-indigo-100 rounded px-1.5 py-0.5 font-mono text-[10px] truncate max-w-full">
                <span className="inline-block w-1.5 h-1.5 rounded-full bg-indigo-400 flex-shrink-0" />
                {r.model}
              </span>
            : <span className="text-gray-300 italic text-[10px]">unknown</span>}
        </td>

        {/* AI Judge Model — the model that evaluated / scored the answer */}
        <td className="px-4 py-3 text-[11px] text-gray-500 max-w-[160px] truncate" title={r.judge_model || "Unknown"}>
          {r.judge_model
            ? <span className="inline-flex items-center gap-1 bg-violet-50 text-violet-700 border border-violet-100 rounded px-1.5 py-0.5 font-mono text-[10px] truncate max-w-full">
                <span className="inline-block w-1.5 h-1.5 rounded-full bg-violet-400 flex-shrink-0" />
                {r.judge_model}
              </span>
            : <span className="text-gray-300 italic text-[10px]">unknown</span>}
        </td>

        <td className="px-4 py-3 text-xs text-gray-500 max-w-sm">{r.reason || "—"}</td>
        <td className="px-4 py-3 text-[11px] text-gray-400 whitespace-nowrap">
          {r.created_at ? toIST(r.created_at) : "—"}
        </td>
        <td className="px-4 py-3 text-gray-400">
          {expanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
        </td>
      </tr>

      {expanded && (
        <tr className="bg-gray-50">
          <td colSpan={8} className="px-5 py-4 border-b border-gray-100">
            <div className="space-y-2 text-xs text-gray-600">
              <div className="text-[11px] text-gray-500 bg-white border border-gray-100 rounded px-3 py-2">
                <span className="font-semibold text-gray-700">What was checked: </span>
                {meta.what || meta.title}
              </div>
              {r.question && (
                <div>
                  <span className="font-semibold text-gray-700">Original question: </span>
                  <span className="italic text-gray-500">{r.question}</span>
                </div>
              )}
              {r.metadata?.issues?.length > 0 && (
                <div>
                  <span className="font-semibold text-red-600">Issues found:</span>
                  <ul className="mt-1 ml-3 space-y-0.5">
                    {r.metadata.issues.map((iss, i) => (
                      <li key={i} className="text-red-600 list-disc list-inside">{iss}</li>
                    ))}
                  </ul>
                </div>
              )}
              {r.metadata?.criteria && Object.keys(r.metadata.criteria).length > 0 && (
                <div>
                  <span className="font-semibold text-gray-700">Criteria breakdown: </span>
                  <div className="flex flex-wrap gap-1.5 mt-1">
                    {Object.entries(r.metadata.criteria).map(([k, v]) => (
                      <span key={k} className={`inline-flex items-center gap-0.5 px-2 py-0.5 rounded text-[10px] font-medium ${
                        v ? "bg-emerald-100 text-emerald-700" : "bg-red-100 text-red-700"
                      }`}>
                        {v ? <CheckCircle size={9} /> : <XCircle size={9} />} {k}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {r.run_id && (
                <div className="text-gray-400">
                  SDLC Run: <code className="bg-gray-100 px-1 rounded text-[10px]">{r.run_id}</code>
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

// ── Empty State ───────────────────────────────────────────────────────────────

function EmptyState() {
  return (
    <div className="text-center py-16 px-6">
      <Zap size={32} className="text-gray-200 mx-auto mb-3" strokeWidth={1.5} />
      <p className="text-sm font-medium text-gray-400">No eval data yet</p>
      <p className="text-xs text-gray-300 mt-1 max-w-sm mx-auto">
        Quality scores appear automatically after the AI responds to questions or runs an SDLC pipeline.
        Send a message in Chat or trigger an SDLC run to see results here.
      </p>
    </div>
  );
}

// ── Main Dashboard ────────────────────────────────────────────────────────────

export default function EvalsDashboard() {
  const [summary,        setSummary]        = useState([]);   // week window (hours=168)
  const [todaySummary,   setTodaySummary]   = useState([]);   // 24h window for delta
  const [results,        setResults]        = useState([]);
  const [trend,          setTrend]          = useState({});   // {eval_type: [daily scores]}
  const [total,          setTotal]          = useState(0);
  const [loading,        setLoading]        = useState(false);
  const [hours,          setHours]          = useState(168);
  const [platform,       setPlatform]       = useState("");   // "" = All Platforms
  const [filterType,     setFilterType]     = useState("");
  const [filterFlag,     setFilterFlag]     = useState("");
  const [page,           setPage]           = useState(0);
  const [showResults,    setShowResults]    = useState(false);   // Individual Check Results — hidden by default
  const [modelBreakdown, setModelBreakdown] = useState([]);      // groundedness by source model
  const PAGE = 30;

  const fetchAll = useCallback(() => {
    // Build platform param — only append when a specific platform is selected
    const platformParam = platform ? `&platform=${encodeURIComponent(platform)}` : "";

    // Summary (selected window)
    authFetch(`${API_BASE}/evals/summary?hours=${hours}${platformParam}`)
      .then(r => r.json()).then(d => setSummary(d.eval_types || [])).catch(() => {});

    // Today's summary for delta badges
    authFetch(`${API_BASE}/evals/summary?hours=24${platformParam}`)
      .then(r => r.json()).then(d => setTodaySummary(d.eval_types || [])).catch(() => {});

    // 7-day trend sparklines
    authFetch(`${API_BASE}/evals/trend?days=7${platformParam}`)
      .then(r => r.json()).then(d => setTrend(d.series || {})).catch(() => {});

    // Model breakdown — groundedness scores by source model
    authFetch(`${API_BASE}/evals/model-breakdown?hours=${hours}${platformParam}`)
      .then(r => r.json()).then(d => setModelBreakdown(d.breakdown || [])).catch(() => {});
  }, [hours, platform]);

  const fetchResults = useCallback(() => {
    setLoading(true);
    const params = new URLSearchParams({ limit: PAGE, offset: page * PAGE });
    if (platform)   params.set("platform",  platform);
    if (filterType) params.set("eval_type", filterType);
    if (filterFlag === "PASS") params.set("min_score", "0.7");
    if (filterFlag === "WARN") { params.set("min_score", "0.4"); params.set("max_score", "0.699"); }
    if (filterFlag === "FAIL") params.set("max_score", "0.399");
    authFetch(`${API_BASE}/evals/results?${params}`)
      .then(r => r.json())
      .then(d => { setResults(d.results || []); setTotal(d.total || 0); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [platform, filterType, filterFlag, page, hours]);

  useEffect(() => { fetchAll(); fetchResults(); }, [fetchAll, fetchResults]);

  // Overall score banner
  const overallPct  = summary.length
    ? Math.round(summary.reduce((s, x) => s + (x.avg_score || 0), 0) / summary.length * 100)
    : null;
  const totalChecked = summary.reduce((s, x) => s + (x.total || 0), 0);

  // Today overall for delta
  const todayOverallPct = todaySummary.length
    ? Math.round(todaySummary.reduce((s, x) => s + (x.avg_score || 0), 0) / todaySummary.length * 100)
    : null;
  const overallDelta = overallPct != null && todayOverallPct != null ? todayOverallPct - overallPct : null;

  // Map today's summary by type for delta badges
  const todayByType = Object.fromEntries(todaySummary.map(t => [t.eval_type, t]));

  return (
    <div className="h-full flex flex-col bg-gray-50 overflow-hidden">

      {/* ── Header ── */}
      <div className="flex-shrink-0 border-b border-gray-200 bg-white px-6 py-4">
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-sm font-semibold  text-indigo-700">AI Quality Monitor</h1>
            <p className="text-xs text-gray-400 mt-0.5">
              A second AI automatically grades every response — checking for hallucinations,
              irrelevant answers, and unsafe generated code.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {/* Platform filter dropdown */}
            <select
              value={platform}
              onChange={e => { setPlatform(e.target.value); setPage(0); }}
              className="text-xs border border-gray-200 rounded cursor-pointer hover:bg-gray-100 px-3 py-1.5 bg-white text-gray-600 focus:outline-none focus:border-indigo-300"
            >
              <option value="">All Platforms</option>
              <option value="chat">Chat</option>
              <option value="knowledge_base">Knowledge Base</option>
              <option value="my_workspace">My Workspace</option>
              <option value="agent_studio">Agent Studio</option>
            </select>
            {/* Time window selector */}
            <select
              value={hours}
              onChange={e => { setHours(+e.target.value); setPage(0); }}
              className="text-xs border border-gray-200 rounded cursor-pointer hover:bg-gray-100 px-3 py-1.5 bg-white text-gray-600 focus:outline-none focus:border-indigo-300"
            >
              <option value={1}>Last 1h</option>
              <option value={6}>Last 6h</option>
              <option value={24}>Last 24h</option>
              <option value={72}>Last 3 days</option>
              <option value={168}>Last 7 days</option>
            </select>
            <button
              onClick={() => { fetchAll(); fetchResults(); }}
              className="flex items-center gap-1.5 text-xs hover:bg-gray-100 rounded px-3 py-2 transition cursor-pointer text-gray-600"
            >
              <RefreshCw size={12} /> Refresh
            </button>
          </div>
        </div>

        {/* Overall score banner */}
        {overallPct !== null && (
          <div className={`mt-3 flex items-center gap-3 px-3 py-2 rounded-lg text-sm ${
            overallPct >= 70 ? "bg-emerald-50 text-emerald-800"
            : overallPct >= 40 ? "bg-amber-50 text-amber-800"
            : "bg-red-50 text-red-800"
          }`}>
            {overallPct >= 70
              ? <CheckCircle size={15} className="text-emerald-500" />
              : overallPct >= 40
              ? <AlertTriangle size={15} className="text-amber-500" />
              : <XCircle size={15} className="text-red-500" />}
            <span className="font-bold text-base tabular-nums">{overallPct}%</span>
            <span className="text-xs opacity-70">
              overall · {summary.length} of {Object.keys(EVAL_META).length} check type{summary.length !== 1 ? "s" : ""} active · {totalChecked.toLocaleString()} responses graded
            </span>
            {overallDelta != null && Math.abs(overallDelta) >= 1 && (
              <span className={`ml-auto flex items-center gap-1 text-xs font-semibold px-2 py-0.5 rounded-full ${
                overallDelta > 0 ? "bg-emerald-200 text-emerald-800" : "bg-red-200 text-red-800"
              }`}>
                {overallDelta > 0 ? <TrendingUp size={10} /> : <TrendingDown size={10} />}
                {overallDelta > 0 ? "+" : ""}{overallDelta}% today
              </span>
            )}
          </div>
        )}
      </div>

      <div className="flex-1 overflow-y-auto p-6 space-y-6">

        {/* ── What gets checked ── */}
        <section className="border border-gray-200 rounded-xl bg-white p-5 shadow-sm">
          <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-widest mb-4">
            What Gets Automatically Checked
          </h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {Object.entries(EVAL_META).map(([key, meta]) => {
              const Icon = meta.icon;
              const c    = COLOR_MAP[meta.color] || COLOR_MAP.indigo;
              return (
                <div key={key} className={`flex gap-3 p-3 rounded-lg border ${c.card}`}>
                  <div className={`mt-0.5 flex-shrink-0 ${c.icon}`}><Icon size={15} /></div>
                  <div>
                    <p className="text-xs font-semibold text-gray-700">{meta.title}</p>
                    <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">{meta.what}</p>
                  </div>
                </div>
              );
            })}
          </div>
          <p className="text-[11px] text-gray-400 mt-4 border-t border-gray-100 pt-3">
            Chat checks run in the background (zero added latency). SDLC pipeline checks are blocking — a failed check
            triggers one automatic retry with the specific issues injected into the AI's next attempt.
          </p>
        </section>

        {/* ── Summary cards ── always show all 8 from EVAL_META.
             Cards with no DB data yet show a "No data yet" placeholder
             in the same colour/icon style so the grid is always complete. */}
        <section>
          <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-widest mb-3">
            Quality by Check Type
          </h2>
          {loading && summary.length === 0 ? (
            // Skeleton placeholders while first load is in flight
            <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
              {Object.keys(EVAL_META).map(k => (
                <div key={k} className="border rounded-xl p-4 bg-gray-50 border-gray-100 animate-pulse h-36" />
              ))}
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
              {Object.keys(EVAL_META).map(evalType => {
                // Find matching DB row for this eval type (may be undefined)
                const item      = summary.find(s => s.eval_type === evalType) || null;
                const todayItem = todayByType[evalType] || null;
                const trendPts  = trend[evalType] || null;
                return (
                  <SummaryCard
                    key={evalType}
                    evalType={evalType}
                    item={item}
                    todayItem={todayItem}
                    trend={trendPts}
                  />
                );
              })}
            </div>
          )}
        </section>

        {/* ── Results section ── model breakdown + individual results (hidden by default) */}
        <section>
          {/* ── Model Hallucination Breakdown ─────────────────────────────── */}
          {modelBreakdown.length > 0 && (
            <div className="mb-6">
              <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-widest mb-3">
                Hallucination by Source Model
                <span className="ml-2 text-[10px] font-normal normal-case tracking-normal text-gray-300">
                  (groundedness check only — lower score = more hallucination)
                </span>
              </h2>
              <div className="border border-gray-200 rounded-xl overflow-hidden bg-white shadow-sm">
                <table className="w-full text-sm">
                  <thead className="border-b border-gray-200 bg-gray-50">
                    <tr>
                      <th className="px-4 py-2.5 text-left text-[0.68rem] font-semibold text-gray-400 uppercase tracking-widest">Model</th>
                      <th className="px-4 py-2.5 text-left text-[0.68rem] font-semibold text-gray-400 uppercase tracking-widest w-36">Avg Score</th>
                      <th className="px-4 py-2.5 text-left text-[0.68rem] font-semibold text-gray-400 uppercase tracking-widest">Pass Rate</th>
                      <th className="px-4 py-2.5 text-left text-[0.68rem] font-semibold text-gray-400 uppercase tracking-widest">Requests</th>
                    </tr>
                  </thead>
                  <tbody>
                    {modelBreakdown.map((m, i) => {
                      const flag = scoreFlag(m.avg_score);
                      const fs   = FLAG_STYLES[flag];
                      return (
                        <tr key={i} className="border-b border-gray-100 last:border-0 hover:bg-gray-50">
                          <td className="px-4 py-3 text-xs font-mono text-gray-700 max-w-[220px] truncate" title={m.model}>{m.model}</td>
                          <td className="px-4 py-3 w-36"><ScoreBar score={m.avg_score} /></td>
                          <td className="px-4 py-3">
                            <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold ${fs.badge}`}>
                              {Math.round(m.pass_rate * 100)}%
                            </span>
                          </td>
                          <td className="px-4 py-3 text-xs text-gray-400">{m.total}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* ── Individual Check Results — toggle ─────────────────────────── */}
          <div className="flex items-center justify-between mb-3">
            <button
              type="button"
              onClick={() => setShowResults(v => !v)}
              className="flex items-center gap-2 text-xs font-semibold text-gray-400 uppercase tracking-widest hover:text-gray-600 transition cursor-pointer"
            >
              <List size={13} />
              Individual Check Results
              {showResults ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
              {total > 0 && (
                <span className="ml-1 text-[10px] font-normal normal-case tracking-normal text-gray-300">
                  {total.toLocaleString()} rows — click to {showResults ? "hide" : "show"}
                </span>
              )}
            </button>
            {showResults && (
              <div className="flex items-center gap-2">
                <select
                  value={filterType}
                  onChange={e => { setFilterType(e.target.value); setPage(0); }}
                  className="text-xs border border-gray-200 rounded-lg px-2.5 py-1.5 bg-white text-gray-600 focus:outline-none"
                >
                  <option value="">All check types</option>
                  {Object.entries(EVAL_META).map(([k, v]) => (
                    <option key={k} value={k}>{v.title}</option>
                  ))}
                </select>
                <select
                  value={filterFlag}
                  onChange={e => { setFilterFlag(e.target.value); setPage(0); }}
                  className="text-xs border border-gray-200 rounded-lg px-2.5 py-1.5 bg-white text-gray-600 focus:outline-none"
                >
                  <option value="">All results</option>
                  <option value="PASS">Passed only</option>
                  <option value="WARN">Warnings only</option>
                  <option value="FAIL">Failed only</option>
                </select>
              </div>
            )}
          </div>

          {showResults && (
            results.length === 0 && !loading ? (
              <div className="text-center py-8 text-gray-400 text-sm border border-gray-200 rounded-xl bg-white">
                No results match the current filters.
              </div>
            ) : (
              <div className="border border-gray-200 rounded-xl overflow-hidden bg-white shadow-sm">
                <table className="w-full text-sm">
                  <thead className="border-b border-gray-200 bg-gray-50">
                    <tr>
                      <th className="px-4 py-2.5 text-left text-[0.68rem] font-semibold text-gray-400 uppercase tracking-widest">Result</th>
                      <th className="px-4 py-2.5 text-left text-[0.68rem] font-semibold text-gray-400 uppercase tracking-widest">Check Type</th>
                      <th className="px-4 py-2.5 text-left text-[0.68rem] font-semibold text-gray-400 uppercase tracking-widest w-36">Score</th>
                      <th className="px-4 py-2.5 text-left text-[0.68rem] font-semibold text-gray-400 uppercase tracking-widest">
                        <span className="flex items-center gap-1">
                          <span className="inline-block w-2 h-2 rounded-full bg-indigo-400" />
                          AI Source Model
                        </span>
                      </th>
                      <th className="px-4 py-2.5 text-left text-[0.68rem] font-semibold text-gray-400 uppercase tracking-widest">
                        <span className="flex items-center gap-1">
                          <span className="inline-block w-2 h-2 rounded-full bg-violet-400" />
                          AI Judge Model
                        </span>
                      </th>
                      <th className="px-4 py-2.5 text-left text-[0.68rem] font-semibold text-gray-400 uppercase tracking-widest">Judge's Reason</th>
                      <th className="px-4 py-2.5 text-left text-[0.68rem] font-semibold text-gray-400 uppercase tracking-widest">Time</th>
                      <th className="px-4 py-2.5 w-6" />
                    </tr>
                  </thead>
                  <tbody>
                    {loading ? (
                      <tr><td colSpan={8} className="text-center py-8 text-gray-400 text-xs">Loading...</td></tr>
                    ) : (
                      results.map(r => <ResultRow key={r.id} r={r} />)
                    )}
                  </tbody>
                </table>

                {total > PAGE && (
                  <div className="flex items-center justify-between px-4 py-2.5 border-t border-gray-100 text-xs text-gray-400">
                    <span>Showing {page * PAGE + 1}–{Math.min((page + 1) * PAGE, total)} of {total}</span>
                    <div className="flex gap-2">
                      <button disabled={page === 0} onClick={() => setPage(p => p - 1)}
                        className="px-3 py-1 border border-gray-200 rounded-md disabled:opacity-40 hover:bg-gray-50">
                        ← Prev
                      </button>
                      <button disabled={(page + 1) * PAGE >= total} onClick={() => setPage(p => p + 1)}
                        className="px-3 py-1 border border-gray-200 rounded-md disabled:opacity-40 hover:bg-gray-50">
                        Next →
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )
          )}
        </section>


      </div>
    </div>
  );
}
