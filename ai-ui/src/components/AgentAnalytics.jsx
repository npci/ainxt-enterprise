// SPDX-License-Identifier: MIT
import { useState, useEffect, useCallback, useRef } from "react";
import {
  BarChart2, Activity, DollarSign, Zap, Users, RefreshCw,
  TrendingUp, TrendingDown, Cpu, GitBranch, ChevronDown, AlertTriangle,
  CheckCircle, Clock, Cloud, Loader2, XCircle, AlertCircle,
} from "lucide-react";
import { toIST } from "../utils/time";
import { API_BASE as API, authFetch } from '../config';
import { setSessionData, getSessionData, removeSessionData } from '../utils/storageUtils';
import { DonutChart } from "./budget/UtilizationView";

// ── Shared helpers ────────────────────────────────────────────

function StatCard({ icon: Icon, label, value, sub, color = "blue" }) {
  const bg = {
    blue:   "bg-blue-50 border-blue-100 text-blue-700",
    green:  "bg-green-50 border-green-100 text-green-700",
    purple: "bg-purple-50 border-purple-100 text-purple-700",
    orange: "bg-orange-50 border-orange-100 text-orange-700",
    gray:   "bg-gray-50 border-gray-100 text-gray-600",
  }[color];
  return (
    <div className={`rounded-lg border p-4 ${bg}`}>
      <div className="flex items-center gap-2 mb-1">
        <Icon size={14} />
        <span className="text-xs font-medium uppercase tracking-wide">{label}</span>
      </div>
      <div className="text-2xl font-bold">{value ?? "—"}</div>
      {sub && <div className="text-xs mt-0.5 opacity-70">{sub}</div>}
    </div>
  );
}

function Bar({ value, max, color = "bg-indigo-500" }) {
  const pct = max ? Math.min(100, Math.round((value / max) * 100)) : 0;
  return (
    <div className="flex-1 bg-gray-100 rounded-full h-1.5">
      <div className={`${color} h-1.5 rounded-full`} style={{ width: `${pct}%` }} />
    </div>
  );
}

// ── Platform Overview ─────────────────────────────────────────

// Day/Week/Month/Quarter tabs, matching the granularity control already
// shipped on Cloud Usage. Selecting a tab scopes every Platform Overview
// query to that window server-side (see /analytics/platform?granularity=)
// instead of the previous unbounded scan of the entire model_usages table.
const PLATFORM_GRANULARITIES = [
  { key: "day",     label: "Day",     hint: "Last 24 hours" },
  { key: "week",    label: "Week",    hint: "Last 7 days" },
  { key: "month",   label: "Month",   hint: "Current month" },
  { key: "quarter", label: "Quarter", hint: "Last 3 months" },
];

function PlatformDashboard() {
  const [granularity, setGranularity] = useState("day");
  const [data,     setData]     = useState(null);
  const [telemetry,setTelemetry]= useState(null);
  const [loading,  setLoading]  = useState(true);

  // Lazy-loading guard: only one request for the platform endpoint may be
  // in flight at a time. Without this, the 30 s auto-refresh timer and a
  // fast Day/Week/Month/Quarter tab click can overlap, stacking concurrent
  // queries against the same table — the exact amplifier called out for the
  // old full-table-scan implementation. `requestSeq` also lets a late
  // response from a since-abandoned granularity be discarded instead of
  // clobbering the currently-selected tab's data.
  const inFlightRef = useRef(false);
  const requestSeqRef = useRef(0);

  const load = useCallback(async (gran) => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    const seq = ++requestSeqRef.current;
    setLoading(true);
    try {
      const [r1, r2] = await Promise.allSettled([
        authFetch(`${API}/analytics/platform?granularity=${gran}`).then(r => r.json()),
        authFetch(`${API}/metrics`).then(r => r.json()),
      ]);
      // Discard stale responses if the user has since switched tabs again.
      if (seq !== requestSeqRef.current) return;
      if (r1.status === "fulfilled") setData(r1.value);
      if (r2.status === "fulfilled") setTelemetry(r2.value?.telemetry || r2.value || null);
    } catch { /* ignore */ }
    finally {
      inFlightRef.current = false;
      if (seq === requestSeqRef.current) setLoading(false);
    }
  }, [inFlightRef, requestSeqRef]);

  // Fetch only the selected window on mount / tab change (lazy per-tab
  // loading — a "Month" click never touches the "Day" data path and vice
  // versa), then keep it fresh on the same 30 s cadence as before.
  useEffect(() => {
    setData(null);
    load(granularity);
    const t = setInterval(() => load(granularity), 30000);
    return () => clearInterval(t);
  }, [granularity, load]);

  const windowLabel = data?.window_label
    || PLATFORM_GRANULARITIES.find(g => g.key === granularity)?.hint
    || "";

  const modelEntries = Object.entries(data?.model_dist || {});
  const modelTotal   = modelEntries.reduce((s, [, v]) => s + v, 0);
  const MODEL_COLORS = ["bg-blue-500","bg-green-500","bg-purple-500","bg-orange-500","bg-red-400","bg-teal-500"];

  const series       = data?.series || [];
  const seriesIsHour = data?.series_granularity === "hour";
  const maxSeries    = Math.max(...series.map(d => d.requests), 1);
  const seriesDense  = series.length > 14;

  // Derived telemetry
  const tel        = telemetry || {};
  const reqTotal   = tel.requests_total || 0;
  const errTotal   = tel.errors_total   || 0;
  const errorRate  = reqTotal > 0 ? ((errTotal / reqTotal) * 100).toFixed(1) : "0";
  const p95        = tel.p95_latency_ms || 0;
  const avgLat     = tel.avg_latency_ms || 0;
  const compBlocks = tel.compliance_blocks || 0;
  const cacheHits  = tel.cache_hits || 0;
  const cacheRate  = reqTotal > 0 ? ((cacheHits / reqTotal) * 100).toFixed(0) : "0";

  const totals     = data?.totals || {};
  const comparison = data?.comparison || {};

  return (
    <div className="space-y-6">

      {/* ── Day / Week / Month / Quarter controls ── */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          {PLATFORM_GRANULARITIES.map(g => (
            <button
              key={g.key}
              onClick={() => setGranularity(g.key)}
              className={`px-3 py-1.5 text-xs font-semibold rounded-sm transition-colors cursor-pointer ${
                granularity === g.key
                  ? "bg-gradient-to-br from-indigo-600 to-violet-600 text-white shadow-sm"
                  : "bg-gray-50 text-gray-600 hover:bg-gray-100"
              }`}
            >
              {g.label}
            </button>
          ))}
        </div>
        {loading && data && (
          <span className="text-[10px] text-gray-400 inline-flex items-center gap-1">
            <Loader2 size={11} className="animate-spin" /> Refreshing…
          </span>
        )}
      </div>

      {loading && !data ? (
        <div className="flex items-center justify-center h-64 text-gray-400 text-sm">
          <Loader2 size={16} className="animate-spin mr-2" /> Loading platform analytics…
        </div>
      ) : !data ? (
        <div className="text-gray-400 text-center mt-20 text-sm">No data available</div>
      ) : (
      <>

      {/* ── Usage overview cards, scoped to the selected window ── */}
      <div>
        <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3">
          Usage Overview · {windowLabel}
        </h3>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <StatCard icon={Activity}   label="Requests"  value={totals.requests?.toLocaleString() ?? 0}  sub={<TrendPill value={comparison.requests_pct_change} />} color="blue" />
          <StatCard icon={Zap}        label="Tokens"    value={totals.tokens?.toLocaleString()   ?? 0}  sub={<TrendPill value={comparison.tokens_pct_change} />}   color="purple" />
          <StatCard icon={DollarSign} label="Cost"      value={`$${(totals.cost_usd || 0).toFixed(4)}`} sub={<TrendPill value={comparison.cost_pct_change} />}     color="orange" />
          <StatCard icon={Users}      label="Agents"
            value={data.agent_stats?.total ?? "—"}
            sub={`${data.agent_stats?.production ?? 0} in production`}
            color="green" />
        </div>
      </div>

      {/* ── System Health Strip (from /metrics) — only shown when OTEL is active ── */}
      {telemetry?.otlp_enabled && (
        <div>
          <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3">System Health</h3>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <StatCard icon={AlertTriangle} label="Error Rate"
              value={`${errorRate}%`}
              sub={`${errTotal.toLocaleString()} total errors`}
              color={parseFloat(errorRate) > 5 ? "orange" : "green"} />
            <StatCard icon={Clock} label="p95 Latency"
              value={p95 ? `${Math.round(p95)}ms` : "—"}
              sub={`avg ${Math.round(avgLat)}ms`}
              color={p95 > 8000 ? "orange" : "green"} />
            <StatCard icon={CheckCircle} label="Cache Hit Rate"
              value={`${cacheRate}%`}
              sub={`${cacheHits.toLocaleString()} hits`}
              color="blue" />
            <StatCard icon={Cpu} label="Compliance Blocks"
              value={compBlocks.toLocaleString()}
              sub="PCI/PII violations"
              color={compBlocks > 0 ? "orange" : "gray"} />
          </div>
        </div>
      )}


      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">

        {/* ── Model distribution ── */}
        <div className="bg-white border border-gray-100 rounded-lg p-4">
          <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3 flex items-center gap-1.5">
            <Cpu size={11} /> Model Distribution
          </h3>
          {modelEntries.length === 0
            ? <p className="text-xs text-gray-400 pt-4">No model usage recorded in this window</p>
            : <div className="space-y-2">
                {modelEntries.sort((a,b) => b[1]-a[1]).map(([model, count], i) => (
                  <div key={model} className="flex items-center gap-2">
                    <div className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${MODEL_COLORS[i % MODEL_COLORS.length]}`} />
                    <span className="text-xs text-gray-700 flex-1 truncate">{model}</span>
                    <Bar value={count} max={modelTotal} color={MODEL_COLORS[i % MODEL_COLORS.length]} />
                    <span className="text-xs text-gray-500 w-8 text-right">{count}</span>
                    <span className="text-xs text-gray-400 w-8 text-right">{Math.round(count/modelTotal*100)}%</span>
                  </div>
                ))}
              </div>
          }
        </div>

        {/* ── Top agents ── */}
        <div className="bg-white border border-gray-100 rounded-lg p-4">
          <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3 flex items-center gap-1.5">
            <BarChart2 size={11} /> Top Agents by Usage
          </h3>
          {(data.top_agents || []).length === 0
            ? <p className="text-xs text-gray-400 pt-4">No agent runs recorded in this window</p>
            : <div className="space-y-2">
                {(data.top_agents || []).slice(0,8).map((a, i) => {
                  const maxReq = data.top_agents[0]?.requests || 1;
                  return (
                    <div key={a.agent} className="flex items-center gap-2">
                      <span className="text-xs text-gray-400 w-4 text-right">{i+1}</span>
                      <span className="text-xs text-gray-700 w-32 truncate">{a.agent}</span>
                      <Bar value={a.requests} max={maxReq} />
                      <span className="text-xs text-gray-600 w-8 text-right">{a.requests}</span>
                      <span className="text-xs text-gray-400 w-14 text-right">${a.cost_usd.toFixed(4)}</span>
                    </div>
                  );
                })}
              </div>
          }
        </div>

        {/* ── SDLC pipeline summary ── */}
        <div className="bg-white border border-gray-100 rounded-lg p-4">
          <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3 flex items-center gap-1.5">
            <GitBranch size={11} /> SDLC Pipeline Summary
          </h3>
          {Object.keys(data.sdlc_summary || {}).length === 0
            ? <p className="text-xs text-gray-400 pt-4">No SDLC runs in this window</p>
            : <div className="space-y-1.5">
                {Object.entries(data.sdlc_summary).sort((a,b) => b[1]-a[1]).map(([state, count]) => {
                  const total = Object.values(data.sdlc_summary).reduce((s,v) => s+v, 0);
                  const color = state === "COMPLETE" ? "bg-green-500" : state === "FAILED" ? "bg-red-400" :
                    state.includes("AWAITING") ? "bg-yellow-400" : "bg-indigo-400";
                  return (
                    <div key={state} className="flex items-center gap-2">
                      <span className="text-xs text-gray-600 w-44 truncate">{state.replace(/_/g," ")}</span>
                      <Bar value={count} max={total} color={color} />
                      <span className="text-xs text-gray-500 w-6 text-right">{count}</span>
                    </div>
                  );
                })}
              </div>
          }
        </div>

        {/* ── Request volume across the window — hourly buckets for
            "Day", daily buckets for Week/Month/Quarter. Dense windows
            (>14 points, i.e. Month/Quarter) drop per-bar labels in favor
            of a thin sparkline-style bar with a native tooltip, mirroring
            the Cloud Usage "Daily Spend" chart. ── */}
        <div className="bg-white border border-gray-100 rounded-lg p-4">
          <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3 flex items-center gap-1.5">
            <TrendingUp size={11} /> Request Volume · {seriesIsHour ? "hourly" : "daily"}
          </h3>
          {series.length === 0 ? (
            <p className="text-xs text-gray-400 pt-4">No requests recorded in this window</p>
          ) : !seriesDense ? (
            <div className="flex items-end gap-1.5 h-28">
              {series.map((d, i) => {
                const h = maxSeries > 0 ? Math.max(4, Math.round((d.requests / maxSeries) * 96)) : 4;
                return (
                  <div key={i} className="flex-1 flex flex-col items-center gap-1">
                    <span className="text-[9px] text-gray-400">{d.requests}</span>
                    <div className="w-full bg-indigo-500 rounded-t" style={{ height: `${h}px` }} title={`${d.label}: ${d.requests}`} />
                    <span className="text-[9px] text-gray-400 whitespace-nowrap">{d.label}</span>
                  </div>
                );
              })}
            </div>
          ) : (
            <div>
              <div className="flex items-end gap-px h-16">
                {series.map((d, i) => {
                  const h = maxSeries > 0 ? Math.max(2, Math.round((d.requests / maxSeries) * 64)) : 2;
                  return (
                    <div
                      key={i}
                      className="flex-1 min-w-0 bg-indigo-500 rounded-t hover:bg-indigo-600 transition-colors"
                      style={{ height: `${h}px` }}
                      title={`${d.label}: ${d.requests} requests`}
                    />
                  );
                })}
              </div>
              <div className="flex mt-1">
                {series.map((d, i) => {
                  const tickEvery = Math.ceil(series.length / 8);
                  return (
                    <div key={i} className="flex-1 min-w-0 text-center">
                      {i % tickEvery === 0 && <span className="text-[8px] text-gray-400">{d.label}</span>}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>

      </div>

      {data.note && (
        <p className="text-xs text-gray-400 italic">{data.note}</p>
      )}
      </>
      )}
    </div>
  );
}

// ── Per-agent drill-down (Agent Studio agents) ──────────────────

// Day/Week/Month/Quarter tabs, matching Platform Overview. Selecting a tab
// scopes the agent list's usage badge and the selected agent's full stats
// panel to that window (see /analytics/agent-studio-agents*).
const AGENT_STUDIO_GRANULARITIES = [
  { key: "day",     label: "Day" },
  { key: "week",    label: "Week" },
  { key: "month",   label: "Month" },
  { key: "quarter", label: "Quarter" },
];

function AgentDrillDown() {
  const [granularity, setGranularity] = useState("day");
  const [agents, setAgents]       = useState([]);
  const [searchQ, setSearchQ]     = useState("");
  const [selected, setSelected]   = useState(null);
  const [analytics, setAnalytics] = useState(null);
  const [loading, setLoading]     = useState(false);
  const [windowLabel, setWindowLabel] = useState("");

  useEffect(() => {
    authFetch(`${API}/analytics/agent-studio-agents?granularity=${granularity}`).then(r => r.json())
      .then(d => {
        const list = d.agents || [];
        setAgents(list);
        setWindowLabel(d.window_label || "");
        // Auto-select first agent name that has actual runs in this window; fallback to first in list
        const first = list.find(a => (a.total_runs || 0) > 0) || list[0];
        setSelected(prev => (prev && list.some(a => a.name === prev)) ? prev : (first ? first.name : null));
      }).catch(() => {});
  }, [granularity]);

  const loadAnalytics = useCallback(() => {
    if (!selected) return;
    setLoading(true);
    authFetch(`${API}/analytics/agent-studio-agents/${encodeURIComponent(selected)}?granularity=${granularity}`)
      .then(r => r.json()).then(setAnalytics).catch(() => setAnalytics(null))
      .finally(() => setLoading(false));
  }, [selected, granularity]);

  useEffect(() => {
    loadAnalytics();
    const t = setInterval(loadAnalytics, 15000);
    return () => clearInterval(t);
  }, [loadAnalytics]);

  const filtered = agents.filter(a =>
    !searchQ || a.name.toLowerCase().includes(searchQ.toLowerCase())
  );

  return (
    <div className="flex flex-col w-full h-full overflow-hidden">
      {/* Day / Week / Month / Quarter controls */}
      <div className="flex items-center gap-2 px-4 py-3 border-b border-gray-100 flex-shrink-0">
        {AGENT_STUDIO_GRANULARITIES.map(g => (
          <button
            key={g.key}
            onClick={() => setGranularity(g.key)}
            className={`px-3 py-1.5 text-xs font-semibold rounded-sm transition-colors cursor-pointer ${
              granularity === g.key
                ? "bg-gradient-to-br from-indigo-600 to-violet-600 text-white shadow-sm"
                : "bg-gray-50 text-gray-600 hover:bg-gray-100"
            }`}
          >
            {g.label}
          </button>
        ))}
        {windowLabel && <span className="text-[10px] text-gray-400 ml-1">{windowLabel}</span>}
      </div>

      <div className="flex flex-1 overflow-hidden">
      {/* Agent list — fixed width, independent scroll */}
      <div className="w-72 bg-gray-50 flex-shrink-0 flex flex-col border-r border-gray-200 overflow-hidden">
        <div className="p-3 border-b border-gray-100">
          <input
            type="text"
            value={searchQ}
            onChange={e => setSearchQ(e.target.value)}
            placeholder="Search Agent Studio agents…"
            className="w-full px-3 py-1.5 text-sm border border-gray-200 rounded-lg outline-none focus:border-indigo-300 shadow-sm bg-white"
          />
        </div>
        <div className="flex-1 overflow-y-auto overflow-x-hidden">
          {filtered.map(a => (
            <button key={a.name} onClick={() => setSelected(a.name)}
              className={`w-full m-0.5 text-left px-2 py-1.5 border-b-1 border-b-gray-100 text-sm transition-colors cursor-pointer rounded  ${
                selected === a.name ? "bg-indigo-50 text-indigo-700 font-semibold border-l-2 border-l-indigo-500" : "text-gray-600 hover:bg-gray-100"
              }`}
              >
              <span className="truncate block">{a.name}</span>
            </button>
          ))}
          {!filtered.length && (
            <div className="text-xs text-gray-400 px-3 pt-3">
              {agents.length ? "No matches" : "No Agent Studio agents"}
            </div>
          )}
        </div>
      </div>

      {/* Stats — independent scroll */}
      <div className="flex-1 overflow-y-auto p-6 bg-white">
        {!selected ? (
          <div className="text-gray-400 text-sm mt-8">Select an agent</div>
        ) : loading && !analytics ? (
          <div className="text-gray-400 text-sm mt-8">Loading…</div>
        ) : !analytics ? (
          <div className="text-gray-400 text-sm mt-8">No data for this agent</div>
        ) : (
          <div className="space-y-6">
            {/* Agent metadata header */}
            {(analytics.agent_meta?.description || analytics.agent_meta?.created_at) && (
              <div className="text-xs text-gray-500 bg-gray-50 rounded-lg px-4 py-2 border border-gray-100">
                {analytics.agent_meta.description || "No description"}
                {analytics.agent_meta.created_at && (
                  <span className="ml-2 text-gray-400">
                    · First created {toIST(analytics.agent_meta.created_at)}
                  </span>
                )}
                {analytics.agent_meta.num_instances > 1 && (
                  <span className="ml-2 text-gray-400">
                    · Cloned into {analytics.agent_meta.num_instances} agents across {analytics.agent_meta.num_users} users
                  </span>
                )}
              </div>
            )}

            <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
              <StatCard icon={Users}      label="No. of Users"  value={(analytics.agent_meta?.num_users ?? 0).toLocaleString()} color="gray" />
              <StatCard icon={Activity}   label="Total Calls"   value={(analytics.total_runs ?? 0).toLocaleString()}            color="blue" />
              <StatCard icon={Zap}        label="Total Tokens"  value={(analytics.total_tokens ?? 0).toLocaleString()}           color="purple" />
              <StatCard icon={TrendingUp} label="Success Rate"  value={`${analytics.success_rate_pct ?? 0}%`}  sub={`avg ${analytics.avg_latency_ms ?? 0}ms`} color="green" />
              <StatCard icon={DollarSign} label="Total Cost"    value={`$${(analytics.total_cost_usd ?? 0).toFixed(4)}`}         color="orange" />
            </div>

            {analytics.total_runs === 0 && (
              <p className="text-xs text-gray-400 italic">
                No model calls recorded for this agent in this window.
              </p>
            )}

            {/* Model usage */}
            {Object.keys(analytics.model_usage || {}).length > 0 && (
              <div className="bg-white border border-gray-100 rounded-lg p-4">
                <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3">Model Usage</div>
                <div className="space-y-2">
                  {Object.entries(analytics.model_usage).map(([model, count], i) => {
                    const total = Object.values(analytics.model_usage).reduce((s,v) => s+v, 0);
                    const COLORS = ["bg-blue-500","bg-green-500","bg-purple-500","bg-orange-500"];
                    return (
                      <div key={model} className="flex items-center gap-2">
                        <div className={`w-2.5 h-2.5 rounded-full ${COLORS[i%COLORS.length]}`} />
                        <span className="text-xs text-gray-700 flex-1">{model}</span>
                        <Bar value={count} max={total} color={COLORS[i%COLORS.length]} />
                        <span className="text-xs text-gray-500 w-8 text-right">{count}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {analytics.note && <p className="text-xs text-gray-400 italic">{analytics.note}</p>}
          </div>
        )}
      </div>
      </div>
    </div>
  );
}

// ── Cloud Usage ───────────────────────────────────────────────

const PROVIDER_META = {
  openai:    { label: "OpenAI",    color: "bg-blue-500",   light: "bg-blue-50 text-blue-700" },
  anthropic: { label: "Claude",    color: "bg-purple-500", light: "bg-purple-50 text-purple-700" },
  gemini:    { label: "Gemini",    color: "bg-teal-500",   light: "bg-teal-50 text-teal-700" },
};

// token_type badge styling for the Model Breakdown panel. 'blended' means
// that provider's spend has not been itemised by token class yet (e.g.
// Gemini's CSV-sourced spend) — the cost shown is the full per-model figure,
// not just one token class, so it's styled distinctly from the real classes.
const TOKEN_TYPE_META = {
  uncached:       { label: "Input",           className: "bg-gray-100 text-gray-600" },
  cache_read:     { label: "Cache Read",      className: "bg-teal-50 text-teal-700" },
  cache_write_5m: { label: "Cache Write 5m",  className: "bg-amber-50 text-amber-700" },
  cache_write_1h: { label: "Cache Write 1h",  className: "bg-amber-50 text-amber-700" },
  output:         { label: "Output",          className: "bg-gray-100 text-gray-600" },
  non_token:      { label: "Non-token",       className: "bg-gray-50 text-gray-400 italic" },
  blended:        { label: "Blended*",        className: "bg-gray-50 text-gray-400 italic" },
};

// Fixed left-to-right token_type ordering + hex colors for the grouped bar
// chart below. Mirrors _TT_ORDER / _TT_CHART_COLOR in
// services/llm_spend/report_builder.py (the same palette used by the
// email-digest chart) so a token type reads as the same color everywhere
// a user might see it — dashboard or email.
const TOKEN_TYPE_ORDER = [
  "uncached", "cache_read", "cache_write_5m", "cache_write_1h",
  "output", "non_token", "blended",
];
const TOKEN_TYPE_COLOR = {
  uncached:       "#374151", // slate-700
  cache_read:     "#0e7490", // cyan-700
  cache_write_5m: "#d97706", // amber-600
  cache_write_1h: "#92400e", // amber-900
  output:         "#1e3a8a", // blue-900
  non_token:      "#9ca3af", // gray-400
  blended:        "#d1d5db", // gray-300
};

// ── Model Breakdown — grouped bar chart, one chart per provider ────────────
//
// Replaces the old flat row-per-(model,token_type) list. Within a provider's
// chart, the X axis is models (sorted by total cost desc); each model gets
// a cluster of side-by-side bars — one bar per token_type it has spend in —
// rather than one stacked bar, so individual token-type costs are easier to
// compare at a glance across models. Hovering (or focusing, for keyboard
// users) a bar reveals its exact cost and token count via both a native SVG
// <title> tooltip and a persistent readout below the chart.
function ModelBreakdownCharts({ modelBreakdown }) {
  const [hover, setHover] = useState(null); // { provider, model, tokenType }

  if (!modelBreakdown || modelBreakdown.length === 0) {
    return <p className="text-xs text-gray-400 pt-4">No model data for this window</p>;
  }

  // provider -> model -> token_type -> { cost, tokens }
  const byProvider = {};
  for (const m of modelBreakdown) {
    const models = (byProvider[m.provider] ??= {});
    const tts = (models[m.model] ??= {});
    tts[m.token_type] = {
      cost: Number(m.cost_usd) || 0,
      tokens: Number(m.input_tokens ? m.input_tokens : m.output_tokens) || 0,
    };
  }

  const providerTotal = (models) =>
    Object.values(models).reduce(
      (s, tts) => s + Object.values(tts).reduce((s2, v) => s2 + v.cost, 0), 0
    );

  const providers = Object.keys(byProvider).sort(
    (a, b) => providerTotal(byProvider[b]) - providerTotal(byProvider[a])
  );

  return (
    <div className="space-y-4">
      {providers.map(provider => (
        <ProviderModelBarChart
          key={provider}
          provider={provider}
          models={byProvider[provider]}
          hover={hover?.provider === provider ? hover : null}
          onHover={(model, tokenType) => setHover(model ? { provider, model, tokenType } : null)}
        />
      ))}
    </div>
  );
}

function ProviderModelBarChart({ provider, models, hover, onHover }) {
  const meta = PROVIDER_META[provider] || { label: provider, color: "bg-gray-500" };

  const modelTotal = (tts) => Object.values(tts).reduce((s, v) => s + v.cost, 0);
  const modelNames = Object.keys(models).sort((a, b) => modelTotal(models[b]) - modelTotal(models[a]));
  const presentTts = TOKEN_TYPE_ORDER.filter(tt => modelNames.some(m => models[m][tt]));

  const maxCost = Math.max(
    ...modelNames.flatMap(m => presentTts.map(tt => models[m][tt]?.cost || 0)),
    0.000001,
  );

  // Layout constants (design pixels; SVG scales via viewBox so it stays
  // crisp at any zoom without a separate @2x asset).
  const barW      = 16;
  const barGap    = 4;
  const groupGap  = 28;
  const chartH    = 140;
  const topPad    = 6;
  const labelPad  = 34;
  const groupW    = presentTts.length * barW + Math.max(presentTts.length - 1, 0) * barGap;
  const leftPad   = 8;
  const width     = leftPad * 2 + modelNames.length * groupW + Math.max(modelNames.length - 1, 0) * groupGap;
  const height    = topPad + chartH + labelPad;

  const hoveredEntry = hover ? models[hover.model]?.[hover.tokenType] : null;

  return (
    <div className="border border-gray-100 rounded-lg p-3">
      <div className="flex items-center justify-between flex-wrap gap-2 mb-2">
        <div className="flex items-center gap-2">
          <div className={`w-2.5 h-2.5 rounded-full ${meta.color}`} />
          <span className="text-xs font-semibold text-gray-700">{meta.label}</span>
        </div>
        <div className="flex flex-wrap gap-2.5">
          {presentTts.map(tt => (
            <div key={tt} className="flex items-center gap-1">
              <span className="w-2 h-2 rounded-sm inline-block" style={{ background: TOKEN_TYPE_COLOR[tt] }} />
              <span className="text-[9px] text-gray-500">{TOKEN_TYPE_META[tt]?.label || tt}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="overflow-x-auto">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          width={width}
          height={height}
          style={{ display: "block" }}
          role="img"
          aria-label={`${meta.label} spend by model`}
        >
          {modelNames.map((model, gi) => {
            const gx = leftPad + gi * (groupW + groupGap);
            return (
              <g key={model}>
                {presentTts.map((tt, ti) => {
                  const entry = models[model][tt];
                  if (!entry) return null;
                  const h = Math.max((entry.cost / maxCost) * chartH, 2);
                  const x = gx + ti * (barW + barGap);
                  const y = topPad + (chartH - h);
                  const isHovered = hover && hover.model === model && hover.tokenType === tt;
                  const dimmed = hover && !isHovered;
                  return (
                    <rect
                      key={tt}
                      x={x} y={y} width={barW} height={h} rx={1.5}
                      fill={TOKEN_TYPE_COLOR[tt]}
                      opacity={dimmed ? 0.35 : 1}
                      style={{ cursor: "pointer", transition: "opacity 0.12s" }}
                      onMouseEnter={() => onHover(model, tt)}
                      onMouseLeave={() => onHover(null, null)}
                      onFocus={() => onHover(model, tt)}
                      onBlur={() => onHover(null, null)}
                      tabIndex={0}
                    >
                      <title>
                        {`${model} · ${TOKEN_TYPE_META[tt]?.label || tt}: ${_fmtUsd(entry.cost)} · ${_fmtNum(entry.tokens)} tokens`}
                      </title>
                    </rect>
                  );
                })}
                <text
                  x={gx + groupW / 2} y={topPad + chartH + 16}
                  textAnchor="middle" fontSize="9.5" fill="#6b7280"
                >
                  {model.length > 16 ? model.slice(0, 15) + "…" : model}
                </text>
                <text
                  x={gx + groupW / 2} y={topPad + chartH + 28}
                  textAnchor="middle" fontSize="9" fill="#9ca3af"
                >
                  {_fmtUsd(modelTotal(models[model]))}
                </text>
              </g>
            );
          })}
        </svg>
      </div>

      {/* Persistent hover/focus readout — exact cost + token count for the
          highlighted token type, so the information isn't only available
          via the native SVG title tooltip (which some browsers delay or
          truncate). */}
      <div className="mt-2 text-[11px] min-h-[1.25rem]">
        {hoveredEntry ? (
          <span className="inline-flex items-center gap-1.5 bg-gray-50 border border-gray-100 rounded px-2 py-1">
            <span className="w-2 h-2 rounded-sm inline-block" style={{ background: TOKEN_TYPE_COLOR[hover.tokenType] }} />
            <span className="font-medium text-gray-700">{hover.model}</span>
            <span className="text-gray-400">·</span>
            <span className="text-gray-600">{TOKEN_TYPE_META[hover.tokenType]?.label || hover.tokenType}</span>
            <span className="text-gray-400">·</span>
            <span className="font-semibold text-gray-800">{_fmtUsd(hoveredEntry.cost)}</span>
            <span className="text-gray-400">·</span>
            <span className="text-gray-500">{_fmtNum(hoveredEntry.tokens)} tokens</span>
          </span>
        ) : (
          <span className="text-gray-300 italic">Hover or focus a bar for exact cost + token count</span>
        )}
      </div>
    </div>
  );
}

const GRANULARITIES = [
  { key: "day", label: "Day" },
  { key: "week", label: "Week" },
  { key: "month", label: "Month" },
  { key: "quarter", label: "Quarter" },
];

function _fmtUsd(n) {
  const v = Number(n || 0);
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(2)}K`;
  return `$${v.toFixed(4)}`;
}

function _fmtNum(n) {
  return Number(n || 0).toLocaleString();
}

function _yesterdayStr() {
  const d = new Date();
  d.setDate(d.getDate() - 1);
  return d.toISOString().split("T")[0];
}

function _todayStr() {
  return new Date().toISOString().split("T")[0];
}

function TrendPill({ value }) {
  if (value === null || value === undefined || isNaN(value)) {
    return <span className="text-[10px] text-gray-400">—</span>;
  }
  const isUp = value >= 0;
  const Icon = isUp ? TrendingUp : TrendingDown;
  const color = isUp ? "text-green-600" : "text-red-600";
  return (
    <span className={`inline-flex items-center gap-0.5 text-[10px] font-medium ${color}`}>
      <Icon size={10} /> {Math.abs(value).toFixed(1)}%
    </span>
  );
}

// Persist fetch state across tab switches via sessionStorage
const FETCH_STATE_KEY = "llm_spend_fetch_state";

function _saveFetchState(fetching, fetchStatus, dispatch) {
  try {
    if (fetching && fetchStatus) {
      const safeDispatch = dispatch ? {
        since: String(dispatch.since || ''),
        granularity: String(dispatch.granularity || ''),
        reference_date: String(dispatch.reference_date || ''),
      } : null;
      const safeStatus = fetchStatus ? Object.fromEntries(
        Object.entries(fetchStatus).map(([k, v]) => [String(k), { status: String(v?.status ?? '') }])
      ) : null;
      setSessionData(FETCH_STATE_KEY, { fetching: !!fetching, fetchStatus: safeStatus, dispatch: safeDispatch, ts: Date.now() });
    } else {
      removeSessionData(FETCH_STATE_KEY);
    }
  } catch { /* storage unavailable */ }
}

function _loadFetchState() {
  try {
    const s = getSessionData(FETCH_STATE_KEY);
    if (!s) return null;
    // Expire after 10 minutes
    if (Date.now() - s.ts > 10 * 60 * 1000) {
      removeSessionData(FETCH_STATE_KEY);
      return null;
    }
    return s;
  } catch { return null; }
}

function _humanFetchError(err) {
  if (!err) return null;
  const s = String(err);
  if (s.length <= 120) return s;
  // Trim to first sentence or 120 chars
  const dot = s.indexOf(". ");
  return dot > 0 && dot < 120 ? s.slice(0, dot + 1) : s.slice(0, 120) + "…";
}

function CloudUsageDashboard() {
  const [granularity, setGranularity] = useState("month");
  const [referenceDate, setReferenceDate] = useState(_yesterdayStr);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState("");

  // Restore persisted fetch state on mount
  const _saved = _loadFetchState();
  const [fetching, setFetching] = useState(_saved?.fetching ?? false);
  const [fetchStatus, setFetchStatus] = useState(_saved?.fetchStatus ?? null);
  // { since, granularity, reference_date } captured when the fetch was dispatched
  const [dispatch, setDispatch] = useState(_saved?.dispatch ?? null);

  // Keep sessionStorage in sync whenever fetch state changes
  useEffect(() => {
    _saveFetchState(fetching, fetchStatus, dispatch);
  }, [fetching, fetchStatus, dispatch]);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    setData(null);
    try {
      const params = new URLSearchParams({ granularity, reference_date: referenceDate });
      const r = await authFetch(`${API}/admin/llm-spend/usage-summary?${params}`);
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        throw new Error(e.detail || `HTTP ${r.status}`);
      }
      setData(await r.json());
    } catch (err) {
      setError(err.message || "Failed to load cloud usage summary");
    } finally {
      setLoading(false);
    }
  }, [granularity, referenceDate]);

  useEffect(() => {
    const t = setTimeout(load, 0);
    return () => clearTimeout(t);
  }, [load]);

  // Poll fetch-status while a fetch is in progress; auto-expire after 10 min
  useEffect(() => {
    if (!fetching || !dispatch) return;

    let cancelled = false;

    const poll = async () => {
      try {
        const params = new URLSearchParams({
          since: dispatch.since,
          granularity: dispatch.granularity,
          reference_date: dispatch.reference_date,
        });
        const r = await authFetch(`${API}/admin/llm-spend/fetch-status?${params}`);
        if (!r.ok) return;
        const body = await r.json();
        if (cancelled) return;

        // Merge run results (ok/failed + error/rows) into the banner state.
        setFetchStatus(prev => {
          if (!prev) return prev;
          const merged = { ...prev };
          for (const [prov, run] of Object.entries(body.runs || {})) {
            merged[prov] = { ...merged[prov], status: run.status, rows: run.rows, error: run.error };
          }
          return merged;
        });

        // "done" = every fetched provider now has a run result.
        if (body.done) {
          setFetching(false);
          load(); // refresh dashboard with freshly-landed data
        }
      } catch { /* transient — keep polling */ }
    };

    poll();
    const t = setInterval(poll, 10000);
    const stop = setTimeout(() => setFetching(false), 10 * 60 * 1000);
    return () => { cancelled = true; clearInterval(t); clearTimeout(stop); };
  }, [fetching, dispatch, load]);

  const openPreview = async () => {
    if (fetching) return;
    setError("");
    try {
      const params = new URLSearchParams({ granularity, reference_date: referenceDate });
      const r = await authFetch(`${API}/admin/llm-spend/fetch-preview?${params}`);
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        throw new Error(e.detail || `HTTP ${r.status}`);
      }
      setPreview(await r.json());
    } catch (err) {
      setError(err.message || "Failed to preview fetch gaps");
    }
  };

  const confirmFetch = async () => {
    setPreview(null);
    // Capture the dispatch window + timestamp so polling can match runs.
    const since = new Date().toISOString();
    const thisDispatch = { since, granularity, reference_date: referenceDate };
    setFetching(true);
    setFetchStatus(preview?.providers || null);
    setDispatch(thisDispatch);
    try {
      const params = new URLSearchParams({ granularity, reference_date: referenceDate });
      const r = await authFetch(`${API}/admin/llm-spend/fetch-async?${params}`, { method: "POST" });
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        throw new Error(e.detail || `HTTP ${r.status}`);
      }
      const body = await r.json();
      if (body.accepted) {
        setFetching(true);
        setFetchStatus(body.providers);
      } else {
        // Nothing to fetch — everything already present.
        setFetching(false);
        setFetchStatus(null);
        setDispatch(null);
        load();
      }
    } catch (err) {
      setError(err.message || "Failed to start fetch");
      setFetching(false);
      setFetchStatus(null);
      setDispatch(null);
    }
  };

  const dismissFetch = () => {
    setFetching(false);
    setFetchStatus(null);
    setDispatch(null);
  };

  const current = data?.current || {};
  const comparison = data?.comparison || {};
  const hasData = Boolean(current.has_data);
  const providerTotals = current.provider_totals || [];
  const cacheTotals = current.cache_totals || [];
  const cacheSavings = current.cache_savings || null;
  const maxDaily = Math.max(...(current.daily_series || []).map(d => Number(d.cost_usd)), 1);
  const totalCost = Number(current.total_cost_usd || 0);

  // Derive per-provider fetch result state from fetchStatus
  const fetchProviderState = (info) => {
    if (!info) return { icon: null, color: "", label: "" };
    if (info.action === "skip") return { icon: "skip", color: "text-green-600", label: "Already in database" };
    if (info.status === "ok") return { icon: "ok", color: "text-green-600", label: `Done · ${info.rows ?? 0} rows` };
    if (info.status === "failed") return { icon: "fail", color: "text-red-600", label: _humanFetchError(info.error) || "Fetch failed" };
    // still in progress
    return { icon: "loading", color: "text-indigo-600", label: `Fetching… (~${info.estimated_seconds}s)` };
  };

  const anyFetchFailed = fetchStatus && Object.values(fetchStatus).some(i => i.status === "failed");
  // Header state: running while polling; else completed (ok or with errors).
  const bannerTone = anyFetchFailed ? "error" : fetching ? "running" : "done";
  const bannerHeader = {
    running: "Fetch in progress — do not navigate away",
    done:    "Fetch complete",
    error:   "Fetch completed with errors",
  }[bannerTone];

  return (
    <div className="space-y-6">
      {/* ── Fetch banner — always on top, persists across tab switches ── */}
      {fetchStatus && (
        <div className={`rounded-lg border p-4 ${
          bannerTone === "error" ? "border-red-200 bg-red-50"
          : bannerTone === "done" ? "border-green-200 bg-green-50"
          : "border-indigo-100 bg-indigo-50"
        }`}>
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              {bannerTone === "error" ? <AlertCircle size={14} className="text-red-600" />
                : bannerTone === "done" ? <CheckCircle size={14} className="text-green-600" />
                : <Loader2 size={14} className="text-indigo-600 animate-spin" />}
              <span className={`text-xs font-semibold ${
                bannerTone === "error" ? "text-red-700"
                : bannerTone === "done" ? "text-green-700"
                : "text-indigo-700"
              }`}>
                {bannerHeader}
              </span>
            </div>
            <button onClick={dismissFetch} className="text-gray-400 hover:text-gray-600 cursor-pointer" title="Dismiss">
              <XCircle size={14} />
            </button>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
            {Object.entries(fetchStatus).map(([prov, info]) => {
              const meta = PROVIDER_META[prov] || { label: prov, color: "bg-gray-400" };
              const { icon, color, label } = fetchProviderState(info);
              return (
                <div key={prov} className="bg-white border border-gray-100 rounded-lg p-2.5">
                  <div className="flex items-center gap-2 mb-1">
                    <div className={`w-2 h-2 rounded-full ${meta.color}`} />
                    <span className="text-xs font-medium text-gray-700">{meta.label}</span>
                  </div>
                  <div className={`text-[10px] ${color} inline-flex items-center gap-1`}>
                    {icon === "skip"    && <><CheckCircle size={10} /> {label}</>}
                    {icon === "ok"      && <><CheckCircle size={10} /> {label}</>}
                    {icon === "fail"    && <><XCircle size={10} /> {label}</>}
                    {icon === "loading" && <><Loader2 size={10} className="animate-spin" /> {label}</>}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* ── Controls ── */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          {GRANULARITIES.map(g => (
            <button
              key={g.key}
              onClick={() => setGranularity(g.key)}
              className={`px-3 py-1.5 text-xs font-semibold rounded-sm transition-colors cursor-pointer ${
                granularity === g.key
                  ? "bg-gradient-to-br from-indigo-600 to-violet-600 text-white shadow-sm"
                  : "bg-gray-50 text-gray-600 hover:bg-gray-100"
              }`}
            >
              {g.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <label className="text-xs text-gray-500">Reference date</label>
          <input
            type="date"
            value={referenceDate}
            onChange={e => setReferenceDate(e.target.value)}
            className="px-2 py-1.5 text-xs border border-gray-200 rounded-lg outline-none focus:border-indigo-300"
          />
          {fetching ? (
            <span className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-sm bg-amber-50 border border-amber-200 text-amber-700">
              <Loader2 size={12} className="animate-spin" /> Fetch in progress…
            </span>
          ) : (
            <button
              onClick={openPreview}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-sm bg-indigo-600 text-white hover:bg-indigo-700 cursor-pointer"
            >
              <Cloud size={12} /> Fetch from providers
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700">
          <AlertCircle size={14} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {data && (data.stale_providers || []).length > 0 && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>
            Data for the following providers may be stale or incomplete: {data.stale_providers.join(", ")}.
            Click "Fetch from providers" to refresh.
          </span>
        </div>
      )}

      {loading && !data ? (
        <div className="flex items-center justify-center h-64 text-gray-400 text-sm">
          <Loader2 size={16} className="animate-spin mr-2" /> Loading cloud usage…
        </div>
      ) : !data || !hasData ? (
        <div className="text-gray-400 text-center mt-20 text-sm">No data available</div>
      ) : (
        <>
          {/* Summary cards */}
          <div>
            <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3">
              {granularity} Overview · {data.window_start} to {data.window_end}
            </h3>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
              <StatCard icon={DollarSign} label="Total Cost" value={_fmtUsd(current.total_cost_usd)} sub={<TrendPill value={comparison.cost_pct_change} />} color="orange" />
              <StatCard icon={Zap} label="Input Tokens" value={_fmtNum(current.total_input_tokens)} sub={<TrendPill value={comparison.input_tokens_pct_change} />} color="blue" />
              <StatCard icon={Zap} label="Output Tokens" value={_fmtNum(current.total_output_tokens)} sub={<TrendPill value={comparison.output_tokens_pct_change} />} color="purple" />
              <StatCard icon={Activity} label="Requests" value={_fmtNum(current.total_requests)} sub={<TrendPill value={comparison.requests_pct_change} />} color="green" />
            </div>
          </div>

          {/* Cache savings — only shown when at least one provider has an
              itemised (non-blended) uncached+cache_read pair for this
              window. Absent entirely otherwise, rather than showing a $0
              card that could be misread as "no savings" instead of "no data
              yet". */}
          {cacheSavings && cacheSavings.rows.length > 0 && (
            <div className="bg-white border border-gray-100 rounded-lg p-4">
              <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3 flex items-center gap-1.5">
                <Zap size={11} /> Prompt Cache Savings
              </h3>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-3">
                <div className="rounded-lg border border-teal-100 bg-teal-50 p-3">
                  <div className="text-[10px] uppercase tracking-wide text-gray-500 font-semibold">Saved this {granularity}</div>
                  <div className="text-lg font-bold text-teal-700 mt-0.5">{_fmtUsd(cacheSavings.total_saved_usd)}</div>
                  <div className="text-[10px] text-gray-500 mt-0.5">{Number(cacheSavings.total_saved_pct).toFixed(1)}% vs. full rate</div>
                </div>
                <div className="rounded-lg border border-gray-100 bg-gray-50 p-3">
                  <div className="text-[10px] uppercase tracking-wide text-gray-500 font-semibold">Actually paid</div>
                  <div className="text-base font-semibold text-gray-800 mt-0.5">{_fmtUsd(cacheSavings.total_cache_read_cost_usd)}</div>
                </div>
                <div className="rounded-lg border border-gray-100 bg-gray-50 p-3">
                  <div className="text-[10px] uppercase tracking-wide text-gray-500 font-semibold">Would cost at full rate</div>
                  <div className="text-base font-semibold text-gray-500 mt-0.5">{_fmtUsd(cacheSavings.total_would_cost_usd)}</div>
                </div>
              </div>
              <p className="text-[10px] text-gray-400 italic mt-2 leading-relaxed">
                "Saved" is a counterfactual (full-rate cost minus what was actually billed for cache reads), not an invoice line item.
                {cacheSavings.covered_providers?.length > 0 && (
                  <> Covers: {cacheSavings.covered_providers.map(p => PROVIDER_META[p]?.label || p).join(", ")} only.</>
                )}
                {cacheSavings.excluded_providers?.length > 0 && (
                  <> Excluded (no per-token breakdown yet): {cacheSavings.excluded_providers.map(p => PROVIDER_META[p]?.label || p).join(", ")} — not reflected above.</>
                )}
              </p>
              {current.discount_note && (
                <p className="text-[10px] text-gray-400 italic mt-1.5 pt-1.5 border-t border-gray-100">
                  ℹ {current.discount_note}
                </p>
              )}
            </div>
          )}

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Provider breakdown */}
            <div className="bg-white border border-gray-100 rounded-lg p-4">
              <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3 flex items-center gap-1.5">
                <Cloud size={11} /> Provider Breakdown
              </h3>
              {providerTotals.length === 0 ? (
                <p className="text-xs text-gray-400 pt-4">No provider data for this window</p>
              ) : (
                <div className="space-y-3">
                  {providerTotals.map(pt => {
                    const meta = PROVIDER_META[pt.provider] || { label: pt.provider, color: "bg-gray-500" };
                    const pct = totalCost ? Math.min(100, Math.round((Number(pt.cost_usd) / totalCost) * 100)) : 0;
                    const stale = (data.stale_providers || []).includes(pt.provider);
                    const cacheInfo = cacheTotals.find(c => c.provider === pt.provider);
                    const hasCache = cacheInfo && (Number(cacheInfo.cache_read_tokens) > 0 || Number(cacheInfo.cache_write_cost_usd) > 0);
                    return (
                      <div key={pt.provider} className="space-y-1">
                        <div className="flex items-center justify-between text-xs">
                          <div className="flex items-center gap-2">
                            <div className={`w-2.5 h-2.5 rounded-full ${meta.color}`} />
                            <span className="font-medium text-gray-700">{meta.label}</span>
                            {stale && <span className="text-[9px] text-amber-600 bg-amber-50 px-1.5 py-0.5 rounded-full">stale</span>}
                          </div>
                          <span className="text-gray-600">{_fmtUsd(pt.cost_usd)} ({pct}%)</span>
                        </div>
                        <div className="flex items-center gap-2 text-[10px] text-gray-400">
                          <Bar value={Number(pt.cost_usd)} max={totalCost} color={meta.color} />
                        </div>
                        <div className="flex gap-3 text-[10px] text-gray-500">
                          <span>{_fmtNum(pt.input_tokens)} in</span>
                          <span>{_fmtNum(pt.output_tokens)} out</span>
                          <span>{_fmtNum(pt.requests)} req</span>
                        </div>
                        {hasCache && (
                          <div className="flex gap-3 text-[10px] text-teal-600">
                            <span>{_fmtNum(cacheInfo.cache_read_tokens)} cache-read ({_fmtUsd(cacheInfo.cache_read_cost_usd)})</span>
                            {Number(cacheInfo.cache_write_cost_usd) > 0 && (
                              <span>{_fmtUsd(cacheInfo.cache_write_cost_usd)} cache-write</span>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

          </div>

          {/* Daily spend chart — full-width row of its own so it never
              competes for half the grid's width. For dense windows (month/
              quarter, >14 days) the permanent per-bar cost/date labels are
              dropped in favor of a compact sparkline-style bar with a native
              tooltip, since fixed-width nowrap labels on ~30-90 bars are what
              forced horizontal overflow/scrolling before.

              Skipped entirely on the "Day" tab: with a single day selected,
              daily_series has exactly one point, so the chart is just a
              solid rectangular slab that conveys no information beyond what
              the summary cards above already show. */}
          {granularity !== "day" && (
          <div className="bg-white border border-gray-100 rounded-lg p-4">
            <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3 flex items-center gap-1.5">
              <TrendingUp size={11} /> Daily Spend
            </h3>
            {(current.daily_series || []).length === 0 ? (
              <p className="text-xs text-gray-400 pt-4">No daily data for this window</p>
            ) : (() => {
              const days = current.daily_series;
              const isDense = days.length > 14;
              if (!isDense) {
                return (
                  <div className="flex items-end gap-1.5 h-28">
                    {days.map((d, i) => {
                      const v = Number(d.cost_usd);
                      const h = maxDaily > 0 ? Math.max(4, Math.round((v / maxDaily) * 96)) : 4;
                      return (
                        <div key={i} className="flex-1 flex flex-col items-center gap-1">
                          <span className="text-[9px] text-gray-400">{_fmtUsd(v)}</span>
                          <div className="w-full bg-indigo-500 rounded-t" style={{ height: `${h}px` }} title={`${d.usage_date}: ${_fmtUsd(v)}`} />
                          <span className="text-[9px] text-gray-400 whitespace-nowrap">{d.usage_date.slice(5)}</span>
                        </div>
                      );
                    })}
                  </div>
                );
              }
              // Dense (month/quarter): thin bars, no per-bar labels (they no
              // longer have a fixed min-width, so the row truly fits the
              // container), hover/title still exposes exact date + cost, and
              // a handful of evenly-spaced date ticks anchor the axis.
              const tickEvery = Math.ceil(days.length / 8);
              return (
                <div>
                  <div className="flex items-end gap-px h-16">
                    {days.map((d, i) => {
                      const v = Number(d.cost_usd);
                      const h = maxDaily > 0 ? Math.max(2, Math.round((v / maxDaily) * 64)) : 2;
                      return (
                        <div
                          key={i}
                          className="flex-1 min-w-0 bg-indigo-500 rounded-t hover:bg-indigo-600 transition-colors"
                          style={{ height: `${h}px` }}
                          title={`${d.usage_date}: ${_fmtUsd(v)}`}
                        />
                      );
                    })}
                  </div>
                  <div className="flex mt-1">
                    {days.map((d, i) => (
                      <div key={i} className="flex-1 min-w-0 text-center">
                        {i % tickEvery === 0 && (
                          <span className="text-[8px] text-gray-400">{d.usage_date.slice(5)}</span>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              );
            })()}
          </div>
          )}

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Model breakdown — grouped bar chart(s), one per provider.
                Within a provider's chart, models are grouped along the X
                axis and each model has one side-by-side bar per token_type
                (not stacked), so per-token-type costs are directly
                comparable across models at a glance. Hover/focus a bar for
                its exact cost + token count. */}
            <div className="bg-white border border-gray-100 rounded-lg p-4 lg:col-span-2">
              <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3 flex items-center gap-1.5">
                <Cpu size={11} /> Model Breakdown
              </h3>
              <ModelBreakdownCharts modelBreakdown={current.model_breakdown || []} />
            </div>
          </div>
        </>
      )}

      {/* Fetch preview confirmation dialog */}
      {preview && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="bg-white rounded-lg shadow-lg w-full max-w-md p-5 space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-gray-800">Confirm provider fetch</h3>
              <button onClick={() => setPreview(null)} className="text-gray-400 hover:text-gray-600 cursor-pointer">
                <XCircle size={16} />
              </button>
            </div>
            <p className="text-xs text-gray-500">
              Window: <span className="font-medium text-gray-700">{preview.window_start}</span> to{" "}
              <span className="font-medium text-gray-700">{preview.window_end}</span>
            </p>
            <div className="space-y-2">
              {Object.entries(preview.providers).map(([prov, info]) => {
                const meta = PROVIDER_META[prov] || { label: prov, color: "bg-gray-500" };
                const isSkip = info.action === "skip";
                const isMissing = info.reason === "missing";
                const statusClass = isSkip ? "text-green-600" : isMissing ? "text-red-600" : "text-amber-600";
                const StatusIcon = isSkip ? CheckCircle : isMissing ? XCircle : AlertCircle;
                const label = isSkip ? "Present" : isMissing ? "Missing" : "Partial/stale";
                return (
                  <div key={prov} className="flex items-center justify-between rounded-lg border border-gray-100 p-2.5">
                    <div className="flex items-center gap-2">
                      <div className={`w-2 h-2 rounded-full ${meta.color}`} />
                      <span className="text-xs font-medium text-gray-700">{meta.label}</span>
                    </div>
                    <div className={`text-[10px] text-right ${statusClass}`}>
                      <span className="inline-flex items-center gap-1">
                        <StatusIcon size={10} />
                        {label} · {info.days_present}/{info.days_expected} days present
                        {!isSkip && ` · ${info.days_missing} missing · fetch ~${info.estimated_seconds}s`}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="flex justify-end gap-2 pt-2">
              <button
                onClick={() => setPreview(null)}
                className="px-3 py-1.5 text-xs font-semibold text-gray-600 bg-gray-50 hover:bg-gray-100 rounded-lg cursor-pointer"
              >
                Cancel
              </button>
              <button
                onClick={confirmFetch}
                className="px-3 py-1.5 text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg cursor-pointer"
              >
                Confirm Fetch
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Platform Usage (admin) ────────────────────────────────────

const USAGE_PERIODS = [
  { key: "day",   label: "Day",   hint: "Last 24 hours" },
  { key: "week",  label: "Week",  hint: "Last 7 days" },
  { key: "month", label: "Month", hint: "Current month" },
];

// Inline legend table for the platform usage donuts.
// Extends the budget BreakdownLegend pattern with an extra "Users" column.
function UsageLegend({ rows }) {
  return (
    <div className="w-full overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="bg-gray-50">
          <tr>
            <th className="px-3 py-2 text-left text-xs text-gray-500">Segment</th>
            <th className="px-3 py-2 text-right text-xs text-gray-500">Cost (USD)</th>
            <th className="px-3 py-2 text-right text-xs text-gray-500">Users</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const PALETTE = [
              "#6366f1","#22c55e","#f59e0b","#ef4444","#06b6d4",
              "#a855f7","#ec4899","#14b8a6","#eab308","#3b82f6",
              "#f97316","#84cc16",
            ];
            const color = PALETTE[i % PALETTE.length];
            return (
              <tr key={r.key} className="border-t border-gray-100">
                <td className="px-3 py-2 text-gray-700">
                  <span className="inline-flex items-center gap-2">
                    <span className="inline-block w-3 h-3 rounded-sm flex-shrink-0" style={{ background: color }} />
                    <span className="truncate max-w-[10rem]">{r.key}</span>
                  </span>
                </td>
                <td className="px-3 py-2 text-right text-gray-600">${(r.cost_usd || 0).toFixed(4)}</td>
                <td className="px-3 py-2 text-right text-gray-500">{(r.unique_users ?? 0).toLocaleString()}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function PlatformUsageDashboard() {
  const [period, setPeriod]                 = useState("day");
  const [referenceDate, setReferenceDate]   = useState(_todayStr);
  const [channelData, setChannelData]       = useState(null);
  const [modelData, setModelData]           = useState(null);
  const [loading, setLoading]               = useState(false);
  const [error, setError]                   = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError("");
      try {
        const q = new URLSearchParams({ period, reference_date: referenceDate });
        const [chRes, mdRes] = await Promise.all([
          authFetch(`${API}/budget/admin/platform-utilization?dimension=channel&${q}`),
          authFetch(`${API}/budget/admin/platform-utilization?dimension=model&${q}`),
        ]);
        if (!chRes.ok || !mdRes.ok) {
          const e = await (chRes.ok ? mdRes : chRes).json().catch(() => ({}));
          throw new Error(e.detail || "Failed to load platform utilization");
        }
        const [ch, md] = await Promise.all([chRes.json(), mdRes.json()]);
        if (!cancelled) {
          setChannelData(ch);
          setModelData(md);
        }
      } catch (err) {
        if (!cancelled) setError(err.message || "Failed to load platform utilization");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [period, referenceDate]);

  // Prefer the server-resolved window so the
  // header reflects exactly the range that was aggregated; fall back to the
  // static hint before the first response lands.
  const windowLabel = channelData?.window_start && channelData?.window_end
    ? `${channelData.window_start} to ${channelData.window_end}`
    : (USAGE_PERIODS.find(p => p.key === period)?.hint ?? "");
  // Short label for the compact KPI cards (the full window range goes in the header).
  const shortPeriodLabel = USAGE_PERIODS.find(p => p.key === period)?.label ?? "";
  const channelBreakdown = channelData?.breakdown ?? [];
  const modelBreakdown   = modelData?.breakdown   ?? [];
  const totalCost        = channelData?.total_cost_usd     ?? 0;
  const totalUsers       = channelData?.total_unique_users ?? 0;
  const totalRequests    = channelBreakdown.reduce((s, r) => s + r.requests, 0);

  const channelPieData = channelBreakdown.map(r => ({ key: r.key, value: r.cost_usd }));
  const modelPieData   = modelBreakdown.map(r => ({ key: r.key, value: r.cost_usd }));

  return (
    <div className="space-y-6">

      {/* ── Controls: period tabs + reference date (mirrors Cloud Usage) ── */}
      <div className="space-y-2">
        <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
          Platform Usage · {windowLabel}
        </h3>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex gap-1">
            {USAGE_PERIODS.map(p => (
              <button
                key={p.key}
                onClick={() => setPeriod(p.key)}
                className={`px-3 py-1.5 text-xs font-semibold rounded-sm transition-colors cursor-pointer ${
                  period === p.key
                    ? "bg-gradient-to-br from-indigo-600 to-violet-600 text-white shadow-sm"
                    : "bg-gray-50 text-gray-600 hover:bg-gray-100"
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-gray-500">Reference date</label>
            <input
              type="date"
              value={referenceDate}
              max={_todayStr()}
              onChange={e => setReferenceDate(e.target.value || _todayStr())}
              className="px-2 py-1.5 text-xs border border-gray-200 rounded-lg outline-none focus:border-indigo-300"
            />
            {loading && channelData && (
              <span className="text-[10px] text-gray-400 inline-flex items-center gap-1">
                <Loader2 size={11} className="animate-spin" /> Refreshing…
              </span>
            )}
          </div>
        </div>
      </div>

      {/* ── Loading / error states ── */}
      {loading && !channelData && (
        <div className="flex items-center justify-center h-48 text-gray-400 text-sm">
          <Loader2 size={16} className="animate-spin mr-2" /> Loading platform usage…
        </div>
      )}
      {error && (
        <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg p-3">{error}</div>
      )}

      {!loading && !error && channelData && (
        <>
          {/* ── Headline KPI cards ── */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <StatCard icon={DollarSign} label="Total Spend"    value={`$${totalCost.toFixed(2)}`}              sub={shortPeriodLabel} color="orange" />
            <StatCard icon={Users}      label="Active Users"   value={totalUsers.toLocaleString()}              sub="across all channels" color="blue" />
            <StatCard icon={Activity}   label="Total Requests" value={totalRequests.toLocaleString()}           sub={shortPeriodLabel} color="purple" />
            <StatCard icon={Zap}        label="Channels"       value={channelBreakdown.length.toLocaleString()} sub="active this period"  color="green" />
          </div>

          {/* ── Channel + Model breakdown side by side ── */}
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">

            {/* By Channel */}
            <div className="bg-white border border-gray-100 rounded-lg p-4 space-y-4">
              <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide flex items-center gap-1.5">
                <Activity size={11} /> By Channel
              </h3>
              {channelBreakdown.length === 0 ? (
                <p className="text-xs text-gray-400 italic py-4">No channel usage recorded this period.</p>
              ) : (
                <div className="flex flex-col items-center gap-4">
                  <DonutChart data={channelPieData} size={180} thickness={38} />
                  <UsageLegend rows={channelBreakdown} />
                </div>
              )}
            </div>

            {/* By Model */}
            <div className="bg-white border border-gray-100 rounded-lg p-4 space-y-4">
              <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide flex items-center gap-1.5">
                <Cpu size={11} /> By Model
              </h3>
              {modelBreakdown.length === 0 ? (
                <p className="text-xs text-gray-400 italic py-4">No model usage recorded this period.</p>
              ) : (
                <div className="flex flex-col items-center gap-4">
                  <DonutChart data={modelPieData} size={180} thickness={38} />
                  <UsageLegend rows={modelBreakdown} />
                </div>
              )}
            </div>

          </div>
        </>
      )}
    </div>
  );
}

// ── Main ──────────────────────────────────────────────────────

export default function AgentAnalytics({ user }) {
  const [tab, setTab] = useState("cloud");
  const isAdmin = user?.role === "admin";

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100 flex-shrink-0">
        <div className="flex items-center gap-2">
          <BarChart2 size={18} className="text-indigo-700" />
          <h1 className="text-sm font-semibold  text-indigo-700">Analytics</h1>
        </div>
        <div className="flex gap-1 bg-gray-50 rounded-lg p-2">
          <button
            onClick={() => setTab("platform")}
            className={`px-3 py-1.5 text-xs rounded-sm font-semibold transition-colors cursor-pointer ${tab === "platform" ? "brand-grad hover:opacity-70 shadow-sm text-white" : "text-gray-600 hover:text-gray-600 bg-gray-50 hover:bg-gray-100"}`}
          >
            Platform Overview
          </button>
          <button
            onClick={() => setTab("agent")}
            className={`px-3 py-1.5 text-xs rounded-sm font-semibold transition-colors cursor-pointer ${tab === "agent" ? "brand-grad hover:opacity-70 shadow-sm text-white" : "text-gray-600 hover:text-gray-600 bg-gray-50 hover:bg-gray-100"}`}
          >
            Per-Agent (Agent Studio)
          </button>
          {isAdmin && (
            <button
              onClick={() => setTab("cloud")}
              className={`px-3 py-1.5 text-xs rounded-sm font-semibold transition-colors cursor-pointer ${tab === "cloud" ? "bg-gradient-to-br from-indigo-600 to-violet-600 hover:opacity-70 shadow-sm text-white" : "text-gray-600 hover:text-gray-600 bg-gray-50 hover:bg-gray-100"}`}
            >
              Cloud Usage
            </button>
          )}
          {isAdmin && (
            <button
              onClick={() => setTab("usage")}
              className={`px-3 py-1.5 text-xs rounded-sm font-semibold transition-colors cursor-pointer ${tab === "usage" ? "bg-gradient-to-br from-indigo-600 to-violet-600 hover:opacity-70 shadow-sm text-white" : "text-gray-600 hover:text-gray-600 bg-gray-50 hover:bg-gray-100"}`}
            >
              Platform Usage
            </button>
          )}
        </div>
      </div>

      {/* Body */}
      {tab === "platform" ? (
        <div className="flex-1 overflow-y-auto p-6">
          <PlatformDashboard />
        </div>
      ) : tab === "cloud" ? (
        <div className="flex-1 overflow-y-auto p-6">
          <CloudUsageDashboard />
        </div>
      ) : tab === "usage" ? (
        <div className="flex-1 overflow-y-auto p-6">
          <PlatformUsageDashboard />
        </div>
      ) : (
        <div className="flex-1 overflow-hidden flex">
          <AgentDrillDown />
        </div>
      )}
    </div>
  );
}
