// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect, useCallback } from "react";
import {
  Activity, CheckCircle, XCircle, AlertCircle, RefreshCw,
  Cpu, Zap, Clock, BarChart2, GitBranch, Layers,
  Shield, AlertTriangle, TrendingUp, Database, Users,
} from "lucide-react";
import { API_BASE as API, authFetch } from "../config";
import { toIST } from "../utils/time";

// ── Helpers ─────────────────────────────────────────────────────

function fmtDuration(ms) {
  if (!ms) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${s % 60}s`;
}

// Relative time for recent events; IST absolute for anything older than 1 hour
function ago(iso) {
  if (!iso) return "—";
  let d;
  if (iso instanceof Date)       d = iso;
  else if (typeof iso === "number") d = new Date(iso < 1e10 ? iso * 1000 : iso); // sec vs ms
  else                           d = new Date(iso);
  const ts = d.getTime();
  if (isNaN(ts))                 return "—";
  const diff = Date.now() - ts;
  const s    = Math.round(diff / 1000);
  if (s < 60)   return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return toIST(d);
}

function fmt(n, decimals = 0) {
  if (n == null) return "—";
  return typeof n === "number" ? n.toLocaleString(undefined, { maximumFractionDigits: decimals }) : n;
}

// ── Stat Card ────────────────────────────────────────────────────

function StatCard({ icon: Icon, label, value, sub, color = "gray", pulse = false }) {
  const bg = {
    green:  "bg-green-50  border-green-200",
    red:    "bg-red-50    border-red-200",
    blue:   "bg-blue-50   border-blue-200",
    yellow: "bg-yellow-50 border-yellow-200",
    purple: "bg-purple-50 border-purple-200",
    orange: "bg-orange-50 border-orange-200",
    gray:   "bg-gray-50   border-gray-200",
  }[color];
  const ic = {
    green:  "text-green-500",  red:    "text-red-500",
    blue:   "text-blue-500",   yellow: "text-yellow-500",
    purple: "text-purple-500", orange: "text-orange-500",
    gray:   "text-gray-400",
  }[color];
  return (
    <div className={`rounded-xl border p-4 ${bg}`}>
      <div className="flex items-center gap-2 mb-2">
        <Icon size={14} className={ic} />
        <span className="text-xs text-gray-500 font-medium">{label}</span>
        {pulse && <span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse ml-auto" />}
      </div>
      <div className="text-2xl font-bold text-gray-800 tabular-nums">{value ?? "—"}</div>
      {sub && <div className="text-xs text-gray-400 mt-0.5">{sub}</div>}
    </div>
  );
}

// ── Circuit Breaker Card ─────────────────────────────────────────

function BreakerCard({ breaker }) {
  const { name, state, failures, failure_threshold, recovery_timeout, opened_at } = breaker;
  const isClosed   = state === "CLOSED";
  const isHalfOpen = state === "HALF-OPEN";

  const cls = isClosed
    ? "bg-green-50 border-green-200 text-green-700"
    : isHalfOpen ? "bg-yellow-50 border-yellow-200 text-yellow-700"
    : "bg-red-50 border-red-200 text-red-700";
  const dot = isClosed ? "bg-green-500" : isHalfOpen ? "bg-yellow-400" : "bg-red-500";

  return (
    <div className={`rounded-xl border p-4 ${cls}`}>
      <div className="flex items-center justify-between mb-3">
        <span className="font-semibold text-sm capitalize">{name}</span>
        <span className={`flex items-center gap-1 text-xs font-bold px-2 py-0.5 rounded-full border ${cls}`}>
          <span className={`w-1.5 h-1.5 rounded-full ${dot} ${!isClosed ? "animate-pulse" : ""}`} />
          {state}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs text-gray-600">
        <div>
          <span className="text-gray-400">Failures</span>
          <div className="font-semibold text-gray-800">{failures}/{failure_threshold}</div>
        </div>
        <div>
          <span className="text-gray-400">Timeout</span>
          <div className="font-semibold text-gray-800">{recovery_timeout}s</div>
        </div>
        {opened_at && (
          <div className="col-span-2">
            <span className="text-gray-400">Opened</span>
            <div className="font-semibold text-red-600">{ago(opened_at)}</div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Queue Health Bar ─────────────────────────────────────────────

function QueueRow({ name, stats }) {
  const total    = (stats.queued || 0) + (stats.started || 0) + (stats.finished || 0) + (stats.failed || 0);
  const failRate = total > 0 ? ((stats.failed || 0) / total * 100).toFixed(1) : "0";
  const health   = (stats.failed || 0) > 0 ? "red" : (stats.started || 0) > 20 ? "yellow" : "green";
  const dot      = { green: "bg-green-400", yellow: "bg-yellow-400", red: "bg-red-500" }[health];

  return (
    <div className="flex items-center gap-4 py-3 border-b border-gray-100 last:border-0">
      <div className="flex items-center gap-2 w-36">
        <span className={`w-2 h-2 rounded-full ${dot}`} />
        <span className="text-sm font-medium text-gray-700 truncate">{name.replace("_queue", "")}</span>
      </div>
      <div className="flex gap-4 text-xs flex-1">
        <span className="text-yellow-600 font-medium w-16">
          <span className="text-gray-400 font-normal">queued </span>{stats.queued || 0}
        </span>
        <span className="text-blue-600 font-medium w-16">
          <span className="text-gray-400 font-normal">running </span>{stats.started || 0}
        </span>
        <span className="text-green-600 font-medium w-20">
          <span className="text-gray-400 font-normal">done </span>{(stats.finished || 0).toLocaleString()}
        </span>
        <span className="text-red-500 font-medium">
          <span className="text-gray-400 font-normal">failed </span>{stats.failed || 0}
          {total > 0 && <span className="text-gray-400 font-normal ml-1">({failRate}%)</span>}
        </span>
      </div>
    </div>
  );
}

// ── Job Row ──────────────────────────────────────────────────────

function JobRow({ job }) {
  const cls = {
    finished: "text-green-600 bg-green-50",
    failed:   "text-red-600 bg-red-50",
    started:  "text-blue-600 bg-blue-50",
    queued:   "text-yellow-600 bg-yellow-50",
  }[job.status] || "text-gray-500 bg-gray-50";

  const dur = job.ended_at && job.started_at
    ? Date.parse(job.ended_at) - Date.parse(job.started_at) : null;
  const name = (job.fn || job.id || "").split(".").pop();

  return (
    <tr className="border-b border-gray-100 hover:bg-gray-50 transition">
      <td className="py-2 px-3">
        <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${cls}`}>{job.status}</span>
      </td>
      <td className="py-2 px-3 text-xs text-gray-700 font-mono max-w-xs truncate">{name}</td>
      <td className="py-2 px-3 text-xs text-gray-400">{ago(job.enqueued_at)}</td>
      <td className="py-2 px-3 text-xs text-gray-500">{dur ? fmtDuration(dur) : "—"}</td>
      {job.error && (
        <td className="py-2 px-3 text-xs text-red-500 max-w-xs truncate">{job.error}</td>
      )}
    </tr>
  );
}

// ── Mini Sparkline ───────────────────────────────────────────────

function MiniBar({ value, max, color = "bg-blue-400" }) {
  const pct = max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0;
  return (
    <div className="flex-1 h-1.5 bg-gray-100 rounded-full overflow-hidden">
      <div className={`h-full rounded-full ${color}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

// ── Main ─────────────────────────────────────────────────────────

export default function Monitoring({ user }) {
  // canAccess: driven by can_approve from backend (uses APPROVAL_AD_LEVEL config)
  const canAccess = user && (
    user.role === "admin" || user.role === "operator" ||
    user.can_approve === true
  );
  if (!canAccess) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-gray-400">
        <Shield size={40} className="mb-3 text-gray-300" />
        <p className="text-sm font-medium">Access restricted</p>
        <p className="text-xs mt-1 text-center max-w-xs">
          Monitoring is available to Director-level (AD level ≤ 3) and above, or operator/admin roles.
        </p>
      </div>
    );
  }

  const [health,     setHealth]     = useState(null);
  const [breakers,   setBreakers]   = useState([]);
  const [sdlcStats,       setSdlcStats]       = useState(null);
  const [jobs,            setJobs]            = useState([]);
  const [telemetry,       setTelemetry]       = useState(null);
  const [queues,          setQueues]          = useState({});
  const [compressStats,   setCompressStats]   = useState(null); // Phase 2 telemetry
  const [loading,         setLoading]         = useState(true);
  const [lastRefresh,     setLastRefresh]      = useState(null);
  const [error,           setError]           = useState(null);

  const fetchAll = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [h, cb, stats, j, met, q, cmp] = await Promise.allSettled([
        authFetch(`${API}/health`).then(r => r.json()),
        authFetch(`${API}/health/circuit-breakers`).then(r => r.json()),
        authFetch(`${API}/sdlc/stats`).then(r => r.json()),
        authFetch(`${API}/jobs?limit=20`).then(r => r.json()),
        authFetch(`${API}/metrics`).then(r => r.json()),
        authFetch(`${API}/jobs/stats/queues`).then(r => r.json()),
        authFetch(`${API}/metrics/compression?days=7`).then(r => r.json()),
      ]);
      if (h.status    === "fulfilled") setHealth(h.value);
      if (cb.status   === "fulfilled") setBreakers(cb.value.breakers || []);
      if (stats.status=== "fulfilled") setSdlcStats(stats.value);
      if (j.status    === "fulfilled") setJobs(j.value.jobs || j.value || []);
      if (met.status  === "fulfilled") setTelemetry(met.value?.telemetry || met.value || null);
      if (q.status    === "fulfilled") setQueues(q.value || {});
      if (cmp.status  === "fulfilled") setCompressStats(cmp.value || null);
      setLastRefresh(new Date());
    } catch (e) { setError(e.message); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    fetchAll();
    const t = setInterval(fetchAll, 30000);
    return () => clearInterval(t);
  }, [fetchAll]);

  // ── Derived values ──────────────────────────────────────────
  const tel          = telemetry || {};
  const reqTotal     = tel.requests_total     || 0;
  const errTotal     = tel.errors_total       || 0;
  const errorRate    = reqTotal > 0 ? ((errTotal / reqTotal) * 100).toFixed(1) : "0";
  const p95          = tel.p95_latency_ms     || 0;
  const avgLat       = tel.avg_latency_ms     || 0;
  const cacheHits    = tel.cache_hits         || 0;
  const cacheRate    = reqTotal > 0 ? ((cacheHits / reqTotal) * 100).toFixed(0) : "0";
  const compBlocks   = tel.compliance_blocks  || 0;
  const agentRuns    = tel.agent_executions   || 0;
  const agentSucc    = tel.agent_success      || 0;
  const successRate  = agentRuns > 0 ? Math.round((agentSucc / agentRuns) * 100) : null;

  const isHealthy    = health?.status === "healthy";
  const allClosed    = breakers.length > 0 && breakers.every(b => b.state === "CLOSED");

  const stateColors  = {
    COMPLETE: "bg-green-100 text-green-700", FAILED: "bg-red-100 text-red-700",
    AWAITING_PR_APPROVAL: "bg-blue-100 text-blue-700",
    AWAITING_DESIGN_APPROVAL: "bg-blue-100 text-blue-700",
    AWAITING_SOLUTION_APPROVAL: "bg-blue-100 text-blue-700",
    AWAITING_USER_INPUT: "bg-yellow-100 text-yellow-700",
    TICKET_NORMALIZATION: "bg-sky-100 text-sky-700",
    DIAGNOSING: "bg-blue-100 text-blue-700",
    MANIFEST_VALIDATION: "bg-violet-100 text-violet-700",
    PRE_CODING_BUILD: "bg-amber-100 text-amber-700",
    SLT_RUNNING: "bg-cyan-100 text-cyan-700",
    CREATED: "bg-gray-100 text-gray-600",
  };
  const typeColors   = {
    feature: "bg-purple-100 text-purple-700",
    bug:     "bg-red-100 text-red-700",
    pr_review: "bg-blue-100 text-blue-700",
  };

  // Health checks from /health endpoint
  const checks = health?.checks || {};
  const checkList = Object.entries(checks);

  return (
    <div className="flex flex-col h-screen bg-gray-50 overflow-hidden">

      {/* ── Header ── */}
      <div className="bg-white border-b border-gray-200 px-6 py-4 flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-3">
          <Activity size={18} className="text-indigo-700" />
          <div>
            <h1 className="text-sm font-semibold  text-indigo-700">Platform Monitoring</h1>
            <p className="text-xs text-gray-400">
              {lastRefresh ? `Updated ${toIST(lastRefresh)} IST` : "Loading…"} · auto-refreshes every 30s
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <span className={`flex items-center gap-1.5 text-xs font-semibold px-3 py-1 rounded-full border ${
            isHealthy
              ? "bg-green-50 text-green-700 border-green-200"
              : "bg-red-50 text-red-700 border-red-200"
          }`}>
            {isHealthy ? <CheckCircle size={12} /> : <XCircle size={12} />}
            {isHealthy ? "All Systems Healthy" : health?.status === "degraded" ? "Degraded" : "Unhealthy"}
          </span>
          <button
            onClick={fetchAll} disabled={loading}
            className="flex items-center gap-1.5 text-xs hover:bg-gray-100  rounded px-3 py-1.5 transition cursor-pointer text-gray-600"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} /> Refresh
          </button>
        </div>
      </div>

      {error && (
        <div className="mx-6 mt-4 px-4 py-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-600">
          {error}
        </div>
      )}

      <div className="flex-1 overflow-y-auto p-6 space-y-6">

        {/* ── Key Metrics Row ── */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <StatCard icon={Activity}     label="Total Requests"     value={reqTotal.toLocaleString()}         sub="all-time"                        color="blue"   pulse />
          <StatCard icon={AlertTriangle} label="Error Rate"        value={`${errorRate}%`}                   sub={`${errTotal.toLocaleString()} total errors`}   color={parseFloat(errorRate) > 5 ? "red" : "green"} />
          <StatCard icon={Zap}           label="p95 Latency"       value={p95 ? `${Math.round(p95)}ms` : "—"} sub={`avg ${Math.round(avgLat)}ms`}  color={p95 > 8000 ? "yellow" : "green"} />
          <StatCard icon={Shield}        label="Compliance Blocks" value={compBlocks.toLocaleString()}        sub="PCI/PII violations blocked"      color={compBlocks > 0 ? "yellow" : "green"} />
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <StatCard icon={Cpu}       label="Agent Executions" value={agentRuns.toLocaleString()}   sub={successRate != null ? `${successRate}% success rate` : ""} color="purple" />
          <StatCard icon={Database}  label="Cache Hit Rate"   value={`${cacheRate}%`}              sub={`${cacheHits.toLocaleString()} cache hits`}               color="blue"   />
          <StatCard icon={Layers}    label="SDLC Runs"        value={sdlcStats?.total ?? "—"}      sub={`${sdlcStats?.by_state?.COMPLETE || 0} complete`}          color="blue"   />
          <StatCard icon={TrendingUp} label="Circuit Breakers" value={allClosed ? "All Closed" : `${breakers.filter(b => b.state !== "CLOSED").length} Open`}
            sub={`${breakers.length} monitored`} color={allClosed ? "green" : "red"} />
        </div>

        {/* ── Service Health Checks ── */}
        {checkList.length > 0 && (
          <section>
            <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-3 flex items-center gap-2">
              <Activity size={12} /> Service Health
            </h2>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {checkList.map(([svc, info]) => {
                const ok = info?.status === "ok" || info === "ok";
                return (
                  <div key={svc} className={`rounded-xl border p-3 flex items-center gap-3 ${ok ? "bg-green-50 border-green-200" : "bg-red-50 border-red-200"}`}>
                    <span className={`w-2 h-2 rounded-full flex-shrink-0 ${ok ? "bg-green-500" : "bg-red-500 animate-pulse"}`} />
                    <div className="min-w-0">
                      <div className="text-xs font-semibold text-gray-700 capitalize">{svc}</div>
                      {typeof info === "object" && info.latency_ms && (
                        <div className="text-[10px] text-gray-400">{info.latency_ms}ms</div>
                      )}
                    </div>
                    <span className={`ml-auto text-xs font-bold ${ok ? "text-green-600" : "text-red-600"}`}>
                      {ok ? "OK" : "DOWN"}
                    </span>
                  </div>
                );
              })}
            </div>
          </section>
        )}

        {/* ── Circuit Breakers ── */}
        <section>
          <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-3 flex items-center gap-2">
            <Shield size={12} /> Circuit Breakers
          </h2>
          {breakers.length === 0 ? (
            <div className="bg-white border border-gray-200 rounded-xl p-6 text-center text-sm text-gray-400">
              No circuit breakers registered yet
            </div>
          ) : (
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
              {breakers.map(b => <BreakerCard key={b.name} breaker={b} />)}
            </div>
          )}
        </section>

        {/* ── Queue Health ── */}
        {Object.keys(queues).length > 0 && (
          <section>
            <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-3 flex items-center gap-2">
              <Clock size={12} /> Queue Health
            </h2>
            <div className="bg-white border border-gray-200 rounded-xl overflow-hidden">
              <div className="px-4 py-2 bg-gray-50 border-b border-gray-100 grid grid-cols-4 text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
                <span>Queue</span>
                <span className="col-span-3 pl-2">Status</span>
              </div>
              <div className="px-4">
                {Object.entries(queues).map(([name, stats]) => (
                  <QueueRow key={name} name={name} stats={stats} />
                ))}
              </div>
            </div>
          </section>
        )}

        {/* ── SDLC Stats ── */}
        {sdlcStats && (
          <section className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="bg-white border border-gray-200 rounded-xl p-4">
              <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-3 flex items-center gap-2">
                <GitBranch size={12} /> SDLC Runs by State
              </h2>
              <div className="space-y-2">
                {Object.entries(sdlcStats.by_state || {}).map(([state, count]) => {
                  const pct  = Math.round((count / (sdlcStats.total || 1)) * 100);
                  const cls  = stateColors[state] || "bg-gray-100 text-gray-600";
                  const barC = state === "COMPLETE" ? "bg-green-400" : state === "FAILED" ? "bg-red-400" :
                    state.includes("AWAITING") ? "bg-blue-400" : "bg-gray-300";
                  return (
                    <div key={state} className="flex items-center gap-3">
                      <span className={`text-xs font-medium px-2 py-0.5 rounded-full w-52 truncate ${cls}`}>
                        {state.replace(/_/g, " ")}
                      </span>
                      <MiniBar value={count} max={sdlcStats.total || 1} color={barC} />
                      <span className="text-xs text-gray-500 w-6 text-right tabular-nums">{count}</span>
                    </div>
                  );
                })}
              </div>
            </div>

            <div className="bg-white border border-gray-200 rounded-xl p-4">
              <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-3 flex items-center gap-2">
                <BarChart2 size={12} /> SDLC Runs by Type
              </h2>
              <div className="space-y-2">
                {Object.entries(sdlcStats.by_type || {}).map(([type, count]) => {
                  const cls  = typeColors[type] || "bg-gray-100 text-gray-600";
                  const col  = type === "feature" ? "bg-purple-400" : type === "bug" ? "bg-red-400" : "bg-blue-400";
                  return (
                    <div key={type} className="flex items-center gap-3">
                      <span className={`text-xs font-medium px-2 py-0.5 rounded-full w-28 truncate ${cls}`}>
                        {type.replace(/_/g, " ")}
                      </span>
                      <MiniBar value={count} max={sdlcStats.total || 1} color={col} />
                      <span className="text-xs text-gray-500 w-6 text-right tabular-nums">{count}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          </section>
        )}

        {/* ── Model Usage ── */}
        {Object.keys(tel.model_calls || {}).length > 0 && (
          <section>
            <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-3 flex items-center gap-2">
              <Cpu size={12} /> Model Usage
            </h2>
            <div className="bg-white border border-gray-200 rounded-xl p-4">
              <div className="space-y-3">
                {Object.entries(tel.model_calls)
                  .sort((a, b) => b[1] - a[1])
                  .map(([model, calls], i) => {
                    const totalCalls = Object.values(tel.model_calls).reduce((s, v) => s + v, 0);
                    const cost  = (tel.model_cost_usd || {})[model] || 0;
                    const tokens= (tel.model_tokens   || {})[model] || 0;
                    const COLS  = ["bg-blue-500","bg-green-500","bg-purple-500","bg-orange-500","bg-red-400","bg-teal-500"];
                    return (
                      <div key={model} className="flex items-center gap-3">
                        <div className={`w-2 h-2 rounded-full flex-shrink-0 ${COLS[i % COLS.length]}`} />
                        <span className="text-sm text-gray-700 w-48 truncate">{model}</span>
                        <MiniBar value={calls} max={totalCalls} color={COLS[i % COLS.length]} />
                        <span className="text-xs text-gray-600 w-14 text-right tabular-nums">{calls.toLocaleString()} calls</span>
                        <span className="text-xs text-gray-400 w-24 text-right tabular-nums">{(tokens/1000).toFixed(0)}K tokens</span>
                        <span className="text-xs text-gray-400 w-16 text-right tabular-nums">${cost.toFixed(4)}</span>
                      </div>
                    );
                  })}
              </div>
            </div>
          </section>
        )}

        {/* ── Context Compression Telemetry (Phase 2) ── */}
        {compressStats && compressStats.totals && Object.keys(compressStats.totals).length > 0 && (
          <section>
            <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-3 flex items-center gap-2">
              <Layers size={12} /> Context Compression — 7-Day Stats
            </h2>
            <div className="bg-white border border-gray-200 rounded-xl p-4">
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 mb-4">
                {Object.entries(compressStats.totals).map(([src, v]) => {
                  const label = {
                    ide_session: "IDE Session",
                    ide_tool:    "IDE File Read",
                    sdlc_build:  "SDLC Build Log",
                    sdlc_test:   "SDLC Test",
                    rag_phase1:  "RAG Dedup",
                    lingua_rag:  "LLMLingua-2",
                  }[src] || src;
                  const pct = v.reduction_pct || 0;
                  const color = pct >= 80 ? "text-green-600" : pct >= 40 ? "text-yellow-600" : "text-gray-600";
                  return (
                    <div key={src} className="bg-gray-50 rounded-lg p-3 text-center">
                      <div className={`text-2xl font-bold tabular-nums ${color}`}>{pct.toFixed(0)}%</div>
                      <div className="text-[10px] text-gray-500 mt-1 font-medium uppercase tracking-wide">{label}</div>
                      <div className="text-[10px] text-gray-400 mt-1">{(v.calls || 0).toLocaleString()} calls</div>
                      <div className="text-[10px] text-gray-400">
                        {((v.before || 0) / 1000).toFixed(0)}K → {((v.after || 0) / 1000).toFixed(0)}K chars
                      </div>
                    </div>
                  );
                })}
              </div>
              <div className="text-[10px] text-gray-400 mt-2">
                Total chars compressed (7d): {Object.values(compressStats.totals).reduce((s, v) => s + (v.before || 0), 0).toLocaleString()}
                {" → "}
                {Object.values(compressStats.totals).reduce((s, v) => s + (v.after || 0), 0).toLocaleString()}
              </div>
            </div>
          </section>
        )}

        {/* ── Recent Jobs ── */}
        <section>
          <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-3 flex items-center gap-2">
            <Clock size={12} /> Recent Jobs
          </h2>
          <div className="bg-white border border-gray-200 rounded-xl overflow-hidden">
            {jobs.length === 0 ? (
              <div className="p-6 text-center text-sm text-gray-400">No jobs found</div>
            ) : (
              <table className="w-full">
                <thead>
                  <tr className="bg-gray-50 border-b border-gray-200">
                    <th className="py-2 px-3 text-left text-xs font-medium text-gray-500">Status</th>
                    <th className="py-2 px-3 text-left text-xs font-medium text-gray-500">Job</th>
                    <th className="py-2 px-3 text-left text-xs font-medium text-gray-500">Queued</th>
                    <th className="py-2 px-3 text-left text-xs font-medium text-gray-500">Duration</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.slice(0, 20).map(job => <JobRow key={job.id} job={job} />)}
                </tbody>
              </table>
            )}
          </div>
        </section>

      </div>
    </div>
  );
}
