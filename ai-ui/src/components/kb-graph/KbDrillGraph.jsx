// SPDX-License-Identifier: MIT
// KbDrillGraph — drill-down graph scope picker for KB chat.
//
// Levels: Domain → Product → Spec Version → Document
// Derives all levels from actual KB doc data (not hardcoded lists).
// Docs with empty fields are grouped under "(Unclassified)" / "(No product)" etc.

import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import {
  Globe2, Package, GitBranch, FileText,
  ChevronRight, Loader2, AlertTriangle, CheckCircle2, Search,
} from "lucide-react";
import { API_BASE, authFetch } from "../../config";
import { highlightMatch } from "../../utils/kbFormat.js";

const NO_VALUE = "(Unclassified)";

const STYLE = {
  domain:   { icon: Globe2,    border: "#818cf8", cls: "bg-indigo-50 border-indigo-300 text-indigo-900" },
  product:  { icon: Package,   border: "#38bdf8", cls: "bg-sky-50 border-sky-300 text-sky-900" },
  version:  { icon: GitBranch, border: "#a78bfa", cls: "bg-violet-50 border-violet-300 text-violet-900" },
  document: { icon: FileText,  border: "#34d399", cls: "bg-emerald-50 border-emerald-300 text-emerald-900" },
};

// ── Data ───────────────────────────────────────────────────────────────
// Module-scope cache with a 15s TTL. Previously cached for the whole SPA
// lifetime, so newly uploaded/approved docs only appeared after a hard
// reload (the original "uploading a new HR doc hides the old one" report).

const CACHE_TTL_MS = 15_000;

let productsCache = null;
let productsCacheAt = 0;
let kbCache = null;
let kbCacheAt = 0;

async function getProducts() {
  const now = Date.now();
  if (productsCache && (now - productsCacheAt) < CACHE_TTL_MS) return productsCache;
  const res = await authFetch(`${API_BASE}/products?limit=200`);
  if (!res.ok) throw new Error(`Products: ${res.status}`);
  const d = await res.json();
  productsCache = d.products || d.items || [];
  productsCacheAt = now;
  return productsCache;
}

async function getDocs() {
  const now = Date.now();
  if (kbCache && (now - kbCacheAt) < CACHE_TTL_MS) return kbCache;
  const res = await authFetch(`${API_BASE}/kb?status=ACTIVE&limit=10000`);
  if (!res.ok) throw new Error(`KB: ${res.status}`);
  const d = await res.json();
  kbCache = d.docs || d.items || [];
  kbCacheAt = now;
  return kbCache;
}

async function loadLevel(path) {
  const depth = path.length;
  // Depth 1 also needs products for labelling — kick both fetches off in
  // parallel so a cold cache only pays one round-trip of latency.
  const [docs, products] = depth === 1
    ? await Promise.all([getDocs(), getProducts()])
    : [await getDocs(), null];

  // Level 0: Domains derived from docs.
  if (depth === 0) {
    const counts = new Map();
    for (const d of docs) {
      const dom = (d.domain && d.domain.trim()) || NO_VALUE;
      counts.set(dom, (counts.get(dom) || 0) + 1);
    }
    return [...counts.entries()]
      .map(([dom, c]) => ({ layer: "domain", id: dom, label: dom, subtitle: `${c} doc${c !== 1 ? "s" : ""}`, data: { domain: dom === NO_VALUE ? "" : dom } }))
      .sort((a, b) => a.label === NO_VALUE ? 1 : b.label === NO_VALUE ? -1 : a.label.localeCompare(b.label));
  }

  // Level 1: Products within selected domain.
  if (depth === 1) {
    const domain = path[0].data.domain;
    const pMap = new Map(products.map(p => [p.id, p.name]));
    const counts = new Map();
    for (const d of docs) {
      const dd = (d.domain && d.domain.trim()) || "";
      if (dd !== domain) continue;
      const pid = d.product_id || "";
      counts.set(pid, (counts.get(pid) || 0) + 1);
    }
    return [...counts.entries()]
      .map(([pid, c]) => ({
        layer: "product", id: pid || "__none__",
        label: pid ? (pMap.get(pid) || pid) : "(No product)",
        subtitle: `${c} doc${c !== 1 ? "s" : ""}`,
        data: { product_id: pid },
      }))
      .sort((a, b) => a.label === "(No product)" ? 1 : b.label === "(No product)" ? -1 : a.label.localeCompare(b.label));
  }

  // Level 2: Versions within domain + product.
  if (depth === 2) {
    const domain = path[0].data.domain;
    const pid = path[1].data.product_id;
    const counts = new Map();
    for (const d of docs) {
      if (((d.domain && d.domain.trim()) || "") !== domain) continue;
      if ((d.product_id || "") !== pid) continue;
      const v = (d.spec_version && d.spec_version.trim()) || NO_VALUE;
      counts.set(v, (counts.get(v) || 0) + 1);
    }
    return [...counts.entries()]
      .map(([v, c]) => ({
        layer: "version", id: v,
        label: v, subtitle: `${c} doc${c !== 1 ? "s" : ""}`,
        data: { spec_version: v === NO_VALUE ? "" : v },
      }))
      .sort((a, b) => a.label === NO_VALUE ? 1 : b.label === NO_VALUE ? -1 : a.label.localeCompare(b.label));
  }

  // Level 3: Documents.
  if (depth === 3) {
    const domain = path[0].data.domain;
    const pid = path[1].data.product_id;
    const ver = path[2].data.spec_version;
    return docs
      .filter(d => {
        if (((d.domain && d.domain.trim()) || "") !== domain) return false;
        if ((d.product_id || "") !== pid) return false;
        if (((d.spec_version && d.spec_version.trim()) || "") !== ver) return false;
        return true;
      })
      .map(d => ({
        layer: "document", id: d.id,
        label: d.name || d.filename || d.id,
        subtitle: d.namespace || undefined,
        data: { doc_id: d.id, doc_name: d.name || d.filename || d.id },
      }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }
  return [];
}

// ── Node ───────────────────────────────────────────────────────────────

// Ancestor nodes are now rendered as circles (UX redesign 2026-06-15).
// ANC_W/ANC_H are retained for layout math (edge endpoints, breadcrumb
// horizontal spacing) and represent the circle's bounding box. The visible
// circle has diameter = min(ANC_W, ANC_H). A caption below the circle
// (within ANC_W) shows a truncated label; the full label appears on hover
// via the native browser tooltip (title attribute).
const ANC_W = 130;
const ANC_H = 56;
const ANC_R = 22;          // visible circle radius
const ANC_CAP_GAP = 4;     // gap between circle and caption
// Children + initial nodes use auto-width. We only need a fixed
// height for layout spacing calculations.
const CHILD_H = 36;
// Row pitch for the virtualized children column (vertical spacing
// from one child's top to the next child's top).
const CHILD_PITCH = 44;
// Horizontal stub length from trunk to each child's left edge.
const STUB_LEN = 22;
// Gap from last ancestor's right edge to the trunk.
const TRUNK_GAP = 30;
// Extra rows rendered above/below the viewport so scrolling stays smooth.
const OVERSCAN = 4;

// ── Virtualized children column ────────────────────────────────────────
//
// Renders only the child nodes currently in the viewport (plus overscan).
// A single SVG inside the scroll container draws the vertical trunk that
// spans the full scrollable height, with short horizontal stubs to each
// rendered child. A second SVG outside the scroll container draws the
// short connector from the last ancestor to the top of the trunk.

function ChildrenColumn({ trunk, viewportH, items, onPick }) {
  // Parent remounts this component on items change via a key prop, so
  // useState here always starts fresh at 0 — no reset effect needed.
  const scrollRef = useRef(null);
  const [scrollTop, setScrollTop] = useState(0);

  const n = items.length;
  const totalH = n * CHILD_PITCH;
  // Pane height = viewport height minus a small padding. We always use the
  // full viewport so virtualization math is consistent.
  const paneH = Math.max(120, viewportH - 32);
  // Position the pane vertically centered around the ancestor's cy.
  const paneTop = Math.max(16, trunk.ancCy - paneH / 2);

  const color = trunk.color;
  const stubX1 = trunk.trunkX; // trunk's absolute x in outer coords

  // Virtualization: only render children in the visible scroll window.
  const firstIdx = Math.max(0, Math.floor(scrollTop / CHILD_PITCH) - OVERSCAN);
  const lastIdx  = Math.min(n - 1, Math.ceil((scrollTop + paneH) / CHILD_PITCH) + OVERSCAN);
  const visible = [];
  for (let i = firstIdx; i <= lastIdx; i++) visible.push(i);

  return (
    <>
      {/* Outer connector: from last ancestor's right edge horizontally to
          the trunk column, drawn in outer coords (does not scroll). */}
      <svg
        className="absolute pointer-events-none"
        style={{ left: 0, top: 0, width: "100%", height: "100%" }}
      >
        <path
          d={`M${trunk.ancRightX},${trunk.ancCy} L${stubX1},${trunk.ancCy}`}
          fill="none" stroke={color} strokeWidth={1.5} opacity={0.55}
        />
      </svg>

      {/* Scrollable virtualized pane — positioned only over the right side
          starting at the trunk column, so ancestor pills on the left remain
          clickable. Coordinates inside the pane are translated by -paneLeft. */}
      <div
        ref={scrollRef}
        onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
        className="absolute overflow-y-auto"
        style={{
          left: stubX1 - 4,
          top: paneTop,
          right: 0,
          height: paneH,
          scrollbarWidth: "thin",
        }}
      >
        {/* Scroll surface: full content height. Children/stubs are drawn
            in coords relative to the pane's left edge. */}
        <div style={{ position: "relative", height: totalH, width: "100%" }}>
          {/* Single SVG spanning the whole scroll surface: continuous trunk
              + stubs for every rendered child. */}
          <svg
            className="pointer-events-none"
            style={{ position: "absolute", left: 0, top: 0, width: "100%", height: totalH }}
          >
            {/* Vertical trunk at the left edge of the pane. */}
            <line
              x1={4} x2={4}
              y1={CHILD_PITCH / 2}
              y2={(n - 1) * CHILD_PITCH + CHILD_PITCH / 2}
              stroke={color} strokeWidth={1.5} opacity={0.55}
            />
            {/* Horizontal stubs — only for rendered children. */}
            {visible.map(i => {
              const cy = i * CHILD_PITCH + CHILD_PITCH / 2;
              return (
                <line key={`stub-${i}`}
                  x1={4} x2={4 + STUB_LEN} y1={cy} y2={cy}
                  stroke={color} strokeWidth={1.5} opacity={0.55}
                />
              );
            })}
          </svg>

          {/* Visible child nodes only — positioned at the stub end. */}
          {visible.map(i => {
            const it = items[i];
            const y = i * CHILD_PITCH + (CHILD_PITCH - CHILD_H) / 2;
            return (
              <Node
                key={`k-${it.id}`}
                x={4 + STUB_LEN}
                y={y}
                layer={it.layer}
                label={it.label}
                subtitle={it.subtitle}
                onClick={() => onPick(it)}
              />
            );
          })}
        </div>
      </div>
    </>
  );
}

// ── DocumentsPanel ────────────────────────────────────────────────────
//
// Right-docked panel rendered when the user has drilled Domain → Product
// → Spec Version and is now picking a specific document. Replaces the
// previous cramped vertical "trunk+stubs" column used at depth 3, which
// did not scale well for 1000+ docs and offered no search.
//
// Features:
//   - Pinned search input at the top (250 ms debounce).
//   - Match counter ("42 of 1,284 documents").
//   - Substring highlighting via highlightMatch() from utils/kbFormat.
//   - Virtualization is intentionally NOT used here — at ~1k DOM rows
//     modern browsers handle it fine, and we avoid a new dependency.
//     If usage scales beyond ~5k docs, swap to react-window.
//   - Keyboard accessibility: arrow keys + Enter on focused item.

function DocumentsPanel({
  ancestorPath,
  items,
  onPick,
  highlightedId,
}) {
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const inputRef = useRef(null);

  // Reset filter when scope changes (parent remounts via key).
  // 250 ms debounce keeps typing responsive without re-filtering on
  // every keystroke for huge lists.
  useEffect(() => {
    const t = setTimeout(() => setDebounced(query.trim()), 250);
    return () => clearTimeout(t);
  }, [query]);

  // Focus the search input as soon as the panel mounts so the user can
  // type immediately — matches the "/" shortcut the rest of the app uses.
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const total = items.length;
  const filtered = useMemo(() => {
    const q = debounced.toLowerCase();
    if (!q) return items;
    return items.filter(it =>
      (it.label || "").toLowerCase().includes(q) ||
      (it.subtitle || "").toLowerCase().includes(q),
    );
  }, [items, debounced]);

  // Breadcrumb summary string for the panel header.
  const crumb = ancestorPath
    .map(e => e.label)
    .filter(Boolean)
    .join(" / ");

  return (
    <div className="absolute inset-y-3 right-3 w-[360px] max-w-[55%] bg-white border border-gray-200 rounded-xl shadow-md flex flex-col overflow-hidden z-10">
      {/* Header */}
      <div className="px-3 py-2 border-b border-gray-100 bg-gradient-to-r from-emerald-50 to-white">
        <div className="flex items-center gap-2 text-[11px] text-gray-500">
          <FileText size={12} className="text-emerald-500" />
          <span className="truncate" title={crumb}>{crumb}</span>
        </div>
        <div className="mt-1 text-[10px] text-gray-400">
          {debounced
            ? `${filtered.length} of ${total} document${total !== 1 ? "s" : ""}`
            : `${total} document${total !== 1 ? "s" : ""}`}
        </div>
      </div>

      {/* Search */}
      <div className="px-3 py-2 border-b border-gray-100">
        <div className="relative">
          <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            ref={inputRef}
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search documents in this scope…"
            aria-label="Search documents"
            className="w-full pl-7 pr-2 py-1.5 text-xs border border-gray-200 rounded-md focus:outline-none focus:border-emerald-300 focus:ring-2 focus:ring-emerald-100"
          />
        </div>
      </div>

      {/* List */}
      <div
        className="flex-1 overflow-y-auto"
        role="listbox"
        aria-label="Documents in scope"
      >
        {filtered.length === 0 && (
          <div className="px-3 py-6 text-center">
            <FileText size={20} className="mx-auto text-gray-300 mb-1.5" />
            <p className="text-xs text-gray-500">
              {debounced
                ? `No documents match “${debounced}” in this scope.`
                : "No documents in this scope."}
            </p>
          </div>
        )}
        {filtered.map((it) => {
          const isActive = it.id === highlightedId;
          const segs = highlightMatch(it.label, debounced);
          return (
            <div
              key={`doc-${it.id}`}
              role="option"
              aria-selected={isActive}
              tabIndex={0}
              onClick={() => onPick(it)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onPick(it);
                }
              }}
              className={[
                "group flex items-start gap-2 px-3 py-2 cursor-pointer border-b border-gray-50",
                "hover:bg-emerald-50 focus:bg-emerald-50 focus:outline-none",
                isActive ? "bg-emerald-100/60" : "",
              ].join(" ")}
            >
              <FileText size={13} className="mt-0.5 text-emerald-500 flex-shrink-0" />
              <div className="min-w-0 flex-1">
                <p className="text-xs font-medium text-gray-800 truncate">
                  {segs.map((s, i) =>
                    s.match
                      ? <mark key={i} className="bg-amber-200 text-gray-900 rounded px-0.5">{s.text}</mark>
                      : <span key={i}>{s.text}</span>
                  )}
                </p>
                {it.subtitle && (
                  <p className="text-[10px] text-gray-400 truncate mt-0.5">{it.subtitle}</p>
                )}
              </div>
              <ChevronRight size={12} className="mt-1 text-gray-300 group-hover:text-emerald-500 flex-shrink-0" />
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Node({ x, y, layer, label, subtitle, small, onClick, delay }) {
  const S = STYLE[layer];
  const Icon = S.icon;

  // ── Ancestor (small) = circular badge + caption below.
  // Edge endpoints in the parent layout still anchor to the circle's center
  // (cx = x + ANC_W/2, cy = y + ANC_R) so existing edge math is consistent.
  if (small) {
    const cx = ANC_W / 2;
    const circleTop = 0;
    const captionTop = ANC_R * 2 + ANC_CAP_GAP;
    // Caption truncation: keep ~12 chars then ellipsis. Browser title gives
    // full label on hover for accessibility.
    const captionText = label.length > 12 ? `${label.slice(0, 11)}…` : label;
    return (
      <div
        onClick={onClick}
        title={label}
        aria-label={`${layer}: ${label}`}
        style={{
          position: "absolute",
          left: x, top: y,
          width: ANC_W, height: ANC_H,
          opacity: delay ? 0 : 1,
          animation: delay ? `kbSlide 0.25s ease ${delay}ms forwards` : undefined,
        }}
        className="flex flex-col items-center cursor-pointer select-none group"
      >
        <div
          style={{
            position: "absolute", left: cx - ANC_R, top: circleTop,
            width: ANC_R * 2, height: ANC_R * 2, borderRadius: "9999px",
          }}
          className={[
            "flex items-center justify-center border shadow-sm",
            "hover:shadow-md transition-shadow",
            S.cls,
          ].join(" ")}
        >
          <Icon size={14} className="opacity-70" />
        </div>
        <span
          style={{
            position: "absolute",
            left: 0, top: captionTop,
            width: ANC_W,
          }}
          className="text-[10px] font-medium text-gray-600 text-center truncate px-1"
        >
          {captionText}
        </span>
      </div>
    );
  }

  // ── Non-ancestor (child / root) = retains capsule pill, auto-width.
  return (
    <div
      onClick={onClick}
      style={{
        position: "absolute", left: x, top: y,
        height: CHILD_H,
        opacity: delay ? 0 : 1,
        animation: delay ? `kbSlide 0.25s ease ${delay}ms forwards` : undefined,
      }}
      className={[
        "flex items-center gap-2 rounded-full border shadow-sm cursor-pointer",
        "hover:shadow-md transition-shadow select-none",
        S.cls,
        "px-3.5 text-xs whitespace-nowrap",
      ].join(" ")}
    >
      <Icon size={14} className="flex-shrink-0 opacity-60" />
      <span className="font-medium">{label}</span>
      {subtitle && <span className="opacity-40 text-[9px] ml-1">({subtitle})</span>}
      <ChevronRight size={11} className="opacity-25 flex-shrink-0 ml-1" />
    </div>
  );
}

// ── Main ───────────────────────────────────────────────────────────────

export default function KbDrillGraph({ onScopeReady }) {
  const [path, setPath]       = useState([]);
  const [items, setItems]     = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState(null);
  const boxRef                = useRef(null);
  const [size, setSize]       = useState(null);

  // Measure container.
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) setSize({ w: r.width, h: r.height });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Fetch on path change. Render every level — auto-skipping single-child
  // levels hid the existence of sibling branches and made docs invisible
  // when they differed only on spec_version.
  useEffect(() => {
    let dead = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setError(null);
    loadLevel(path).then(list => {
      if (dead) return;
      setItems(list);
      setLoading(false);
    }).catch(e => {
      if (!dead) { setError(String(e?.message || e)); setLoading(false); }
    });
    return () => { dead = true; };
  }, [path]);

  const drill  = useCallback(item => setPath(p => [...p, item]), []);
  const goBack = useCallback(i => setPath(p => p.slice(0, i)), []);

  const confirm = useCallback(() => {
    const s = { product_id: null, domain: null, spec_version: "", parent_doc_id: null, _productName: null, _documentName: null };
    for (const e of path) {
      if (e.layer === "domain")   s.domain = e.data.domain || null;
      if (e.layer === "product")  { s.product_id = e.data.product_id || null; s._productName = e.label; }
      if (e.layer === "version")  s.spec_version = e.data.spec_version;
      if (e.layer === "document") { s.parent_doc_id = e.data.doc_id; s._documentName = e.label; }
    }
    onScopeReady?.(s);
  }, [path, onScopeReady]);

  // Confirm is only allowed after a specific document is picked. Without a
  // document, the gateway has no kb_doc_id to give the coverage_retriever, so
  // KB_RETRIEVAL_SCOPE=full_file and =both silently degrade to RAG-only. The
  // spec is: "after selecting the doc, below one button will be present
  // having 'chat with scope'" — enforce that here, not in the badge text.
  const canConfirm = path.some(e => e.layer === "document");
  const isLeaf = path.length >= 4;

  // ── Layout ─────────────────────────────────────────────────────────

  const layout = useMemo(() => {
    if (!size) return null;
    const { w, h } = size;
    const ancs = [], ancEdges = [];

    if (path.length === 0) {
      // Grid for root items — no virtualization; root usually has few entries.
      const n = items.length;
      if (n === 0) return { ancs, rootKids: [], ancEdges, fanKids: [], fanEdges: [], trunk: null };
      const cols = n <= 4 ? 2 : Math.min(3, Math.ceil(Math.sqrt(n)));
      const rows = Math.ceil(n / cols);
      const gapX = 220, gapY = CHILD_H + 16;
      const offX = (w - (cols - 1) * gapX) / 2 - 60;
      const offY = (h - (rows - 1) * gapY) / 2 - 18;
      const rootKids = items.map((it, i) => ({
        item: it,
        x: offX + (i % cols) * gapX,
        y: offY + Math.floor(i / cols) * gapY,
      }));
      return { ancs, rootKids, ancEdges, fanKids: [], fanEdges: [], trunk: null };
    }

    // Ancestors: horizontal row, vertically centered on the circle center.
    // ANC_W is the bounding box width; the circle sits at the box's
    // horizontal center with radius ANC_R, caption below. We compute cx/cy
    // for the visible circle so edges land cleanly on its perimeter.
    const ancGapX = 10;
    const ancStartX = Math.max(16, w * 0.03);
    // Centre the entire ancestor bounding box (circle + caption) on h/2.
    const ancY = h / 2 - ANC_H / 2;

    path.forEach((entry, i) => {
      const ax = ancStartX + i * (ANC_W + ancGapX);
      const cx = ax + ANC_W / 2;
      const cy = ancY + ANC_R; // top half of the box = circle
      ancs.push({
        item: entry, x: ax, y: ancY,
        cx, cy,
        // Right/left perimeter points for edge anchoring.
        rightX: cx + ANC_R,
        leftX:  cx - ANC_R,
      });
    });

    // Horizontal lines between adjacent ancestor circles — anchor to the
    // circle perimeter (not the bounding box) so the line never crosses
    // into the caption space.
    for (let i = 1; i < ancs.length; i++) {
      const prev = ancs[i - 1];
      const curr = ancs[i];
      ancEdges.push({
        id: `a${i}`,
        x1: prev.rightX, y1: prev.cy,
        x2: curr.leftX,  y2: curr.cy,
        color: STYLE[curr.item.layer].border,
      });
    }

    const lastAnc = ancs[ancs.length - 1];
    const ancRightX = lastAnc ? lastAnc.rightX : ancStartX + ANC_W;
    const ancCy = lastAnc ? lastAnc.cy : h / 2;

    // Decide between fan-out and trunk+stubs based on whether children fit.
    // We need: n * minPitch <= availableH for the fan to look comfortable.
    // minPitch = CHILD_H + 14 matches the previous spacing.
    const n = items.length;
    const pad = 16;
    const minPitch = CHILD_H + 14;
    const availH = h - pad * 2;
    const fits = !isLeaf && n > 0 && n * minPitch <= availH;

    let fanKids = [];
    let fanEdges = [];
    let trunk = null;

    if (fits) {
      // Fan-out layout: children evenly spread top-to-bottom, each with its
      // own bezier edge from the last ancestor.
      const kidGap = Math.min(minPitch + 4, availH / Math.max(n, 1));
      const kidTotalH = (n - 1) * kidGap;
      const kidStartY = (h - kidTotalH) / 2 - CHILD_H / 2;
      const kx = ancRightX + 50;
      items.forEach((it, i) => {
        const ky = kidStartY + i * kidGap;
        fanKids.push({ item: it, x: kx, y: ky });
        fanEdges.push({
          id: `k${i}`,
          x1: ancRightX, y1: ancCy,
          x2: kx,        y2: ky + CHILD_H / 2,
          color: STYLE[it.layer].border,
          delay: i * 25,
        });
      });
    } else if (!isLeaf && n > 0) {
      // Trunk+stubs layout: children scroll in a virtualized column.
      const trunkX = ancRightX + TRUNK_GAP;
      const childX = trunkX + STUB_LEN;
      trunk = {
        ancRightX,
        ancCy,
        trunkX,
        childX,
        color: STYLE[items[0].layer].border,
      };
    }

    return { ancs, rootKids: [], ancEdges, fanKids, fanEdges, trunk };
  }, [size, path, items, isLeaf]);

  // ── Render ─────────────────────────────────────────────────────────

  return (
    <div className="flex flex-col flex-1" style={{ minHeight: 0 }}>
      <style>{`@keyframes kbSlide { from { opacity:0; transform:translateX(-10px); } to { opacity:1; transform:translateX(0); } }`}</style>

      {/* Breadcrumb */}
      <div className="flex-shrink-0 flex items-center gap-1.5 flex-wrap px-4 py-2 border-b border-gray-100 bg-white min-h-[38px]">
        {path.length === 0 ? (
          <span className="text-xs text-gray-400 italic">Select a domain to begin</span>
        ) : (
          <>
            <button type="button" onClick={() => setPath([])} className="text-[10px] text-gray-400 hover:text-gray-600 cursor-pointer underline">All</button>
            {path.map((e, i) => {
              const S = STYLE[e.layer]; const Icon = S.icon;
              return (
                <span key={`${e.id}-${i}`} className="flex items-center gap-1">
                  <ChevronRight size={10} className="text-gray-300" />
                  <button type="button" onClick={() => goBack(i)}
                    className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[11px] font-medium cursor-pointer hover:brightness-95 ${S.cls}`}>
                    <Icon size={10} /><span className="truncate max-w-[100px]">{e.label}</span>
                  </button>
                </span>
              );
            })}
            {canConfirm && (
              <button type="button" onClick={confirm}
                className="ml-auto flex items-center gap-1.5 px-3 py-1 rounded-full bg-indigo-600 text-white text-[11px] font-medium hover:bg-indigo-700 cursor-pointer transition shadow-sm">
                <CheckCircle2 size={12} /> Chat with this scope
              </button>
            )}
          </>
        )}
      </div>

      {/* Graph area */}
      <div ref={boxRef} className="flex-1 relative overflow-hidden" style={{ minHeight: 300, background: "linear-gradient(135deg, #f8fafc 0%, #fff 100%)" }}>

        {loading && (
          <div className="absolute inset-0 z-20 flex items-center justify-center">
            <Loader2 size={22} className="animate-spin text-indigo-400" />
          </div>
        )}

        {error && (
          <div className="absolute top-4 left-4 right-4 z-20 flex items-center gap-2 px-3 py-2 bg-red-50 border border-red-200 rounded-lg text-xs text-red-700">
            <AlertTriangle size={13} /> {error}
          </div>
        )}

        {isLeaf && !loading && (
          <div className="absolute bottom-6 left-0 right-0 z-10 flex items-center justify-center">
            <div className="flex items-center gap-3 bg-white border border-gray-200 rounded-xl shadow-md px-5 py-3">
              <CheckCircle2 size={20} className="text-emerald-500 flex-shrink-0" />
              <span className="text-sm text-gray-700 font-medium">Scope narrowed</span>
              <button type="button" onClick={confirm}
                className="flex items-center gap-1.5 px-4 py-1.5 rounded-full bg-indigo-600 text-white text-xs font-medium hover:bg-indigo-700 cursor-pointer transition shadow-sm">
                <CheckCircle2 size={12} /> Chat with this scope
              </button>
            </div>
          </div>
        )}

        {!loading && !error && !isLeaf && items.length === 0 && path.length !== 3 && (
          <div className="absolute inset-0 z-10 flex flex-col items-center justify-center text-center px-8">
            <Globe2 size={32} className="text-gray-300 mb-3" />
            <p className="text-sm text-gray-500 font-medium">No items at this level</p>
            <p className="text-xs text-gray-400 mt-1">
              {path.length === 0 ? "No approved documents found. Upload documents first." : "Try going back and choosing a different path."}
            </p>
            {canConfirm && (
              <button type="button" onClick={confirm}
                className="mt-3 flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-indigo-600 text-white text-[11px] font-medium hover:bg-indigo-700 cursor-pointer transition">
                <CheckCircle2 size={12} /> Chat with current scope
              </button>
            )}
          </div>
        )}

        {/* SVG: ancestor straight edges + fan-out bezier edges (when used).
            The trunk+stubs edges are drawn inside the ChildrenColumn instead. */}
        {layout && size && (layout.ancEdges.length > 0 || layout.fanEdges.length > 0) && (
          <svg className="absolute inset-0 pointer-events-none" width={size.w} height={size.h}>
            {layout.ancEdges.map(e => (
              <path key={e.id} d={`M${e.x1},${e.y1} L${e.x2},${e.y2}`}
                fill="none" stroke={e.color} strokeWidth={1.5} opacity={0.4} />
            ))}
            {path.length !== 3 && layout.fanEdges.map(e => {
              const dx = e.x2 - e.x1;
              const d = `M${e.x1},${e.y1} C${e.x1 + dx * 0.5},${e.y1} ${e.x2 - dx * 0.5},${e.y2} ${e.x2},${e.y2}`;
              return (
                <path key={e.id} d={d}
                  fill="none" stroke={e.color} strokeWidth={1.5} opacity={0.45}
                  style={e.delay ? { opacity: 0, animation: `kbSlide 0.25s ease ${e.delay}ms forwards` } : undefined} />
              );
            })}
          </svg>
        )}

        {/* Ancestor pills */}
        {layout?.ancs.map((a, i) => (
          <Node key={`a-${a.item.id}-${i}`} x={a.x} y={a.y} layer={a.item.layer} label={a.item.label} small onClick={() => goBack(i)} />
        ))}

        {/* Root grid (no ancestors yet) */}
        {layout?.rootKids.map((k, i) => (
          <Node key={`r-${k.item.id}`} x={k.x} y={k.y} layer={k.item.layer} label={k.item.label}
            subtitle={k.item.subtitle} onClick={() => drill(k.item)} delay={i * 35 + 60} />
        ))}

        {/* Fan-out child nodes (when children fit in viewport).
            Suppressed at depth 3 because DocumentsPanel takes over there. */}
        {!isLeaf && path.length !== 3 && layout?.fanKids.map((k, i) => (
          <Node key={`f-${k.item.id}`} x={k.x} y={k.y} layer={k.item.layer} label={k.item.label}
            subtitle={k.item.subtitle} onClick={() => drill(k.item)} delay={i * 35 + 60} />
        ))}

        {/* Virtualized children column with trunk+stubs edges (only when
            children don't fit in viewport). Key derived from path depth +
            last entry id so the column remounts (scrollTop resets) on level change.
            Suppressed at depth 3 in favour of the cleaner DocumentsPanel. */}
        {!isLeaf && path.length !== 3 && layout?.trunk && items.length > 0 && size && (
          <ChildrenColumn
            key={`cc-${path.length}-${path[path.length - 1]?.id || "root"}`}
            trunk={layout.trunk}
            viewportH={size.h}
            items={items}
            onPick={drill}
          />
        )}

        {/* Depth-3 (documents) — right-docked panel with search.
            Replaces the cramped fan / virtualized trunk for the doc picker. */}
        {!isLeaf && path.length === 3 && !loading && (
          <DocumentsPanel
            key={`docs-${path.map(p => p.id).join("/")}`}
            ancestorPath={path}
            items={items}
            onPick={drill}
            highlightedId={null}
          />
        )}

        {/* Hint */}
        {!loading && !isLeaf && items.length > 0 && (
          <div className="absolute bottom-3 left-0 right-0 text-center pointer-events-none z-10">
            <span className="text-[10px] text-gray-400 bg-white/80 px-2 py-1 rounded">
              {path.length === 0 && "Click a domain to see its products"}
              {path.length === 1 && "Click a product to see its versions"}
              {path.length === 2 && "Click a version to see its documents"}
              {path.length === 3 && "Click a document or chat with current scope"}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
