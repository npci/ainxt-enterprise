// SPDX-License-Identifier: Apache-2.0
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { API_BASE, authFetch } from "../config";
import {
  Share2, Search, RefreshCw, Layers, FileText, Code2, X,
  Maximize2, Minimize2, Crosshair,
} from "lucide-react";

// ── Node-type → colour (lite theme) ─────────────────────────────────────────
const TYPE_COLOR = {
  class: "#2563eb", interface: "#2563eb", module: "#6366f1", function: "#0891b2",
  concept: "#059669", system: "#7c3aed", process: "#d97706", domain: "#db2777",
  document: "#475569", person: "#0d9488", policy: "#dc2626", cross: "#9333ea",
};
const colorFor = (t) => TYPE_COLOR[(t || "").toLowerCase()] || "#94a3b8";

const W = 1600, H = 1000;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

// ── Force layout — strong spread so big star graphs don't collapse ──────────
function computeLayout(nodes, edges) {
  const pos = {};
  const n = nodes.length || 1;
  // degree for hub-aware initial placement
  const deg = {};
  nodes.forEach((nd) => (deg[nd.id] = 0));
  edges.forEach((e) => { if (deg[e.src] != null) deg[e.src]++; if (deg[e.dst] != null) deg[e.dst]++; });
  const R = Math.min(W, H) / 2.3;
  nodes.forEach((nd, i) => {
    const a = (2 * Math.PI * i) / n;
    // hubs start nearer the centre, leaves on the rim
    const rr = R * (deg[nd.id] > 6 ? 0.25 : 1);
    pos[nd.id] = { x: W / 2 + Math.cos(a) * rr, y: H / 2 + Math.sin(a) * rr, vx: 0, vy: 0 };
  });
  const adj = edges
    .filter((e) => pos[e.src] && pos[e.dst] && e.src !== e.dst)
    .map((e) => [e.src, e.dst]);
  const REP = 48000, SPRING = 0.02, LEN = 190, CENTER = 0.004, DAMP = 0.9, MAXV = 60;
  const iters = Math.min(400, 220 + n * 4);
  for (let it = 0; it < iters; it++) {
    const cool = 1 - (it / iters) * 0.7;
    for (let i = 0; i < nodes.length; i++) {
      const A = pos[nodes[i].id];
      for (let j = i + 1; j < nodes.length; j++) {
        const B = pos[nodes[j].id];
        let dx = A.x - B.x, dy = A.y - B.y;
        let d2 = dx * dx + dy * dy || 0.01;
        const d = Math.sqrt(d2);
        const f = (REP / d2) * cool;
        const fx = (dx / d) * f, fy = (dy / d) * f;
        A.vx += fx; A.vy += fy; B.vx -= fx; B.vy -= fy;
      }
      A.vx += (W / 2 - A.x) * CENTER;
      A.vy += (H / 2 - A.y) * CENTER;
    }
    for (const [s, t] of adj) {
      const A = pos[s], B = pos[t];
      let dx = B.x - A.x, dy = B.y - A.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = SPRING * (d - LEN);
      const fx = (dx / d) * f, fy = (dy / d) * f;
      A.vx += fx; A.vy += fy; B.vx -= fx; B.vy -= fy;
    }
    for (const nd of nodes) {
      const P = pos[nd.id];
      P.vx = clamp(P.vx, -MAXV, MAXV); P.vy = clamp(P.vy, -MAXV, MAXV);
      P.x += P.vx * DAMP; P.y += P.vy * DAMP;
      P.vx *= DAMP; P.vy *= DAMP;
      P.x = clamp(P.x, 60, W - 60); P.y = clamp(P.y, 60, H - 60);
    }
  }
  const out = {};
  for (const nd of nodes) out[nd.id] = { x: pos[nd.id].x, y: pos[nd.id].y };
  return out;
}

export default function KnowledgeGraph() {
  const [graphs, setGraphs] = useState([]);
  const [graphId, setGraphId] = useState("");
  const [data, setData] = useState({ nodes: [], edges: [], total_nodes: 0, truncated: false });
  const [positions, setPositions] = useState({});
  const [depth, setDepth] = useState(2);
  const [seed, setSeed] = useState("");
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [hover, setHover] = useState(null);
  const [query, setQuery] = useState("");
  const [queryRes, setQueryRes] = useState(null);
  const [domains, setDomains] = useState([]);
  const [activeDomain, setActiveDomain] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [full, setFull] = useState(false);
  const [tf, setTf] = useState({ k: 1, x: 0, y: 0 });

  const svgRef = useRef(null);
  const drag = useRef(null);

  // ── data loads ──
  useEffect(() => {
    (async () => {
      try {
        const j = await authFetch(`${API_BASE}/graph/list`).then((r) => r.json());
        const gs = j.graphs || [];
        setGraphs(gs);
        if (gs.length) setGraphId((cur) => cur || gs[0].graph_id);
      } catch { setError("Failed to load graphs"); }
    })();
  }, []);

  useEffect(() => {
    if (!graphId) return;
    let dead = false;
    (async () => {
      setLoading(true); setError(""); setSelected(null); setDetail(null);
      setQueryRes(null); setActiveDomain(null); setTf({ k: 1, x: 0, y: 0 });
      try {
        const q = new URLSearchParams({ graph_id: graphId, depth: String(depth) });
        if (seed) q.set("seed", seed);
        const [ex, dm] = await Promise.all([
          authFetch(`${API_BASE}/graph/explore?${q}`).then((r) => r.json()),
          authFetch(`${API_BASE}/graph/domain?graph_id=${encodeURIComponent(graphId)}`).then((r) => r.json()).catch(() => ({ domains: [] })),
        ]);
        if (dead) return;
        const nodes = ex.nodes || [], edges = ex.edges || [];
        setData({ nodes, edges, total_nodes: ex.total_nodes || nodes.length, truncated: !!ex.truncated });
        setPositions(computeLayout(nodes, edges));
        setDomains(dm.domains || []);
      } catch { if (!dead) setError("Failed to load graph"); }
      finally { if (!dead) setLoading(false); }
    })();
    return () => { dead = true; };
  }, [graphId, depth, seed]);

  // degree + hubs
  const degree = useMemo(() => {
    const d = {}; data.nodes.forEach((n) => (d[n.id] = 0));
    data.edges.forEach((e) => { if (d[e.src] != null) d[e.src]++; if (d[e.dst] != null) d[e.dst]++; });
    return d;
  }, [data]);
  const hubs = useMemo(
    () => [...data.nodes].sort((a, b) => (degree[b.id] || 0) - (degree[a.id] || 0)).slice(0, 6),
    [data, degree]
  );
  const typeCounts = useMemo(() => {
    const c = {}; data.nodes.forEach((n) => (c[n.type] = (c[n.type] || 0) + 1));
    return Object.entries(c).sort((a, b) => b[1] - a[1]);
  }, [data]);

  const matchedNames = useMemo(() => {
    const s = new Set();
    (queryRes?.matched || []).forEach((m) => s.add(m.toLowerCase()));
    (queryRes?.sources || []).forEach((m) => s.add((m.name || "").toLowerCase()));
    return s;
  }, [queryRes]);
  const domainMembers = useMemo(() => new Set((activeDomain?.members || activeDomain?.member_node_ids || []).map(String)), [activeDomain]);

  const nodeById = useMemo(() => { const m = {}; data.nodes.forEach((n) => (m[n.id] = n)); return m; }, [data]);
  // Neighbours computed from the loaded edges — BOTH directions, so a leaf node
  // (e.g. a doc entity that is only "mentioned") still shows what it connects to.
  const selectedNeighbors = useMemo(() => {
    if (!selected) return [];
    const out = []; const seen = new Set();
    for (const e of data.edges) {
      let other, dir;
      if (e.src === selected.id) { other = e.dst; dir = "→"; }
      else if (e.dst === selected.id) { other = e.src; dir = "←"; }
      else continue;
      const key = `${dir}|${e.type}|${other}`;
      if (seen.has(key)) continue; seen.add(key);
      const o = nodeById[other];
      out.push({ id: other, name: o?.name || other, type: o?.type, edge: e.type, dir });
    }
    return out;
  }, [selected, data, nodeById]);

  const selectNode = useCallback(async (nd) => {
    setSelected(nd); setDetail(null);
    try {
      const r = await authFetch(`${API_BASE}/graph/node/${encodeURIComponent(nd.id)}?graph_id=${encodeURIComponent(graphId)}`);
      if (r.ok) setDetail(await r.json());
    } catch { /* explore data already has basics */ }
  }, [graphId]);

  const runQuery = async () => {
    if (!query.trim() || !graphId) return;
    setLoading(true);
    try {
      const r = await authFetch(`${API_BASE}/graph/query`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ graph_id: graphId, question: query, max_hops: depth }),
      });
      setQueryRes(await r.json());
    } catch { setError("Query failed"); } finally { setLoading(false); }
  };

  // ── pan / zoom / drag ──
  const toGraph = (evt) => {
    const r = svgRef.current.getBoundingClientRect();
    const vx = ((evt.clientX - r.left) / r.width) * W;
    const vy = ((evt.clientY - r.top) / r.height) * H;
    return { x: (vx - tf.x) / tf.k, y: (vy - tf.y) / tf.k, vx, vy };
  };
  const onWheel = (e) => {
    e.preventDefault();
    const p = toGraph(e);
    const k2 = clamp(tf.k * (e.deltaY < 0 ? 1.15 : 0.87), 0.25, 6);
    setTf({ k: k2, x: p.vx - p.x * k2, y: p.vy - p.y * k2 });
  };
  const onDownNode = (e, nd) => { e.stopPropagation(); drag.current = { type: "node", id: nd.id }; };
  const onDownBg = (e) => { drag.current = { type: "pan", sx: e.clientX, sy: e.clientY, ox: tf.x, oy: tf.y }; };
  const onMove = (e) => {
    if (!drag.current) return;
    if (drag.current.type === "node") {
      const p = toGraph(e);
      setPositions((pp) => ({ ...pp, [drag.current.id]: { x: p.x, y: p.y } }));
    } else {
      const r = svgRef.current.getBoundingClientRect();
      const dx = ((e.clientX - drag.current.sx) / r.width) * W;
      const dy = ((e.clientY - drag.current.sy) / r.height) * H;
      setTf((t) => ({ ...t, x: drag.current.ox + dx, y: drag.current.oy + dy }));
    }
  };
  const onUp = () => { drag.current = null; };
  const resetView = () => setTf({ k: 1, x: 0, y: 0 });

  useEffect(() => {
    const esc = (e) => e.key === "Escape" && setFull(false);
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, []);

  const kindIcon = (k) => (k === "repo" ? <Code2 size={13} /> : k === "kb" ? <FileText size={13} /> : <Share2 size={13} />);
  const sel = graphs.find((g) => g.graph_id === graphId);

  const showLabel = (nd) =>
    selected?.id === nd.id || hover === nd.id ||
    matchedNames.has((nd.name || "").toLowerCase()) ||
    (degree[nd.id] || 0) >= 5 || data.nodes.length <= 16 || tf.k >= 1.7;

  return (
    <div className={full ? "fixed inset-0 z-[100] bg-gray-50 flex flex-col" : "h-full flex flex-col bg-gray-50 text-gray-800 overflow-hidden"}>
      {/* Header */}
      <div className="px-5 py-3 border-b border-gray-200 bg-white flex items-center gap-3 flex-wrap">
        <div className="flex items-center gap-2 font-semibold text-gray-900">
          <Share2 size={18} className="text-violet-600" /> Knowledge Graph
        </div>
        <select value={graphId} onChange={(e) => { setGraphId(e.target.value); setSeed(""); }}
          className="text-sm border border-gray-300 rounded-md px-2 py-1.5 bg-white min-w-[200px]">
          {graphs.length === 0 && <option value="">No graphs built yet</option>}
          {graphs.map((g) => <option key={g.graph_id} value={g.graph_id}>{g.graph_id}  ({g.nodes})</option>)}
        </select>
        <div className="flex items-center gap-1 text-xs text-gray-500">depth
          <select value={depth} onChange={(e) => setDepth(Number(e.target.value))} className="border border-gray-300 rounded px-1 py-0.5 bg-white">
            {[1, 2, 3].map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>
        <div className="flex items-center gap-1 border border-gray-300 rounded-md px-2 py-1 bg-white">
          <Search size={13} className="text-gray-400" />
          <input value={seed} onChange={(e) => setSeed(e.target.value)} placeholder="seed node…" className="text-sm outline-none w-28 bg-transparent" />
        </div>
        <button onClick={() => setSeed((s) => (s ? s.trim() : " "))} title="reload" className="p-1.5 rounded-md hover:bg-gray-100 text-gray-500">
          <RefreshCw size={15} className={loading ? "animate-spin" : ""} />
        </button>
        <div className="ml-auto flex items-center gap-3 text-xs text-gray-500">
          {sel && <span className="inline-flex items-center gap-1">{kindIcon(sel.kind)} {sel.kind}</span>}
          <span>{data.nodes.length}{data.truncated ? "+" : ""} nodes · {data.edges.length} edges</span>
          {sel?.status && <span className="px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200">{sel.status}</span>}
          <button onClick={() => setFull((f) => !f)} title={full ? "Exit fullscreen (Esc)" : "Fullscreen"} className="p-1.5 rounded-md hover:bg-gray-100 text-gray-600">
            {full ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
          </button>
        </div>
      </div>

      {/* Query bar */}
      <div className="px-5 py-2 border-b border-gray-200 bg-white flex items-center gap-2">
        <input value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => e.key === "Enter" && runQuery()}
          placeholder='Ask the graph — e.g. "what does OrchestratorAgent call?"'
          className="flex-1 text-sm border border-gray-300 rounded-md px-3 py-1.5 outline-none focus:border-violet-400" />
        <button onClick={runQuery} className="text-sm px-3 py-1.5 rounded-md bg-violet-600 text-white hover:bg-violet-700">Query</button>
        {queryRes && <button onClick={() => setQueryRes(null)} className="p-1.5 text-gray-400 hover:text-gray-700"><X size={15} /></button>}
      </div>

      <div className="flex-1 flex overflow-hidden">
        {/* Canvas */}
        <div className="flex-1 relative overflow-hidden bg-white">
          {error && <div className="absolute top-3 left-3 z-10 text-sm text-red-600 bg-red-50 border border-red-200 px-3 py-1 rounded">{error}</div>}
          {data.nodes.length === 0 && !loading && (
            <div className="absolute inset-0 flex items-center justify-center text-gray-400 text-sm">No nodes — pick a graph or seed a node.</div>
          )}
          {/* zoom controls */}
          <div className="absolute bottom-4 left-4 z-10 flex flex-col gap-1">
            <button onClick={() => setTf((t) => ({ ...t, k: clamp(t.k * 1.25, 0.25, 6) }))} className="w-8 h-8 rounded-md bg-white border border-gray-300 text-gray-700 shadow-sm hover:bg-gray-50">＋</button>
            <button onClick={() => setTf((t) => ({ ...t, k: clamp(t.k * 0.8, 0.25, 6) }))} className="w-8 h-8 rounded-md bg-white border border-gray-300 text-gray-700 shadow-sm hover:bg-gray-50">－</button>
            <button onClick={resetView} title="reset view" className="w-8 h-8 rounded-md bg-white border border-gray-300 text-gray-600 shadow-sm hover:bg-gray-50 flex items-center justify-center"><Crosshair size={14} /></button>
          </div>
          <div className="absolute top-3 right-3 z-10 text-[11px] text-gray-400 select-none">drag node · scroll zoom · drag bg pan</div>

          <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet"
            className="w-full h-full" style={{ cursor: drag.current?.type === "pan" ? "grabbing" : "default" }}
            onWheel={onWheel} onMouseDown={onDownBg} onMouseMove={onMove} onMouseUp={onUp} onMouseLeave={onUp}>
            <g transform={`translate(${tf.x} ${tf.y}) scale(${tf.k})`}>
              {data.edges.map((e, i) => {
                const a = positions[e.src], b = positions[e.dst];
                if (!a || !b) return null;
                const hot = hover && (e.src === hover || e.dst === hover);
                return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={hot ? "#a78bfa" : "#d1d5db"} strokeWidth={hot ? 1.6 : 0.9} />;
              })}
              {data.nodes.map((nd) => {
                const p = positions[nd.id]; if (!p) return null;
                const isSel = selected?.id === nd.id;
                const isMatch = matchedNames.has((nd.name || "").toLowerCase());
                const inDomain = domainMembers.has(nd.id);
                const dimmed = activeDomain && !inDomain;
                const c = colorFor(nd.type);
                const r = clamp(5 + (degree[nd.id] || 0) * 0.7, 5, 13);
                const label = (nd.name || "").length > 26 ? nd.name.slice(0, 25) + "…" : nd.name;
                return (
                  <g key={nd.id} transform={`translate(${p.x},${p.y})`} opacity={dimmed ? 0.18 : 1}
                    style={{ cursor: "grab" }}
                    onMouseDown={(e) => onDownNode(e, nd)}
                    onClick={(e) => { e.stopPropagation(); selectNode(nd); }}
                    onMouseEnter={() => setHover(nd.id)} onMouseLeave={() => setHover(null)}>
                    {isMatch && <circle r={r + 7} fill="none" stroke="#f59e0b" strokeWidth="2.5" />}
                    {inDomain && <circle r={r + 5} fill="none" stroke="#db2777" strokeWidth="2" />}
                    <circle r={isSel ? r + 3 : r} fill={c} stroke={isSel ? "#111827" : "#fff"} strokeWidth={isSel ? 2.5 : 1.5} />
                    {showLabel(nd) && (
                      <text x={r + 4} y="4" fontSize={11 / Math.max(1, Math.sqrt(tf.k))} fill="#1f2937"
                        className="select-none pointer-events-none" style={{ paintOrder: "stroke", stroke: "#fff", strokeWidth: 3 }}>
                        {label}
                      </text>
                    )}
                  </g>
                );
              })}
            </g>
          </svg>
        </div>

        {/* Right panel */}
        <div className="w-80 border-l border-gray-200 bg-gray-50 overflow-auto flex-shrink-0">
          {selected ? (
            <div className="p-4 border-b border-gray-200 bg-white">
              <div className="flex items-start justify-between">
                <span className="px-2 py-0.5 rounded-full text-xs text-white" style={{ background: colorFor(selected.type) }}>{selected.type}</span>
                <button onClick={() => setSelected(null)} className="text-gray-400 hover:text-gray-700"><X size={14} /></button>
              </div>
              <div className="font-semibold text-gray-900 mt-2 break-words">{selected.name}</div>
              <div className="text-xs text-gray-400 mt-0.5">{selected.source_type} · {degree[selected.id] || 0} links</div>
              {(detail?.node?.summary || selected.summary) && <p className="text-sm text-gray-600 mt-2 leading-snug">{detail?.node?.summary || selected.summary}</p>}
              {detail?.node?.source_ref && <div className="text-[11px] text-gray-500 mt-2 break-all">↳ {detail.node.source_ref}</div>}
              {selectedNeighbors.length > 0 ? (
                <div className="mt-3">
                  <div className="text-[11px] font-semibold text-gray-400 mb-1">connects to ({selectedNeighbors.length})</div>
                  <div className="space-y-0.5 max-h-52 overflow-auto">
                    {selectedNeighbors.map((nb, i) => (
                      <button key={i} onClick={() => { const t = nodeById[nb.id]; if (t) selectNode(t); }}
                        title={nb.name}
                        className="flex items-center gap-1.5 w-full text-left text-xs text-gray-600 hover:text-violet-700">
                        <span className="text-gray-400 w-3 text-center flex-shrink-0">{nb.dir}</span>
                        <span className="text-amber-600 flex-shrink-0">{nb.edge}</span>
                        <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ background: colorFor(nb.type) }} />
                        <span className="truncate">{nb.name}</span>
                      </button>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="mt-3 text-[11px] text-gray-400">No connections at this depth — try increasing depth or pick another node.</div>
              )}
            </div>
          ) : (
            <div className="p-4 border-b border-gray-200">
              <div className="text-xs font-semibold text-gray-500 mb-2">Most-connected nodes</div>
              <div className="space-y-1">
                {hubs.map((nd) => (
                  <button key={nd.id} onClick={() => selectNode(nd)}
                    className="flex items-center gap-2 w-full text-left text-sm text-gray-700 hover:text-violet-700">
                    <span className="w-2.5 h-2.5 rounded-full flex-shrink-0" style={{ background: colorFor(nd.type) }} />
                    <span className="truncate">{nd.name}</span>
                    <span className="ml-auto text-xs text-gray-400">{degree[nd.id]}</span>
                  </button>
                ))}
                {hubs.length === 0 && <div className="text-xs text-gray-400">No nodes.</div>}
              </div>
            </div>
          )}

          {queryRes && (
            <div className="p-4 border-b border-gray-200 bg-amber-50/40">
              <div className="text-xs font-semibold text-amber-700 flex items-center gap-1 mb-2"><Maximize2 size={12} /> Query result</div>
              <div className="text-xs text-gray-600 mb-2">Matched: {(queryRes.matched || []).join(", ") || "—"}</div>
              <div className="space-y-1.5 max-h-52 overflow-auto">
                {(queryRes.sources || []).map((s, i) => (
                  <button key={i} onClick={() => { const t = data.nodes.find((x) => x.name === s.name); if (t) selectNode(t); }}
                    className="block w-full text-left text-xs bg-white border border-gray-200 rounded px-2 py-1 hover:border-violet-300">
                    <span className="font-medium text-gray-800">{s.name}</span><span className="text-gray-400"> · {s.type}</span>
                    {s.summary && <div className="text-gray-500 mt-0.5 leading-snug">{s.summary}</div>}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* type breakdown */}
          <div className="p-4 border-b border-gray-200">
            <div className="text-xs font-semibold text-gray-500 mb-2">Node types</div>
            <div className="flex flex-wrap gap-1.5">
              {typeCounts.map(([t, c]) => (
                <span key={t} className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border border-gray-200 bg-white">
                  <span className="w-2 h-2 rounded-full" style={{ background: colorFor(t) }} /> {t} <span className="text-gray-400">{c}</span>
                </span>
              ))}
            </div>
          </div>

          {/* domains */}
          <div className="p-4">
            <div className="text-xs font-semibold text-gray-500 flex items-center gap-1 mb-2"><Layers size={12} /> Domains ({domains.length})</div>
            <div className="space-y-1.5">
              {domains.map((d, i) => {
                const on = activeDomain === d;
                const mc = d.member_count ?? d.members?.length ?? d.member_node_ids?.length ?? 0;
                return (
                  <button key={i} onClick={() => setActiveDomain(on ? null : d)}
                    className={`block w-full text-left text-xs rounded-md px-2 py-1.5 border ${on ? "border-pink-400 bg-pink-50" : "border-gray-200 bg-white hover:border-pink-200"}`}>
                    <div className="font-medium text-gray-800">{d.name || d.domain_name}</div>
                    {d.description && <div className="text-gray-500 mt-0.5 leading-snug">{d.description}</div>}
                    {mc > 0 && <div className="text-[10px] text-pink-600 mt-0.5">{mc} members · click to highlight</div>}
                  </button>
                );
              })}
              {domains.length === 0 && <div className="text-xs text-gray-400">No domains clustered.</div>}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
