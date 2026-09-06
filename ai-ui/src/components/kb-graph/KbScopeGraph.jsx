// SPDX-License-Identifier: MIT
// KbScopeGraph — force-directed scope picker for KB chat.
//
// REPLACES KbDrillGraph.jsx (the strict 4-level drill-down) with a single
// canvas where the entire KB taxonomy is visible at once. The user picks a
// node at whatever depth they're confident about — Domain, Product, Spec
// Version, or a specific Document — and starts a scoped chat from there.
//
// Why this exists:
//   The drill-down forced linear navigation and hid sibling branches once you
//   stepped down a level. Users who only know "it's somewhere in HR" had to
//   pick a doc anyway because the confirm gate required it. The graph treats
//   every depth as a legitimate scope target and lets the user see the whole
//   taxonomy while they decide.
//
// Backend contract preserved: the shape handed to onScopeReady is identical
// to the drill-down's — { product_id, domain, spec_version, parent_doc_id,
// _productName, _documentName } — so KbChatPanel.handleScopeReady's
// POST /chats call is untouched.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Globe2, Package, GitBranch, FileText, Database,
  Search, CheckCircle2, RefreshCw, Loader2, AlertTriangle,
  Crosshair, Maximize2, Minimize2, X,
} from "lucide-react";
import { API_BASE, authFetch } from "../../config";
import { highlightMatch } from "../../utils/kbFormat.js";

// ── Layer styling ──────────────────────────────────────────────────────
// Hues spaced across the colour wheel so each layer is unambiguously
// distinguishable at a glance. The previous palette (indigo / sky /
// violet / emerald) was too cool-clustered — indigo and sky read as the
// same shade of blue and violet collided with the spine glow.
//
// New layout:
//   root       — slate (neutral anchor)
//   domain     — indigo  (user's primary entry point; matches the UI accent)
//   product    — amber   (warm, totally distinct from indigo)
//   version    — teal    (cool but different family from indigo; ≠ amber)
//   document   — rose   (warm; distinct from amber via hue, distinct from
//                         teal via temperature)
// Result: domain → product → version → document moves the hue around
// the wheel (indigo → amber → teal → rose) so no two adjacent layers
// share a colour family.
const LAYER = {
  root:     { color: "#475569", ring: "#94a3b8", icon: Database,  label: "Knowledge Base" },
  domain:   { color: "#4f46e5", ring: "#818cf8", icon: Globe2 },
  product:  { color: "#d97706", ring: "#fbbf24", icon: Package },
  version:  { color: "#0d9488", ring: "#5eead4", icon: GitBranch },
  document: { color: "#e11d48", ring: "#fb7185", icon: FileText },
};

const NO_DOMAIN  = "(Unclassified)";
const NO_PRODUCT = "(No product)";
const NO_VERSION = "(No version)";

// ── Module-level cache w/ short TTL ────────────────────────────────────
// Newly-approved docs should appear without a hard reload — 15s mirrors the
// drill-down's cache (fixes the "uploading a new HR doc hides the old one"
// report we documented during the previous redesign).
const CACHE_TTL_MS = 15_000;
let productsCache = null, productsCacheAt = 0;
let kbCache = null, kbCacheAt = 0;

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

// ── Tree assembly ──────────────────────────────────────────────────────
// Build a rooted tree from the docs list. Documents are NOT inserted as
// nodes by default — they're materialised only when their parent version is
// "expanded" (single-clicked). This keeps the rendered node count
// manageable when a corpus has thousands of docs.
//
// Returns:
//   { nodes:  [{ id, type, label, parent, count, doc? }],
//     edges:  [{ src, dst }],
//     docsByVersion: Map<versionId, doc[]> }
function buildTree(docs, products, expandedVersions) {
  const pName = new Map(products.map(p => [p.id, p.name]));
  const nodes = [];
  const edges = [];

  // Root
  nodes.push({ id: "__root__", type: "root", label: "Knowledge Base", parent: null, count: docs.length });

  // Group docs by (domain, product_id, spec_version)
  const tree = new Map(); // domain -> Map(product_id -> Map(spec_version -> doc[]))
  for (const d of docs) {
    const dom = (d.domain && d.domain.trim()) || NO_DOMAIN;
    const pid = d.product_id || "";
    const ver = (d.spec_version && d.spec_version.trim()) || NO_VERSION;
    if (!tree.has(dom)) tree.set(dom, new Map());
    const byProd = tree.get(dom);
    if (!byProd.has(pid)) byProd.set(pid, new Map());
    const byVer = byProd.get(pid);
    if (!byVer.has(ver)) byVer.set(ver, []);
    byVer.get(ver).push(d);
  }

  const docsByVersion = new Map();

  for (const [dom, byProd] of tree) {
    const domId = `d|${dom}`;
    const domCount = [...byProd.values()].reduce(
      (acc, m) => acc + [...m.values()].reduce((a, ds) => a + ds.length, 0), 0,
    );
    nodes.push({
      id: domId, type: "domain",
      label: dom, parent: "__root__", count: domCount,
      data: { domain: dom === NO_DOMAIN ? "" : dom },
    });
    edges.push({ src: "__root__", dst: domId });

    for (const [pid, byVer] of byProd) {
      const prodId = `p|${dom}|${pid}`;
      const prodLabel = pid ? (pName.get(pid) || pid) : NO_PRODUCT;
      const prodCount = [...byVer.values()].reduce((a, ds) => a + ds.length, 0);
      nodes.push({
        id: prodId, type: "product",
        label: prodLabel, parent: domId, count: prodCount,
        data: {
          domain: dom === NO_DOMAIN ? "" : dom,
          product_id: pid || "",
          _productName: pid ? prodLabel : null,
        },
      });
      edges.push({ src: domId, dst: prodId });

      for (const [ver, ds] of byVer) {
        const verId = `v|${dom}|${pid}|${ver}`;
        nodes.push({
          id: verId, type: "version",
          label: ver, parent: prodId, count: ds.length,
          data: {
            domain: dom === NO_DOMAIN ? "" : dom,
            product_id: pid || "",
            _productName: pid ? prodLabel : null,
            spec_version: ver === NO_VERSION ? "" : ver,
          },
        });
        edges.push({ src: prodId, dst: verId });

        docsByVersion.set(verId, ds);

        if (expandedVersions.has(verId)) {
          for (const doc of ds) {
            const docId = `doc|${doc.id}`;
            nodes.push({
              id: docId, type: "document",
              label: doc.name || doc.filename || doc.id,
              parent: verId, count: 0, doc,
              data: {
                domain: dom === NO_DOMAIN ? "" : dom,
                product_id: pid || "",
                _productName: pid ? prodLabel : null,
                spec_version: ver === NO_VERSION ? "" : ver,
                parent_doc_id: doc.id,
                _documentName: doc.name || doc.filename || doc.id,
              },
            });
            edges.push({ src: verId, dst: docId });
          }
        }
      }
    }
  }

  return { nodes, edges, docsByVersion };
}

// ── Force layout ───────────────────────────────────────────────────────
// Adapted from KnowledgeGraph.jsx. Tuned for sparse trees (smaller
// repulsion, shorter spring length). Initial placement is concentric: root
// at the origin, each subsequent depth on a wider ring — this gives the
// solver a sensible starting state for tree-shaped graphs and avoids the
// "collapsed cluster then explode" startup we'd otherwise see.
const W = 1600, H = 1000;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

function depthOf(nodes) {
  // BFS from root to compute each node's depth. O(n).
  const byId = new Map(nodes.map(n => [n.id, n]));
  const depth = new Map();
  depth.set("__root__", 0);
  // Iterate until stable — tree is acyclic so this terminates in O(maxDepth).
  let changed = true;
  while (changed) {
    changed = false;
    for (const n of nodes) {
      if (depth.has(n.id)) continue;
      if (n.parent && depth.has(n.parent)) {
        depth.set(n.id, depth.get(n.parent) + 1);
        changed = true;
      }
    }
  }
  // Defensive: any orphaned nodes get depth 1.
  for (const n of nodes) if (!depth.has(n.id)) depth.set(n.id, 1);
  return { depth, byId };
}

// computeLayout(nodes, edges, prevPositions?)
//
// `prevPositions` (optional) is a map of node-id → {x, y} from a
// previous run. When provided:
//   - Nodes that already exist there reuse their old position exactly
//     and are PINNED through the solver (zero velocity, never moved).
//     This is what stops the "version jumps to a new place when it
//     expands" effect — the version itself, its ancestors, its
//     siblings, and every other already-laid-out node stay put.
//   - Brand-new nodes (e.g. the documents that just appeared when the
//     user expanded a version) get a fresh initial position near their
//     parent and are run through a small number of solver iterations
//     so they fan out cleanly without colliding.
//
// When `prevPositions` is null/undefined (first paint), the full
// concentric initial placement and full solver run are used as before.
function computeLayout(nodes, edges, prevPositions = null) {
  if (nodes.length === 0) return {};
  const { depth } = depthOf(nodes);
  // Ring radii by depth — root at center, then 220 / 440 / 640 / 820.
  // Documents get a tighter ring around their version parent so clusters
  // stay readable.
  const ringR = [0, 220, 440, 640, 820];

  // Group siblings per depth so we can fan them around the ring evenly.
  const byDepth = new Map();
  for (const n of nodes) {
    const d = depth.get(n.id);
    if (!byDepth.has(d)) byDepth.set(d, []);
    byDepth.get(d).push(n);
  }

  // Build parent-id lookup so we can place new nodes near their parent.
  const parentById = new Map();
  for (const n of nodes) parentById.set(n.id, n.parent);

  const pos = {};
  // pinned[id] === true means the solver MUST NOT move this node.
  const pinned = {};
  // Per-parent precomputed document-arc cache. Used ONLY for nodes
  // whose type is "document" — we lay docs out deterministically in a
  // radial arc around their version parent so dense expansions look
  // tidy. Non-document nodes are unaffected by this; their seed path
  // and force-solve are unchanged.
  const docArcByParent = new Map();

  for (const [d, list] of byDepth) {
    const R = ringR[d] ?? (820 + (d - 4) * 180);
    const n = list.length;
    list.forEach((nd, i) => {
      const prev = prevPositions ? prevPositions[nd.id] : null;
      if (prev) {
        // Existing node — reuse its position EXACTLY and pin it.
        pos[nd.id] = { x: prev.x, y: prev.y, vx: 0, vy: 0 };
        pinned[nd.id] = true;
      } else if (prevPositions) {
        // Incremental layout but this node is new (e.g. a freshly-
        // revealed document). Seed it near its parent so the solver
        // pushes it out radially rather than dragging it across the
        // canvas from the ring origin. We give siblings a small angular
        // spread so they don't all start in the same spot.
        const parentId = parentById.get(nd.id);
        const parentPos = parentId ? (prevPositions[parentId] || pos[parentId]) : null;
        if (parentPos) {
          // ── DOCUMENT ONLY: deterministic geometric arc layout ──
          // Docs revealed by expanding a version benefit from a
          // hard-coded radial arc instead of relying on the force
          // solver, because:
          //   - the spring can pull a doc INWARD past the version
          //     toward root, dropping it on top of an ancestor;
          //   - dense expansions (50+ docs) look chaotic from the
          //     solver's stochastic settling.
          // We compute the arc once per parent (cached in
          // docArcByParent so all siblings see the same geometry) and
          // mark each placed doc as pinned so the force loop and the
          // overlap post-pass leave them alone.
          //
          // This block is strictly gated on `nd.type === "document"`,
          // so non-document nodes (products / versions / domains)
          // continue to use the existing 90-unit symmetric fan seed
          // below. That preserves the previously-correct orientation
          // of the rest of the graph.
          if (nd.type === "document") {
            let arcCache = docArcByParent.get(parentId);
            if (!arcCache) {
              const newSiblings = list
                .filter(s => s.type === "document"
                          && !prevPositions[s.id]
                          && parentById.get(s.id) === parentId)
                .map(s => s.id);
              // Outward axis: root → parent direction.
              const dx0 = parentPos.x - W / 2;
              const dy0 = parentPos.y - H / 2;
              const outwardAngle = Math.atan2(dy0, dx0);
              // Doc-to-doc minimum centre distance. Matches the
              // overlap post-pass's MIN_DIST so the post-pass has no
              // arc-internal work to do.
              const MIN_DIST_DOC = 180;
              // Minimum PERPENDICULAR clearance from any radial line
              // (version → inner doc). If an outer doc's perpendicular
              // distance to that line is less than this, it visually
              // sits "on the line" — the regression the user reported.
              // 80 viewBox-units clears the doc's circle (r≈7) plus
              // halo extent (~30) with breathing room on both sides.
              const RADIAL_CLEARANCE = 80;
              // First arc sits 220 viewBox-units from the version
              // centre — that clears the version's circle + glow
              // halo (r≈22 max) with breathing room.
              const FIRST_ARC_R = 220;
              const ROW_SPACING = MIN_DIST_DOC;
              // Total fan width: 110° on the outward side. Keeps the
              // cluster visibly "in front of" the version rather
              // than wrapping behind it.
              const ARC_TOTAL = (110 * Math.PI) / 180;

              // Slot capacity for an arc of given radius: chord
              // 2R·sin(Δθ/2) must be ≥ MIN_DIST_DOC.
              const slotsForArc = (radius) => {
                const step = 2 * Math.asin(Math.min(0.999, MIN_DIST_DOC / (2 * radius)));
                return Math.max(1, Math.floor(ARC_TOTAL / step) + 1);
              };

              // Tracks every (radius, angle) we've already placed —
              // used to refuse candidate angles on outer rows that
              // would land too close to a radial line through any
              // inner-row doc.
              //
              // The geometric test for "doc at (R_outer, theta_outer)
              // sits on the line version → (R_inner, theta_inner)":
              //   the perpendicular distance from the outer doc to
              //   that line is R_outer · sin(|theta_outer - theta_inner|).
              //   The constraint we want is that distance ≥
              //   RADIAL_CLEARANCE, which gives the minimum allowable
              //   angular separation:
              //     |theta_outer - theta_inner| ≥ asin(RADIAL_CLEARANCE / R_outer)
              //   This shrinks with larger R_outer (more circumference
              //   per radian) so the constraint is naturally laxer the
              //   further out we go.
              const placedPolar = [];  // [{ R, theta }]

              const isAngleClear = (R, theta) => {
                for (const { R: Ri, theta: ti } of placedPolar) {
                  // Only test against INNER docs — they're the ones
                  // whose radial line we could be sitting on.
                  if (Ri >= R) continue;
                  const minSep = Math.asin(Math.min(0.999, RADIAL_CLEARANCE / R));
                  let d = Math.abs(theta - ti);
                  // Angular distance is symmetric; the version's outward
                  // arc never wraps past π so we don't bother with the
                  // 2π modulus, but guard against it for safety.
                  if (d > Math.PI) d = 2 * Math.PI - d;
                  if (d < minSep) return false;
                }
                return true;
              };

              const positions = {};
              let placed = 0;
              let row = 0;
              while (placed < newSiblings.length) {
                const radius = FIRST_ARC_R + row * ROW_SPACING;
                const slots  = slotsForArc(radius);
                const toPlace = Math.min(slots, newSiblings.length - placed);

                // Build a candidate angle list for this row. We
                // generate MORE candidates than slots (× 3) so that
                // when some get rejected by the radial-clearance test
                // we still have enough to choose from.
                const candidateCount = Math.max(toPlace * 3, slots * 2);
                const candidates = [];
                for (let s = 0; s < candidateCount; s++) {
                  const t = candidateCount === 1 ? 0.5 : s / (candidateCount - 1);
                  candidates.push(outwardAngle + (t - 0.5) * ARC_TOTAL);
                }

                // Pick toPlace angles that (a) clear every inner-row
                // doc's radial line AND (b) are at least one full
                // angularStep apart from each other so adjacent
                // siblings on the same row stay separated.
                const chosen = [];
                for (const cand of candidates) {
                  if (chosen.length === toPlace) break;
                  if (!isAngleClear(radius, cand)) continue;
                  // Same-row spacing check: at least ROW_SPACING
                  // chord between this and the previously chosen
                  // same-row doc.
                  const tooCloseToSibling = chosen.some(prev => {
                    const chord = 2 * radius * Math.sin(Math.abs(cand - prev) / 2);
                    return chord < MIN_DIST_DOC;
                  });
                  if (tooCloseToSibling) continue;
                  chosen.push(cand);
                }

                // If we couldn't find enough clearance-respecting
                // angles on this row, fall through to the next outer
                // row with the remaining docs. The extra ring distance
                // naturally widens the angular gaps and re-opens slots.
                for (const theta of chosen) {
                  const id = newSiblings[placed];
                  positions[id] = {
                    x: parentPos.x + Math.cos(theta) * radius,
                    y: parentPos.y + Math.sin(theta) * radius,
                  };
                  placedPolar.push({ R: radius, theta });
                  placed += 1;
                  if (placed >= newSiblings.length) break;
                }

                row += 1;
                if (row > 30) break;  // safety
              }
              arcCache = positions;
              docArcByParent.set(parentId, arcCache);
            }
            const arcPos = arcCache[nd.id];
            if (arcPos) {
              pos[nd.id] = { x: arcPos.x, y: arcPos.y, vx: 0, vy: 0 };
              pinned[nd.id] = true;
            } else {
              // Shouldn't happen, but fall back to the original
              // 90-unit fan so we never crash.
              const siblings = list.filter(s => !prevPositions[s.id]).length || 1;
              const myIdx    = list.filter(s => !prevPositions[s.id]).indexOf(nd);
              const a = (2 * Math.PI * myIdx) / siblings - Math.PI / 2;
              pos[nd.id] = {
                x: parentPos.x + Math.cos(a) * 90,
                y: parentPos.y + Math.sin(a) * 90,
                vx: 0, vy: 0,
              };
            }
          } else {
            // ── Non-document new node (unchanged from the previous
            // commit). Original symmetric-fan seed at 90 viewBox-units
            // from the parent.
            const siblings = list.filter(s => !prevPositions[s.id]).length || 1;
            const myIdx    = list.filter(s => !prevPositions[s.id]).indexOf(nd);
            const a = (2 * Math.PI * myIdx) / siblings - Math.PI / 2;
            pos[nd.id] = {
              x: parentPos.x + Math.cos(a) * 90,
              y: parentPos.y + Math.sin(a) * 90,
              vx: 0, vy: 0,
            };
          }
        } else {
          // No parent reference — fall back to the concentric ring.
          const a = (2 * Math.PI * i) / Math.max(n, 1) - Math.PI / 2;
          pos[nd.id] = { x: W / 2 + Math.cos(a) * R, y: H / 2 + Math.sin(a) * R, vx: 0, vy: 0 };
        }
      } else {
        // First paint — concentric initial placement.
        const a = (2 * Math.PI * i) / Math.max(n, 1) - Math.PI / 2;
        pos[nd.id] = { x: W / 2 + Math.cos(a) * R, y: H / 2 + Math.sin(a) * R, vx: 0, vy: 0 };
      }
    });
  }
  // Pin root to centre.
  if (pos["__root__"]) {
    pos["__root__"].x = W / 2;
    pos["__root__"].y = H / 2;
    pinned["__root__"] = true;
  }

  const adj = edges
    .filter(e => pos[e.src] && pos[e.dst] && e.src !== e.dst)
    .map(e => [e.src, e.dst]);

  // Solver constants — softer than the platform knowledge graph because
  // trees are sparser and benefit more from spring forces than repulsion.
  const REP = 35000, SPRING = 0.04, LEN = 200, CENTER = 0.003, DAMP = 0.88, MAXV = 55;

  // Incremental layouts use FAR fewer iterations than first-paint runs
  // — we only need the new nodes to find their resting place, and
  // pinned nodes don't move anyway. Fewer iterations also means the
  // solver can't perturb the global equilibrium even if it wanted to.
  const isIncremental = !!prevPositions;
  const iters = isIncremental
    ? Math.min(120, 60 + nodes.length)
    : Math.min(320, 180 + nodes.length * 3);

  for (let it = 0; it < iters; it++) {
    const cool = 1 - (it / iters) * 0.7;

    // Pairwise repulsion. We iterate over every ordered pair (i<j) and
    // compute the force regardless of which side is pinned — pinned
    // nodes must still EMIT repulsion (otherwise a freshly-expanded doc
    // can't be pushed away by a pinned ancestor on a different ring and
    // settles on top of it, e.g. a doc landing inside the URCS product
    // when its parent v1.0 version is expanded). Force APPLICATION is
    // gated on each side's pinned state separately.
    for (let i = 0; i < nodes.length; i++) {
      const A = pos[nodes[i].id];
      const aPinned = pinned[nodes[i].id];
      for (let j = i + 1; j < nodes.length; j++) {
        const B = pos[nodes[j].id];
        const bPinned = pinned[nodes[j].id];
        if (aPinned && bPinned) continue;  // neither would move
        const dx = A.x - B.x, dy = A.y - B.y;
        const d2 = dx * dx + dy * dy || 0.01;
        const d = Math.sqrt(d2);
        const f = (REP / d2) * cool;
        const fx = (dx / d) * f, fy = (dy / d) * f;
        if (!aPinned) { A.vx += fx; A.vy += fy; }
        if (!bPinned) { B.vx -= fx; B.vy -= fy; }
      }
      if (!aPinned) {
        A.vx += (W / 2 - A.x) * CENTER;
        A.vy += (H / 2 - A.y) * CENTER;
      }
    }
    for (const [s, t] of adj) {
      const A = pos[s], B = pos[t];
      const sPinned = pinned[s], tPinned = pinned[t];
      if (sPinned && tPinned) continue;
      const dx = B.x - A.x, dy = B.y - A.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = SPRING * (d - LEN);
      const fx = (dx / d) * f, fy = (dy / d) * f;
      if (!sPinned) { A.vx += fx; A.vy += fy; }
      if (!tPinned) { B.vx -= fx; B.vy -= fy; }
    }
    for (const nd of nodes) {
      if (pinned[nd.id]) continue;
      const P = pos[nd.id];
      P.vx = clamp(P.vx, -MAXV, MAXV); P.vy = clamp(P.vy, -MAXV, MAXV);
      P.x += P.vx * DAMP; P.y += P.vy * DAMP;
      P.vx *= DAMP; P.vy *= DAMP;
      P.x = clamp(P.x, 40, W - 40); P.y = clamp(P.y, 40, H - 40);
    }
  }

  // ── Overlap-resolution post-pass ─────────────────────────────────
  // After the main force loop has settled, run a separate sweep that
  // GUARANTEES no two nodes are closer than MIN_DIST. For each pair
  // closer than the threshold, we displace them along their shared
  // axis by half the violation each (or all on the unpinned side if
  // one of them is pinned). The pass is repeated until no overlap is
  // found, capped at 80 sweeps to bound worst-case work.
  //
  // This is separate from the force loop so it doesn't fight the
  // equilibrium of well-spaced nodes — it ONLY acts when nodes are
  // genuinely overlapping, which is the case the user reported.
  //
  // MIN_DIST was 105 — not enough room for the node radii themselves.
  // Root is r=26, a domain or hub product can be r=20+, and the SVG
  // glow filter halos extend another ~15 viewBox-units beyond the
  // circle. At MIN_DIST=180 the closest two visible halos can come is
  // 180 - (26+20+15+15) ≈ 104 viewBox-units of empty space — plenty
  // for clean visual separation at any zoom level.
  const MIN_DIST = 180;
  for (let sweep = 0; sweep < 80; sweep++) {
    let anyOverlap = false;
    for (let i = 0; i < nodes.length; i++) {
      const A = pos[nodes[i].id];
      const aPinned = pinned[nodes[i].id];
      for (let j = i + 1; j < nodes.length; j++) {
        const B = pos[nodes[j].id];
        const bPinned = pinned[nodes[j].id];
        if (aPinned && bPinned) continue;
        const dx = A.x - B.x, dy = A.y - B.y;
        const d2 = dx * dx + dy * dy;
        if (d2 >= MIN_DIST * MIN_DIST) continue;
        anyOverlap = true;
        const d = Math.sqrt(d2) || 0.01;
        const overlap = MIN_DIST - d;
        // Unit vector A → A-side push direction.
        const ux = dx / d, uy = dy / d;
        if (aPinned) {
          // Only B moves — push it the full overlap.
          B.x = clamp(B.x - ux * overlap, 40, W - 40);
          B.y = clamp(B.y - uy * overlap, 40, H - 40);
        } else if (bPinned) {
          A.x = clamp(A.x + ux * overlap, 40, W - 40);
          A.y = clamp(A.y + uy * overlap, 40, H - 40);
        } else {
          // Both move — split the displacement equally.
          A.x = clamp(A.x + ux * overlap / 2, 40, W - 40);
          A.y = clamp(A.y + uy * overlap / 2, 40, H - 40);
          B.x = clamp(B.x - ux * overlap / 2, 40, W - 40);
          B.y = clamp(B.y - uy * overlap / 2, 40, H - 40);
        }
      }
    }
    if (!anyOverlap) break;
  }

  const out = {};
  for (const nd of nodes) out[nd.id] = { x: pos[nd.id].x, y: pos[nd.id].y };
  return out;
}

// ── Fit + tween utilities ──────────────────────────────────────────────
// Shared between (a) the first-paint auto-fit that frames the whole tree
// and (b) the on-selection focus tween that frames the root→selected
// spine. Keeping the math in one place means the two motions look
// consistent: same padding rule, same zoom ceiling.

const MIN_K = 0.25;
const MAX_K = 2.2;  // single-node selection caps here so we don't zoom to absurd levels

// Compute a transform {k, x, y} that frames `targets` (an array of
// {x, y} positions in canvas/world coords) inside the SVG viewBox
// W×H with `padding` (fraction) of empty space on each side.
//
// `aspectRatio` (optional) is the actual rendered SVG aspect ratio
// (width/height of the on-screen <svg> element). Because we use
// preserveAspectRatio="xMidYMid meet" the visible area is letterboxed
// to whatever rectangle fits the SVG viewBox aspect (1.6:1) inside the
// container — so if the container is taller-than-wide or wider-than-tall
// than the viewBox, part of the W×H region is off-screen. Passing the
// real aspect ratio lets the fit math use only the visible slice.
//
// If targets is empty (e.g. layout hasn't run yet) we fall back to the
// identity transform — the caller can recover once positions arrive.
function computeFitTransform(targets, padding = 0.12, aspectRatio = null, maxK = MAX_K) {
  if (!targets || targets.length === 0) return { k: 1, x: 0, y: 0 };

  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const p of targets) {
    if (!p) continue;
    if (p.x < minX) minX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.x > maxX) maxX = p.x;
    if (p.y > maxY) maxY = p.y;
  }
  if (!isFinite(minX)) return { k: 1, x: 0, y: 0 };

  // A single-point bbox would divide by zero — give it a small synthetic
  // size so the resulting k stays sane.
  const bboxW = Math.max(maxX - minX, 60);
  const bboxH = Math.max(maxY - minY, 60);

  // With preserveAspectRatio="xMidYMid meet" the SVG ALWAYS shows the
  // full W×H viewBox — letterboxing pads the rendered element with
  // empty space, it does NOT crop content. So the fit math is simple:
  // the bbox just has to fit inside W×H minus padding. The aspectRatio
  // parameter is no longer used for the fit math itself (kept in the
  // signature for forward-compatibility / explicit documentation).
  void aspectRatio;
  const availW = W * (1 - padding * 2);
  const availH = H * (1 - padding * 2);

  const k = clamp(Math.min(availW / bboxW, availH / bboxH), MIN_K, maxK);

  // Centre the bbox in the viewport at the chosen k.
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  const x = W / 2 - cx * k;
  const y = H / 2 - cy * k;

  return { k, x, y };
}

// Compute a transform {k, x, y} that centres `anchor` ({x, y}) in the
// viewport and chooses k so every position in `targets` fits within
// the viewport with `padding` (fraction) on each side. Used for the
// on-selection focus tween: we want the SELECTED node to be at the
// viewport centre, and we want every spine+child node to still be
// visible. Because the anchor is fixed at viewport centre rather than
// at the bbox centre, the bbox is generally off-centre and the
// half-span from the anchor (not from the bbox centre) is what bounds
// k. Math:
//   For each target, dx = target.x - anchor.x, dy = target.y - anchor.y.
//   We need k * |dx| <= viewportHalfW * (1 - padding)
//             and k * |dy| <= viewportHalfH * (1 - padding).
//   So k = min over targets of (halfW*(1-pad)/|dx|, halfH*(1-pad)/|dy|).
//
// `maxK` (default 8) lets the camera zoom in HARD when the spine and
// children are tight — that's how we ensure tiny clusters actually
// fill the viewport instead of sitting in a 60% sub-rect with the
// global MAX_K cap kicking in. The default ceiling is intentionally
// generous so the cap is reached only in degenerate (single-point)
// cases.
function computeAnchoredFitTransform(anchor, targets, padding = 0.12, maxK = 8) {
  if (!anchor) return { k: 1, x: 0, y: 0 };
  if (!targets || targets.length === 0) targets = [anchor];

  const halfW = W / 2;
  const halfH = H / 2;
  const availHalfW = halfW * (1 - padding);
  const availHalfH = halfH * (1 - padding);

  let maxDx = 0, maxDy = 0;
  for (const p of targets) {
    if (!p) continue;
    const dx = Math.abs(p.x - anchor.x);
    const dy = Math.abs(p.y - anchor.y);
    if (dx > maxDx) maxDx = dx;
    if (dy > maxDy) maxDy = dy;
  }
  // If every target coincides with the anchor (degenerate), give it a
  // sane fallback so we don't divide by zero or pin k at MIN_K.
  if (maxDx < 1 && maxDy < 1) { maxDx = 1; maxDy = 1; }

  const kX = maxDx > 0 ? availHalfW / maxDx : Infinity;
  const kY = maxDy > 0 ? availHalfH / maxDy : Infinity;
  const k = clamp(Math.min(kX, kY), MIN_K, maxK);

  // Centre the anchor in the viewport.
  const x = halfW - anchor.x * k;
  const y = halfH - anchor.y * k;
  return { k, x, y };
}

// easeInOutCubic — smooth start/end, modest middle. Standard pick for
// camera tweens; feels more confident than a linear lerp.
function easeInOutCubic(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

// Tween a transform via requestAnimationFrame.
//
// onUpdate receives the interpolated tf on each frame. `signal` is a
// caller-owned `{ cancelled: boolean }` mutable handle — set
// `signal.cancelled = true` and the loop exits cleanly without firing
// further updates. This lets the component cancel a tween when the user
// interrupts with a wheel/drag, or when a new tween is requested before
// the previous one finishes.
function tweenTransform(fromTf, toTf, duration, onUpdate, signal) {
  const start = performance.now();
  function frame(now) {
    if (signal?.cancelled) return;
    const t = Math.min(1, (now - start) / duration);
    const e = easeInOutCubic(t);
    onUpdate({
      k: fromTf.k + (toTf.k - fromTf.k) * e,
      x: fromTf.x + (toTf.x - fromTf.x) * e,
      y: fromTf.y + (toTf.y - fromTf.y) * e,
    });
    if (t < 1) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

// ── Component ─────────────────────────────────────────────────────────

export default function KbScopeGraph({ onScopeReady }) {
  const [docs, setDocs] = useState([]);
  const [products, setProducts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Which version nodes have their documents fanned out. We use a Set so
  // multiple versions can be expanded simultaneously — the user might want
  // to compare docs across versions.
  const [expandedVersions, setExpandedVersions] = useState(() => new Set());

  const [selectedId, setSelectedId] = useState(null);
  const [hoverId, setHoverId] = useState(null);
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  // tf is the SVG <g transform="translate scale"> state. Initial value
  // is identity; the first-paint auto-fit useEffect immediately overrides
  // it once positions are computed. We deliberately do NOT pick a
  // hard-coded zoom level (the old 0.8 felt arbitrary and made everything
  // small) — the auto-fit math decides for us based on actual node bbox.
  const [tf, setTf] = useState({ k: 1, x: 0, y: 0 });
  // Synchronous ref mirror of `tf`. Used by animateTo so we can read
  // the latest transform WITHOUT calling setTf with a functional
  // updater. The previous implementation invoked the rAF tween from
  // INSIDE a setTf functional updater — a state-updater side-effect —
  // which React StrictMode double-invokes, starting two competing rAF
  // loops on every selection change. The duplicate loops then race,
  // each calling setTf at ~60Hz, producing the high-frequency commit
  // storm that triggered "removeChild: not a child of this node"
  // reconciliation crashes when the user clicked a document.
  const tfRef = useRef({ k: 1, x: 0, y: 0 });
  useEffect(() => { tfRef.current = tf; }, [tf]);
  const [full, setFull] = useState(false);

  const svgRef = useRef(null);
  const drag = useRef(null);
  // Active camera tween. We carry a single mutable signal so any new
  // tween or user-initiated input can cancel the previous one without
  // races. tweenRef.current = { cancelled: false }; setting cancelled=true
  // is what stops the rAF loop on the next frame.
  const tweenRef = useRef(null);
  // The auto-fit target computed on first layout. Stashed so the "reset
  // view" button (and the deselect-clears-camera path) can return to it.
  const homeTfRef = useRef(null);

  // ── Initial data load ─────────────────────────────────────────────
  const loadData = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [d, p] = await Promise.all([getDocs(), getProducts()]);
      setDocs(d);
      setProducts(p);
    } catch (e) {
      setError(String(e?.message || e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  // ── Debounced search ──────────────────────────────────────────────
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query.trim().toLowerCase()), 200);
    return () => clearTimeout(t);
  }, [query]);

  // ── Tree + layout ─────────────────────────────────────────────────
  const tree = useMemo(
    () => buildTree(docs, products, expandedVersions),
    [docs, products, expandedVersions],
  );

  // Recompute layout whenever the rendered node set changes. The solver is
  // O(n² × iters) so we keep it off the typing path — only structural
  // changes (data load, expand/collapse) trigger it, not hover/search.
  const [positions, setPositions] = useState({});
  // Tracks whether we've performed the first-paint auto-fit yet. Without
  // this guard the auto-fit useEffect would re-frame the whole tree any
  // time a version is expanded (which re-runs the layout), yanking the
  // camera away from whatever the user was looking at.
  const didInitialFitRef = useRef(false);
  // We carry the previous positions through to the next layout run so
  // existing nodes stay pinned exactly where they were. Reading
  // `positions` directly from state inside the effect would create a
  // dependency cycle (effect → setPositions → effect), so we mirror
  // them into a ref synchronously alongside every setPositions call.
  const positionsRef = useRef({});
  useEffect(() => {
    const seed = didInitialFitRef.current ? positionsRef.current : null;
    const next = computeLayout(tree.nodes, tree.edges, seed);
    positionsRef.current = next;
    setPositions(next);
  }, [tree]);

  // Helper: read the SVG element's actual on-screen aspect ratio so the
  // fit math knows how much of the W×H viewBox is letterboxed. Falls
  // back to the viewBox's intrinsic 1.6:1 ratio if the ref isn't ready
  // yet (first paint pre-mount).
  const readSvgAspect = useCallback(() => {
    const el = svgRef.current;
    if (!el) return W / H;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return W / H;
    return r.width / r.height;
  }, []);

  // viewBoxUnitsPerCssPixel: the SVG renders its W×H=1600×1000 viewBox
  // into the on-screen rectangle, so one CSS pixel maps to W/renderedW
  // viewBox-units. We track this ratio in state (driven by the resize
  // observer below) so label-size math can express "I want N actual CSS
  // pixels on screen" reliably across container sizes and tf.k zoom
  // levels. This is what fixes the "domain text not large enough"
  // complaint — the previous fontSize=14 was in viewBox-units, which
  // visually translates to ~9 CSS pixels in typical container sizes.
  const [vbPerPx, setVbPerPx] = useState(1);
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const update = () => {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) return;
      // The on-screen viewBox is scaled to fit (preserveAspectRatio=meet),
      // so the effective scale is min(renderedW/W, renderedH/H). We pick
      // whichever side is binding — that's the conversion factor for
      // viewBox-units to CSS pixels along the visible axis.
      const scale = Math.min(r.width / W, r.height / H);
      // viewBoxUnits per CSS pixel = 1 / scale.
      setVbPerPx(scale > 0 ? 1 / scale : 1);
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // cssPx(n): convert a desired CSS-pixel size to a fontSize value
  // that, when rendered through the <g transform="scale(tf.k)"> chain,
  // lands at exactly n CSS pixels on the user's screen. Used for the
  // "constant on-screen" treatment of domain and spine labels so they
  // stay legible regardless of zoom or container width.
  const cssPx = useCallback((n) => (n * vbPerPx) / Math.max(tf.k, 0.01), [vbPerPx, tf.k]);

  // Hard fit rules — single source of truth for camera framing.
  //
  // The two rules:
  //   1. Home (initial) — the entire tree fits in the viewport.
  //   2. Selection — the selected node sits at the viewport centre AND
  //      the spine (root → selected) + immediate children fit.
  //
  // "Fits" in both cases means: every relevant node centre lies inside
  // the viewport with a padding band on each side. The padding values
  // are intentionally generous because:
  //   - The fit math uses node CENTRES; the nodes themselves (radius
  //     ~14–26 viewBox-units) extend beyond their centres.
  //   - Labels hang further to the right of their nodes.
  //   - Without the padding, at high k the nodes/labels visibly overflow
  //     the visible canvas — which the user reads as "zoomed too much."
  //
  // HOME_FIT_MAX_K=1.0: never zoom IN past natural scale for the home
  // view. Small KBs (where the bbox is small relative to the viewport)
  // stay at k=1 and sit in the middle of the canvas; only large KBs
  // ever scale down. This is what the user means by "fits perfectly" —
  // not "fills aggressively."
  //
  // FOCUS_MAX_K=2.5: lets the selection view zoom in enough to truly
  // fill the viewport with the spine + subtree. The previous 1.5 cap
  // left noticeable empty bands around the cluster because the math
  // wanted ~1.6-2.0 for a typical Domain selection — the cap held it
  // back. 2.5 is generous enough that the cap only matters for very
  // deep, very tight selections (a single doc), in which case the cap
  // is a sanity guard.
  //
  // FOCUS_PADDING=0.08: tight breathing band. Just enough that node
  // circles and labels don't clip at the viewport edge — anything
  // larger leaves the user looking at empty margins instead of the
  // scope they picked.
  const HOME_FIT_PADDING = 0.15;
  const FOCUS_PADDING = 0.08;
  const HOME_FIT_MAX_K = 1.0;
  const FOCUS_MAX_K = 2.5;

  // Stash the positions that produced the initial fit. We use these
  // (and only these) for subsequent re-fits triggered by container
  // resize — never the *current* positions, because the layout solver
  // re-runs on every tree restructure (version expand/collapse) and
  // produces a slightly different spread each time. Reading the current
  // positions on each fit caused the home zoom to drift outward every
  // time the user clicked a node and then KB to reset — the cycle
  // re-fit against a more spread-out layout, yielding a smaller k.
  const initialFitPositionsRef = useRef(null);

  // Home fit — fits the entire tree exactly. No MAX_K cap: small KBs
  // are allowed to zoom IN so they fill the viewport (the previous cap
  // at 1.0 meant tiny KBs sat clustered in the middle with vast empty
  // margins, which the user read as "the graph is not visible").
  useEffect(() => {
    if (didInitialFitRef.current) return;
    if (!tree.nodes.length) return;
    const allHavePositions = tree.nodes.every(n => positions[n.id]);
    if (!allHavePositions) return;
    const pts = tree.nodes.map(n => positions[n.id]);
    const target = computeFitTransform(pts, HOME_FIT_PADDING, readSvgAspect(), HOME_FIT_MAX_K);
    initialFitPositionsRef.current = pts;
    homeTfRef.current = target;
    setTf(target);
    didInitialFitRef.current = true;
  }, [tree, positions, readSvgAspect]);

  // Container resize: re-derive the home fit using the SAME positions
  // that produced the initial fit (stored in initialFitPositionsRef).
  // This is what gives the user the "exact same home zoom every time"
  // behaviour they asked for — no drift across click cycles. The only
  // legitimate reason to recompute the home fit is a real container
  // size change.
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const pts = initialFitPositionsRef.current;
      if (!pts || !pts.length) return;
      homeTfRef.current = computeFitTransform(pts, HOME_FIT_PADDING, readSvgAspect(), HOME_FIT_MAX_K);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [readSvgAspect]);

  // ── Lookup tables ─────────────────────────────────────────────────
  const nodeById = useMemo(() => {
    const m = new Map();
    for (const n of tree.nodes) m.set(n.id, n);
    return m;
  }, [tree]);

  const childrenById = useMemo(() => {
    const m = new Map();
    for (const e of tree.edges) {
      if (!m.has(e.src)) m.set(e.src, []);
      m.get(e.src).push(e.dst);
    }
    return m;
  }, [tree]);

  const selected = selectedId ? nodeById.get(selectedId) : null;

  // Path root → selected, used to highlight the spine of the chosen scope.
  const selectedPath = useMemo(() => {
    if (!selected) return new Set();
    const set = new Set();
    let cur = selected;
    while (cur) {
      set.add(cur.id);
      cur = cur.parent ? nodeById.get(cur.parent) : null;
    }
    return set;
  }, [selected, nodeById]);

  // Subtree of the selected node — every descendant gets a soft highlight
  // so the user sees what's within their chosen scope.
  const selectedSubtree = useMemo(() => {
    if (!selected) return new Set();
    const set = new Set();
    const stack = [selected.id];
    while (stack.length) {
      const id = stack.pop();
      if (set.has(id)) continue;
      set.add(id);
      const kids = childrenById.get(id);
      if (kids) for (const k of kids) stack.push(k);
    }
    return set;
  }, [selected, childrenById]);

  // Immediate children of the selected node — gets a distinct softer
  // glow so it's obvious which nodes the user can drill into next. This
  // is a strict subset of selectedSubtree (only depth-1 descendants),
  // and we treat it differently in the render layer.
  //
  // At idle (no user selection), we treat the ROOT as the effective
  // selection for child-glow purposes — so Domains light up as the
  // user's invited entry points. The root itself isn't drawn with the
  // selected glow because there's no real selection; it just acts as the
  // implicit "you are here" anchor so the next-step affordance is clear
  // even before the user picks anything.
  const selectedChildren = useMemo(() => {
    const anchorId = selected ? selected.id : "__root__";
    const kids = childrenById.get(anchorId);
    return kids ? new Set(kids) : new Set();
  }, [selected, childrenById]);

  // Search matches — include matches by document name even when the doc
  // node hasn't been fanned out yet, so the user can find a doc without
  // pre-expanding its version. Matched-but-collapsed versions get the
  // amber ring on the version node and a counter in the right panel.
  const searchMatches = useMemo(() => {
    const out = { nodes: new Set(), hiddenDocsByVersion: new Map(), count: 0 };
    if (!debouncedQuery) return out;
    for (const n of tree.nodes) {
      if ((n.label || "").toLowerCase().includes(debouncedQuery)) {
        out.nodes.add(n.id);
        out.count++;
      }
    }
    // Also scan hidden docs (those whose version isn't expanded).
    for (const [verId, list] of tree.docsByVersion) {
      if (expandedVersions.has(verId)) continue;
      const hits = list.filter(d =>
        (d.name || d.filename || d.id || "").toLowerCase().includes(debouncedQuery),
      );
      if (hits.length) {
        out.hiddenDocsByVersion.set(verId, hits);
        out.nodes.add(verId); // light up the version node too
        out.count += hits.length;
      }
    }
    return out;
  }, [debouncedQuery, tree, expandedVersions]);

  // ── Camera tween wiring ───────────────────────────────────────────
  // cancelTween: aborts the active rAF loop (if any). Called at the top
  // of every new tween, and also from user-initiated wheel/drag so a
  // mid-flight tween yields to direct manipulation.
  const cancelTween = useCallback(() => {
    if (tweenRef.current) tweenRef.current.cancelled = true;
    tweenRef.current = null;
  }, []);

  // animateTo: queues a new tween towards `target` (a full tf object).
  // Cancels any active tween first so the camera always moves from its
  // CURRENT position — never snaps. Duration defaults to 350ms.
  //
  // IMPORTANT: the rAF loop is started OUTSIDE setTf. Calling setTf
  // with a functional updater here would run the updater twice under
  // StrictMode, spawning two competing rAF loops per call — the cause
  // of the doc-click reconciler crash. Read the latest tf from
  // tfRef.current, which is kept in sync by the useEffect alongside
  // the setTf declaration.
  const animateTo = useCallback((target, duration = 350) => {
    cancelTween();
    const signal = { cancelled: false };
    tweenRef.current = signal;
    tweenTransform(tfRef.current, target, duration, (next) => {
      if (signal.cancelled) return;
      setTf(next);
    }, signal);
  }, [cancelTween]);

  // ── Focus-on-selection tween ──────────────────────────────────────
  // Hard rule when a node is selected:
  //   1. The selected node sits at the viewport CENTRE.
  //   2. The camera zoom is set so the entire spine (root → selected)
  //      AND the entire subtree below the selected node (every
  //      descendant, all the way to the leaves) fit inside the viewport,
  //      with a breathing band.
  // When selection is cleared, tween back to the home fit.
  //
  // selectedSubtree already gives us the full descendant set (BFS to
  // leaves), so we use that instead of just direct children.
  //
  // We use computeAnchoredFitTransform here (not computeFitTransform)
  // because the latter centres on the bbox, not on the anchor — that
  // would leave the selected node off-centre whenever the spine extends
  // asymmetrically from it.
  useEffect(() => {
    if (!homeTfRef.current) return;
    if (!selectedId) {
      animateTo(homeTfRef.current);
      return;
    }
    if (!selected) return;
    const anchor = positions[selectedId];
    if (!anchor) return;

    const targets = [];
    for (const id of selectedPath) {
      if (positions[id]) targets.push(positions[id]);
    }
    for (const id of selectedSubtree) {
      // selectedSubtree includes the selected node itself; the spine
      // loop above already pushed it, so this just adds descendants.
      if (id === selectedId) continue;
      if (positions[id]) targets.push(positions[id]);
    }
    animateTo(computeAnchoredFitTransform(anchor, targets, FOCUS_PADDING, FOCUS_MAX_K));
  // selected/selectedPath/selectedSubtree/positions are deliberately
  // not in the deps — we only want this to fire on a genuine selection
  // change, not on every layout micro-tick.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  // ── Interactions ──────────────────────────────────────────────────
  const onPickNode = useCallback((nd) => {
    // Root click resets the picker to its initial state every time:
    //   1. Selection cleared.
    //   2. Every expanded version collapsed.
    //   3. Camera explicitly tweened back to the home auto-fit.
    // Step 3 is explicit (rather than relying on the selectedId effect
    // to fire) so the reset works even when there was NO selection in
    // the first place — e.g. the user has only panned/zoomed away from
    // the initial view. Without the explicit tween, clicking KB in that
    // state would feel like a no-op.
    if (nd.type === "root") {
      setSelectedId(null);
      setExpandedVersions(new Set());
      if (homeTfRef.current) animateTo(homeTfRef.current);
      return;
    }
    // Version click handling:
    //
    //   - If the currently selected node is a DESCENDANT of this
    //     version (e.g. the user has drilled into one of its docs
    //     and is now clicking the version itself to re-focus on it),
    //     we treat the click as "re-select the version" — keep it
    //     expanded, just move selection up. This is the natural
    //     "go back to the version" gesture and avoids collapsing
    //     docs the user might still want to see.
    //
    //   - Otherwise, if the version is already expanded, we toggle:
    //     collapse it and, if it was the active selection, move
    //     selection up to its parent product so the camera stays in
    //     context (clearing selection would tween home and feel like
    //     the whole graph "snapped shut").
    //
    //   - If neither (version is not expanded), fall through to the
    //     default below: select it AND expand it.
    // Special case: clicking the SAME version twice in a row (already
    // selected AND already expanded) collapses it. Preserves the
    // "two-click to fold" gesture users expect on a tree node. After
    // the collapse we move selection up to the parent product so the
    // camera stays in context instead of snapping home.
    if (nd.type === "version"
        && selectedId === nd.id
        && expandedVersions.has(nd.id)) {
      setExpandedVersions(prev => {
        const next = new Set(prev);
        next.delete(nd.id);
        return next;
      });
      setSelectedId(nd.parent || null);
      return;
    }

    // Otherwise: select the picked node, and PRUNE expandedVersions
    // so only versions on the new selection's ancestor chain stay
    // expanded. Any other version that was previously open folds up
    // — this is the rule the user asked for: clicking any domain,
    // product, or version (or a doc) anywhere in the graph collapses
    // every unrelated expanded version. Docs from foreign versions
    // never linger on the canvas after the user has moved focus
    // somewhere else.
    setSelectedId(nd.id);

    setExpandedVersions(prev => {
      const next = new Set();
      // Walk up the new selection's ancestor chain and carry forward
      // any ancestor version that was already expanded.
      let cur = nd;
      while (cur) {
        if (cur.type === "version" && prev.has(cur.id)) next.add(cur.id);
        cur = cur.parent ? nodeById.get(cur.parent) : null;
      }
      // If the clicked node IS a version, auto-expand it (matches the
      // previous "first click on a version expands it" behaviour).
      if (nd.type === "version") next.add(nd.id);
      return next;
    });
  }, [expandedVersions, selectedId, nodeById, animateTo]);

  const toggleExpandVersion = useCallback((verId) => {
    setExpandedVersions(prev => {
      const next = new Set(prev);
      if (next.has(verId)) next.delete(verId);
      else next.add(verId);
      return next;
    });
  }, []);

  // ── Pan / zoom / drag — same model as KnowledgeGraph.jsx ──────────
  // All direct-manipulation inputs (wheel, pan, drag) cancel any active
  // camera tween so the user always wins over the animation. Without this,
  // a mid-flight tween fights the user's scroll and the camera judders.
  const toGraph = (evt) => {
    const r = svgRef.current.getBoundingClientRect();
    const vx = ((evt.clientX - r.left) / r.width) * W;
    const vy = ((evt.clientY - r.top) / r.height) * H;
    return { x: (vx - tf.x) / tf.k, y: (vy - tf.y) / tf.k, vx, vy };
  };
  const onWheel = (e) => {
    e.preventDefault();
    cancelTween();
    const p = toGraph(e);
    const k2 = clamp(tf.k * (e.deltaY < 0 ? 1.15 : 0.87), MIN_K, 5);
    setTf({ k: k2, x: p.vx - p.x * k2, y: p.vy - p.y * k2 });
  };
  const onDownNode = (e, nd) => {
    e.stopPropagation();
    cancelTween();
    drag.current = { type: "node", id: nd.id, moved: false, nodeRef: nd };
  };
  const onDownBg = (e) => {
    cancelTween();
    drag.current = { type: "pan", sx: e.clientX, sy: e.clientY, ox: tf.x, oy: tf.y, moved: false };
  };
  const onMove = (e) => {
    // Snapshot the drag state at the TOP of the handler. The
    // setPositions / setTf functional updaters below run
    // asynchronously during React's reducer phase; if `onUp` fires
    // (mouse released) between the setter call and the updater
    // actually running, `drag.current` becomes null and the original
    // code would throw "Cannot read properties of null (reading
    // 'id')" / 'ox'. Capturing the needed fields in local variables
    // here makes the updaters closure-stable, independent of any
    // later mutation of `drag.current`.
    const d = drag.current;
    if (!d) return;
    d.moved = true;
    if (d.type === "node") {
      const p = toGraph(e);
      const id = d.id;
      // Mirror into positionsRef so the next layout run sees the dragged
      // location as the node's "pinned" position rather than the stale
      // pre-drag one.
      positionsRef.current = {
        ...positionsRef.current,
        [id]: { x: p.x, y: p.y },
      };
      setPositions(pp => ({ ...pp, [id]: { x: p.x, y: p.y } }));
    } else {
      const r = svgRef.current.getBoundingClientRect();
      const dx = ((e.clientX - d.sx) / r.width) * W;
      const dy = ((e.clientY - d.sy) / r.height) * H;
      const ox = d.ox, oy = d.oy;
      setTf(t => ({ ...t, x: ox + dx, y: oy + dy }));
    }
  };
  const onUp = () => {
    // Distinguish a pure click (no drag motion) from a drag-release — only
    // pure clicks should change selection so users can freely reposition
    // nodes without losing their current scope pick. A pure background
    // click (pan-type, no movement) is a deliberate no-op: empty-canvas
    // clicks must NOT clear the selection or reset the camera. The user
    // explicitly asked for the "dead zone" feeling here — only the KB
    // root node resets the picker; the background never does.
    const d = drag.current;
    drag.current = null;
    if (d?.type === "node" && !d.moved && d.nodeRef) onPickNode(d.nodeRef);
  };
  // Smart reset: returns to whichever frame is most useful in context.
  // With a selection active, "reset" means re-frame the spine (handy if
  // the user manually panned away after picking). With no selection,
  // return to the home auto-fit. Both paths tween instead of snapping
  // so the motion reads as intentional.
  const resetView = useCallback(() => {
    if (selected && positions[selected.id]) {
      const targets = [];
      for (const id of selectedPath) {
        if (positions[id]) targets.push(positions[id]);
      }
      for (const id of selectedSubtree) {
        if (id === selected.id) continue;
        if (positions[id]) targets.push(positions[id]);
      }
      animateTo(computeAnchoredFitTransform(positions[selected.id], targets, FOCUS_PADDING, FOCUS_MAX_K));
      return;
    }
    if (homeTfRef.current) animateTo(homeTfRef.current);
  }, [animateTo, selected, selectedPath, selectedSubtree, positions]);

  // Esc — first press clears the active selection (with a camera tween
  // back to home); second press exits fullscreen. Lets the user back out
  // of mistakes without reaching for the mouse.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== "Escape") return;
      if (selectedId) { setSelectedId(null); return; }
      if (full) setFull(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId, full]);

  // ── Confirm — build the scope tuple from the selected node ────────
  // The scope object shape matches KbDrillGraph.confirm() exactly so
  // KbChatPanel.handleScopeReady doesn't need to know which picker is
  // mounted.
  const confirm = useCallback(() => {
    if (!selected || selected.type === "root") return;
    const s = {
      product_id:    null,
      domain:        null,
      spec_version:  "",
      parent_doc_id: null,
      _productName:  null,
      _documentName: null,
    };
    // Walk root-ward, filling each level as we encounter it.
    let cur = selected;
    while (cur) {
      const d = cur.data || {};
      if (cur.type === "domain"   && s.domain        === null) s.domain        = d.domain        || null;
      if (cur.type === "product") {
        if (s.product_id   === null) s.product_id   = d.product_id   || null;
        if (s._productName === null) s._productName = d._productName || null;
      }
      if (cur.type === "version"  && s.spec_version  === "")   s.spec_version  = d.spec_version  || "";
      if (cur.type === "document") {
        if (s.parent_doc_id  === null) s.parent_doc_id  = d.parent_doc_id  || null;
        if (s._documentName  === null) s._documentName  = d._documentName  || null;
      }
      cur = cur.parent ? nodeById.get(cur.parent) : null;
    }
    onScopeReady?.(s);
  }, [selected, nodeById, onScopeReady]);

  // ── Label visibility heuristic ────────────────────────────────────
  // Three-tier opacity model so the user's eye is drawn to the
  // relevant labels without losing the descendant scope context:
  //
  //   FULL (1)    — labels the user is meant to engage with right now:
  //                 the selected node itself, the spine (root → selected),
  //                 IMMEDIATE children of the selected node (the next
  //                 candidates to drill into), hovered nodes, search
  //                 matches, root and every domain (always-visible
  //                 entry points).
  //   DIM  (0.30) — labels in the selected subtree that are deeper
  //                 than the immediate children. The user can see what's
  //                 under their pick, but those labels don't compete
  //                 with the foreground.
  //   HIDDEN (0)  — everything else at idle (deep nodes outside the
  //                 selection), unless the user has manually zoomed
  //                 past 1.1x — at that point we show everything.
  const labelOpacity = (nd) => {
    if (nd.id === selectedId) return 1;
    if (nd.id === hoverId) return 1;
    if (searchMatches.nodes.has(nd.id)) return 1;
    if (nd.type === "root" || nd.type === "domain") return 1;
    if (selectedPath.has(nd.id)) return 1;
    if (selectedChildren.has(nd.id)) return 1;
    if (selectedSubtree.has(nd.id)) return 0.30;
    if (tf.k >= 1.1) return 1;
    return 0;
  };

  // Node radius scales with subtree size (so big domains/products look
  // heavier) but is clamped so document nodes aren't invisible and root
  // doesn't dominate. Spine nodes (root → selected) get a 1.35× bump at
  // render time so the user's chosen path visibly dominates — this is a
  // pure visual concern; the underlying layout positions don't move.
  const radiusFor = (nd) => {
    let r;
    if (nd.type === "root")          r = 26;
    else if (nd.type === "document") r = 7;
    else {
      const base = nd.type === "domain" ? 14 : nd.type === "product" ? 12 : 10;
      const bump = Math.log2(Math.max(1, nd.count)) * 1.4;
      r = clamp(base + bump, 8, 26);
    }
    if (selectedPath.has(nd.id)) r *= 1.35;
    return r;
  };

  // Font size for node labels.
  //
  // EVERY label is sized in CSS pixels via cssPx(), so every visible
  // label is comfortably readable without the user having to manually
  // zoom in. cssPx(N) compensates for both the camera zoom (tf.k) AND
  // the on-screen meet-scale of the SVG container, so the rendered
  // text always lands at exactly N CSS pixels on screen regardless of
  // how zoomed-out the canvas is.
  //
  // The per-layer targets preserve a visual hierarchy:
  //   - Spine (root → selected) — 15px, slightly heavier than peers so
  //     the user's chosen path reads as the focus.
  //   - Domain — 14px, the user's main entry points.
  //   - Product — 13px.
  //   - Version — 12px.
  //   - Document — 11px (smallest, since they only appear when a
  //     version is expanded and are typically many of them).
  // All values pass through cssPx() so manual zoom-in DOESN'T blow them
  // up; manual zoom-out DOESN'T shrink them. They are always exactly
  // the target on-screen height.
  const labelFontSize = (nd) => {
    if (selectedPath.has(nd.id)) return cssPx(42);
    switch (nd.type) {
      case "domain":   return cssPx(38);
      case "product":  return cssPx(34);
      case "version":  return cssPx(30);
      case "document": return cssPx(28);
      default:         return cssPx(34);
    }
  };

  // ── Render ────────────────────────────────────────────────────────
  const totalDocs = docs.length;
  const totalDomains = useMemo(
    () => tree.nodes.filter(n => n.type === "domain").length,
    [tree.nodes],
  );

  // Bottom-hint text — derived from current state. Mirrors the goal-
  // oriented hint pattern the old drill-down used (per-level "click X to
  // see Y" pills). Each branch is intentionally action-oriented so the
  // user always sees what their NEXT move could be — including the
  // "or Chat with this scope" option so they don't think they have to
  // drill all the way.
  const bottomHint = useMemo(() => {
    if (debouncedQuery && searchMatches.count > 0) {
      const n = searchMatches.count;
      return `${n} match${n !== 1 ? "es" : ""} highlighted — click any to scope to it`;
    }
    if (debouncedQuery && searchMatches.count === 0) {
      return `No matches for "${debouncedQuery}"`;
    }
    if (!selected) {
      return "Click a domain to start narrowing your scope";
    }
    if (selected.type === "domain") {
      return `${selected.label} selected — click a product to narrow, or Chat with this scope`;
    }
    if (selected.type === "product") {
      return `${selected.label} selected — click a version, or Chat with this scope`;
    }
    if (selected.type === "version") {
      return `${selected.label} selected — click a document, or Chat with this scope`;
    }
    if (selected.type === "document") {
      return "All set — hit Chat with this scope to start your conversation";
    }
    return "";
  }, [selected, debouncedQuery, searchMatches.count]);

  // Auto-fade the bottom hint after a quiet stretch so it's not perpetual
  // noise for power users. Any state change that affects the text resets
  // the timer. The hint never fully disappears — it fades to 0.4 — so
  // a quick glance still reveals it.
  const [hintFaded, setHintFaded] = useState(false);
  useEffect(() => {
    setHintFaded(false);
    const t = setTimeout(() => setHintFaded(true), 6000);
    return () => clearTimeout(t);
  }, [bottomHint]);

  return (
    <div className={
      full
        ? "fixed inset-0 z-[100] bg-white flex flex-col"
        : "flex-1 flex flex-col min-h-0 bg-gradient-to-br from-slate-50 to-white"
    }>
      {/* ── Toolbar ── */}
      <div className="flex-shrink-0 flex items-center gap-2 px-4 py-2 border-b border-gray-200 bg-white">
        <div className="flex items-center gap-2 font-semibold text-gray-900">
          <Database size={16} className="text-indigo-600" />
          <span className="text-sm">Scope</span>
        </div>
        <span className="text-[11px] text-gray-400">
          {totalDomains} domain{totalDomains !== 1 ? "s" : ""} · {totalDocs} doc{totalDocs !== 1 ? "s" : ""}
        </span>

        <div className="flex items-center gap-1 ml-2 border border-gray-200 rounded-md px-2 py-1 bg-white">
          <Search size={12} className="text-gray-400" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search any level…"
            aria-label="Search the scope graph"
            className="text-xs outline-none w-48 bg-transparent placeholder:text-gray-300"
          />
          {query && (
            <button
              type="button"
              onClick={() => setQuery("")}
              className="text-gray-300 hover:text-gray-600 cursor-pointer"
              aria-label="Clear search"
            >
              <X size={11} />
            </button>
          )}
        </div>

        {debouncedQuery && (
          <span className="text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded-full px-2 py-0.5">
            {searchMatches.count} match{searchMatches.count !== 1 ? "es" : ""}
          </span>
        )}

        <button
          type="button"
          onClick={() => { productsCache = null; kbCache = null; loadData(); }}
          title="Reload from server"
          className="p-1.5 rounded-md hover:bg-gray-100 text-gray-500 cursor-pointer"
        >
          <RefreshCw size={13} className={loading ? "animate-spin" : ""} />
        </button>

        <button
          type="button"
          onClick={() => setFull(f => !f)}
          title={full ? "Exit fullscreen (Esc)" : "Fullscreen"}
          className="p-1.5 rounded-md hover:bg-gray-100 text-gray-600 cursor-pointer ml-auto"
        >
          {full ? <Minimize2 size={13} /> : <Maximize2 size={13} />}
        </button>
      </div>

      {/* ── Body: canvas + right rail ── */}
      <div className="flex-1 flex overflow-hidden min-h-0">
        {/* Canvas */}
        <div className="flex-1 relative overflow-hidden">
          {error && (
            <div className="absolute top-3 left-3 z-20 flex items-center gap-2 px-3 py-1.5 bg-red-50 border border-red-200 rounded-md text-xs text-red-700">
              <AlertTriangle size={13} /> {error}
            </div>
          )}

          {loading && (
            <div className="absolute inset-0 z-10 flex items-center justify-center">
              <Loader2 size={22} className="animate-spin text-indigo-400" />
            </div>
          )}

          {!loading && tree.nodes.length <= 1 && (
            <div className="absolute inset-0 flex flex-col items-center justify-center text-gray-400 text-sm gap-2">
              <Globe2 size={28} className="text-gray-300" />
              <span>No approved documents in the Knowledge Base yet.</span>
              <span className="text-[11px]">Upload documents from the Upload tab to populate the graph.</span>
            </div>
          )}

          {/* Zoom controls. Each button cancels any active camera tween
              so the manual nudge isn't fighting an in-flight animation. */}
          <div className="absolute bottom-4 left-4 z-10 flex flex-col gap-1">
            <button
              type="button"
              onClick={() => { cancelTween(); setTf(t => ({ ...t, k: clamp(t.k * 1.25, MIN_K, 5) })); }}
              className="w-8 h-8 rounded-md bg-white border border-gray-300 text-gray-700 shadow-sm hover:bg-gray-50 cursor-pointer"
              title="Zoom in"
            >＋</button>
            <button
              type="button"
              onClick={() => { cancelTween(); setTf(t => ({ ...t, k: clamp(t.k * 0.8, MIN_K, 5) })); }}
              className="w-8 h-8 rounded-md bg-white border border-gray-300 text-gray-700 shadow-sm hover:bg-gray-50 cursor-pointer"
              title="Zoom out"
            >－</button>
            <button
              type="button"
              onClick={resetView}
              title={selected ? "Re-frame selected scope" : "Reset view"}
              className="w-8 h-8 rounded-md bg-white border border-gray-300 text-gray-600 shadow-sm hover:bg-gray-50 flex items-center justify-center cursor-pointer"
            >
              <Crosshair size={13} />
            </button>
          </div>

          {/* Bottom-centre context hint. Tells the user what their NEXT
              meaningful move is in the current state. Sits above the
              SVG (z-10) and centred horizontally with a left offset
              that clears the bottom-left zoom controls. pointer-events-
              none so clicks pass through to nodes/canvas below it.
              ALWAYS mounted (visibility controlled by opacity) so the
              sibling order against the <svg> below stays stable across
              renders — avoids a class of React reconciliation crashes
              where conditional siblings shifting position mid-tween
              left React's fiber tree out of sync with the DOM. */}
          <div
            className={`absolute bottom-5 left-1/2 -translate-x-1/2 z-10 pointer-events-none transition-opacity duration-300`}
            style={{
              opacity: (bottomHint && !loading && tree.nodes.length > 1)
                ? (hintFaded ? 0.4 : 1)
                : 0,
            }}
          >
            {/* Pill styling intentionally muted so it reads as a soft
                hint, not a foreground element. The previous treatment
                (font-medium + text-indigo-900 + shadow-sm + fully-
                opaque border) competed with the graph for attention;
                user asked to dim it. New treatment: nearly-transparent
                background so the graph behind shows through, very
                faint border, no shadow, and neutral grey text
                (text-gray-400) at normal weight so the text itself
                stays clearly readable. The fade-after-6s timer
                (hintFaded ? 0.4 : 1) above still applies on top of
                this baseline. */}
            <div className="px-3 py-1.5 rounded-full bg-white/25 backdrop-blur-[2px] border border-gray-200/30">
              <span className="text-[11px] font-normal text-gray-400 select-none whitespace-nowrap">
                {bottomHint || "\u00A0"}
              </span>
            </div>
          </div>

          {tree.nodes.length > 1 && (
          <svg
            ref={svgRef}
            viewBox={`0 0 ${W} ${H}`}
            preserveAspectRatio="xMidYMid meet"
            className="w-full h-full"
            style={{ cursor: drag.current?.type === "pan" ? "grabbing" : "default" }}
            onWheel={onWheel}
            onMouseDown={onDownBg}
            onMouseMove={onMove}
            onMouseUp={onUp}
            onMouseLeave={onUp}
          >
            {/* Radial-gradient halo paint servers.
                Unlike <filter>, gradients have no per-element graphics
                buffer — they're a static paint server referenced by
                URL. Switching a node's halo opacity 0↔1 commits as a
                plain attribute change, with zero browser-side
                teardown. No reconciliation race risk.
                Each gradient runs from the layer fill colour at high
                alpha at the centre, softly fading to fully transparent
                at the edge. Painted as the FILL of a large halo circle
                around each glowing node, this gives a real soft-edge
                glow — no concentric rings, no white outline. */}
            <defs>
              {Object.entries(LAYER).map(([type, L]) => (
                <radialGradient
                  key={`halo-${type}`}
                  id={`halo-${type}`}
                  cx="50%" cy="50%" r="50%"
                  fx="50%" fy="50%"
                >
                  {/* Bright core — the node's own colour at high alpha,
                      with the alpha falloff starting partway out so
                      the centre reads as a solid bloom rather than a
                      faint smudge. */}
                  <stop offset="0%"   stopColor={L.color} stopOpacity="0.55" />
                  <stop offset="35%"  stopColor={L.color} stopOpacity="0.40" />
                  <stop offset="70%"  stopColor={L.color} stopOpacity="0.14" />
                  <stop offset="100%" stopColor={L.color} stopOpacity="0" />
                </radialGradient>
              ))}
            </defs>
            <g transform={`translate(${tf.x} ${tf.y}) scale(${tf.k})`}>
              {/* Edges first so nodes sit on top */}
              {tree.edges.map((e, i) => {
                const a = positions[e.src], b = positions[e.dst];
                if (!a || !b) return null;
                const onSpine = selectedPath.has(e.src) && selectedPath.has(e.dst);
                const inSubtree = selectedSubtree.has(e.src) && selectedSubtree.has(e.dst);
                const hot = hoverId && (e.src === hoverId || e.dst === hoverId);
                // Edge colour comes from the CHILD layer (dst side of
                // the edge under buildTree's edge contract — see the
                // edges.push({ src: parentId, dst: childId }) calls).
                // KB→Domain takes the domain's indigo, Domain→Product
                // takes the product's amber, Product→Version takes
                // the version's teal, Version→Document takes the
                // document's rose. The edge therefore reads as the
                // child's "incoming" connection, so each ring of
                // outgoing lines shares the colour of the ring it
                // arrives at.
                const srcType = nodeById.get(e.src)?.type;
                const dstType = nodeById.get(e.dst)?.type;
                const stroke = dstType ? LAYER[dstType].color : "#e5e7eb";
                // Stroke widths kept thin so lines read as connectors,
                // not features. Spine stays slightly bolder so the
                // user can still see the chosen path at a glance.
                const sw = onSpine ? 2 : hot ? 1.2 : 0.7;
                // Edge opacity model — pulled WAY down across the
                // board so connections register as "yes, related"
                // without competing with the nodes for the user's
                // attention. The relative ordering is preserved:
                // spine/subtree under selection are still the most
                // prominent, the root↔domain skeleton at idle is
                // still the next most visible, and deeper idle
                // connections are the faintest.
                let opacity;
                if (selected) {
                  opacity = (onSpine || inSubtree) ? 0.55 : 0.06;
                } else {
                  const skeleton =
                    (srcType === "root" && dstType === "domain") ||
                    (srcType === "domain" && dstType === "root");
                  opacity = skeleton ? 0.40 : 0.18;
                }
                return (
                  <line
                    key={`e-${i}`}
                    x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                    stroke={stroke} strokeWidth={sw}
                    opacity={opacity}
                  />
                );
              })}

              {/* Nodes */}
              {tree.nodes.map(nd => {
                const p = positions[nd.id];
                if (!p) return null;
                const L = LAYER[nd.type];
                const r = radiusFor(nd);
                const isSel = nd.id === selectedId;
                const isChildOfSelected = selectedChildren.has(nd.id);
                const isMatch = searchMatches.nodes.has(nd.id);
                const inSubtree = selectedSubtree.has(nd.id);
                const onSpine = selectedPath.has(nd.id);
                const isRoot = nd.type === "root";

                // Always use the full label — no truncation. The SVG
                // <title> element below provides a native tooltip with
                // the full name on hover for every node type.
                const label = nd.label || "";

                // Halo visibility — drives an ALWAYS-MOUNTED gradient-
                // filled circle (rendered below). The gradient
                // (defined in <defs> at the top of the <svg>) uses
                // the node's own LAYER colour with alpha falloff,
                // giving a soft-edge bloom that reads as a real glow.
                // Selected vs child-of-selected is distinguished by
                // halo size and intensity.
                const haloVisible = (isSel || isChildOfSelected);

                // Node opacity model — mirrors edge model above so the
                // canvas has one consistent visual story:
                //   selection active + on spine/in subtree → 1
                //   selection active + elsewhere           → 0.10
                //   no selection + root/domain             → 1
                //   no selection + product/version/document → 0.35
                let opacity;
                if (selected) {
                  opacity = (onSpine || inSubtree) ? 1 : 0.10;
                } else {
                  opacity = (nd.type === "root" || nd.type === "domain") ? 1 : 0.35;
                }

                return (
                  <g
                    key={nd.id}
                    transform={`translate(${p.x},${p.y})`}
                    opacity={opacity}
                    style={{ cursor: "grab" }}
                    onMouseDown={(e) => onDownNode(e, nd)}
                    onMouseEnter={() => setHoverId(nd.id)}
                    onMouseLeave={() => setHoverId(null)}
                  >
                    {/* Native SVG tooltip — shows the full node name on
                        hover for every node type (domain, product,
                        version, document). Browsers render this as a
                        system tooltip so it works without any extra
                        positioning logic. */}
                    <title>{nd.label || ""}{nd.count > 0 ? ` (${nd.count} doc${nd.count !== 1 ? "s" : ""})` : ""}</title>
                    {/* Invisible enlarged hit target so small nodes
                        (especially documents) are selectable when the
                        cursor is nearby, not just exactly on the dot. */}
                    <circle
                      r={Math.max(r + 12, nd.type === "document" ? 22 : 18)}
                      fill="transparent"
                      style={{ cursor: "grab" }}
                    />
                    {/* Search-match ring — always mounted, opacity-
                        toggled to avoid mount/unmount churn. */}
                    <circle
                      r={r + 9}
                      fill="none"
                      stroke="#f59e0b"
                      strokeWidth={2.5}
                      opacity={isMatch ? 1 : 0}
                    />
                    {/* Modern soft-edge glow — a single large filled
                        circle whose paint is a radial gradient that
                        runs from the node's own colour (at moderate
                        alpha) at the centre to fully transparent at
                        the edge. Looks like a real bloom of light
                        radiating outward from the node, with no
                        visible rings, banding, or hard outlines.
                        The selected node gets a noticeably bigger
                        and brighter halo than child-of-selected:
                          - radius: r + (24 or 16) vs r + (16 or 11)
                          - intensity (controlled by opacity)
                        Both share the same per-layer gradient so the
                        glow colour matches the node colour. Always
                        mounted; opacity 0 when neither selected nor
                        a child of selected. */}
                    <circle
                      r={r + (isSel ? 24 : 16)}
                      fill={`url(#halo-${nd.type})`}
                      opacity={haloVisible ? (isSel ? 1 : 0.75) : 0}
                      pointerEvents="none"
                    />
                    <circle
                      r={r}
                      fill={L.color}
                    />
                    {/* Internal "KB" label — ALWAYS mounted, invisible
                        for non-root via opacity=0. Keeping it
                        unconditional eliminates the asymmetric
                        sibling-pair (this + external label below)
                        that React could otherwise reconcile
                        inconsistently. */}
                    <text
                      textAnchor="middle"
                      dominantBaseline="central"
                      fontSize={cssPx(32)}
                      fill="#ffffff"
                      fontWeight={700}
                      opacity={isRoot ? 1 : 0}
                      className="select-none pointer-events-none"
                    >
                      KB
                    </text>
                    {/* External label — ALWAYS mounted; invisible for
                        root (which uses the KB inner label above) and
                        for any non-spine/non-subtree node at low zoom.
                        Same opacity-only-toggle rationale as every
                        other DOM-stable element in this <g>. */}
                    <text
                      x={r + 5}
                      y={4}
                      fontSize={labelFontSize(nd)}
                      fill="#1f2937"
                      fontWeight={onSpine ? 600 : 400}
                      opacity={isRoot ? 0 : labelOpacity(nd)}
                      className="select-none pointer-events-none"
                      style={{ paintOrder: "stroke", stroke: "#ffffff", strokeWidth: 3 }}
                    >
                      {label}
                    </text>
                  </g>
                );
              })}
            </g>
          </svg>
          )}
        </div>

        {/* ── Right rail: scope summary + actions ── */}
        <div className="w-72 border-l border-gray-200 bg-white flex flex-col flex-shrink-0 overflow-hidden">
          <ScopeSummary
            selected={selected}
            nodeById={nodeById}
            expandedVersions={expandedVersions}
            toggleExpandVersion={toggleExpandVersion}
            onConfirm={confirm}
            onClear={() => setSelectedId(null)}
            childrenById={childrenById}
            onPickNode={onPickNode}
            searchMatches={searchMatches}
            tree={tree}
            debouncedQuery={debouncedQuery}
          />
        </div>
      </div>
    </div>
  );
}

// ── Right-rail summary panel ───────────────────────────────────────────
// Shows the resolved 4-tuple, the action button, and either the matched
// search hits (when searching) or the children of the selected node (when
// browsing). Stays mounted across selection changes so position/scroll is
// preserved.

function ScopeSummary({
  selected, nodeById, expandedVersions, toggleExpandVersion,
  onConfirm, onClear, childrenById, onPickNode,
  searchMatches, tree, debouncedQuery,
}) {
  // Resolve the scope tuple from the selected node by walking root-ward.
  const resolved = useMemo(() => {
    const out = { domain: null, product: null, version: null, document: null };
    if (!selected || selected.type === "root") return out;
    let cur = selected;
    while (cur) {
      if (cur.type === "domain"   && !out.domain)   out.domain   = cur.label;
      if (cur.type === "product"  && !out.product)  out.product  = cur.label;
      if (cur.type === "version"  && !out.version)  out.version  = cur.label;
      if (cur.type === "document" && !out.document) out.document = cur.label;
      cur = cur.parent ? nodeById.get(cur.parent) : null;
    }
    return out;
  }, [selected, nodeById]);

  const canChat = !!selected && selected.type !== "root";
  const depth = !selected ? 0
    : selected.type === "domain"   ? 1
    : selected.type === "product"  ? 2
    : selected.type === "version"  ? 3
    : selected.type === "document" ? 4
    : 0;

  // Children of the selected node — drives the "narrow further" list when
  // not searching.
  const childList = useMemo(() => {
    if (!selected) return [];
    const ids = childrenById.get(selected.id) || [];
    return ids.map(id => tree.nodes.find(n => n.id === id)).filter(Boolean);
  }, [selected, childrenById, tree]);

  // Hidden doc matches the search found under collapsed versions.
  const hiddenDocList = useMemo(() => {
    const out = [];
    for (const [verId, list] of searchMatches.hiddenDocsByVersion) {
      const ver = tree.nodes.find(n => n.id === verId);
      for (const doc of list) {
        out.push({ ver, doc });
      }
    }
    return out;
  }, [searchMatches, tree]);

  return (
    <>
      {/* Scope tuple */}
      <div className="p-4 border-b border-gray-200">
        <div className="flex items-center justify-between mb-2">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
            Resolved Scope
          </span>
          {selected && (
            <button
              type="button"
              onClick={onClear}
              className="text-gray-300 hover:text-gray-600 cursor-pointer"
              title="Clear selection"
            >
              <X size={12} />
            </button>
          )}
        </div>

        <ScopeRow icon={Globe2}    layer="domain"   label="Domain"   value={resolved.domain} />
        <ScopeRow icon={Package}   layer="product"  label="Product"  value={resolved.product} />
        <ScopeRow icon={GitBranch} layer="version"  label="Version"  value={resolved.version} />
        <ScopeRow icon={FileText}  layer="document" label="Document" value={resolved.document} />

        <button
          type="button"
          onClick={onConfirm}
          disabled={!canChat}
          className={[
            "mt-4 w-full flex items-center justify-center gap-1.5 px-3 py-2 rounded-md text-xs font-medium transition shadow-sm cursor-pointer",
            canChat
              ? "bg-indigo-600 text-white hover:bg-indigo-700"
              : "bg-gray-100 text-gray-400 cursor-not-allowed",
          ].join(" ")}
        >
          <CheckCircle2 size={13} />
          Chat with this scope
        </button>

        {canChat && (
          <p className="mt-2 text-[10px] text-gray-400 leading-snug">
            Retrieval will be filtered to {depth === 1 ? "this whole domain" : depth === 2 ? "every version of this product" : depth === 3 ? "every doc in this version" : "this single document"}.
            {depth < 4 && " Drill deeper for tighter precision."}
          </p>
        )}
        {!canChat && (
          <p className="mt-2 text-[10px] text-gray-400 leading-snug">
            Click any node — domain, product, version, or document — to set it as your retrieval scope.
          </p>
        )}
      </div>

      {/* Search results (collapsed-doc matches) — shown only while searching */}
      {debouncedQuery && hiddenDocList.length > 0 && (
        <div className="p-4 border-b border-gray-200 bg-amber-50/40">
          <div className="text-[11px] font-semibold text-amber-700 mb-2">
            {hiddenDocList.length} document match{hiddenDocList.length !== 1 ? "es" : ""} in collapsed versions
          </div>
          <div className="space-y-1 max-h-52 overflow-auto">
            {hiddenDocList.slice(0, 40).map(({ ver, doc }, i) => (
              <button
                key={`hd-${doc.id}-${i}`}
                type="button"
                onClick={() => {
                  // Expand the parent version so the doc node materialises
                  // in the next tree rebuild. The graph will then layout +
                  // render it; the user can click that node to actually
                  // pick it as scope. Two-step is intentional: we want the
                  // search hit to reveal context, not silently jump scope.
                  toggleExpandVersion(ver.id);
                }}
                className="block w-full text-left text-xs px-2 py-1.5 rounded border border-amber-200 bg-white hover:border-amber-400 cursor-pointer"
              >
                <div className="font-medium text-gray-800 truncate">
                  {highlightMatch(doc.name || doc.filename || doc.id, debouncedQuery).map((s, j) =>
                    s.match
                      ? <mark key={j} className="bg-amber-200 text-gray-900 rounded px-0.5">{s.text}</mark>
                      : <span key={j}>{s.text}</span>,
                  )}
                </div>
                <div className="text-[10px] text-gray-400 truncate">
                  in {ver.label}
                </div>
              </button>
            ))}
            {hiddenDocList.length > 40 && (
              <div className="text-[10px] text-amber-600 px-2 pt-1">
                Showing first 40 matches — refine your search to narrow further.
              </div>
            )}
          </div>
        </div>
      )}

      {/* Browse children of selected node */}
      {selected && childList.length > 0 && !debouncedQuery && (
        <div className="p-4 border-b border-gray-200 flex-1 overflow-auto">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-500 mb-2">
            Narrow further ({childList.length})
          </div>
          <div className="space-y-1">
            {childList.slice(0, 80).map(c => {
              const L = LAYER[c.type];
              return (
                <button
                  key={c.id}
                  type="button"
                  onClick={() => onPickNode(c)}
                  className="flex items-center gap-2 w-full text-left text-xs text-gray-700 hover:text-indigo-700 px-1.5 py-1 rounded hover:bg-indigo-50 cursor-pointer"
                  title={c.label}
                >
                  <span
                    className="w-2.5 h-2.5 rounded-full flex-shrink-0"
                    style={{ background: L.color }}
                  />
                  <span className="truncate flex-1">{c.label}</span>
                  {c.count > 0 && c.type !== "document" && (
                    <span className="text-[10px] text-gray-400 flex-shrink-0">{c.count}</span>
                  )}
                </button>
              );
            })}
            {childList.length > 80 && (
              <div className="text-[10px] text-gray-400 px-1.5 pt-1">
                Showing first 80. Use search to find specific items.
              </div>
            )}
          </div>
        </div>
      )}

      {/* Idle hint */}
      {!selected && !debouncedQuery && (
        <div className="p-4 text-[11px] text-gray-400 leading-relaxed">
          <p className="mb-2">
            The graph shows your Knowledge Base as a tree:
          </p>
          <ul className="space-y-1 ml-3 list-disc">
            <li><span className="text-indigo-600 font-medium">Domain</span> — top-level area (HR, Tech, …)</li>
            <li><span className="text-amber-600 font-medium">Product</span> — system within a domain</li>
            <li><span className="text-teal-600 font-medium">Version</span> — spec revision</li>
            <li><span className="text-rose-600 font-medium">Document</span> — individual file</li>
          </ul>
          <p className="mt-3">
            Pick a node at the depth you&apos;re confident about — you don&apos;t need to drill all the way to a document.
          </p>
        </div>
      )}

      {/* Version expand/collapse hint */}
      {selected?.type === "version" && (
        <div className="px-4 py-2 border-t border-gray-100 flex items-center justify-between bg-gray-50">
          <span className="text-[10px] text-gray-500">
            {expandedVersions.has(selected.id) ? "Documents shown" : "Documents hidden"}
          </span>
          <button
            type="button"
            onClick={() => toggleExpandVersion(selected.id)}
            className="text-[10px] text-indigo-600 hover:text-indigo-800 cursor-pointer font-medium"
          >
            {expandedVersions.has(selected.id) ? "Collapse" : "Expand documents"}
          </button>
        </div>
      )}
    </>
  );
}

// ScopeRow — single row in the right-rail's resolved-scope summary.
// `Icon` is the layer's lucide component (Globe2/Package/…); we render it
// inline rather than destructure-with-rename so the eslint config (which
// doesn't load react/jsx-uses-vars) doesn't flag it as unused.
function ScopeRow(props) {
  const Icon = props.icon;
  const { layer, label, value } = props;
  const L = LAYER[layer];
  const set = value != null && value !== "";
  return (
    <div className="flex items-center gap-2 py-1">
      <Icon
        size={12}
        className="flex-shrink-0"
        style={{ color: set ? L.color : "#cbd5e1" }}
      />
      <span className={`text-[10px] uppercase tracking-wide w-16 flex-shrink-0 ${set ? "text-gray-500" : "text-gray-300"}`}>
        {label}
      </span>
      <span className={`text-xs truncate ${set ? "text-gray-800 font-medium" : "text-gray-300 italic"}`}>
        {set ? value : "any"}
      </span>
    </div>
  );
}
