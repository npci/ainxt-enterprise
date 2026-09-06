// SPDX-License-Identifier: MIT
import { useState, useEffect } from "react";
import { ArrowLeft, Clock } from "lucide-react";

import { authFetch } from "../../config";

// ── Utilization drill-down views ────────────────────────────────────────────
// Zero-dependency pie/donut rendered with inline SVG (the private npm registry
// blocks recharts). Used by My Budget, Team, and Admin drill-downs to show a
// channel-wise or model-wise cost breakdown sourced from model_usages.

// Stable palette — cycled if there are more slices than colours.
const PALETTE = [
  "#6366f1", "#22c55e", "#f59e0b", "#ef4444", "#06b6d4",
  "#a855f7", "#ec4899", "#14b8a6", "#eab308", "#3b82f6",
  "#f97316", "#84cc16",
];

function colorFor(i) {
  return PALETTE[i % PALETTE.length];
}

function polarToCartesian(cx, cy, r, angleDeg) {
  const a = ((angleDeg - 90) * Math.PI) / 180.0;
  return { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) };
}

// Build an SVG arc path for a donut segment between two angles.
function arcPath(cx, cy, rOuter, rInner, startAngle, endAngle) {
  const largeArc = endAngle - startAngle > 180 ? 1 : 0;
  const p1 = polarToCartesian(cx, cy, rOuter, endAngle);
  const p2 = polarToCartesian(cx, cy, rOuter, startAngle);
  const p3 = polarToCartesian(cx, cy, rInner, startAngle);
  const p4 = polarToCartesian(cx, cy, rInner, endAngle);
  return [
    `M ${p1.x} ${p1.y}`,
    `A ${rOuter} ${rOuter} 0 ${largeArc} 0 ${p2.x} ${p2.y}`,
    `L ${p3.x} ${p3.y}`,
    `A ${rInner} ${rInner} 0 ${largeArc} 1 ${p4.x} ${p4.y}`,
    "Z",
  ].join(" ");
}

// ── Donut chart (inline SVG) ─────────────────────────────────────────────────
export function DonutChart({ data, size = 220, thickness = 46 }) {
  const [hover, setHover] = useState(null);
  const total = data.reduce((s, d) => s + (d.value || 0), 0);
  const cx = size / 2;
  const cy = size / 2;
  const rOuter = size / 2 - 2;
  const rInner = rOuter - thickness;

  if (!data.length || total <= 0) {
    return (
      <div
        className="flex items-center justify-center text-xs text-gray-400 italic border border-dashed border-gray-200 rounded-full"
        style={{ width: size, height: size }}
      >
        No usage recorded
      </div>
    );
  }

  const segments = data.reduce((acc, d, i) => {
    const frac = (d.value || 0) / total;
    const prev = acc.length ? acc[acc.length - 1].cursorEnd : 0;
    const start = prev * 360;
    const cursorEnd = prev + frac;
    const end = cursorEnd * 360;
    // Guard: full circle can't be drawn as a single arc; nudge slightly.
    const drawEnd = end - start >= 360 ? start + 359.999 : end;
    acc.push({
      key: d.key,
      color: colorFor(i),
      path: arcPath(cx, cy, rOuter, rInner, start, drawEnd),
      pct: frac * 100,
      value: d.value,
      index: i,
      cursorEnd,
    });
    return acc;
  }, []);

  const active = hover != null ? segments.find(s => s.index === hover) : null;

  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img">
      {segments.map(seg => (
        <path
          key={seg.key}
          d={seg.path}
          fill={seg.color}
          stroke="#fff"
          strokeWidth={1}
          opacity={hover == null || hover === seg.index ? 1 : 0.35}
          onMouseEnter={() => setHover(seg.index)}
          onMouseLeave={() => setHover(null)}
          style={{ transition: "opacity 0.12s" }}
        />
      ))}
      {/* Center label */}
      <text x={cx} y={cy - 6} textAnchor="middle" className="fill-gray-800" style={{ fontSize: 15, fontWeight: 600 }}>
        {active ? `${active.pct.toFixed(1)}%` : `$${total.toFixed(2)}`}
      </text>
      <text x={cx} y={cy + 12} textAnchor="middle" className="fill-gray-400" style={{ fontSize: 10 }}>
        {active ? active.key : "total"}
      </text>
    </svg>
  );
}

// ── Legend + breakdown table ─────────────────────────────────────────────────
function BreakdownLegend({ rows, total }) {
  return (
    <div className="flex-1 min-w-[16rem]">
      <table className="w-full text-sm">
        <thead className="bg-gray-50">
          <tr>
            <th className="px-3 py-2 text-left text-xs text-gray-500">Segment</th>
            <th className="px-3 py-2 text-right text-xs text-gray-500">Cost (USD)</th>
            <th className="px-3 py-2 text-right text-xs text-gray-500">Share</th>
            <th className="px-3 py-2 text-right text-xs text-gray-500">Requests</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.key} className="border-t border-gray-100">
              <td className="px-3 py-2 text-gray-700">
                <span className="inline-flex items-center gap-2">
                  <span className="inline-block w-3 h-3 rounded-sm" style={{ background: colorFor(i) }} />
                  {r.key}
                </span>
              </td>
              <td className="px-3 py-2 text-right text-gray-600">${(r.cost_usd || 0).toFixed(4)}</td>
              <td className="px-3 py-2 text-right text-gray-500">
                {total > 0 ? (((r.cost_usd || 0) / total) * 100).toFixed(1) : "0.0"}%
              </td>
              <td className="px-3 py-2 text-right text-gray-500">{r.requests ?? 0}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Pie panel: fetches a breakdown for a dimension and renders donut+legend ──
// `endpoint` is a function (dimension) => url so the same panel works for
// user / me / team utilization endpoints.
export function UtilizationPie({ endpoint, headers, dimension }) {
  const [breakdown, setBreakdown] = useState([]);
  const [total, setTotal]         = useState(0);
  const [loading, setLoading]     = useState(true);
  const [error, setError]         = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true); setError("");
      try {
        const r = await authFetch(endpoint(dimension), headers ? { headers } : undefined);
        if (!r.ok) {
          const d = await r.json().catch(() => ({}));
          if (!cancelled) { setError(d?.detail || `HTTP ${r.status}`); setBreakdown([]); }
        } else {
          const d = await r.json();
          if (!cancelled) {
            setBreakdown(Array.isArray(d.breakdown) ? d.breakdown : []);
            setTotal(Number(d.total_cost_usd || 0));
          }
        }
      } catch (e) {
        if (!cancelled) setError(e?.message || String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [endpoint, dimension]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-xs text-gray-400 py-8">
        <Clock size={13} className="animate-pulse" /> Loading utilization…
      </div>
    );
  }
  if (error) {
    return <div className="text-xs text-red-600 py-4">Failed to load utilization: {error}</div>;
  }

  const pieData = breakdown.map(b => ({ key: b.key, value: b.cost_usd || 0 }));

  return (
    <div className="flex flex-wrap items-center gap-8">
      <div className="flex-shrink-0">
        <DonutChart data={pieData} />
      </div>
      {breakdown.length === 0 ? (
        <div className="text-xs text-gray-400 italic py-4">No usage recorded this period.</div>
      ) : (
        <div className="flex-1 min-w-[16rem] bg-white border border-gray-200 rounded-lg overflow-hidden">
          <BreakdownLegend rows={breakdown} total={total} />
        </div>
      )}
    </div>
  );
}

// ── Month-to-Date history table (Date wise view) ─────────────────────────────
export function MtdHistoryTable({ history }) {
  const rows = history || [];
  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
      <table className="w-full text-sm">
        <thead className="bg-gray-50">
          <tr>
            <th className="px-4 py-2 text-left text-xs text-gray-500">Date</th>
            <th className="px-4 py-2 text-right text-xs text-gray-500">Cost (USD)</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={2} className="px-4 py-4 text-center text-xs text-gray-400 italic">
                No usage recorded this period.
              </td>
            </tr>
          ) : rows.map(row => (
            <tr key={row.date} className="border-t border-gray-100">
              <td className="px-4 py-2 text-gray-700">{row.date}</td>
              <td className="px-4 py-2 text-right text-gray-600">${(row.cost_usd_spent || 0).toFixed(4)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Full-page Utilization view with optional back button + dimension dropdown ─
// options: array of { value, label } describing the dropdown entries. The
// caller decides whether "date" (MTD history) is included and the default.
// - endpoint(dimension): url builder for channel/model pie
// - headers: optional request headers (e.g. X-User-Id for the /me endpoint)
// - history: MTD history rows (only needed when "date" option is present)
// - showBack: set to false to hide the Back button (e.g. inline detail panels)
export function UtilizationPage({
  onBack,
  endpoint,
  headers,
  options,
  defaultView,
  history,
  showBack = true,
}) {
  const opts = options && options.length
    ? options
    : [{ value: "channel", label: "Channel wise usage" }, { value: "model", label: "Model wise usage" }];
  const [view, setView] = useState(defaultView || opts[0].value);

  return (
    <div className="space-y-4">
      {/* Header row: dimension dropdown on the left, Back on the far right (optional) */}
      <div className="flex items-center justify-between gap-3">
        <select
          value={view}
          onChange={e => setView(e.target.value)}
          className="px-3 py-1.5 text-sm text-white rounded-md brand-grad hover:opacity-70 outline-none cursor-pointer min-w-[12rem] [&>option]:text-gray-700 [&>option]:bg-white"
        >
          {opts.map(o => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        {showBack && (
          <button
            onClick={onBack}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-white rounded-md brand-grad hover:opacity-70 cursor-pointer"
          >
            <ArrowLeft size={14} /> Back
          </button>
        )}
      </div>

      {/* Body */}
      <div className="pt-2">
        {view === "date" ? (
          <div>
            <h3 className="text-xs font-medium text-gray-500 mb-2 uppercase tracking-wide">
              Month-to-Date History
            </h3>
            <MtdHistoryTable history={history} />
          </div>
        ) : (
          <UtilizationPie endpoint={endpoint} headers={headers} dimension={view} />
        )}
      </div>
    </div>
  );
}
