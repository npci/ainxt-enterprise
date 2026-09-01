// SPDX-License-Identifier: Apache-2.0
import { useMemo, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  Handle,
  Position,
  MarkerType,
} from "@xyflow/react";
import { X } from "lucide-react";
import "@xyflow/react/dist/style.css";

// ── Read-only workflow renderer ───────────────────────────────
// Self-contained mini React Flow canvas for previewing a submitted workflow in
// the Inbox approval panel. Deliberately does NOT reuse ABStudio's Build Studio
// node components — those are coupled to three Zustand stores and [data-ac]
// CSS, which don't exist in this app. These presentational nodes read purely
// from `data` and render nothing interactive.

const NODE_STYLES = {
  start:           { badge: "Start",      color: "#059669", bg: "#ecfdf5", border: "#a7f3d0" },
  end:             { badge: "End",        color: "#dc2626", bg: "#fef2f2", border: "#fecaca" },
  agent:           { badge: "Agent",      color: "#7c3aed", bg: "#f5f3ff", border: "#ddd6fe" },
  condition:       { badge: "Condition",  color: "#d97706", bg: "#fffbeb", border: "#fde68a" },
  subflow:         { badge: "Subflow",    color: "#2563eb", bg: "#eff6ff", border: "#bfdbfe" },
  loop:            { badge: "Loop",       color: "#0891b2", bg: "#ecfeff", border: "#a5f3fc" },
  evaluation_gate: { badge: "Eval Gate",  color: "#be185d", bg: "#fdf2f8", border: "#fbcfe8" },
};

const DEFAULT_STYLE = { badge: "Node", color: "#475569", bg: "#f8fafc", border: "#e2e8f0" };

// ── Shared agent detail (system prompt + tools + skills) ──────
// Rendered both inside the diagram overlay (click a node) and inline in the
// Inbox summary list (expand a row). Reads purely from a node's `data`.
export function AgentDetail({ data }) {
  const instructions = data?.instructions || data?.systemPrompt || "";
  const tools = Array.isArray(data?.tools) ? data.tools : [];
  const skills = Array.isArray(data?.skills) ? data.skills : [];

  return (
    <div className="space-y-3 text-xs">
      <div>
        <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-400 mb-1">
          System prompt
        </div>
        {instructions ? (
          <pre className="whitespace-pre-wrap break-words text-gray-700 bg-gray-50 border border-gray-100 rounded-md p-2 max-h-56 overflow-auto leading-relaxed font-sans">
            {instructions}
          </pre>
        ) : (
          <div className="text-gray-400 italic">No system prompt set.</div>
        )}
      </div>

      <div>
        <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-400 mb-1">
          Tools {tools.length > 0 && `(${tools.length})`}
        </div>
        {tools.length > 0 ? (
          <ul className="space-y-1">
            {tools.map((t, i) => {
              const nm = t?.name || (typeof t === "string" ? t : `tool ${i + 1}`);
              const desc = t?.description || "";
              return (
                <li key={`${nm}-${i}`} className="flex flex-col">
                  <span className="font-medium text-gray-700">{nm}</span>
                  {desc && <span className="text-gray-500">{desc}</span>}
                </li>
              );
            })}
          </ul>
        ) : (
          <div className="text-gray-400 italic">No tools attached.</div>
        )}
      </div>

      <div>
        <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-400 mb-1">
          Skills {skills.length > 0 && `(${skills.length})`}
        </div>
        {skills.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {skills.map((s, i) => {
              const nm = s?.name || (typeof s === "string" ? s : `skill ${i + 1}`);
              return (
                <span key={`${nm}-${i}`} className="px-2 py-0.5 rounded-full bg-gray-100 text-gray-600 border border-gray-200">
                  {nm}
                </span>
              );
            })}
          </div>
        ) : (
          <div className="text-gray-400 italic">No skills attached.</div>
        )}
      </div>
    </div>
  );
}

function PreviewNode({ data, type }) {
  const s = NODE_STYLES[type] || DEFAULT_STYLE;
  const label = data?.name || data?.label || s.badge;
  const tools = Array.isArray(data?.tools) ? data.tools : [];
  const skills = Array.isArray(data?.skills) ? data.skills : [];
  const showHandleTop = type !== "start";
  const showHandleBottom = type !== "end";

  return (
    <div
      style={{
        minWidth: 140,
        maxWidth: 220,
        padding: "8px 12px",
        borderRadius: 10,
        background: s.bg,
        border: `1.5px solid ${s.border}`,
        fontSize: 12,
        cursor: type === "agent" ? "pointer" : "default",
      }}
    >
      {showHandleTop && <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />}
      <div style={{ fontSize: 10, fontWeight: 600, color: s.color, textTransform: "uppercase", letterSpacing: 0.4 }}>
        {s.badge}
      </div>
      <div style={{ fontWeight: 600, color: "#111827", marginTop: 2, wordBreak: "break-word" }}>
        {label}
      </div>
      {type === "agent" && (
        <div style={{ marginTop: 4, color: "#6b7280", fontSize: 10 }}>
          {tools.length > 0 && <span>{tools.length} tool{tools.length !== 1 ? "s" : ""}</span>}
          {tools.length > 0 && skills.length > 0 && <span> · </span>}
          {skills.length > 0 && <span>{skills.length} skill{skills.length !== 1 ? "s" : ""}</span>}
          {(tools.length > 0 || skills.length > 0) && <span> · </span>}
          <span style={{ color: "#7c3aed" }}>click to inspect</span>
        </div>
      )}
      {showHandleBottom && <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />}
    </div>
  );
}

// One node component covers every type — the badge/color is chosen from `type`.
const NODE_TYPES = Object.keys(NODE_STYLES).reduce((acc, t) => {
  acc[t] = PreviewNode;
  return acc;
}, {});

export default function WorkflowPreview({ graphData }) {
  const nodes = useMemo(() => (Array.isArray(graphData?.nodes) ? graphData.nodes : []), [graphData]);
  // Normalize edges so they render clearly regardless of how Build Studio saved
  // them: drop sourceHandle/targetHandle (our simple nodes expose a single
  // default handle each — keeping the author's handle IDs would orphan the
  // edge), and give every edge a visible stroke + arrowhead.
  const edges = useMemo(() => {
    const raw = Array.isArray(graphData?.edges) ? graphData.edges : [];
    return raw.map((e) => ({
      id: e.id || `${e.source}-${e.target}`,
      source: e.source,
      target: e.target,
      type: "smoothstep",
      animated: false,
      style: { stroke: "#94a3b8", strokeWidth: 1.5 },
      markerEnd: { type: MarkerType.ArrowClosed, color: "#94a3b8", width: 18, height: 18 },
    }));
  }, [graphData]);

  const [selected, setSelected] = useState(null); // clicked agent node

  if (nodes.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-sm text-gray-400">
        No nodes to display
      </div>
    );
  }

  const onNodeClick = (_e, node) => {
    // Only agent nodes carry a system prompt / tools worth inspecting.
    if (node?.type === "agent") setSelected(node);
  };

  return (
    <div style={{ position: "relative", width: "100%", height: "100%" }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        onNodeClick={onNodeClick}
        fitView
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnDrag
        zoomOnScroll
        minZoom={0.2}
        defaultEdgeOptions={{ type: "smoothstep" }}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={16} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>

      {selected && (
        <div
          className="absolute top-2 right-2 bottom-2 w-72 bg-white border border-gray-200 rounded-lg shadow-lg flex flex-col z-10"
        >
          <div className="flex items-center justify-between px-3 py-2 border-b border-gray-100">
            <div className="min-w-0">
              <div className="text-[10px] font-semibold uppercase tracking-wide text-violet-600">Agent</div>
              <div className="text-sm font-semibold text-gray-800 truncate">
                {selected.data?.name || selected.data?.label || "Agent"}
              </div>
            </div>
            <button
              onClick={() => setSelected(null)}
              className="text-gray-400 hover:text-gray-600 flex-shrink-0 cursor-pointer"
              aria-label="Close"
            >
              <X size={15} />
            </button>
          </div>
          <div className="p-3 overflow-auto">
            <AgentDetail data={selected.data} />
          </div>
        </div>
      )}
    </div>
  );
}
